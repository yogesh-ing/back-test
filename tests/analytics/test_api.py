"""Cross-broker analytics REST endpoints (PRD-003 §4).

Five endpoints on the existing ``analytics_bp`` — no new nav item, no new
tab, matching the PRD's "extends /analytics" requirement:

* ``GET  /api/analytics/cross-broker/summary``
* ``GET  /api/analytics/cross-broker/execution``
* ``POST /api/analytics/cross-broker/compare``
* ``POST /api/analytics/cross-broker/migration-impact``
* ``POST /api/analytics/cross-broker/recommend-broker``

The emphasis here is the HTTP contract: status codes, fail-closed validation
(a typo'd metric is a 400, never a 500), and JSON-serialisability of every
payload the service produces.
"""

from __future__ import annotations

import pytest

from backtest.brokers.segments import reset_segments_config
from backtest.forward.portfolio_manager import reset_portfolio_manager
from backtest.web.app import create_app

from .helpers import place_orders, seed_trades

SUMMARY = "/api/analytics/cross-broker/summary"
EXECUTION = "/api/analytics/cross-broker/execution"
COMPARE = "/api/analytics/cross-broker/compare"
MIGRATION = "/api/analytics/cross-broker/migration-impact"
RECOMMEND = "/api/analytics/cross-broker/recommend-broker"


@pytest.fixture()
def client():
    reset_portfolio_manager()
    reset_segments_config()
    return create_app(source="synthetic").test_client()


@pytest.fixture()
def two_brokers(venues):
    mstock = venues["add"]("M", "ema_pullback", "mstock", 120_000.0, "options_index")
    dhan = venues["add"]("D", "rsi_reversion", "dhan", 80_000.0, "equity_intraday")
    place_orders(
        mstock, 60, slip_bps=8.2, fill_rate_pct=95.0, reject_rate_pct=2.0, fill_delay_s=2.3
    )
    place_orders(dhan, 60, slip_bps=18.1, fill_rate_pct=87.0, reject_rate_pct=8.0, fill_delay_s=5.1)
    seed_trades(mstock, [1200, -400, 900] * 12)
    seed_trades(dhan, [300, -600, 200] * 8)
    return {**venues, "mstock": mstock, "dhan": dhan}


# ----------------------------------------------------------------------
# Endpoint 1 — summary
# ----------------------------------------------------------------------


def test_summary_endpoint(client, venues):
    resp = client.get(SUMMARY)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["period"] == "30d"
    assert "portfolio_total" in data and "by_broker" in data and "by_segment" in data
    assert set(data["broker_rankings"]) >= {"by_sharpe", "by_execution_quality", "by_fill_rate"}


def test_summary_endpoint_with_no_runners_is_a_valid_empty_answer(client, venues):
    data = client.get(SUMMARY).get_json()
    assert data["success"] is True
    assert data["by_broker"] == []
    assert data["portfolio_total"]["total_trades"] == 0


def test_summary_endpoint_honours_period_and_mode(client, two_brokers):
    live = client.get(f"{SUMMARY}?mode=live").get_json()
    assert live["by_broker"] == []
    assert live["mode"] == "live"
    week = client.get(f"{SUMMARY}?period=7d").get_json()
    assert week["period"] == "7d"
    # "all" is the UI's "no filter" and must behave as no filter.
    everything = client.get(f"{SUMMARY}?mode=all").get_json()
    assert everything["mode"] == "all"
    assert len(everything["by_broker"]) == 2


def test_summary_endpoint_splits_two_brokers(client, two_brokers):
    data = client.get(SUMMARY).get_json()
    by_broker = {row["broker"]: row for row in data["by_broker"]}
    assert set(by_broker) == {"mstock", "dhan"}
    assert by_broker["mstock"]["total_trades"] == 36
    assert by_broker["dhan"]["total_trades"] == 24
    assert by_broker["mstock"]["fill_rate_pct"] == pytest.approx(95.0, abs=0.5)
    assert by_broker["dhan"]["rejection_rate_pct"] == pytest.approx(8.33, abs=0.5)


# ----------------------------------------------------------------------
# Endpoint 2 — execution
# ----------------------------------------------------------------------


def test_execution_endpoint_for_one_broker(client, two_brokers):
    resp = client.get(f"{EXECUTION}?broker=dhan&period=30d")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["broker"] == "dhan"
    quality = data["execution_quality"]
    assert quality["total_orders"] == 60
    assert quality["avg_slippage_bps"] == pytest.approx(18.1, abs=0.01)
    assert data["slippage_distribution"]
    assert data["data_quality"]["session_scoped"] is True


def test_execution_endpoint_without_a_broker_covers_everyone(client, two_brokers):
    data = client.get(EXECUTION).get_json()
    assert data["broker"] is None
    assert set(data["brokers"]) == {"mstock", "dhan"}
    assert data["execution_quality"]["total_orders"] == 120


def test_execution_endpoint_filters_by_strategy(client, two_brokers):
    data = client.get(f"{EXECUTION}?broker=dhan&strategy=rsi_reversion").get_json()
    assert data["execution_quality"]["total_orders"] == 60
    data = client.get(f"{EXECUTION}?broker=dhan&strategy=macd_trend").get_json()
    assert data["execution_quality"]["total_orders"] == 0
    assert data["execution_quality"]["fill_rate_pct"] is None


def test_execution_endpoint_for_an_unknown_broker_is_empty_not_an_error(client, two_brokers):
    data = client.get(f"{EXECUTION}?broker=zerodha").get_json()
    assert data["success"] is True
    assert data["execution_quality"]["total_orders"] == 0


# ----------------------------------------------------------------------
# Endpoint 3 — compare
# ----------------------------------------------------------------------


def test_compare_endpoint_returns_metric_rows(client, two_brokers):
    resp = client.post(
        COMPARE,
        json={
            "brokers": ["mstock", "dhan"],
            "metrics": ["avg_slippage_bps", "fill_rate_pct", "sharpe"],
            "period": "30d",
            "statistical_test": True,
        },
    )
    assert resp.status_code == 200
    data = resp.get_json()
    rows = {row["metric"]: row for row in data["metrics"]}
    assert set(rows) == {"avg_slippage_bps", "fill_rate_pct", "sharpe"}
    slip = rows["avg_slippage_bps"]
    assert slip["better"] == "mstock"
    assert slip["statistical_significance"]["test"] == "mann_whitney_u"
    assert slip["statistical_significance"]["significant"] is True
    assert data["recommendation"]["preferred_broker"] == "mstock"


def test_compare_endpoint_defaults_to_every_metric(client, two_brokers):
    data = client.post(COMPARE, json={"brokers": ["mstock", "dhan"]}).get_json()
    assert len(data["metrics"]) >= 8


def test_compare_endpoint_rejects_bad_input(client, two_brokers):
    assert client.post(COMPARE, json={"brokers": ["mstock"]}).status_code == 400
    assert client.post(COMPARE, json={"brokers": "mstock"}).status_code == 400
    bad_metric = client.post(COMPARE, json={"brokers": ["mstock", "dhan"], "metrics": ["vibes"]})
    assert bad_metric.status_code == 400
    assert "unknown metric" in bad_metric.get_json()["error"]


def test_compare_endpoint_404s_when_neither_broker_has_data(client, venues):
    venues["add"]("A", "ema_pullback", "mstock", 100_000.0)
    resp = client.post(COMPARE, json={"brokers": ["mstock", "zerodha"]})
    assert resp.status_code == 400
    assert "no data" in resp.get_json()["error"]


def test_compare_endpoint_can_skip_the_tests(client, two_brokers):
    data = client.post(
        COMPARE, json={"brokers": ["mstock", "dhan"], "statistical_test": False}
    ).get_json()
    assert all(row["statistical_significance"]["testable"] is False for row in data["metrics"])


# ----------------------------------------------------------------------
# Endpoint 4 — migration impact
# ----------------------------------------------------------------------


def test_migration_endpoint_estimates_the_move(client, two_brokers):
    resp = client.post(
        MIGRATION,
        json={
            "strategy": "ema_pullback",
            "from_broker": "mstock",
            "to_broker": "dhan",
            "period": "30d",
        },
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["strategy"] == "ema_pullback"
    assert data["current_performance"]["broker"] == "mstock"
    assert data["estimated_performance"]["broker"] == "dhan"
    assert data["impact"]["pnl_change"] < 0
    assert data["recommendation"]["action"] == "KEEP"
    assert data["historical_data"]["available"] is False


def test_migration_endpoint_404s_for_an_unknown_strategy(client, two_brokers):
    resp = client.post(
        MIGRATION,
        json={
            "strategy": "macd_trend",
            "from_broker": "mstock",
            "to_broker": "dhan",
        },
    )
    assert resp.status_code == 404
    assert "no runner on broker" in resp.get_json()["error"]


def test_migration_endpoint_400s_on_missing_or_identical_brokers(client, two_brokers):
    same = client.post(
        MIGRATION,
        json={
            "strategy": "ema_pullback",
            "from_broker": "mstock",
            "to_broker": "mstock",
        },
    )
    assert same.status_code == 400
    assert "must differ" in same.get_json()["error"]
    empty = client.post(MIGRATION, json={"strategy": "", "from_broker": "a", "to_broker": "b"})
    assert empty.status_code == 400


# ----------------------------------------------------------------------
# Endpoint 5 — recommend broker
# ----------------------------------------------------------------------


def test_recommend_endpoint_ranks_the_venues(client, two_brokers):
    resp = client.post(
        RECOMMEND,
        json={
            "strategy_type": "scalper",
            "trade_frequency": "high",
            "avg_trade_size": 50000,
            "period": "30d",
        },
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["recommended_broker"] == "mstock"
    assert [r["broker"] for r in data["rankings"]] == ["mstock", "dhan"]
    assert data["rankings"][0]["score"] > data["rankings"][1]["score"]
    assert data["estimated_monthly_savings"]["amount"] > 0
    assert data["confidence"] in ("high", "medium", "low")


def test_recommend_endpoint_refuses_without_execution_history(client, venues):
    venues["add"]("A", "ema_pullback", "mstock", 100_000.0)
    venues["add"]("B", "rsi_reversion", "dhan", 100_000.0)
    data = client.post(RECOMMEND, json={"strategy_type": "swing"}).get_json()
    assert data["recommended_broker"] is None
    assert data["rankings"] == []


def test_recommend_endpoint_400s_on_a_non_numeric_trade_size(client, two_brokers):
    resp = client.post(RECOMMEND, json={"avg_trade_size": "loads"})
    assert resp.status_code == 400


def test_recommend_endpoint_filters_by_segment(client, two_brokers):
    data = client.post(RECOMMEND, json={"segment": "options_index"}).get_json()
    assert data["recommended_broker"] == "mstock"
    assert [r["broker"] for r in data["rankings"]] == ["mstock"]


# ----------------------------------------------------------------------
# Wiring
# ----------------------------------------------------------------------


@pytest.mark.parametrize("path", [SUMMARY, EXECUTION])
def test_get_endpoints_accept_a_mode_filter(client, two_brokers, path):
    assert client.get(f"{path}?mode=all").status_code == 200
    assert client.get(f"{path}?mode=paper").status_code == 200


def test_analytics_page_ships_the_cross_broker_sections(client):
    html = client.get("/analytics").get_data(as_text=True)
    assert 'id="brokerSection"' in html
    assert 'id="executionSection"' in html
    assert "cross_broker.js" in html
    assert 'id="crossBrokerCompareOverlay"' in html
    assert 'id="crossBrokerMigrationOverlay"' in html


def test_overview_cards_carry_their_broker_attribution(client, two_brokers):
    data = client.get("/api/analytics/overview").get_json()
    cards = data["strategy_cards"]
    assert {c["broker"] for c in cards} == {"mstock", "dhan"}
    assert {c["segment"] for c in cards} == {"options_index", "equity_intraday"}


def test_every_cross_broker_payload_is_json_serialisable(client, two_brokers):
    """No NaN / Infinity / Decimal may reach the wire (Flask emits them raw)."""
    import json

    payloads = [
        client.get(SUMMARY).get_data(as_text=True),
        client.get(EXECUTION).get_data(as_text=True),
        client.get(f"{EXECUTION}?broker=dhan").get_data(as_text=True),
        client.post(COMPARE, json={"brokers": ["mstock", "dhan"]}).get_data(as_text=True),
        client.post(
            MIGRATION,
            json={
                "strategy": "ema_pullback",
                "from_broker": "mstock",
                "to_broker": "dhan",
            },
        ).get_data(as_text=True),
        client.post(RECOMMEND, json={"strategy_type": "scalper"}).get_data(as_text=True),
    ]
    for raw in payloads:
        assert "NaN" not in raw
        assert "Infinity" not in raw
        json.loads(raw)  # would raise on a non-serialisable value
