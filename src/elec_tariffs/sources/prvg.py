"""Gas reference prices (prix repère de vente de gaz) published by the CRE.

The CRE publishes, each month, a reference price for homes on the GRDF network:
a yearly subscription and an average price of a kWh, for two profiles (cooking and
hot water, heating). Its open data file is a spreadsheet:

- the sheet "Historique grilles" has one block per month since June 2023: a row
  with the first day of the month (an Excel date) and the profiles, a row telling
  the columns without (HT) and with taxes (TTC) apart, then the subscription and
  the average price of a kWh;
- the sheet "Grille tarifaire TTC" has the prices of the month to come, titled
  "… au 1er décembre 2026", in €/an and €/MWh.

Only the prices with taxes are read.
"""

import io
import re
import zipfile
from collections.abc import Mapping
from datetime import date, timedelta
from typing import Final
from xml.etree import ElementTree as ET

import aiohttp

from elec_tariffs.errors import PrvgFormatError, PrvgServerError
from elec_tariffs.models import GasPricePeriod, GasProfile

PRVG_URL: Final = "https://www.cre.fr/fileadmin/Documents/Open_data/Marches_de_detail/OPEN_DATA_GRDF.xlsx"

_HISTORY: Final = "Historique grilles"
_COMING: Final = "Grille tarifaire TTC"
_PROFILES: Final[Mapping[GasProfile, str]] = {
    GasProfile.COOKING_WATER: "Cuisson/eau chaude",
    GasProfile.HEATING: "Chauffage",
}
_TAXES_INCLUDED: Final = "TTC"
_SUBSCRIPTION: Final = "Abonnement"
_AVERAGE_PRICE: Final = "Part variable moyenne"
_MONTHS: Final = {
    name: number
    for number, name in enumerate(
        (
            "janvier",
            "février",
            "mars",
            "avril",
            "mai",
            "juin",
            "juillet",
            "août",
            "septembre",
            "octobre",
            "novembre",
            "décembre",
        ),
        start=1,
    )
}
_COMING_TITLE: Final = re.compile(r"au 1er (\w+) (\d{4})", re.IGNORECASE)
_EXCEL_EPOCH: Final = date(1899, 12, 30)
# Far more than the file needs (about 100 kB, sheets under 100 kB): refuse a
# file that would unpack to something huge.
_MAX_SIZE: Final = 10_000_000
_MAIN: Final = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_RELATIONSHIPS: Final = (
    "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
)
_CELL: Final = re.compile(r"([A-Z]+)\d+")
_TIMEOUT: Final = aiohttp.ClientTimeout(total=30)

type _Row = dict[str, str]


class _Workbook:
    """The cell values of an XLSX file, as text, by sheet name."""

    def __init__(self, content: bytes) -> None:
        try:
            self._zip = zipfile.ZipFile(io.BytesIO(content))
            self._shared = self._shared_strings()
            self._sheets = self._sheet_paths()
        except (zipfile.BadZipFile, KeyError, ET.ParseError) as err:
            msg = f"The gas reference prices are not a readable spreadsheet: {err}"
            raise PrvgFormatError(msg) from err

    def _read(self, name: str) -> ET.Element:
        if self._zip.getinfo(name).file_size > _MAX_SIZE:
            msg = f"{name} is too large for a gas reference price file"
            raise PrvgFormatError(msg)
        return ET.fromstring(self._zip.read(name))  # noqa: S314 - expat refuses entity expansion attacks

    def _shared_strings(self) -> list[str]:
        if "xl/sharedStrings.xml" not in self._zip.namelist():
            return []
        return [
            "".join(text.text or "" for text in item.iter(f"{_MAIN}t"))
            for item in self._read("xl/sharedStrings.xml").iter(f"{_MAIN}si")
        ]

    def _sheet_paths(self) -> dict[str, str]:
        targets = {
            relation.get("Id"): relation.get("Target", "")
            for relation in self._read("xl/_rels/workbook.xml.rels")
        }
        paths: dict[str, str] = {}
        for sheet in self._read("xl/workbook.xml").iter(f"{_MAIN}sheet"):
            target = targets.get(sheet.get(_RELATIONSHIPS))
            if target:
                paths[sheet.get("name", "")] = (
                    target.lstrip("/") if target.startswith("/") else f"xl/{target}"
                )
        return paths

    def rows(self, sheet: str) -> list[_Row]:
        """Return the rows of a sheet, each cell's text by its column letters."""
        if sheet not in self._sheets:
            msg = f"The gas reference prices have no sheet {sheet!r}"
            raise PrvgFormatError(msg)
        try:
            root = self._read(self._sheets[sheet])
        except (KeyError, ET.ParseError) as err:
            msg = f"The sheet {sheet!r} can't be read: {err}"
            raise PrvgFormatError(msg) from err
        return [self._row(row) for row in root.iter(f"{_MAIN}row")]

    def _row(self, row: ET.Element) -> _Row:
        cells: _Row = {}
        for cell in row.iter(f"{_MAIN}c"):
            match = _CELL.fullmatch(cell.get("r", ""))
            if match is None:
                continue
            if cell.get("t") == "inlineStr":
                value = "".join(text.text or "" for text in cell.iter(f"{_MAIN}t"))
            elif (raw := cell.find(f"{_MAIN}v")) is None or raw.text is None:
                continue
            elif cell.get("t") == "s":
                try:
                    value = self._shared[int(raw.text)]
                except (IndexError, ValueError) as err:
                    msg = f"Cell {cell.get('r')} points to no text"
                    raise PrvgFormatError(msg) from err
            else:
                value = raw.text
            cells[match.group(1)] = value.strip()
        return cells


def _number(value: str | None, where: str) -> float:
    try:
        return float(value or "")
    except ValueError as err:
        msg = f"{where} is not a number: {value!r}"
        raise PrvgFormatError(msg) from err


def _excel_date(value: str) -> date | None:
    try:
        serial = float(value)
    except ValueError:
        return None
    if not serial.is_integer():
        return None
    return _EXCEL_EPOCH + timedelta(days=int(serial))


def _taxed_column(profile_row: _Row, taxes_row: _Row, profile: GasProfile) -> str:
    """Return the column of a profile's prices with taxes.

    The profile's name heads its two merged columns, without then with taxes.
    """
    for column, value in profile_row.items():
        if value != _PROFILES[profile]:
            continue
        for candidate in sorted(taxes_row, key=lambda name: (len(name), name)):
            if (len(candidate), candidate) >= (len(column), column) and (
                taxes_row[candidate] == _TAXES_INCLUDED
            ):
                return candidate
    msg = f"No {_PROFILES[profile]!r} prices with taxes"
    raise PrvgFormatError(msg)


def _labelled(rows: list[_Row], label: str) -> _Row:
    for row in rows:
        if any(value.startswith(label) for value in row.values()):
            return row
    msg = f"No {label!r} row"
    raise PrvgFormatError(msg)


def _history(
    workbook: _Workbook, profile: GasProfile
) -> dict[date, tuple[float, float]]:
    """Return the subscription and price of a kWh of each month, by its first day."""
    rows = workbook.rows(_HISTORY)
    months: dict[date, tuple[float, float]] = {}
    for index, row in enumerate(rows):
        first_day = _excel_date(row.get("A", ""))
        if first_day is None or _PROFILES[profile] not in row.values():
            continue
        block = rows[index + 1 : index + 6]
        if not block:
            msg = f"The prices of {first_day.isoformat()} are missing"
            raise PrvgFormatError(msg)
        column = _taxed_column(row, block[0], profile)
        where = f"The {profile} price of {first_day.isoformat()}"
        months[first_day] = (
            _number(_labelled(block, _SUBSCRIPTION).get(column), where),
            _number(_labelled(block, _AVERAGE_PRICE).get(column), where),
        )
    if not months:
        msg = f"The sheet {_HISTORY!r} has no {profile} prices"
        raise PrvgFormatError(msg)
    return months


def _numeric(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True


def _coming(workbook: _Workbook, profile: GasProfile) -> tuple[date, float, float]:
    """Return the first day, subscription and price of a kWh of the month to come."""
    rows = workbook.rows(_COMING)
    first_day: date | None = None
    for row in rows:
        for value in row.values():
            if (match := _COMING_TITLE.search(value)) and (
                month := _MONTHS.get(match.group(1).lower())
            ):
                first_day = date(int(match.group(2)), month, 1)
                break
        if first_day:
            break
    if first_day is None:
        msg = f"The sheet {_COMING!r} doesn't say when its prices apply"
        raise PrvgFormatError(msg)
    # The profile's own row, named exactly as in the history.
    found = next(
        (
            (row, column)
            for row in rows
            for column, value in row.items()
            if value == _PROFILES[profile]
        ),
        None,
    )
    if found is None:
        msg = f"The sheet {_COMING!r} has no {_PROFILES[profile]!r} row"
        raise PrvgFormatError(msg)
    row, label = found
    columns = sorted(row, key=lambda name: (len(name), name))
    numbers = [column for column in columns if _numeric(row[column])]
    # After the label: the subscription (€/an), then the lowest, average and
    # highest prices of a MWh.
    after = [
        column for column in numbers if (len(column), column) > (len(label), label)
    ]
    if len(after) < 3:  # noqa: PLR2004
        msg = f"The {profile} prices of the month to come are incomplete"
        raise PrvgFormatError(msg)
    where = f"The {profile} price of {first_day.isoformat()}"
    subscription = _number(row[after[0]], where)
    per_mwh = _number(row[after[2]], where)
    return first_day, subscription, per_mwh / 1000


def parse_prvg_xlsx(content: bytes, profile: GasProfile) -> list[GasPricePeriod]:
    """Return the reference prices of one profile, oldest first, with taxes.

    Each month's prices last until the next month's; the last ones are open.
    Raise PrvgFormatError when a sheet, a row or a value can't be read.
    """
    workbook = _Workbook(content)
    months = _history(workbook, profile)
    first_day, subscription, price = _coming(workbook, profile)
    if first_day > max(months):
        months[first_day] = (subscription, price)
    starts = sorted(months)
    return [
        GasPricePeriod(
            start=start,
            end=following - timedelta(days=1) if following else None,
            fixed_per_year=months[start][0],
            price=months[start][1],
        )
        for start, following in zip(starts, [*starts[1:], None], strict=True)
    ]


class PrvgClient:
    """Download the gas reference prices from the CRE."""

    def __init__(self, session: aiohttp.ClientSession, *, url: str = PRVG_URL) -> None:
        """Use the caller's HTTP session; `url` replaces the CRE's file."""
        self._session = session
        self._url = url

    async def async_price_periods(self, profile: GasProfile) -> list[GasPricePeriod]:
        """Return the reference prices of one profile, oldest first, with taxes."""
        try:
            async with self._session.get(self._url, timeout=_TIMEOUT) as response:
                response.raise_for_status()
                content = await response.read()
        except (aiohttp.ClientError, TimeoutError) as err:
            msg = f"Could not download the gas reference prices: {err}"
            raise PrvgServerError(msg) from err
        return parse_prvg_xlsx(content, profile)
