"""Broker-session alerts — the monitor must inform, not just log.

A dead mStock session silently stops feeds and pauses runner entries, so the
expiry monitor publishes platform alerts on transitions:

* ``expiring_soon`` → ``broker_session_expiring`` (warning, per broker)
* ``expired``       → ``broker_session_expired`` (critical; supersedes the
  expiring alert and fires exactly once per cycle — Telegram must not get a
  message every 5-minute tick)
* re-login / logout / recovery → both keys resolve for that broker.

Publishing is best-effort: a broken alerts layer must never kill the monitor
thread.
"""

from __future__ import annotations

from typing import Any

import pytest

from backtest.alerts.broker import reset_alert_broker
from backtest.alerts.types import AlertType
from backtest.brokers.base import (
    STATUS_AUTHENTICATED,
    STATUS_EXPIRED,
    STATUS_EXPIRING_SOON,
    STATUS_UNAUTHENTICATED,
    BrokerAuthBase,
)
from backtest.brokers.session_manager import BrokerSessionManager


class _StubBroker(BrokerAuthBase):
    broker_name = "stub"
    broker_display_name = "Stub Broker"

    def __init__(self, status: str = STATUS_AUTHENTICATED):
        self._status = status
        self._token = "stub-token-123" if status == STATUS_AUTHENTICATED else None
        self._expires_at = "2026-08-25T15:45:00"
        self.logout_calls = 0
        self._hold_expired = False

    def login(self, username: str, password: str) -> dict[str, Any]:
        return {"success": True, "message": "ok", "requires_totp": True}

    def verify_totp(self, totp_code: str) -> dict[str, Any]:
        self._status = STATUS_AUTHENTICATED
        self._token = "stub-token-123"
        return {"success": True, "message": "ok", "expires_at": self._expires_at}

    def get_session_status(self) -> dict[str, Any]:
        return {"status": self._status, "expires_at": self._expires_at, "broker": self.broker_name}

    def get_session_token(self) -> str | None:
        if self._status in (STATUS_AUTHENTICATED, STATUS_EXPIRING_SOON):
            return self._token
        return None

    def logout(self) -> None:
        self.logout_calls += 1
        if not self._hold_expired:
            self._status = STATUS_UNAUTHENTICATED
        self._token = None


@pytest.fixture()
def alert_broker():
    """Fresh AlertBroker singleton per test (the session monitor publishes to it)."""
    broker = reset_alert_broker()
    yield broker
    reset_alert_broker()


@pytest.fixture()
def stub() -> _StubBroker:
    return _StubBroker()


@pytest.fixture()
def manager(stub: _StubBroker) -> BrokerSessionManager:
    return BrokerSessionManager(broker_factory=lambda: stub)


def _open(broker, alert_type: str):
    return [a for a in broker.get_active_alerts() if str(a.alert_type) == alert_type]


# ---------------------------------------------------------------------------
# Monitor transitions → alerts
# ---------------------------------------------------------------------------


def test_expiring_transition_raises_warning_alert(manager, stub, alert_broker):
    stub._status = STATUS_EXPIRING_SOON
    manager._poll_once()

    rows = _open(alert_broker, AlertType.BROKER_SESSION_EXPIRING.value)
    assert len(rows) == 1
    alert = rows[0]
    assert str(alert.severity) == "warning"
    assert alert.subject == "stub"
    assert alert.data["broker"] == "stub"
    assert alert.data["broker_display_name"] == "Stub Broker"
    assert alert.data["expires_at"] == "2026-08-25T15:45:00"
    assert "Re-authenticate" in alert.message or "re-authenticate" in alert.message


def test_still_expiring_does_not_republish(manager, stub, alert_broker):
    stub._status = STATUS_EXPIRING_SOON
    manager._poll_once()
    manager._poll_once()  # same state — no new information

    rows = _open(alert_broker, AlertType.BROKER_SESSION_EXPIRING.value)
    assert len(rows) == 1
    assert rows[0].occurrences == 1


def test_expired_supersedes_expiring_and_is_critical(manager, stub, alert_broker):
    stub._status = STATUS_EXPIRING_SOON
    manager._poll_once()
    stub._status = STATUS_EXPIRED
    manager._poll_once()

    assert _open(alert_broker, AlertType.BROKER_SESSION_EXPIRING.value) == []
    rows = _open(alert_broker, AlertType.BROKER_SESSION_EXPIRED.value)
    assert len(rows) == 1
    assert str(rows[0].severity) == "critical"
    assert rows[0].data["broker"] == "stub"


def test_expired_raises_once_even_if_state_persists(manager, stub, alert_broker):
    """Telegram must not get one message per 5-minute monitor tick."""
    notified: list[tuple[str, dict]] = []
    alert_broker.subscribe(
        AlertType.BROKER_SESSION_EXPIRED.value,
        lambda t, d: notified.append((t, d)),
        subscriber_id="recorder",
    )
    stub._hold_expired = True  # logout does not move it out of EXPIRED
    stub._status = STATUS_EXPIRED
    manager._poll_once()
    manager._poll_once()
    manager._poll_once()

    rows = _open(alert_broker, AlertType.BROKER_SESSION_EXPIRED.value)
    assert len(rows) == 1
    assert len(notified) == 1


def test_recovered_session_resolves_both_alerts(manager, stub, alert_broker):
    stub._status = STATUS_EXPIRING_SOON
    manager._poll_once()
    stub._status = STATUS_EXPIRED
    manager._poll_once()
    assert len(alert_broker.get_active_alerts()) == 1

    stub._status = STATUS_AUTHENTICATED  # e.g. re-login from the popup
    manager._poll_once()
    assert alert_broker.get_active_alerts() == []


def test_verified_totp_resolves_immediately_without_a_tick(manager, stub, alert_broker):
    stub._status = STATUS_EXPIRING_SOON
    manager._poll_once()
    assert _open(alert_broker, AlertType.BROKER_SESSION_EXPIRING.value)

    manager.verify_totp("123456")  # re-auth lands here from /api/broker/verify-totp
    assert alert_broker.get_active_alerts() == []


def test_manual_logout_resolves_session_alerts(manager, stub, alert_broker):
    stub._status = STATUS_EXPIRING_SOON
    manager._poll_once()
    assert _open(alert_broker, AlertType.BROKER_SESSION_EXPIRING.value)

    manager.logout()  # deliberate — the alert would otherwise haunt the widget
    assert alert_broker.get_active_alerts() == []


# ---------------------------------------------------------------------------
# Isolation: a broken alerts layer must not kill the monitor
# ---------------------------------------------------------------------------


def test_publish_failure_does_not_break_the_monitor(manager, stub, monkeypatch, alert_broker):
    """The helpers swallow everything — even a dying alert singleton."""

    def _boom():
        raise RuntimeError("alerts layer on fire")

    monkeypatch.setattr("backtest.brokers.session_manager._alert_broker", _boom)
    stub._status = STATUS_EXPIRING_SOON
    manager._poll_once()  # must not raise

    # The pre-existing notification surface still works.
    assert manager.consume_expiring_soon_notification() is True


def test_resolve_failure_does_not_break_verify_totp(manager, stub, monkeypatch, alert_broker):
    def _boom():
        raise RuntimeError("no broker")

    monkeypatch.setattr("backtest.brokers.session_manager._alert_broker", _boom)
    result = manager.verify_totp("123456")
    assert result["success"] is True


def test_alert_types_and_routing_registered(alert_broker):
    """The vocabulary and the Telegram route exist for both session types."""
    import yaml
    from pathlib import Path

    assert AlertType.BROKER_SESSION_EXPIRING.value == "broker_session_expiring"
    assert AlertType.BROKER_SESSION_EXPIRED.value == "broker_session_expired"

    cfg = yaml.safe_load(Path("config/alerts.yaml").read_text(encoding="utf-8"))
    default = cfg["default"]
    routing = default["routing"]
    assert "telegram" in routing["broker_session_expiring"]
    assert "telegram" in routing["broker_session_expired"]
    templates = default["templates"]
    assert "{broker_display_name}" in templates["broker_session_expiring"]
    assert "{broker_display_name}" in templates["broker_session_expired"]

    from backtest.alerts.catalog import CATALOG

    for t in (AlertType.BROKER_SESSION_EXPIRING.value, AlertType.BROKER_SESSION_EXPIRED.value):
        assert t in CATALOG and CATALOG[t]["typical_responses"]


def test_expired_session_reaches_telegram_once_with_the_real_config():
    """End-to-end: monitor transition → notifier routed by config/alerts.yaml.

    The user's complaint was silence — this proves the dead-session event is a
    high-priority outbound message, sent exactly once per expiry cycle.
    """
    import json
    import urllib.parse
    from pathlib import Path

    from backtest.alerts.notifier import AlertNotifier, load_notifier_config

    sent: list[str] = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"ok": True}).encode()

    def opener(request, timeout=None):
        payload = json.loads(request.data.decode())
        sent.append(payload["text"])
        return _Resp()

    cfg = load_notifier_config(
        path=Path("config/alerts.yaml"),
        env={"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "42"},
    )
    broker = reset_alert_broker()
    notifier = AlertNotifier(broker, cfg, synchronous=True, telegram_opener=opener).start()
    try:
        stub = _StubBroker(status=STATUS_EXPIRING_SOON)
        manager = BrokerSessionManager(broker_factory=lambda: stub)
        manager._poll_once()  # expiring_soon → one warning message
        stub._status = STATUS_EXPIRED
        manager._poll_once()  # expired → one critical message
        assert len(sent) == 2
        assert "expires" in sent[0]
        assert "EXPIRED" in sent[1]

        stub._status = STATUS_AUTHENTICATED  # re-login
        stub._token = "t"
        manager._poll_once()
        manager._poll_once()
        assert len(sent) == 2  # recovery resolves silently, no re-notify
    finally:
        notifier.stop()
        reset_alert_broker()
