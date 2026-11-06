"""Spread a day's consumption over its hours when only the daily total is known.

Linky meters without a load curve only report daily totals, which carry no
off-peak split. A day shape estimates it from days whose hours are known: the
night consumption (22:00 to 6:00, the off-peak hours of Tempo and of most HP/HC
contracts) is fitted as a straight line of the daily total, and both the night
and the day part follow the average hourly profile.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Final

from .engine import is_off_peak
from .models import TimeRange

NIGHT: Final = (TimeRange(start=time(22), end=time(6)),)
MIN_DAYS: Final = 14
HOURS_PER_DAY: Final = 24

_NIGHT_HOURS: Final = tuple(is_off_peak(time(hour), NIGHT) for hour in range(24))


@dataclass(frozen=True, kw_only=True)
class DayShape:
    """How a day's consumption splits between its hours."""

    weights: tuple[float, ...]
    """Average share of each local hour (0 to 23) in its half of the day."""
    intercept: float
    slope: float
    """Night consumption = intercept + slope * daily total."""
    days: int
    """Number of days the shape was learned from (0 for the flat shape)."""

    @classmethod
    def flat(cls) -> DayShape:
        """Spread every day evenly over its hours."""
        night_share = sum(_NIGHT_HOURS) / HOURS_PER_DAY
        return cls(
            weights=(1.0,) * HOURS_PER_DAY, intercept=0.0, slope=night_share, days=0
        )

    @classmethod
    def learn(cls, days: Mapping[date, Sequence[float]]) -> DayShape:
        """Learn from days whose 24 hourly values are known, in local time.

        Days with another number of values (daylight saving changes, missing
        hours) are left out. With fewer than MIN_DAYS days, the shape is flat.
        """
        complete = [
            values
            for values in days.values()
            if len(values) == HOURS_PER_DAY and sum(values) > 0
        ]
        if len(complete) < MIN_DAYS:
            return cls.flat()
        weights = [0.0] * HOURS_PER_DAY
        totals: list[float] = []
        nights: list[float] = []
        for values in complete:
            night = sum(v for hour, v in enumerate(values) if _NIGHT_HOURS[hour])
            day = sum(values) - night
            for hour, value in enumerate(values):
                part = night if _NIGHT_HOURS[hour] else day
                if part > 0:
                    weights[hour] += value / part
            totals.append(night + day)
            nights.append(night)
        mean_total = sum(totals) / len(totals)
        mean_night = sum(nights) / len(nights)
        variance = sum((total - mean_total) ** 2 for total in totals)
        slope = (
            sum(
                (total - mean_total) * (night - mean_night)
                for total, night in zip(totals, nights, strict=True)
            )
            / variance
            if variance
            else 0.0
        )
        return cls(
            weights=tuple(weight / len(complete) for weight in weights),
            intercept=mean_night - slope * mean_total,
            slope=slope,
            days=len(complete),
        )

    def night_kwh(self, total: float) -> float:
        """Estimate the night consumption of a day, between 0 and the total."""
        return min(max(self.intercept + self.slope * total, 0.0), total)

    def spread(self, hours: Sequence[datetime], total: float) -> list[float]:
        """Share `total` between the hours (local, aware) of one day.

        The result follows the order of `hours` and adds up to `total`. A day
        with 23 or 25 hours uses the weight of each hour's local time.
        """
        if not hours:
            return []
        is_night = [_NIGHT_HOURS[hour.hour] for hour in hours]
        night_total = self.night_kwh(total)
        if all(is_night) or not any(is_night):
            night_total = total if all(is_night) else 0.0
        values = [0.0] * len(hours)
        for night, part in ((True, night_total), (False, total - night_total)):
            indices = [index for index, flag in enumerate(is_night) if flag is night]
            weights = [self.weights[hours[index].hour] for index in indices]
            weight_sum = sum(weights)
            for index, weight in zip(indices, weights, strict=True):
                # A part without learned weight is spread evenly over its hours.
                share = weight / weight_sum if weight_sum else 1 / len(indices)
                values[index] = part * share
        return values
