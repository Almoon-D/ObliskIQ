"""The minimal workbook reader on hand-built workbooks: every cell type it knows, and a refusal
for everything it does not.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import io
import zipfile
from datetime import date
from typing import Any

import pytest

from obliskiq.config.schema_research import AqrDataset
from obliskiq.errors import DataSourceError
from obliskiq.research.data import parse_aqr, parse_ecb
from obliskiq.research.xlsx import column_index, excel_serial_date, read_sheet

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
DOC = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def workbook(
    cells: str,
    *,
    shared: list[str] | None = None,
    target: str = "worksheets/sheet1.xml",
    parts: dict[str, str] | None = None,
) -> bytes:
    files = {
        "xl/workbook.xml": (
            f'<workbook xmlns="{MAIN}" xmlns:r="{DOC}"><sheets>'
            '<sheet name="Data" sheetId="1" r:id="rId1"/></sheets></workbook>'
        ),
        "xl/_rels/workbook.xml.rels": (
            f'<Relationships xmlns="{RELS}"><Relationship Id="rId1" Target="{target}"/>'
            "</Relationships>"
        ),
        "xl/worksheets/sheet1.xml": (
            f'<worksheet xmlns="{MAIN}"><sheetData>{cells}</sheetData></worksheet>'
        ),
    }
    if shared is not None:
        items = "".join(f"<si><t>{text}</t></si>" for text in shared)
        files["xl/sharedStrings.xml"] = f'<sst xmlns="{MAIN}">{items}</sst>'
    files.update(parts or {})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return buffer.getvalue()


def row(number: int, *cells: str) -> str:
    return f'<row r="{number}">{"".join(cells)}</row>'


def test_every_cell_type_is_read():
    content = workbook(
        row(
            1,
            '<c r="A1" t="s"><v>0</v></c>',
            '<c r="B1" t="inlineStr"><is><t>inline</t></is></c>',
            '<c r="C1"><v>2.5</v></c>',
            '<c r="D1" t="b"><v>1</v></c>',
            '<c r="E1" t="str"><v>formula</v></c>',
            '<c r="F1"/>',
        ),
        shared=["shared"],
    )
    assert read_sheet(content, "Data") == {
        1: {"A": "shared", "B": "inline", "C": 2.5, "D": True, "E": "formula"}
    }


def test_a_workbook_without_shared_strings_or_with_an_absolute_target_is_read():
    content = workbook(row(1, '<c r="A1"><v>1</v></c>'), target="/xl/worksheets/sheet1.xml")
    assert read_sheet(content, "Data") == {1: {"A": 1.0}}


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"not a zip", "not a zip archive"),
        (workbook(row(1, '<c r="A1" t="e"><v>#N/A</v></c>')), "type .'e'. this reader"),
        (workbook(row(1, '<c r="A1" t="s"><v>3</v></c>'), shared=[]), "unreadable as s"),
        (workbook(row(1, '<c r="A1"><v>x</v></c>')), "unreadable as n"),
        (workbook(row(1, '<c r="1A"><v>1</v></c>')), "no reference this reader understands"),
        (workbook("", target="worksheets/missing.xml"), "no part xl/worksheets/missing.xml"),
        (
            workbook("", parts={"xl/worksheets/sheet1.xml": "<not xml"}),
            "is not XML",
        ),
        (
            workbook(
                "",
                parts={"xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{RELS}"/>'},
            ),
            "does not say where sheet 'Data' is",
        ),
    ],
)
def test_a_workbook_this_reader_does_not_understand_is_refused(content, message):
    with pytest.raises(DataSourceError, match=message):
        read_sheet(content, "Data")


def test_a_missing_sheet_names_the_ones_there():
    with pytest.raises(DataSourceError, match=r"no sheet 'Other' \(sheets: \['Data'\]\)"):
        read_sheet(workbook(""), "Other")


def test_excel_serials_and_column_letters():
    assert excel_serial_date(31078) == date(1985, 1, 31)
    assert excel_serial_date(1) == date(1899, 12, 31)
    for bad in (0, 1.5):
        with pytest.raises(DataSourceError, match="not a whole Excel date serial"):
            excel_serial_date(bad)
    assert [column_index(name) for name in ("A", "Z", "AA", "AB")] == [0, 25, 26, 27]


def aqr(**overrides: Any) -> AqrDataset:
    fields: dict[str, Any] = {
        "id": "a",
        "vendor": "aqr",
        "file": "f.xlsx",
        "sheet": "Data",
        "header_row": 1,
        "columns": ["X"],
        "units": "decimal",
        "release_lag_days": 0,
        "bias_notes": ["n"],
    }
    fields.update(overrides)
    return AqrDataset(**fields)


def test_aqr_rows_without_a_date_or_a_value_are_skipped_and_bad_cells_refused():
    header = row(1, '<c r="B1" t="inlineStr"><is><t>X</t></is></c>')
    content = workbook(
        header
        + row(2, '<c r="B2"><v>0.5</v></c>')  # no date: a note under the table
        + row(3, '<c r="A3"><v>31078</v></c>')  # a date without a value
        + row(4, '<c r="A4"><v>31106</v></c>', '<c r="B4"><v>0.25</v></c>')
    )
    assert parse_aqr(content, aqr()) == {"X": [(date(1985, 2, 28), 0.25)]}
    text_date = workbook(header + row(2, '<c r="A2" t="inlineStr"><is><t>Jan</t></is></c>'))
    with pytest.raises(DataSourceError, match="is not a serial"):
        parse_aqr(text_date, aqr())
    text_value = workbook(
        header
        + row(2, '<c r="A2"><v>31078</v></c>', '<c r="B2" t="inlineStr"><is><t>n/a</t></is></c>')
    )
    with pytest.raises(DataSourceError, match="row 2 X: 'n/a'"):
        parse_aqr(text_value, aqr())


def test_the_ecb_file_skips_blank_rows():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "eurofxref-hist.csv", "Date,USD,\n2024-01-02,1.0956,\n\n,\n2024-01-03,1.0919\n"
        )
    rates = parse_ecb(buffer.getvalue(), ["USD"])
    assert rates == {"USD": [(date(2024, 1, 2), 1.0956), (date(2024, 1, 3), 1.0919)]}
