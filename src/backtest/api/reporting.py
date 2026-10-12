"""Reporting API — consolidated P&L, exports, reconciliation (PRD-002).

* ``GET  /api/reporting/pnl/consolidated``  the report itself (JSON)
* ``GET  /api/reporting/config``            tax rules + thresholds (no secrets)
* ``POST /api/reporting/pnl/export/pdf``    the PDF statement
* ``POST /api/reporting/pnl/export/itr``    ITR annexures (xlsx / csv / json)
* ``POST /api/reporting/pnl/export/trades`` the trade ledger (csv)
* ``POST /api/reporting/pnl/reconcile``     platform vs contract note
* ``POST /api/reporting/email``             the monthly email (dry-run default)

Everything is fail-soft at the data layer and strict at the input layer: a
missing database yields an empty report with a warning (never a 500), while a
malformed date or an unparseable number is a 400 — silently guessing an
input to a tax report is how wrong numbers get filed.
"""

from __future__ import annotations

import io
from typing import Any, Mapping

from flask import Blueprint, jsonify, request, send_file

from backtest.logging_config import get_logger
from backtest.reporting.consolidator import ConsolidatedPnL, PnLReport
from backtest.reporting.email_report import (
    MonthlyReportEmailer,
    load_email_config,
)
from backtest.reporting.exports import build_workbook, render_pnl_pdf, write_csv
from backtest.reporting.exports.xlsx import Worksheet
from backtest.reporting.records import Period
from backtest.reporting.reconciliation import (
    BrokerReconciliation,
    load_reconciliation_thresholds,
)
from backtest.reporting.tax import load_tax_rules

__all__ = ["reporting_bp"]

logger = get_logger(__name__)

reporting_bp = Blueprint("reporting_api", __name__)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def _manager() -> Any:
    from backtest.forward.portfolio_manager import get_portfolio_manager

    return get_portfolio_manager()


def _consolidator() -> ConsolidatedPnL:
    """A consolidator wired to the live manager and the configured database.

    The DB manager is built lazily *inside* the source, so a deployment with no
    database still serves memory-book reports instead of failing here.
    """
    return ConsolidatedPnL(manager=_manager())


def _report_from(data: Mapping[str, Any]) -> PnLReport:
    """Build a report from request data (query args or JSON body)."""
    period = _period_from(data)
    include_paper = _bool(data.get("include_paper"), default=True)
    demo = _bool(data.get("demo"), default=False)
    brokers = data.get("brokers")
    if isinstance(brokers, str):
        brokers = [b for b in (part.strip() for part in brokers.split(",")) if b]
    return _consolidator().generate_report(
        period.start,
        period.end,
        include_paper=include_paper,
        brokers=brokers or None,
        demo=demo,
    )


def _period_from(data: Mapping[str, Any]) -> Period:
    try:
        return Period.parse(data.get("from_date"), data.get("to_date"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid period: {exc}") from exc


def _bool(value: Any, *, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _error(message: str, status: int = 400) -> tuple:
    return jsonify({"success": False, "error": message}), status


def _body() -> dict[str, Any]:
    """JSON body merged with query args (body wins)."""
    payload = dict(request.args)
    payload.pop("success", None)
    body = request.get_json(silent=True) or {}
    if isinstance(body, Mapping):
        payload.update(body)
    return payload


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@reporting_bp.get("/api/reporting/pnl/consolidated")
def consolidated_pnl() -> tuple:
    """The consolidated P&L for a period (default: financial-year-to-date)."""
    data = _body()
    try:
        report = _report_from(data)
    except ValueError as exc:
        return _error(str(exc))
    except Exception as exc:  # noqa: BLE001 — a report failure must explain itself
        logger.exception("consolidated P&L failed")
        return _error(f"report generation failed: {exc}", 500)
    include_trades = _bool(data.get("include_trades"), default=True)
    payload = report.to_dict(include_trades=include_trades)
    payload["success"] = True
    return jsonify(payload), 200


@reporting_bp.get("/api/reporting/config")
def reporting_config() -> tuple:
    """Tax rules, reconciliation thresholds and whether the email can send."""
    rules = load_tax_rules()
    thresholds = load_reconciliation_thresholds()
    email = load_email_config()
    return (
        jsonify(
            {
                "success": True,
                "tax": rules.as_dict(),
                "reconciliation": {k: float(v) for k, v in thresholds.items()},
                "email": email.describe(),
                "disclaimer": rules.disclaimer,
            }
        ),
        200,
    )


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------


@reporting_bp.post("/api/reporting/pnl/export/pdf")
def export_pdf() -> Any:
    """The consolidated statement as a PDF download."""
    data = _body()
    try:
        report = _report_from(data)
    except ValueError as exc:
        return _error(str(exc))
    include_trades = _bool(data.get("include_trades"), default=True)
    pdf = render_pnl_pdf(report, include_trades=include_trades)
    name = f"consolidated_pnl_{report.period.start}_{report.period.end}.pdf"
    return send_file(
        io.BytesIO(pdf), mimetype="application/pdf", as_attachment=True, download_name=name
    )


@reporting_bp.post("/api/reporting/pnl/export/itr")
def export_itr() -> Any:
    """ITR annexures.

    Body: ``{"from_date": …, "to_date": …, "format": "xlsx"|"csv"|"json",
    "annexures": ["capital_gains", "pgbp", "stt", "turnover", "guidance"]}``.
    ``csv`` returns one CSV per annexure inside a ZIP (a single CSV cannot hold
    five schedules without losing their names).
    """
    data = _body()
    try:
        report = _report_from(data)
    except ValueError as exc:
        return _error(str(exc))
    annexures = data.get("annexures")
    if isinstance(annexures, str):
        annexures = [part.strip() for part in annexures.split(",") if part.strip()]
    fmt = str(data.get("format") or "xlsx").strip().lower()
    workbook = build_workbook(report, annexures if isinstance(annexures, list) else None)

    if fmt == "json":
        return (
            jsonify(
                {
                    "success": True,
                    "sheets": [
                        {"title": sheet.title, "rows": sheet.rows, "notes": sheet.notes}
                        for sheet in workbook.sheets
                    ],
                    "disclaimer": load_tax_rules().disclaimer,
                }
            ),
            200,
        )
    if fmt == "csv":
        return _zip_of_csv(workbook, report)
    if fmt != "xlsx":
        return _error(f"unsupported format {fmt!r} (xlsx | csv | json)")

    blob = workbook.to_bytes()
    name = f"itr_annexures_{report.period.start}_{report.period.end}.xlsx"
    return send_file(
        io.BytesIO(blob),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=name,
    )


@reporting_bp.post("/api/reporting/pnl/export/trades")
def export_trades() -> Any:
    """The trade ledger as CSV (uncapped — the PDF caps its table)."""
    data = _body()
    try:
        report = _report_from(data)
    except ValueError as exc:
        return _error(str(exc))
    sheet = Worksheet(title="trades")
    sheet.append(
        [
            "trade_id",
            "broker",
            "mode",
            "segment",
            "symbol",
            "strategy",
            "entry_date",
            "exit_date",
            "quantity",
            "gross_pnl",
            "fees",
            "slippage",
            "net_pnl",
            "tax_category",
            "fees_basis",
            "source",
            "tag",
        ]
    )
    for trade in report.trades:
        sheet.append(
            [
                trade.trade_id,
                trade.broker,
                trade.mode,
                trade.segment,
                trade.symbol,
                trade.strategy,
                trade.entry_date.isoformat() if trade.entry_date else "",
                trade.exit_date.isoformat() if trade.exit_date else "",
                float(trade.quantity),
                float(trade.gross_pnl),
                float(trade.fees_total),
                float(trade.slippage_total),
                float(trade.net_pnl),
                trade.tax_category.value,
                trade.fees_basis,
                trade.source,
                trade.tag,
            ]
        )
    for note in list(report.warnings) + list(report.data_notes):
        sheet.add_note(f"# {note}")
    text = write_csv(sheet)
    name = f"trades_{report.period.start}_{report.period.end}.csv"
    return send_file(
        io.BytesIO(text.encode("utf-8")),
        mimetype="text/csv",
        as_attachment=True,
        download_name=name,
    )


def _zip_of_csv(workbook: Any, report: PnLReport) -> Any:
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, sheet in enumerate(workbook.sheets, start=1):
            archive.writestr(
                f"{index:02d}_{_slug(sheet.title)}.csv",
                write_csv(sheet).encode("utf-8"),
            )
    buffer.seek(0)
    name = f"itr_annexures_{report.period.start}_{report.period.end}.zip"
    return send_file(buffer, mimetype="application/zip", as_attachment=True, download_name=name)


def _slug(text: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in str(text).lower()).strip("_")[:40]


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


@reporting_bp.post("/api/reporting/pnl/reconcile")
def reconcile() -> tuple:
    """Compare the platform's numbers for a broker against its contract note.

    Body (single broker)::

        {"from_date": "2026-09-01", "to_date": "2026-09-30", "broker": "mstock",
         "contract_note_pnl": 185200, "contract_note_fees": {"stt": 12340, ...},
         "fees_total": 28450, "note_ref": "CN-2026-09-30"}

    or several at once with ``{"notes": {"mstock": {...}, "dhan": 78340}}``.
    """
    data = _body()
    try:
        report = _report_from(data)
    except ValueError as exc:
        return _error(str(exc))
    reconciler = BrokerReconciliation(load_reconciliation_thresholds())

    notes = data.get("notes")
    if isinstance(notes, Mapping) and notes:
        try:
            results = reconciler.reconcile_report(report, notes)
        except ValueError as exc:  # a note without a P&L is an input error
            return _error(str(exc))
        return jsonify({"success": True, "results": [r.to_dict() for r in results]}), 200

    broker = str(data.get("broker") or "").strip()
    if not broker:
        return _error("broker is required (or pass a `notes` map)")
    if data.get("contract_note_pnl") in (None, ""):
        return _error("contract_note_pnl is required")
    try:
        result = reconciler.reconcile(
            report,
            broker,
            contract_note_pnl=data.get("contract_note_pnl"),
            contract_note_fees=data.get("contract_note_fees"),
            broker_fees_total=data.get("fees_total"),
            note_ref=str(data.get("note_ref") or ""),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("reconciliation failed")
        return _error(f"reconciliation failed: {exc}", 500)
    payload = result.to_dict()
    payload["success"] = True
    payload["platform_summary"] = report.summary()
    return jsonify(payload), 200


# ---------------------------------------------------------------------------
# Monthly email
# ---------------------------------------------------------------------------


@reporting_bp.post("/api/reporting/email")
def send_report_email() -> tuple:
    """Send (or dry-run) the monthly email.

    Dry-run is the default: without ``"dry_run": false`` the message is written
    to the outbox directory and the response says exactly where. Real sending
    also requires ``smtp.host`` + ``monthly_email.enabled`` in the config.
    """
    data = _body()
    try:
        report = _report_from(data)
    except ValueError as exc:
        return _error(str(exc))
    dry_run = _bool(data.get("dry_run"), default=True)
    emailer = MonthlyReportEmailer(load_email_config())
    try:
        result = emailer.send(
            report,
            to_email=(str(data.get("to_email")) if data.get("to_email") else None),
            dry_run=dry_run,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("monthly email failed")
        return _error(f"email failed: {exc}", 500)
    result["success"] = True
    result["summary"] = report.summary()
    return jsonify(result), 200
