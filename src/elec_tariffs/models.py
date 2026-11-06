"""Contracts, tranches and prices."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, time
from enum import StrEnum


class ContractKind(StrEnum):
    """How a contract splits consumption into tranches."""

    BASE = "base"
    HPHC = "hphc"
    TEMPO = "tempo"


class TempoColor(StrEnum):
    """Color of a Tempo day."""

    BLUE = "blue"
    WHITE = "white"
    RED = "red"


class Tranche(StrEnum):
    """A part of the consumption billed at one price."""

    BASE = "base"
    HC = "hc"
    HP = "hp"
    BLUE_HC = "blue_hc"
    BLUE_HP = "blue_hp"
    WHITE_HC = "white_hc"
    WHITE_HP = "white_hp"
    RED_HC = "red_hc"
    RED_HP = "red_hp"


TRANCHES: Mapping[ContractKind, tuple[Tranche, ...]] = {
    ContractKind.BASE: (Tranche.BASE,),
    ContractKind.HPHC: (Tranche.HC, Tranche.HP),
    ContractKind.TEMPO: (
        Tranche.BLUE_HC,
        Tranche.BLUE_HP,
        Tranche.WHITE_HC,
        Tranche.WHITE_HP,
        Tranche.RED_HC,
        Tranche.RED_HP,
    ),
}

DayCalendar = Mapping[date, TempoColor]


@dataclass(frozen=True, kw_only=True)
class TimeRange:
    """Off-peak hours from `start` to `end`, crossing midnight when end <= start."""

    start: time
    end: time

    def __post_init__(self) -> None:
        """Only accept whole and half hours, the finest split an hourly row allows."""
        for value in (self.start, self.end):
            if value.minute not in (0, 30) or value.second or value.microsecond:
                msg = f"{value.isoformat()} is not on a whole or half hour"
                raise ValueError(msg)

    def contains(self, value: time) -> bool:
        """Tell whether `value` falls in the range (start included, end excluded)."""
        if self.start < self.end:
            return self.start <= value < self.end
        return value >= self.start or value < self.end


@dataclass(frozen=True, kw_only=True)
class ContractRules:
    """What decides the tranche of each half hour."""

    kind: ContractKind
    off_peak: tuple[TimeRange, ...] = ()
    weekend_off_peak: bool = False

    def __post_init__(self) -> None:
        """Refuse options that only make sense for an HP/HC contract."""
        if self.kind is not ContractKind.HPHC and (
            self.off_peak or self.weekend_off_peak
        ):
            msg = "Off-peak ranges only apply to HP/HC contracts"
            raise ValueError(msg)


@dataclass(frozen=True, kw_only=True)
class PricePeriod:
    """Prices in force from `start` to `end` (both included, `end` None if open)."""

    start: date
    end: date | None
    fixed_per_year: float
    prices: Mapping[Tranche, float] = field(default_factory=dict)

    def covers(self, day: date) -> bool:
        """Tell whether the period applies on `day`."""
        return self.start <= day and (self.end is None or day <= self.end)
