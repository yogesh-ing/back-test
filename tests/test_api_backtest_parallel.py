"""Process-pool backtest tests (ticket P2.3).

``POST /api/backtest/run-many`` runs each slot through
:func:`backtest.api.backtest.run_single_backtest` in a
:class:`~concurrent.futures.ProcessPoolExecutor` — top-level function,
plain-dict in, plain-dict out. The tests prove the jobs really run in
separate worker processes (not threads) via the ``worker_pid`` the worker
stamps into each slot's config: on Windows (spawn start method) pool
workers re-import this module fresh, so any in-parent monkeypatching or
global state is invisible to them — the pid must come back in the payload
itself.
"""

from __future__ import annotations

import os

import pytest

from backtest.web.app import create_app


@pytest.fixture()
def client():
    return create_app(source="synthetic").test_client()


def _many_body(n_slots: int, extra_slot=None) -> dict:
    slots = [
        {
            "id": i,
            "strategy": "sma_crossover",
            "timeframe": "1D",
            "params": {"fast": 10, "slow": 30},
        }
        for i in range(1, n_slots + 1)
    ]
    if extra_slot is not None:
        slots[-1] = extra_slot
    return {
        "shared": {
            "symbol": "DEMO",
            "from_date": "2021-01-01",
            "to_date": "2024-01-01",
            "capital": 100_000,
        },
        "slots": slots,
    }


def test_multiple_backtests_run_in_process_pool(client):
    """The ticket's acceptance test: 3 jobs all complete successfully, and
    in separate PROCESSES (each slot reports a worker pid that differs from
    the web process pid)."""
    resp = client.post("/api/backtest/run-many", json=_many_body(3))
    assert resp.status_code == 200
    results = resp.get_json()["results"]
    assert len(results) == 3
    # every job succeeded (a failed job would carry an "error" key)
    assert all("metrics" in r and "equity" in r for r in results.values())
    assert all(r["config"]["engine"] == "backtest_driver" for r in results.values())

    # the jobs ran in worker processes, not in the web process
    worker_pids = {r["config"]["worker_pid"] for r in results.values()}
    assert worker_pids
    assert os.getpid() not in worker_pids


def test_broken_job_isolated_in_process_pool(client):
    """One bad strategy fails only its own job; the neighbours still succeed."""
    resp = client.post(
        "/api/backtest/run-many",
        json=_many_body(
            2,
            extra_slot={"id": 2, "strategy": "no_such_strategy", "timeframe": "1D", "params": {}},
        ),
    )
    assert resp.status_code == 200
    results = resp.get_json()["results"]
    assert "error" in results["2"]
    assert "metrics" in results["1"]


def test_quick_screen_slot_runs_in_process_pool(client):
    """The vectorized quick path works from a worker process too."""
    body = _many_body(
        2,
        extra_slot={
            "id": 2,
            "strategy": "sma_crossover",
            "timeframe": "1D",
            "params": {"fast": 10, "slow": 30},
            "mode": "quick_screen",
        },
    )
    resp = client.post("/api/backtest/run-many", json=body)
    assert resp.status_code == 200
    results = resp.get_json()["results"]
    assert results["1"]["config"]["engine"] == "backtest_driver"
    assert results["2"]["config"]["engine"] == "quick_screen"
    assert "metrics" in results["1"] and "metrics" in results["2"]
