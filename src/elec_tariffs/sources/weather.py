"""Outdoor temperature from Open-Meteo's historical weather API.

The archive gives the hourly air temperature at 2 m anywhere since 1940, free for
non-commercial use and without an account (data under CC BY 4.0). Its default
model blends the ERA5 reanalyses with the ECMWF forecast model, so it covers the
last days too; their hours are revised once the reanalysis catches up, about five
days later.
"""

from datetime import UTC, date, datetime, timedelta
from typing import Any, Final

import aiohttp

from elec_tariffs.errors import (
    WeatherFormatError,
    WeatherRateLimitError,
    WeatherServerError,
)
from elec_tariffs.sources.tempo import USER_AGENT

ARCHIVE_URL: Final = "https://archive-api.open-meteo.com/v1/archive"
MAX_DAYS: Final = 366

_TIMEOUT: Final = aiohttp.ClientTimeout(total=60)
_HTTP_OK: Final = 200
_HTTP_TOO_MANY_REQUESTS: Final = 429
_HTTP_SERVER_ERROR: Final = 500
_UNIT: Final = "°C"

type Temperatures = dict[datetime, float]


def parse_hourly_temperatures(payload: Any) -> Temperatures:
    """Read the hourly `temperature_2m` of an answer asked in GMT.

    Hours without a value are left out. Anything unexpected raises
    WeatherFormatError, rather than giving wrong temperatures.
    """
    if not isinstance(payload, dict) or payload.get("utc_offset_seconds") != 0:
        msg = "The answer is not in GMT"
        raise WeatherFormatError(msg)
    hourly = payload.get("hourly")
    units = payload.get("hourly_units")
    if not isinstance(hourly, dict) or not isinstance(units, dict):
        msg = "The answer has no hourly values"
        raise WeatherFormatError(msg)
    if units.get("temperature_2m") != _UNIT:
        msg = f"Unexpected temperature unit {units.get('temperature_2m')!r}"
        raise WeatherFormatError(msg)
    times = hourly.get("time")
    values = hourly.get("temperature_2m")
    if (
        not isinstance(times, list)
        or not isinstance(values, list)
        or len(times) != len(values)
    ):
        msg = "The hourly times and temperatures don't match"
        raise WeatherFormatError(msg)
    temperatures: Temperatures = {}
    for moment, value in zip(times, values, strict=True):
        try:
            start = datetime.fromisoformat(moment).replace(tzinfo=UTC)
        except (TypeError, ValueError) as err:
            msg = f"Unexpected time {moment!r}"
            raise WeatherFormatError(msg) from err
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int | float):
            msg = f"Unexpected temperature {value!r}"
            raise WeatherFormatError(msg)
        temperatures[start] = float(value)
    return temperatures


class OpenMeteoClient:
    """Open-Meteo's historical weather API, no account needed."""

    def __init__(
        self, session: aiohttp.ClientSession, *, url: str = ARCHIVE_URL
    ) -> None:
        """Use the caller's HTTP session; `url` replaces Open-Meteo's address."""
        self._session = session
        self._url = url

    async def _get(self, params: dict[str, str]) -> Any:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        try:
            async with self._session.get(
                self._url, params=params, headers=headers, timeout=_TIMEOUT
            ) as response:
                if response.status == _HTTP_TOO_MANY_REQUESTS:
                    msg = "Too many requests (HTTP 429)"
                    raise WeatherRateLimitError(msg)
                if response.status >= _HTTP_SERVER_ERROR:
                    msg = f"Server error (HTTP {response.status})"
                    raise WeatherServerError(msg)
                payload = await response.json(content_type=None)
        except ValueError as err:
            msg = "Open-Meteo did not answer with JSON"
            raise WeatherFormatError(msg) from err
        except (aiohttp.ClientError, TimeoutError) as err:
            msg = f"Could not reach Open-Meteo: {err}"
            raise WeatherServerError(msg) from err
        if response.status != _HTTP_OK:
            reason = payload.get("reason") if isinstance(payload, dict) else None
            msg = f"Open-Meteo refused the request (HTTP {response.status}): {reason}"
            raise WeatherFormatError(msg)
        return payload

    async def async_hourly_temperatures(
        self, latitude: float, longitude: float, start: date, end: date
    ) -> Temperatures:
        """Return the temperature (°C) of each hour of the UTC days `start` to `end`.

        Keys are the UTC start of each hour. Hours without a value yet are missing.
        Long ranges are asked in parts of at most MAX_DAYS days.
        """
        temperatures: Temperatures = {}
        first = start
        while first <= end:
            last = min(end, first + timedelta(days=MAX_DAYS - 1))
            payload = await self._get(
                {
                    "latitude": str(latitude),
                    "longitude": str(longitude),
                    "start_date": first.isoformat(),
                    "end_date": last.isoformat(),
                    "hourly": "temperature_2m",
                    "timezone": "GMT",
                }
            )
            temperatures.update(parse_hourly_temperatures(payload))
            first = last + timedelta(days=1)
        return temperatures
