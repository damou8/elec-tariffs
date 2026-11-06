"""Split an hour of consumption into tranches and cost it.

An hourly statistic row starts at the top of an hour. Every hour is costed as two
half hours with half the energy each: that is the finest boundary of an off-peak
range, and it assumes the consumption is even within the hour.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Final

from .errors import MissingColorError, MissingPriceError
from .models import (
    ContractKind,
    ContractRules,
    DayCalendar,
    PricePeriod,
    TempoColor,
    TimeRange,
    Tranche,
)

TEMPO_DAY_START: Final = time(6)
TEMPO_OFF_PEAK: Final = (TimeRange(start=time(22), end=time(6)),)

_TEMPO_TRANCHES: Final = {
    (TempoColor.BLUE, True): Tranche.BLUE_HC,
    (TempoColor.BLUE, False): Tranche.BLUE_HP,
    (TempoColor.WHITE, True): Tranche.WHITE_HC,
    (TempoColor.WHITE, False): Tranche.WHITE_HP,
    (TempoColor.RED, True): Tranche.RED_HC,
    (TempoColor.RED, False): Tranche.RED_HP,
}
_SATURDAY: Final = 5


def tempo_day(local: datetime) -> date:
    """Return the Tempo day that `local` belongs to: they run from 6:00 to 6:00."""
    wall = local.replace(tzinfo=None) - timedelta(hours=TEMPO_DAY_START.hour)
    return wall.date()


def is_off_peak(value: time, ranges: Sequence[TimeRange]) -> bool:
    """Tell whether a time of day falls in one of the off-peak ranges."""
    return any(time_range.contains(value) for time_range in ranges)


def _tranche(local: datetime, rules: ContractRules, calendar: DayCalendar) -> Tranche:
    """Return the tranche of the half hour starting at `local`."""
    if rules.kind is ContractKind.BASE:
        return Tranche.BASE
    if rules.kind is ContractKind.HPHC:
        weekend = rules.weekend_off_peak and local.weekday() >= _SATURDAY
        if weekend or is_off_peak(local.time(), rules.off_peak):
            return Tranche.HC
        return Tranche.HP
    day = tempo_day(local)
    color = calendar.get(day)
    if color is None:
        raise MissingColorError(day)
    return _TEMPO_TRANCHES[color, is_off_peak(local.time(), TEMPO_OFF_PEAK)]


def split_hour(
    start: datetime, kwh: float, rules: ContractRules, calendar: DayCalendar
) -> dict[Tranche, float]:
    """Split the energy of the hour starting at `start` (local, aware) into tranches.

    Raise MissingColorError when a Tempo contract needs a color that is not known.
    """
    if start.minute or start.second or start.microsecond:
        msg = f"{start.isoformat()} is not the start of an hour"
        raise ValueError(msg)
    split: dict[Tranche, float] = {}
    for half in (start, start.replace(minute=30)):
        tranche = _tranche(half, rules, calendar)
        split[tranche] = split.get(tranche, 0.0) + kwh / 2
    return split


def period_at(periods: Sequence[PricePeriod], day: date) -> PricePeriod:
    """Return the price period in force on `day`, or raise MissingPriceError."""
    for period in periods:
        if period.covers(day):
            return period
    raise MissingPriceError(day)


def price_at(periods: Sequence[PricePeriod], day: date, tranche: Tranche) -> float:
    """Return the price of a tranche on `day`, or raise MissingPriceError."""
    period = period_at(periods, day)
    if tranche not in period.prices:
        raise MissingPriceError(day)
    return period.prices[tranche]


def fixed_cost_per_hour(period: PricePeriod, start: datetime) -> float:
    """Spread the yearly fixed part evenly over the hours of the year of `start`."""
    year = start.year
    days = (date(year + 1, 1, 1) - date(year, 1, 1)).days
    return period.fixed_per_year / days / 24


@dataclass(frozen=True, kw_only=True)
class HourCost:
    """Energy (kWh) and variable cost per tranche of one hour, and its fixed cost."""

    energy: dict[Tranche, float]
    variable: dict[Tranche, float]
    fixed: float

    @property
    def total(self) -> float:
        """Variable and fixed cost together."""
        return sum(self.variable.values()) + self.fixed


def cost_hour(
    start: datetime,
    kwh: float,
    rules: ContractRules,
    periods: Sequence[PricePeriod],
    calendar: DayCalendar,
) -> HourCost:
    """Cost the hour starting at `start` (local, aware).

    Prices change at midnight local time, so the whole hour uses the prices of its
    day. Raise MissingColorError or MissingPriceError when data is missing.
    """
    energy = split_hour(start, kwh, rules, calendar)
    period = period_at(periods, start.date())
    variable: dict[Tranche, float] = {}
    for tranche, tranche_kwh in energy.items():
        if tranche not in period.prices:
            raise MissingPriceError(start.date())
        variable[tranche] = tranche_kwh * period.prices[tranche]
    return HourCost(
        energy=energy, variable=variable, fixed=fixed_cost_per_hour(period, start)
    )
