"""Portfolio Intelligence — end-to-end through the real PortfolioManager.

Runners, the options bridge, the alert broker, strategy subscriptions and the
REST API wired together, driven bar-by-bar (no feed thread).

The PRD's core promise is pinned here: the platform calculates, informs and
broadcasts; **strategies decide**. A subscribed strategy may pause its own
entries or *request* an exit that the runner executes through the normal
close path — the platform itself never closes anything in response to an
alert (an unsubscribed runner's book is untouched by every alert).
"""

from __future__ import annotations

import time

import pytest

from backtest.alerts.types import AlertType
from backtest.forward.paper_runner import RunnerConfig
from backtest.forward.portfolio_manager import reset_portfolio_manager
from backtest.plugins import discover_plugins
from backtest.strategy.registry import get_strategy

GAMMA = AlertType.PORTFOLIO_GAMMA_CRITICAL.value


@pytest.fixture(scope="module", autouse=True)
def _plugins():
    discover_plugins()


@pytest.fixture()
def mgr():
    m = reset_portfolio_manager(auto_start_feed=False)
    yield m
    m.shutdown()


def strangle_config(name="Strangle A", **params):
    return RunnerConfig(
        name=name,
        strategy_name="immediate_strangle",
        allocated_capital=500000,
        symbols=["NIFTY"],
        target_type="SINGLE_SYMBOL",
        instrument={"type": "option",
                    "expression": {"type": "strangle", "strike_selection": "atm", "quantity": 1}},
        mode="paper",
        strategy_params=params,
    )


def equity_config(name="Equity", symbol="RELIANCE", strategy="buy_and_hold"):
    return RunnerConfig(
        name=name,
        strategy_name=strategy,
        allocated_capital=1_000_000,
        symbols=[symbol],
        target_type="SINGLE_SYMBOL",
        mode="paper",
    )


_clock = {"i": 0}


def feed(mgr, symbol="NIFTY", n=3, price=25000.0, step=0.0):
    for _ in range(n):
        i = _clock["i"]
        _clock["i"] += 1
        ts = f"2026-09-16T{9 + i // 60:02d}:{i % 60:02d}:00"
        bar = {"ts": ts, "open": price, "high": price + 5, "low": price - 5, "close": price,
               "volume": 1000}
        mgr._on_bar(symbol, bar)
        mgr._on_tick_end(ts)
        price += step


def evaluate(mgr):
    mgr.intelligence.invalidate()
    mgr.intelligence.maybe_evaluate(force=True)
    return mgr.intelligence.broker


def open_types(broker):
    return {a.alert_type for a in broker.get_active_alerts(include_dismissed=True)}


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_manager_owns_intelligence_and_registers_subscriptions(mgr):
    iid = mgr.add_runner(strangle_config())
    subs = mgr.intelligence.broker.subscriptions()["subscribers"]
    assert [s["instance_id"] for s in subs] == [iid]
    assert set(subs[0]["alert_types"]) == {GAMMA, "vix_regime_change"}
    mgr.remove_runner(iid)
    assert mgr.intelligence.broker.subscriptions()["subscribers"] == []


def test_runtime_subscribe_reregisters(mgr):
    iid = mgr.add_runner(equity_config())
    strategy = mgr.get_runner(iid).strategy
    assert mgr.intelligence.broker.subscribers_for("data_feed_stale") == []
    strategy.subscribe_to_alerts(["data_feed_stale"])
    subs = mgr.intelligence.broker.subscribers_for("data_feed_stale")
    assert [s["instance_id"] for s in subs] == [iid]
    with pytest.raises(ValueError):
        strategy.subscribe_to_alerts(["not_a_real_alert"])


def test_greeks_aggregate_real_option_book(mgr):
    mgr.add_runner(strangle_config())
    feed(mgr, n=3)
    g = mgr.intelligence.greeks()
    assert g["positions"] == 1 and g["legs_priced"] == 2
    assert g["net_gamma"] < 0 and g["net_theta"] > 0
    row = g["breakdown_by_strategy"][0]
    assert row["strategy"] == "immediate_strangle" and row["positions"] == 1


# ---------------------------------------------------------------------------
# Gamma alert → strategy decides
# ---------------------------------------------------------------------------


def test_gamma_alert_pauses_subscriber_and_resolution_resumes(mgr):
    iid = mgr.add_runner(strangle_config())
    feed(mgr, n=3)
    runner = mgr.get_runner(iid)
    mgr.intelligence.config.gamma_critical = -1.0
    broker = evaluate(mgr)
    alerts = [a for a in broker.get_active_alerts() if a.alert_type == GAMMA]
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.severity == "critical"
    assert alert.data["contributors"][0]["instance_id"] == iid
    assert alert.data["move_2pct_pnl"] < 0
    assert [n["subscriber_id"] for n in alert.notified_strategies] == [iid]
    assert runner.strategy.pause_new_entries is True
    assert any(e["kind"] == "ALERT" for e in runner.signal_log)
    # The platform did NOT close anything: the structure is still open.
    assert runner.options_bridge._has_open_structure()

    mgr.intelligence.config.gamma_critical = -1e9
    evaluate(mgr)
    assert alert.resolved
    assert runner.strategy.pause_new_entries is False


def test_exit_response_requests_close_through_engine(mgr):
    iid = mgr.add_runner(strangle_config(alert_response="exit"))
    feed(mgr, n=3)
    runner = mgr.get_runner(iid)
    assert runner.options_bridge._has_open_structure()
    mgr.intelligence.config.gamma_critical = -1.0
    evaluate(mgr)
    # Requested, not executed yet — execution happens on the runner's bar.
    assert runner.options_bridge._has_open_structure()
    feed(mgr, n=1)
    assert not runner.options_bridge._has_open_structure()
    reasons = [t.get("exit_reason") for t in runner.closed_trades]
    assert any(str(r).startswith("strategy_alert:") for r in reasons), reasons
    # Paused: no new strangle is opened while the alert is active.
    feed(mgr, n=2)
    assert not runner.options_bridge._has_open_structure()
    assert any(e["kind"] == "OPTION_BLOCKED" for e in runner.signal_log)


def test_unsubscribed_runner_is_never_touched(mgr):
    a = mgr.add_runner(strangle_config("Subscribed"))
    b = mgr.add_runner(strangle_config("Ignorer", alert_response="ignore"))
    feed(mgr, n=3)
    mgr.intelligence.config.gamma_critical = -1.0
    evaluate(mgr)
    assert mgr.get_runner(a).strategy.pause_new_entries is True
    assert mgr.get_runner(b).strategy.pause_new_entries is False
    assert mgr.get_runner(b).options_bridge._has_open_structure()


def test_partial_exit_on_option_structure_is_refused_atomically(mgr):
    iid = mgr.add_runner(strangle_config())
    feed(mgr, n=3)
    runner = mgr.get_runner(iid)
    runner.strategy.request_exit(0.5, reason="trim")
    feed(mgr, n=1)
    assert runner.options_bridge._has_open_structure()
    assert any(e["kind"] == "ALERT_EXIT_REFUSED" for e in runner.signal_log)


def test_pause_gate_blocks_equity_entries_but_not_exits(mgr):
    strategy = get_strategy("buy_and_hold")()
    strategy.pause_new_entries = True
    iid = mgr.add_runner(equity_config(), strategy=strategy)
    runner = mgr.get_runner(iid)
    feed(mgr, symbol="RELIANCE", n=20, price=2500.0)
    assert runner.positions == {}
    assert any("paused by strategy" in (e["reason"] or "") for e in runner.signal_log)
    strategy.pause_new_entries = False
    feed(mgr, symbol="RELIANCE", n=1, price=2500.0)
    assert "RELIANCE" in runner.positions
    qty = runner.positions["RELIANCE"]["qty"]
    strategy.request_exit(0.5, reason="scale_out")
    feed(mgr, symbol="RELIANCE", n=1, price=2500.0)
    assert runner.positions["RELIANCE"]["qty"] == pytest.approx(qty - int(qty * 0.5), abs=1)


# ---------------------------------------------------------------------------
# Other rules
# ---------------------------------------------------------------------------


def test_delta_warning_and_auto_resolve(mgr):
    iid = mgr.add_runner(equity_config())
    feed(mgr, symbol="RELIANCE", n=20, price=2500.0)
    assert mgr.get_runner(iid).positions
    mgr.intelligence.config.delta_warning_abs = 10
    broker = evaluate(mgr)
    delta = [a for a in broker.get_active_alerts() if a.alert_type == "portfolio_delta_warning"]
    assert len(delta) == 1 and delta[0].data["net_delta"] > 10
    mgr.intelligence.config.delta_warning_abs = 1e12
    evaluate(mgr)
    assert delta[0].resolved


def test_concentration_and_strike_clustering(mgr):
    for i in range(3):
        mgr.add_runner(strangle_config(f"Strangle {i}"))
    feed(mgr, n=3)
    broker = evaluate(mgr)
    types = open_types(broker)
    assert "concentration_high" in types
    clusters = [a for a in broker.get_active_alerts() if a.alert_type == "strike_clustering"]
    assert clusters and all(a.severity == "info" for a in clusters)
    assert clusters[0].data["positions"] == 3


def test_regime_change_alert_and_strategy_regime_response(mgr):
    iid = mgr.add_runner(strangle_config())
    feed(mgr, n=2)
    runner = mgr.get_runner(iid)
    intel = mgr.intelligence
    intel.ingest_vix(13.0)
    assert intel.regime_snapshot()["regime"] == "low_vol"
    intel.ingest_vix(25.0)
    alerts = [a for a in intel.broker.get_active_alerts() if a.alert_type == "vix_regime_change"]
    assert len(alerts) == 1
    assert alerts[0].data["old_regime"] == "low_vol" and alerts[0].data["new_regime"] == "high_vol"
    assert [x["instance_id"] for x in alerts[0].data["affected"]] == [iid]
    assert runner.strategy.pause_new_entries is True
    fit = intel.regime_snapshot()["strategy_fit"][0]
    assert fit["status"] == "unfavorable" and fit["runner_status"] == "RUNNING"
    intel.broker.renotify_cooldown_s = 0
    intel.ingest_vix(12.0)
    current = [a for a in intel.broker.get_active_alerts() if a.alert_type == "vix_regime_change"]
    assert len(current) == 1 and current[0].data["new_regime"] == "low_vol"
    assert alerts[0].resolved  # superseded
    assert runner.strategy.pause_new_entries is False


def test_correlation_spike_between_runners(mgr):
    a = mgr.add_runner(equity_config("A", "RELIANCE"))
    b = mgr.add_runner(equity_config("B", "TCS"))
    intel = mgr.intelligence
    ea = eb = 1_000_000.0
    import random

    rng = random.Random(3)
    for _ in range(40):
        shock = rng.gauss(0, 500)
        ea += shock
        eb += shock * 0.9 + rng.gauss(0, 20)
        intel.correlation.record({a: ea, b: eb})
    intel.correlation._cache = None
    broker = evaluate(mgr)
    spikes = [x for x in broker.get_active_alerts() if x.alert_type == "correlation_spike"]
    assert len(spikes) == 1 and spikes[0].data["correlation"] > 0.8
    assert spikes[0].subject == "|".join(sorted([a, b]))


def test_data_feed_stale_raised_per_source_and_resolved_by_a_bar(mgr):
    iid = mgr.add_runner(equity_config())
    intel = mgr.intelligence
    intel._last_bar_wall["RELIANCE"] = time.time() - 500
    broker = evaluate(mgr)
    stale = [a for a in broker.get_active_alerts() if a.alert_type == "data_feed_stale"]
    assert len(stale) == 1
    assert stale[0].severity == "critical" and stale[0].subject == "synthetic"
    assert stale[0].data["symbols"] == ["RELIANCE"]
    feed(mgr, symbol="RELIANCE", n=1, price=2500.0)
    evaluate(mgr)
    assert stale[0].resolved
    # A paused runner is not "stale" — nobody expects bars for it to matter.
    mgr.get_runner(iid).pause()
    intel._last_bar_wall["RELIANCE"] = time.time() - 500
    broker = evaluate(mgr)
    assert "data_feed_stale" not in open_types(broker)


def test_new_runner_gets_a_grace_period(mgr):
    mgr.add_runner(equity_config())
    broker = evaluate(mgr)
    assert "data_feed_stale" not in open_types(broker)


def test_chain_activity_raises_oi_anomaly(mgr):
    intel = mgr.intelligence
    oi = 100000
    for step in (1000, 1100, 900, 1000, 9000):
        oi += step
        result = intel.ingest_chain("NIFTY", [{"strike": 23500, "option_type": "CE", "oi": oi}])
    assert len(result["oi_anomalies"]) == 1
    alerts = [a for a in intel.broker.get_active_alerts() if a.alert_type == "oi_anomaly"]
    assert alerts and alerts[0].severity == "info"


def test_disabled_intelligence_is_inert(mgr):
    mgr.add_runner(strangle_config())
    mgr.intelligence.enabled = False
    mgr.intelligence.config.gamma_critical = -1.0
    feed(mgr, n=3)
    assert mgr.intelligence.maybe_evaluate(force=True) is False
    assert mgr.intelligence.broker.counts()["total"] == 0


def test_stream_summary_is_compact(mgr):
    mgr.add_runner(strangle_config())
    feed(mgr, n=3)
    s = mgr.intelligence.stream_summary()
    assert set(s) >= {"net_delta", "net_gamma", "net_vega", "net_theta", "by_strategy", "alerts"}
    assert s["by_strategy"][0]["strategy"] == "immediate_strangle"


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(mgr):
    from backtest.web.app import create_app

    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_api_portfolio_endpoints(mgr, client):
    mgr.add_runner(strangle_config())
    feed(mgr, n=3)
    g = client.get("/api/portfolio/greeks").get_json()
    assert g["success"] and g["positions"] == 1 and g["net_gamma"] < 0
    assert client.get("/api/portfolio/greeks?mode=live").get_json()["positions"] == 0
    assert client.get("/api/portfolio/greeks?mode=bogus").status_code == 400
    c = client.get("/api/portfolio/concentration").get_json()
    assert c["by_underlying"]["NIFTY"]["pct"] == 100
    corr = client.get("/api/portfolio/correlation?group_by=strategy&refresh=1").get_json()
    assert corr["group_by"] == "strategy"
    assert client.get("/api/portfolio/correlation?group_by=x").status_code == 400
    overview = client.get("/api/portfolio/intelligence").get_json()
    expected = {"greeks", "concentration", "correlation", "regime", "alerts", "thresholds"}
    assert expected <= set(overview)


def test_api_market_endpoints(client):
    r = client.post("/api/market/vix", json={"value": 18.4})
    assert r.status_code == 200 and r.get_json()["regime"] == "moderate_vol"
    assert client.post("/api/market/vix", json={"value": "x"}).status_code == 400
    assert client.post("/api/market/vix", json={"value": -3}).status_code == 400
    regime = client.get("/api/market/regime").get_json()
    assert regime["source"] == "manual" and "strategy_fit" in regime
    oi = client.get("/api/market/oi-activity?symbol=NIFTY").get_json()
    assert oi["has_oi_data"] is False and oi["note"]
    bad = client.post("/api/market/chain-activity", json={"underlying": "NIFTY", "rows": []})
    assert bad.status_code == 400
    rows = [{"strike": 23500, "option_type": "CE", "oi": 10}]
    ok = client.post("/api/market/chain-activity", json={"underlying": "NIFTY", "rows": rows})
    assert ok.status_code == 200


def test_api_alert_lifecycle(mgr, client):
    iid = mgr.add_runner(strangle_config())
    feed(mgr, n=3)
    mgr.intelligence.config.gamma_critical = -1.0
    evaluate(mgr)
    active = client.get("/api/alerts/active").get_json()
    assert active["counts"]["critical"] == 1
    row = active["alerts"][0]
    assert row["title"] and row["section"] == "pi-greeks"
    detail = client.get(f"/api/alerts/{row['alert_id']}").get_json()["alert"]
    assert detail["what_it_means"] and detail["typical_responses"]
    assert [s["instance_id"] for s in detail["subscriptions"]["subscribed"]] == [iid]
    subs = client.get("/api/alerts/subscriptions").get_json()
    assert subs["subscribers"][0]["instance_id"] == iid

    r = client.post(f"/api/alerts/{row['alert_id']}/dismiss", json={"by": "tester"})
    assert r.status_code == 200 and r.get_json()["alert"]["status"] == "dismissed"
    assert client.get("/api/alerts/active").get_json()["counts"]["total"] == 0
    assert client.get("/api/alerts/active?include_dismissed=1").get_json()["alerts"]
    for action, status in (("review", "reviewed"), ("resolve", "resolved")):
        r = client.post(f"/api/alerts/{row['alert_id']}/{action}")
        assert r.get_json()["alert"]["status"] == status
    assert client.post("/api/alerts/nope/dismiss").status_code == 404
    assert client.get("/api/alerts/nope").status_code == 404

    hist = client.get("/api/alerts/history?from=24h&type=portfolio_gamma_critical").get_json()
    assert hist["source"] == "memory" and hist["count"] == 1
    assert hist["alerts"][0]["status"] == "resolved"
    assert client.get("/api/alerts/history?from=garbage").status_code == 400
    assert client.get("/api/alerts/history?type=nope").status_code == 400


def test_api_disabled_returns_503(mgr, client):
    mgr.intelligence.enabled = False
    assert client.get("/api/portfolio/greeks").status_code == 503
    assert client.get("/api/alerts/active").status_code == 503


def test_alert_widget_renders_on_every_page(client):
    for page in ("/dashboard", "/backtest", "/portfolio", "/risk", "/data"):
        html = client.get(page).get_data(as_text=True)
        assert 'id="alert-widget"' in html, page
        assert "alert_widget.js" in html, page


def test_risk_board_has_intelligence_sections(client):
    html = client.get("/portfolio").get_data(as_text=True)
    for section in ("pi-greeks", "pi-concentration", "pi-correlation", "pi-regime", "pi-activity"):
        assert f'id="{section}"' in html
    assert "portfolio_intelligence.js" in html


def test_widget_hidden_when_disabled(client):
    client.application.config["PORTFOLIO_INTELLIGENCE_ENABLED"] = False
    html = client.get("/dashboard").get_data(as_text=True)
    assert 'id="alert-widget"' not in html


# ---------------------------------------------------------------------------
# Persistence (opt-in, SQLite)
# ---------------------------------------------------------------------------


def test_persistence_writes_alerts_and_history(mgr, client, tmp_path):
    from backtest.db import DatabaseManager
    from backtest.intelligence.persistence import IntelligencePersister

    db = DatabaseManager.from_env(url=f"sqlite:///{tmp_path / 'pi.db'}")
    persister = IntelligencePersister(db, ensure_schema=True)
    intel = mgr.intelligence
    intel.attach_persister(persister)
    intel.config.greeks_history_s = 0
    mgr.add_runner(strangle_config())
    feed(mgr, n=3)
    intel.config.gamma_critical = -1.0
    intel.ingest_vix(14.0)
    evaluate(mgr)
    persister.flush()
    rows = persister.alert_history()
    assert rows and rows[0]["alert_type"] == GAMMA
    hist = client.get("/api/alerts/history").get_json()
    assert hist["source"] == "database" and hist["alerts"][0]["title"]

    from sqlalchemy import func, select

    from backtest.db.models import MarketRegimeSnapshot, PortfolioGreeksSnapshot

    with db.session() as s:
        assert s.execute(select(func.count()).select_from(PortfolioGreeksSnapshot)).scalar() >= 1
        assert s.execute(select(func.count()).select_from(MarketRegimeSnapshot)).scalar() >= 1
    persister.stop()
