"""SESSION_PHASE emission (``_rule_session``).

The phase is a pure clock read; this pins the baseline-then-flip contract and
that a repeat phase does not re-raise.
"""

from __future__ import annotations

from types import SimpleNamespace

from backtest.alerts.types import AlertType
from backtest.intelligence import service as svc
from backtest.intelligence.service import PortfolioIntelligence

PHASE = AlertType.SESSION_PHASE.value


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


def _intel(monkeypatch, phase):
    broker = RecordingBroker()
    intel = PortfolioIntelligence(manager=object(), broker=broker)
    monkeypatch.setattr(svc, "session_factor", lambda *a, **k: SimpleNamespace(status=phase))
    return intel, broker


def test_first_observation_is_a_silent_baseline(monkeypatch):
    intel, broker = _intel(monkeypatch, "opening_drive")
    intel._rule_session()
    assert broker.raised == []
    assert intel._last_session_phase == "opening_drive"


def test_phase_change_raises_one_alert(monkeypatch):
    intel, broker = _intel(monkeypatch, "opening_drive")
    intel._rule_session()
    monkeypatch.setattr(svc, "session_factor", lambda *a, **k: SimpleNamespace(status="midday_chop"))
    intel._rule_session()
    assert len(broker.raised) == 1
    assert broker.raised[0]["type"] == PHASE
    assert broker.raised[0]["data"]["phase"] == "midday_chop"
    assert broker.raised[0]["data"]["previous"] == "opening_drive"


def test_repeat_phase_does_not_re_raise(monkeypatch):
    intel, broker = _intel(monkeypatch, "opening_drive")
    intel._rule_session()
    monkeypatch.setattr(svc, "session_factor", lambda *a, **k: SimpleNamespace(status="midday_chop"))
    intel._rule_session()
    intel._rule_session()
    assert len(broker.raised) == 1