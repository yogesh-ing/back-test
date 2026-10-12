"""Export helpers — PDF, Excel and CSV annexures with **no third-party deps**.

``reportlab`` and ``openpyxl`` are not in this project's requirements, and a
tax report is exactly the wrong place to add a dependency that a deployment
might be missing. So the writers here are small, deterministic and stdlib-only:

* :mod:`backtest.reporting.exports.pdf` — a minimal PDF writer + the report
  layout (Helvetica, uncompressed streams: greppable, and ``pdftotext``-able).
* :mod:`backtest.reporting.exports.xlsx` — a minimal OOXML workbook writer,
  so the ITR annexures open in Excel / LibreOffice / Google Sheets.
* :mod:`backtest.reporting.exports.itr` — the annexure *content* (Schedule CG,
  the PGBP statement, the STT summary, and the "which ITR form" guidance),
  independent of how it is written out.
"""

from backtest.reporting.exports.itr import (
    ITR_ANNEXURES,
    build_workbook,
    capital_gains_schedule,
    fno_pnl_statement,
    itr_guidance,
    stt_summary,
)
from backtest.reporting.exports.pdf import render_pnl_pdf
from backtest.reporting.exports.xlsx import Workbook, write_csv, write_xlsx

__all__ = [
    "render_pnl_pdf",
    "Workbook",
    "write_xlsx",
    "write_csv",
    "capital_gains_schedule",
    "fno_pnl_statement",
    "stt_summary",
    "itr_guidance",
    "ITR_ANNEXURES",
    "build_workbook",
]
