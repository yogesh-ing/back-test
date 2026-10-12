"""Exporters — PDF, XLSX and CSV annexures (PRD-002).

The point of these tests is that a *filer* can trust the artifact without a
library being installed: the files must be structurally valid, must carry the
disclaimer, and must not claim an STT deduction a capital-gains schedule is not
allowed to take.

``pypdf``/``openpyxl`` are optional readers used only to double-check the
artifacts when they happen to be installed (same pattern as the simulator's
trade-analyzer tests); the stdlib assertions are the contract.
"""

from __future__ import annotations

import io
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from decimal import Decimal

import pytest

from backtest.reporting.consolidator import ConsolidatedPnL
from backtest.reporting.exports import (
    ITR_ANNEXURES,
    Workbook,
    build_workbook,
    capital_gains_schedule,
    fno_pnl_statement,
    itr_guidance,
    render_pnl_pdf,
    stt_summary,
    write_csv,
    write_xlsx,
)
from backtest.reporting.exports.itr import turnover_sheet
from backtest.reporting.exports.xlsx import Worksheet
from backtest.reporting.records import TradeRecord
from backtest.simulator.fees import FeeBreakdown, TradeSegment


def dec(value) -> Decimal:
    return Decimal(str(value))


class StaticSource:
    name = "static"

    def __init__(self, records):
        self.records = list(records)

    def fetch(self, period):
        return [r for r in self.records if period.contains(r.exit_time)]

    def describe(self):
        return {"kind": "static", "name": self.name, "note": f"{len(self.records)} record(s)"}


def record(**kwargs) -> TradeRecord:
    kwargs.setdefault("broker", "mstock")
    kwargs.setdefault("mode", "live")
    kwargs.setdefault("segment", TradeSegment.EQUITY_DELIVERY)
    kwargs.setdefault("symbol", "INFY")
    kwargs.setdefault("quantity", dec(10))
    kwargs.setdefault("entry_price", dec(100))
    kwargs.setdefault("exit_price", dec(110))
    kwargs.setdefault("entry_time", datetime(2026, 5, 4, 10, 0))
    kwargs.setdefault("exit_time", datetime(2026, 5, 4, 15, 0))
    kwargs.setdefault("gross_pnl", dec(1000))
    return TradeRecord(**kwargs)


@pytest.fixture()
def report():
    """A mixed book: capital gains, F&O, and a paper trade that must not file."""
    records = [
        record(  # delivery gain, STT paid on both sides
            trade_id="cg",
            symbol="TCS",
            gross_pnl=dec(40000),
            fees=FeeBreakdown(components={"stt": dec(400), "brokerage": dec(20)}),
            exit_time=datetime(2026, 5, 20, 15, 0),
        ),
        record(
            trade_id="fno",
            symbol="NIFTY26JUNFUT",
            segment=TradeSegment.FUTURES,
            gross_pnl=dec(-1500),
            fees=FeeBreakdown(components={"stt": dec(60), "brokerage": dec(40)}),
            exit_time=datetime(2026, 6, 4, 15, 0),
        ),
        record(
            trade_id="paper",
            symbol="ITC",
            mode="paper",
            gross_pnl=dec(9000),
            exit_time=datetime(2026, 6, 10, 15, 0),
        ),
    ]
    return ConsolidatedPnL(sources=[StaticSource(records)]).generate_report(
        "2026-04-01", "2026-09-30"
    )


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def test_pdf_is_wellformed_and_ends_cleanly(report):
    blob = render_pnl_pdf(report)
    assert blob.startswith(b"%PDF-1.4")
    assert blob.rstrip().endswith(b"%%EOF")
    assert b"/Type /Catalog" in blob
    assert b"/Type /Page" in blob and b"/Count 1" in blob
    assert b"not tax advice" in blob.lower()


def test_pdf_survives_an_empty_book():
    empty = ConsolidatedPnL(sources=[StaticSource([])]).generate_report(
        "2026-04-01", "2026-09-30"
    )
    blob = render_pnl_pdf(empty)
    assert blob.startswith(b"%PDF")
    assert b"0.00" in blob


def test_pdf_caps_the_trade_table_but_says_so():
    records = [
        record(
            trade_id=f"t{i}",
            symbol=f"SYM{i:02d}",  # zero-padded: the table sorts by exit time then symbol
            gross_pnl=dec(100 + i),
            exit_time=datetime(2026, 5, 4, 15, 0),
        )
        for i in range(12)
    ]
    big = ConsolidatedPnL(sources=[StaticSource(records)]).generate_report(
        "2026-04-01", "2026-09-30"
    )
    blob = render_pnl_pdf(big, max_trades=5)
    assert blob.startswith(b"%PDF")
    assert b"SYM04" in blob  # the cap allows the first five
    assert b"SYM11" not in blob


def test_pdf_can_leave_out_the_trade_table(report):
    with_trades = render_pnl_pdf(report, include_trades=True)
    without = render_pnl_pdf(report, include_trades=False)
    assert len(without) < len(with_trades)


def test_pdf_text_reads_back_when_pypdf_is_available(report):
    pypdf = pytest.importorskip("pypdf")
    reader = pypdf.PdfReader(io.BytesIO(render_pnl_pdf(report)))
    text = "\n".join(page.extract_text() for page in reader.pages)
    assert "Consolidated P&L" in text
    assert "Net P&L" in text
    assert "TCS" in text
    assert "chartered accountant" in text.lower() or "CA" in text


# ---------------------------------------------------------------------------
# XLSX
# ---------------------------------------------------------------------------


def _zip(blob: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(blob))


def test_workbook_writes_valid_ooxml(report):
    blob = build_workbook(report).to_bytes()
    archive = _zip(blob)
    names = archive.namelist()
    assert "[Content_Types].xml" in names
    assert "_rels/.rels" in names
    assert "xl/workbook.xml" in names
    assert "xl/worksheets/sheet1.xml" in names
    # Every part must be parseable XML — a stray '<' is a corrupt download.
    for name in names:
        if name.endswith(".xml") or name.endswith(".rels"):
            ET.fromstring(archive.read(name))


def test_workbook_contains_the_itrs_annexures_and_a_disclaimer(report):
    workbook = build_workbook(report)
    titles = [sheet.title for sheet in workbook.sheets]
    assert titles[0].startswith("Capital gains")
    assert any("PGBP" in title for title in titles)
    assert any("STT" in title for title in titles)
    guidance = next(sheet for sheet in workbook.sheets if "guidance" in sheet.title.lower())
    flat = " ".join(str(cell) for row in guidance.rows for cell in row)
    assert "ITR-3" in flat and "ITR-2" in flat
    assert "chartered accountant" in flat.lower()
    assert "ca" in flat.lower()
    assert "44AD" in flat


def test_workbook_can_be_limited_to_selected_annexures(report):
    workbook = build_workbook(report, ["capital_gains"])
    assert [sheet.title for sheet in workbook.sheets] == [
        ITR_ANNEXURES["capital_gains"][0]
    ]


def test_workbook_title_sanitisation():
    book = Workbook()
    sheet = book.add_sheet("Bad/Name:With*Chars?[x]", [["a", 1]])
    assert len(sheet.title) <= 31
    assert not set(sheet.title) & set("/\\*?:[]")
    assert book.to_bytes().startswith(b"PK")


def test_xlsx_opens_in_an_excel_reader_when_available(report):
    openpyxl = pytest.importorskip("openpyxl")
    blob = write_xlsx(build_workbook(report))
    loaded = openpyxl.load_workbook(io.BytesIO(blob))
    assert loaded.sheetnames[0].startswith("Capital gains")
    sheet = loaded[loaded.sheetnames[0]]
    values = [cell.value for row in sheet.iter_rows() for cell in row]
    assert any(isinstance(value, str) and "Schedule CG" in value for value in values)


def test_row_numbers_and_dates_survive_a_round_trip(report):
    openpyxl = pytest.importorskip("openpyxl")
    sheet = Worksheet(title="flat")
    sheet.append(["net_pnl", dec("1234.56")])
    sheet.append(["exit_date", datetime(2026, 6, 4, 15, 0).date()])
    sheet.append(["flag", True])
    loaded = openpyxl.load_workbook(io.BytesIO(write_xlsx(Workbook(sheets=[sheet]))))
    rows = list(loaded["flat"].iter_rows(values_only=True))
    assert float(rows[0][1]) == pytest.approx(1234.56)
    assert str(rows[1][1]).startswith("2026-06-04")


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


def test_csv_quoting_and_values():
    sheet = Worksheet(title="flat")
    sheet.append(["symbol", "note", "pnl"])
    sheet.append(["INFY", "has, a comma and \"quotes\"", dec("10.50")])
    text = write_csv(sheet)
    lines = text.strip().splitlines()
    assert lines[0] == "symbol,note,pnl"
    assert '"has, a comma and ""quotes"""' in lines[1]
    # A Decimal keeps its scale, so money is not silently re-formatted.
    assert lines[1].endswith(",10.50")


def test_csv_carries_notes_as_comments_so_caveats_travel_with_the_data(report):
    workbook = build_workbook(report)
    sheet = workbook.sheets[0]
    sheet.add_note("estimated fees on 1 trade, and other, comma-heavy prose")
    text = write_csv(sheet)
    # Notes travel with the data as literal comment lines — including when the
    # note itself contains commas (quoting them would hide the "#").
    assert "# estimated fees on 1 trade, and other, comma-heavy prose" in text
    assert "Schedule CG" in text


# ---------------------------------------------------------------------------
# Annexure content
# ---------------------------------------------------------------------------


def test_capital_gains_schedule_excludes_stt_from_transfer_expenses(report):
    rows = capital_gains_schedule(report)
    flat = " ".join(str(cell) for row in rows for cell in row)
    assert "Schedule CG" in flat
    assert "STT" in flat  # named explicitly…
    assert "not deductible" in flat.lower() or "not allowed" in flat.lower()


def test_fno_pnl_statement_uses_absolute_pnl_turnover(report):
    rows = fno_pnl_statement(report)
    flat = " ".join(str(cell) for row in rows for cell in row)
    assert "ITR-3" in flat or "PGBP" in flat
    assert "ICAI basis" in flat
    assert "-1600.0" in flat  # the F&O loss plus its own cost stack


def test_stt_summary_totals_what_was_actually_paid(report):
    rows = stt_summary(report)
    flat = " ".join(str(cell) for row in rows for cell in row)
    assert "460.0" in flat  # 400 delivery + 60 F&O
    assert "s.36(1)(xv)" in flat
    assert "proviso to s.48" in flat


def test_turnover_sheet_reports_the_audit_read_out(report):
    rows = turnover_sheet(report)
    flat = " ".join(str(cell) for row in rows for cell in row)
    assert "s.44AB" in flat
    assert "40000.0" in flat  # delivery turnover, reported separately
    assert "subsequent" not in flat


def test_guidance_names_the_right_form_and_the_reason(report):
    rows = itr_guidance(report)
    flat = " ".join(str(cell) for row in rows for cell in row)
    assert "ITR-3" in flat
    assert "capital gains" in flat.lower()
    assert "44AD" in flat


def test_every_export_carries_the_disclaimer(report):
    pdf = render_pnl_pdf(report)
    assert b"not tax advice" in pdf.lower()
    assert b"chartered accountant" in pdf.lower()

    workbook = build_workbook(report)
    for sheet in workbook.sheets:
        flat = " ".join(str(cell) for row in sheet.rows for cell in row).lower()
        assert "estimate" in flat and "chartered accountant (ca)" in flat

    csv_text = write_csv(workbook.sheets[0]).lower()
    assert "chartered accountant (ca)" in csv_text
