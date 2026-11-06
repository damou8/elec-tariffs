"""Exceptions raised by elec-tariffs."""

from datetime import date


class ElecTariffsError(Exception):
    """Base class of every error raised by this library."""


class MissingColorError(ElecTariffsError):
    """The Tempo color of a day is not known yet."""

    def __init__(self, day: date) -> None:
        """Keep the day whose color is missing."""
        super().__init__(f"No Tempo color for {day.isoformat()}")
        self.day = day


class MissingPriceError(ElecTariffsError):
    """No price period covers a day."""

    def __init__(self, day: date) -> None:
        """Keep the day that no price period covers."""
        super().__init__(f"No price period covers {day.isoformat()}")
        self.day = day


class TrvError(ElecTariffsError):
    """The regulated prices could not be read."""


class TrvServerError(TrvError):
    """data.gouv.fr could not be reached or answered with an error."""


class TrvFormatError(TrvError):
    """A regulated price file does not have the expected columns or values."""


class TempoError(ElecTariffsError):
    """The Tempo calendar could not be read."""


class TempoServerError(TempoError):
    """The calendar source could not be reached or answered with a server error."""


class TempoRateLimitError(TempoError):
    """The calendar source asks to slow down (HTTP 429)."""


class TempoAuthError(TempoError):
    """The RTE API refused the client credentials."""


class TempoFormatError(TempoError):
    """The calendar source answered with data in an unexpected format."""
