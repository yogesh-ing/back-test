"""Deflated Sharpe on a real completed run — PRD backTest-enhance Part 2 §3.

The maths is pinned in ``test_deflation.py``. This file pins the wiring: that
a finished run carries the statistic, that it counts the combinations it
actually tried, and that the gap warning reaches the warnings list.
"""

from __future__ import annotations

import copy

import pytest


@pytest.fixture()
def finished_run_for(service, store, sma_doc):
    """A completed run, plus the row as it was persisted.

    Two views on purpose: the run dict is what the service built in memory, the
    stored row is what a later reader actually gets. A value that reaches only
    the first is not persisted.
    """
    run = service.wait(service.submit(copy.deepcopy(sma_doc))["run_id"], timeout=120)
    return run, store.get_run(run["run_id"])


class TestItReachesTheRun:
    def test_a_completed_run_carries_the_statistic(self, finished_run_for):
        run, stored = finished_run_for
        assert run["status"] == "completed"
        dsr = (stored.get("analysis") or {}).get("deflated_sharpe")
        assert dsr, "analysis must carry the deflated Sharpe block"
        assert "deflated_sharpe" in dsr
        assert "probability" in dsr

    def test_the_column_is_persisted_alongside_robustness(self, store, finished_run_for):
        _, stored = finished_run_for
        assert "robustness_score" in stored
        assert "deflated_sharpe" in stored, "the sortable column must exist"

    def test_the_block_records_how_many_combinations_were_tried(self, finished_run_for):
        run, stored = finished_run_for
        dsr = stored["analysis"]["deflated_sharpe"]
        assert dsr["trials"] is not None
        # Trials is the number *tried*, so it is at least the number written.
        assert dsr["trials"] >= (stored.get("tested_combinations") or 0)

    def test_the_observations_come_from_the_candles(self, finished_run_for):
        _, stored = finished_run_for
        dsr = stored["analysis"]["deflated_sharpe"]
        assert dsr["observations"] == stored["analysis"]["stats"]["bars"]


class TestDegradationIsNotAFailure:
    def test_a_run_that_cannot_be_deflated_still_completes(self, service, sma_doc):
        """A missing statistic is a normal outcome. The run must not fail for
        want of one, and must not carry a confident-looking number."""
        doc = copy.deepcopy(sma_doc)
        # One combination means there is no multiple-testing to correct for.
        doc["parameters"] = [
            {"name": "fast", "type": "int", "optimize": True, "min": 20, "max": 20, "step": 1}
        ]
        doc["parameters"].append({"name": "slow", "type": "int", "optimize": False, "value": 50})
        run = service.wait(service.submit(doc)["run_id"], timeout=120)
        assert run["status"] == "completed"
        dsr = run["analysis"]["deflated_sharpe"]
        assert dsr["status"] == "insufficient_data"
        assert dsr["deflated_sharpe"] is None
        assert dsr["reason"]

    def test_the_block_always_has_the_same_keys(self, service, finished_run_for):
        """Callers render this without special-casing; a missing key in the
        degraded case would be a KeyError on exactly the runs that need
        explaining most."""
        _, stored = finished_run_for
        assert set(stored["analysis"]["deflated_sharpe"]) == {
            "deflated_sharpe",
            "probability",
            "expected_max_sharpe",
            "trials",
            "observations",
            "sharpe_variance",
            "observed_sharpe",
            "status",
            "reason",
        }


class TestTheGapWarning:
    def test_the_warning_joins_the_existing_ones(self, finished_run_for):
        _, stored = finished_run_for
        warnings = stored["analysis"]["warnings"]
        assert isinstance(warnings, list)
        # Whether this particular run trips the gap is data-dependent; what is
        # pinned is that the list is still well-formed either way.
        for w in warnings:
            assert set(w) >= {"level", "code", "message"}

    def test_a_wide_gap_is_detectable_end_to_end(self, service, sma_doc, monkeypatch):
        """The PRD's orange warning, reached through a real run.

        Both halves of the rule have to hold -- an observed Sharpe above 1.5
        and a deflated bar below 0.5 -- so both are forced. Forcing only one
        is how this test could pass while proving nothing.
        """
        from backtest.optimization import evaluator as ev
        from backtest.optimization import service as service_mod

        real_metrics = ev.standardize_metrics
        real_dsr = service_mod.deflated_sharpe

        def loud_metrics(*args, **kwargs):
            out = real_metrics(*args, **kwargs)
            out["sharpe"] = 2.4
            return out

        def rigged_dsr(*args, **kwargs):
            out = real_dsr(*args, **kwargs)
            if out.get("status") == "ok":
                out["deflated_sharpe"] = 0.2
            return out

        monkeypatch.setattr(ev, "standardize_metrics", loud_metrics)
        monkeypatch.setattr(service_mod, "deflated_sharpe", rigged_dsr)
        run = service.wait(service.submit(copy.deepcopy(sma_doc))["run_id"], timeout=120)

        assert run["analysis"]["deflated_sharpe"]["status"] == "ok"
        codes = [w["code"] for w in run["analysis"]["warnings"]]
        assert "deflation_gap" in codes, f"expected a deflation_gap warning, got {codes}"
        gap = next(w for w in run["analysis"]["warnings"] if w["code"] == "deflation_gap")
        assert gap["level"] == "warning"
        assert "combinations" in gap["message"]
