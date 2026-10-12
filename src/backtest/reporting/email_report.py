"""The monthly report email — and the reason it defaults to dry-run.

A cron job that sends mail is a cron job that can send mail 200 times when
something is misconfigured. So this module has one rule: **it never sends
unless a human has configured a host and explicitly asked for delivery.** With
no SMTP host configured (the default), it writes a complete ``.eml`` file to
``var/reporting/outbox`` instead — same message, same attachment, inspectable,
and safe to run on the 1st of every month while you decide.

Configuration comes from ``config/reporting.yaml`` (``monthly_email`` + ``smtp``
blocks) with the password read from the environment
(``REPORTING_SMTP_PASSWORD``). Nothing secret is ever written to the config
file or the logs.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Mapping

from backtest.reporting.consolidator import ConsolidatedPnL, PnLReport
from backtest.reporting.exports.pdf import render_pnl_pdf
from backtest.reporting.records import Period

__all__ = [
    "MonthlyReportEmailer",
    "EmailConfig",
    "load_email_config",
    "send_monthly_report",
]

logger = logging.getLogger("backtest.reporting.email")

_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "reporting.yaml"
_CONFIG_ENV = "REPORTING_CONFIG_PATH"
_TO_ENV = "REPORTING_EMAIL_TO"
_PASSWORD_ENV = "REPORTING_SMTP_PASSWORD"


@dataclass
class EmailConfig:
    """Everything the mailer needs, with safe defaults (no host = dry run)."""

    enabled: bool = False
    send_on_day: int = 1
    send_at_hour: int = 9
    report_previous_month: bool = True
    to_email: str = ""
    subject_prefix: str = "Monthly P&L"
    host: str = ""
    port: int = 587
    user: str = ""
    password: str = ""
    from_email: str = ""
    use_tls: bool = True
    outbox_dir: Path = field(
        default_factory=lambda: Path("var") / "reporting" / "outbox"
    )

    @property
    def can_send(self) -> bool:
        """True only when delivery was configured for real."""
        return bool(self.host and self.to_email and self.enabled)

    def describe(self) -> dict[str, Any]:
        """Config as the UI/API may see it — never the password."""
        return {
            "enabled": self.enabled,
            "send_on_day": self.send_on_day,
            "send_at_hour": self.send_at_hour,
            "report_previous_month": self.report_previous_month,
            "to_email": self.to_email,
            "subject_prefix": self.subject_prefix,
            "smtp_host": self.host,
            "smtp_port": self.port,
            "smtp_user": self.user,
            "from_email": self.from_email,
            "use_tls": self.use_tls,
            "can_send": self.can_send,
            "outbox_dir": str(self.outbox_dir),
        }


def load_email_config(path: str | Path | None = None) -> EmailConfig:
    """Read the email config; fail-soft to the dry-run defaults."""
    candidate = Path(path) if path else Path(os.getenv(_CONFIG_ENV) or _CONFIG_PATH)
    document: Mapping[str, Any] = {}
    if candidate.exists():
        try:
            import yaml

            document = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # noqa: BLE001 — a bad file means "do not send"
            logger.warning("could not read %s (%s) — email stays in dry-run", candidate, exc)
            document = {}
    monthly = document.get("monthly_email") or {}
    smtp = document.get("smtp") or {}
    password_env = str(smtp.get("password_env") or _PASSWORD_ENV)
    user = str(smtp.get("user") or "")
    outbox = Path(str(smtp.get("outbox_dir") or "var/reporting/outbox"))
    enabled = bool(monthly.get("enabled", False))
    if os.getenv("REPORTING_EMAIL_ENABLED", "").strip().lower() in ("1", "true", "yes", "on"):
        enabled = True
    return EmailConfig(
        enabled=enabled,
        send_on_day=int(monthly.get("send_on_day", 1) or 1),
        send_at_hour=int(monthly.get("send_at_hour", 9) or 9),
        report_previous_month=bool(monthly.get("report_previous_month", True)),
        to_email=str(monthly.get("to_email") or os.getenv(_TO_ENV) or ""),
        subject_prefix=str(monthly.get("subject_prefix") or "Monthly P&L"),
        host=str(smtp.get("host") or ""),
        port=int(smtp.get("port", 587) or 587),
        user=user,
        password=os.getenv(password_env, ""),
        from_email=str(smtp.get("from_email") or user),
        use_tls=bool(smtp.get("use_tls", True)),
        outbox_dir=outbox,
    )


class MonthlyReportEmailer:
    """Builds and (maybe) sends the monthly consolidated P&L email."""

    def __init__(
        self,
        config: EmailConfig | None = None,
        *,
        consolidator: ConsolidatedPnL | None = None,
    ):
        self.config = config or load_email_config()
        self._consolidator = consolidator

    # -- message -----------------------------------------------------------

    def build_message(
        self,
        report: PnLReport,
        pdf_bytes: bytes,
        *,
        to_email: str | None = None,
        now: datetime | None = None,
    ) -> EmailMessage:
        recipient = (to_email or self.config.to_email or "").strip()
        if not recipient:
            raise ValueError(
                "no recipient configured (set monthly_email.to_email or REPORTING_EMAIL_TO)"
            )
        subject = f"{self.config.subject_prefix} — {report.period.label}"
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.config.from_email or self.config.user or "reports@localhost"
        message["To"] = recipient
        message.set_content(_plain_body(report))
        message.add_alternative(_html_body(report), subtype="html")
        filename = f"consolidated_pnl_{report.period.start}_{report.period.end}.pdf"
        message.add_attachment(
            pdf_bytes, maintype="application", subtype="pdf", filename=filename
        )
        return message

    # -- delivery ----------------------------------------------------------

    def send(
        self,
        report: PnLReport,
        *,
        to_email: str | None = None,
        dry_run: bool | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Send the report, or write it to the outbox when not configured.

        Returns a result dict with ``sent``/``dry_run``/``path``/``error`` so a
        scheduler (or the API) can report what actually happened — "email
        sent" is never assumed.

        Delivery requires **both** an explicit ``dry_run=False`` and a
        configured host: ``None`` (the default) and ``True`` both write to the
        outbox, so the only way a real message leaves is a caller that asked
        for one.
        """
        stamp = (now or datetime.now()).strftime("%Y-%m-%d_%H%M")
        pdf_bytes = render_pnl_pdf(report)
        try:
            message = self.build_message(report, pdf_bytes, to_email=to_email, now=now)
        except ValueError as exc:
            return {"sent": False, "dry_run": True, "error": str(exc), "path": None}

        deliver = bool(dry_run is False and self.config.can_send)
        if not deliver:
            outbox = self.config.outbox_dir
            outbox.mkdir(parents=True, exist_ok=True)
            path = outbox / f"pnl_{report.period.end.isoformat()}_{stamp}.eml"
            path.write_bytes(bytes(message))
            logger.info("report email written to %s (dry run)", path)
            reason = (
                "dry run requested (pass dry_run=False to deliver)"
                if dry_run is not False
                else "smtp.host / monthly_email.enabled / to_email not all configured"
            )
            return {
                "sent": False,
                "dry_run": True,
                "path": str(path),
                "subject": message["Subject"],
                "to": message["To"],
                "reason": reason,
                "error": None,
            }

        try:
            self._deliver(message)
        except Exception as exc:  # noqa: BLE001 — a mail failure is reported, not raised
            logger.exception("report email delivery failed")
            return {
                "sent": False,
                "dry_run": False,
                "path": None,
                "subject": message["Subject"],
                "to": message["To"],
                "error": str(exc),
            }
        logger.info("report email sent to %s", message["To"])
        return {
            "sent": True,
            "dry_run": False,
            "path": None,
            "subject": message["Subject"],
            "to": message["To"],
            "error": None,
        }

    def _deliver(self, message: EmailMessage) -> None:
        config = self.config
        context = ssl.create_default_context()
        if config.port == 465:
            with smtplib.SMTP_SSL(config.host, config.port, context=context, timeout=30) as server:
                if config.user:
                    server.login(config.user, config.password)
                server.send_message(message)
            return
        with smtplib.SMTP(config.host, config.port, timeout=30) as server:
            if config.use_tls:
                server.starttls(context=context)
            if config.user:
                server.login(config.user, config.password)
            server.send_message(message)

    # -- convenience -------------------------------------------------------

    def send_monthly_report(
        self,
        *,
        period: Period | None = None,
        to_email: str | None = None,
        dry_run: bool | None = None,
        include_paper: bool = False,
        demo: bool = False,
    ) -> dict[str, Any]:
        """Build the (previous month's) report and send it.

        Defaults to the month that just closed and to live trades only — a
        monthly statement that leads with simulated money is the "Instagram
        trading" failure this feature exists to avoid.
        """
        target = period or (
            Period.previous_month() if self.config.report_previous_month else Period.month()
        )
        consolidator = self._consolidator or ConsolidatedPnL()
        report = consolidator.generate_report(
            target.start, target.end, include_paper=include_paper, demo=demo
        )
        result = self.send(report, to_email=to_email, dry_run=dry_run)
        result["period"] = target.to_dict()
        result["summary"] = report.summary()
        return result


def send_monthly_report(
    *,
    period: Period | None = None,
    to_email: str | None = None,
    dry_run: bool | None = None,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Module-level entry point for a cron job / CLI."""
    emailer = MonthlyReportEmailer(load_email_config(config_path))
    return emailer.send_monthly_report(period=period, to_email=to_email, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Bodies
# ---------------------------------------------------------------------------


def _money(value: Any) -> str:
    return f"{float(value):,.2f}"


def _plain_body(report: PnLReport) -> str:
    lines = [
        f"Consolidated P&L — {report.period.label}",
        "",
        f"Gross P&L        {_money(report.gross_pnl)}",
        f"Costs            -{_money(report.total_costs)}",
        f"Net P&L          {_money(report.net_pnl)}",
        f"Estimated tax    -{_money(report.tax.total)}",
        f"Net after tax    {_money(report.net_after_tax)}",
        "",
        f"Trades: {report.trade_count}, win rate {report.win_rate * 100:.1f}%",
        "",
        "By broker:",
    ]
    lines.extend(
        f"  {row.broker}: {_money(row.net_pnl)} ({row.trades} trades)"
        for row in report.by_broker
    )
    if report.warnings:
        lines.append("")
        lines.append("Caveats:")
        lines.extend(f"  - {note}" for note in report.warnings)
    lines.append("")
    lines.append(
        "Estimates only — verify against broker contract notes and your CA. "
        "Detailed PDF attached."
    )
    return "\n".join(lines)


def _html_body(report: PnLReport) -> str:
    def row(label: str, value: Any, *, strong: bool = False) -> str:
        style = ' style="background:#e8f5e9;font-weight:700;"' if strong else ""
        return (
            f'<tr{style}><td style="padding:6px 10px;border:1px solid #ddd;">{label}</td>'
            f'<td style="padding:6px 10px;border:1px solid #ddd;text-align:right;">'
            f"{_money(value)}</td></tr>"
        )

    brokers = "".join(
        f"<li>{row_.broker}: {_money(row_.net_pnl)} ({row_.trades} trades)</li>"
        for row_ in report.by_broker
    )
    caveats = "".join(f"<li>{note}</li>" for note in report.warnings)
    caveat_block = f"<h3>Caveats</h3><ul>{caveats}</ul>" if caveats else ""
    return f"""<html><body style="font-family:Arial,Helvetica,sans-serif;color:#222;">
<h2 style="margin-bottom:4px;">Consolidated P&amp;L — {report.period.label}</h2>
<p style="color:#666;margin-top:0;font-size:12px;">Generated
{report.generated_at.strftime('%d-%b-%Y %H:%M')} UTC ·
{'paper + live' if report.include_paper else 'live only'}</p>
<table style="border-collapse:collapse;font-size:14px;min-width:320px;">
{row("Gross P&L", report.gross_pnl)}
{row("Costs", -report.total_costs)}
{row("Net P&L", report.net_pnl, strong=True)}
{row("Estimated tax", -report.tax.total)}
{row("Net after tax", report.net_after_tax, strong=True)}
</table>
<p style="font-size:13px;">Trades: <b>{report.trade_count}</b> ·
win rate <b>{report.win_rate * 100:.1f}%</b> ·
cost + tax drag <b>{report.cost_drag_pct:.1f}%</b> of gross profit</p>
<h3>By broker</h3><ul style="font-size:13px;">{brokers}</ul>
{caveat_block}
<p style="color:#666;font-size:12px;">Estimates only, built from platform-recorded
trades. Verify against broker contract notes and your CA before filing —
this is not tax advice. The detailed PDF is attached.</p>
</body></html>"""
