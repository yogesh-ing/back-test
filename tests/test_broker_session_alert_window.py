"""Sprint A4 — session paging is market-hours-only, and T − 10m is the gate.

Two failures this file exists to prevent:

* **Paging at 21:00.** A token that dies after the close is normal — nothing is
  trading. An evening of backtesting or diary review must not be interrupted by
  a Telegram and a login popup, so every session page is held back outside
  ``PRE_OPEN``/``OPEN``.
* **Paging too late.** The old 5-minute cadence could step straight over the
  T − 10m action gate: a session expiring at 10:07 polled at 10:05 and again at
  10:10 is already dead at the second tick, so the operator hears about it after
  the feed stopped instead of before. Hence the 30-second tick and an explicit
  ``broker_session_expiring_urgent`` stage raised CRITICAL (which the notifier
  already treats as quiet-hours and rate-limit exempt).

The held-back case is why the publish latch is "is this alert already open" and
not "is this the first tick that saw it": a first sighting at 20:00 publishes
nothing, and 09:00 the next morning is not a first sighting — so an edge-only
latch would lose the alert forever. ``test_a_held_back_expiry_pages_at_the_next_open``
is the one that fails if anyone "simplifies" that back.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
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
from backtest.brokers.session_manager import (
    EXPIRING_URGENT_WINDOW_MINUTES,
    MONITOR_INTERVAL_SECONDS,
    BrokerSessionManager,
)
from backtest.timekeeping import EXCHANGE_TZ, now_utc

EXPIRING = AlertType.BROKER_SESSION_EXPIRING.value
URGENT = AlertType.BROKER_SESSION_EXPIRING_URGENT.value
EXPIRED = AlertType.BROKER_SESSION_EXPIRED.value


class _StubBroker(BrokerAuthBase):
    """A broker whose expiry the test moves by hand.

    ``expires_at`` is naive local ISO on purpose: that is exactly what mStock
    reports (its session clock is ``datetime.now()`` and ``restore_session``
    strips the offset), and ``_minutes_until`` has to be exercised on the real
    payload shape rather than a convenient aware one.
    """

    broker_name = "stub"
    broker_display_name = "Stub Broker"

    def __init__(self, status: str = STATUS_AUTHENTICATED, minutes_left: float | None = 25.0):
        self._status = status
        self.minutes_left = minutes_left
        self.logout_calls = 0
        self.hold_expired = False
        self._token = None if status == STATUS_UNAUTHENTICATED else "stub-token"

    def _expires_at(self) -> str | None:
        if self.minutes_left is None:
            return None
        local = now_utc().astimezone(EXCHANGE_TZ) + timedelta(minutes=self.minutes_left)
        return local.replace(tzinfo=None).isoformat()

    def login(self, username: str, password: str) -> dict[str, Any]:
        return {"success": True, "message": "ok", "requires_totp": True}

    def verify_totp(self, totp_code: str) -> dict[str, Any]:
        self._status = STATUS_AUTHENTICATED
        self._token = "stub-token"
        return {"success": True, "message": "ok", "expires_at": self._expires_at()}

    def get_session_status(self) -> dict[str, Any]:
        return {
            "status": self._status,
            "expires_at": self._expires_at(),
            "broker": self.broker_name,
        }

    def get_session_token(self) -> str | None:
        if self._status in (STATUS_AUTHENTICATED, STATUS_EXPIRING_SOON):
            return self._token
        return None

    def logout(self) -> None:
        self.logout_calls += 1
        if not self.hold_expired:
            self._status = STATUS_UNAUTHENTICATED
        self._token = None


@pytest.fixture(autouse=True)
def alert_broker():
    broker = reset_alert_broker()
    yield broker
    reset_alert_broker()


@pytest.fixture(autouse=True)
def market_state(monkeypatch):
    """Drive the calendar the gate reads — OPEN unless a test says otherwise.

    Patches ``get_market_day_state`` rather than ``_market_window_open`` so the
    gate function itself (its session-state vocabulary and its fail-open
    behaviour) is under test, not stubbed out.
    """

    def _set(session_state: str) -> None:
        monkeypatch.setattr(
            "backtest.live.market_status.get_market_day_state",
            lambda *a, **k: {"session_state": session_state},
        )

    _set("OPEN")
    return _set


@pytest.fixture()
def stub() -> _StubBroker:
    return _StubBroker(status=STATUS_EXPIRING_SOON)


@pytest.fixture()
def manager(stub: _StubBroker) -> BrokerSessionManager:
    return BrokerSessionManager(broker_factory=lambda: stub)


def _open(broker, alert_type: str):
    return [a for a in broker.get_active_alerts() if str(a.alert_type) == alert_type]


# ---------------------------------------------------------------------------
# The market-hours gate
# ---------------------------------------------------------------------------


def test_no_page_outside_the_market_window(manager, stub, alert_broker, market_state):
    """After the close the session ladder is silent — but the state still moves."""
    market_state("POST_CLOSE")
    stub.minutes_left = 20.0
    manager._poll_once()

    assert alert_broker.get_active_alerts() == []
    # The in-app notification surface is not paging: it must still report the
    # transition, or a UI that polls it would never learn the session is short.
    assert manager.consume_expiring_soon_notification() is True


def test_an_expired_session_is_still_cleared_outside_the_window(
    manager, stub, alert_broker, market_state
):
    """Holding back the page must not hold back the token clear."""
    market_state("POST_CLOSE")
    stub._status = STATUS_EXPIRED
    stub.hold_expired = True
    manager._poll_once()

    assert stub.logout_calls == 1
    assert alert_broker.get_active_alerts() == []


def test_a_held_back_expiry_pages_at_the_next_open(
    manager, stub, alert_broker, market_state
):
    """The 20:00 expiry is reported at 09:00 — not silently lost.

    This is the whole reason the publish latch asks "already open?" instead of
    "first sighting?". The first sighting happened while the gate was shut.
    """
    market_state("POST_CLOSE")
    stub._status = STATUS_EXPIRED
    stub.hold_expired = True
    manager._poll_once()
    manager._poll_once()
    assert alert_broker.get_active_alerts() == []

    market_state("OPEN")  # next morning
    manager._poll_once()

    rows = _open(alert_broker, EXPIRED)
    assert len(rows) == 1
    assert str(rows[0].severity) == "critical"


def test_pre_open_counts_as_the_active_window(manager, stub, alert_broker, market_state):
    """09:00–09:15 is when a login is cheapest — the gate must be open then."""
    market_state("PRE_OPEN")
    stub.minutes_left = 5.0
    manager._poll_once()

    assert len(_open(alert_broker, URGENT)) == 1


def test_an_unreadable_calendar_fails_open(manager, stub, alert_broker, monkeypatch):
    """A broken holiday table must not mute the one alert that keeps the feed alive."""

    def _boom(*a, **k):
        raise RuntimeError("market_holidays unreachable")

    monkeypatch.setattr("backtest.live.market_status.get_market_day_state", _boom)
    stub._status = STATUS_EXPIRED
    stub.hold_expired = True
    manager._poll_once()

    assert len(_open(alert_broker, EXPIRED)) == 1


# ---------------------------------------------------------------------------
# Stage 2: T − 10m
# ---------------------------------------------------------------------------


def test_inside_ten_minutes_the_page_goes_critical(manager, stub, alert_broker):
    stub.minutes_left = 7.0
    manager._poll_once()

    rows = _open(alert_broker, URGENT)
    assert len(rows) == 1
    alert = rows[0]
    assert str(alert.severity) == "critical"
    assert alert.subject == "stub"
    assert alert.data["broker_display_name"] == "Stub Broker"
    assert alert.data["minutes_remaining"] == pytest.approx(7.0, abs=0.1)
    assert "NOW" in alert.message


def test_twenty_minutes_out_only_the_warning_fires(manager, stub, alert_broker):
    """The ladder has two rungs; the first one must not scream."""
    stub.minutes_left = 20.0
    manager._poll_once()

    assert len(_open(alert_broker, EXPIRING)) == 1
    assert str(_open(alert_broker, EXPIRING)[0].severity) == "warning"
    assert _open(alert_broker, URGENT) == []


def test_the_urgent_stage_fires_on_a_later_tick(manager, stub, alert_broker):
    """Crossing T − 10m is not a status edge — it happens mid-``expiring_soon``.

    An edge-only check would see ``expiring_soon`` at 20 minutes, then never
    look again, and step straight over the only actionable moment.
    """
    stub.minutes_left = 20.0
    manager._poll_once()
    assert _open(alert_broker, URGENT) == []

    stub.minutes_left = 5.0  # twenty minutes pass, status unchanged
    manager._poll_once()

    assert len(_open(alert_broker, URGENT)) == 1


def test_the_urgent_stage_pages_once_per_cycle(manager, stub, alert_broker):
    """A 30-second tick must not become a 30-second Telegram."""
    notified: list[tuple[str, dict]] = []
    alert_broker.subscribe(URGENT, lambda t, d: notified.append((t, d)), subscriber_id="rec")
    stub.minutes_left = 5.0
    for _ in range(6):
        manager._poll_once()

    assert len(_open(alert_broker, URGENT)) == 1
    assert len(notified) == 1


def test_an_already_past_expiry_does_not_page_as_urgent(manager, stub, alert_broker):
    """``expires in under a minute`` would be a lie about a dead session."""
    stub.minutes_left = -3.0
    manager._poll_once()

    assert _open(alert_broker, URGENT) == []


def test_expiry_supersedes_the_urgent_page(manager, stub, alert_broker):
    stub.minutes_left = 5.0
    manager._poll_once()
    assert len(_open(alert_broker, URGENT)) == 1

    stub._status = STATUS_EXPIRED
    stub.hold_expired = True
    manager._poll_once()

    assert _open(alert_broker, URGENT) == []
    assert len(_open(alert_broker, EXPIRED)) == 1


def test_reauth_resolves_the_urgent_page(manager, stub, alert_broker):
    stub.minutes_left = 5.0
    manager._poll_once()
    assert len(_open(alert_broker, URGENT)) == 1

    stub._status = STATUS_AUTHENTICATED
    stub.minutes_left = 240.0
    manager._poll_once()

    assert alert_broker.get_active_alerts() == []


# ---------------------------------------------------------------------------
# Cadence
# ---------------------------------------------------------------------------


def test_the_monitor_polls_every_thirty_seconds():
    """At 300s the tick could land after the expiry it was meant to warn about."""
    assert MONITOR_INTERVAL_SECONDS == 30.0
    assert EXPIRING_URGENT_WINDOW_MINUTES == 10
    # Two ticks inside the action window is the minimum for the gate to mean
    # anything: one to page, one to still be early enough to act.
    assert MONITOR_INTERVAL_SECONDS * 2 <= EXPIRING_URGENT_WINDOW_MINUTES * 60
    assert BrokerSessionManager(broker_factory=lambda: _StubBroker())._monitor_interval == 30.0


# ---------------------------------------------------------------------------
# Routing — both rungs reach every channel this deployment has
# ---------------------------------------------------------------------------


def test_urgent_and_expired_fan_out_to_every_channel():
    import yaml

    cfg = yaml.safe_load(Path("config/alerts.yaml").read_text(encoding="utf-8"))
    default = cfg["default"]
    expected = {"telegram", "email", "slack", "discord", "sms", "desktop", "log"}
    for alert_type in (URGENT, EXPIRED):
        assert set(default["routing"][alert_type]) == expected
        assert alert_type in default["templates"]

    # A template only renders if every placeholder is in the payload the monitor
    # actually sends — otherwise the notifier silently falls back to the raw
    # message and the operator loses the countdown.
    from backtest.alerts.notifier import _render_template

    payload = {
        "broker": "stub",
        "broker_display_name": "Stub Broker",
        "expires_at": "2026-11-03T10:00:00",
        "minutes_remaining": 7.4,
    }
    assert _render_template(default["templates"][URGENT], payload)

    from backtest.alerts.catalog import CATALOG

    assert CATALOG[URGENT]["typical_responses"]


def test_the_urgent_page_is_not_rate_limited():
    """CRITICAL is the notifier's rate-limit bypass — prove the page uses it.

    Paired control: the same budget swallows the warnings and lets every
    critical through, so the assertion is about severity and not about the
    limiter being off.
    """
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
        sent.append(json.loads(request.data.decode())["text"])
        return _Resp()

    cfg = load_notifier_config(
        path=Path("config/alerts.yaml"),
        env={"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "42"},
    )
    cfg["rate_limit_enabled"] = True
    cfg["max_alerts_per_hour"] = 1
    cfg["max_alerts_per_hour_per_channel"] = {"telegram": 1}

    broker = reset_alert_broker()
    notifier = AlertNotifier(broker, cfg, synchronous=True, telegram_opener=opener).start()
    try:
        for i in range(3):
            broker.raise_alert(EXPIRING, "warning", f"warning {i}", subject=f"b{i}")
        warnings_sent = len(sent)

        for i in range(3):
            broker.raise_alert(URGENT, "critical", f"urgent {i}", subject=f"b{i}")
        assert warnings_sent == 1
        assert len(sent) == 4
    finally:
        notifier.stop()
        reset_alert_broker()
