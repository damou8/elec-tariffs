"""Tests of the hourly split and costing."""

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from elec_tariffs import (
    ContractKind,
    ContractRules,
    MissingColorError,
    MissingPriceError,
    PricePeriod,
    TempoColor,
    TimeRange,
    Tranche,
    cost_hour,
    fixed_cost_per_hour,
    parse_trv_csv,
    price_at,
    split_hour,
    tempo_day,
)

from .conftest import load_fixture

PARIS = ZoneInfo("Europe/Paris")
TEMPO = ContractRules(kind=ContractKind.TEMPO)
BASE = ContractRules(kind=ContractKind.BASE)
NIGHT = (TimeRange(start=time(22), end=time(6)),)


def at(year: int, month: int, day: int, hour: int, *, fold: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=PARIS, fold=fold)


def test_tempo_day_starts_at_six() -> None:
    assert tempo_day(at(2026, 1, 5, 5)) == date(2026, 1, 4)
    assert tempo_day(at(2026, 1, 5, 6)) == date(2026, 1, 5)
    assert tempo_day(at(2026, 1, 5, 23)) == date(2026, 1, 5)


def test_tempo_night_takes_the_color_of_the_evening() -> None:
    # Sunday 28 December 2025 is blue, Monday 29 is red: the night between them
    # is blue off-peak until 6:00, then the red day starts.
    calendar = {date(2025, 12, 28): TempoColor.BLUE, date(2025, 12, 29): TempoColor.RED}
    assert split_hour(at(2025, 12, 28, 23), 2.0, TEMPO, calendar) == {
        Tranche.BLUE_HC: 2.0
    }
    assert split_hour(at(2025, 12, 29, 5), 2.0, TEMPO, calendar) == {
        Tranche.BLUE_HC: 2.0
    }
    assert split_hour(at(2025, 12, 29, 6), 2.0, TEMPO, calendar) == {
        Tranche.RED_HP: 2.0
    }
    assert split_hour(at(2025, 12, 29, 21), 2.0, TEMPO, calendar) == {
        Tranche.RED_HP: 2.0
    }
    assert split_hour(at(2025, 12, 29, 22), 2.0, TEMPO, calendar) == {
        Tranche.RED_HC: 2.0
    }


def test_tempo_missing_color() -> None:
    with pytest.raises(MissingColorError) as caught:
        split_hour(at(2026, 1, 5, 5), 1.0, TEMPO, {date(2026, 1, 5): TempoColor.RED})
    assert caught.value.day == date(2026, 1, 4)


def test_half_hour_off_peak_range_splits_the_hour() -> None:
    rules = ContractRules(
        kind=ContractKind.HPHC,
        off_peak=(TimeRange(start=time(12, 30), end=time(14)), *NIGHT),
    )
    assert split_hour(at(2026, 3, 4, 12), 1.0, rules, {}) == {
        Tranche.HP: 0.5,
        Tranche.HC: 0.5,
    }
    assert split_hour(at(2026, 3, 4, 13), 1.0, rules, {}) == {Tranche.HC: 1.0}
    assert split_hour(at(2026, 3, 4, 14), 1.0, rules, {}) == {Tranche.HP: 1.0}
    assert split_hour(at(2026, 3, 4, 23), 1.0, rules, {}) == {Tranche.HC: 1.0}


def test_weekend_off_peak() -> None:
    rules = ContractRules(kind=ContractKind.HPHC, off_peak=NIGHT, weekend_off_peak=True)
    assert split_hour(at(2026, 3, 7, 15), 1.0, rules, {}) == {
        Tranche.HC: 1.0
    }  # Saturday
    assert split_hour(at(2026, 3, 8, 15), 1.0, rules, {}) == {Tranche.HC: 1.0}  # Sunday
    assert split_hour(at(2026, 3, 9, 15), 1.0, rules, {}) == {Tranche.HP: 1.0}  # Monday
    weekdays_only = ContractRules(kind=ContractKind.HPHC, off_peak=NIGHT)
    assert split_hour(at(2026, 3, 7, 15), 1.0, weekdays_only, {}) == {Tranche.HP: 1.0}


def test_base_is_one_tranche() -> None:
    assert split_hour(at(2026, 3, 7, 15), 1.5, BASE, {}) == {Tranche.BASE: 1.5}


def test_split_refuses_a_row_off_the_hour() -> None:
    with pytest.raises(ValueError, match="start of an hour"):
        split_hour(datetime(2026, 3, 7, 15, 30, tzinfo=PARIS), 1.0, BASE, {})


def test_daylight_saving_days() -> None:
    calendar = {
        date(2026, 3, 28): TempoColor.WHITE,
        date(2026, 3, 29): TempoColor.BLUE,
        date(2026, 10, 24): TempoColor.BLUE,
        date(2026, 10, 25): TempoColor.BLUE,
    }
    # 29 March 2026 jumps from 2:00 to 3:00: the hour after 1:00 starts at 3:00.
    after_jump = datetime(2026, 3, 29, 1, tzinfo=UTC).astimezone(PARIS)
    assert after_jump.hour == 3
    assert split_hour(after_jump, 1.0, TEMPO, calendar) == {Tranche.WHITE_HC: 1.0}
    # 25 October 2026 has 2:00 twice; both hours belong to the 24 October Tempo day.
    for fold in (0, 1):
        assert split_hour(at(2026, 10, 25, 2, fold=fold), 1.0, TEMPO, calendar) == {
            Tranche.BLUE_HC: 1.0
        }


def tempo_periods() -> list[PricePeriod]:
    return parse_trv_csv(load_fixture("trv_tempo.csv"), ContractKind.TEMPO, 6)


def test_prices_change_on_the_first_of_august() -> None:
    periods = tempo_periods()
    assert price_at(periods, date(2026, 7, 31), Tranche.BLUE_HC) == pytest.approx(
        0.1325
    )
    assert price_at(periods, date(2026, 8, 1), Tranche.BLUE_HC) == pytest.approx(0.1356)
    assert price_at(periods, date(2026, 8, 1), Tranche.RED_HP) == pytest.approx(0.7295)


def test_prices_change_on_the_first_of_february() -> None:
    periods = tempo_periods()
    assert price_at(periods, date(2025, 1, 31), Tranche.RED_HP) != price_at(
        periods, date(2025, 2, 1), Tranche.RED_HP
    )
    assert price_at(periods, date(2025, 2, 1), Tranche.RED_HP) == pytest.approx(0.6586)


def test_missing_price() -> None:
    periods = tempo_periods()
    with pytest.raises(MissingPriceError) as caught:
        price_at(periods, date(2010, 1, 1), Tranche.BLUE_HC)
    assert caught.value.day == date(2010, 1, 1)
    with pytest.raises(MissingPriceError):
        price_at(periods, date(2026, 8, 1), Tranche.BASE)


def test_fixed_cost_is_spread_over_the_hours_of_the_year() -> None:
    period = PricePeriod(start=date(2026, 1, 1), end=None, fixed_per_year=365 * 24)
    assert fixed_cost_per_hour(period, at(2026, 6, 1, 12)) == pytest.approx(1.0)
    assert fixed_cost_per_hour(period, at(2028, 6, 1, 12)) == pytest.approx(365 / 366)


def test_cost_hour() -> None:
    calendar = {date(2026, 1, 5): TempoColor.RED}
    cost = cost_hour(at(2026, 1, 5, 21), 2.0, TEMPO, tempo_periods(), calendar)
    assert cost.energy == {Tranche.RED_HP: 2.0}
    rate = price_at(tempo_periods(), date(2026, 1, 5), Tranche.RED_HP)
    assert cost.variable == {Tranche.RED_HP: pytest.approx(2.0 * rate)}
    assert cost.fixed > 0
    assert cost.total == pytest.approx(2.0 * rate + cost.fixed)


def test_cost_hour_uses_the_price_of_the_day() -> None:
    calendar = {date(2026, 7, 31): TempoColor.BLUE}
    periods = tempo_periods()
    # 1 August 0:00 still belongs to the 31 July Tempo day but uses August prices.
    cost = cost_hour(at(2026, 8, 1, 0), 1.0, TEMPO, periods, calendar)
    assert cost.variable[Tranche.BLUE_HC] == pytest.approx(0.1356)
    assert cost.fixed == pytest.approx(189.9802924 / 365 / 24)


def test_cost_hour_missing_tranche_price() -> None:
    periods = [PricePeriod(start=date(2026, 1, 1), end=None, fixed_per_year=0)]
    with pytest.raises(MissingPriceError):
        cost_hour(at(2026, 3, 7, 15), 1.0, BASE, periods, {})
