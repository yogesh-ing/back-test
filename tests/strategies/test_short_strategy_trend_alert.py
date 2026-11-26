"""Short-premium strategies react to TREND_REGIME_CHANGE.

A confirmed trend (up/down) pauses new entries; a return to sideways resumes
them. The reaction is per-reason, so a gamma pause is not cleared by a trend
resume.
"""

from __future__ import annotations

import pytest

from backtest.alerts.types import AlertType
from backtest.strategy.registry import _REGISTRY
from backtest.strategy.short_strategy_base import ShortStrategyBase

TREND = AlertType.TREND_REGIME_CHANGE.value
SESSION = AlertType.SESSION_PHASE.value
IVRV = AlertType.IV_RV_DISLOCATION.value
GAMMA = AlertType.PORTFOLIO_GAMMA_CRITICAL.value

NAME = "trend_probe_short"


@pytest.fixture()
def strategy():
    # Defined inside the fixture so the __init_subclass__ auto-registration is
    # scoped to this test and popped on teardown (no global registry leak).
    class _ProbeShort(ShortStrategyBase):
        name = NAME
        description = "trend alert probe"
        version = "1"
        author = "tests"
        params = {}
        opening_structure = {
            "structure": "short_straddle",
            "strike_selection": "atm",
            "expiry_policy": "nearest",
        }

    s = _ProbeShort()
    try:
        yield s
    finally:
        _REGISTRY.pop(NAME, None)


def test_subscribes_to_trend_regime_change(strategy):
    assert TREND in strategy.subscribed_alerts


@pytest.mark.parametrize("regime", ["up", "down"])
def test_trend_pauses_new_entries(strategy, regime):
    assert strategy.on_alert(TREND, {"regime": regime}) == "paused"
    assert strategy.pause_new_entries is True


def test_sideways_resumes(strategy):
    strategy.on_alert(TREND, {"regime": "up"})
    assert strategy.on_alert(TREND, {"regime": "sideways"}) == "resumed"
    assert strategy.pause_new_entries is False


def test_trend_resume_does_not_clear_a_gamma_pause(strategy):
    strategy.on_alert(GAMMA, {"self_contribution": {"share": 0.1}})
    assert strategy.pause_new_entries is True
    strategy.on_alert(TREND, {"regime": "sideways"})
    assert strategy.pause_new_entries is True


def test_subscribes_to_session_phase(strategy):
    assert SESSION in strategy.subscribed_alerts


@pytest.mark.parametrize("phase", ["midday_chop", "closing_squareoff"])
def test_session_pauses_in_chop_and_squareoff(strategy, phase):
    assert strategy.on_alert(SESSION, {"phase": phase}) == "paused"
    assert strategy.pause_new_entries is True


def test_session_resumes_outside_chop(strategy):
    strategy.on_alert(SESSION, {"phase": "midday_chop"})
    assert strategy.on_alert(SESSION, {"phase": "morning_trend"}) == "resumed"
    assert strategy.pause_new_entries is False


def test_subscribes_to_iv_rv_dislocation(strategy):
    assert IVRV in strategy.subscribed_alerts


def test_buy_vol_pauses_and_sell_vol_resumes(strategy):
    assert strategy.on_alert(IVRV, {"state": "buy_vol"}) == "paused"
    assert strategy.pause_new_entries is True
    assert strategy.on_alert(IVRV, {"state": "sell_vol"}) == "resumed"
    assert strategy.pause_new_entries is False