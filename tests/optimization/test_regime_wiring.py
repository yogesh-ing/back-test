"""The regime breakdown on a real completed run — PRD Part 2 §6.1.

The maths is pinned in ``test_regimes.py``. This file pins that a run
actually carries the table, that the trade counts reconcile against the run's
own headline number, and that a failure to split does not cost the user their
search.
"""

from __future__ import annotations

import copy

import pytest


@pytest.fixture()
def finished_run_for(service, store, sma_doc):
    run = service.wait(service.submit(copy.deepcopy(sma_doc))["run_id"], timeout=120)
    return run, store.get_run(run["run_id"])


class TestItReachesTheRun:
    def test_a_completed_run_carries_the_breakdown(self, finished_run_for):
        run, stored = finished_run_for
        regimes = run["analysis"].get("regimes")
        assert regimes, "analysis must carry the regime breakdown"
        assert regimes["available"] is True
        assert regimes["periods"]

    def test_it_is_persisted(self, finished_run_for):
        _, stored = finished_run_for
        assert (stored.get("analysis") or {}).get("regimes", {}).get("available") is True

    def test_every_row_has_the_prds_four_numbers(self, finished_run_for):
        run, _ = finished_run_for
        for row in run["analysis"]["regimes"]["periods"]:
            assert {"return_pct", "sharpe", "max_drawdown_pct", "trades"} <= set(row)

    def test_trade_counts_reconcile_with_the_run(self, finished_run_for):
        """
        The per-period trade counts must sum to the run's own closed-trade
        number. They do not have to — a trade can straddle a boundary — but a
        silent mismatch would mean the table is counting something else.
        """
        run, _ = finished_run_for
        regimes = run["analysis"]["regimes"]
        counted = sum(p["trades"] or 0 for p in regimes["periods"])
        closed = run["best_metrics"]["closed_trades"]
        assert counted == closed, f"{counted} across periods vs {closed} closed overall"

    def test_the_bars_reconcile(self, finished_run_for):
        run, _ = finished_run_for
        regimes = run["analysis"]["regimes"]
        assert sum(p["bars"] for p in regimes["periods"]) == regimes["total_bars"]

    def test_coverage_is_reported(self, finished_run_for):
        """A run outside 2020-2024 must not look fully covered."""
        run, _ = finished_run_for
        assert 0.0 <= run["analysis"]["regimes"]["named_coverage_pct"] <= 100.0


class TestItCannotCostYouTheRun:
    """
    The breakdown is computed in the LAST step, after thousands of backtests.
    Failing the run there would throw away a real result over a cosmetic
    table, so the helper returns None and the run completes as normal.
    """

    @pytest.fixture()
    def cfg(self, service, sma_doc):
        return service.parse(sma_doc)

    @pytest.fixture()
    def candles(self, service, cfg):
        return service.loader(cfg)

    def test_an_evaluation_that_errored_yields_no_breakdown(
        self, service, cfg, candles, monkeypatch
    ):
        """How this actually degrades: evaluate() never raises, it returns an
        error payload with no regimes in it."""
        from backtest.optimization import evaluator as ev

        monkeypatch.setattr(ev, "evaluate", lambda *a, **k: {"error": "boom", "params": {}})
        out = service._regime_breakdown_for(cfg, {"params": {"fast": 5, "slow": 40}}, candles)
        assert out is None

    def test_a_helper_that_raises_never_propagates(self, service, cfg, candles, monkeypatch):
        """Defence in depth around a call that is already meant not to raise."""
        from backtest.optimization import evaluator as ev

        def boom(*_a, **_k):
            raise RuntimeError("no calendar for you")

        monkeypatch.setattr(ev, "evaluate", boom)
        out = service._regime_breakdown_for(cfg, {"params": {"fast": 5, "slow": 40}}, candles)
        assert out is None

    def test_a_run_with_no_result_has_no_breakdown(self, service, sma_doc):
        doc = copy.deepcopy(sma_doc)
        doc["constraints"] = [{"metric": "min_trades", "operator": ">=", "value": 10_000}]
        run = service.wait(service.submit(doc)["run_id"], timeout=120)
        assert run["status"] == "completed"
        assert run["analysis"]["regimes"] is None
