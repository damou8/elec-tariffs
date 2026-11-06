"""Tests of the regulated prices from data.gouv.fr."""

import itertools
from datetime import date

import aiohttp
import pytest
from aiohttp import web

from elec_tariffs import (
    ContractKind,
    TrvClient,
    TrvFormatError,
    TrvServerError,
    parse_trv_csv,
)
from elec_tariffs.models import Tranche
from elec_tariffs.sources.trv import TRV_URLS

from .conftest import FakeServer, load_fixture


def test_tempo_prices_in_force() -> None:
    periods = parse_trv_csv(load_fixture("trv_tempo.csv"), ContractKind.TEMPO, 6)
    current = periods[-1]
    assert current.start == date(2026, 8, 1)
    assert current.end is None
    assert current.fixed_per_year == pytest.approx(189.98, abs=0.01)
    assert current.prices == {
        Tranche.BLUE_HC: pytest.approx(0.1356),
        Tranche.BLUE_HP: pytest.approx(0.1654),
        Tranche.WHITE_HC: pytest.approx(0.1536),
        Tranche.WHITE_HP: pytest.approx(0.1921),
        Tranche.RED_HC: pytest.approx(0.1615),
        Tranche.RED_HP: pytest.approx(0.7295),
    }


def test_periods_follow_each_other() -> None:
    periods = parse_trv_csv(load_fixture("trv_tempo.csv"), ContractKind.TEMPO, 6)
    for before, after in itertools.pairwise(periods):
        assert before.end is not None
        assert before.end < after.start
    assert all(period.end for period in periods[:-1])


def test_powers_not_offered_are_skipped() -> None:
    # Tempo 6 kVA rows of 2016 are empty: the offer started later at this power.
    periods = parse_trv_csv(load_fixture("trv_tempo.csv"), ContractKind.TEMPO, 6)
    assert periods[0].start > date(2016, 8, 1)
    assert parse_trv_csv(load_fixture("trv_tempo.csv"), ContractKind.TEMPO, 7) == []


def test_base_and_hphc_prices() -> None:
    base = parse_trv_csv(load_fixture("trv_base.csv"), ContractKind.BASE, 6)[-1]
    assert base.start == date(2026, 8, 1)
    assert base.fixed_per_year == pytest.approx(190.81)
    assert base.prices == {Tranche.BASE: pytest.approx(0.2001)}
    hphc = parse_trv_csv(load_fixture("trv_hphc.csv"), ContractKind.HPHC, 6)[-1]
    assert hphc.prices == {
        Tranche.HC: pytest.approx(0.1589),
        Tranche.HP: pytest.approx(0.2142),
    }


def test_byte_order_mark_is_ignored() -> None:
    text = "﻿" + load_fixture("trv_base.csv")
    assert parse_trv_csv(text, ContractKind.BASE, 6)


def test_missing_column() -> None:
    with pytest.raises(TrvFormatError, match="PART_VARIABLE_HCBleu_TTC"):
        parse_trv_csv(load_fixture("trv_base.csv"), ContractKind.TEMPO, 6)


def test_unreadable_value() -> None:
    text = (
        "DATE_DEBUT;DATE_FIN;P_SOUSCRITE;PART_FIXE_HT;PART_FIXE_TTC;"
        "PART_VARIABLE_HT;PART_VARIABLE_TTC\r\n"
        "01/08/2026;;6;143,28;190,81;0,1361;n/a\r\n"
    )
    with pytest.raises(TrvFormatError, match="01/08/2026"):
        parse_trv_csv(text, ContractKind.BASE, 6)


def urls(server: FakeServer) -> dict[ContractKind, str]:
    return {kind: f"{server.url}/{kind}.csv" for kind in ContractKind}


def test_short_row() -> None:
    text = (
        "DATE_DEBUT;DATE_FIN;P_SOUSCRITE;PART_FIXE_HT;PART_FIXE_TTC;"
        "PART_VARIABLE_HT;PART_VARIABLE_TTC\r\n"
        "01/08/2026;;6;143,28;190,81\r\n"
    )
    with pytest.raises(TrvFormatError, match="01/08/2026"):
        parse_trv_csv(text, ContractKind.BASE, 6)


async def test_client_downloads_and_parses(
    session: aiohttp.ClientSession, server: FakeServer
) -> None:
    server.text("GET", "/base.csv", load_fixture("trv_base.csv"))
    client = TrvClient(session, urls=urls(server))
    periods = await client.async_price_periods(ContractKind.BASE, 6)
    assert periods[-1].prices == {Tranche.BASE: pytest.approx(0.2001)}


async def test_client_server_error(
    session: aiohttp.ClientSession, server: FakeServer
) -> None:
    server.text("GET", "/tempo.csv", "Unavailable", status=503)
    with pytest.raises(TrvServerError):
        await TrvClient(session, urls=urls(server)).async_price_periods(
            ContractKind.TEMPO, 6
        )


async def test_client_not_utf8(
    session: aiohttp.ClientSession, server: FakeServer
) -> None:
    async def latin1(_: web.Request) -> web.Response:
        return web.Response(body="Électricité".encode("latin-1"))

    server.add("GET", "/base.csv", latin1)
    with pytest.raises(TrvFormatError, match="UTF-8"):
        await TrvClient(session, urls=urls(server)).async_price_periods(
            ContractKind.BASE, 6
        )


async def test_client_unreachable(
    session: aiohttp.ClientSession, closed_url: str
) -> None:
    client = TrvClient(session, urls=dict.fromkeys(ContractKind, closed_url))
    with pytest.raises(TrvServerError):
        await client.async_price_periods(ContractKind.HPHC, 6)


def test_default_urls_are_data_gouv() -> None:
    assert all(url.startswith("https://www.data.gouv.fr/") for url in TRV_URLS.values())
