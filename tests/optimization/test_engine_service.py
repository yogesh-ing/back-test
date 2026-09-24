"""Evaluator, store and service — real backtests on synthetic data, SQLite store."""

from __future__ import annotations

import threading

import pytest

from backtest.optimization.config import parse_config
from backtest.optimization.evaluator import (
    Cancelled,
    EvalWindow,
    Evaluator,
    evaluate,
    split_params,
)
from backtest.optimization.service import OptimizationError, params_diff
from backtest.optimization.store import SORTABLE, clean_json
from backtest.optimization.walk_forward import run_walk_forward

from .support import candles as _candles

SETTINGS = {"capital": 100000.0, "symbol": "DEMO", "engine": "driver", "timeframe": "1day",
            "selector_type": None}


@pytest.fixture(scope="module")
def candles():
    return _candles("DEMO", "2021-01-01", "2023-12-31", "1day")


# ---------------------------------------------------------------------------
# evaluator
# ---------------------------------------------------------------------------


def test_evaluate_returns_the_standard_metric_set(candles):
    out = evaluate(candles, SETTINGS, "sma_crossover", {"fast": 10, "slow": 50}, keep_curve=True)
    assert out["error"] is None
    m = out["metrics"]
    for key in ("sharpe", "sortino", "calmar", "total_return", "cagr", "max_drawdown",
                "win_rate", "profit_factor", "total_trades", "expectancy"):
        assert key in m, key
    assert m["max_drawdown"] <= 0 and 0 <= m["win_rate"] <= 100
    assert out["curve"] and len(out["curve"]) <= 401


def test_evaluate_never_raises(candles):
    out = evaluate(candles, SETTINGS, "sma_crossover", {"fast": "x", "slow": 50})
    assert out["error"] and out["metrics"] == {} and "traceback" in out


def test_window_scores_only_the_window_but_warms_up(candles):
    w = EvalWindow(start="2023-01-01", end="2023-12-31", warmup_from="2022-06-01")
    out = evaluate(candles, SETTINGS, "sma_crossover", {"fast": 10, "slow": 50}, window=w,
                   keep_curve=True)
    assert out["error"] is None
    assert out["curve"][0][0] >= "2023-01-01"


def test_split_params_separates_engine_knobs():
    assert split_params({"fast": 5, "engine.delta_target": 0.4}) == (
        {"fast": 5}, {"delta_target": 0.4})


def test_evaluator_caches_and_cancels(candles):
    cancel = threading.Event()
    seen = []
    with Evaluator(candles, SETTINGS, "sma_crossover", workers=1, cancel_event=cancel) as ev:
        pts = [{"fast": 5, "slow": 50}, {"fast": 10, "slow": 50}]
        ev.evaluate_batch(pts, on_result=lambda p, fresh: seen.append(fresh))
        ev.evaluate_batch(pts, on_result=lambda p, fresh: seen.append(fresh))
        assert seen == [True, True, False, False] and ev.evaluations == 2
        assert ev.cached({"slow": 50, "fast": 5}) is not None  # key is order-independent
        cancel.set()
        with pytest.raises(Cancelled):
            ev.evaluate_batch([{"fast": 15, "slow": 50}])


def test_parallel_pool_matches_inline(candles):
    pts = [{"fast": f, "slow": 60} for f in (5, 10, 15, 20)]
    with Evaluator(candles, SETTINGS, "sma_crossover", workers=1) as ev:
        inline = [r["metrics"]["sharpe"] for r in ev.evaluate_batch(pts)]
    with Evaluator(candles, SETTINGS, "sma_crossover", workers=2) as ev:
        pooled = [r["metrics"]["sharpe"] for r in ev.evaluate_batch(pts)]
    assert inline == pytest.approx(pooled)


def test_options_engine_smoke():
    nifty = _candles("NIFTY", "2024-01-01", "2024-06-30", "1day")
    settings = {**SETTINGS, "symbol": "NIFTY", "engine": "options"}
    out = evaluate(nifty, settings, "directional_options",
                   {"scale_points": 2, "engine.max_open_structures": 1})
    assert out["error"] is None, out.get("error")
    assert "total_trades" in out["metrics"]


def test_walk_forward_end_to_end(candles):
    cfg = parse_config({
        "strategyId": "sma_crossover", "method": "grid",
        "parameters": [{"name": "fast", "min": 5, "max": 15, "step": 5},
                       {"name": "slow", "min": 40, "max": 60, "step": 20}],
        "backtestConfig": {"startDate": "2021-01-01", "endDate": "2023-12-31"},
        "walkForward": {"enabled": True, "trainPeriodDays": 365, "testPeriodDays": 180,
                        "stepDays": 180},
    })
    with Evaluator(candles, SETTINGS, "sma_crossover", workers=1) as ev:
        report = run_walk_forward(cfg, ev)
    assert report["n_splits"] >= 3
    assert all(r["train_score"] is not None for r in report["splits"])
    assert isinstance(report["overfitted"], bool) and report["verdict"]


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------


def test_store_seeds_default_presets_idempotently(store):
    store.ensure_schema()
    defaults = [p for p in store.list_presets() if p["strategy_id"] == "default"]
    assert sorted(p["name"] for p in defaults) == ["Aggressive", "Conservative", "Moderate"]
    assert store.list_presets("sma_crossover", include_defaults=False) == []


def test_store_run_results_roundtrip(store):
    rid = store.create_run(strategy_id="sma_crossover", objective="sharpe", method="grid",
                           param_space=[], constraints=[], backtest_config={},
                           walk_forward_enabled=False, walk_forward_config={},
                           total_combinations=2)
    rows = [
        {"params": {"fast": 5}, "score": 1.2, "constraints_met": True,
         "metrics": {"sharpe": 1.2, "total_return": 0.1, "max_drawdown": -0.05,
                     "total_trades": 20, "win_rate": 55.0}},
        {"params": {"fast": 10}, "score": float("inf"), "constraints_met": False,
         "violations": [{"metric": "min_trades"}],
         "metrics": {"sharpe": float("nan"), "total_trades": 1}},
    ]
    store.save_results(rid, rows)
    items, total = store.get_results(rid, sort="objective_score", order="desc")
    assert total == 2
    # +inf saturates rather than violating NOT NULL
    assert items[0]["params"] == {"fast": 10} and items[0]["objective_score"] >= 999_999
    assert items[1]["params"] == {"fast": 5} and items[1]["win_rate"] == 55.0
    assert store.get_results(rid, compliant_only=True)[1] == 1
    by_trades, _ = store.get_results(rid, sort="total_trades", order="asc")
    assert [r["total_trades"] for r in by_trades] == [1, 20]
    assert "sharpe" in SORTABLE
    assert store.delete_run(rid) and store.get_results(rid)[1] == 0  # cascade


def test_clean_json_strips_non_finite():
    assert clean_json({"a": float("nan"), "b": [float("inf"), 1.5]}) == {"a": None,
                                                                         "b": [None, 1.5]}


def test_fail_stale_runs(store):
    rid = store.create_run(strategy_id="sma_crossover", objective="sharpe", method="grid",
                           param_space=[], constraints=[], backtest_config={},
                           walk_forward_enabled=False, walk_forward_config={},
                           total_combinations=1, status="running")
    assert store.fail_stale_runs(stale_after_seconds=-1) == 1
    run = store.get_run(rid)
    assert run["status"] == "failed" and "interrupted" in run["error_message"]


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------


def test_estimate(service, sma_doc):
    est = service.estimate(service.parse(sma_doc))
    assert est["grid_size"] == 12 and est["search_evaluations"] == 12
    assert est["total_evaluations"] >= 13 and est["workers"] == 1
    assert est["estimated_seconds"] > 0


def test_full_run_lifecycle(service, store, sma_doc):
    sma_doc["walkForward"] = {"enabled": True, "trainPeriodDays": 365, "testPeriodDays": 180,
                              "stepDays": 180, "maxEvalsPerSplit": 6}
    status = service.submit(sma_doc, created_by="tester")
    run = service.wait(status["run_id"], timeout=120)
    assert run["status"] == "completed", run.get("error_message")
    assert run["tested_combinations"] == 12 and 0 < run["valid_combinations"] <= 12
    assert run["best_params"]["fast"] in (5, 10, 15, 20)
    assert run["baseline_params"] == {"fast": 20, "slow": 50}
    assert run["baseline_score"] is not None
    assert run["walk_forward_results"]["n_splits"] >= 3
    assert isinstance(run["overfitted"], bool)
    assert run["robustness_score"] is not None
    analysis = run["analysis"]
    assert set(analysis["sensitivity"]) == {"fast", "slow"}
    assert analysis["stats"]["errors"] == 0

    items, total = store.get_results(run["run_id"], limit=50, compliant_only=True)
    assert total == run["valid_combinations"] or total >= run["valid_combinations"]
    assert items[0]["objective_score"] == pytest.approx(run["best_score"])

    hm = service.heatmap(run["run_id"], "fast", "slow")
    assert hm["x_values"] == [5, 10, 15, 20] and hm["y_values"] == [40, 70, 100]
    with pytest.raises(OptimizationError) as exc:
        service.heatmap(run["run_id"], "fast", "nope")
    assert "nope" in str(exc.value) and "fast" not in str(exc.value).split(":")[-1]

    # rerun copies the config (with overrides)
    again = service.rerun(run["run_id"], overrides={"objectiveFunction": "calmar",
                                                    "walkForward": {"enabled": False}})
    rerun = service.wait(again["run_id"], timeout=120)
    assert rerun["status"] == "completed" and rerun["objective_function"] == "calmar"
    assert service.config_from_run(rerun).grid_size() == 12


def test_failed_loader_marks_run_failed(store, fake_manager, sma_doc):
    from backtest.optimization.service import OptimizationService

    svc = OptimizationService(store, workers=1, loader=lambda cfg: None,
                              manager_getter=lambda: fake_manager)
    run = svc.wait(svc.submit(sma_doc)["run_id"], timeout=30)
    assert run["status"] == "failed" and "no candles" in run["error_message"]


def test_cancel_mid_run(service, sma_doc):
    sma_doc["method"] = "random"
    sma_doc["parameters"] = [{"name": "fast", "min": 2, "max": 40, "step": 1},
                             {"name": "slow", "min": 50, "max": 200, "step": 5}]
    sma_doc["methodSettings"] = {"nSamples": 400}
    rid = service.submit(sma_doc)["run_id"]
    service.cancel(rid)
    run = service.wait(rid, timeout=60)
    assert run["status"] == "cancelled"
    with pytest.raises(OptimizationError):
        service.cancel(rid)  # not running any more


@pytest.fixture()
def finished_run(service, sma_doc):
    return service.wait(service.submit(sma_doc)["run_id"], timeout=120)


def test_apply_none_records_preset_and_audit(service, store, finished_run):
    out = service.apply(finished_run["run_id"], target="none", user_id="alice")
    assert out["instance_id"] is None
    audit = out["audit"]
    assert audit["action"] == "apply" and audit["new_params"] == finished_run["best_params"]
    assert audit["old_params"] == {"fast": 20, "slow": 50}
    preset = store.get_preset(out["preset"]["preset_id"])
    assert preset["params"] == finished_run["best_params"] and preset["last_applied_at"]
    rb = service.rollback(audit["audit_id"])
    assert rb["action"] == "record_only"


def test_apply_paper_spawns_runner_and_rollback_removes_it(service, fake_manager, finished_run):
    out = service.apply(finished_run["run_id"], target="paper")
    iid = out["instance_id"]
    assert iid in fake_manager.runners
    cfg = fake_manager.runners[iid].config
    assert cfg.strategy_name == "sma_crossover" and cfg.mode == "paper"
    assert cfg.strategy_params == finished_run["best_params"]
    assert any(a.startswith("OPTIMIZE_APPLY") for a in fake_manager.audit)
    rb = service.rollback(out["audit"]["audit_id"])
    assert rb["action"] == "removed" and iid not in fake_manager.runners
    with pytest.raises(OptimizationError, match="already rolled back"):
        service.rollback(out["audit"]["audit_id"])


def test_apply_to_existing_runner_restarts_and_rollback_restores(
        service, fake_manager, finished_run):
    from backtest.forward.paper_runner import RunnerConfig

    iid = fake_manager.add_runner(RunnerConfig(
        name="mine", strategy_name="sma_crossover", allocated_capital=100000,
        symbols=["DEMO"], timeframe="1day", strategy_params={"fast": 7, "slow": 77},
        mode="paper", source="synthetic"))
    params = {"fast": 15, "slow": 70}
    out = service.apply(finished_run["run_id"], target="paper", instance_id=iid, params=params)
    new_id = out["instance_id"]
    assert iid not in fake_manager.runners
    assert fake_manager.runners[new_id].config.strategy_params == params
    assert ("flatten", iid) in fake_manager.calls
    assert out["audit"]["old_params"] == {"fast": 7, "slow": 77}
    assert out["audit"]["params_diff"]["fast"] == {"old": 7, "new": 15, "change": 8.0}
    rb = service.rollback(out["audit"]["audit_id"])
    assert rb["action"] == "restart"
    restored = fake_manager.runners[rb["instance_id"]].config
    assert restored.strategy_params == {"fast": 7, "slow": 77}


def test_apply_gates(service, fake_manager, finished_run):
    rid = finished_run["run_id"]
    with pytest.raises(OptimizationError, match="confirm_live"):
        service.apply(rid, target="live")
    with pytest.raises(OptimizationError, match="walk-forward"):
        service.apply(rid, target="live", confirm_live=True)
    with pytest.raises(OptimizationError, match="instance_id"):
        service.apply(rid, target="live", confirm_live=True, allow_unvalidated=True)
    with pytest.raises(OptimizationError, match="unknown parameter"):
        service.apply(rid, target="none", params={"bogus": 1})
    with pytest.raises(OptimizationError, match="target must be"):
        service.apply(rid, target="prod")
    with pytest.raises(OptimizationError, match="not found"):
        service.apply(rid, target="paper", instance_id="missing")


def test_save_preset_from_run(service, store, finished_run):
    preset = service.save_preset_from_run(finished_run["run_id"], name="My best")
    assert preset["params"] == finished_run["best_params"]
    assert preset["source"] == "optimization"
    assert any(p["preset_id"] == preset["preset_id"]
               for p in store.list_presets("sma_crossover"))


def test_params_diff():
    assert params_diff({"a": 1, "b": 2}, {"a": 1, "b": 3, "c": 4}) == {
        "b": {"old": 2, "new": 3, "change": 1.0}, "c": {"old": None, "new": 4}}
