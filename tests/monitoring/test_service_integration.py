"""PortfolioMonitor against a real PortfolioManager: collector, tick sampling,
sweep cadence and failure budget, audit trail."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from backtest.forward.paper_runner import RunnerConfig
from backtest.forward.portfolio_manager import reset_portfolio_manager
from backtest.monitoring import MonitorConfig, PortfolioMonitor
from backtest.strategy.intent import Direction, MarketView

BASE = datetime(2026, 9, 16, 9, 15)


def _equity(mgr, name, strat, sym="NIFTY"):
    return mgr.add_runner(RunnerConfig(
        name=name, strategy_name=strat, allocated_capital=100_000, symbols=[sym],
        target_type="SINGLE_SYMBOL", mode="paper"), start=True)


def _bull_spread(mgr, spot=25000.0, feed_bar=True):
    iid = mgr.add_runner(RunnerConfig(
        name="Bull spread", strategy_name="rsi_reversion", allocated_capital=100_000,
        symbols=["NIFTY"], target_type="SINGLE_SYMBOL", mode="paper",
        instrument={"type": "option",
                    "expression": {"type": "bull_call_spread", "strike_selection": "atm",
                                   "quantity": 1}}), start=False)
    bridge = mgr.get_runner(iid).options_bridge
    bridge.on_bar("NIFTY", spot, ts="2026-09-16T09:15:00")
    bridge.on_market_view(MarketView(direction=Direction.BULLISH, confidence=0.8,
                                     underlying="NIFTY", spot_price=Decimal(str(spot))), "t")
    assert bridge._has_open_structure()
    if not feed_bar:
        bridge.last_spot = None  # as if the bridge never priced a bar
    return iid


def _ticks(mgr, n, start=1):
    # Advancing bar timestamps: runners de-dup a bar whose ts ≤ the last one.
    for i in range(start, start + n):
        mgr.tick(ts=BASE + timedelta(minutes=i))


@pytest.fixture
def mgr():
    m = reset_portfolio_manager(auto_start_feed=False)
    yield m
    m.shutdown()


def test_option_runner_greeks_and_json_safety(mgr):
    _bull_spread(mgr)
    snap = PortfolioMonitor(mgr, config=MonitorConfig()).evaluate()
    json.dumps(snap)  # the API returns this verbatim
    legs = [leg for leg in snap["greeks"]["legs"] if leg["instrument_type"] == "option"]
    assert len(legs) == 2 and {leg["side"] for leg in legs} == {"LONG", "SHORT"}
    row = next(r for r in snap["greeks"]["by_strategy"] if r["strategy_name"] == "Bull spread")
    assert row["delta_1pct"] > 0  # a bull call spread is long delta
    assert row["long_legs"] == 1 and row["short_legs"] == 1


def test_legs_share_one_spot_when_bridge_has_none(mgr):
    """Regression: spot fell back to each leg's OWN strike, so a bull call
    spread's legs priced at different spots and read delta-neutral/short."""
    _bull_spread(mgr, feed_bar=False)
    snap = PortfolioMonitor(mgr, config=MonitorConfig()).evaluate(sections=["greeks"])
    legs = [leg for leg in snap["greeks"]["legs"] if leg["instrument_type"] == "option"]
    assert len({leg["underlying_price"] for leg in legs}) == 1
    long_leg = next(leg for leg in legs if leg["side"] == "LONG")
    short_leg = next(leg for leg in legs if leg["side"] == "SHORT")
    assert long_leg["delta_units"] + short_leg["delta_units"] > 0


def test_spot_falls_back_to_other_runners_price(mgr):
    _equity(mgr, "B&H", "buy_and_hold")
    _ticks(mgr, 3)
    market = mgr.get_runner(next(iter(mgr._runners))).last_price["NIFTY"]
    _bull_spread(mgr, feed_bar=False)
    snap = PortfolioMonitor(mgr, config=MonitorConfig()).evaluate(sections=["greeks"])
    legs = [leg for leg in snap["greeks"]["legs"] if leg["instrument_type"] == "option"]
    assert all(leg["underlying_price"] == pytest.approx(market) for leg in legs)


def test_regime_fit_uses_open_legs(mgr):
    _bull_spread(mgr)
    _ticks(mgr, 5)
    snap = PortfolioMonitor(mgr, config=MonitorConfig()).evaluate(sections=["regime"])
    fit = next(f for f in snap["regime"]["strategy_fit"] if f["strategy_name"] == "Bull spread")
    # Mixed legs → the debit-spread profile from the structure name.
    assert "directional" in fit["profile"]["tags"]


def test_manager_sweeps_on_cadence_and_samples_every_tick(mgr):
    for name, strat in (("SMA", "sma_crossover"), ("RSI", "rsi_reversion"),
                        ("B&H", "buy_and_hold")):
        _equity(mgr, name, strat)
    mon = mgr.get_monitor()
    mon.config.sweep_every_ticks = 5
    _ticks(mgr, 12)
    assert len(mon._samples) == 12  # record_tick on every tick
    assert mon.last_sweep is not None  # swept at ticks 5 and 10


def test_sampled_correlation_is_the_primary_source(mgr):
    for name, strat in (("RSI", "rsi_reversion"), ("B&H", "buy_and_hold")):
        _equity(mgr, name, strat)
    mon = mgr.get_monitor()
    mon.config.correlation.min_observations = 5
    _ticks(mgr, 30)
    corr = mon.evaluate(sections=["correlation"])["correlation"]
    assert corr["series_source"] == "tick_samples"
    assert corr["observations"] >= 25


def test_sweep_failure_budget_disables_after_five(mgr, monkeypatch):
    _equity(mgr, "B&H", "buy_and_hold")
    mon = mgr.get_monitor()
    mon.config.sweep_every_ticks = 1
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(mon, "sweep", boom)
    _ticks(mgr, 10)  # the feed keeps ticking regardless
    assert calls["n"] == mgr.MONITOR_MAX_FAILURES
    assert mgr.tick_index >= 10


def test_sweep_disabled_with_zero_cadence(mgr):
    _equity(mgr, "B&H", "buy_and_hold")
    mon = mgr.get_monitor()
    mon.config.sweep_every_ticks = 0
    _ticks(mgr, 6)
    assert mon.last_sweep is None


def test_alert_transitions_reach_the_audit_log(mgr):
    for name, strat in (("A", "buy_and_hold"), ("B", "buy_and_hold"), ("C", "rsi_reversion")):
        _equity(mgr, name, strat)
    _ticks(mgr, 40)
    mgr.get_monitor().sweep()
    entries = mgr.get_audit_log(scope="monitor", limit=50)
    # Three runners all long NIFTY: 100% one underlying → concentration critical.
    assert any("MONITOR_RAISED" in str(e.get("action")) and "NIFTY" in str(e)
               for e in entries)


def test_paper_scope_excludes_nothing_live_and_rejects_nothing(mgr):
    _equity(mgr, "B&H", "buy_and_hold")
    _ticks(mgr, 3)
    mon = mgr.get_monitor()
    assert mon.evaluate(mode="paper")["mode"] == "paper"
    live = mon.evaluate(mode="live")
    assert live["summary"]["strategies"] == 0 and live["greeks"]["position_count"] == 0
