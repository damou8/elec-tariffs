"""Tempo calendar: the color of each day, from RTE.

Two sources give the same data:

- RTE's public Tempo page backend (`services-rte.com/cms/open_data`), without an
  account. It is not documented and may change without notice, so its answers are
  checked strictly.
- RTE's official "Tempo Like Supply Contract" API, which needs the client ID and
  secret of an application created on data.rte-france.com.
"""

import base64
import time as monotonic_time
from collections.abc import Callable, Sequence
from datetime import date, datetime, time, timedelta
from typing import Any, Final, Protocol
from zoneinfo import ZoneInfo

import aiohttp

from elec_tariffs._version import __version__
from elec_tariffs.errors import (
    TempoAuthError,
    TempoError,
    TempoFormatError,
    TempoRateLimitError,
    TempoServerError,
)
from elec_tariffs.models import TempoColor

PUBLIC_BASE_URL: Final = "https://www.services-rte.com/cms/open_data/v1"
OFFICIAL_BASE_URL: Final = "https://digital.iservices.rte-france.com"
TOKEN_PATH: Final = "/token/oauth/"  # noqa: S105 - a path, not a password
CALENDAR_PATH: Final = "/open_api/tempo_like_supply_contract/v1/tempo_like_calendars"

MAX_RED_DAYS: Final = 22
MAX_WHITE_DAYS: Final = 43
FIRST_SEASON: Final = "2014-2015"
OFFICIAL_MAX_DAYS: Final = 366

PARIS: Final = ZoneInfo("Europe/Paris")
USER_AGENT: Final = (
    f"elec-tariffs/{__version__} (+https://github.com/damou8/elec-tariffs)"
)

_SEASON_START_MONTH: Final = 9
_TIMEOUT: Final = aiohttp.ClientTimeout(total=30)
_TOKEN_MARGIN: Final = 60
_DEFAULT_TOKEN_LIFETIME: Final = 3600
_COLORS: Final = {
    "BLUE": TempoColor.BLUE,
    "WHITE": TempoColor.WHITE,
    "RED": TempoColor.RED,
}
_HTTP_OK: Final = 200
_HTTP_UNAUTHORIZED: Final = 401
_HTTP_FORBIDDEN: Final = 403
_HTTP_TOO_MANY_REQUESTS: Final = 429
_HTTP_SERVER_ERROR: Final = 500

type Calendar = dict[date, TempoColor]


def season_of(day: date) -> str:
    """Return the Tempo season of a day, like "2025-2026" (September to August)."""
    first = day.year if day.month >= _SEASON_START_MONTH else day.year - 1
    return f"{first}-{first + 1}"


def season_bounds(season: str) -> tuple[date, date]:
    """Return the first and last day of a season named like "2025-2026"."""
    first = int(season.split("-", 1)[0])
    return date(first, _SEASON_START_MONTH, 1), date(first + 1, 8, 31)


def _seasons(start: date, end: date) -> list[str]:
    first = int(season_of(start).split("-", 1)[0])
    last = int(season_of(end).split("-", 1)[0])
    return [f"{year}-{year + 1}" for year in range(first, last + 1)]


def _color(value: Any) -> TempoColor:
    color = _COLORS.get(value) if isinstance(value, str) else None
    if color is None:
        msg = f"Unknown Tempo color {value!r}"
        raise TempoFormatError(msg)
    return color


def parse_public_values(payload: Any) -> Calendar:
    """Read a `{"values": {"YYYY-MM-DD": "BLUE", ...}}` answer of the public source."""
    values = payload.get("values") if isinstance(payload, dict) else None
    if not isinstance(values, dict):
        msg = "The answer has no 'values' object"
        raise TempoFormatError(msg)
    calendar: Calendar = {}
    for key, value in values.items():
        if key.endswith("-fallback"):
            continue
        try:
            day = date.fromisoformat(key)
        except ValueError as err:
            msg = f"Unexpected key {key!r}"
            raise TempoFormatError(msg) from err
        calendar[day] = _color(value)
    return calendar


def check_season(calendar: Calendar, season: str) -> None:
    """Refuse a season with days outside it or more red or white days than allowed."""
    first, last = season_bounds(season)
    if any(not first <= day <= last for day in calendar):
        msg = f"Days outside the {season} season"
        raise TempoFormatError(msg)
    colors = list(calendar.values())
    if (
        colors.count(TempoColor.RED) > MAX_RED_DAYS
        or colors.count(TempoColor.WHITE) > MAX_WHITE_DAYS
    ):
        msg = f"Too many red or white days in the {season} season"
        raise TempoFormatError(msg)


def _raise_for_status(status: int) -> None:
    if status == _HTTP_OK:
        return
    if status in (_HTTP_UNAUTHORIZED, _HTTP_FORBIDDEN):
        msg = f"Access refused (HTTP {status})"
        raise TempoAuthError(msg)
    if status == _HTTP_TOO_MANY_REQUESTS:
        msg = "Too many requests (HTTP 429)"
        raise TempoRateLimitError(msg)
    if status >= _HTTP_SERVER_ERROR:
        msg = f"Server error (HTTP {status})"
        raise TempoServerError(msg)
    msg = f"Unexpected answer (HTTP {status})"
    raise TempoFormatError(msg)


class TempoCalendarSource(Protocol):
    """Something that gives the Tempo color of days."""

    @property
    def name(self) -> str:
        """Short, stable name of the source, used in logs and diagnostics."""
        ...

    async def async_calendar(self, start: date, end: date) -> Calendar:
        """Return the known colors from `start` to `end`, both included.

        Days not published yet are missing from the result.
        """
        ...


class RtePublicTempoClient:
    """RTE's public Tempo page backend, no account needed."""

    def __init__(
        self, session: aiohttp.ClientSession, *, base_url: str = PUBLIC_BASE_URL
    ) -> None:
        """Use the caller's HTTP session; `base_url` replaces RTE's address."""
        self._session = session
        self._base_url = base_url.rstrip("/")

    @property
    def name(self) -> str:
        """Name of the source."""
        return "rte_public"

    async def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        try:
            async with self._session.get(
                f"{self._base_url}/{path}",
                params=params,
                headers=headers,
                timeout=_TIMEOUT,
            ) as response:
                _raise_for_status(response.status)
                return await response.json(content_type=None)
        except TempoAuthError as err:
            msg = f"The public Tempo source refused the request: {err}"
            raise TempoFormatError(msg) from err
        except ValueError as err:
            msg = "The public Tempo source did not answer with JSON"
            raise TempoFormatError(msg) from err
        except (aiohttp.ClientError, TimeoutError) as err:
            msg = f"Could not reach the public Tempo source: {err}"
            raise TempoServerError(msg) from err

    async def async_season(self, season: str) -> Calendar:
        """Return the colors of a season published so far."""
        calendar = parse_public_values(await self._get("tempo", {"season": season}))
        check_season(calendar, season)
        return calendar

    async def async_today_tomorrow(self) -> Calendar:
        """Return today's color and tomorrow's once published."""
        return parse_public_values(await self._get("tempoLight"))

    async def async_calendar(self, start: date, end: date) -> Calendar:
        """Return the known colors from `start` to `end`, both included."""
        if (end - start).days <= 1:
            light = await self.async_today_tomorrow()
            if start in light and end in light:
                return {
                    day: color for day, color in light.items() if start <= day <= end
                }
        calendar: Calendar = {}
        for season in _seasons(start, end):
            calendar.update(await self.async_season(season))
        return {day: color for day, color in calendar.items() if start <= day <= end}


class RteOfficialTempoClient:
    """RTE's official Tempo Like Supply Contract API (OAuth2 client credentials)."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        client_id: str,
        client_secret: str,
        *,
        base_url: str = OFFICIAL_BASE_URL,
        clock: Callable[[], float] = monotonic_time.monotonic,
    ) -> None:
        """Use the caller's HTTP session and the application's credentials.

        `base_url` replaces RTE's address and `clock` the monotonic clock that
        tells when the token expires.
        """
        self._session = session
        self._base_url = base_url.rstrip("/")
        credentials = f"{client_id}:{client_secret}".encode()
        self._basic_auth = f"Basic {base64.b64encode(credentials).decode('ascii')}"
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0

    @property
    def name(self) -> str:
        """Name of the source."""
        return "rte_official"

    async def _async_token(self, *, refresh: bool = False) -> str:
        if self._token and not refresh and self._clock() < self._expires_at:
            return self._token
        try:
            async with self._session.post(
                f"{self._base_url}{TOKEN_PATH}",
                data={"grant_type": "client_credentials"},
                headers={"Authorization": self._basic_auth, "User-Agent": USER_AGENT},
                timeout=_TIMEOUT,
            ) as response:
                if response.status == 400:  # noqa: PLR2004 - OAuth answers 400 for a bad client
                    msg = "RTE refused the client credentials (HTTP 400)"
                    raise TempoAuthError(msg)
                _raise_for_status(response.status)
                payload = await response.json(content_type=None)
        except ValueError as err:
            msg = "The RTE token endpoint did not answer with JSON"
            raise TempoFormatError(msg) from err
        except (aiohttp.ClientError, TimeoutError) as err:
            msg = f"Could not reach RTE: {err}"
            raise TempoServerError(msg) from err
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            msg = "The RTE token answer has no access_token"
            raise TempoFormatError(msg)
        lifetime = payload.get("expires_in", _DEFAULT_TOKEN_LIFETIME)
        if not isinstance(lifetime, int):
            lifetime = _DEFAULT_TOKEN_LIFETIME
        self._token = token
        self._expires_at = self._clock() + max(lifetime - _TOKEN_MARGIN, 0)
        return token

    async def async_check_credentials(self) -> None:
        """Get a token, raising TempoAuthError if RTE refuses the credentials."""
        await self._async_token(refresh=True)

    async def _async_get_calendar(
        self, params: dict[str, str], *, refresh: bool
    ) -> tuple[int, Any]:
        """Return the status and, when it is 200, the JSON answer."""
        token = await self._async_token(refresh=refresh)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        try:
            async with self._session.get(
                f"{self._base_url}{CALENDAR_PATH}",
                params=params,
                headers=headers,
                timeout=_TIMEOUT,
            ) as response:
                if response.status != _HTTP_OK:
                    return response.status, None
                return response.status, await response.json(content_type=None)
        except ValueError as err:
            msg = "The RTE calendar did not answer with JSON"
            raise TempoFormatError(msg) from err
        except (aiohttp.ClientError, TimeoutError) as err:
            msg = f"Could not reach RTE: {err}"
            raise TempoServerError(msg) from err

    async def _async_chunk(self, start: date, end: date) -> Calendar:
        params = {
            "start_date": datetime.combine(start, time(), PARIS).isoformat(),
            "end_date": datetime.combine(
                end + timedelta(days=1), time(), PARIS
            ).isoformat(),
        }
        status, payload = await self._async_get_calendar(params, refresh=False)
        if status == _HTTP_UNAUTHORIZED:
            # The token may have been revoked before it expired: get a new one once.
            status, payload = await self._async_get_calendar(params, refresh=True)
        _raise_for_status(status)
        return parse_official_values(payload)

    async def async_calendar(self, start: date, end: date) -> Calendar:
        """Return the known colors from `start` to `end`, both included."""
        calendar: Calendar = {}
        chunk_start = start
        while chunk_start <= end:
            chunk_end = min(end, chunk_start + timedelta(days=OFFICIAL_MAX_DAYS - 1))
            calendar.update(await self._async_chunk(chunk_start, chunk_end))
            chunk_start = chunk_end + timedelta(days=1)
        return {day: color for day, color in calendar.items() if start <= day <= end}


def parse_official_values(payload: Any) -> Calendar:
    """Read a `tempo_like_calendars.values[]` answer of the official API."""
    container = (
        payload.get("tempo_like_calendars") if isinstance(payload, dict) else None
    )
    values = container.get("values") if isinstance(container, dict) else None
    if not isinstance(values, list):
        msg = "The answer has no 'tempo_like_calendars.values' list"
        raise TempoFormatError(msg)
    calendar: Calendar = {}
    for item in values:
        start = item.get("start_date") if isinstance(item, dict) else None
        if not isinstance(start, str):
            msg = f"Unexpected calendar item {item!r}"
            raise TempoFormatError(msg)
        try:
            day = datetime.fromisoformat(start).date()
        except ValueError as err:
            msg = f"Unexpected start_date {start!r}"
            raise TempoFormatError(msg) from err
        calendar[day] = _color(item.get("value"))
    return calendar


class FallbackTempoSource:
    """Ask each source in turn and return the first answer."""

    def __init__(self, sources: Sequence[TempoCalendarSource]) -> None:
        """Try `sources` in this order."""
        if not sources:
            msg = "At least one source is needed"
            raise ValueError(msg)
        self._sources = tuple(sources)
        self.last_source: str | None = None
        self.last_errors: dict[str, TempoError] = {}

    @property
    def name(self) -> str:
        """Names of the sources, in order."""
        return "+".join(source.name for source in self._sources)

    async def async_calendar(self, start: date, end: date) -> Calendar:
        """Return the first source's answer; raise the last error if all fail.

        `last_source` names the source that answered, `last_errors` the errors of
        the sources tried before it (or of all of them).
        """
        self.last_errors = {}
        self.last_source = None
        error: TempoError | None = None
        for source in self._sources:
            try:
                calendar = await source.async_calendar(start, end)
            except TempoError as err:
                self.last_errors[source.name] = err
                error = err
                continue
            self.last_source = source.name
            return calendar
        assert error is not None  # noqa: S101 - at least one source was tried
        raise error
