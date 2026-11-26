"""TREND_REGIME_CHANGE emission (``_rule_trend``).

The trend factor is refreshed off the tick path; this pins the rule's contract:
first observation is a silent baseline, a flip raises one event alert, and a
repeat status does not re-raise.
"""

from __future__ import annotations

from backtest.alerts.types import AlertType
from backtest.intelligence.service import PortfolioIntelligence

TREND = AlertType.TREND_REGIME_CHANGE.value


class RecordingBroker:
    event_ttl_s = 3600.0
    ignore_ttl_s = 3600.0
    renotify_cooldown_s = 60.0
    auto_dismiss_exempt = set()

    def __init__(self):
        self.raised = []
        self.resolved = []

    def raise_alert(self, alert_type, severity, message, data=None, subject="", **_):
        self.raised.append({"type": str(alert_type), "message": message, "data": data or {}})
        return None, True

    def resolve_missing(self, alert_type, active, reason=""):
        self.resolved.append((str(alert_type), reason))


def _intel():
    broker = RecordingBroker()
    intel = PortfolioIntelligence(manager=object(), broker=broker)
    return intel, broker


def test_first_observation_is_a_silent_baseline():
    intel, broker = _intel()
    intel._trend_status = "up"
    intel._rule_trend()
    assert broker.raised == []
    assert intel._last_trend_status == "up"


def test_flip_raises_one_alert():
    intel, broker = _intel()
    intel._trend_status = "up"
    intel._rule_trend()
    intel._trend_status = "down"
    intel._rule_trend()
    assert len(broker.raised) == 1
    alert = broker.raised[0]
    assert alert["type"] == TREND
    assert alert["data"]["regime"] == "down"
    assert alert["data"]["previous"] == "up"


def test_repeat_status_does_not_re_raise():
    intel, broker = _intel()
    intel._trend_status = "up"
    intel._rule_trend()
    intel._trend_status = "down"
    intel._rule_trend()
    intel._rule_trend()
    assert len(broker.raised) == 1


def test_unknown_status_is_ignored():
    intel, broker = _intel()
    intel._trend_status = "up"
    intel._rule_trend()
    intel._trend_status = "unknown"
    intel._rule_trend()
    assert len(broker.raised) == 0
    assert intel._last_trend_status == "up"