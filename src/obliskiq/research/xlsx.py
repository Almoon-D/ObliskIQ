"""A minimal reader for the cells of one worksheet of an Office Open XML workbook (``.xlsx``).

AQR publishes its data libraries as workbooks; the harness needs the values of one sheet and
nothing else: no styles, formulas or drawings. A cell is a number (``float``), a string (shared
or inline) or a boolean; anything else, and any workbook this reader does not understand, raises
:class:`~obliskiq.errors.DataSourceError` rather than yielding a guess. A date is an Excel serial
number in the 1900 system and is converted by :func:`excel_serial_date`.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import io
import re
import zipfile
from datetime import date, timedelta
from typing import Final
from xml.etree import ElementTree

from ..errors import DataSourceError

_MAIN: Final = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_RELS: Final = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_DOC_RELS: Final = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_REF = re.compile(r"([A-Z]+)(\d+)")
#: Day zero of Excel's 1900 date system, as Excel counts it (it treats 1900 as a leap year).
EXCEL_EPOCH: Final = date(1899, 12, 30)
#: Letters in a column name.
_ALPHABET: Final = 26

type Cell = float | str | bool
type Sheet = dict[int, dict[str, Cell]]


def excel_serial_date(serial: float) -> date:
    """The date an Excel 1900-system serial number names."""
    if serial < 1 or serial != int(serial):
        raise DataSourceError(f"{serial!r} is not a whole Excel date serial")
    return EXCEL_EPOCH + timedelta(days=int(serial))


def column_index(letters: str) -> int:
    """``A`` is 0, ``Z`` 25, ``AA`` 26."""
    index = 0
    for letter in letters:
        index = index * _ALPHABET + ord(letter) - ord("A") + 1
    return index - 1


def _xml(archive: zipfile.ZipFile, name: str) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(archive.read(name))
    except KeyError:
        raise DataSourceError(f"the workbook has no part {name}") from None
    except ElementTree.ParseError as exc:
        raise DataSourceError(f"the workbook part {name} is not XML: {exc}") from None


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = _xml(archive, "xl/sharedStrings.xml")
    return ["".join(t.text or "" for t in item.iter(f"{_MAIN}t")) for item in root]


def _sheet_part(archive: zipfile.ZipFile, sheet_name: str) -> str:
    workbook = _xml(archive, "xl/workbook.xml")
    relation = None
    for sheet in workbook.iter(f"{_MAIN}sheet"):
        if sheet.get("name") == sheet_name:
            relation = sheet.get(f"{_DOC_RELS}id")
    if relation is None:
        names = [s.get("name") for s in workbook.iter(f"{_MAIN}sheet")]
        raise DataSourceError(f"the workbook has no sheet {sheet_name!r} (sheets: {names})")
    for link in _xml(archive, "xl/_rels/workbook.xml.rels").iter(f"{_RELS}Relationship"):
        if link.get("Id") == relation:
            target = link.get("Target", "")
            return target.lstrip("/") if target.startswith("/") else f"xl/{target}"
    raise DataSourceError(f"the workbook does not say where sheet {sheet_name!r} is")


def _value(cell: ElementTree.Element, shared: list[str], where: str) -> Cell | None:
    kind = cell.get("t", "n")
    if kind == "inlineStr":
        return "".join(t.text or "" for t in cell.iter(f"{_MAIN}t"))
    raw = cell.find(f"{_MAIN}v")
    if raw is None or raw.text is None:
        return None
    text = raw.text
    try:
        if kind == "s":
            return shared[int(text)]
        if kind == "n":
            return float(text)
        if kind == "b":
            return text == "1"
        if kind == "str":
            return text
    except ValueError, IndexError:
        raise DataSourceError(f"cell {where} holds {text!r}, unreadable as {kind}") from None
    raise DataSourceError(f"cell {where} has a type ({kind!r}) this reader does not know")


def read_sheet(content: bytes, sheet_name: str) -> Sheet:
    """Every non-empty cell of ``sheet_name``: row number (1-based) to column letters to value."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        raise DataSourceError("the workbook is not a zip archive") from None
    with archive:
        shared = _shared_strings(archive)
        root = _xml(archive, _sheet_part(archive, sheet_name))
    rows: Sheet = {}
    for cell in root.iter(f"{_MAIN}c"):
        reference = cell.get("r", "")
        match = _REF.fullmatch(reference)
        if match is None:
            raise DataSourceError(f"a cell has no reference this reader understands: {reference!r}")
        value = _value(cell, shared, reference)
        if value is not None:
            rows.setdefault(int(match.group(2)), {})[match.group(1)] = value
    return rows
