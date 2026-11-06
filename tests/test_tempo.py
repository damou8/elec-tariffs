"""Tests of the Tempo calendar sources."""

import json
from datetime import date, timedelta
from typing import Any

import aiohttp
import pytest
from aiohttp import web

from elec_tariffs import (
    FallbackTempoSource,
    RteOfficialTempoClient,
    RtePublicTempoClient,
    TempoAuthError,
    TempoColor,
    TempoError,
    TempoFormatError,
    TempoRateLimitError,
    TempoServerError,
    season_bounds,
    season_of,
)
from elec_tariffs.sources.tempo import (
    CALENDAR_PATH,
    TOKEN_PATH,
    USER_AGENT,
    Calendar,
    check_season,
    parse_official_values,
    parse_public_values,
)

from .conftest import FakeServer, load_fixture


def recorded(season: str) -> Any:
    return json.loads(load_fixture(f"rte_tempo_{season}.json"))


def official_payload(calendar: Calendar) -> dict[str, Any]:
    """Build an answer in the format of the official API."""
    values = [
        {
            "start_date": f"{day.isoformat()}T00:00:00+01:00",
            "end_date": f"{(day + timedelta(days=1)).isoformat()}T00:00:00+01:00",
            "value": color.upper(),
            "updated_date": f"{day.isoformat()}T10:20:00+01:00",
        }
        for day, color in sorted(calendar.items())
    ]
    return {"tempo_like_calendars": {"values": values}}


# Seasons.


def test_season_of() -> None:
    assert season_of(date(2025, 8, 31)) == "2024-2025"
    assert season_of(date(2025, 9, 1)) == "2025-2026"
    assert season_of(date(2026, 1, 1)) == "2025-2026"


def test_season_bounds() -> None:
    assert season_bounds("2027-2028") == (date(2027, 9, 1), date(2028, 8, 31))


# Public source: parsing.


@pytest.mark.parametrize("season", ["2024-2025", "2025-2026"])
def test_recorded_seasons(season: str) -> None:
    calendar = parse_public_values(recorded(season))
    check_season(calendar, season)
    colors = list(calendar.values())
    assert len(calendar) == 365
    assert colors.count(TempoColor.BLUE) == 300
    assert colors.count(TempoColor.WHITE) == 43
    assert colors.count(TempoColor.RED) == 22


def test_recorded_colors() -> None:
    calendar = parse_public_values(recorded("2025-2026"))
    assert calendar[date(2025, 12, 29)] is TempoColor.RED
    assert calendar[date(2025, 11, 20)] is TempoColor.WHITE
    assert calendar[date(2026, 2, 10)] is TempoColor.BLUE


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"values": []},
        {"values": {"10/02/2026": "BLUE"}},
        {"values": {"2026-02-10": "PINK"}},
        {"values": {"2026-02-10": 1}},
    ],
)
def test_unexpected_public_format(payload: Any) -> None:
    with pytest.raises(TempoFormatError):
        parse_public_values(payload)


def test_season_with_days_outside() -> None:
    with pytest.raises(TempoFormatError, match="outside"):
        check_season({date(2025, 8, 31): TempoColor.BLUE}, "2025-2026")


@pytest.mark.parametrize(
    ("color", "count"), [(TempoColor.RED, 23), (TempoColor.WHITE, 44)]
)
def test_season_with_too_many_days(color: TempoColor, count: int) -> None:
    first, _ = season_bounds("2025-2026")
    calendar = {first + timedelta(days=offset): color for offset in range(count)}
    with pytest.raises(TempoFormatError, match="Too many"):
        check_season(calendar, "2025-2026")


# Public source: requests.


async def test_public_season(
    session: aiohttp.ClientSession, server: FakeServer
) -> None:
    server.json("GET", "/tempo", recorded("2025-2026"))
    client = RtePublicTempoClient(session, base_url=server.url)
    calendar = await client.async_season("2025-2026")
    assert len(calendar) == 365
    (request,) = server.calls("/tempo")
    assert request.query["season"] == "2025-2026"
    assert request.headers["User-Agent"] == USER_AGENT


async def test_public_calendar_across_seasons(
    session: aiohttp.ClientSession, server: FakeServer
) -> None:
    server.json("GET", "/tempo", recorded("2024-2025"))
    server.json("GET", "/tempo", recorded("2025-2026"))
    client = RtePublicTempoClient(session, base_url=server.url)
    calendar = await client.async_calendar(date(2025, 8, 30), date(2025, 9, 2))
    assert sorted(calendar) == [date(2025, 8, 30) + timedelta(days=n) for n in range(4)]
    assert [call.query["season"] for call in server.calls("/tempo")] == [
        "2024-2025",
        "2025-2026",
    ]


async def test_public_today_and_tomorrow(
    session: aiohttp.ClientSession, server: FakeServer
) -> None:
    server.json("GET", "/tempoLight", json.loads(load_fixture("rte_tempo_light.json")))
    client = RtePublicTempoClient(session, base_url=server.url)
    calendar = await client.async_calendar(date(2026, 11, 6), date(2026, 11, 7))
    assert calendar == {
        date(2026, 11, 6): TempoColor.WHITE,
        date(2026, 11, 7): TempoColor.BLUE,
    }
    assert not server.calls("/tempo")


async def test_public_tomorrow_not_published(
    session: aiohttp.ClientSession, server: FakeServer
) -> None:
    server.json("GET", "/tempoLight", {"values": {"2026-11-06": "WHITE"}})
    server.json("GET", "/tempo", {"values": {"2026-11-06": "WHITE"}})
    client = RtePublicTempoClient(session, base_url=server.url)
    calendar = await client.async_calendar(date(2026, 11, 6), date(2026, 11, 7))
    assert calendar == {date(2026, 11, 6): TempoColor.WHITE}
    assert server.calls("/tempo")[0].query["season"] == "2026-2027"


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (403, TempoFormatError),
        (404, TempoFormatError),
        (429, TempoRateLimitError),
        (502, TempoServerError),
    ],
)
async def test_public_http_errors(
    session: aiohttp.ClientSession,
    server: FakeServer,
    status: int,
    error: type[TempoError],
) -> None:
    server.text("GET", "/tempo", "Error", status=status)
    client = RtePublicTempoClient(session, base_url=server.url)
    with pytest.raises(error):
        await client.async_season("2025-2026")


async def test_public_not_json(
    session: aiohttp.ClientSession, server: FakeServer
) -> None:
    server.text("GET", "/tempoLight", "<html>Maintenance</html>")
    client = RtePublicTempoClient(session, base_url=server.url)
    with pytest.raises(TempoFormatError, match="JSON"):
        await client.async_today_tomorrow()


async def test_public_unreachable(
    session: aiohttp.ClientSession, closed_url: str
) -> None:
    client = RtePublicTempoClient(session, base_url=closed_url)
    with pytest.raises(TempoServerError):
        await client.async_today_tomorrow()


# Official source.


class Clock:
    """A monotonic clock moved by hand."""

    def __init__(self) -> None:
        """Start at zero."""
        self.now = 0.0

    def __call__(self) -> float:
        """Return the current time."""
        return self.now


def official(
    session: aiohttp.ClientSession, server: FakeServer, clock: Clock | None = None
) -> RteOfficialTempoClient:
    return RteOfficialTempoClient(
        session, "my-id", "my-secret", base_url=server.url, clock=clock or Clock()
    )


def token(
    server: FakeServer, value: str = "token-1", expires_in: object = 7200
) -> None:
    server.json(
        "POST",
        TOKEN_PATH,
        {"access_token": value, "token_type": "Bearer", "expires_in": expires_in},
    )


async def test_official_calendar(
    session: aiohttp.ClientSession, server: FakeServer
) -> None:
    token(server)
    days = {date(2026, 1, 5): TempoColor.RED, date(2026, 1, 6): TempoColor.WHITE}
    server.json("GET", CALENDAR_PATH, official_payload(days))
    calendar = await official(session, server).async_calendar(
        date(2026, 1, 5), date(2026, 1, 6)
    )
    assert calendar == days
    (token_call,) = server.calls(TOKEN_PATH)
    assert (
        token_call.headers["Authorization"]
        == "Basic bXktaWQ6bXktc2VjcmV0"  # base64 of my-id:my-secret
    )
    assert token_call.body == "grant_type=client_credentials"
    (call,) = server.calls(CALENDAR_PATH)
    assert call.headers["Authorization"] == "Bearer token-1"
    assert call.query["start_date"] == "2026-01-05T00:00:00+01:00"
    assert call.query["end_date"] == "2026-01-07T00:00:00+01:00"


async def test_official_summer_offset(
    session: aiohttp.ClientSession, server: FakeServer
) -> None:
    token(server)
    server.json("GET", CALENDAR_PATH, official_payload({}))
    await official(session, server).async_calendar(date(2026, 7, 1), date(2026, 7, 1))
    assert server.calls(CALENDAR_PATH)[0].query["start_date"] == (
        "2026-07-01T00:00:00+02:00"
    )


async def test_official_token_is_reused_until_it_expires(
    session: aiohttp.ClientSession, server: FakeServer
) -> None:
    clock = Clock()
    client = official(session, server, clock)
    token(server, "token-1", 3600)
    token(server, "token-2", 3600)
    for _ in range(3):
        server.json("GET", CALENDAR_PATH, official_payload({}))
    day = date(2026, 1, 5)
    await client.async_calendar(day, day)
    clock.now = 3000
    await client.async_calendar(day, day)
    clock.now = 3541
    await client.async_calendar(day, day)
    assert len(server.calls(TOKEN_PATH)) == 2
    assert [call.headers["Authorization"] for call in server.calls(CALENDAR_PATH)] == [
        "Bearer token-1",
        "Bearer token-1",
        "Bearer token-2",
    ]


async def test_official_token_without_lifetime(
    session: aiohttp.ClientSession, server: FakeServer
) -> None:
    clock = Clock()
    client = official(session, server, clock)
    token(server, expires_in="soon")
    await client.async_check_credentials()
    assert client._expires_at == 3600 - 60


async def test_official_refreshes_a_refused_token(
    session: aiohttp.ClientSession, server: FakeServer
) -> None:
    token(server, "token-1")
    token(server, "token-2")
    server.text("GET", CALENDAR_PATH, "Expired", status=401)
    server.json(
        "GET", CALENDAR_PATH, official_payload({date(2026, 1, 5): TempoColor.RED})
    )
    calendar = await official(session, server).async_calendar(
        date(2026, 1, 5), date(2026, 1, 5)
    )
    assert calendar == {date(2026, 1, 5): TempoColor.RED}
    assert len(server.calls(TOKEN_PATH)) == 2


async def test_official_refuses_twice(
    session: aiohttp.ClientSession, server: FakeServer
) -> None:
    token(server, "token-1")
    token(server, "token-2")
    server.text("GET", CALENDAR_PATH, "Expired", status=401)
    server.text("GET", CALENDAR_PATH, "Expired", status=401)
    with pytest.raises(TempoAuthError):
        await official(session, server).async_calendar(
            date(2026, 1, 5), date(2026, 1, 5)
        )


async def test_official_long_range_is_split(
    session: aiohttp.ClientSession, server: FakeServer
) -> None:
    token(server)
    first = parse_public_values(recorded("2024-2025"))
    second = parse_public_values(recorded("2025-2026"))
    server.json(
        "GET",
        CALENDAR_PATH,
        official_payload(first | {date(2025, 9, 1): TempoColor.BLUE}),
    )
    server.json("GET", CALENDAR_PATH, official_payload(second))
    calendar = await official(session, server).async_calendar(
        date(2024, 9, 1), date(2026, 8, 31)
    )
    assert calendar == first | second
    calls = server.calls(CALENDAR_PATH)
    assert [(call.query["start_date"], call.query["end_date"]) for call in calls] == [
        ("2024-09-01T00:00:00+02:00", "2025-09-02T00:00:00+02:00"),
        ("2025-09-02T00:00:00+02:00", "2026-09-01T00:00:00+02:00"),
    ]


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (400, TempoAuthError),
        (401, TempoAuthError),
        (403, TempoAuthError),
        (429, TempoRateLimitError),
        (500, TempoServerError),
        (418, TempoFormatError),
    ],
)
async def test_official_token_errors(
    session: aiohttp.ClientSession,
    server: FakeServer,
    status: int,
    error: type[TempoError],
) -> None:
    server.text("POST", TOKEN_PATH, "Error", status=status)
    with pytest.raises(error):
        await official(session, server).async_check_credentials()


@pytest.mark.parametrize("payload", ["not json", {"token_type": "Bearer"}, []])
async def test_official_bad_token_answer(
    session: aiohttp.ClientSession, server: FakeServer, payload: object
) -> None:
    if isinstance(payload, str):
        server.text("POST", TOKEN_PATH, payload)
    else:
        server.json("POST", TOKEN_PATH, payload)
    with pytest.raises(TempoFormatError):
        await official(session, server).async_check_credentials()


@pytest.mark.parametrize(
    ("status", "error"),
    [(403, TempoAuthError), (429, TempoRateLimitError), (503, TempoServerError)],
)
async def test_official_calendar_errors(
    session: aiohttp.ClientSession,
    server: FakeServer,
    status: int,
    error: type[TempoError],
) -> None:
    token(server)
    server.text("GET", CALENDAR_PATH, "Error", status=status)
    with pytest.raises(error):
        await official(session, server).async_calendar(
            date(2026, 1, 5), date(2026, 1, 5)
        )


async def test_official_calendar_not_json(
    session: aiohttp.ClientSession, server: FakeServer
) -> None:
    token(server)
    server.text("GET", CALENDAR_PATH, "<html></html>")
    with pytest.raises(TempoFormatError, match="JSON"):
        await official(session, server).async_calendar(
            date(2026, 1, 5), date(2026, 1, 5)
        )


async def test_official_unreachable(
    session: aiohttp.ClientSession, closed_url: str
) -> None:
    client = RteOfficialTempoClient(session, "id", "secret", base_url=closed_url)
    with pytest.raises(TempoServerError):
        await client.async_check_credentials()


async def test_official_calendar_unreachable(
    session: aiohttp.ClientSession, server: FakeServer
) -> None:
    async def drop(request: web.Request) -> web.StreamResponse:
        assert request.transport is not None
        request.transport.close()
        return web.Response()

    token(server)
    # aiohttp retries a GET once when a reused connection drops.
    server.add("GET", CALENDAR_PATH, drop)
    server.add("GET", CALENDAR_PATH, drop)
    with pytest.raises(TempoServerError):
        await official(session, server).async_calendar(
            date(2026, 1, 5), date(2026, 1, 5)
        )


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"tempo_like_calendars": {}},
        {"tempo_like_calendars": {"values": ["BLUE"]}},
        {
            "tempo_like_calendars": {
                "values": [{"start_date": "5 janvier", "value": "RED"}]
            }
        },
        {
            "tempo_like_calendars": {
                "values": [{"start_date": "2026-01-05T00:00:00+01:00"}]
            }
        },
    ],
)
def test_unexpected_official_format(payload: Any) -> None:
    with pytest.raises(TempoFormatError):
        parse_official_values(payload)


# Fallback.


class StubSource:
    """A source that answers or fails as told."""

    def __init__(self, name: str, answer: Calendar | TempoError) -> None:
        """Answer with `answer`, or raise it."""
        self._name = name
        self._answer = answer
        self.calls = 0

    @property
    def name(self) -> str:
        """Name of the source."""
        return self._name

    async def async_calendar(self, start: date, end: date) -> Calendar:
        """Answer or fail."""
        del start, end
        self.calls += 1
        if isinstance(self._answer, TempoError):
            raise self._answer
        return self._answer


DAY = date(2026, 1, 5)


async def test_fallback_uses_the_first_answer() -> None:
    primary = StubSource("public", {DAY: TempoColor.RED})
    secondary = StubSource("official", {DAY: TempoColor.BLUE})
    source = FallbackTempoSource([primary, secondary])
    assert source.name == "public+official"
    assert await source.async_calendar(DAY, DAY) == {DAY: TempoColor.RED}
    assert source.last_source == "public"
    assert source.last_errors == {}
    assert secondary.calls == 0


async def test_fallback_after_an_error() -> None:
    error = TempoFormatError("changed")
    source = FallbackTempoSource(
        [StubSource("public", error), StubSource("official", {DAY: TempoColor.RED})]
    )
    assert await source.async_calendar(DAY, DAY) == {DAY: TempoColor.RED}
    assert source.last_source == "official"
    assert source.last_errors == {"public": error}


async def test_fallback_all_fail() -> None:
    last = TempoAuthError("refused")
    source = FallbackTempoSource(
        [StubSource("public", TempoServerError("down")), StubSource("official", last)]
    )
    with pytest.raises(TempoAuthError):
        await source.async_calendar(DAY, DAY)
    assert source.last_source is None
    assert set(source.last_errors) == {"public", "official"}


async def test_source_names(session: aiohttp.ClientSession) -> None:
    source = FallbackTempoSource(
        [
            RtePublicTempoClient(session),
            RteOfficialTempoClient(session, "id", "secret"),
        ]
    )
    assert source.name == "rte_public+rte_official"


def test_fallback_needs_a_source() -> None:
    with pytest.raises(ValueError, match="source"):
        FallbackTempoSource([])
