"""Tests of the contract models."""

from datetime import date, time

import pytest

from elec_tariffs import TRANCHES, ContractKind, ContractRules, PricePeriod, TimeRange


def test_time_range_refuses_quarter_hours() -> None:
    with pytest.raises(ValueError, match="22:15"):
        TimeRange(start=time(22, 15), end=time(6))


def test_time_range_within_a_day() -> None:
    noon = TimeRange(start=time(12, 30), end=time(14))
    assert noon.contains(time(12, 30))
    assert noon.contains(time(13, 30))
    assert not noon.contains(time(14))
    assert not noon.contains(time(12))


def test_time_range_crossing_midnight() -> None:
    night = TimeRange(start=time(22), end=time(6))
    assert night.contains(time(22))
    assert night.contains(time(0))
    assert night.contains(time(5, 30))
    assert not night.contains(time(6))
    assert not night.contains(time(21, 30))


@pytest.mark.parametrize("kind", [ContractKind.BASE, ContractKind.TEMPO])
def test_off_peak_options_only_for_hphc(kind: ContractKind) -> None:
    with pytest.raises(ValueError, match="HP/HC"):
        ContractRules(kind=kind, weekend_off_peak=True)
    with pytest.raises(ValueError, match="HP/HC"):
        ContractRules(kind=kind, off_peak=(TimeRange(start=time(22), end=time(6)),))


def test_price_period_covers() -> None:
    closed = PricePeriod(
        start=date(2026, 2, 1), end=date(2026, 7, 31), fixed_per_year=1
    )
    assert closed.covers(date(2026, 2, 1))
    assert closed.covers(date(2026, 7, 31))
    assert not closed.covers(date(2026, 8, 1))
    assert not closed.covers(date(2026, 1, 31))
    open_ended = PricePeriod(start=date(2026, 8, 1), end=None, fixed_per_year=1)
    assert open_ended.covers(date(2040, 1, 1))


def test_every_kind_has_tranches() -> None:
    assert set(TRANCHES) == set(ContractKind)
    assert len(TRANCHES[ContractKind.TEMPO]) == 6
