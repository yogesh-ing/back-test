"""Outbound notifier — routing, limits, quiet hours, delivery isolation.

The notifier is the way alerts leave the process (Telegram / email / log).
These tests pin the behaviour a trading deployment depends on:

* every alert type is subscribed, so a new type is never silently missed;
* routing (type-then-severity), min-level, quiet hours and hourly rate
  limits decide what actually goes out — critical is exempt from quiet
  hours and rate limits;
* a template is used only when every placeholder can be filled, otherwise
  a plain fallback goes out (never a rendered "None");
* one dead channel never blocks the others and nothing raises into the
  broker;
* async delivery drains on ``flush()`` and stops cleanly.

All transports are fakes — no network, no SMTP.
"""

from __future__ import annotations

import logging
import textwrap
from datetime import datetime, timezone

import pytest

from backtest.alerts import AlertBroker, AlertType
from backtest.alerts.notifier import (
    AlertNotifier,
    build_channels,
    get_notifier,
    load_notifier_config,
    start_alert_notifier,
    stop_alert_notifier,
)

GAMMA = AlertType.PORTFOLIO_GAMMA_CRITICAL.value
DELTA = AlertType.PORTFOLIO_DELTA_WARNING.value
OI = AlertType.OI_ANOMALY.value
FEED = AlertType.DATA_FEED_STALE.value
BREACH = AlertType.RISK_LIMIT_BREACH.value


# ---------------------------------------------------------------------------
# Fake transports
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, body: bytes = b'{"ok": true}') -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        pass


class FakeOpener:
    """Stands in for urllib's opener; records (url, json_body) per call."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list = []
        self.error = error

    def __call__(self, request, timeout=None):
        self.calls.append(
            (request.full_url, __import__("json").loads(request.data.decode("utf-8")))
        )
        if self.error:
            raise self.error
        return FakeResponse()


class FakeSMTP:
    """Stands in for smtplib.SMTP; records messages per instance."""

    instances: list = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.messages: list = []
        self.logged_in = None
        self.tls = False
        self.quit_called = False
        FakeSMTP.instances.append(self)

    def ehlo(self):  # noqa: N802 — smtplib's name
        pass

    def starttls(self):
        self.tls = True

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, message):
        self.messages.append(message)

    def quit(self):
        self.quit_called = True


@pytest.fixture()
def fake_smtp_cls():
    FakeSMTP.instances.clear()
    yield FakeSMTP
    FakeSMTP.instances.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TELEGRAM_CHANNEL = {
    "enabled": True,
    "telegram_bot_token": "tok",
    "telegram_chat_id": "42",
}


def make_config(**over):
    cfg = {
        "min_level": "info",
        "channels": {"log": {"enabled": True}, "telegram": dict(TELEGRAM_CHANNEL)},
        "routing": {"critical": ["telegram", "log"]},
        "templates": {},
    }
    cfg.update(over)
    return cfg


def make_notifier(broker, opener=None, smtp_factory=None, synchronous=True, **over):
    now = over.pop("now", None)  # clock override, not config
    return AlertNotifier(
        broker,
        make_config(**over),
        synchronous=synchronous,
        telegram_opener=opener,
        smtp_factory=smtp_factory,
        now=now,
    ).start()


@pytest.fixture()
def broker() -> AlertBroker:
    return AlertBroker()


# ---------------------------------------------------------------------------
# Subscription + delivery
# ---------------------------------------------------------------------------


def test_subscribes_to_every_alert_type(broker):
    n = make_notifier(broker)
    try:
        for alert_type in AlertType:
            subs = broker.subscribers_for(alert_type.value)
            assert any(s["subscriber_id"] == n.subscriber_id for s in subs), alert_type
    finally:
        n.stop()


def test_alert_reaches_telegram_and_log(broker, caplog):
    opener = FakeOpener()
    n = make_notifier(broker, opener=opener)
    try:
        with caplog.at_level(logging.CRITICAL, logger="backtest.alerts.outbound"):
            broker.raise_alert(GAMMA, "critical", "gamma -1247", data={"net_gamma": -1247})
        assert len(opener.calls) == 1
        url, body = opener.calls[0]
        assert url.endswith("/bottok/sendMessage")
        assert body["chat_id"] == "42"
        assert "portfolio gamma critical" in body["text"]
        assert "gamma -1247" in body["text"]
        assert n.sent == 2  # telegram + log
        assert not n.failures
        assert "gamma -1247" in caplog.text
    finally:
        n.stop()


def test_telegram_escapes_markdown_identifiers(broker):
    opener = FakeOpener()
    n = make_notifier(
        broker,
        opener=opener,
        channels={
            "log": {"enabled": True},
            "telegram": {**TELEGRAM_CHANNEL, "telegram_parse_mode": "Markdown"},
        },
        templates={BREACH: "⚠️ Risk limit breached: {limit_type} | {message}"},
    )
    try:
        broker.raise_alert(
            BREACH,
            "critical",
            "halted",
            subject="bucket:paper",
            data={"limit_type": "portfolio_daily_loss"},
        )
        text = opener.calls[0][1]["text"]
        # identifiers in template data have underscores — escaped so Telegram
        # shows them as plain text instead of italics/broken entities
        assert "portfolio\\_daily\\_loss" in text
        assert opener.calls[0][1]["parse_mode"] == "Markdown"
    finally:
        n.stop()


def test_type_routing_overrides_severity(broker, fake_smtp_cls):
    opener = FakeOpener()
    n = make_notifier(
        broker,
        opener=opener,
        smtp_factory=fake_smtp_cls,
        channels={
            "log": {"enabled": True},
            "telegram": dict(TELEGRAM_CHANNEL),
            "email": {
                "enabled": True,
                "smtp_host": "smtp.test",
                "from_email": "bot@test",
                "to_emails": ["ops@test"],
            },
        },
        routing={BREACH: ["email"], "critical": ["telegram"]},
    )
    try:
        broker.raise_alert(BREACH, "critical", "paper halted", subject="bucket:paper")
        assert opener.calls == []
        assert len(fake_smtp_cls.instances[0].messages) == 1
    finally:
        n.stop()


# ---------------------------------------------------------------------------
# Filters: min level, disabled channels, quiet hours, rate limits
# ---------------------------------------------------------------------------


def test_min_level_filters_low_severity(broker):
    opener = FakeOpener()
    n = make_notifier(broker, opener=opener, min_level="warning")
    try:
        broker.raise_alert(OI, "info", "oi spike", subject="NIFTY:24000CE")
        assert opener.calls == []
        assert n.sent == 0
    finally:
        n.stop()


def test_unconfigured_channels_are_skipped(broker):
    n = make_notifier(
        broker,
        channels={"log": {"enabled": True}},
        routing={"critical": ["telegram", "email", "log"]},
    )
    try:
        assert sorted(n.channels) == ["log"]
        broker.raise_alert(GAMMA, "critical", "x", subject="g")
        assert n.sent == 1  # log floor only
    finally:
        n.stop()


def test_quiet_hours_silence_warning_but_not_critical(broker):
    opener = FakeOpener()
    n = make_notifier(
        broker,
        opener=opener,
        routing={"warning": ["telegram", "log"], "critical": ["telegram", "log"]},
        quiet_hours_enabled=True,
        quiet_start="22:00",
        quiet_end="07:00",
        quiet_timezone="Asia/Kolkata",
        quiet_allow_critical=True,
        # 18:00 UTC == 23:30 IST — inside the quiet window
        now=lambda: datetime(2026, 9, 27, 18, 0, tzinfo=timezone.utc),
    )
    try:
        broker.raise_alert(DELTA, "warning", "delta 900", subject="w")
        broker.raise_alert(GAMMA, "critical", "gamma -1247", subject="c")
        assert len(opener.calls) == 1
        assert "portfolio gamma critical" in opener.calls[0][1]["text"]
        assert n.sent == 3  # warning→log, critical→telegram+log
    finally:
        n.stop()


def test_hourly_rate_limit_suppresses_after_cap(broker):
    opener = FakeOpener()
    n = make_notifier(
        broker,
        opener=opener,
        routing={"warning": ["telegram"]},
        rate_limit_enabled=True,
        max_alerts_per_hour=2,
    )
    try:
        for i, alert_type in enumerate(
            (
                AlertType.PORTFOLIO_DELTA_WARNING,
                AlertType.CONCENTRATION_HIGH,
                AlertType.CORRELATION_SPIKE,
            )
        ):
            broker.raise_alert(alert_type.value, "warning", f"w{i}", subject=f"s{i}")
        assert len(opener.calls) == 2
        assert n.dropped == 1
    finally:
        n.stop()


def test_critical_is_exempt_from_rate_limit(broker):
    opener = FakeOpener()
    n = make_notifier(
        broker,
        opener=opener,
        routing={"critical": ["telegram"]},
        rate_limit_enabled=True,
        max_alerts_per_hour=1,
    )
    try:
        broker.raise_alert(GAMMA, "critical", "g", subject="a")
        broker.raise_alert(FEED, "critical", "d", subject="b")
        assert len(opener.calls) == 2
    finally:
        n.stop()


# ---------------------------------------------------------------------------
# Message composition
# ---------------------------------------------------------------------------


def test_template_rendered_when_placeholders_present(broker):
    opener = FakeOpener()
    n = make_notifier(
        broker,
        opener=opener,
        templates={
            BREACH: "Risk limit breached: {limit_type} for {symbol} | {reason}",
        },
    )
    try:
        broker.raise_alert(
            BREACH,
            "critical",
            "PAPER halted",
            subject="bucket:paper",
            data={
                "limit_type": "portfolio_daily_loss",
                "symbol": "PAPER",
                "reason": "Global daily loss breached limit",
            },
        )
        assert opener.calls[0][1]["text"] == (
            "Risk limit breached: portfolio_daily_loss for PAPER | "
            "Global daily loss breached limit"
        )
    finally:
        n.stop()


def test_template_falls_back_when_placeholder_missing(broker):
    opener = FakeOpener()
    n = make_notifier(
        broker,
        opener=opener,
        templates={BREACH: "Risk limit breached: {limit_type} for {symbol}"},
    )
    try:
        broker.raise_alert(
            BREACH, "critical", "PAPER halted", subject="bucket:paper",
            data={"limit_type": "portfolio_daily_loss"},  # no symbol
        )
        text = opener.calls[0][1]["text"]
        assert text.startswith("[CRITICAL] risk limit breach")
        assert "PAPER halted" in text
    finally:
        n.stop()


# ---------------------------------------------------------------------------
# Failure isolation + async worker
# ---------------------------------------------------------------------------


def test_channel_failure_does_not_block_other_channels(broker, fake_smtp_cls):
    opener = FakeOpener(error=RuntimeError("network down"))
    n = make_notifier(
        broker,
        opener=opener,
        smtp_factory=fake_smtp_cls,
        channels={
            "log": {"enabled": True},
            "telegram": dict(TELEGRAM_CHANNEL),
            "email": {
                "enabled": True,
                "smtp_host": "smtp.test",
                "smtp_user": "bot@test",
                "smtp_password": "pw",
                "to_emails": ["ops@test"],
            },
        },
        routing={"critical": ["telegram", "email"]},
    )
    try:
        broker.raise_alert(GAMMA, "critical", "boom", subject="x")
        assert n.failures == 1
        assert n.sent == 1  # email still went out
        smtp = fake_smtp_cls.instances[0]
        assert len(smtp.messages) == 1
        assert smtp.logged_in == ("bot@test", "pw")
        assert smtp.tls is True
        assert smtp.quit_called is True
    finally:
        n.stop()


def test_async_delivery_drains_on_flush_and_stops(broker):
    opener = FakeOpener()
    n = make_notifier(broker, opener=opener, synchronous=False, routing={"critical": ["telegram"]})
    try:
        broker.raise_alert(GAMMA, "critical", "async", subject="a")
        assert n.flush(5.0) is True
        assert len(opener.calls) == 1
    finally:
        n.stop()
    assert n.running is False
    assert broker.subscribers_for(GAMMA) == []


# ---------------------------------------------------------------------------
# Config loading (yaml + env)
# ---------------------------------------------------------------------------

YAML_TEMPLATE = textwrap.dedent(
    """
    active_profile: default

    default:
      min_level: info
      channels:
        log:
          enabled: true
        telegram:
          enabled: false
      routing:
        critical: [telegram, log]

    profiles:
      telegram_only:
        min_level: warning
        routing:
          warning: [telegram, log]
    """
)


def test_loader_merges_profile_and_env(tmp_path):
    path = tmp_path / "alerts.yaml"
    path.write_text(YAML_TEMPLATE, encoding="utf-8")
    env = {
        "ALERT_PROFILE": "telegram_only",
        "TELEGRAM_BOT_TOKEN": "tok",
        "TELEGRAM_CHAT_ID": "7",
        "ALERT_SMTP_HOST": "smtp.example.com",
        "ALERT_SMTP_PORT": "2525",
        "ALERT_SMTP_USER": "bot@example.com",
        "ALERT_EMAIL_PASSWORD": "pw",
        "ALERT_TO_EMAILS": "a@x.com, b@y.com",
    }
    cfg = load_notifier_config(path=path, env=env)
    assert cfg["min_level"] == "warning"  # profile overrides default
    assert cfg["routing"]["warning"] == ["telegram", "log"]
    assert cfg["routing"]["critical"] == ["telegram", "log"]  # inherited
    channels = build_channels(cfg)
    assert sorted(channels) == ["email", "log", "telegram"]
    assert channels["email"].port == 2525
    assert channels["email"].from_email == "bot@example.com"
    assert channels["email"].to_emails == ["a@x.com", "b@y.com"]


def test_loader_missing_file_degrades_to_log(tmp_path):
    cfg = load_notifier_config(path=tmp_path / "nope.yaml", env={})
    assert sorted(build_channels(cfg)) == ["log"]


def test_master_switch_disables_and_singleton_is_idempotent():
    stop_alert_notifier()
    assert start_alert_notifier(env={"ALERT_NOTIFIER": "0"}) is None
    assert get_notifier() is None
    n = start_alert_notifier(env={})
    try:
        assert n is not None and n.running
        assert start_alert_notifier(env={}) is n  # idempotent
    finally:
        stop_alert_notifier()
    assert get_notifier() is None


def test_repo_yaml_ships_breach_routing_and_log_channel():
    cfg = load_notifier_config(env={})
    assert cfg["routing"]["risk_limit_breach"] == ["telegram", "slack", "log"]
    assert "log" in build_channels(cfg)
