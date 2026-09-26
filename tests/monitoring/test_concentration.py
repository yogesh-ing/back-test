"""PRD 1.2a — Concentration: underlying share, correlated groups, stacking,
strike clusters — and the gating that stops one strategy's own design from
reading as *hidden* concentration."""

from __future__ import annotations

import pytest

from backtest.monitoring import ConcentrationMonitor, MonitorConfig

from .helpers import equity_pos, inputs, option_leg


@pytest.fixture
def mon():
    return ConcentrationMonitor(MonitorConfig())


def _alerts(report):
    return {a["key"]: a for a in report["alerts"]}


def test_shares_sum_to_one_and_hhi(mon):
    legs = [equity_pos(strategy="A", symbol="NIFTY", units=8, price=25000),  # ₹2L
            equity_pos(strategy="B", symbol="RELIANCE", units=100, price=2000)]  # ₹2L
    r = mon.calculate(inputs(legs))
    pct = {u["underlying"]: u["pct"] for u in r["by_underlying"]}
    assert pct == pytest.approx({"NIFTY": 0.5, "RELIANCE": 0.5})
    assert r["herfindahl"] == pytest.approx(0.5)
    assert r["effective_underlyings"] == pytest.approx(2.0)


def test_underlying_critical_above_70pct(mon):
    legs = [equity_pos(strategy="A", symbol="NIFTY", units=16, price=25000),  # ₹4L
            equity_pos(strategy="B", symbol="RELIANCE", units=50, price=2000)]  # ₹1L
    a = _alerts(mon.calculate(inputs(legs)))
    assert a["concentration:underlying:NIFTY"]["severity"] == "critical"  # 80%
    assert "concentration:underlying:RELIANCE" not in a


def test_underlying_warning_between_50_and_70(mon):
    legs = [equity_pos(strategy="A", symbol="NIFTY", units=12, price=25000),  # ₹3L
            equity_pos(strategy="B", symbol="RELIANCE", units=100, price=2000)]  # ₹2L
    a = _alerts(mon.calculate(inputs(legs)))
    assert a["concentration:underlying:NIFTY"]["severity"] == "warning"  # 60%


def test_correlated_group_rolls_up_indices(mon):
    legs = [equity_pos(strategy="A", symbol="NIFTY", units=8, price=25000),  # ₹2L
            equity_pos(strategy="B", symbol="BANKNIFTY", units=4, price=50000),  # ₹2L
            equity_pos(strategy="C", symbol="RELIANCE", units=25, price=2000)]  # ₹0.5L
    r = mon.calculate(inputs(legs))
    groups = {g["group"]: g for g in r["by_group"]}
    assert groups["INDIA_INDEX"]["pct"] == pytest.approx(4 / 4.5, rel=1e-3)
    assert _alerts(r)["concentration:group:INDIA_INDEX"]["severity"] == "critical"  # 88.9%


def test_single_strategy_single_structure_is_not_hidden_concentration(mon):
    # One bull call spread = 100% NIFTY by construction; that is its design.
    legs = [option_leg(strategy="Spread", side="LONG", strike=25000, structure_id="s1"),
            option_leg(strategy="Spread", side="SHORT", strike=25100, structure_id="s1")]
    r = mon.calculate(inputs(legs))
    assert not any(k.startswith("concentration:underlying") for k in _alerts(r))
    assert r["exposure_count"] == 1 and r["strategy_count"] == 1


def test_stacking_three_strategies_on_one_name(mon):
    legs = [equity_pos(strategy=s, symbol="NIFTY", units=1) for s in ("A", "B", "C")]
    a = _alerts(mon.calculate(inputs(legs)))
    stack = a["concentration:stacking:NIFTY"]
    assert "3 strategies" in stack["title"]


def test_strike_cluster_liquidity_warning(mon):
    legs = [option_leg(strategy=s, strike=25000, structure_id=f"{s}-1") for s in ("A", "B", "C")]
    r = mon.calculate(inputs(legs))
    cluster = r["strike_clusters"][0]
    assert cluster["positions"] == 3 and cluster["strategies"] == ["A", "B", "C"]
    key = f"concentration:strike:{cluster['key']}"
    assert _alerts(r)[key]["severity"] == "warning"


def test_two_legs_at_a_strike_is_not_a_cluster(mon):
    legs = [option_leg(strategy=s, strike=25000, structure_id=f"{s}-1") for s in ("A", "B")]
    keys = _alerts(mon.calculate(inputs(legs)))
    assert not any(k.startswith("concentration:strike") for k in keys)


def test_option_notional_is_underlying_not_premium(mon):
    leg = option_leg(strategy="A", units=75, spot=25000, premium=100)
    r = mon.calculate(inputs([leg, equity_pos(strategy="B", symbol="RELIANCE")]))
    nifty = next(u for u in r["by_underlying"] if u["underlying"] == "NIFTY")
    assert nifty["notional"] == pytest.approx(75 * 25000)
