"""Tests of the gas reference prices from the CRE."""

import io
import itertools
import zipfile
from collections.abc import Callable
from datetime import date
from xml.sax.saxutils import escape

import aiohttp
import pytest

from elec_tariffs import (
    PRVG_URL,
    GasProfile,
    PrvgClient,
    PrvgFormatError,
    PrvgServerError,
    parse_prvg_xlsx,
)

from .conftest import FIXTURES, FakeServer

type Cells = dict[str, str | float]

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
RELS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def real_file() -> bytes:
    return (FIXTURES / "cre_open_data_grdf.xlsx").read_bytes()


def month(
    serial: int, cooking: tuple[float, float], heating: tuple[float, float]
) -> list[Cells]:
    """One block of the history sheet: subscription and price, with taxes."""
    return [
        {"A": serial, "B": "Cuisson/eau chaude", "D": "Chauffage"},
        {"B": "HT", "C": "TTC", "D": "HT", "E": "TTC"},
        {
            "A": "Abonnement (€/an)",
            "B": 1.0,
            "C": cooking[0],
            "D": 1.0,
            "E": heating[0],
        },
        {
            "A": "Part variable moyenne (€/kWh)",
            "B": 0.01,
            "C": cooking[1],
            "D": 0.01,
            "E": heating[1],
        },
        {},
    ]


def coming(
    title: str = "Grilles tarifaires TTC prix repère au 1er février 2024",
) -> list[Cells]:
    return [
        {"B": title},
        {"C": "Part fixe en €/an", "D": "Part variable (€/MWh)"},
        {"B": "Cuisson/eau chaude", "C": 130.0, "D": 190.0, "E": 200.0, "F": 210.0},
        {"B": "Chauffage", "C": 300.0, "D": 150.0, "E": 160.0, "F": 170.0},
    ]


HISTORY = [
    # 1 December 2023 and 1 January 2024, the newest first like the CRE's file.
    *month(45292, (120.0, 0.18), (280.0, 0.14)),
    *month(45261, (110.0, 0.17), (270.0, 0.13)),
]


def cell(reference: str, value: str | float, strings: list[str]) -> str:
    if isinstance(value, str):
        strings.append(value)
        return f'<c r="{reference}" t="s"><v>{len(strings) - 1}</v></c>'
    return f'<c r="{reference}"><v>{value}</v></c>'


def workbook(sheets: dict[str, list[Cells]], *, inline: bool = False) -> bytes:
    """Build an XLSX file the way Excel lays it out, with shared strings."""
    strings: list[str] = []
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        listed = []
        relations = []
        for number, (name, rows) in enumerate(sheets.items(), start=1):
            listed.append(
                f'<sheet name="{escape(name)}" sheetId="{number}" r:id="rId{number}"/>'
            )
            relations.append(
                f'<Relationship Id="rId{number}" Type="{RELS}/worksheet" '
                f'Target="worksheets/sheet{number}.xml"/>'
            )
            body = []
            for index, row in enumerate(rows, start=1):
                cells = "".join(
                    f'<c r="{column}{index}" t="inlineStr">'
                    f"<is><t>{escape(value)}</t></is></c>"
                    if inline and isinstance(value, str)
                    else cell(f"{column}{index}", value, strings)
                    for column, value in row.items()
                )
                body.append(f'<row r="{index}">{cells}</row>')
            archive.writestr(
                f"xl/worksheets/sheet{number}.xml",
                f'<worksheet xmlns="{MAIN}"><sheetData>{"".join(body)}'
                "</sheetData></worksheet>",
            )
        archive.writestr(
            "xl/workbook.xml",
            f'<workbook xmlns="{MAIN}" xmlns:r="{RELS}">'
            f"<sheets>{''.join(listed)}</sheets></workbook>",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f"{''.join(relations)}</Relationships>",
        )
        if strings:
            shared = "".join(f"<si><t>{escape(text)}</t></si>" for text in strings)
            archive.writestr(
                "xl/sharedStrings.xml", f'<sst xmlns="{MAIN}">{shared}</sst>'
            )
    return buffer.getvalue()


def rewritten(content: bytes, name: str, change: Callable[[bytes], bytes]) -> bytes:
    """Copy an XLSX file with one of its files changed."""
    buffer = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(content)) as source,
        zipfile.ZipFile(buffer, "w") as copy,
    ):
        for item in source.infolist():
            data = source.read(item)
            copy.writestr(item, change(data) if item.filename == name else data)
    return buffer.getvalue()


def test_heating_prices_since_june_2023() -> None:
    periods = parse_prvg_xlsx(real_file(), GasProfile.HEATING)
    assert periods[0].start == date(2023, 6, 1)
    assert periods[0].end == date(2023, 6, 30)
    assert periods[0].fixed_per_year == pytest.approx(249.48)
    assert periods[0].price == pytest.approx(0.09446)
    november = next(period for period in periods if period.start == date(2026, 11, 1))
    assert november.end == date(2026, 11, 30)
    assert november.fixed_per_year == pytest.approx(360.79, abs=0.01)
    assert november.price == pytest.approx(0.1576)


def test_the_month_to_come_is_open() -> None:
    periods = parse_prvg_xlsx(real_file(), GasProfile.COOKING_WATER)
    last = periods[-1]
    assert last.start == date(2026, 12, 1)
    assert last.end is None
    assert last.fixed_per_year == pytest.approx(152.46, abs=0.01)
    assert last.price == pytest.approx(0.20155, abs=0.00001)


def test_months_follow_each_other() -> None:
    periods = parse_prvg_xlsx(real_file(), GasProfile.HEATING)
    for before, after in itertools.pairwise(periods):
        assert before.end is not None
        assert (after.start - before.end).days == 1


def test_built_workbook() -> None:
    content = workbook(
        {"Historique grilles": HISTORY, "Grille tarifaire TTC": coming()}
    )
    periods = parse_prvg_xlsx(content, GasProfile.HEATING)
    assert [(period.start, period.end) for period in periods] == [
        (date(2023, 12, 1), date(2023, 12, 31)),
        (date(2024, 1, 1), date(2024, 1, 31)),
        (date(2024, 2, 1), None),
    ]
    assert [period.price for period in periods] == [0.13, 0.14, pytest.approx(0.16)]
    assert periods[-1].fixed_per_year == 300.0


def test_inline_strings() -> None:
    content = workbook(
        {"Historique grilles": HISTORY, "Grille tarifaire TTC": coming()}, inline=True
    )
    assert parse_prvg_xlsx(content, GasProfile.COOKING_WATER)[0].price == 0.17


def test_month_to_come_already_in_the_history() -> None:
    content = workbook(
        {
            "Historique grilles": HISTORY,
            "Grille tarifaire TTC": coming(
                "Grilles tarifaires TTC prix repère au 1er janvier 2024"
            ),
        }
    )
    periods = parse_prvg_xlsx(content, GasProfile.HEATING)
    assert periods[-1].start == date(2024, 1, 1)
    assert periods[-1].end is None
    assert periods[-1].price == 0.14


@pytest.mark.parametrize(
    ("sheets", "message"),
    [
        pytest.param({"Grille tarifaire TTC": coming()}, "no sheet", id="no-history"),
        pytest.param({"Historique grilles": HISTORY}, "no sheet", id="no-coming"),
        pytest.param(
            {
                "Historique grilles": [{"A": "nothing"}],
                "Grille tarifaire TTC": coming(),
            },
            "no heating prices",
            id="empty-history",
        ),
        pytest.param(
            {
                "Historique grilles": [
                    {"A": 45292, "B": "Cuisson/eau chaude", "D": "Chauffage"}
                ],
                "Grille tarifaire TTC": coming(),
            },
            "missing",
            id="block-cut-short",
        ),
        pytest.param(
            {
                "Historique grilles": [
                    {"A": 45292, "B": "Cuisson/eau chaude", "D": "Chauffage"},
                    {"B": "HT", "C": "HT", "D": "HT", "E": "HT"},
                ],
                "Grille tarifaire TTC": coming(),
            },
            "with taxes",
            id="no-taxed-column",
        ),
        pytest.param(
            {
                "Historique grilles": month(45292, (1.0, 0.1), (1.0, 0.1))[:3],
                "Grille tarifaire TTC": coming(),
            },
            "Part variable moyenne",
            id="no-price-row",
        ),
        pytest.param(
            {
                "Historique grilles": [
                    *month(45292, (1.0, 0.1), (1.0, 0.1))[:3],
                    {"A": "Part variable moyenne (€/kWh)", "E": "n/a"},
                ],
                "Grille tarifaire TTC": coming(),
            },
            "not a number",
            id="unreadable-price",
        ),
        pytest.param(
            {
                "Historique grilles": HISTORY,
                "Grille tarifaire TTC": coming("Grilles TTC"),
            },
            "when its prices apply",
            id="undated-coming",
        ),
        pytest.param(
            {
                "Historique grilles": HISTORY,
                "Grille tarifaire TTC": [*coming()[:3], {"B": "Chauffage", "C": 300.0}],
            },
            "incomplete",
            id="coming-incomplete",
        ),
        pytest.param(
            {
                "Historique grilles": HISTORY,
                "Grille tarifaire TTC": [
                    *coming()[:3],
                    {"B": "Chauffage individuel", "C": 300.0, "D": 1.0, "E": 2.0},
                ],
            },
            "no 'Chauffage' row",
            id="coming-profile-renamed",
        ),
    ],
)
def test_unexpected_content(sheets: dict[str, list[Cells]], message: str) -> None:
    with pytest.raises(PrvgFormatError, match=message):
        parse_prvg_xlsx(workbook(sheets), GasProfile.HEATING)


def test_not_a_spreadsheet() -> None:
    with pytest.raises(PrvgFormatError, match="readable spreadsheet"):
        parse_prvg_xlsx(b"<html>Maintenance</html>", GasProfile.HEATING)


def test_shared_string_out_of_range() -> None:
    content = rewritten(
        workbook({"Historique grilles": HISTORY, "Grille tarifaire TTC": coming()}),
        "xl/worksheets/sheet1.xml",
        lambda data: data.replace(b"<v>0</v>", b"<v>999</v>", 1),
    )
    with pytest.raises(PrvgFormatError, match="points to no text"):
        parse_prvg_xlsx(content, GasProfile.HEATING)


def test_sheet_too_large() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", f'<workbook xmlns="{MAIN}"/>')
        archive.writestr("xl/_rels/workbook.xml.rels", "<Relationships/>")
        archive.writestr("xl/sharedStrings.xml", b" " * 10_000_001)
    with pytest.raises(PrvgFormatError, match="too large"):
        parse_prvg_xlsx(buffer.getvalue(), GasProfile.HEATING)


def test_sheet_that_is_not_xml() -> None:
    content = rewritten(
        workbook({"Historique grilles": HISTORY, "Grille tarifaire TTC": coming()}),
        "xl/worksheets/sheet1.xml",
        lambda _: b"not xml",
    )
    with pytest.raises(PrvgFormatError, match="can't be read"):
        parse_prvg_xlsx(content, GasProfile.HEATING)


async def test_client_downloads_and_parses(
    session: aiohttp.ClientSession, server: FakeServer
) -> None:
    async def answer(_: object) -> aiohttp.web.Response:
        return aiohttp.web.Response(body=real_file())

    server.add("GET", "/grdf.xlsx", answer)
    client = PrvgClient(session, url=f"{server.url}/grdf.xlsx")
    periods = await client.async_price_periods(GasProfile.HEATING)
    assert periods[-1].start == date(2026, 12, 1)


async def test_client_server_error(
    session: aiohttp.ClientSession, server: FakeServer
) -> None:
    server.text("GET", "/grdf.xlsx", "Unavailable", status=503)
    with pytest.raises(PrvgServerError):
        await PrvgClient(session, url=f"{server.url}/grdf.xlsx").async_price_periods(
            GasProfile.HEATING
        )


async def test_client_unreachable(
    session: aiohttp.ClientSession, closed_url: str
) -> None:
    with pytest.raises(PrvgServerError):
        await PrvgClient(session, url=closed_url).async_price_periods(
            GasProfile.COOKING_WATER
        )


def test_default_url_is_the_cre() -> None:
    assert PRVG_URL.startswith("https://www.cre.fr/")


def test_cells_without_reference_and_dates_with_a_time_are_skipped() -> None:
    history: list[Cells] = [{"A": 45292.5, "D": "Chauffage"}, *HISTORY]
    content = rewritten(
        workbook({"Historique grilles": history, "Grille tarifaire TTC": coming()}),
        "xl/worksheets/sheet1.xml",
        lambda data: data.replace(b'<row r="1">', b'<row r="1"><c><v>7</v></c>', 1),
    )
    periods = parse_prvg_xlsx(content, GasProfile.HEATING)
    assert periods[0].start == date(2023, 12, 1)


def test_period_covers() -> None:
    period = parse_prvg_xlsx(real_file(), GasProfile.HEATING)[0]
    assert period.covers(date(2023, 6, 30))
    assert not period.covers(date(2023, 7, 1))
    assert not period.covers(date(2023, 5, 31))


def test_sheet_without_a_file() -> None:
    content = rewritten(
        workbook({"Historique grilles": HISTORY, "Grille tarifaire TTC": coming()}),
        "xl/_rels/workbook.xml.rels",
        lambda data: data.replace(b'Id="rId1"', b'Id="rId9"'),
    )
    with pytest.raises(PrvgFormatError, match="no sheet 'Historique grilles'"):
        parse_prvg_xlsx(content, GasProfile.HEATING)
