"""Regulated prices (Tarif Bleu) published on data.gouv.fr.

Each CSV has one row per price period and subscribed power: start and end dates
(DD/MM/YYYY, an empty end for the prices in force), the yearly fixed part and the
price of each tranche, with and without taxes. Rows of powers not offered at the
time are empty.
"""

import csv
from collections.abc import Mapping
from datetime import date, datetime
from typing import Final

import aiohttp

from elec_tariffs.errors import TrvFormatError, TrvServerError
from elec_tariffs.models import ContractKind, PricePeriod, Tranche

TRV_URLS: Final[Mapping[ContractKind, str]] = {
    ContractKind.BASE: "https://www.data.gouv.fr/fr/datasets/r/c13d05e5-9e55-4d03-bf7e-042a2ade7e49",
    ContractKind.HPHC: "https://www.data.gouv.fr/fr/datasets/r/f7303b3a-93c7-4242-813d-84919034c416",
    ContractKind.TEMPO: "https://www.data.gouv.fr/fr/datasets/r/0c3d1d36-c412-4620-8566-e5cbb4fa2b5a",
}

_PRICE_COLUMNS: Final[Mapping[ContractKind, Mapping[Tranche, str]]] = {
    ContractKind.BASE: {Tranche.BASE: "PART_VARIABLE_TTC"},
    ContractKind.HPHC: {
        Tranche.HC: "PART_VARIABLE_HC_TTC",
        Tranche.HP: "PART_VARIABLE_HP_TTC",
    },
    ContractKind.TEMPO: {
        Tranche.BLUE_HC: "PART_VARIABLE_HCBleu_TTC",
        Tranche.BLUE_HP: "PART_VARIABLE_HPBleu_TTC",
        Tranche.WHITE_HC: "PART_VARIABLE_HCBlanc_TTC",
        Tranche.WHITE_HP: "PART_VARIABLE_HPBlanc_TTC",
        Tranche.RED_HC: "PART_VARIABLE_HCRouge_TTC",
        Tranche.RED_HP: "PART_VARIABLE_HPRouge_TTC",
    },
}
_START: Final = "DATE_DEBUT"
_END: Final = "DATE_FIN"
_POWER: Final = "P_SOUSCRITE"
_FIXED: Final = "PART_FIXE_TTC"
_TIMEOUT: Final = aiohttp.ClientTimeout(total=30)


def _date(value: str) -> date:
    return datetime.strptime(value, "%d/%m/%Y").date()  # noqa: DTZ007


def _number(value: str) -> float:
    return float(value.replace(",", "."))


def parse_trv_csv(text: str, kind: ContractKind, kva: int) -> list[PricePeriod]:
    """Return the price periods of one contract kind and power, oldest first.

    Raise TrvFormatError when a column is missing or a value can't be read.
    """
    reader = csv.DictReader(text.lstrip("﻿").splitlines(), delimiter=";")
    price_columns = _PRICE_COLUMNS[kind]
    required = {_START, _END, _POWER, _FIXED, *price_columns.values()}
    missing = required - set(reader.fieldnames or ())
    if missing:
        msg = f"Missing columns in the {kind} prices: {', '.join(sorted(missing))}"
        raise TrvFormatError(msg)

    periods: list[PricePeriod] = []
    for raw in reader:
        # A short row leaves None in its last columns; extra fields have no name.
        row = {name: (value or "").strip() for name, value in raw.items() if name}
        if row[_POWER] != str(kva) or not row[_FIXED]:
            continue
        try:
            periods.append(
                PricePeriod(
                    start=_date(row[_START]),
                    end=_date(row[_END]) if row[_END] else None,
                    fixed_per_year=_number(row[_FIXED]),
                    prices={
                        tranche: _number(row[column])
                        for tranche, column in price_columns.items()
                    },
                )
            )
        except ValueError as err:
            msg = f"Unreadable {kind} price row starting {row[_START]}: {err}"
            raise TrvFormatError(msg) from err
    return sorted(periods, key=lambda period: period.start)


class TrvClient:
    """Download the regulated prices from data.gouv.fr."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        urls: Mapping[ContractKind, str] = TRV_URLS,
    ) -> None:
        """Use the caller's HTTP session; `urls` replaces the data.gouv.fr files."""
        self._session = session
        self._urls = urls

    async def async_price_periods(
        self, kind: ContractKind, kva: int
    ) -> list[PricePeriod]:
        """Return the price periods of one contract kind and power, oldest first."""
        try:
            async with self._session.get(
                self._urls[kind], timeout=_TIMEOUT
            ) as response:
                response.raise_for_status()
                text = await response.text(encoding="utf-8")
        except UnicodeDecodeError as err:
            msg = f"The {kind} prices are not UTF-8 text"
            raise TrvFormatError(msg) from err
        except (aiohttp.ClientError, TimeoutError) as err:
            msg = f"Could not download the {kind} prices: {err}"
            raise TrvServerError(msg) from err
        return parse_trv_csv(text, kind, kva)
