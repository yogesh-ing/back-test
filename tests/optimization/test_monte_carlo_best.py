"""Monte Carlo on the winning result — PRD backTest-enhance Part 2 §4.

Walk-forward asks whether the *parameters* generalise across time. This asks
whether the *trade sequence* that produced the winner is a lucky ordering.
Different questions about the same run.

The engine is pinned in ``tests/test_run_checks.py``; what is pinned here is
that the right thing gets resampled — the winner, over the run's own candles
and its own config — and that the awkward cases refuse rather than invent.
"""

from __future__ import annotations

import copy

import pytest

from backtest.optimization.service import OptimizationError
from backtest.web.app import create_app


@pytest.fixture()
def client(service):
    """A Flask client over the SAME service the run was created on.

    Two services means two in-memory databases, and a run submitted through
    one is simply not there in the other — a 404 that looks like a routing
    bug and is really a fixture bug.
    """
    app = create_app(source="synthetic")
    app.config.update(TESTING=True, OPTIMIZATION_SERVICE=service)
    return app.test_client()


@pytest.fixture()
def completed(service, sma_doc):
    run = service.wait(service.submit(copy.deepcopy(sma_doc))["run_id"], timeout=120)
    assert run["status"] == "completed"
    return run


class TestItRunsOnTheWinner:
    def test_it_returns_a_block(self, service, completed):
        mc = service.monte_carlo_best(completed["run_id"], simulations=200)
        assert mc["available"] is True
        assert mc["trades"] > 0
        assert mc["simulations"] == 200

    def test_the_params_are_the_winners(self, service, completed):
        """The whole point. Resampling a losing candidate proves nothing."""
        mc = service.monte_carlo_best(completed["run_id"], simulations=200)
        assert mc["params"] == completed["best_params"]

    def test_it_is_repeatable(self, service, completed):
        """Seeded: a page that changes on refresh reads as a bug in the numbers."""
        a = service.monte_carlo_best(completed["run_id"], simulations=200)
        b = service.monte_carlo_best(completed["run_id"], simulations=200)
        assert a["bootstrap"] == b["bootstrap"]
        assert a["fan"] == b["fan"]

    def test_the_fan_chart_data_is_present(self, service, completed):
        mc = service.monte_carlo_best(completed["run_id"], simulations=200)
        fan = mc["fan"]
        assert fan["points"] > 1
        assert set(fan["bands"]) == {"5", "25", "50", "75", "95"}
        for band in fan["bands"].values():
            assert len(band) == fan["points"]

    def test_the_fan_starts_at_the_starting_capital(self, service, completed):
        mc = service.monte_carlo_best(completed["run_id"], simulations=200)
        for band in mc["fan"]["bands"].values():
            assert band[0] == pytest.approx(mc["starting_capital"], abs=0.01)

    def test_the_fan_bands_are_ordered(self, service, completed):
        """p5 must be below p50 below p95 at every point, or the chart is lying."""
        bands = service.monte_carlo_best(completed["run_id"], simulations=200)["fan"]["bands"]
        keys = ["5", "25", "50", "75", "95"]
        for i in range(len(bands["5"])):
            values = [bands[k][i] for k in keys]
            assert values == sorted(values), f"bands cross at point {i}"


class TestItRefusesRatherThanInvents:
    def test_an_unknown_run(self, service):
        with pytest.raises(OptimizationError) as exc:
            service.monte_carlo_best("00000000-0000-0000-0000-000000000000")
        assert exc.value.status == 404

    def test_a_run_that_has_not_finished(self, service, sma_doc):
        run = service.submit(copy.deepcopy(sma_doc), start=False)
        with pytest.raises(OptimizationError) as exc:
            service.monte_carlo_best(run["run_id"])
        assert exc.value.status == 409
        assert "not finished" in str(exc.value)

    def test_a_run_with_no_valid_result(self, service, sma_doc, monkeypatch):
        """No winner means nothing to resample. Fabricating a default would be
        worse than refusing."""
        doc = copy.deepcopy(sma_doc)
        # A constraint nothing can satisfy: no valid result, but the run still
        # completes normally.
        doc["constraints"] = [{"metric": "min_trades", "operator": ">=", "value": 10_000}]
        run = service.wait(service.submit(doc)["run_id"], timeout=120)
        assert run["status"] == "completed"
        with pytest.raises(OptimizationError):
            service.monte_carlo_best(run["run_id"])


class TestTheWiring:
    def test_the_endpoint_returns_the_block(self, client, completed):
        r = client.post(
            f"/api/optimize/runs/{completed['run_id']}/monte-carlo", json={"simulations": 200}
        )
        assert r.status_code == 200
        mc = r.get_json()["monte_carlo"]
        assert mc["available"] is True
        assert mc["params"] == completed["best_params"]

    def test_the_endpoint_defaults_to_a_thousand_simulations(self, client, completed):
        r = client.post(f"/api/optimize/runs/{completed['run_id']}/monte-carlo", json={})
        assert r.get_json()["monte_carlo"]["simulations"] == 1000

    @pytest.mark.parametrize("bad", [0, 1, 50_001, "many", None])
    def test_the_endpoint_rejects_a_silly_simulation_count(self, client, completed, bad):
        r = client.post(
            f"/api/optimize/runs/{completed['run_id']}/monte-carlo", json={"simulations": bad}
        )
        assert r.status_code == 400
        assert "simulations" in r.get_json()["error"]

    def test_the_endpoint_reports_an_unknown_run(self, client):
        r = client.post(
            "/api/optimize/runs/00000000-0000-0000-0000-000000000000/monte-carlo", json={}
        )
        assert r.status_code == 404

    def test_it_is_not_blocked_by_the_data_source_policy(self, client, completed):
        """
        The candles were already read to produce this run, and the resampling
        is arithmetic on trades that exist. Refusing it would remove a check
        on a result the user can already see.
        """
        r = client.post(
            f"/api/optimize/runs/{completed['run_id']}/monte-carlo", json={"simulations": 100}
        )
        assert r.get_json().get("code") != "data_source_disabled"
        assert r.status_code == 200


class TestTheApplyGate:
    """PRD Part 2 §4 — a visible flag that must be acknowledged, not a block.

    Monte Carlo is opt-in, so a *hard* gate would make Apply depend on a check
    that may never have been run. The requirement lives in the browser; what
    the server does is record honestly what was acknowledged.
    """

    def test_a_paper_apply_is_not_blocked_by_a_missing_check(self, service, completed):
        out = service.apply(completed["run_id"], target="none")
        assert out["audit"]["action"] in ("record", "record_only", "apply")

    def test_the_acknowledgement_and_the_number_are_recorded(self, service, completed):
        out = service.apply(
            completed["run_id"],
            target="none",
            monte_carlo_acknowledged=True,
            monte_carlo_profit_probability=42.5,
        )
        mc = out["audit"]["action_details"]["monte_carlo"]
        assert mc["acknowledged"] is True
        assert mc["profit_probability_pct"] == pytest.approx(42.5)

    def test_an_unrun_check_is_recorded_as_such(self, service, completed):
        """Absent must not read as passed."""
        out = service.apply(completed["run_id"], target="none")
        mc = out["audit"]["action_details"]["monte_carlo"]
        assert mc["acknowledged"] is False
        assert mc["profit_probability_pct"] is None

    def test_the_flag_does_not_block_paper(self, service, completed, fake_manager):
        out = service.apply(
            completed["run_id"],
            target="paper",
            monte_carlo_acknowledged=True,
            monte_carlo_profit_probability=12.0,
        )
        assert out["instance_id"] in fake_manager.runners
