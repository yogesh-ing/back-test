"""A small PDF writer and the consolidated P&L layout.

Why hand-roll a PDF instead of adding ``reportlab``: the report is the one
artefact that has to be produced on a machine you do not control (a cron box,
a client's laptop) and then kept for years. A 300-line stdlib writer that
cannot fail to install beats a dependency that can — and because the content
streams are uncompressed, the output is ``grep``-able and ``pdftotext``-able,
which is what an auditor will actually do with it.

Scope is deliberately narrow: Helvetica, WinAnsi text, lines and filled
rectangles. That is everything a P&L statement needs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from backtest.reporting.consolidator import PnLReport

__all__ = ["render_pnl_pdf", "PdfDocument"]

logger = logging.getLogger("backtest.reporting.exports.pdf")

A4_WIDTH = 595.28
A4_HEIGHT = 841.89
MARGIN = 42.0

#: Glyphs outside WinAnsi that appear in our own prose, mapped to plain text.
#: (The rupee sign is not in WinAnsi; printing it raw produces mojibake, so it
#: becomes "Rs." — clearly readable in every viewer.)
_UNICODE_FALLBACKS = {
    "₹": "Rs. ",
    "–": "-",
    "—": "-",
    "’": "'",
    "‘": "'",
    "“": '"',
    "”": '"',
    "≤": "<=",
    "≥": ">=",
    "→": "->",
    "×": "x",
    "•": "-",
    "…": "...",
    "∞": "inf",
    "₹": "Rs. ",
}


def _winansi(text: Any) -> str:
    """Coerce any string to something WinAnsi can print."""
    out = str(text if text is not None else "")
    for source, target in _UNICODE_FALLBACKS.items():
        out = out.replace(source, target)
    return out.encode("cp1252", errors="replace").decode("cp1252")


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _num(value: Any) -> str:
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)


@dataclass
class _Page:
    ops: list[str] = field(default_factory=list)


class PdfDocument:
    """Minimal single-font-family PDF builder (Helvetica / Helvetica-Bold)."""

    def __init__(self, title: str = "Consolidated P&L", author: str = "Back-Test Platform"):
        self.title = _winansi(title)
        self.author = _winansi(author)
        self.pages: list[_Page] = []
        self._page: _Page | None = None
        self.cursor = A4_HEIGHT - MARGIN

    # -- page & cursor -----------------------------------------------------

    def new_page(self) -> None:
        self._page = _Page()
        self.pages.append(self._page)
        self.cursor = A4_HEIGHT - MARGIN

    def ensure(self, height: float) -> None:
        """Start a new page when ``height`` points would overflow this one."""
        if self._page is None:
            self.new_page()
        if self.cursor - height < MARGIN:
            self.new_page()

    def space(self, points: float = 8.0) -> None:
        self.cursor -= points

    # -- primitives --------------------------------------------------------

    def text(
        self,
        x: float,
        text: str,
        *,
        size: float = 10.0,
        bold: bool = False,
        gray: float = 0.0,
    ) -> None:
        self.ensure(size + 2)
        font = "F2" if bold else "F1"
        self._write(f"BT /{font} {size} Tf {gray:g} g 1 0 0 1 {x:.2f} {self.cursor:.2f} Tm")
        self._write(f"({_escape(_winansi(text))}) Tj ET")
        self._write("0 g")

    def text_right(
        self, x_right: float, text: str, *, size: float = 10.0, bold: bool = False
    ) -> None:
        width = self._width(text, size, bold)
        self.text(x_right - width, text, size=size, bold=bold)

    def line(self, x1: float, y: float, x2: float, gray: float = 0.7) -> None:
        self._write(f"{gray:g} G 0.6 w {x1:.2f} {y:.2f} m {x2:.2f} {y:.2f} l S 0 G")

    def rule(self, x1: float, x2: float, gray: float = 0.7) -> None:
        """A horizontal rule at the current cursor."""
        self.line(x1, self.cursor, x2, gray=gray)

    def shade(self, x: float, y: float, width: float, height: float, gray: float = 0.93) -> None:
        self._write(f"{gray:g} g {x:.2f} {y:.2f} {width:.2f} {height:.2f} re f 0 g")

    # -- rows --------------------------------------------------------------

    def row(
        self,
        cells: Sequence[tuple[float, str, str]],
        *,
        size: float = 9.5,
        bold: bool = False,
    ) -> None:
        """Write one row: ``(x_position, text, "left"|"right")`` tuples."""
        self.ensure(size + 6)
        for x_pos, text, align in cells:
            if align == "right":
                self.text_right(x_pos, text, size=size, bold=bold)
            else:
                self.text(x_pos, text, size=size, bold=bold)
        self.space(size + 4)

    @staticmethod
    def _width(text: str, size: float, bold: bool) -> float:
        """Helvetica advance widths, approximated well enough for alignment."""
        factor = 0.5 if bold else 0.485
        return len(_winansi(text)) * size * factor

    # -- output ------------------------------------------------------------

    def _write(self, op: str) -> None:
        if self._page is None:
            self.new_page()
        assert self._page is not None
        self._page.ops.append(op)

    def to_bytes(self) -> bytes:
        if not self.pages:
            self.new_page()
        objects: list[bytes] = []

        font_regular = (
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
            b"/Encoding /WinAnsiEncoding >>"
        )
        font_bold = (
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
            b"/Encoding /WinAnsiEncoding >>"
        )

        # Object numbers: 1 catalog, 2 pages, 3 info, 4/5 fonts, then per page
        # a content stream + a page object.
        content_ids: list[int] = []
        page_ids: list[int] = []
        next_id = 6
        for _ in self.pages:
            content_ids.append(next_id)
            page_ids.append(next_id + 1)
            next_id += 2

        objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
        kids = " ".join(f"{pid} 0 R" for pid in page_ids)
        objects.append(
            f"<< /Type /Pages /Count {len(page_ids)} /Kids [{kids}] >>".encode("latin-1")
        )
        created = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%SZ")
        objects.append(
            (
                "<< /Title ({title}) /Author ({author}) /Producer (backtest.reporting) "
                "/Creator (backtest.reporting) /CreationDate (D:{created}) >>"
            )
            .format(title=_escape(self.title), author=_escape(self.author), created=created)
            .encode("latin-1")
        )
        objects.append(font_regular)
        objects.append(font_bold)

        for content_id, page_id, page in zip(content_ids, page_ids, self.pages):
            stream = "\n".join(page.ops).encode("latin-1", errors="replace")
            objects.append(
                b"<< /Length "
                + str(len(stream)).encode()
                + b" >>\nstream\n"
                + stream
                + b"\nendstream"
            )
            objects.append(
                (
                    f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {A4_WIDTH:.2f} {A4_HEIGHT:.2f}] "
                    f"/Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> "
                    f"/Contents {content_id} 0 R >>"
                ).encode("latin-1")
            )

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets: list[int] = []
        for index, body in enumerate(objects, start=1):
            offsets.append(len(out))
            out += f"{index} 0 obj\n".encode("latin-1") + body + b"\nendobj\n"
        xref_offset = len(out)
        out += f"xref\n0 {len(objects) + 1}\n".encode("latin-1")
        out += b"0000000000 65535 f \n"
        for offset in offsets:
            out += f"{offset:010d} 00000 n \n".encode("latin-1")
        out += (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info 3 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("latin-1")
        return bytes(out)


# ---------------------------------------------------------------------------
# Report layout
# ---------------------------------------------------------------------------

_LEFT = MARGIN
_RIGHT = A4_WIDTH - MARGIN


def render_pnl_pdf(
    report: PnLReport, *, include_trades: bool = True, max_trades: int = 400
) -> bytes:
    """Render a :class:`~backtest.reporting.consolidator.PnLReport` to PDF bytes."""
    doc = PdfDocument(title=f"Consolidated P&L {report.period.label}")
    doc.new_page()
    _header(doc, report)
    _summary(doc, report)
    _by_broker(doc, report)
    _by_category(doc, report)
    _tax(doc, report)
    _caveats(doc, report)
    if include_trades and report.trades:
        _trade_ledger(doc, report, max_trades=max_trades)
    _footer(doc, report)
    return doc.to_bytes()


def _header(doc: PdfDocument, report: PnLReport) -> None:
    doc.text(_LEFT, "Consolidated P&L Statement", size=17, bold=True)
    doc.space(20)
    doc.text(_LEFT, f"Period: {report.period.label}", size=10.5)
    doc.space(13)
    doc.text(
        _LEFT,
        "Generated "
        + report.generated_at.astimezone(timezone.utc).strftime("%d-%b-%Y %H:%M UTC")
        + " · "
        + ("paper + live" if report.include_paper else "live only")
        + (" · demo book" if report.demo else ""),
        size=9,
    )
    doc.space(10)
    if report.demo:
        doc.text(
            _LEFT,
            "DEMO BOOK — sample trades, not a record of any real account.",
            size=10,
            bold=True,
        )
        doc.space(12)
    doc.rule(_LEFT, _RIGHT)
    doc.space(10)


def _summary(doc: PdfDocument, report: PnLReport) -> None:
    doc.text(_LEFT, "1. Summary (all brokers)", size=12, bold=True)
    doc.space(16)
    rows: list[tuple[str, float]] = [("Gross P&L", float(report.gross_pnl))]
    for row in report.fee_rows():
        rows.append((row["label"], -float(row["amount"])))
    if report.slippage:
        rows.append(("Slippage (recorded)", -float(report.slippage)))
    for label, amount in rows:
        doc.row(
            [
                (_LEFT, label, "left"),
                (_RIGHT, _num(amount), "right"),
            ]
        )
    doc.rule(_LEFT, _RIGHT)
    doc.space(6)
    doc.row([(_LEFT, "Net P&L", "left"), (_RIGHT, _num(report.net_pnl), "right")], bold=True)
    doc.row(
        [(_LEFT, "Estimated tax", "left"), (_RIGHT, _num(-report.tax.total), "right")]
    )
    doc.rule(_LEFT, _RIGHT)
    doc.space(6)
    doc.row(
        [
            (_LEFT, "Net after tax", "left"),
            (_RIGHT, _num(report.net_after_tax), "right"),
        ],
        size=11,
        bold=True,
    )
    doc.space(10)
    doc.text(
        _LEFT,
        f"Trades: {report.trade_count}  ·  win rate {report.win_rate * 100:.1f}%  ·  "
        f"winners {report.winners} / losers {report.losers}"
        + (f" / flat {report.breakeven}" if report.breakeven else ""),
        size=9,
    )
    doc.space(12)
    doc.text(
        _LEFT,
        f"Cost + tax drag: {report.cost_drag_pct:.1f}% of gross profit "
        "(this is the number to judge a strategy on).",
        size=9,
    )
    doc.space(16)


def _by_broker(doc: PdfDocument, report: PnLReport) -> None:
    doc.text(_LEFT, "2. By broker", size=12, bold=True)
    doc.space(16)
    columns = (
        (_LEFT, "Broker"),
        (_LEFT + 200, "Trades"),
        (_LEFT + 270, "Net P&L"),
        (_LEFT + 380, "Share"),
    )
    _table_header(doc, columns, right_columns={2, 3})
    for row in report.by_broker:
        doc.row(
            [
                (_LEFT, row.broker, "left"),
                (_LEFT + 200, str(row.trades), "right"),
                (_LEFT + 380, _num(row.net_pnl), "right"),
                (_RIGHT, f"{row.share_pct:.1f}%", "right"),
            ],
            size=9,
        )
    doc.space(10)
    doc.text(
        _LEFT,
        "Live book net "
        f"{_num(report.live_net_pnl)}  ·  paper book net {_num(report.paper_net_pnl)}"
        + ("" if report.include_paper else "  (paper excluded)"),
        size=8.5,
        gray=0.35,
    )
    doc.space(14)


def _by_category(doc: PdfDocument, report: PnLReport) -> None:
    doc.text(_LEFT, "3. Tax categorisation", size=12, bold=True)
    doc.space(16)
    doc.row(
        [
            (_LEFT, "Category", "left"),
            (_LEFT + 230, "Net P&L", "right"),
            (_LEFT + 330, "Rate", "right"),
            (_RIGHT, "Est. tax", "right"),
        ],
        size=8.5,
        bold=True,
    )
    doc.rule(_LEFT, _RIGHT)
    for row in report.by_category:
        doc.row(
            [
                (_LEFT, row.category.label, "left"),
                (_LEFT + 330, _num(row.net_pnl), "right"),
                (_LEFT + 410, f"{row.rate * 100:.1f}%" if row.rate else "—", "right"),
                (_RIGHT, _num(row.estimated_tax), "right"),
            ],
            size=9,
        )
    doc.rule(_LEFT, _RIGHT)
    doc.space(6)
    doc.row(
        [
            (_LEFT, "Total estimated tax (incl. cess)", "left"),
            (_RIGHT, _num(report.tax.total), "right"),
        ],
        bold=True,
    )
    doc.space(10)
    for note in report.tax.notes[:2]:
        doc.text(_LEFT, f"· {note}", size=8, gray=0.35)
        doc.space(9)
    doc.space(8)


def _tax(doc: PdfDocument, report: PnLReport) -> None:
    if not report.tax.carry_forward:
        return
    doc.text(_LEFT, "4. Losses carried forward", size=12, bold=True)
    doc.space(16)
    for entry in report.tax.carry_forward:
        doc.text(
            _LEFT,
            f"{entry['label']}: {_num(entry['amount'])} — {entry['rule']}",
            size=9,
        )
        doc.space(12)
    doc.space(6)


def _caveats(doc: PdfDocument, report: PnLReport) -> None:
    notes = list(report.warnings) + list(report.data_notes)
    if not notes:
        return
    doc.text(_LEFT, "Data caveats", size=11, bold=True)
    doc.space(15)
    for note in notes[:14]:
        doc.text(_LEFT, f"· {note}", size=8, gray=0.3)
        doc.space(9)
    doc.space(8)


def _trade_ledger(doc: PdfDocument, report: PnLReport, *, max_trades: int) -> None:
    trades = report.trades[:max_trades]
    doc.ensure(60)
    doc.text(_LEFT, "Trade ledger", size=12, bold=True)
    doc.space(16)
    doc.row(
        [
            (_LEFT, "Exit", "left"),
            (_LEFT + 62, "Broker", "left"),
            (_LEFT + 150, "Symbol", "left"),
            (_LEFT + 260, "Qty", "right"),
            (_LEFT + 320, "Fees", "right"),
            (_LEFT + 380, "Net P&L", "right"),
            (_RIGHT, "Tax bucket", "right"),
        ],
        size=8,
        bold=True,
    )
    doc.rule(_LEFT, _RIGHT)
    for trade in trades:
        doc.row(
            [
                (_LEFT, trade.exit_date.isoformat() if trade.exit_date else "—", "left"),
                (_LEFT + 62, trade.broker[:14], "left"),
                (_LEFT + 150, trade.symbol[:20], "left"),
                (_LEFT + 260, f"{float(trade.quantity):,.0f}", "right"),
                (_LEFT + 380, _num(trade.fees_total), "right"),
                (_RIGHT, _num(trade.net_pnl), "right"),
            ],
            size=8,
        )
        doc.cursor += 12
        doc.text(
            _LEFT + 62,
            f"{trade.tax_category.label} · {trade.mode}"
            + (f" · {trade.tag}" if trade.tag else ""),
            size=7,
            gray=0.4,
        )
        doc.space(4)
    if len(report.trades) > len(trades):
        doc.space(8)
        doc.text(
            _LEFT,
            f"… {len(report.trades) - len(trades)} more trades in the Excel/CSV export.",
            size=8,
            gray=0.35,
        )


def _footer(doc: PdfDocument, report: PnLReport) -> None:
    doc.space(14)
    doc.rule(_LEFT, _RIGHT)
    doc.space(8)
    doc.text(
        _LEFT,
        "Tax figures are ESTIMATES built from platform-recorded trades and the rates in "
        "config/reporting.yaml. Verify every number against broker contract notes and a "
        "chartered accountant before filing. This document is not tax advice.",
        size=8,
        gray=0.3,
    )
    doc.space(10)
    doc.text(_LEFT, f"Sources: {_source_summary(report)}", size=7.5, gray=0.4)


def _source_summary(report: PnLReport) -> str:
    bits: list[str] = []
    for source in report.sources:
        note = source.get("note") or ""
        bits.append(f"{source.get('name')}{f' ({note})' if note else ''}")
    return " · ".join(bits) or "none"


def _table_header(
    doc: PdfDocument, columns: Iterable[tuple[float, str]], right_columns: set[int]
) -> None:
    doc.ensure(20)
    for index, (x, label) in enumerate(columns):
        if index in right_columns:
            doc.text_right(x, label, size=8.5, bold=True)
        else:
            doc.text(x, label, size=8.5, bold=True)
    doc.space(13)
    doc.rule(_LEFT, _RIGHT)
