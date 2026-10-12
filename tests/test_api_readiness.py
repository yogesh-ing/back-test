"""The §5 readiness block as the API actually ships it.

The bands and the fourth state are pinned in ``tests/test_readiness.py``. What
is pinned HERE is the wiring: that the block reaches a real response, that
Compare gets one per slot (§5 says "per-strategy in Compare"), and that a slot
which failed has no readiness rather than a flattering default.
"""

from __future__ import annotations

import pytest

from backtest.web.app import create_app


@pytest.fixture()
def client():
    return create_app(source="synthetic").test_client()


CFG = {
    "strategy": "sma_crossover",
    "symbol": "INFY",
    "from_date": "2022-01-01",
    "to_date": "2024-01-01",
    "capital": 100_000,
    "timeframe": "1day",
    "params": {"fast": 10, "slow": 30},
}

CHECK_IDS = {
    "engine",
    "data_source",
    "trade_count",
    "beats_benchmark",
    "cost_shock_2x",
    "profit_factor",
    "max_drawdown",
    "monte_carlo_p_profit",
}


class TestSingleRun:
    def test_the_block_is_in_the_response(self, client):
        resp = client.post("/api/backtest/run", json=CFG)
        assert resp.status_code == 200
        rd = resp.get_json()["readiness"]
        assert {c["id"] for c in rd["checks"]} == CHECK_IDS

    def test_the_block_is_marked_advisory(self, client):
        rd = client.post("/api/backtest/run", json=CFG).get_json()["readiness"]
        assert rd["advisory"] is True
        assert rd["gates_nothing"] is True

    def test_synthetic_data_is_flagged_red(self, client):
        """
        The headline case: on synthetic data nothing here is certifiable, and
        the panel has to say so rather than let six green ticks do the talking.
        """
        rd = client.post("/api/backtest/run", json=CFG).get_json()["readiness"]
        data = next(c for c in rd["checks"] if c["id"] == "data_source")
        assert data["status"] == "red"
        assert "Synthetic" in data["value"]

    def test_quick_screen_is_red_on_engine_and_unknown_on_cost_shock(self, client):
        """
        One fact, one penalty.

        Quick-Screen is red for the engine. It also cannot run a cost shock —
        and that must be `unknown`, or the same fact is charged twice and a
        single limitation reads as a strategy that failed four checks.
        """
        rd = client.post("/api/backtest/run", json={**CFG, "mode": "quick_screen"}).get_json()[
            "readiness"
        ]
        by_id = {c["id"]: c for c in rd["checks"]}
        assert by_id["engine"]["status"] == "red"
        assert by_id["cost_shock_2x"]["status"] == "unknown"
        assert by_id["cost_shock_2x"]["detail"]

    def test_the_block_describes_the_run_it_travels_with(self, client):
        """A stale or defaulted block would be worse than none."""
        payload = client.post("/api/backtest/run", json=CFG).get_json()
        rd = payload["readiness"]
        trades = next(c for c in rd["checks"] if c["id"] == "trade_count")
        assert str(payload["metrics"]["closed_trades"]) in trades["label"]
        assert rd["closed_trades"] == payload["metrics"]["closed_trades"]


class TestCompare:
    """§5: "shown at the bottom of every Backtest result, and per-strategy in Compare"."""

    def test_every_successful_slot_carries_its_own_block(self, client):
        resp = client.post(
            "/api/backtest/run-many",
            json={
                "shared": {
                    "symbol": "INFY",
                    "from_date": "2022-01-01",
                    "to_date": "2024-01-01",
                    "capital": 100_000,
                },
                "slots": [
                    {
                        "id": 1,
                        "strategy": "sma_crossover",
                        "timeframe": "1day",
                        "params": {"fast": 10, "slow": 30},
                    },
                    {
                        "id": 2,
                        "strategy": "rsi_reversion",
                        "timeframe": "1day",
                        "params": {"period": 14},
                    },
                ],
            },
        )
        assert resp.status_code == 200
        results = resp.get_json()["results"]
        for sid, payload in results.items():
            assert "readiness" in payload, f"slot {sid} has no readiness block"
            assert {c["id"] for c in payload["readiness"]["checks"]} == CHECK_IDS

    def test_two_slots_get_two_different_blocks(self, client):
        """If they were identical, the panel would be decorative."""
        resp = client.post(
            "/api/backtest/run-many",
            json={
                "shared": {
                    "symbol": "INFY",
                    "from_date": "2022-01-01",
                    "to_date": "2024-01-01",
                    "capital": 100_000,
                },
                "slots": [
                    {
                        "id": 1,
                        "strategy": "sma_crossover",
                        "timeframe": "1day",
                        "params": {"fast": 10, "slow": 30},
                    },
                    {
                        "id": 2,
                        "strategy": "rsi_reversion",
                        "timeframe": "1day",
                        "params": {"period": 14},
                    },
                ],
            },
        )
        one, two = (resp.get_json()["results"][k]["readiness"] for k in ("1", "2"))
        assert one["closed_trades"] == resp.get_json()["results"]["1"]["metrics"]["closed_trades"]
        assert two["closed_trades"] == resp.get_json()["results"]["2"]["metrics"]["closed_trades"]
        assert one["checks"] != two["checks"] or one["counts"] != two["counts"]

    def test_a_failed_slot_has_no_readiness_block(self, client):
        """
        §4.1 keeps the failed slot visible. It must not be handed a
        readiness block, which would render a full set of ticks beside an
        error message.
        """
        resp = client.post(
            "/api/backtest/run-many",
            json={
                "shared": {
                    "symbol": "INFY",
                    "from_date": "2022-01-01",
                    "to_date": "2024-01-01",
                    "capital": 100_000,
                },
                "slots": [
                    {
                        "id": 1,
                        "strategy": "sma_crossover",
                        "timeframe": "1day",
                        "params": {"fast": 10, "slow": 30},
                    },
                    {"id": 2, "strategy": "no_such_strategy", "timeframe": "1day", "params": {}},
                ],
            },
        )
        results = resp.get_json()["results"]
        assert "error" in results["2"]
        assert "readiness" not in results["2"]


class TestPageWiring:
    def test_the_backtest_page_ships_the_mount_point(self, client):
        html = client.get("/backtest").get_data(as_text=True)
        assert 'id="certification"' in html
        assert "js/components/certification.js" in html

    def test_the_component_is_served(self, client):
        assert client.get("/static/js/components/certification.js").status_code == 200


class TestTuneThisWiring:
    """PRD §6: the Backtest → Optimize hand-off has to be reachable."""

    def test_the_backtest_page_ships_the_button_and_its_script(self, client):
        html = client.get("/backtest").get_data(as_text=True)
        assert 'id="tuneThis"' in html
        assert "js/components/tune_this.js" in html
        assert client.get("/static/js/components/tune_this.js").status_code == 200

    def test_the_optimize_page_ships_the_carried_over_notice(self, client):
        html = client.get("/optimize").get_data(as_text=True)
        assert 'id="optPrefillNotice"' in html

    def test_the_pages_ship_the_data_source_gate(self, client):
        """The gate is only useful if it is on the page that starts runs."""
        for path in ("/backtest", "/optimize"):
            html = client.get(path).get_data(as_text=True)
            assert 'id="dataSourceGate"' in html, path
            assert "js/components/data_source_gate.js" in html, path
        assert client.get("/static/js/components/data_source_gate.js").status_code == 200

    def test_the_run_page_ships_the_monte_carlo_controls(self, client):
        """PRD Part 2 §4: the button next to Apply, and the gate in the modal."""
        html = client.get("/optimize/runs/00000000-0000-0000-0000-000000000000").get_data(
            as_text=True
        )
        assert 'id="btnMonteCarlo"' in html
        assert 'id="monteCarloBox"' in html
        assert 'id="applyMcGate"' in html

    def test_the_run_page_ships_the_warning_panel_and_regime_table(self, client):
        """PRD Part 2 §6.1/§6.2."""
        html = client.get("/optimize/runs/00000000-0000-0000-0000-000000000000").get_data(
            as_text=True
        )
        assert 'id="warningPanel"' in html
        assert 'id="regimeBox"' in html

    def test_the_run_page_ships_the_audit_chain(self, client):
        html = client.get("/optimize/runs/00000000-0000-0000-0000-000000000000").get_data(
            as_text=True
        )
        assert 'id="applyChain"' in html
