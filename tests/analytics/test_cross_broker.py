"""Cross-broker analytics service behaviour (PRD-003).

What these pin, in order of how badly a bug would hurt:

1. **Attribution** — a trade and an order land on the broker their RUNNER
   routes to, and a removed runner's orders are dropped rather than silently
   reassigned.
2. **Rate definitions** — fill/rejection rates are computed over RESOLVED
   orders, so they cannot swing just because orders are in flight, and a
   broker with no orders reports ``None``, never ``0%``.
3. **Significance honesty** — a thin sample says "not enough data"; the
   recommendation follows the SIGNIFICANT metrics, not the most metrics.
4. **The migration maths** — each leg (trade count, slippage, Sharpe) is
   isolated and every number traces back to an observed input.
"""

from __future__ import annotations

import pytest
from analytics.helpers import place_order, place_orders, seed_trades

from backtest.analytics.cross_broker import (
    MIGRATION_NOISE_PCT,
    PERIOD_DAYS,
    CrossBrokerAnalyticsService,
    _broker_display,
    _estimate_migration,
    _order_fill_time_sec,
    _order_slippage_bps,
    _scoring_weights,
)

SERVICE_KWARGS = {}


@pytest.fixture()
def two_brokers(venues):
    """mStock: 8.2 bps slippage / 95% fill / 2.3s · Dhan: 18.1 / 87% / 5.1s."""
    mstock = venues["add"]("M", "ema_pullback", "mstock", 120_000.0, "options_index")
    dhan = venues["add"]("D", "rsi_reversion", "dhan", 80_000.0, "equity_intraday")
    place_orders(
        mstock, 60, slip_bps=8.2, fill_rate_pct=95.0, reject_rate_pct=2.0, fill_delay_s=2.3
    )
    place_orders(dhan, 60, slip_bps=18.1, fill_rate_pct=87.0, reject_rate_pct=8.0, fill_delay_s=5.1)
    yield {**venues, "mstock": mstock, "dhan": dhan}


# ----------------------------------------------------------------------
# Summary
# ----------------------------------------------------------------------


def test_summary_is_empty_but_well_formed_with_no_runners(venues):
    data = CrossBrokerAnalyticsService().get_summary(period="30d")
    assert data["by_broker"] == []
    assert data["by_segment"] == []
    assert data["portfolio_total"]["total_trades"] == 0
    assert data["portfolio_total"]["sharpe_ratio"] == 0.0
    assert data["data_quality"]["available"] is True  # the ledger exists, it is empty


def test_summary_attributes_trades_and_orders_to_each_broker(two_brokers):
    seed_trades(two_brokers["mstock"], [1200, -400, 900] * 12)
    seed_trades(two_brokers["dhan"], [300, -600, 200] * 8)

    data = CrossBrokerAnalyticsService().get_summary(period="30d")
    by_broker = {row["broker"]: row for row in data["by_broker"]}
    assert set(by_broker) == {"mstock", "dhan"}
    assert by_broker["mstock"]["total_trades"] == 36
    assert by_broker["dhan"]["total_trades"] == 24
    assert by_broker["mstock"]["total_orders"] == 60
    assert by_broker["dhan"]["total_orders"] == 60
    assert by_broker["mstock"]["avg_slippage_bps"] == pytest.approx(8.2, abs=0.01)
    assert by_broker["dhan"]["avg_slippage_bps"] == pytest.approx(18.1, abs=0.01)
    assert by_broker["mstock"]["segments"] == ["options_index"]
    assert by_broker["dhan"]["segments"] == ["equity_intraday"]


def test_portfolio_total_is_the_sum_across_brokers(two_brokers):
    seed_trades(two_brokers["mstock"], [1000, -500, 750])
    seed_trades(two_brokers["dhan"], [200, -300])
    data = CrossBrokerAnalyticsService().get_summary(period="30d")
    total = data["portfolio_total"]
    assert total["total_trades"] == 5
    assert total["total_pnl"] == 1150.0
    assert total["broker_count"] == 2
    assert total["total_capital_deployed"] == 200_000.0
    assert total["gross_pnl"] == 1950.0


def test_pnl_pct_is_a_share_of_contribution_and_sums_to_100(two_brokers):
    seed_trades(two_brokers["mstock"], [1000] * 4)
    seed_trades(two_brokers["dhan"], [500] * 4)
    rows = CrossBrokerAnalyticsService().get_summary(period="30d")["by_broker"]
    assert sum(r["pnl_pct"] for r in rows) == pytest.approx(100.0, abs=0.01)
    shares = {r["broker"]: r["pnl_pct"] for r in rows}
    assert shares["mstock"] > shares["dhan"]


def test_pnl_pct_can_exceed_100_when_a_broker_loses_money(two_brokers):
    """A losing broker drags the denominator below the winner's P&L."""
    seed_trades(two_brokers["mstock"], [1000] * 4)
    seed_trades(two_brokers["dhan"], [-500] * 4)
    shares = {
        r["broker"]: r["pnl_pct"]
        for r in CrossBrokerAnalyticsService().get_summary(period="30d")["by_broker"]
    }
    assert shares["mstock"] > 100.0
    assert shares["dhan"] < 0.0
    assert shares["mstock"] + shares["dhan"] == pytest.approx(100.0, abs=0.01)


def test_segments_roll_up_independently_of_brokers(two_brokers):
    seed_trades(two_brokers["mstock"], [100] * 3)
    seed_trades(two_brokers["dhan"], [-100] * 3)
    segments = {
        row["segment"]: row
        for row in CrossBrokerAnalyticsService().get_summary(period="30d")["by_segment"]
    }
    assert segments["options_index"]["broker"] == "mstock"
    assert segments["equity_intraday"]["broker"] == "dhan"
    assert segments["options_index"]["net_pnl"] == 300.0
    assert segments["equity_intraday"]["net_pnl"] == -300.0


def test_rankings_cover_every_broker_even_without_execution_data(venues):
    venues["add"]("A", "ema_pullback", "mstock", 100_000.0)
    venues["add"]("B", "rsi_reversion", "dhan", 100_000.0)
    data = CrossBrokerAnalyticsService().get_summary(period="30d")
    for ranking in data["broker_rankings"].values():
        assert sorted(ranking) == ["dhan", "mstock"]


def test_insights_name_the_loser_when_slippage_diverges(two_brokers):
    seed_trades(two_brokers["mstock"], [100] * 3)
    seed_trades(two_brokers["dhan"], [100] * 3)
    data = CrossBrokerAnalyticsService().get_summary(period="30d")
    kinds = {i["kind"] for i in data["insights"]}
    assert "slippage_gap" in kinds
    slippage_insight = next(i for i in data["insights"] if i["kind"] == "slippage_gap")
    assert slippage_insight["brokers"] == ["dhan", "mstock"]  # worst first
    assert "18.1" in slippage_insight["message"]


def test_alerts_flag_a_bad_venue(two_brokers):
    seed_trades(two_brokers["dhan"], [100] * 3)
    alerts = CrossBrokerAnalyticsService().get_summary(period="30d")["alerts"]
    flagged = {a["broker"] for a in alerts}
    assert "dhan" in flagged
    assert all("mstock" not in a["message"] for a in alerts if a["broker"] == "dhan")


# ----------------------------------------------------------------------
# Execution quality
# ----------------------------------------------------------------------


def test_execution_rates_use_resolved_orders_as_the_denominator(two_brokers):
    quality = CrossBrokerAnalyticsService().get_execution_quality(broker="dhan", period="30d")[
        "execution_quality"
    ]
    assert quality["total_orders"] == 60
    assert quality["resolved_orders"] == 60
    assert quality["fill_rate_pct"] == pytest.approx(87.0, abs=0.5)
    assert quality["rejection_rate_pct"] == pytest.approx(8.33, abs=0.5)
    assert (
        quality["filled_orders"] + quality["rejected_orders"] + quality["cancelled_orders"]
        == quality["resolved_orders"]
    )


def test_working_orders_do_not_depress_the_fill_rate(two_brokers):
    """A broker with 20 fills and 80 in flight is not a 20% fill rate."""
    for _ in range(5):
        place_order(two_brokers["dhan"], status="PENDING", requested_price=100.0)
    quality = CrossBrokerAnalyticsService().get_execution_quality(broker="dhan", period="30d")[
        "execution_quality"
    ]
    assert quality["pending_orders"] == 5
    assert quality["resolved_orders"] == 60  # pending excluded
    assert quality["fill_rate_pct"] == pytest.approx(87.0, abs=0.5)
    assert any(
        "still working" in note
        for note in CrossBrokerAnalyticsService().get_execution_quality(
            broker="dhan", period="30d"
        )["notes"]
    )


def test_stale_working_orders_are_flagged(two_brokers):
    place_order(
        two_brokers["dhan"],
        status="PENDING",
        requested_price=100.0,
        created_ts="2026-01-01T10:00:00+00:00",
    )
    quality = CrossBrokerAnalyticsService().get_execution_quality(broker="dhan", period="all_time")[
        "execution_quality"
    ]
    assert quality["stale_orders"] >= 1
    assert quality["stale_order_pct"] == pytest.approx(100.0)


def test_broker_with_no_orders_reports_none_not_zero(venues):
    venues["add"]("A", "ema_pullback", "mstock", 100_000.0)
    payload = CrossBrokerAnalyticsService().get_execution_quality(broker="mstock", period="30d")
    quality = payload["execution_quality"]
    assert quality["total_orders"] == 0
    assert quality["fill_rate_pct"] is None
    assert quality["avg_slippage_bps"] is None
    assert any("undefined, not zero" in note for note in payload["notes"])


def test_slippage_distribution_buckets_sum_to_the_sample_count(two_brokers):
    payload = CrossBrokerAnalyticsService().get_execution_quality(broker="dhan", period="30d")
    buckets = payload["slippage_distribution"]
    assert [b["bucket"] for b in buckets] == ["0-5 bps", "5-10 bps", "10-20 bps", "20+ bps"]
    total = sum(b["count"] for b in buckets)
    assert total == payload["execution_quality"]["slippage_samples"]


def test_by_strategy_splits_the_brokers_order_flow(two_brokers):
    rows = CrossBrokerAnalyticsService().get_execution_quality(broker="mstock", period="30d")[
        "by_strategy"
    ]
    assert len(rows) == 1
    assert rows[0]["strategy"] == "ema_pullback"
    assert rows[0]["total_orders"] == 60


def test_peer_benchmarks_show_the_other_venues(two_brokers):
    peers = CrossBrokerAnalyticsService().get_execution_quality(broker="dhan", period="30d")[
        "peer_benchmarks"
    ]
    assert {p["broker"] for p in peers} == {"mstock", "dhan"}
    subject = next(p for p in peers if p["broker"] == "dhan")
    assert subject["is_subject"] is True
    assert (
        subject["avg_slippage_bps"]
        > next(p for p in peers if p["broker"] == "mstock")["avg_slippage_bps"]
    )


def test_data_quality_reports_the_session_scoped_ledger(two_brokers):
    payload = CrossBrokerAnalyticsService().get_execution_quality(period="30d")
    dq = payload["data_quality"]
    assert dq["source"] == "order_ledger"
    assert dq["session_scoped"] is True
    assert dq["scanned"] == 120
    assert dq["truncated"] is False
    assert dq["in_period"] == 120
    assert "restart" in dq["note"]


def test_orders_from_a_removed_runner_are_dropped_not_reassigned(two_brokers):
    extra = venues_add_and_remove(two_brokers)
    rows, provenance = CrossBrokerAnalyticsService()._orders(period="all_time")
    assert provenance["orphaned"] >= 60
    assert all(r["instance_id"] != extra for r in rows)


def venues_add_and_remove(fixture):
    """Register a third runner, then delete it — the ledger keeps its orders."""
    from backtest.forward.portfolio_manager import get_portfolio_manager

    mgr = get_portfolio_manager()
    instance_id = fixture["add"]("Gone", "macd_trend", "dhan", 10_000.0, "ghost")
    place_orders(instance_id, 60, slip_bps=40.0, fill_rate_pct=50.0)
    with mgr._lock:
        mgr._runners.pop(instance_id, None)
    return instance_id


# ----------------------------------------------------------------------
# Per-order derivations
# ----------------------------------------------------------------------


def test_slippage_is_adverse_positive_on_both_sides():
    buy = {"side": "BUY", "requested_price": 100.0, "avg_fill_price": 100.1}
    sell = {"side": "SELL", "requested_price": 100.0, "avg_fill_price": 99.9}
    assert _order_slippage_bps(buy) == pytest.approx(10.0, abs=1e-6)
    assert _order_slippage_bps(sell) == pytest.approx(10.0, abs=1e-6)
    # Price IMPROVEMENT is negative, not clamped away.
    assert (
        _order_slippage_bps({"side": "BUY", "requested_price": 100.0, "avg_fill_price": 99.9}) < 0
    )


def test_slippage_prefers_the_ledgers_own_side_aware_value():
    row = {"side": "BUY", "requested_price": 100.0, "avg_fill_price": 101.0, "slippage_pct": 0.0004}
    assert _order_slippage_bps(row) == pytest.approx(4.0, abs=1e-6)


def test_slippage_is_none_without_a_reference_price():
    """A fill with no request price is NOT a zero-slippage fill."""
    assert _order_slippage_bps({"side": "BUY", "avg_fill_price": 100.0}) is None
    assert (
        _order_slippage_bps({"side": "BUY", "requested_price": 0.0, "avg_fill_price": 100.0})
        is None
    )


def test_fill_time_is_never_negative():
    assert (
        _order_fill_time_sec(
            {"created_ts": "2026-09-01T10:00:10+00:00", "filled_ts": "2026-09-01T10:00:00+00:00"}
        )
        == 0.0
    )
    assert _order_fill_time_sec({"created_ts": "2026-09-01T10:00:00+00:00"}) is None


# ----------------------------------------------------------------------
# Comparison
# ----------------------------------------------------------------------


def test_compare_picks_a_winner_only_from_significant_metrics(two_brokers):
    seed_trades(two_brokers["mstock"], [1200, -400, 900, 1500, -300] * 8)
    seed_trades(two_brokers["dhan"], [300, -600, 200, -400, 500] * 4)
    result = CrossBrokerAnalyticsService().compare_brokers(["mstock", "dhan"])
    rows = {row["metric"]: row for row in result["metrics"]}
    assert rows["avg_slippage_bps"]["better"] == "mstock"
    assert rows["avg_slippage_bps"]["statistical_significance"]["significant"] is True
    assert result["recommendation"]["preferred_broker"] == "mstock"


def test_compare_refuses_to_test_a_thin_trade_sample(two_brokers):
    seed_trades(two_brokers["mstock"], [100, -50])
    seed_trades(two_brokers["dhan"], [50, -25])
    rows = {
        row["metric"]: row
        for row in CrossBrokerAnalyticsService().compare_brokers(["mstock", "dhan"])["metrics"]
    }
    sharpe = rows["sharpe"]["statistical_significance"]
    assert sharpe["testable"] is False
    assert "insufficient sample" in sharpe["reason"]


def test_net_pnl_is_described_but_never_significance_tested(two_brokers):
    seed_trades(two_brokers["mstock"], [100] * 30)
    seed_trades(two_brokers["dhan"], [10] * 30)
    rows = {
        row["metric"]: row
        for row in CrossBrokerAnalyticsService().compare_brokers(["mstock", "dhan"])["metrics"]
    }
    pnl = rows["net_pnl"]
    assert pnl["mstock"] == 3000.0
    assert pnl["better"] == "mstock"
    assert pnl["statistical_significance"]["testable"] is False
    assert "not normalised" in pnl["statistical_significance"]["reason"]


def test_compare_can_skip_the_tests_entirely(two_brokers):
    result = CrossBrokerAnalyticsService().compare_brokers(
        ["mstock", "dhan"], statistical_test=False
    )
    assert all(row["statistical_significance"]["testable"] is False for row in result["metrics"])
    assert all(
        "statistical_test=false" in row["statistical_significance"]["reason"]
        for row in result["metrics"]
    )


def test_compare_rejects_a_single_broker_and_unknown_metrics(two_brokers):
    svc = CrossBrokerAnalyticsService()
    with pytest.raises(ValueError, match="at least two brokers"):
        svc.compare_brokers(["mstock"])
    with pytest.raises(ValueError, match="unknown metric"):
        svc.compare_brokers(["mstock", "dhan"], metrics=["vibes"])


def test_compare_reports_brokers_with_no_runners(two_brokers):
    result = CrossBrokerAnalyticsService().compare_brokers(["mstock", "dhan", "zerodha"])
    assert result["brokers"] == ["mstock", "dhan"]
    assert result["unknown_brokers"] == ["zerodha"]


def test_compare_counts_stay_integers(two_brokers):
    seed_trades(two_brokers["mstock"], [100] * 5)
    seed_trades(two_brokers["dhan"], [50] * 3)
    rows = {
        row["metric"]: row
        for row in CrossBrokerAnalyticsService().compare_brokers(["mstock", "dhan"])["metrics"]
    }
    assert rows["total_trades"]["mstock"] == 5
    assert isinstance(rows["total_trades"]["difference"], int)


def test_recommendation_is_none_when_nothing_is_significant(venues):
    """Two brokers with identical telemetry must not produce a 'winner'."""
    a = venues["add"]("A", "ema_pullback", "mstock", 100_000.0)
    b = venues["add"]("B", "rsi_reversion", "dhan", 100_000.0)
    for runner in (a, b):
        place_orders(runner, 60, slip_bps=10.0, fill_rate_pct=90.0, reject_rate_pct=5.0)
        seed_trades(runner, [100, -50, 75] * 10)
    result = CrossBrokerAnalyticsService().compare_brokers(["mstock", "dhan"])
    assert result["recommendation"]["preferred_broker"] is None
    assert result["recommendation"]["confidence"] == "none"


# ----------------------------------------------------------------------
# Migration what-if
# ----------------------------------------------------------------------


def test_migration_estimates_a_penalty_for_the_worse_venue(two_brokers):
    seed_trades(two_brokers["mstock"], [1000, -400, 800] * 10)
    result = CrossBrokerAnalyticsService().migration_impact(
        "ema_pullback", "mstock", "dhan", period="30d"
    )
    current, estimated = result["current_performance"], result["estimated_performance"]
    assert current["fill_rate_pct"] > estimated["fill_rate_pct"]
    assert estimated["avg_slippage_bps"] > current["avg_slippage_bps"]
    assert estimated["estimated_trades"] < current["total_trades"]
    assert estimated["estimated_net_pnl"] < current["net_pnl"]
    assert result["impact"]["pnl_change"] < 0
    assert result["recommendation"]["action"] == "KEEP"
    assert any("slippage" in r for r in result["impact"]["reasons"])


def test_migration_recommends_moving_to_a_better_venue(two_brokers):
    seed_trades(two_brokers["dhan"], [1000, -400, 800] * 10)
    result = CrossBrokerAnalyticsService().migration_impact(
        "rsi_reversion", "dhan", "mstock", period="30d"
    )
    assert result["impact"]["pnl_change"] > 0
    assert result["recommendation"]["action"] == "MOVE"


def test_migration_holds_when_the_two_venues_are_indistinguishable(venues):
    a = venues["add"]("A", "ema_pullback", "mstock", 100_000.0)
    b = venues["add"]("B", "rsi_reversion", "dhan", 100_000.0)
    for runner in (a, b):
        place_orders(runner, 60, slip_bps=10.0, fill_rate_pct=90.0, reject_rate_pct=5.0)
        seed_trades(runner, [100, -50, 75] * 10)
    result = CrossBrokerAnalyticsService().migration_impact(
        "ema_pullback", "mstock", "dhan", period="30d"
    )
    assert result["recommendation"]["action"] == "HOLD"


def test_migration_says_so_when_the_strategy_never_ran_on_the_target(two_brokers):
    seed_trades(two_brokers["mstock"], [1000, -400] * 10)
    result = CrossBrokerAnalyticsService().migration_impact(
        "ema_pullback", "mstock", "dhan", period="30d"
    )
    assert result["historical_data"]["available"] is False
    assert "never ran on Dhan" in result["historical_data"]["note"]


def test_migration_reports_observed_numbers_when_the_strategy_has_run_both_ways(venues):
    a = venues["add"]("A", "ema_pullback", "mstock", 100_000.0)
    b = venues["add"]("B", "ema_pullback", "dhan", 100_000.0, "ghost")
    place_orders(a, 60, slip_bps=8.0, fill_rate_pct=95.0)
    place_orders(b, 60, slip_bps=18.0, fill_rate_pct=87.0)
    seed_trades(a, [1000, -400] * 15)
    seed_trades(b, [400, -200] * 15)
    result = CrossBrokerAnalyticsService().migration_impact(
        "ema_pullback", "mstock", "dhan", period="30d"
    )
    history = result["historical_data"]
    assert history["available"] is True
    assert history["net_pnl"] == 3000.0
    assert "OBSERVED" in history["note"]


def test_migration_refuses_an_unknown_strategy_or_same_broker(two_brokers):
    svc = CrossBrokerAnalyticsService()
    with pytest.raises(LookupError, match="no runner on broker"):
        svc.migration_impact("macd_trend", "mstock", "dhan")
    with pytest.raises(ValueError, match="must differ"):
        svc.migration_impact("ema_pullback", "mstock", "mstock")
    with pytest.raises(ValueError, match="required"):
        svc.migration_impact("", "mstock", "dhan")


def test_migration_refuses_to_estimate_without_a_target_fill_rate(venues):
    a = venues["add"]("A", "ema_pullback", "mstock", 100_000.0)
    venues["add"]("B", "rsi_reversion", "dhan", 100_000.0)
    place_orders(a, 60, slip_bps=8.0, fill_rate_pct=95.0)
    seed_trades(a, [1000, -400] * 10)  # Dhan never traded → no fill rate
    result = CrossBrokerAnalyticsService().migration_impact(
        "ema_pullback", "mstock", "dhan", period="30d"
    )
    assert result["impact"]["pnl_change"] is None
    assert result["recommendation"]["action"] == "INSUFFICIENT DATA"
    assert any("no recorded fill rate" in r for r in result["recommendation"]["reasoning"])


def test_estimate_migration_isolates_each_leg():
    current = {
        "broker": "mstock",
        "net_pnl": 10_000.0,
        "total_trades": 100,
        "sharpe": 2.0,
        "fill_rate_pct": 95.0,
        "avg_slippage_bps": 8.0,
        "orders": 100,
    }
    target = {
        "fill_rate_pct": 85.0,
        "avg_slippage_bps": 18.0,
        "avg_order_notional": 10_000.0,
        "avg_fill_time_sec": 5.0,
    }
    estimated, impact, reasons = _estimate_migration(current, target, "mstock", "dhan")
    # Trade count: 100 * 85/95 = 89.47 → 89
    assert estimated["estimated_trades"] == 89
    assert impact["trade_count_change"] == -11
    assert impact["trade_retention"] == pytest.approx(0.8947, abs=1e-4)
    # Slippage leg: 2 sides * 10 bps * ₹10,000 = ₹20 per retained round trip
    assert any("₹20" in r for r in reasons)
    # P&L = 10000 * 0.8947 - 20 * 89
    assert estimated["estimated_net_pnl"] == pytest.approx(10000 * 0.89474 - 20 * 89, abs=1.0)
    assert impact["pnl_change"] < 0
    # Sharpe scales with the P&L change at constant volatility
    assert impact["estimated_sharpe"] == pytest.approx(
        2.0 * estimated["estimated_net_pnl"] / 10_000.0, abs=0.01
    )
    assert impact["sharpe_change"] < 0


def test_estimate_migration_omits_the_slippage_leg_without_a_notional():
    current = {
        "broker": "mstock",
        "net_pnl": 10_000.0,
        "total_trades": 100,
        "sharpe": 2.0,
        "fill_rate_pct": 95.0,
        "avg_slippage_bps": 8.0,
        "orders": 100,
    }
    target = {"fill_rate_pct": 85.0, "avg_slippage_bps": 18.0, "avg_order_notional": None}
    estimated, _impact, reasons = _estimate_migration(current, target, "mstock", "dhan")
    assert any("omitted rather than guessed" in r for r in reasons)
    assert estimated["estimated_trades"] == 89


def test_migration_noise_band_is_a_documented_constant():
    assert 0 < MIGRATION_NOISE_PCT < 10
    assert "all_time" in PERIOD_DAYS and PERIOD_DAYS["all_time"] is None


# ----------------------------------------------------------------------
# Broker recommendation for a new strategy
# ----------------------------------------------------------------------


def test_recommendation_prefers_the_fast_filling_venue_for_a_scalper(two_brokers):
    result = CrossBrokerAnalyticsService().recommend_broker(
        strategy_type="scalper", trade_frequency="high", period="30d"
    )
    assert result["recommended_broker"] == "mstock"
    assert result["rankings"][0]["broker"] == "mstock"
    assert "Avg fill time" in result["rankings"][0]["strengths"]
    assert result["rankings"][0]["sub_scores"]["fill_time_sec"] == 100.0


def test_recommendation_refuses_to_guess_without_execution_history(venues):
    venues["add"]("A", "ema_pullback", "mstock", 100_000.0)
    venues["add"]("B", "rsi_reversion", "dhan", 100_000.0)
    result = CrossBrokerAnalyticsService().recommend_broker(period="30d")
    assert result["recommended_broker"] is None
    assert result["rankings"] == []
    assert any("not enough execution history" in r for r in result["reasoning"])


def test_recommendation_savings_are_scaled_by_observed_order_rate(two_brokers):
    result = CrossBrokerAnalyticsService().recommend_broker(
        strategy_type="scalper", avg_trade_size=50_000.0, period="30d"
    )
    savings = result["estimated_monthly_savings"]
    assert savings["amount"] > 0
    # 120 orders in 30d → 120/month; 2 sides × 9.9 bps × ₹50,000
    assert savings["monthly_orders"] == pytest.approx(120.0)
    assert savings["amount"] == pytest.approx(120 * 2 * (9.9 / 10_000) * 50_000, rel=0.01)
    assert "orders/month" in savings["calculation"]


def test_recommendation_falls_back_when_the_segment_has_no_runners(venues):
    instance_id = venues["add"]("A", "ema_pullback", "mstock", 100_000.0, "options_index")
    place_orders(instance_id, 60, slip_bps=8.0, fill_rate_pct=95.0)
    result = CrossBrokerAnalyticsService().recommend_broker(segment="does_not_exist")
    assert result["recommended_broker"] == "mstock"
    assert "No runner is currently deployed" in result["segment_note"]


def test_scoring_weights_normalise_and_respect_the_strategy_style():
    high = _scoring_weights("scalper", "high")
    assert sum(high.values()) == pytest.approx(1.0, abs=1e-3)
    # A scalper cares about latency more than a swing trade does.
    assert high["fill_time_sec"] > _scoring_weights("swing", "high")["fill_time_sec"]
    assert _scoring_weights("swing", "low")["avg_slippage_bps"] > high["avg_slippage_bps"]
    # An unknown frequency falls back to medium, never to an empty weight set.
    assert sum(_scoring_weights("scalper", "telepathic").values()) == pytest.approx(1.0, abs=1e-3)


def test_broker_display_names_use_the_brand():
    assert _broker_display("mstock") == "mStock"
    assert _broker_display("dhan") == "Dhan"
    assert _broker_display("paper") == "Paper (sim)"
    assert _broker_display("") == "Unassigned"
    assert _broker_display("zerodha") == "Zerodha"
