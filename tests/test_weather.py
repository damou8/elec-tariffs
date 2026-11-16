"""Tests of the Open-Meteo outdoor temperature client."""

import json
from datetime import UTC, date, datetime
from typing import Any

import aiohttp
import pytest
from aiohttp import web

from elec_tariffs import (
    OpenMeteoClient,
    WeatherFormatError,
    WeatherRateLimitError,
    WeatherServerError,
    parse_hourly_temperatures,
)
from elec_tariffs.sources.tempo import USER_AGENT

from .conftest import FakeServer, load_fixture

PATH = "/v1/archive"


def recorded() -> Any:
    return json.loads(load_fixture("open_meteo_archive.json"))


def answer(times: list[Any], values: Any) -> dict[str, Any]:
    return {
        "utc_offset_seconds": 0,
        "hourly_units": {"time": "iso8601", "temperature_2m": "°C"},
        "hourly": {"time": times, "temperature_2m": values},
    }


def test_parse_the_recorded_answer() -> None:
    temperatures = parse_hourly_temperatures(recorded())
    assert len(temperatures) == 48
    first = datetime(2026, 1, 4, tzinfo=UTC)
    assert min(temperatures) == first
    assert max(temperatures) == datetime(2026, 1, 5, 23, tzinfo=UTC)
    assert temperatures[first] == recorded()["hourly"]["temperature_2m"][0]


def test_hours_without_a_value_are_left_out() -> None:
    temperatures = parse_hourly_temperatures(
        answer(["2026-01-04T00:00", "2026-01-04T01:00"], [1.5, None])
    )
    assert temperatures == {datetime(2026, 1, 4, tzinfo=UTC): 1.5}


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {**answer([], []), "utc_offset_seconds": 3600},
        {"utc_offset_seconds": 0, "hourly": {}},
        {**answer([], []), "hourly_units": {"temperature_2m": "°F"}},
        answer(["2026-01-04T00:00"], []),
        answer(["2026-01-04T00:00"], "1.5"),
        answer(["yesterday"], [1.5]),
        answer([None], [1.5]),
        answer(["2026-01-04T00:00"], ["1.5"]),
        answer(["2026-01-04T00:00"], [True]),
    ],
)
def test_unexpected_answers_are_refused(payload: Any) -> None:
    with pytest.raises(WeatherFormatError):
        parse_hourly_temperatures(payload)


async def test_ask_in_gmt(session: aiohttp.ClientSession, server: FakeServer) -> None:
    server.json("GET", PATH, recorded())
    client = OpenMeteoClient(session, url=f"{server.url}{PATH}")
    temperatures = await client.async_hourly_temperatures(
        48.9, 2.4, date(2026, 1, 4), date(2026, 1, 5)
    )
    assert len(temperatures) == 48
    [request] = server.calls(PATH)
    assert dict(request.query) == {
        "latitude": "48.9",
        "longitude": "2.4",
        "start_date": "2026-01-04",
        "end_date": "2026-01-05",
        "hourly": "temperature_2m",
        "timezone": "GMT",
    }
    assert request.headers["User-Agent"] == USER_AGENT


async def test_long_ranges_are_asked_in_parts(
    session: aiohttp.ClientSession, server: FakeServer
) -> None:
    server.json("GET", PATH, answer(["2025-01-01T00:00"], [1.0]))
    server.json("GET", PATH, answer(["2026-01-02T00:00"], [2.0]))
    client = OpenMeteoClient(session, url=f"{server.url}{PATH}")
    temperatures = await client.async_hourly_temperatures(
        48.9, 2.4, date(2025, 1, 1), date(2026, 1, 2)
    )
    assert temperatures == {
        datetime(2025, 1, 1, tzinfo=UTC): 1.0,
        datetime(2026, 1, 2, tzinfo=UTC): 2.0,
    }
    ranges = [
        (request.query["start_date"], request.query["end_date"])
        for request in server.calls(PATH)
    ]
    assert ranges == [("2025-01-01", "2026-01-01"), ("2026-01-02", "2026-01-02")]


async def test_an_empty_range_asks_nothing(
    session: aiohttp.ClientSession, server: FakeServer
) -> None:
    client = OpenMeteoClient(session, url=f"{server.url}{PATH}")
    assert (
        await client.async_hourly_temperatures(
            48.9, 2.4, date(2026, 1, 5), date(2026, 1, 4)
        )
        == {}
    )
    assert server.requests == []


@pytest.mark.parametrize(
    ("status", "error"),
    [(429, WeatherRateLimitError), (502, WeatherServerError)],
)
async def test_server_errors(
    session: aiohttp.ClientSession,
    server: FakeServer,
    status: int,
    error: type[Exception],
) -> None:
    server.text("GET", PATH, "busy", status=status)
    client = OpenMeteoClient(session, url=f"{server.url}{PATH}")
    with pytest.raises(error):
        await client.async_hourly_temperatures(
            48.9, 2.4, date(2026, 1, 4), date(2026, 1, 4)
        )


async def test_a_refused_request(
    session: aiohttp.ClientSession, server: FakeServer
) -> None:
    server.json(
        "GET",
        PATH,
        {"error": True, "reason": "End-date must be larger"},
        status=400,
    )
    client = OpenMeteoClient(session, url=f"{server.url}{PATH}")
    with pytest.raises(WeatherFormatError, match="End-date must be larger"):
        await client.async_hourly_temperatures(
            48.9, 2.4, date(2026, 1, 4), date(2026, 1, 4)
        )


async def test_not_json(session: aiohttp.ClientSession, server: FakeServer) -> None:
    server.text("GET", PATH, "<html>")
    client = OpenMeteoClient(session, url=f"{server.url}{PATH}")
    with pytest.raises(WeatherFormatError):
        await client.async_hourly_temperatures(
            48.9, 2.4, date(2026, 1, 4), date(2026, 1, 4)
        )


async def test_unreachable(session: aiohttp.ClientSession, closed_url: str) -> None:
    client = OpenMeteoClient(session, url=closed_url)
    with pytest.raises(WeatherServerError):
        await client.async_hourly_temperatures(
            48.9, 2.4, date(2026, 1, 4), date(2026, 1, 4)
        )


async def test_an_error_answer_that_is_not_an_object(
    session: aiohttp.ClientSession, server: FakeServer
) -> None:
    async def handler(_: web.Request) -> web.Response:
        return web.json_response([], status=404)

    server.add("GET", PATH, handler)
    client = OpenMeteoClient(session, url=f"{server.url}{PATH}")
    with pytest.raises(WeatherFormatError, match="HTTP 404"):
        await client.async_hourly_temperatures(
            48.9, 2.4, date(2026, 1, 4), date(2026, 1, 4)
        )
