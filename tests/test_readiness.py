"""Certification readiness — the §5 traffic light.

The panel is advisory, so the tests cannot assert "it blocks X". What they can
assert is the thing that actually matters: **a check that was never evaluated
must never be shown as passed.**

That is the failure mode worth pinning. A green/yellow/red table has no room
for "we don't know", so the easy mistake is to let a missing input fall through
to green — a Quick-Screen run has no cost shock, so "no cost shock" becomes
"no cost problem", and the reader gets a tick nobody earned on the most
promotional panel on the page. Forcing it red is the opposite mistake: the same
underlying fact gets penalised twice, once as Engine and again as Cost-Shock.
"""

from __future__ import annotations

import pytest

from backtest.engine.readiness import (
    GREEN,
    RED,
    UNKNOWN,
    YELLOW,
    build_readiness,
)


def _metrics(**over):
    base = {
        "closed_trades": 40,
        "profit_factor": 2.0,
        "max_drawdown_pct": -8.0,
        "total_return_pct": 30.0,
    }
    base.update(over)
    return base


def _provenance(**over):
    base = {
        "engine_canonical": True,
        "engine_label": "Fill-Exact (Canonical)",
        "data_source": "db",
        "data_source_label": "Database (Cached)",
        "data_source_real": True,
    }
    base.update(over)
    return base


def _benchmark(alpha: float = 0.05) -> dict:
    return {"available": True, "label": "Buy & Hold", "alpha": alpha, "beta": 0.7}


def _cost_shock(profitable: bool = True, **over) -> dict:
    base = {
        "available": True,
        "base_bps": 5.0,
        "base_bps_source": "configured",
        "status": "robust" if profitable else "broken",
        "scenarios": [
            {"multiple": 1, "slippage_bps": 5.0, "profitable": True},
            {"multiple": 2, "slippage_bps": 10.0, "profitable": profitable},
            {"multiple": 3, "slippage_bps": 15.0, "profitable": profitable},
        ],
    }
    base.update(over)
    return base


def _mc(p: float = 85.0) -> dict:
    return {"available": True, "bootstrap": {"profit_probability_pct": p}}


def _result(**over) -> dict:
    result = {
        "metrics": _metrics(),
        "provenance": _provenance(),
        "benchmark": _benchmark(),
        "cost_shock": _cost_shock(),
        "monte_carlo": _mc(),
    }
    result.update(over)
    return result


def _by_id(readiness) -> dict:
    return {c["id"]: c for c in readiness["checks"]}


# ---------------------------------------------------------------------------
# The all-green case — the one the PRD is really about
# ---------------------------------------------------------------------------


class TestAllGreen:
    def test_a_clean_real_run_passes_every_check(self):
        rd = build_readiness(_result())
        assert rd["verdict"] == "pass"
        assert rd["all_green"] is True
        assert rd["counts"][GREEN] == 8

    def test_the_pass_summary_is_the_prd_wording(self):
        rd = build_readiness(_result())
        assert "Basic checks passed" in rd["summary"]
        assert "Optimize" in rd["summary"]

    def test_all_green_unlocks_the_tune_this_button(self):
        """§5 gates the §6 button on this. §6 is not built yet; the flag is."""
        assert build_readiness(_result())["tune_this_available"] is True

    def test_the_panel_is_advisory_and_says_so(self):
        rd = build_readiness(_result())
        assert rd["advisory"] is True
        assert rd["gates_nothing"] is True

    def test_there_are_exactly_the_eight_prd_checks(self):
        ids = {c["id"] for c in build_readiness(_result())["checks"]}
        assert ids == {
            "engine",
            "data_source",
            "trade_count",
            "beats_benchmark",
            "cost_shock_2x",
            "profit_factor",
            "max_drawdown",
            "monte_carlo_p_profit",
        }


# ---------------------------------------------------------------------------
# The fourth state — the module's most important decision
# ---------------------------------------------------------------------------


class TestUnknownIsNeverGreen:
    def test_a_missing_cost_shock_is_unknown_not_green(self):
        rd = build_readiness(_result(cost_shock={"available": False, "reason": "quick screen"}))
        assert _by_id(rd)["cost_shock_2x"]["status"] == UNKNOWN
        assert "quick screen" in _by_id(rd)["cost_shock_2x"]["detail"]

    def test_a_missing_monte_carlo_is_unknown_not_green(self):
        rd = build_readiness(_result(monte_carlo={"available": False, "reason": "too few trades"}))
        check = _by_id(rd)["monte_carlo_p_profit"]
        assert check["status"] == UNKNOWN
        assert "too few trades" in check["detail"]

    def test_a_missing_benchmark_is_unknown_not_green(self):
        rd = build_readiness(_result(benchmark={"available": False, "reason": "no candles"}))
        check = _by_id(rd)["beats_benchmark"]
        assert check["status"] == UNKNOWN
        assert "no candles" in check["detail"]

    def test_unknown_checks_never_make_a_run_pass(self):
        """The headline guarantee: unknown is not a shortcut to green."""
        rd = build_readiness(
            _result(
                cost_shock={"available": False, "reason": "n/a"},
                monte_carlo={"available": False, "reason": "n/a"},
            )
        )
        assert rd["verdict"] != "pass"
        assert rd["all_green"] is False
        assert rd["tune_this_available"] is False

    def test_unknown_checks_are_named_in_the_payload(self):
        rd = build_readiness(
            _result(
                cost_shock={"available": False, "reason": "n/a"},
                monte_carlo={"available": False, "reason": "n/a"},
            )
        )
        assert set(rd["unproven"]) == {"Cost shock (2x)", "Monte Carlo P(profit)"}

    def test_a_gap_is_not_charged_to_the_run_as_a_failure(self):
        """
        A Quick-Screen run must not be penalised twice for the same fact.

        It is red on Engine; the cost shock it cannot run is unknown, not a
        second red. Otherwise the panel reads as "this strategy failed four
        checks" when it failed one and could not be measured on three.
        """
        rd = build_readiness(
            _result(
                provenance=_provenance(
                    engine_canonical=False, engine_label="Quick-Screen (Approximate)"
                ),
                cost_shock={
                    "available": False,
                    "reason": "cost shock runs on the canonical engine",
                },
            )
        )
        checks = _by_id(rd)
        assert checks["engine"]["status"] == RED
        assert checks["cost_shock_2x"]["status"] == UNKNOWN
        assert rd["counts"][RED] == 1

    def test_an_absent_block_is_unknown_rather_than_an_exception(self):
        """A payload missing every optional block must still render."""
        rd = build_readiness({"metrics": _metrics(), "provenance": _provenance()})
        assert len(rd["checks"]) == 8
        assert rd["counts"][UNKNOWN] >= 3


# ---------------------------------------------------------------------------
# A run that never traded
# ---------------------------------------------------------------------------


class TestNoTrades:
    @pytest.fixture()
    def empty(self):
        return build_readiness(
            _result(
                metrics=_metrics(closed_trades=0, profit_factor=None, max_drawdown_pct=0.0),
                monte_carlo={"available": False, "reason": "only 0 closed trades"},
            )
        )

    def test_trade_count_is_red(self, empty):
        assert _by_id(empty)["trade_count"]["status"] == RED

    def test_a_flat_curve_is_not_a_zero_percent_drawdown_pass(self, empty):
        """
        The most flattering false statement the panel could make.

        A strategy that never traded has a 0% max drawdown, and 0% is inside
        the "under 15%" green band. A green tick there would be the strongest
        possible endorsement of a result containing no trades at all.
        """
        check = _by_id(empty)["max_drawdown"]
        assert check["status"] == UNKNOWN
        assert "No closed trades" in check["detail"]

    def test_profit_factor_with_nothing_to_divide_is_unknown(self, empty):
        assert _by_id(empty)["profit_factor"]["status"] == UNKNOWN


# ---------------------------------------------------------------------------
# The bands
# ---------------------------------------------------------------------------


class TestBands:
    @pytest.mark.parametrize(
        "trades,expected",
        [(45, GREEN), (30, GREEN), (29, YELLOW), (20, YELLOW), (19, RED), (0, RED)],
    )
    def test_trade_count_bands(self, trades, expected):
        rd = build_readiness(_result(metrics=_metrics(closed_trades=trades)))
        assert _by_id(rd)["trade_count"]["status"] == expected

    @pytest.mark.parametrize(
        "pf,expected",
        [(1.5, GREEN), (2.0, GREEN), (1.49, YELLOW), (1.0, YELLOW), (0.99, RED), (0.4, RED)],
    )
    def test_profit_factor_bands(self, pf, expected):
        rd = build_readiness(_result(metrics=_metrics(profit_factor=pf)))
        assert _by_id(rd)["profit_factor"]["status"] == expected

    @pytest.mark.parametrize(
        "dd,expected",
        [
            (-5.0, GREEN),
            (-14.9, GREEN),
            (-15.0, YELLOW),
            (-25.0, YELLOW),
            (-25.1, RED),
            (-40.0, RED),
        ],
    )
    def test_drawdown_bands_read_depth_not_the_signed_number(self, dd, expected):
        """
        §5 writes the thresholds as positive depths ("< 15%"). The metric is
        negative, so a naive comparison passes every drawdown on earth.
        """
        rd = build_readiness(_result(metrics=_metrics(max_drawdown_pct=dd)))
        assert _by_id(rd)["max_drawdown"]["status"] == expected

    @pytest.mark.parametrize(
        "p,expected",
        [(75.0, GREEN), (100.0, GREEN), (74.9, YELLOW), (50.0, YELLOW), (49.9, RED), (5.0, RED)],
    )
    def test_monte_carlo_bands(self, p, expected):
        rd = build_readiness(_result(monte_carlo=_mc(p)))
        assert _by_id(rd)["monte_carlo_p_profit"]["status"] == expected


# ---------------------------------------------------------------------------
# The remaining checks
# ---------------------------------------------------------------------------


class TestIndividualChecks:
    def test_quick_screen_engine_is_red(self):
        rd = build_readiness(
            _result(
                provenance=_provenance(
                    engine_canonical=False, engine_label="Quick-Screen (Approximate)"
                )
            )
        )
        assert _by_id(rd)["engine"]["status"] == RED

    @pytest.mark.parametrize(
        "source,label,expected",
        [
            ("db", "Database (Cached)", GREEN),
            ("mstock", "MStock Live", GREEN),
            ("csv", "CSV File", YELLOW),
            ("synthetic", "Synthetic", RED),
        ],
    )
    def test_data_source_bands(self, source, label, expected):
        real = source in {"db", "mstock"}
        rd = build_readiness(
            _result(
                provenance=_provenance(
                    data_source=source, data_source_label=label, data_source_real=real
                )
            )
        )
        assert _by_id(rd)["data_source"]["status"] == expected

    def test_beating_the_benchmark_is_positive_alpha(self):
        rd = build_readiness(_result(benchmark=_benchmark(alpha=0.06)))
        check = _by_id(rd)["beats_benchmark"]
        assert check["status"] == GREEN
        assert "+6.00%" in check["value"]

    def test_being_beaten_by_the_benchmark_is_red(self):
        check = _by_id(build_readiness(_result(benchmark=_benchmark(alpha=-0.04))))[
            "beats_benchmark"
        ]
        assert check["status"] == RED

    def test_exactly_matching_the_benchmark_is_not_a_pass(self):
        """Alpha of zero is 'no better than the index', not 'better'."""
        check = _by_id(build_readiness(_result(benchmark=_benchmark(alpha=0.0))))["beats_benchmark"]
        assert check["status"] == RED

    def test_a_losing_cost_shock_is_red(self):
        rd = build_readiness(_result(cost_shock=_cost_shock(profitable=False)))
        assert _by_id(rd)["cost_shock_2x"]["status"] == RED

    def test_a_cost_shock_on_default_slippage_says_so(self):
        """
        A green tick here would imply the strategy survived doubled costs it
        never paid. The run was frictionless; the base is a stand-in.
        """
        cs = _cost_shock()
        cs["base_bps_source"] = "default"
        check = _by_id(build_readiness(_result(cost_shock=cs)))["cost_shock_2x"]
        assert check["status"] == GREEN
        assert "no slippage" in check["value"]

    def test_the_cost_shock_reads_the_two_x_row_not_the_last_row(self):
        """2x profitable but 3x not: §5 grades on 2x, so this is green."""
        cs = _cost_shock(profitable=True)
        cs["scenarios"][2]["profitable"] = False
        assert _by_id(build_readiness(_result(cost_shock=cs)))["cost_shock_2x"]["status"] == GREEN


# ---------------------------------------------------------------------------
# The summary line
# ---------------------------------------------------------------------------


class TestSummary:
    def test_any_red_says_something_failed(self):
        rd = build_readiness(_result(metrics=_metrics(closed_trades=5)))
        assert rd["verdict"] == "fail"
        assert "failed" in rd["summary"]
        assert rd["red_flags"]

    def test_the_middle_case_does_not_claim_success(self):
        """
        The PRD supplies a line for "any red" and one for "all green" and
        nothing between. Saying "basic checks passed" in the gap would be the
        one thing a reader of this panel is entitled to rely on.
        """
        rd = build_readiness(_result(metrics=_metrics(closed_trades=25)))
        assert rd["verdict"] == "incomplete"
        assert "Basic checks passed" not in rd["summary"]
        assert "weak" in rd["summary"]

    def test_the_middle_case_names_the_unproven_count(self):
        rd = build_readiness(
            _result(
                metrics=_metrics(closed_trades=25),
                monte_carlo={"available": False, "reason": "thin sample"},
            )
        )
        assert rd["verdict"] == "incomplete"
        assert "unproven" in rd["summary"]

    def test_the_middle_case_still_says_what_is_missing(self):
        rd = build_readiness(_result(metrics=_metrics(closed_trades=25)))
        assert "widen the date range" in rd["summary"]

    def test_counts_always_account_for_every_check(self):
        for result in (
            _result(),
            _result(metrics=_metrics(closed_trades=3)),
            _result(cost_shock={"available": False}, monte_carlo={"available": False}),
            {"metrics": {}, "provenance": {}},
        ):
            rd = build_readiness(result)
            assert sum(rd["counts"].values()) == len(rd["checks"]) == 8

    def test_every_check_carries_a_label_value_and_detail(self):
        """An unevaluated check with no reason is a mystery blank."""
        for c in build_readiness(_result(cost_shock={"available": False}))["checks"]:
            assert c["label"] and c["value"] and c["detail"], c
