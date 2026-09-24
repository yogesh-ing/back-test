"""REST API + pages for the parameter optimization engine.

The Flask app gets an injected :class:`OptimizationService` backed by an
in-memory SQLite store (``app.config["OPTIMIZATION_SERVICE"]``), a fake
runner manager and the synthetic data source, so nothing here touches a real
database or a real paper/live runner.
"""

from __future__ import annotations

import csv
import io
import logging

import pytest

from backtest.db import DatabaseManager
from backtest.optimization.service import OptimizationService
from backtest.optimization.store import OptimizationStore
from backtest.web.app import create_app

from .support import SMA_DOC, FakeManager, synthetic_loader


@pytest.fixture(scope="module")
def env():
    logging.getLogger("backtest").setLevel(logging.WARNING)
    manager = DatabaseManager.from_env(profile="testing", url="sqlite:///:memory:")
    manager.connect()
    store = OptimizationStore(manager)
    store.ensure_schema()
    fake = FakeManager()
    service = OptimizationService(store, workers=1, loader=synthetic_loader,
                                  manager_getter=lambda: fake)
    app = create_app(source="synthetic")
    app.config.update(TESTING=True, OPTIMIZATION_SERVICE=service)
    yield {"app": app, "client": app.test_client(), "service": service, "fake": fake}
    manager.disconnect()


@pytest.fixture(scope="module")
def client(env):
    return env["client"]


@pytest.fixture(scope="module")
def run_id(env, client):
    doc = {**SMA_DOC, "walkForward": {"enabled": True, "trainPeriodDays": 365,
                                      "testPeriodDays": 180, "stepDays": 180,
                                      "maxEvalsPerSplit": 6}}
    r = client.post("/api/optimize/runs", json=doc)
    assert r.status_code == 201, r.get_json()
    rid = r.get_json()["run_id"]
    run = env["service"].wait(rid, timeout=120)
    assert run["status"] == "completed", run.get("error_message")
    return rid


# ---------------------------------------------------------------------------
# metadata / setup
# ---------------------------------------------------------------------------


def test_meta(client):
    j = client.get("/api/optimize/meta").get_json()
    assert j["success"] and {o["id"] for o in j["objectives"]} >= {"sharpe", "calmar"}
    assert j["methods"] == ["grid", "random", "bayesian", "genetic"]
    assert j["max_optimized_params"] == 8


def test_strategy_space(client):
    r = client.get("/api/optimize/strategies/sma_crossover/space")
    j = r.get_json()
    assert r.status_code == 200
    names = [p["name"] for p in j["parameters"]]
    assert names == ["fast", "slow"]
    assert client.get("/api/optimize/strategies/nope/space").status_code == 404


def test_estimate_and_validation_errors(client):
    r = client.post("/api/optimize/estimate", json=SMA_DOC)
    assert r.status_code == 200 and r.get_json()["estimate"]["grid_size"] == 12
    bad = client.post("/api/optimize/estimate", json={**SMA_DOC, "method": "magic",
                                                      "objectiveFunction": "luck"})
    assert bad.status_code == 400
    assert {"method", "objectiveFunction"} <= set(bad.get_json()["errors"])


def test_runners_endpoint_lists_fake_manager_runners(env, client):
    from backtest.forward.paper_runner import RunnerConfig

    fake = env["fake"]
    iid = fake.add_runner(RunnerConfig(
        name="r1", strategy_name="sma_crossover", allocated_capital=100000,
        symbols=["DEMO"], timeframe="1day", strategy_params={"fast": 9, "slow": 60},
        mode="paper", source="synthetic"))
    other = fake.add_runner(RunnerConfig(
        name="r2", strategy_name="rsi_reversion", allocated_capital=100000,
        symbols=["DEMO"], timeframe="1day", mode="paper", source="synthetic"))
    j = client.get("/api/optimize/runners?strategy=sma_crossover").get_json()
    assert [r["instance_id"] for r in j["runners"]] == [iid]
    assert j["runners"][0]["strategy_params"] == {"fast": 9, "slow": 60}
    fake.remove_runner(other)
    fake.remove_runner(iid)


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------


def test_run_detail_and_list(client, run_id):
    run = client.get(f"/api/optimize/runs/{run_id}").get_json()["run"]
    assert run["status"] == "completed" and run["best_params"]
    assert run["walk_forward_results"]["n_splits"] >= 3
    listing = client.get("/api/optimize/runs?strategy=sma_crossover").get_json()
    assert run_id in [r["run_id"] for r in listing["runs"]]
    assert client.get("/api/optimize/runs/does-not-exist").status_code == 404


def test_bad_limit_is_a_400_not_a_500(client, run_id):
    assert client.get("/api/optimize/runs?limit=abc").status_code == 400
    assert client.get(f"/api/optimize/runs/{run_id}/results?offset=x").status_code == 400


def test_results_sorting_and_filtering(client, run_id):
    j = client.get(f"/api/optimize/runs/{run_id}/results?limit=5").get_json()
    assert j["total"] >= 12 and len(j["results"]) == 5
    scores = [r["objective_score"] for r in j["results"]]
    assert scores == sorted(scores, reverse=True)
    asc = client.get(f"/api/optimize/runs/{run_id}/results?sort=total_trades&order=asc"
                     "&limit=500").get_json()["results"]
    trades = [r["total_trades"] for r in asc if r["total_trades"] is not None]
    assert trades == sorted(trades)
    compliant = client.get(f"/api/optimize/runs/{run_id}/results?compliant=1&limit=500") \
        .get_json()["results"]
    assert compliant and all(r["constraints_met"] for r in compliant)


def test_heatmap_sensitivity_walk_forward(client, run_id):
    hm = client.get(f"/api/optimize/runs/{run_id}/heatmap?x=fast&y=slow").get_json()
    assert hm["heatmap"]["x_values"] == [5, 10, 15, 20]
    bad = client.get(f"/api/optimize/runs/{run_id}/heatmap?x=fast&y=nope")
    assert bad.status_code == 400 and "nope" in bad.get_json()["error"]
    sens = client.get(f"/api/optimize/runs/{run_id}/sensitivity").get_json()
    assert set(sens["sensitivity"]) == {"fast", "slow"}
    wf = client.get(f"/api/optimize/runs/{run_id}/walk-forward").get_json()
    assert wf["walk_forward"]["splits"]


def test_csv_export(client, run_id):
    r = client.get(f"/api/optimize/runs/{run_id}/export.csv")
    assert r.status_code == 200 and r.mimetype == "text/csv"
    assert f"optimization_sma_crossover_{run_id[:8]}.csv" in r.headers["Content-Disposition"]
    rows = list(csv.DictReader(io.StringIO(r.get_data(as_text=True))))
    assert len(rows) >= 12 and {"fast", "slow", "objective_score"} <= set(rows[0])


def test_unknown_action_is_404(client, run_id):
    assert client.post(f"/api/optimize/runs/{run_id}/explode").status_code == 404


def test_draft_run_then_start_and_cancel(env, client):
    doc = {**SMA_DOC, "method": "random", "methodSettings": {"nSamples": 300},
           "parameters": [{"name": "fast", "min": 2, "max": 40, "step": 1},
                          {"name": "slow", "min": 50, "max": 200, "step": 5}],
           "start": False}
    r = client.post("/api/optimize/runs", json=doc)
    rid = r.get_json()["run_id"]
    assert r.get_json()["run"]["status"] == "draft"
    assert client.post(f"/api/optimize/runs/{rid}/start").status_code == 200
    client.post(f"/api/optimize/runs/{rid}/cancel")
    assert env["service"].wait(rid, timeout=60)["status"] == "cancelled"
    assert client.delete(f"/api/optimize/runs/{rid}").status_code == 200
    assert client.get(f"/api/optimize/runs/{rid}").status_code == 404


# ---------------------------------------------------------------------------
# presets / apply / audit
# ---------------------------------------------------------------------------


def test_presets_crud(client, run_id):
    r = client.post(f"/api/optimize/runs/{run_id}/presets", json={"name": "Best so far"})
    assert r.status_code == 201
    pid = r.get_json()["preset"]["preset_id"]
    assert client.post(f"/api/optimize/runs/{run_id}/presets", json={}).status_code == 400
    listed = client.get("/api/optimize/presets?strategy=sma_crossover").get_json()["presets"]
    assert pid in [p["preset_id"] for p in listed]
    assert {"Conservative", "Moderate", "Aggressive"} <= {p["name"] for p in listed}
    manual = client.post("/api/optimize/presets", json={
        "strategy_id": "sma_crossover", "name": "Manual", "params": {"fast": 8, "slow": 80}})
    assert manual.status_code == 201
    mid = manual.get_json()["preset"]["preset_id"]
    patched = client.patch(f"/api/optimize/presets/{mid}", json={"name": "Renamed"})
    assert patched.get_json()["preset"]["name"] == "Renamed"
    assert client.delete(f"/api/optimize/presets/{mid}").status_code == 200
    assert client.patch(f"/api/optimize/presets/{mid}", json={"name": "x"}).status_code == 404


def test_apply_paper_and_rollback_via_api(env, client, run_id):
    fake = env["fake"]
    r = client.post(f"/api/optimize/runs/{run_id}/apply", json={"target": "paper"},
                    headers={"X-User": "alice"})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    iid = body["instance_id"]
    assert iid in fake.runners
    audit = client.get(f"/api/optimize/audit?run_id={run_id}").get_json()["audit"]
    entry = next(a for a in audit if a["audit_id"] == body["audit"]["audit_id"])
    assert entry["user_id"] == "alice" and entry["applied_to_mode"] == "paper"
    rb = client.post(f"/api/optimize/audit/{entry['audit_id']}/rollback")
    assert rb.status_code == 200 and iid not in fake.runners
    again = client.post(f"/api/optimize/audit/{entry['audit_id']}/rollback")
    assert again.status_code == 409


def test_live_apply_is_gated(client, run_id):
    r = client.post(f"/api/optimize/runs/{run_id}/apply", json={"target": "live"})
    assert r.status_code == 400 and "confirm_live" in r.get_json()["error"]
    r = client.post(f"/api/optimize/runs/{run_id}/apply",
                    json={"target": "live", "confirm_live": True})
    assert r.status_code in (400, 409)


# ---------------------------------------------------------------------------
# parity with the regular backtest endpoint
# ---------------------------------------------------------------------------


def test_optimizer_metrics_match_the_backtest_endpoint(client):
    from backtest.optimization.evaluator import evaluate

    params = {"fast": 10, "slow": 50}
    r = client.post("/api/backtest/run", json={
        "strategy": "sma_crossover", "symbol": "DEMO", "from_date": "2021-01-01",
        "to_date": "2023-12-31", "params": params, "timeframe": "1day"})
    assert r.status_code == 200
    final_equity = r.get_json()["metrics"]["final_equity"]
    candles = synthetic_loader(type("C", (), {"backtest": type("B", (), {
        "symbol": "DEMO", "start_date": "2021-01-01", "end_date": "2023-12-31",
        "timeframe": "1day"})})())
    out = evaluate(candles, {"capital": 100000.0, "symbol": "DEMO", "engine": "driver",
                             "timeframe": "1day", "selector_type": None},
                   "sma_crossover", params)
    expected = 100000.0 * (1 + out["metrics"]["total_return"])
    assert expected == pytest.approx(final_equity, abs=0.01)


# ---------------------------------------------------------------------------
# pages + nav + unavailable DB
# ---------------------------------------------------------------------------


def test_pages_render(client, run_id):
    setup = client.get("/optimize?strategy=sma_crossover")
    assert setup.status_code == 200
    html = setup.get_data(as_text=True)
    assert "optimize_setup.js" in html and "optimize_common.js" in html
    page = client.get(f"/optimize/runs/{run_id}")
    assert page.status_code == 200 and run_id in page.get_data(as_text=True)
    assert "optimize_run.js" in page.get_data(as_text=True)
    assert client.get(f"/optimize/runs/{run_id}/results").status_code == 200


def test_nav_has_optimize_link(client):
    html = client.get("/").get_data(as_text=True)
    assert 'data-key="optimize"' in html and 'href="/optimize"' in html


def test_static_assets_served(client):
    for path in ("/static/js/optimize_common.js", "/static/js/optimize_setup.js",
                 "/static/js/optimize_run.js"):
        assert client.get(path).status_code == 200, path


def test_503_when_no_database(monkeypatch):
    monkeypatch.setenv("OPTIMIZATION_DB", "off")
    app = create_app(source="synthetic")
    app.config.update(TESTING=True, OPTIMIZATION_SERVICE=None)
    c = app.test_client()
    r = c.get("/api/optimize/runs")
    assert r.status_code == 503 and "database" in r.get_json()["error"]
    assert c.get("/api/optimize/meta").status_code == 200  # static metadata still works
