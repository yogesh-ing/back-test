"""Portfolio Intelligence analytics — pure computation units.

Greeks aggregation, concentration, correlation, regime detection, market
activity and config loading. No manager, no feed, no database.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

import pytest

from backtest.intelligence.concentration import ConcentrationMonitor
from backtest.intelligence.config import IntelligenceConfig, load_config
from backtest.intelligence.correlation import CorrelationCalculator
from backtest.intelligence.greeks import ExposureLeg, PortfolioGreeksAggregator
from backtest.intelligence.market_activity import MarketActivityMonitor
from backtest.intelligence.regime import (
    HIGH,
    LOW,
    MODERATE,
    UNKNOWN,
    MarketRegimeDetector,
    regime_fit,
)

TODAY = date(2026, 9, 16)
EXPIRY = date(2026, 9, 24)  # 8 days
SPOT = 25000.0


def opt(sign, opt_type, strike, units=75, source="r1", label="Runner 1", strategy="s",
        underlying="NIFTY", iv=0.12, key="struct-1", spot=SPOT, expiry=EXPIRY):
    return ExposureLeg(
        source_id=source, source_label=label, strategy=strategy, mode="paper",
        position_key=key, kind="option", underlying=underlying, units=units, sign=sign,
        spot=spot, option_type=opt_type, strike=strike, expiry=expiry, iv=iv,
        reference_date=TODAY,
    )


def eq(sign, units, price, symbol="RELIANCE", source="r9", label="Equity"):
    return ExposureLeg(
        source_id=source, source_label=label, strategy="sma", mode="paper",
        position_key=symbol, kind="equity", underlying=symbol, units=units, sign=sign,
        spot=price, current_price=price,
    )


@pytest.fixture()
def agg():
    return PortfolioGreeksAggregator(risk_free_rate=0.06)


# ---------------------------------------------------------------------------
# Greeks aggregation
# ---------------------------------------------------------------------------


def test_empty_portfolio_is_flat(agg):
    g = agg.calculate([], [])
    assert g["net_delta"] == g["net_gamma"] == g["net_vega"] == g["net_theta"] == 0
    assert g["positions"] == 0 and g["breakdown_by_strategy"] == []


def test_long_atm_call_delta_about_half_units(agg):
    g = agg.calculate([opt(+1, "CE", 25000)], [])
    assert 0.45 * 75 < g["net_delta"] < 0.62 * 75
    assert g["net_gamma"] > 0 and g["net_vega"] > 0 and g["net_theta"] < 0
    assert g["bias"]["gamma"] == "long γ"


def test_short_strangle_is_short_gamma_long_theta(agg):
    legs = [opt(-1, "CE", 25500), opt(-1, "PE", 24500)]
    g = agg.calculate(legs, [])
    assert g["net_gamma"] < 0
    assert g["net_vega"] < 0
    assert g["net_theta"] > 0
    assert abs(g["net_delta"]) < 0.15 * 75  # roughly delta-neutral
    assert g["positions"] == 1  # one structure, two legs
    assert g["legs"] == 2 and g["legs_priced"] == 2


def test_gamma_is_per_one_percent_move(agg):
    """net_gamma = Σ Γ·units·S·1% — i.e. the delta change for a 1% move."""
    leg = opt(+1, "CE", 25000)
    g = agg.calculate([leg], [])
    lg = agg.leg_greeks(leg)
    assert g["net_gamma"] == pytest.approx(lg.gamma_pt * SPOT * 0.01, rel=1e-3)
    # Check it against a finite difference of delta.
    up = agg.leg_greeks(opt(+1, "CE", 25000, spot=SPOT * 1.005)).delta
    dn = agg.leg_greeks(opt(+1, "CE", 25000, spot=SPOT * 0.995)).delta
    assert (up - dn) == pytest.approx(g["net_gamma"], rel=0.05)


def test_equity_legs_contribute_delta_only(agg):
    g = agg.calculate([eq(+1, 100, 2500.0), eq(-1, 40, 2500.0, symbol="TCS")], [])
    assert g["net_delta"] == pytest.approx(60)
    assert g["net_gamma"] == 0 and g["net_vega"] == 0


def test_breakdown_by_strategy_and_shares(agg):
    legs = [
        opt(-1, "CE", 25500, source="a", label="A"), opt(-1, "PE", 24500, source="a", label="A"),
        opt(-1, "CE", 25300, source="b", label="B", key="s2"),
    ]
    g = agg.calculate(legs, [])
    rows = {r["label"]: r for r in g["breakdown_by_strategy"]}
    assert set(rows) == {"A", "B"}
    assert rows["A"]["gamma"] + rows["B"]["gamma"] == pytest.approx(g["net_gamma"], abs=0.05)
    assert sum(r["gamma_share"] for r in rows.values()) == pytest.approx(1.0, abs=0.01)
    assert g["breakdown_by_strategy"][0]["gamma"] <= g["breakdown_by_strategy"][1]["gamma"]


def test_scenarios_full_revaluation_signs(agg):
    scenarios = [
        {"key": "down", "label": "-2%", "spot_pct": -0.02, "vol_pts": 0},
        {"key": "up", "label": "+2%", "spot_pct": 0.02, "vol_pts": 0},
        {"key": "vol", "label": "IV+5", "spot_pct": 0, "vol_pts": 5},
    ]
    short = agg.calculate([opt(-1, "CE", 25500), opt(-1, "PE", 24500)], scenarios)
    assert short["scenarios"]["down"] < 0 and short["scenarios"]["up"] < 0  # short gamma
    assert short["scenarios"]["vol"] < 0  # short vega
    long_call = agg.calculate([opt(+1, "CE", 25000)], scenarios)
    assert long_call["scenarios"]["up"] > 0 > long_call["scenarios"]["down"]
    assert [r["label"] for r in short["scenario_list"]] == ["-2%", "+2%", "IV+5"]


def test_unpriceable_legs_are_reported_not_invented(agg):
    bad = opt(-1, "CE", 25500)
    bad.spot = None
    g = agg.calculate([bad, opt(-1, "PE", 24500)], [])
    assert g["legs_priced"] == 1
    assert len(g["missing_greeks"]) == 1
    assert any("unavailable" in w for w in g["warnings"])


def test_unknown_iv_is_assumed_and_flagged(agg):
    g = agg.calculate([opt(+1, "CE", 25000, iv=None)], [])
    assert any("IV unknown" in w for w in g["warnings"])


def test_expiry_day_keeps_gamma(agg):
    """At T=0 BS gamma collapses — expiry day is priced with half a day left."""
    leg = opt(-1, "CE", 25000, expiry=TODAY)
    g = agg.calculate([leg], [])
    assert g["net_gamma"] < 0
    expired = opt(-1, "CE", 25000, expiry=TODAY - timedelta(days=1))
    assert agg.leg_greeks(expired) is None


def test_performance_50_positions_under_500ms(agg):
    legs = []
    for i in range(50):
        strike = 24000 + (i % 20) * 100
        legs.append(opt(-1, "CE", strike + 500, source=f"r{i % 10}", key=f"s{i}"))
        legs.append(opt(-1, "PE", strike - 500, source=f"r{i % 10}", key=f"s{i}"))
    scenarios = IntelligenceConfig().scenarios
    t0 = time.perf_counter()
    g = agg.calculate(legs, scenarios)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert g["positions"] == 50 and g["legs_priced"] == 100
    assert elapsed_ms < 500, f"Greeks for 50 positions took {elapsed_ms:.0f} ms"


# ---------------------------------------------------------------------------
# Concentration
# ---------------------------------------------------------------------------


def test_concentration_by_underlying_flags_above_threshold():
    mon = ConcentrationMonitor(max_pct=0.60, min_positions=2, strike_cluster_min=3)
    legs = [
        opt(-1, "CE", 25500, key="a"), opt(-1, "PE", 24500, key="a"),
        opt(-1, "CE", 25600, key="b", source="r2", label="R2"),
        opt(-1, "CE", 52000, underlying="BANKNIFTY", spot=52000, units=30, key="c"),
    ]
    c = mon.calculate(legs)
    assert c["by_underlying"]["NIFTY"]["high"] is True
    assert c["by_underlying"]["NIFTY"]["pct"] > 60
    alerts = [a for a in c["alerts"] if a["type"] == "high_concentration"]
    assert [a["underlying"] for a in alerts] == ["NIFTY"]
    assert sum(r["pct"] for r in c["by_underlying"].values()) == pytest.approx(100, abs=0.1)


def test_single_position_is_not_a_concentration_alert():
    mon = ConcentrationMonitor(max_pct=0.60, min_positions=2)
    c = mon.calculate([opt(-1, "CE", 25500), opt(-1, "PE", 24500)])
    assert c["by_underlying"]["NIFTY"]["pct"] == 100
    assert not [a for a in c["alerts"] if a["type"] == "high_concentration"]


def test_strike_clustering_needs_three_positions_at_one_strike():
    mon = ConcentrationMonitor(strike_cluster_min=3)
    two = [opt(-1, "PE", 23500, key=f"k{i}", source=f"r{i}") for i in range(2)]
    assert not [a for a in mon.calculate(two)["alerts"] if a["type"] == "strike_clustering"]
    three = two + [opt(-1, "PE", 23500, key="k3", source="r3")]
    alerts = [a for a in mon.calculate(three)["alerts"] if a["type"] == "strike_clustering"]
    assert len(alerts) == 1 and alerts[0]["positions"] == 3 and alerts[0]["strike"] == 23500


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------


def test_correlation_detects_co_moving_runners_and_needs_min_samples():
    calc = CorrelationCalculator(window=240, min_samples=30, warning=0.8, cache_s=0)
    groups = {k: {"label": k.upper(), "members": [k]} for k in ("a", "b", "c")}
    eq_a = eq_b = eq_c = 100000.0
    for i in range(10):
        calc.record({"a": eq_a, "b": eq_b, "c": eq_c})
        eq_a += (i % 3) - 1
    early = calc.matrix(groups, use_cache=False)
    assert early["values"][0][1] is None  # not enough aligned samples yet
    import random

    rng = random.Random(7)
    for _ in range(60):
        shock = rng.gauss(0, 100)
        eq_a += shock
        eq_b += shock * 1.5 + rng.gauss(0, 5)
        eq_c += rng.gauss(0, 100)
        calc.record({"a": eq_a, "b": eq_b, "c": eq_c})
    m = calc.matrix(groups, use_cache=False)
    ia, ib, ic = m["ids"].index("a"), m["ids"].index("b"), m["ids"].index("c")
    assert m["values"][ia][ib] > 0.9
    assert abs(m["values"][ia][ic]) < 0.5
    assert m["values"][ia][ia] == 1.0
    pairs = {(x["id_a"], x["id_b"]) for x in m["alerts"]}
    assert pairs in ({("a", "b")}, {("b", "a")})


def test_correlation_cache_and_forget():
    calc = CorrelationCalculator(min_samples=2, cache_s=300)
    groups = {"a": {"label": "A", "members": ["a"]}, "b": {"label": "B", "members": ["b"]}}
    for i in range(5):
        calc.record({"a": float(i), "b": float(i * 2)})
    first = calc.matrix(groups)
    assert calc.matrix(groups)["cached"] is True
    assert calc.matrix(groups, use_cache=False)["cached"] is False
    calc.forget(["a"])
    assert calc.sample_count("b") == 0
    assert first["cached"] is False


# ---------------------------------------------------------------------------
# Regime
# ---------------------------------------------------------------------------


def test_regime_bands_and_hysteresis():
    d = MarketRegimeDetector(low_max=15, high_min=22, hysteresis=0.5)
    assert d.classify(12) == LOW
    assert d.classify(18) == MODERATE
    assert d.classify(25) == HIGH
    assert d.classify(None) == UNKNOWN
    # 22.2 is HIGH raw, but from MODERATE it must clear 22.5 to flip.
    assert d.classify(22.2, previous=MODERATE) == MODERATE
    assert d.classify(22.6, previous=MODERATE) == HIGH
    assert d.classify(21.8, previous=HIGH) == HIGH
    assert d.classify(21.4, previous=HIGH) == MODERATE


def test_regime_transition_between_known_regimes_only():
    d = MarketRegimeDetector()
    assert d.evaluate() is None  # unknown → unknown
    d.ingest_vix(13.0)
    assert d.evaluate() is None  # unknown → low: first reading, not a "change"
    assert d.snapshot()["regime"] == LOW
    d.ingest_vix(24.0, source="manual")
    t = d.evaluate()
    assert t["old_regime"] == LOW and t["new_regime"] == HIGH and t["vix"] == 24.0
    snap = d.snapshot()
    assert snap["transitioning"] is True and snap["source"] == "manual" and not snap["is_proxy"]
    assert d.evaluate() is None  # no repeated transitions


def test_vix_must_be_positive():
    with pytest.raises(ValueError):
        MarketRegimeDetector().ingest_vix(0)


def test_vix_symbol_bars_feed_the_regime():
    d = MarketRegimeDetector(vix_symbols=["INDIAVIX"])
    d.observe_bar("INDIAVIX", {"close": 17.5, "ts": "2026-09-16T09:15:00"})
    d.evaluate()
    snap = d.snapshot()
    assert snap["regime"] == MODERATE and snap["source"] == "feed:INDIAVIX"


def test_realized_vol_proxy_from_benchmark_bars_is_labelled():
    d = MarketRegimeDetector(benchmark="NIFTY", realized_window=60)
    base = datetime(2026, 9, 16, 9, 15, tzinfo=timezone.utc)
    px = 25000.0
    for i in range(61):
        px *= 1.0 + (0.0004 if i % 2 else -0.0004)
        d.observe_bar("NIFTY", {"close": px, "ts": (base + timedelta(minutes=i)).isoformat()})
    rv = d.realized_vol()
    assert rv is not None and 5 < rv < 40  # ±0.04% per minute ≈ 10 vol points
    d.evaluate()
    snap = d.snapshot()
    assert snap["is_proxy"] is True
    assert snap["source"] == "realized_vol_proxy:NIFTY"


def test_explicit_vix_beats_proxy_until_stale():
    d = MarketRegimeDetector(vix_stale_s=900)
    d.ingest_vix(14.0)
    now = datetime.now(timezone.utc)
    assert d.snapshot(now=now)["source"] == "manual"
    assert d.snapshot(now=now + timedelta(seconds=901))["source"] == "none"


def test_regime_fit():
    assert regime_fit(None, 20)["status"] == "any"
    assert regime_fit((10, 15), None)["status"] == "unknown"
    assert regime_fit((10, 15), 12)["status"] == "favorable"
    assert regime_fit((10, 15), 24)["status"] == "unfavorable"


# ---------------------------------------------------------------------------
# Market activity
# ---------------------------------------------------------------------------


def _row(strike, oi, bid=None, ask=None, opt_type="CE"):
    return {"strike": strike, "option_type": opt_type, "oi": oi, "bid": bid, "ask": ask}


def test_oi_spike_after_min_history():
    mon = MarketActivityMonitor(oi_multiplier=3.0, oi_min_history=3, spread_multiplier=2.0)
    oi = 100000
    for step in (1000, 1200, 900, 1100):
        oi += step
        anomalies, _ = mon.ingest("NIFTY", [_row(23500, oi)])
        assert anomalies == []
    oi += 8000  # ~7.8× the average change
    anomalies, _ = mon.ingest("NIFTY", [_row(23500, oi)])
    assert len(anomalies) == 1
    assert anomalies[0]["strike"] == 23500 and anomalies[0]["multiplier"] > 3
    act = mon.activity("NIFTY")
    assert act["has_oi_data"] is True and len(act["anomalies"]) == 1


def test_spread_dry_up():
    mon = MarketActivityMonitor(oi_min_history=3, spread_multiplier=2.0)
    for _ in range(4):
        _, dry = mon.ingest("NIFTY", [_row(23500, 0, bid=100.0, ask=101.0)])
        assert dry == []
    _, dry = mon.ingest("NIFTY", [_row(23500, 0, bid=100.0, ask=103.5)])
    assert len(dry) == 1 and dry[0]["multiplier"] >= 2


def test_no_oi_data_is_explained():
    mon = MarketActivityMonitor()
    mon.ingest("NIFTY", [_row(23500, 0)])
    act = mon.activity("NIFTY")
    assert act["has_oi_data"] is False and act["note"]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_config_yaml_env_and_overrides(tmp_path):
    cfg_file = tmp_path / "pi.yaml"
    cfg_file.write_text("gamma_critical: -500\nfeed_stale_s: 90\nbogus_key: 1\n")
    cfg = load_config(
        cfg_file, env={"PI_FEED_STALE_S": "45"}, overrides={"delta_warning_abs": 1000}
    )
    assert cfg.gamma_critical == -500
    assert cfg.feed_stale_s == 45
    assert cfg.delta_warning_abs == 1000


def test_repo_config_file_loads_with_prd_defaults():
    cfg = load_config()
    assert cfg.delta_warning_abs == 800
    assert cfg.concentration_max_pct == 0.60
    assert cfg.strike_cluster_min == 3
    assert cfg.correlation_warning == 0.8
    assert cfg.feed_stale_s == 120
    assert cfg.event_alert_ttl_s == 3600
    assert {s["key"] for s in cfg.scenarios} >= {"spot_down_2pct", "spot_up_2pct", "vol_up_5pts"}
