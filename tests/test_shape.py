"""Tests of the day shape."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from elec_tariffs import DayShape
from elec_tariffs.shape import MIN_DAYS

PARIS = ZoneInfo("Europe/Paris")
NIGHT = [*range(6), 22, 23]


def day_values(total: float) -> list[float]:
    """Return a day whose night uses 2 + 0.4 x total kWh, mostly at 2:00 and 23:00."""
    night = 2 + 0.4 * total
    day = total - night
    values = [0.0] * 24
    for hour in NIGHT:
        values[hour] = night * (0.3 if hour in (2, 23) else 0.4 / 6)
    for hour in range(6, 22):
        values[hour] = day / 16
    return values


def learned() -> DayShape:
    days = {
        date(2026, 4, 1) + timedelta(days=n): day_values(10 + n % 10) for n in range(30)
    }
    return DayShape.learn(days)


def local_hours(day: date) -> list[datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=PARIS)
    end = start + timedelta(days=1)
    utc = start.astimezone(ZoneInfo("UTC"))
    hours = []
    while utc.astimezone(PARIS) < end:
        hours.append(utc.astimezone(PARIS))
        utc += timedelta(hours=1)
    return hours


def test_learns_the_night_consumption() -> None:
    shape = learned()
    assert shape.days == 30
    assert shape.intercept == pytest.approx(2)
    assert shape.slope == pytest.approx(0.4)
    assert shape.night_kwh(20) == pytest.approx(10)
    assert shape.weights[2] == pytest.approx(0.3)
    assert shape.weights[12] == pytest.approx(1 / 16)


def test_spread_follows_the_shape() -> None:
    hours = local_hours(date(2026, 1, 15))
    values = learned().spread(hours, 30)
    assert len(values) == 24
    assert sum(values) == pytest.approx(30)
    assert sum(values[hour] for hour in NIGHT) == pytest.approx(14)
    assert values[2] == pytest.approx(14 * 0.3)


@pytest.mark.parametrize(
    ("day", "count"), [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25)]
)
def test_daylight_saving_days(day: date, count: int) -> None:
    hours = local_hours(day)
    assert len(hours) == count
    assert sum(learned().spread(hours, 12)) == pytest.approx(12)


def test_night_estimate_stays_within_the_total() -> None:
    shape = learned()
    assert shape.night_kwh(1) == 1
    negative = DayShape(weights=(1.0,) * 24, intercept=-5, slope=0.1, days=20)
    assert negative.night_kwh(10) == 0
    values = negative.spread(local_hours(date(2026, 1, 15)), 10)
    assert sum(values[hour] for hour in NIGHT) == 0
    assert sum(values) == pytest.approx(10)


def test_flat_without_enough_days() -> None:
    days = {
        date(2026, 4, 1) + timedelta(days=n): day_values(12)
        for n in range(MIN_DAYS - 1)
    }
    days[date(2026, 5, 1)] = [1.0] * 23
    days[date(2026, 5, 2)] = [0.0] * 24
    shape = DayShape.learn(days)
    assert shape == DayShape.flat()
    values = shape.spread(local_hours(date(2026, 1, 15)), 24)
    assert values == pytest.approx([1.0] * 24)


def test_days_of_the_same_total() -> None:
    days = {
        date(2026, 4, 1) + timedelta(days=n): day_values(12) for n in range(MIN_DAYS)
    }
    shape = DayShape.learn(days)
    assert shape.slope == 0
    assert shape.intercept == pytest.approx(2 + 0.4 * 12)


def test_hours_without_weight_are_spread_evenly() -> None:
    # Learned from days without any consumption during the day.
    shape = DayShape(
        weights=tuple(1.0 if hour in NIGHT else 0.0 for hour in range(24)),
        intercept=0,
        slope=0.5,
        days=20,
    )
    values = shape.spread(local_hours(date(2026, 1, 15)), 24)
    assert sum(values[hour] for hour in NIGHT) == pytest.approx(12)
    assert values[12] == pytest.approx(12 / 16)
    assert shape.spread([], 0) == []


def test_hours_of_one_part_only() -> None:
    hours = local_hours(date(2026, 1, 15))
    shape = learned()
    assert sum(shape.spread([hours[2], hours[3]], 4)) == pytest.approx(4)
    assert sum(shape.spread([hours[12]], 3)) == pytest.approx(3)


def test_day_without_night_consumption() -> None:
    days = {
        date(2026, 4, 1) + timedelta(days=n): day_values(12) for n in range(MIN_DAYS)
    }
    days[date(2026, 5, 1)] = [0.0] * 6 + [1.0] * 16 + [0.0] * 2
    shape = DayShape.learn(days)
    assert shape.days == MIN_DAYS + 1
    assert sum(shape.spread(local_hours(date(2026, 1, 15)), 12)) == pytest.approx(12)
