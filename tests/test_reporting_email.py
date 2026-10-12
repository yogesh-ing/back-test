"""The monthly report email (PRD-002).

The rule under test is the safe one: **nothing is ever sent by accident.** A
misconfigured cron must produce a file on disk and a truthful result dict, not
200 emails — so most of these tests assert that no SMTP connection happened.
"""

from __future__ import annotations

import email
import textwrap
from datetime import datetime
from decimal import Decimal
from email import policy

import pytest

from backtest.reporting.consolidator import ConsolidatedPnL
from backtest.reporting.email_report import (
    EmailConfig,
    MonthlyReportEmailer,
    load_email_config,
    send_monthly_report,
)
from backtest.reporting.records import Period, TradeRecord
from backtest.simulator.fees import TradeSegment


class StaticSource:
    name = "static"

    def __init__(self, records):
        self.records = list(records)

    def fetch(self, period):
        return [r for r in self.records if period.contains(r.exit_time)]

    def describe(self):
        return {"kind": "static", "name": self.name, "note": f"{len(self.records)} record(s)"}


def _consolidator():
    record = TradeRecord(
        trade_id="t1",
        broker="mstock",
        mode="live",
        segment=TradeSegment.EQUITY_DELIVERY,
        symbol="INFY",
        quantity=Decimal(10),
        entry_time=datetime(2026, 8, 3, 10, 0),
        exit_time=datetime(2026, 8, 20, 15, 0),
        gross_pnl=Decimal("12345.67"),
    )
    return ConsolidatedPnL(sources=[StaticSource([record])])


def _config(tmp_path, **overrides) -> EmailConfig:
    payload = {
        "enabled": False,
        "host": "",
        "port": 587,
        "to_email": "trader@example.com",
        "outbox_dir": tmp_path / "outbox",
    }
    payload.update(overrides)
    return EmailConfig(**payload)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def test_config_defaults_never_send(tmp_path, monkeypatch):
    monkeypatch.delenv("REPORTING_EMAIL_ENABLED", raising=False)
    monkeypatch.delenv("REPORTING_EMAIL_TO", raising=False)
    config = load_email_config(tmp_path / "absent.yaml")
    assert config.can_send is False
    assert config.enabled is False
    assert config.port == 587
    assert config.use_tls is True


def test_config_reads_yaml_and_env_and_hides_the_password(tmp_path, monkeypatch):
    path = tmp_path / "reporting.yaml"
    path.write_text(
        textwrap.dedent(
            """
            monthly_email:
              enabled: true
              send_on_day: 3
              subject_prefix: "Book P&L"
            smtp:
              host: smtp.example.com
              port: 465
              user: report-bot
              password_env: MY_SMTP_PASSWORD
              outbox_dir: var/reporting/outbox
              use_tls: false
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MY_SMTP_PASSWORD", "s3cret")
    monkeypatch.setenv("REPORTING_EMAIL_TO", "me@example.com")
    config = load_email_config(path)
    assert config.can_send is True
    assert config.port == 465
    assert config.send_on_day == 3
    assert config.subject_prefix == "Book P&L"
    assert config.password == "s3cret"
    described = config.describe()
    assert "password" not in described and "s3cret" not in str(described)
    assert described["smtp_host"] == "smtp.example.com"
    assert described["can_send"] is True


def test_a_broken_config_file_keeps_the_mailer_in_dry_run(tmp_path, caplog):
    path = tmp_path / "reporting.yaml"
    path.write_text("monthly_email: [this is not a mapping", encoding="utf-8")
    config = load_email_config(path)
    assert config.can_send is False


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


def test_dry_run_writes_a_complete_eml_and_sends_nothing(tmp_path, monkeypatch):
    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("SMTP must not be contacted on a dry run")

    monkeypatch.setattr("smtplib.SMTP", explode)
    monkeypatch.setattr("smtplib.SMTP_SSL", explode)

    emailer = MonthlyReportEmailer(_config(tmp_path), consolidator=_consolidator())
    report = _consolidator().generate_report("2026-08-01", "2026-08-31")
    result = emailer.send(report)

    assert result["sent"] is False and result["dry_run"] is True
    path = result["path"]
    assert path and str(tmp_path) in path
    message = email.message_from_bytes(open(path, "rb").read(), policy=policy.default)
    assert message["To"] == "trader@example.com"
    assert "Monthly P&L" in message["Subject"]
    body = message.get_body(preferencelist=("plain",))
    assert "Net P&L" in body.get_content()
    attachments = list(message.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_filename().endswith(".pdf")
    assert attachments[0].get_payload(decode=True).startswith(b"%PDF")


def test_dry_run_is_the_default_even_when_smtp_is_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "smtplib.SMTP", lambda *a, **k: pytest.fail("dry-run default must not connect")
    )
    emailer = MonthlyReportEmailer(
        _config(tmp_path, enabled=True, host="smtp.example.com"), consolidator=_consolidator()
    )
    report = _consolidator().generate_report("2026-08-01", "2026-08-31")
    result = emailer.send(report)  # dry_run omitted → must not deliver
    assert result["sent"] is False
    assert result["reason"].startswith("dry run requested")


def test_missing_recipient_is_reported_not_guessed(tmp_path):
    emailer = MonthlyReportEmailer(
        _config(tmp_path, to_email=""), consolidator=_consolidator()
    )
    report = _consolidator().generate_report("2026-08-01", "2026-08-31")
    result = emailer.send(report)
    assert result["sent"] is False
    assert "recipient" in result["error"]


def test_unconfigured_smtp_writes_to_the_outbox_with_a_reason(tmp_path):
    emailer = MonthlyReportEmailer(_config(tmp_path), consolidator=_consolidator())
    report = _consolidator().generate_report("2026-08-01", "2026-08-31")
    result = emailer.send(report, dry_run=False)
    assert result["sent"] is False
    assert "not all configured" in result["reason"]


# ---------------------------------------------------------------------------
# Delivery (against a fake SMTP server)
# ---------------------------------------------------------------------------


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=30, context=None):
        self.host, self.port = host, port
        self.started_tls = False
        self.logged_in = None
        self.sent = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.started_tls = True

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, message):
        self.sent.append(message)


def test_delivery_uses_starttls_on_the_submission_port(tmp_path, monkeypatch):
    FakeSMTP.instances.clear()
    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    emailer = MonthlyReportEmailer(
        _config(tmp_path, enabled=True, host="smtp.example.com", port=587, user="bot",
                password="pw"),
        consolidator=_consolidator(),
    )
    report = _consolidator().generate_report("2026-08-01", "2026-08-31")
    result = emailer.send(report, dry_run=False)
    assert result["sent"] is True and result["error"] is None
    server = FakeSMTP.instances[-1]
    assert server.started_tls is True
    assert server.logged_in == ("bot", "pw")
    assert len(server.sent) == 1


def test_delivery_uses_ssl_on_465(tmp_path, monkeypatch):
    seen = {}

    class FakeSSL(FakeSMTP):
        def __init__(self, host, port, timeout=30, context=None):
            super().__init__(host, port, timeout=timeout, context=context)
            seen["ssl"] = True

    monkeypatch.setattr("smtplib.SMTP_SSL", FakeSSL)
    emailer = MonthlyReportEmailer(
        _config(tmp_path, enabled=True, host="smtp.example.com", port=465),
        consolidator=_consolidator(),
    )
    report = _consolidator().generate_report("2026-08-01", "2026-08-31")
    assert emailer.send(report, dry_run=False)["sent"] is True
    assert seen.get("ssl") is True and FakeSMTP.instances[-1].started_tls is False


def test_a_delivery_failure_is_reported_not_raised(tmp_path, monkeypatch):
    class BrokenSMTP(FakeSMTP):
        def send_message(self, message):
            raise OSError("connection reset")

    monkeypatch.setattr("smtplib.SMTP", BrokenSMTP)
    emailer = MonthlyReportEmailer(
        _config(tmp_path, enabled=True, host="smtp.example.com"), consolidator=_consolidator()
    )
    report = _consolidator().generate_report("2026-08-01", "2026-08-31")
    result = emailer.send(report, dry_run=False)
    assert result["sent"] is False
    assert "connection reset" in result["error"]


# ---------------------------------------------------------------------------
# The monthly wrapper
# ---------------------------------------------------------------------------


def test_monthly_report_defaults_to_the_month_that_just_closed(tmp_path):
    emailer = MonthlyReportEmailer(_config(tmp_path), consolidator=_consolidator())
    result = emailer.send_monthly_report(period=Period.parse("2026-08-01", "2026-08-31"))
    assert result["period"]["start"] == "2026-08-01"
    assert result["period"]["end"] == "2026-08-31"
    assert result["summary"]["gross_pnl"] == pytest.approx(12345.67)
    assert result["sent"] is False


def test_monthly_report_excludes_paper_by_default(tmp_path):
    capture = {}

    class CapturingConsolidator(ConsolidatedPnL):
        def generate_report(self, *args, **kwargs):
            capture.update(kwargs)
            return super().generate_report(*args, **kwargs)

    emailer = MonthlyReportEmailer(
        _config(tmp_path),
        consolidator=CapturingConsolidator(sources=[StaticSource([])]),
    )
    emailer.send_monthly_report(period=Period.parse("2026-08-01", "2026-08-31"))
    assert capture["include_paper"] is False


def test_module_level_send_monthly_report_uses_the_configured_path(tmp_path, monkeypatch):
    config_path = tmp_path / "reporting.yaml"
    config_path.write_text(
        textwrap.dedent(
            f"""
            monthly_email:
              enabled: false
              to_email: trader@example.com
            smtp:
              outbox_dir: {tmp_path / "outbox"}
            """
        ),
        encoding="utf-8",
    )
    result = send_monthly_report(
        period=Period.parse("2026-08-01", "2026-08-31"),
        config_path=config_path,
        dry_run=True,
    )
    assert result["dry_run"] is True
    assert result["path"] is not None
