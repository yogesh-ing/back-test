"""PRD 1.1 — Portfolio Greeks dashboard: aggregation, scenarios, limits.

The anchor is a hand-checkable book: ₹5L equity, 150 units short ATM NIFTY
straddle, 7 DTE, IV 15%, r 6%. Black-Scholes by hand gives, per unit,
Γ = φ(d1)/(Sσ√T) = 0.000767, call Δ = 0.5262, vega = 13.78 ₹/pt, so the book's
convexity loss on a 2% move is ½·(2·150·Γ)·(500)² ≈ ₹28,745 — 5.7% of equity.
"""

from __future__ import annotations

import pytest

from backtest.monitoring import MonitorConfig, PortfolioGreeksAggregator
from backtest.monitoring.config import Threshold
from backtest.monitoring.greeks import money

from .helpers import equity_pos, inputs, option_leg, straddle


@pytest.fixture
def agg():
    return PortfolioGreeksAggregator(MonitorConfig())


def _spot_rows(agg):
    rows = agg.calculate(inputs(straddle()))["scenarios"]["spot"]
    return {row["spot_pct"]: row for row in rows}


def _alerts(report):
    return {a["key"]: a for a in report["alerts"]}


class TestLegGreeks:
    def test_matches_hand_black_scholes(self, agg):
        leg = agg.calculate(inputs(straddle()))["legs"][0]
        assert leg["gamma"] == pytest.approx(0.000767, rel=1e-3)
        assert leg["delta"] == pytest.approx(0.5262, rel=1e-3)
        assert leg["vega"] == pytest.approx(13.782, rel=1e-3)

    def test_equity_is_pure_delta(self, agg):
        r = agg.calculate(inputs([equity_pos(units=10, price=1000.0, symbol="RELIANCE")]))
        t = r["totals"]
        assert t["delta_1pct"] == pytest.approx(100.0)  # 10 sh × ₹1000 × 1%
        assert t["gamma_2pct_pnl"] == 0 and t["vega"] == 0 and t["theta_day"] == 0

    def test_short_leg_signs(self, agg):
        leg = agg.calculate(inputs([option_leg(side="SHORT", option_type="CE")]))["legs"][0]
        assert leg["delta_units"] < 0
        assert leg["position_vega"] < 0
        assert leg["position_theta_day"] > 0  # a seller collects decay


class TestAggregation:
    def test_short_straddle_totals(self, agg):
        t = agg.calculate(inputs(straddle()))["totals"]
        assert t["gamma_2pct_pnl"] == pytest.approx(-28_745, rel=0.01)
        assert t["gamma_2pct_pnl"] == pytest.approx(4 * t["gamma_1pct_pnl"], rel=1e-6)
        assert t["vega"] == pytest.approx(-4_134.6, rel=0.01)
        assert t["theta_day"] > 0
        # ATM straddle is near delta-neutral: (0.526 - 0.474) × 150 × ₹250.
        assert t["delta_1pct"] == pytest.approx(-1_966, rel=0.02)

    def test_breakdown_by_strategy_sums_to_total(self, agg):
        legs = straddle("A") + [equity_pos(strategy="B", units=20)]
        r = agg.calculate(inputs(legs))
        rows = {row["strategy_name"]: row for row in r["by_strategy"]}
        assert set(rows) == {"A", "B"}
        assert sum(row["delta_1pct"] for row in rows.values()) == pytest.approx(
            r["totals"]["delta_1pct"], abs=0.05)
        assert rows["A"]["short_legs"] == 2 and rows["A"]["long_legs"] == 0
        assert rows["B"]["delta_units"] == {"NIFTY": 20.0}

    def test_by_underlying_keeps_names_apart(self, agg):
        legs = [equity_pos(symbol="NIFTY", units=1), equity_pos(symbol="RELIANCE", units=5,
                                                                price=3000.0)]
        r = agg.calculate(inputs(legs))
        assert {u["underlying"] for u in r["by_underlying"]} == {"NIFTY", "RELIANCE"}

    def test_empty_book(self, agg):
        r = agg.calculate(inputs([]))
        assert r["position_count"] == 0 and r["alerts"] == []
        assert r["totals"]["delta_1pct"] == 0


class TestScenarios:
    def test_short_gamma_loses_both_ways(self, agg):
        spot = _spot_rows(agg)
        assert spot[-2.0]["pnl"] < 0 and spot[2.0]["pnl"] < 0

    def test_full_reval_close_to_delta_gamma_for_small_moves(self, agg):
        spot = _spot_rows(agg)
        small = spot[0.5]
        assert small["pnl"] == pytest.approx(small["delta_gamma_pnl"], rel=0.05)

    def test_iv_and_time_decay(self, agg):
        sc = agg.calculate(inputs(straddle()))["scenarios"]
        iv = {row["iv_pts"]: row for row in sc["iv"]}
        assert iv[5.0]["pnl"] < 0 < iv[-5.0]["pnl"]  # short vega
        assert sc["time_decay_1d"]["pnl"] > 0

    def test_worst_is_the_minimum(self, agg):
        sc = agg.calculate(inputs(straddle()))["scenarios"]
        every = sc["spot"] + sc["iv"] + sc["stress"]
        assert sc["worst"]["pnl"] == pytest.approx(min(r["pnl"] for r in every))


class TestLimits:
    def test_prd_book_trips_gamma_critical_and_vega_warning(self, agg):
        a = _alerts(agg.calculate(inputs(straddle())))
        assert a["greeks:gamma:portfolio"]["severity"] == "critical"  # 5.7% ≥ 1%
        assert a["greeks:vega:portfolio"]["severity"] == "warning"  # 0.83% ∈ [0.75, 1.5)
        assert "greeks:delta:portfolio" not in a  # 0.39% < 1%
        assert "Close or hedge" in a["greeks:gamma:portfolio"]["recommendation"]

    def test_long_gamma_never_alerts_on_gamma(self, agg):
        legs = straddle(side="LONG")
        a = _alerts(agg.calculate(inputs(legs)))
        assert "greeks:gamma:portfolio" not in a

    def test_delta_alert_suggests_futures_hedge(self, agg):
        # 4 lots long calls deep ITM ≈ 300 Δ units ≈ ₹75k per 1% on ₹5L = 15%.
        legs = [option_leg(side="LONG", strike=23000, units=300, structure_type="long_call")]
        a = _alerts(agg.calculate(inputs(legs)))
        d = a["greeks:delta:portfolio"]
        assert d["severity"] == "critical" and "LONG" in d["title"]
        assert "Sell ~" in d["recommendation"] and "futures lots" in d["recommendation"]

    def test_limits_are_configurable(self):
        cfg = MonitorConfig()
        cfg.greeks.gamma_2pct = Threshold(0.5, 0.9)  # absurdly loose
        a = _alerts(PortfolioGreeksAggregator(cfg).calculate(inputs(straddle())))
        assert "greeks:gamma:portfolio" not in a

    def test_worst_scenario_critical_when_beyond_breaker_headroom(self, agg):
        # Tiny position → worst loss far below the 3% ratio, but only ₹100 of
        # daily-loss headroom is left: the breaker would fire mid-move.
        legs = straddle(units=5)
        r = agg.calculate(inputs(legs, daily_pnl=-9_900, daily_loss_limit=10_000))
        w = _alerts(r)["scenario:worst:portfolio"]
        assert w["severity"] == "critical"
        assert "breaker" in w["message"]

    def test_currency_symbol_flows_into_text(self):
        cfg = MonitorConfig(currency_symbol="$")
        a = _alerts(PortfolioGreeksAggregator(cfg).calculate(inputs(straddle())))
        assert "$" in a["greeks:gamma:portfolio"]["message"]
        assert "₹" not in a["greeks:gamma:portfolio"]["message"]
        assert money(-1234.4, "$") == "\u2212$1,234"  # typographic minus
