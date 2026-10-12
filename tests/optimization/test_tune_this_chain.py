"""PRD backTest-enhance §6 reverse flow — the three-link audit chain.

When an Optimize run is applied, the audit entry records where the parameters
came from: the backtest result that started the search, the optimize run
itself, and the runner that now carries them.

The first link is a **session handle** minted by the Backtest page, not a
stored backtest record — nothing about a completed backtest is persisted. That
is fine for an audit chain (it answers "which result was this tuned from?") and
would not be fine if it implied a record that could be opened, so the wording
in the UI says which one it is.

A run that did NOT come from a backtest still produces a chain, with the first
link explicitly absent. Silently omitting the link would make an un-traced
apply look identical to a traced one.
"""

from __future__ import annotations

import copy

import pytest


@pytest.fixture()
def _run(service, sma_doc):
    def _make(origin=None):
        # The sma_doc fixture is already a dict; copy so the origin does not
        # leak into the shared fixture.
        doc = copy.deepcopy(sma_doc)
        if origin:
            doc["backtestConfig"]["sourceBacktestId"] = origin
        return service.wait(service.submit(doc)["run_id"], timeout=120)

    return _make


def _chain(audit):
    return audit["action_details"]["chain"]


def test_the_chain_names_all_three_links(service, _run):
    run = _run(origin="bt_abc123")
    out = service.apply(run["run_id"], target="none")
    chain = _chain(out["audit"])
    assert [c["step"] for c in chain] == ["backtest", "optimize", "runner"]
    assert chain[0]["id"] == "bt_abc123"
    assert chain[1]["id"] == run["run_id"]


def test_a_run_not_started_from_a_backtest_still_chains(service, _run):
    """The link degrades to an explicit absence; it does not disappear."""
    run = _run()
    out = service.apply(run["run_id"], target="none")
    chain = _chain(out["audit"])
    assert chain[0]["id"] is None
    assert chain[0]["step"] == "backtest"
    assert chain[1]["id"] == run["run_id"]


def test_record_only_has_no_runner_link(service, _run):
    run = _run(origin="bt_abc123")
    out = service.apply(run["run_id"], target="none")
    assert out["instance_id"] is None
    assert _chain(out["audit"])[2]["id"] is None


def test_applying_to_a_runner_completes_the_third_link(service, _run, fake_manager):
    run = _run(origin="bt_abc123")
    out = service.apply(run["run_id"], target="paper")
    iid = out["instance_id"]
    assert iid in fake_manager.runners
    chain = _chain(out["audit"])
    assert chain[2]["id"] == iid
    assert chain[2]["step"] == "runner"
    assert chain[2]["label"] == "Paper runner"


def test_the_origin_reaches_the_stored_run(service, _run):
    """The chain is only buildable if the handle survived the whole journey."""
    run = _run(origin="bt_abc123")
    assert (run.get("backtest_config") or {}).get("sourceBacktestId") == "bt_abc123"
