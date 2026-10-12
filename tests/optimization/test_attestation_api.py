"""The attestation over HTTP — PRD backTest-enhance Part 2 §2.

The record itself is pinned in ``test_attestation.py``. This file pins the two
places it is read back: the setup page's preview endpoint, and the run payload
that both the results page and the audit trail read.
"""

from __future__ import annotations

import copy

import pytest

from backtest.web.app import create_app


@pytest.fixture()
def client(service):
    """A Flask client over the SAME service the run was created on.

    Two services means two in-memory databases, and a run submitted through one
    is simply not there in the other — a 404 that looks like a routing bug and
    is really a fixture bug.
    """
    app = create_app(source="synthetic")
    app.config.update(TESTING=True, OPTIMIZATION_SERVICE=service)
    return app.test_client()


class TestThePreviewEndpoint:
    def test_it_answers_with_the_attestation_and_whether_the_run_may_start(
        self, client, unacknowledged
    ):
        res = client.post("/api/optimize/attestation", json=unacknowledged).get_json()
        assert res["success"] is True
        assert res["attestation"]["data_source"] == "synthetic"
        assert res["satisfied"] is False, "synthetic must not start unacknowledged"

    def test_it_names_what_has_to_be_ticked(self, client, unacknowledged):
        res = client.post("/api/optimize/attestation", json=unacknowledged).get_json()
        assert res["acknowledgement"].startswith("I understand this is synthetic")

    def test_it_never_reports_a_bar_count_it_has_not_measured(self, client, sma_doc):
        """The preview runs before the candles are fetched. A count here would
        be invented."""
        res = client.post("/api/optimize/attestation", json=sma_doc).get_json()
        assert res["attestation"]["bars_count"] is None

    def test_real_data_is_satisfied_with_no_tick(self, client, sma_doc):
        doc = sma_doc
        doc["backtestConfig"]["source"] = "db"
        res = client.post("/api/optimize/attestation", json=doc).get_json()
        assert res["satisfied"] is True
        assert res["attestation"]["requires_acknowledgement"] is False

    def test_an_invalid_config_is_a_400_not_a_traceback(self, client, sma_doc):
        doc = sma_doc
        doc["strategyId"] = "no_such_strategy"
        res = client.post("/api/optimize/attestation", json=doc)
        assert res.status_code == 400
        assert res.get_json()["success"] is False


class TestTheRefusal:
    def test_the_refusal_carries_a_code_not_just_a_sentence(self, client, unacknowledged):
        """So the setup page can point at the tick without matching on wording."""
        res = client.post("/api/optimize/runs", json=unacknowledged)
        assert res.status_code == 409
        body = res.get_json()
        assert body["code"] == "synthetic_data_not_acknowledged"
        assert body["data_source"] == "synthetic"

    def test_the_run_starts_once_the_tick_is_sent(self, client, sma_doc):
        """
        Submitted as a draft: this is pinning that the GATE let the submission
        through, and a draft proves exactly that without leaving a run thread
        racing the in-memory SQLite teardown.
        """
        doc = copy.deepcopy(sma_doc)  # the ticked builder
        doc["start"] = False
        res = client.post("/api/optimize/runs", json=doc)
        assert res.status_code == 201, res.get_data(as_text=True)
        assert res.get_json()["run"]["status"] == "draft"


class TestTheRunPayload:
    def test_the_stored_attestation_is_what_the_results_page_reads(
        self, client, service, store, sma_doc
    ):
        run = service.wait(service.submit(sma_doc)["run_id"], timeout=120)
        res = client.get(f"/api/optimize/runs/{run['run_id']}").get_json()
        prov = res["run"]["provenance"]
        assert prov["derived"] is False, "a measured record is not a reconstruction"
        assert prov["bars_count"] == 781
        assert prov["data_source"] == "synthetic"
        assert prov["acknowledged"] is True

    def test_the_engine_half_survives_the_merge(self, client, service, sma_doc):
        """The stored attestation is the DATA half only. Dropping the engine
        label would make a run that remembers its data look more complete while
        quietly losing something else."""
        run = service.wait(service.submit(sma_doc)["run_id"], timeout=120)
        prov = client.get(f"/api/optimize/runs/{run['run_id']}").get_json()["run"]["provenance"]
        assert prov["engine_used"] == "backtest_driver"
        assert prov["engine_tier"] == "canonical"

    def test_a_run_from_before_the_migration_is_marked_as_reconstructed(
        self, client, service, sma_doc
    ):
        """
        Every existing optimize run has no attestation. Saying so is the point:
        a rebuilt record presented with the same authority as a measured one is
        the failure §2 exists to prevent.
        """
        run = service.wait(service.submit(sma_doc)["run_id"], timeout=120)
        service.store.update_run(run["run_id"], data_attestation=None)
        prov = client.get(f"/api/optimize/runs/{run['run_id']}").get_json()["run"]["provenance"]
        assert prov["derived"] is True
        assert prov["data_source"] == "synthetic", "it still describes the run"

    def test_the_runs_list_carries_the_source(self, client, service, sma_doc):
        run = service.wait(service.submit(sma_doc)["run_id"], timeout=120)
        runs = client.get("/api/optimize/runs").get_json()["runs"]
        row = next(r for r in runs if r["run_id"] == run["run_id"])
        assert row["data_source"] == "synthetic"
        assert row["bars_count"] == 781


class TestTheMigration:
    def test_015_is_wired_after_014(self):
        from pathlib import Path

        import backtest

        versions = Path(backtest.__file__).parents[2] / "db" / "alembic" / "versions"
        text = (versions / "20260929_1100_015_optimization_data_attestation.py").read_text()
        assert 'revision: str = "015"' in text
        assert 'down_revision: Union[str, None] = "014"' in text

    def test_it_is_additive_only(self):
        """No backfill: a row written before this has no attestation, and that
        is the truth of it. A guessed value is a claim nobody checked."""
        from pathlib import Path

        import backtest

        versions = Path(backtest.__file__).parents[2] / "db" / "alembic" / "versions"
        text = (versions / "20260929_1100_015_optimization_data_attestation.py").read_text()
        assert "nullable=True" in text
        assert "server_default" not in text
        assert "UPDATE optimization_runs" not in text

    def test_the_model_declares_every_column_the_migration_adds(self):
        """A column added to the migration but not the model is one nobody can
        read back — worse than not having it."""
        from backtest.db.models import OptimizationRun
        from backtest.optimization.attestation import attestation_columns

        declared = {c.name for c in OptimizationRun.__table__.columns}
        assert set(attestation_columns(None)) <= declared


class TestTheAuditTrail:
    def test_applying_copies_the_attestation_onto_the_audit_row(self, service, store, sma_doc):
        """
        The audit row is the last place a run's data can be checked, long after
        the results page has scrolled away. Copied in, not re-derived: the
        attestation is a fact about the candles the search loaded, and those
        are not here to measure again.
        """
        run = service.wait(service.submit(sma_doc)["run_id"], timeout=120)
        out = service.apply(run["run_id"], target="none")
        att = (out["audit"].get("action_details") or {}).get("data_attestation")
        assert att, "the apply audit row must carry the attestation"
        assert att["data_source"] == "synthetic"
        assert att["bars_count"] == 781
        assert att["acknowledged"] is True
