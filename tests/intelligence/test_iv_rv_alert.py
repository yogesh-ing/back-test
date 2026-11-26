"""IV_RV_DISLOCATION emission (``_rule_iv_rv``) and the ATM-IV proxy."""

from __future__ import annotations

from types import SimpleNamespace

from backtest.alerts.types import AlertType
from backtest.intelligence.service import PortfolioIntelligence

DISLOC = AlertType.IV_RV_DISLOCATION.value


class RecordingBroker:
    event_ttl_s = 3600.0
    ignore_ttl_s = 3600.0
    renotify_cooldown_s = 60.0
    auto_dismiss_exempt = set()

    def __init__(self):
        self.raised = []
        self.resolved = []

    def raise_alert(self, alert_type, severity, message, data=None, subject="", **_):
        self.raised.append({"type": str(alert_type), "data": data or {}})
        return None, True

    def resolve_missing(self, alert_type, active, reason=""):
        self.resolved.append((str(alert_type), reason))


def _intel(iv, realized):
    broker = RecordingBroker()
    intel = PortfolioIntelligence(manager=object(), broker=broker)
    intel._atm_iv_proxy = lambda legs: iv
    intel.regime.realized_vol = lambda: realized
    return intel, broker


def test_atm_iv_proxy_is_quantity_weighted():
    legs = [
        SimpleNamespace(iv=20.0, units=1),
        SimpleNamespace(iv=10.0, units=3),
        SimpleNamespace(iv=None, units=5),
    ]
    assert PortfolioIntelligence._atm_iv_proxy(legs) == 12.5


def test_atm_iv_proxy_none_without_usable_legs():
    assert PortfolioIntelligence._atm_iv_proxy([]) is None
    assert PortfolioIntelligence._atm_iv_proxy([SimpleNamespace(iv=None, units=1)]) is None


def test_first_observation_is_a_silent_baseline():
    intel, broker = _intel(30.0, 20.0)  # sell_vol
    intel._rule_iv_rv([])
    assert broker.raised == []


def test_flip_raises_one_alert():
    intel, broker = _intel(30.0, 20.0)  # sell_vol baseline
    intel._rule_iv_rv([])
    intel._atm_iv_proxy = lambda legs: 10.0  # buy_vol
    intel._rule_iv_rv([])
    assert len(broker.raised) == 1
    assert broker.raised[0]["type"] == DISLOC
    assert broker.raised[0]["data"]["state"] == "buy_vol"


def test_unknown_never_alerts():
    intel, broker = _intel(None, 20.0)
    intel._rule_iv_rv([])
    intel._atm_iv_proxy = lambda legs: 30.0
    intel._rule_iv_rv([])
    assert broker.raised == []