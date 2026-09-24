"""Search methods, analysis helpers and walk-forward maths (no backtests)."""

from __future__ import annotations

import math

import pytest

from backtest.optimization import analysis as an
from backtest.optimization.config import (
    Constraint,
    ParameterSpec,
    WalkForwardSettings,
    parse_config,
)
from backtest.optimization.methods import (
    SearchSpace,
    _erf,
    bayesian_search,
    genetic_search,
    grid_search,
    random_search,
    run_method,
)
from backtest.optimization.walk_forward import make_splits, scaled_constraints, summarize


def _space():
    return SearchSpace(
        specs=(ParameterSpec("a", "int", 0, 20, 1, 10),
               ParameterSpec("b", "float", 0.0, 1.0, 0.1, 0.5)),
        fixed={"c": 7},
    )


def _peak(params):
    """Smooth unimodal surface with its maximum at a=14, b=0.3."""
    return -((params["a"] - 14) / 10) ** 2 - (params["b"] - 0.3) ** 2 * 4


def _recorder():
    seen = []

    def evaluate(points):
        out = []
        for p in points:
            seen.append(p)
            out.append((p, _peak(p)))
        return out

    return seen, evaluate


def test_search_space_decode_and_grid():
    space = _space()
    assert space.total == 21 * 11
    assert space.point(space.decode(0)) == {"c": 7, "a": 0, "b": 0.0}
    assert space.point(space.decode(space.total - 1)) == {"c": 7, "a": 20, "b": 1.0}
    assert len(list(space.iter_grid())) == space.total


def test_grid_visits_every_point_exactly_once():
    seen, evaluate = _recorder()
    grid_search(_space(), evaluate, batch_size=17)
    keys = {tuple(sorted(p.items())) for p in seen}
    assert len(seen) == len(keys) == 231
    assert all(p["c"] == 7 for p in seen)  # fixed params ride along


def test_random_is_distinct_and_seeded():
    s1, e1 = _recorder()
    s2, e2 = _recorder()
    random_search(_space(), e1, 40, seed=3)
    random_search(_space(), e2, 40, seed=3)
    assert s1 == s2
    assert len({tuple(sorted(p.items())) for p in s1}) == 40


def test_bayesian_beats_random_on_a_smooth_surface():
    best_bo, best_rs = [], []
    for seed in range(3):
        sb, eb = _recorder()
        bayesian_search(_space(), eb, n_calls=25, seed=seed, batch_size=2)
        assert len({tuple(sorted(p.items())) for p in sb}) == len(sb) == 25
        best_bo.append(max(_peak(p) for p in sb))
        sr, er = _recorder()
        random_search(_space(), er, 25, seed=seed)
        best_rs.append(max(_peak(p) for p in sr))
    assert sum(best_bo) / 3 >= sum(best_rs) / 3
    assert max(best_bo) > -0.05  # finds (near) the optimum


def test_genetic_respects_budget_and_improves():
    seen, evaluate = _recorder()
    genetic_search(_space(), evaluate, population=10, generations=5, seed=1)
    assert 10 <= len(seen) <= 50
    assert len({tuple(sorted(p.items())) for p in seen}) == len(seen)
    first_gen = max(_peak(p) for p in seen[:10])
    assert max(_peak(p) for p in seen) >= first_gen


def test_run_method_budget_turns_big_grids_into_sampling():
    cfg = parse_config({
        "strategyId": "sma_crossover",
        "parameters": [{"name": "fast", "min": 2, "max": 40, "step": 1},
                       {"name": "slow", "min": 50, "max": 200, "step": 10}],
        "backtestConfig": {"startDate": "2022-01-01", "endDate": "2023-12-31"},
    })
    seen, evaluate = _recorder_for_sma()
    run_method(cfg, SearchSpace.from_config(cfg), evaluate, budget=30)
    assert len(seen) == 30


def _recorder_for_sma():
    seen = []

    def evaluate(points):
        seen.extend(points)
        return [(p, -abs(p["fast"] - 10)) for p in points]

    return seen, evaluate


def test_erf_matches_math_erf():
    import numpy as np

    xs = np.linspace(-3, 3, 25)
    assert max(abs(_erf(xs) - np.array([math.erf(x) for x in xs]))) < 2e-7


# ---------------------------------------------------------------------------
# analysis
# ---------------------------------------------------------------------------


def _rows():
    rows = []
    for a in range(0, 21, 2):
        for b in (0.1, 0.3, 0.5):
            p = {"a": a, "b": b, "c": 7}
            s = _peak(p)
            rows.append({"params": p, "score": s, "constraints_met": a != 20,
                         "metrics": {"sharpe": s, "total_trades": 40}})
    rows.append({"params": {"a": 1, "b": 0.1, "c": 7}, "score": -1e6, "constraints_met": False,
                 "metrics": {}})  # failed run is ignored
    return rows


def test_heatmap_max_mean_and_slice():
    rows = _rows()
    h = an.heatmap(rows, "a", "b")
    assert h["x_values"] == list(range(0, 21, 2)) and h["y_values"] == [0.1, 0.3, 0.5]
    assert h["best"]["x"] == 14 and h["best"]["y"] == 0.3
    assert h["coverage"] == 1.0
    mean = an.heatmap(rows, "a", "b", agg="mean")
    assert mean["z"] == h["z"]  # one row per cell → mean == max
    sl = an.heatmap(rows, "a", "b", agg="slice", anchor={"a": 14, "b": 0.3, "c": 7})
    assert sl["anchor"]["c"] == 7
    compliant = an.heatmap(rows, "a", "b", compliant_only=True)
    assert 20 not in compliant["x_values"]
    with pytest.raises(ValueError):
        an.heatmap(rows, "a", "b", agg="median")


def test_sensitivity_plateau_vs_sharp_peak():
    flat = an.sensitivity_curve([1, 2, 3, 4, 5], [1.0, 1.0, 1.02, 1.0, 0.99], 3)
    assert flat["label"] == "stable" and flat["plateau"] == [1, 5] and flat["stability"] == 1.0
    sharp = an.sensitivity_curve([1, 2, 3, 4, 5], [0.1, 0.1, 2.0, 0.1, 0.1], 3)
    assert sharp["label"] == "sensitive" and sharp["plateau"] == [3, 3]
    none = an.sensitivity_curve([1, 2], [None, -1e6], 1)
    assert none["label"] == "unknown"


def test_sweep_values_thins_but_keeps_ends():
    spec = ParameterSpec("n", "int", 1, 100, 1, 50)
    vals = an.sweep_values(spec, 25)
    assert len(vals) <= 25 and vals[0] == 1 and vals[-1] == 100


def test_robustness_score_renormalises_missing_evidence():
    only_trades = an.robustness_score(None, None, None, {"total_trades": 30})
    assert only_trades["score"] == 10.0 and set(only_trades["components"]) == {"sample_size"}
    full = an.robustness_score({"a": {"stability": 0.5}}, {"efficiency": 0.8},
                               {"dispersion": {"a": 0.0}}, {"total_trades": 30})
    assert full["score"] == 10.0
    bad = an.robustness_score({"a": {"stability": 0.0}}, {"efficiency": -0.5},
                              {"dispersion": {"a": 0.5}}, {"total_trades": 0})
    assert bad["score"] == 0.0
    assert an.robustness_score(None, None, None, None)["score"] is None


def test_warning_signs():
    cfg = parse_config({
        "strategyId": "sma_crossover",
        "parameters": [{"name": "fast", "min": 5, "max": 20, "step": 5},
                       {"name": "slow", "min": 30, "max": 90, "step": 30}],
        "backtestConfig": {"startDate": "2022-01-01", "endDate": "2023-12-31"},
    })
    best = {"params": {"fast": 5, "slow": 60}, "score": 3.5,
            "metrics": {"sharpe": 3.5, "total_trades": 12}}
    codes = {w["code"] for w in an.warning_signs(
        cfg, best, {"slow": {"label": "sensitive", "stability": 0.0}}, None,
        baseline={"score": 0.5})}
    assert {"high_sharpe", "few_trades", "trades_per_param", "edge_fast", "sharp_peak_slow",
            "no_wf", "big_jump"} <= codes
    assert an.warning_signs(cfg, None, None, None)[0]["code"] == "no_valid"


def test_top_cluster():
    specs = (ParameterSpec("a", "int", 0, 20, 2, 10),
             ParameterSpec("b", "float", 0.1, 0.5, 0.2, 0.3))
    cl = an.top_cluster(_rows(), specs, top_n=5)
    assert cl["n"] == 5 and set(cl["dispersion"]) == {"a", "b"}


# ---------------------------------------------------------------------------
# walk-forward maths
# ---------------------------------------------------------------------------


def test_make_splits_prd_geometry():
    splits = make_splits("2024-01-01", "2024-12-31", 60, 30, 30)
    assert len(splits) == 10
    first = splits[0]
    assert (first.train_start, first.train_end, first.test_start, first.test_end) == (
        "2024-01-01", "2024-02-29", "2024-03-01", "2024-03-30")
    assert all(s.test_end <= "2024-12-31" for s in splits)
    assert splits[1].train_start == "2024-01-31"


def test_scaled_min_trades_constraint():
    cons = (Constraint("min_trades", ">=", 30.0), Constraint("sharpe", ">", 1.0))
    scaled = scaled_constraints(cons, 73, 365)
    assert scaled[0].value == 6.0 and scaled[1] == cons[1]


def _wf_cfg(objective="sharpe"):
    return parse_config({
        "strategyId": "sma_crossover", "objectiveFunction": objective,
        "parameters": [{"name": "fast", "min": 5, "max": 20, "step": 5}],
        "backtestConfig": {"startDate": "2022-01-01", "endDate": "2023-12-31"},
        "walkForward": {"enabled": True, "trainPeriodDays": 180, "testPeriodDays": 60,
                        "stepDays": 60},
    })


def test_summarize_ratio_objective_uses_degradation():
    rows = [{"params": {"fast": 5}, "train_score": 1.5, "test_score": 0.5, "degradation": 1.0},
            {"params": {"fast": 10}, "train_score": 1.2, "test_score": 0.4, "degradation": 0.8}]
    rep = summarize(_wf_cfg(), rows)
    assert rep["overfitted"] is True and rep["avg_degradation"] == pytest.approx(0.9)
    assert rep["efficiency"] == pytest.approx(0.45 / 1.35, abs=1e-3)
    assert rep["param_stability"]["fast"]["values"] == [5.0, 10.0]
    good = [{"params": {"fast": 5}, "train_score": 1.0, "test_score": 0.9, "degradation": 0.1}]
    assert summarize(_wf_cfg(), good)["overfitted"] is False


def test_summarize_non_ratio_objective_uses_efficiency():
    rows = [{"params": {"fast": 5}, "train_score": 0.2, "test_score": 0.15, "degradation": 0.05}]
    assert summarize(_wf_cfg("total_return"), rows)["overfitted"] is False
    rows = [{"params": {"fast": 5}, "train_score": 0.2, "test_score": 0.02, "degradation": 0.18}]
    rep = summarize(_wf_cfg("total_return"), rows)
    assert rep["overfitted"] is True and rep["overfit_rule"] == "efficiency < 0.5"


def test_summarize_with_no_scored_split():
    rep = summarize(_wf_cfg(), [{"params": None, "test_score": None}])
    assert rep["overfitted"] is None and "no scored splits" in rep["verdict"]
    assert WalkForwardSettings().overfit_threshold == 0.3
