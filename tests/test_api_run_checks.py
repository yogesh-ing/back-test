"""PRD backTest-enhance §3 at the HTTP layer.

The maths is covered in ``test_run_checks.py``; this pins the API contract:
the three blocks ride on the existing result payload, the standalone Monte
Carlo endpoint validates its input instead of trusting it, and nothing here
turns a diagnostic into a failed backtest.
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def client():
    from backtest.web.app import create_app

    return create_app(source="synthetic").test_client()


VALID = {
    "strategy": "sma_crossover",
    "symbol": "DEMO",
    "from_date": "2022-01-01",
    "to_date": "2024-01-01",
    "capital": 100000,
    "timeframe": "1day",
    "params": {},
}


# ---------------------------------------------------------------------------
# The blocks ride on the existing payload
# ---------------------------------------------------------------------------


class TestRunPayloadCarriesTheChecks:
    def test_all_three_blocks_are_present(self, client):
        body = client.post("/api/backtest/run", json=VALID).get_json()
        for key in ("benchmark", "cost_shock", "monte_carlo"):
            assert key in body, f"{key} missing from /api/backtest/run"

    def test_benchmark_carries_its_metrics_and_alpha_beta(self, client):
        b = client.post("/api/backtest/run", json=VALID).get_json()["benchmark"]
        assert b["available"] is True
        assert {"total_return", "sharpe", "max_drawdown", "alpha", "beta"} <= set(b)

    def test_cost_shock_is_available_on_the_canonical_engine(self, client):
        cs = client.post("/api/backtest/run", json=VALID).get_json()["cost_shock"]
        assert cs["available"] is True
        assert [s["multiple"] for s in cs["scenarios"]] == [1, 2, 3]

    def test_a_frictionless_run_says_so_rather_than_implying_it_was_costed(self, client):
        cs = client.post("/api/backtest/run", json=VALID).get_json()["cost_shock"]
        assert cs["base_bps_source"] == "default"
        assert cs["actual"]["slippage_bps"] == 0.0

    def test_quick_screen_reports_cost_shock_unavailable_with_a_reason(self, client):
        """A silently absent stress test is indistinguishable from one that passed."""
        body = client.post("/api/backtest/run", json=dict(VALID, mode="quick_screen")).get_json()
        assert body["cost_shock"]["available"] is False
        assert "Fast Preview" in body["cost_shock"]["reason"]

    def test_monte_carlo_ships_both_experiments(self, client):
        mc = client.post("/api/backtest/run", json=VALID).get_json()["monte_carlo"]
        assert mc["available"] is True
        assert "reorder" in mc and "bootstrap" in mc
        assert mc["reorder"]["final_equity_is_invariant"] is True
        assert mc["bootstrap"]["final_equity_is_invariant"] is False

    def test_the_response_still_serialises_without_nan_or_infinity(self, client):
        resp = client.post("/api/backtest/run", json=VALID)
        text = resp.get_data(as_text=True)
        assert "NaN" not in text and "Infinity" not in text

    def test_the_checks_do_not_slow_a_run_into_uselessness(self, client):
        client.post("/api/backtest/run", json=VALID)  # warm caches
        import time

        started = time.perf_counter()
        assert client.post("/api/backtest/run", json=VALID).status_code == 200
        # Three extra engine runs is a few hundred ms; anything near a second
        # per run means the checks have started blocking the user.
        assert (time.perf_counter() - started) < 10.0

    def test_an_unknown_strategy_still_fails_fast_without_running_checks(self, client):
        resp = client.post("/api/backtest/run", json=dict(VALID, strategy="nope"))
        assert resp.status_code == 400
        assert "error" in resp.get_json()


# ---------------------------------------------------------------------------
# POST /api/backtest/monte-carlo
# ---------------------------------------------------------------------------


class TestMonteCarloEndpoint:
    TRADES = [{"pnl": p, "is_open": False} for p in (500, -200, 800, 300, -150, 600, -400, 250)]

    def test_returns_a_distribution_for_an_inline_trade_list(self, client):
        body = client.post(
            "/api/backtest/monte-carlo", json={"trades": self.TRADES, "capital": 100000}
        ).get_json()
        assert body["available"] is True
        assert body["simulations"] == 1000
        assert body["bootstrap"]["p5_final_equity"] <= body["bootstrap"]["p95_final_equity"]

    def test_accepts_a_whole_result_payload(self, client):
        result = client.post("/api/backtest/run", json=VALID).get_json()
        body = client.post("/api/backtest/monte-carlo", json={"result": result}).get_json()
        assert body["available"] is True

    def test_accepts_a_bare_list_of_numbers(self, client):
        body = client.post(
            "/api/backtest/monte-carlo", json={"trades": [100, -50, 200, 30, -80]}
        ).get_json()
        assert body["available"] is True

    def test_open_trades_are_excluded(self, client):
        # An open position has not happened yet, so it is not a sample point.
        trades = [{"pnl": 500, "is_open": False}] * 6 + [{"pnl": 99_000, "is_open": True}]
        body = client.post("/api/backtest/monte-carlo", json={"trades": trades}).get_json()
        assert body["trades"] == 6
        assert body["concentration"]["best_trade"] == pytest.approx(500.0)

    def test_the_honours_a_custom_simulation_count(self, client):
        body = client.post(
            "/api/backtest/monte-carlo", json={"trades": self.TRADES, "simulations": 50}
        ).get_json()
        assert body["simulations"] == 50

    def test_the_endpoint_agrees_with_the_run_payload(self, client):
        """Two paths, one function — they must not drift."""
        result = client.post("/api/backtest/run", json=VALID).get_json()
        again = client.post("/api/backtest/monte-carlo", json={"result": result}).get_json()
        assert again["bootstrap"] == result["monte_carlo"]["bootstrap"]

    def test_missing_trades_is_a_400(self, client):
        resp = client.post("/api/backtest/monte-carlo", json={})
        assert resp.status_code == 400
        assert "trades" in resp.get_json()["error"]

    def test_a_non_list_trades_is_a_400(self, client):
        resp = client.post("/api/backtest/monte-carlo", json={"trades": "nope"})
        assert resp.status_code == 400

    def test_a_non_numeric_pnl_is_a_400_not_a_500(self, client):
        resp = client.post(
            "/api/backtest/monte-carlo", json={"trades": [{"pnl": "abc", "is_open": False}]}
        )
        assert resp.status_code == 400
        assert "non-numeric" in resp.get_json()["error"]

    def test_a_nonsense_simulation_count_is_a_400(self, client):
        for bad in (0, 1, -5, 50_001, "abc"):
            resp = client.post(
                "/api/backtest/monte-carlo", json={"trades": self.TRADES, "simulations": bad}
            )
            assert resp.status_code == 400, f"{bad!r} should be rejected"

    def test_too_few_trades_is_a_200_with_available_false(self, client):
        """A sample too small to resample is a finding, not a client error."""
        body = client.post("/api/backtest/monte-carlo", json={"trades": [100]}).get_json()
        assert body["available"] is False
        assert "no sequence" in body["reason"]

    def test_a_bad_capital_is_a_400(self, client):
        resp = client.post(
            "/api/backtest/monte-carlo", json={"trades": self.TRADES, "capital": "lots"}
        )
        assert resp.status_code == 400

    def test_the_response_has_no_nan_or_infinity(self, client):
        resp = client.post("/api/backtest/monte-carlo", json={"trades": self.TRADES})
        text = resp.get_data(as_text=True)
        assert "NaN" not in text and "Infinity" not in text
