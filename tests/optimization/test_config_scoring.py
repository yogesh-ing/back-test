"""Optimization config parsing/validation + objectives/constraints."""

from __future__ import annotations

import pytest

from backtest.optimization.config import (
    MAX_OPTIMIZED_PARAMS,
    ConfigValidationError,
    ParameterSpec,
    default_space,
    parse_config,
)
from backtest.optimization.scoring import (
    FAILED_SCORE,
    check_constraints,
    compliance_report,
    objective_score,
    violation_label,
)


def _doc(**over):
    doc = {
        "strategyId": "sma_crossover",
        "objectiveFunction": "sharpe",
        "method": "grid",
        "parameters": [
            {"name": "fast", "min": 5, "max": 20, "step": 5},
            {"name": "slow", "min": 30, "max": 90, "step": 30},
        ],
        "constraints": [],
        "backtestConfig": {"startDate": "2022-01-01", "endDate": "2023-12-31"},
    }
    doc.update(over)
    return doc


# ---------------------------------------------------------------------------
# ParameterSpec
# ---------------------------------------------------------------------------


def test_float_grid_is_decimal_exact():
    spec = ParameterSpec("x", "float", 0.1, 0.5, 0.1, 0.3)
    assert spec.values() == [0.1, 0.2, 0.3, 0.4, 0.5]  # no 0.30000000000000004
    assert spec.size() == 5


def test_int_grid_and_snap():
    spec = ParameterSpec("n", "int", 5, 50, 5, 20)
    assert spec.values()[:3] == [5, 10, 15] and spec.values()[-1] == 50
    assert spec.snap(12.4) == 10 and spec.snap(999) == 50 and spec.snap(-3) == 5
    assert spec.index_of(25) == 4


def test_fixed_param_contributes_one_value():
    spec = ParameterSpec("n", "int", 5, 50, 5, 20, optimize=False)
    assert spec.values() == [20] and spec.size() == 1


# ---------------------------------------------------------------------------
# parse_config
# ---------------------------------------------------------------------------


def test_parse_camel_case_prd_document():
    cfg = parse_config(_doc())
    assert cfg.strategy_id == "sma_crossover"
    assert cfg.grid_size() == 4 * 3
    assert cfg.backtest.engine == "driver"
    assert cfg.backtest.symbol == "DEMO"
    assert cfg.planned_evaluations() == 12
    assert cfg.baseline_params == {"fast": 20, "slow": 50}  # schema defaults


def test_snake_case_is_accepted_too():
    cfg = parse_config({
        "strategy_id": "sma_crossover", "objective": "calmar", "method": "random",
        "parameters": [{"name": "fast", "min": 5, "max": 20, "step": 5}],
        "backtest_config": {"start_date": "2022-01-01", "end_date": "2023-12-31"},
    })
    assert cfg.objective == "calmar" and cfg.method == "random"


def test_every_error_is_reported_at_once():
    with pytest.raises(ConfigValidationError) as exc:
        parse_config({
            "strategyId": "sma_crossover", "objectiveFunction": "nope", "method": "magic",
            "parameters": [{"name": "fast", "min": 1, "max": 20, "step": 0}],
            "backtestConfig": {"startDate": "2024-01-01", "endDate": "2023-01-01"},
        })
    errors = exc.value.errors
    expected = {"objectiveFunction", "method", "parameters.fast", "backtestConfig.endDate"}
    assert expected <= set(errors)
    # a bad parameter must not ALSO produce a misleading "select one" error
    assert "parameters" not in errors


def test_bounds_come_from_the_strategy_schema():
    with pytest.raises(ConfigValidationError) as exc:
        parse_config(_doc(parameters=[{"name": "fast", "min": 1, "max": 20, "step": 1}]))
    assert "allowed minimum" in exc.value.errors["parameters.fast"]


def test_unknown_param_and_unknown_strategy():
    with pytest.raises(ConfigValidationError) as exc:
        parse_config(_doc(parameters=[{"name": "bogus", "min": 1, "max": 2, "step": 1}]))
    assert "not a parameter" in exc.value.errors["parameters.bogus"]
    with pytest.raises(ConfigValidationError) as exc:
        parse_config(_doc(strategyId="does_not_exist"))
    assert "unknown strategy" in exc.value.errors["strategyId"]


def test_int_param_rejects_fractional_step():
    with pytest.raises(ConfigValidationError) as exc:
        parse_config(_doc(parameters=[{"name": "fast", "min": 5, "max": 20, "step": 2.5}]))
    assert "integer" in exc.value.errors["parameters.fast"]


def test_too_many_optimized_params_is_refused(monkeypatch):
    """No shipped strategy exposes >8 numeric params, so lower the cap to test the guard."""
    import backtest.optimization.config as config_mod

    assert MAX_OPTIMIZED_PARAMS == 8
    monkeypatch.setattr(config_mod, "MAX_OPTIMIZED_PARAMS", 1)
    with pytest.raises(ConfigValidationError) as exc:
        parse_config(_doc())
    assert "at most 1" in exc.value.errors["parameters"]


def test_grid_too_large_suggests_sampling_methods():
    doc = _doc(strategyId="rsi_reversion", parameters=[
        {"name": "period", "min": 2, "max": 50, "step": 1},
        {"name": "lower", "min": 1, "max": 49, "step": 1},
        {"name": "exit_level", "min": 50, "max": 90, "step": 1},
    ])
    with pytest.raises(ConfigValidationError) as exc:
        parse_config(doc)
    assert "random/bayesian/genetic" in exc.value.errors["method"]
    assert parse_config({**doc, "method": "bayesian"}).planned_evaluations() == 60


@pytest.mark.parametrize(
    "op,value,expected_op,expected_value",
    [
        (">", -0.15, "<", 15.0),   # PRD signed decimal: DD better than -15 %
        (">=", -15, "<=", 15.0),   # signed percent
        ("<", 15, "<", 15.0),      # magnitude percent (setup page)
        ("<", 0.2, "<", 20.0),     # magnitude decimal
    ],
)
def test_drawdown_constraint_normalisation(op, value, expected_op, expected_value):
    cfg = parse_config(_doc(constraints=[{"metric": "max_drawdown", "operator": op,
                                          "value": value}]))
    (c,) = cfg.constraints
    assert (c.operator, c.value) == (expected_op, expected_value)


def test_win_rate_fraction_and_disabled_constraints():
    cfg = parse_config(_doc(constraints=[
        {"metric": "win_rate", "operator": ">=", "value": 0.45},
        {"metric": "sharpe", "operator": ">", "value": 1, "enabled": False},
        {"metric": "total_trades", "operator": ">=", "value": 30},
    ]))
    got = [(c.metric, c.value) for c in cfg.constraints]
    assert got == [("win_rate", 45.0), ("min_trades", 30.0)]


def test_walk_forward_window_must_fit_the_period():
    with pytest.raises(ConfigValidationError) as exc:
        parse_config(_doc(walkForward={"enabled": True, "trainPeriodDays": 600,
                                       "testPeriodDays": 300}))
    assert "exceeds the backtest period" in exc.value.errors["walkForward.trainPeriodDays"]


def test_warnings_for_short_period_and_no_walk_forward():
    cfg = parse_config(_doc(backtestConfig={"startDate": "2024-01-01", "endDate": "2024-03-01"}))
    text = " ".join(cfg.warnings)
    assert "6 months" in text and "Walk-forward" in text


def test_option_strategy_defaults_to_the_options_engine_and_knobs():
    cfg = parse_config({
        "strategyId": "directional_options",
        "parameters": [{"name": "scale_points", "min": 1, "max": 3, "step": 1},
                       {"name": "engine.delta_target", "min": 0.3, "max": 0.5, "step": 0.1}],
        "backtestConfig": {"startDate": "2024-01-01", "endDate": "2024-12-31"},
    })
    assert cfg.backtest.engine == "options"
    assert cfg.backtest.symbol == "NIFTY"
    assert cfg.grid_size() == 9


def test_round_trip_through_to_dict():
    cfg = parse_config(_doc(walkForward={"enabled": True, "trainPeriodDays": 180,
                                         "testPeriodDays": 60, "stepDays": 60}))
    again = parse_config(cfg.to_dict())
    assert again.to_dict() == cfg.to_dict()


# ---------------------------------------------------------------------------
# default_space
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("strategy", ["sma_crossover", "rsi_reversion", "bollinger_reversion",
                                      "directional_options", "banknifty_straddle"])
def test_default_space_is_always_valid_and_contains_the_default(strategy):
    rows = default_space(strategy)
    assert rows and any(r["optimize"] for r in rows)
    for r in rows:
        spec = ParameterSpec(r["name"], r["type"], r["min"], r["max"], r["step"], r["current"])
        assert r["current"] in spec.values(), r
    cfg = parse_config({"strategyId": strategy, "parameters": rows, "method": "random",
                        "backtestConfig": {"startDate": "2023-01-01", "endDate": "2024-12-31"}})
    assert cfg.grid_size() >= 2


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------

METRICS = {"sharpe": 1.4, "total_return": 0.12, "max_drawdown": -0.083, "total_trades": 42,
           "win_rate": 55.0, "profit_factor": 1.8, "calmar": 1.1, "sortino": 2.0,
           "expectancy": 350.0}


def test_objective_score_and_failed_runs():
    assert objective_score(METRICS, "sharpe") == 1.4
    assert objective_score({"sharpe": float("nan")}, "sharpe") == 0.0
    assert objective_score({"error": "boom"}, "sharpe") == FAILED_SCORE
    assert objective_score({}, "sharpe") == FAILED_SCORE


def test_constraints_use_percent_units():
    cfg = parse_config(_doc(constraints=[
        {"metric": "max_drawdown", "operator": "<", "value": 10},
        {"metric": "min_trades", "operator": ">=", "value": 30},
        {"metric": "win_rate", "operator": ">=", "value": 60},
        {"metric": "total_return", "operator": ">", "value": 5},
    ]))
    violations = check_constraints(METRICS, cfg.constraints)
    assert [v["metric"] for v in violations] == ["win_rate"]
    assert violations[0]["actual"] == 55.0
    assert violation_label(violations) == "WinRate"
    report = compliance_report(METRICS, cfg.constraints)
    assert [r["passed"] for r in report] == [True, True, False, True]
    assert report[0]["actual"] == pytest.approx(8.3)


def test_a_failed_backtest_violates_everything():
    cfg = parse_config(_doc(constraints=[{"metric": "sharpe", "operator": ">", "value": 0}]))
    assert len(check_constraints({"error": "x"}, cfg.constraints)) == 1
