"""Provenance on the backtest HTTP surface (PRD backTest-enhance §1.1 + §1.2).

The payload contract: a backtest result says which ENGINE produced it and
which DATA it ran on, and the page renders that permanently. These tests pin
the stamp at the three places a result leaves the server — single run, the
process-pool slot worker, and the shared block on a comparison.
"""

from __future__ import annotations

import pytest

from backtest.data.provenance import ENGINE_FILL_EXACT, ENGINE_MIXED, ENGINE_QUICK_SCREEN
from backtest.web.app import create_app

_RUN = {
    "strategy": "sma_crossover",
    "symbol": "DEMO",
    "timeframe": "1D",
    "from_date": "2021-01-01",
    "to_date": "2024-01-01",
    "capital": 100_000,
    "params": {"fast": 10, "slow": 30},
}

_SHARED = {
    "symbol": "DEMO",
    "from_date": "2021-01-01",
    "to_date": "2024-01-01",
    "capital": 100_000,
}

_SLOTS = [
    {"id": 1, "strategy": "sma_crossover", "timeframe": "1D", "params": {"fast": 10, "slow": 30}},
    {"id": 2, "strategy": "rsi_reversion", "timeframe": "1D", "params": {"period": 14}},
]


@pytest.fixture()
def client():
    return create_app(source="synthetic").test_client()


def _codes(prov):
    return [w["code"] for w in prov["warnings"]]


# --- single run -------------------------------------------------------------


def test_run_stamps_engine_and_data(client):
    prov = client.post("/api/backtest/run", json=_RUN).get_json()["provenance"]

    assert prov["engine_used"] == ENGINE_FILL_EXACT
    assert prov["engine_label"] == "Fill-Exact (Canonical)"
    assert prov["engine_canonical"] is True
    assert prov["data_source"] == "synthetic"
    assert prov["data_source_label"] == "Synthetic"
    assert prov["data_source_real"] is False


def test_run_stamps_the_audit_trail(client):
    """Everything the PRD asks to be attached to a result record."""
    body = client.post("/api/backtest/run", json=_RUN).get_json()
    prov = body["provenance"]

    assert prov["symbol"] == "DEMO"
    assert prov["timeframe"] == "1D"
    assert prov["date_range"] == {"from": "2021-01-01", "to": "2024-01-01"}
    assert prov["bars_count"] == body["metrics"]["bars"] > 0
    assert prov["data_from"] >= "2021-01-01"
    assert prov["data_to"] <= "2024-01-01"


def test_run_warns_that_synthetic_data_is_not_certification_grade(client):
    prov = client.post("/api/backtest/run", json=_RUN).get_json()["provenance"]
    assert "non_real_data" in _codes(prov)


def test_quick_screen_is_opt_in_and_never_the_default(client):
    """§1.1: the approximate engine must not be reachable without asking for it."""
    default = client.post("/api/backtest/run", json=_RUN).get_json()["provenance"]
    assert default["engine_used"] == ENGINE_FILL_EXACT
    assert "approximate_engine" not in _codes(default)

    preview = client.post("/api/backtest/run", json=dict(_RUN, mode="quick_screen")).get_json()[
        "provenance"
    ]
    assert preview["engine_used"] == ENGINE_QUICK_SCREEN
    assert preview["engine_canonical"] is False
    assert "approximate_engine" in _codes(preview)


def test_unknown_source_fails_closed_on_the_badge(client):
    """A source the app does not recognise must not be badged as real."""
    prov = client.post("/api/backtest/run", json=_RUN).get_json()["provenance"]
    assert prov["data_source_real"] is False
    assert prov["warnings"], "an unrecognised data source always carries a banner"


# --- parallel slots ---------------------------------------------------------


def test_every_slot_carries_its_own_provenance(client):
    results = client.post(
        "/api/backtest/run-many", json={"shared": _SHARED, "slots": _SLOTS}
    ).get_json()["results"]

    for slot_id, payload in results.items():
        prov = payload["provenance"]
        assert prov["engine_used"] == ENGINE_FILL_EXACT, slot_id
        assert prov["symbol"] == "DEMO"
        assert prov["data_source"] == "synthetic"
        assert prov["bars_count"] > 0
        # The slot's engine must agree with the engine it reports in config.
        assert prov["engine_used"] == payload["config"]["engine"]


def test_run_many_returns_one_shared_provenance_block(client):
    shared = client.post(
        "/api/backtest/run-many", json={"shared": _SHARED, "slots": _SLOTS}
    ).get_json()["provenance"]

    assert shared["engine_used"] == ENGINE_FILL_EXACT
    assert shared["symbol"] == "DEMO"
    assert shared["date_range"] == {"from": "2021-01-01", "to": "2024-01-01"}
    assert shared["engines_used"] == [ENGINE_FILL_EXACT]


def test_slots_on_different_engines_are_stamped_mixed(client):
    """A fill-exact slot next to a quick-screen slot is not a like-for-like
    comparison, and the shared block has to say so."""
    slots = [dict(_SLOTS[0]), dict(_SLOTS[1], mode="quick_screen")]
    shared = client.post(
        "/api/backtest/run-many", json={"shared": _SHARED, "slots": slots}
    ).get_json()["provenance"]

    assert shared["engine_used"] == ENGINE_MIXED
    assert shared["engine_canonical"] is False
    assert "mixed_engine" in _codes(shared)
    assert sorted(shared["engines_used"]) == [ENGINE_FILL_EXACT, ENGINE_QUICK_SCREEN]


def test_shared_provenance_lists_every_timeframe_in_play(client):
    slots = [dict(_SLOTS[0]), dict(_SLOTS[1], timeframe="1H")]
    shared = client.post(
        "/api/backtest/run-many", json={"shared": _SHARED, "slots": slots}
    ).get_json()["provenance"]
    assert shared["timeframe"] == "1D,1H"


def test_failed_slots_do_not_break_the_shared_stamp(client):
    slots = [dict(_SLOTS[0]), {"id": 2, "strategy": "nope", "timeframe": "1D"}]
    body = client.post("/api/backtest/run-many", json={"shared": _SHARED, "slots": slots})
    assert body.status_code == 200
    assert "error" in body.get_json()["results"]["2"]
    assert body.get_json()["provenance"]["engine_used"] == ENGINE_FILL_EXACT


# --- the pages must actually mount the strip ---------------------------------


def test_backtest_page_mounts_the_provenance_strip(client):
    html = client.get("/backtest").get_data(as_text=True)
    assert 'id="resultProvenance"' in html
    assert "components/provenance.js" in html
    # The approximate engine is opt-in only, and says so on the form.
    assert 'id="fastPreview"' in html
    assert "quick_screen" in client.get("/static/js/backtest.js").get_data(as_text=True)


def test_compare_page_mounts_the_provenance_strip(client):
    html = client.get("/compare").get_data(as_text=True)
    assert 'id="compareProvenance"' in html
    assert "components/provenance.js" in html
    # Engine is a shared condition, so the toggle is in the SHARED config panel.
    assert 'id="fastPreview"' in html


def test_provenance_component_is_served(client):
    r = client.get("/static/js/components/provenance.js")
    assert r.status_code == 200
    assert "Provenance" in r.get_data(as_text=True)


def test_shared_block_reports_no_bar_count_of_its_own(client):
    """It describes the shared conditions, not a run — the slots carry the bars."""
    shared = client.post(
        "/api/backtest/run-many", json={"shared": _SHARED, "slots": _SLOTS}
    ).get_json()["provenance"]
    assert shared["bars_count"] is None
    results = client.post(
        "/api/backtest/run-many", json={"shared": _SHARED, "slots": _SLOTS}
    ).get_json()["results"]
    assert all(results[str(s["id"])]["provenance"]["bars_count"] > 0 for s in _SLOTS)
