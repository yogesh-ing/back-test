"""Phase 3 item 3 — the primary-feed-loss alert (impl plan / §8 item 8).

Wired to the existing staleness signal in ``forward/feed_quality.py``, not a
new subsystem: losing the PRIMARY feed pages (``DATA_FEED_STALE`` on the alert
broker), losing the WITNESS costs one log line and never a halt (the item-F
rule). Acceptance: a fake primary-socket death raises the alert; a fake
witness death raises only a log line.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from backtest.alerts.types import AlertType
from backtest.data import cross_broker_shadow as shadow
from backtest.forward import feed_quality
from backtest.forward.feed_quality import (
    FEED_ROLE_PRIMARY,
    FEED_ROLE_WITNESS,
    report_feed_loss,
    report_feed_recovery,
    reset_quality_monitor,
)
from backtest.forward.feed_registry import _BrokerBarFeedBase


class _StubAlertBroker:
    def __init__(self):
        self.raised: list = []
        self.resolved: list = []

    def raise_alert(self, alert_type, severity, message, data=None, subject="", **kw):
        self.raised.append((alert_type, severity, message, subject, dict(data or {})))
        return (SimpleNamespace(alert_id="a1"), True)

    def resolve_key(self, alert_type, subject, reason=""):
        self.resolved.append((alert_type, subject, reason))
        return True


@pytest.fixture()
def alerts(monkeypatch, tmp_path):
    stub = _StubAlertBroker()
    monkeypatch.setattr("backtest.alerts.broker.get_alert_broker", lambda: stub)
    monkeypatch.setattr(feed_quality, "QUALITY_LOG_PATH", str(tmp_path / "q.log"))
    reset_quality_monitor()
    yield stub
    reset_quality_monitor()


class _FakePrimaryFeed(_BrokerBarFeedBase):
    """The bar feed is the book's primary — a client whose socket is dead."""

    broker_name = "mstock"
    _feed_client_class = object

    def __init__(self, client):
        self._client = client


class _DeadClient:
    def latest_bar(self, symbol):
        raise ConnectionError("socket closed")  # the fake primary-socket death


def test_fake_primary_socket_death_raises_the_alert(alerts):
    feed = _FakePrimaryFeed(_DeadClient())

    assert feed._fetch_bar("NIFTY") is None

    (raised,) = alerts.raised
    alert_type, severity, message, subject, data = raised
    assert alert_type == AlertType.DATA_FEED_STALE.value
    assert severity == "critical"
    assert subject == "mstock:NIFTY"
    assert "PRIMARY" in message and "socket closed" in message
    assert data["role"] == FEED_ROLE_PRIMARY


def test_primary_recovery_closes_the_episode(alerts):
    report_feed_loss(FEED_ROLE_PRIMARY, "mstock", "NIFTY", "socket closed")
    report_feed_recovery(FEED_ROLE_PRIMARY, "mstock", "NIFTY")
    assert alerts.resolved == [
        (AlertType.DATA_FEED_STALE.value, "mstock:NIFTY", "feed delivering again")
    ]


def test_fake_witness_death_raises_only_a_log_line(alerts, caplog):
    caplog.set_level(logging.WARNING, logger=feed_quality.logger.name)

    report_feed_loss(FEED_ROLE_WITNESS, "upstox", "NIFTY", "socket closed")

    assert alerts.raised == [], "the witness rule: one log line, never a page"
    assert "FEED_LOSS_WITNESS" in caplog.text


def test_the_shadow_reports_witness_loss_once_per_episode(alerts, caplog):
    caplog.set_level(logging.WARNING, logger=feed_quality.logger.name)
    instance = shadow.CrossBrokerShadow(
        "NIFTY",
        SimpleNamespace(source_name="live:mstock"),
        token_provider=lambda: None,
        upstox_chain=lambda underlying: [],
        wrapper_factory=lambda _tp: None,
    )

    instance._report_witness_loss(ValueError("socket closed"))
    instance._report_witness_loss(ValueError("socket closed"))  # still one episode
    assert caplog.text.count("FEED_LOSS_WITNESS") == 1
    assert alerts.raised == []

    instance._witness_loss_reported = False  # a good pass re-arms
    instance._report_witness_loss(ValueError("socket closed"))
    assert caplog.text.count("FEED_LOSS_WITNESS") == 2


def test_witness_recovery_is_as_quiet_as_witness_loss(alerts, caplog):
    report_feed_recovery(FEED_ROLE_WITNESS, "upstox", "NIFTY")
    assert alerts.resolved == []
    assert "FEED_LOSS" not in caplog.text
