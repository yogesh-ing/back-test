"""A minimal OOXML (``.xlsx``) writer — stdlib only.

The ITR annexures have to open in Excel, LibreOffice or Google Sheets, and a
``.csv`` alone is not that (multi-sheet, typed numbers, a header row that stays
readable). ``openpyxl`` is not a dependency of this project, so this module
writes the subset of the format that a spreadsheet actually needs: one or more
worksheets of inline-string and number cells, a bold header row, and a
thousands-separated number format.

What it deliberately does not do: formulas, styling beyond bold/number format,
shared strings, or charts. If a future export needs any of those, add the
dependency then — do not grow this into a spreadsheet engine.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Sequence
from xml.sax.saxutils import escape

__all__ = ["Workbook", "Worksheet", "write_xlsx", "write_csv"]

logger = logging.getLogger("backtest.reporting.exports.xlsx")

_ILLEGAL_SHEET_CHARS = re.compile(r"[\[\]:*?/\\]")
_NUMBER_FORMAT = "#,##0.00"
_STYLE_DEFAULT = 0
_STYLE_BOLD = 1
_STYLE_NUMBER = 2
_STYLE_BOLD_NUMBER = 3


@dataclass
class Worksheet:
    """One sheet: a title plus rows of values (first row rendered bold)."""

    title: str
    rows: list[list[Any]] = field(default_factory=list)
    #: Optional note rows appended after a blank line (kept out of the grid).
    notes: list[str] = field(default_factory=list)

    def append(self, row: Sequence[Any]) -> None:
        self.rows.append(list(row))

    def add_note(self, note: str) -> None:
        self.notes.append(str(note))


@dataclass
class Workbook:
    """A collection of worksheets."""

    sheets: list[Worksheet] = field(default_factory=list)

    def add_sheet(self, title: str, rows: Iterable[Sequence[Any]] | None = None) -> Worksheet:
        sheet = Worksheet(title=_safe_title(title))
        for row in rows or ():
            sheet.append(row)
        self.sheets.append(sheet)
        return sheet

    def to_bytes(self) -> bytes:
        return write_xlsx(self)


def _safe_title(title: str) -> str:
    cleaned = _ILLEGAL_SHEET_CHARS.sub(" ", str(title or "Sheet")).strip() or "Sheet"
    return cleaned[:31]


def _cell_reference(row_index: int, column_index: int) -> str:
    letters = ""
    index = column_index
    while True:
        letters = chr(ord("A") + index % 26) + letters
        index = index // 26 - 1
        if index < 0:
            break
    return f"{letters}{row_index}"


def _cell_xml(ref: str, value: Any, *, bold: bool) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        style = _STYLE_BOLD if bold else _STYLE_DEFAULT
        return f'<c r="{ref}" s="{style}" t="b"><v>{int(value)}</v></c>'
    if isinstance(value, (int, float, Decimal)):
        style = _STYLE_BOLD_NUMBER if bold else _STYLE_NUMBER
        number = f"{float(value):.4f}".rstrip("0").rstrip(".") or "0"
        return f'<c r="{ref}" s="{style}"><v>{number}</v></c>'
    if isinstance(value, (date, datetime)):
        value = value.isoformat()
    text = escape(str(value))
    style = _STYLE_BOLD if bold else _STYLE_DEFAULT
    return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'


def _sheet_xml(sheet: Worksheet) -> str:
    rows_xml: list[str] = []
    for row_index, row in enumerate(sheet.rows, start=1):
        cells = [
            _cell_xml(_cell_reference(row_index, col_index), value, bold=row_index == 1)
            for col_index, value in enumerate(row)
        ]
        rows_xml.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    if sheet.notes:
        rows_xml.append("")
        start = len(sheet.rows) + 2
        for offset, note in enumerate(sheet.notes):
            ref = _cell_reference(start + offset, 0)
            rows_xml.append(
                f'<row r="{start + offset}">{_cell_xml(ref, note, bold=False)}</row>'
            )
    widths = _column_widths(sheet)
    cols_xml = ""
    if widths:
        cols_xml = "<cols>" + "".join(
            f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>'
            for i, w in enumerate(widths)
        ) + "</cols>"
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"{cols_xml}<sheetData>{''.join(rows_xml)}</sheetData></worksheet>"
    )


def _column_widths(sheet: Worksheet) -> list[float]:
    if not sheet.rows:
        return []
    width = max(len(row) for row in sheet.rows)
    widths = []
    for index in range(width):
        longest = 8
        for row in sheet.rows:
            if index < len(row):
                longest = max(longest, len(str(row[index])) + 2)
        widths.append(min(longest, 42))
    return widths


def _styles_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<numFmts count="1"><numFmt numFmtId="164" formatCode="{_NUMBER_FORMAT}"/></numFmts>'
        '<fonts count="2">'
        '<font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><name val="Calibri"/></font>'
        "</fonts>"
        '<fills count="2"><fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill></fills>'
        '<borders count="1"><border/></borders>'
        '<cellStyleXfs count="1">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="4">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
        '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" '
        'applyNumberFormat="1"/>'
        '<xf numFmtId="164" fontId="1" fillId="0" borderId="0" xfId="0" '
        'applyNumberFormat="1" applyFont="1"/>'
        "</cellXfs>"
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        "</styleSheet>"
    )


def write_xlsx(workbook: Workbook, path: str | None = None) -> bytes:
    """Serialise ``workbook``; return the bytes (and optionally write to ``path``)."""
    sheets = workbook.sheets or [Worksheet(title="Sheet1", rows=[["(empty)"]])]
    base = "application/vnd.openxmlformats-officedocument.spreadsheetml"
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f'<Override PartName="/xl/workbook.xml" ContentType="{base}.sheet.main+xml"/>'
        f'<Override PartName="/xl/styles.xml" ContentType="{base}.styles+xml"/>'
        + "".join(
            f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
            f'ContentType="{base}.worksheet+xml"/>'
            for i in range(1, len(sheets) + 1)
        )
        + "</Types>"
    )
    rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{rel_ns}/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        "<sheets>"
        + "".join(
            f'<sheet name="{escape(sheet.title)}" sheetId="{i}" r:id="rId{i}"/>'
            for i, sheet in enumerate(sheets, start=1)
        )
        + "</sheets></workbook>"
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(
            f'<Relationship Id="rId{i}" Type="{rel_ns}/worksheet" '
            f'Target="worksheets/sheet{i}.xml"/>'
            for i in range(1, len(sheets) + 1)
        )
        + f'<Relationship Id="rId{len(sheets) + 1}" Type="{rel_ns}/styles" '
        'Target="styles.xml"/>'
        "</Relationships>"
    )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/styles.xml", _styles_xml())
        for index, sheet in enumerate(sheets, start=1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", _sheet_xml(sheet))
    payload = buffer.getvalue()
    if path:
        with open(path, "wb") as handle:
            handle.write(payload)
    return payload


def write_csv(sheet: Worksheet, path: str | None = None) -> str:
    """Write one sheet as CSV text (and optionally to ``path``)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    for row in sheet.rows:
        writer.writerow([_csv_value(v) for v in row])
    for note in sheet.notes:
        # Notes are annotations, not data: keep them as literal ``#`` comment
        # lines so a reader can spot them by prefix. (Passing them through the
        # CSV writer quotes any note containing a comma, which hides the "#"
        # from exactly the check that looks for it.) Newlines are flattened so
        # one note can never look like two rows.
        comment = "# " + " ".join(str(note).replace("\r", " ").split())
        buffer.write(comment + "\r\n")
    text = buffer.getvalue()
    if path:
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
    return text


def _csv_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return "" if value is None else value
