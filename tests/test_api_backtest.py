"""Backtest API endpoint tests (PRD Task 6.3, re-routed in ticket P2.2).

Default path is the canonical :class:`BacktestDriver` (simulator engine);
``mode='quick_screen'`` keeps the legacy vectorized path. Both must return
the identical payload shape (UI unchanged this phase).
"""

import pytest

from backtest.web.app import create_app


@pytest.fixture()
def client():
    return create_app(source="synthetic").test_client()


_VALID = {
    "strategy": "sma_crossover",
    "symbol": "DEMO",
    "timeframe": "1D",
    "from_date": "2021-01-01",
    "to_date": "2024-01-01",
    "capital": 100_000,
    "params": {"fast": 10, "slow": 30},
}

_FULL_SHAPE = {"config", "metrics", "equity", "drawdown", "trades", "signals"}


# --- single backtest (canonical: BacktestDriver) ----------------------------


def test_run_valid_returns_full_shape(client):
    resp = client.post("/api/backtest/run", json=_VALID)
    assert resp.status_code == 200
    body = resp.get_json()
    assert set(body) == _FULL_SHAPE
    assert {"total_pnl", "win_rate_pct", "max_drawdown_pct", "sharpe", "total_trades"} <= set(
        body["metrics"]
    )
    assert body["config"]["strategy"] == "sma_crossover"
    assert body["config"]["symbol"] == "DEMO"
    # P2.2: the default engine is the driver over simulator/.
    assert body["config"]["engine"] == "backtest_driver"


def test_run_driver_matches_paper_runner_pnl(client):
    """Acceptance: /api/backtest/run returns results from BacktestDriver —
    the driver's P&L equals a PaperRunner on the same bars (one loop)."""
    from backtest.data.synthetic import SyntheticSource
    from backtest.engine.backtest_driver import BacktestDriver
    from backtest.forward.paper_runner import PaperRunner, _all_in_size, _FrameSource, free_executor
    from backtest.simulator.portfolio import Portfolio
    from backtest.strategy.registry import get_strategy

    body = client.post("/api/backtest/run", json=_VALID).get_json()

    source = SyntheticSource()
    candles = source.get_candles("DEMO", "2021-01-01", "2024-01-01", "day")
    strategy = get_strategy("sma_crossover")(fast=10, slow=30)

    pf = Portfolio(name="check", initial_capital=100_000)
    driver = BacktestDriver(
        source=_FrameSource(candles),
        strategy=strategy,
        portfolio=pf,
        executor=free_executor(pf, max_participation="1"),
        symbols=["DEMO"],
        size_fn=_all_in_size,
    )
    driver.run()
    driver_pnl = float(pf.calculate_total_equity()) - 100_000

    pf2 = Portfolio(name="check-forward", initial_capital=100_000)
    runner = PaperRunner(
        portfolio=pf2,
        source=_FrameSource(candles),
        strategy=strategy,
        executor=free_executor(pf2, max_participation="1"),
        symbols=["DEMO"],
        size_fn=_all_in_size,
    )
    runner.run()
    forward_pnl = float(pf2.calculate_total_equity()) - 100_000

    assert driver_pnl == forward_pnl  # one shared loop
    assert abs(body["metrics"]["total_pnl"] - driver_pnl) < 1.0  # adapter rounds to 2dp


def test_run_quick_screen_still_returned(client):
    """quick_screen keeps the legacy vectorized path — same shape, other engine."""
    resp = client.post("/api/backtest/run", json=dict(_VALID, mode="quick_screen"))
    assert resp.status_code == 200
    body = resp.get_json()
    assert set(body) == _FULL_SHAPE
    assert body["config"]["engine"] == "quick_screen"

    # A different engine genuinely runs: the two equity curves differ
    # (next-open all-in-98% sizing vs prev-close fully-invested + costs).
    driver_body = client.post("/api/backtest/run", json=_VALID).get_json()
    assert body["equity"]["values"] != driver_body["equity"]["values"]


def test_run_unknown_strategy_returns_400(client):
    body = dict(_VALID, strategy="nope")
    resp = client.post("/api/backtest/run", json=body)
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_run_bad_dates_returns_400(client):
    body = dict(_VALID, from_date="2024-01-01", to_date="2021-01-01")
    resp = client.post("/api/backtest/run", json=body)
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_run_missing_dates_returns_400(client):
    body = {k: v for k, v in _VALID.items() if k not in ("from_date", "to_date")}
    resp = client.post("/api/backtest/run", json=body)
    assert resp.status_code == 400


# --- parallel multi-slot ----------------------------------------------------


_MANY = {
    "shared": {
        "symbol": "DEMO",
        "from_date": "2021-01-01",
        "to_date": "2024-01-01",
        "capital": 100_000,
    },
    "slots": [
        {
            "id": 1,
            "strategy": "sma_crossover",
            "timeframe": "1D",
            "params": {"fast": 10, "slow": 30},
        },
        {"id": 2, "strategy": "rsi_reversion", "timeframe": "1D", "params": {"period": 14}},
        {"id": 3, "strategy": "buy_and_hold", "timeframe": "1D", "params": {}},
        {"id": 4, "strategy": "donchian_breakout", "timeframe": "1D", "params": {"lookback": 20}},
    ],
}


def test_run_many_returns_all_slots(client):
    resp = client.post("/api/backtest/run-many", json=_MANY)
    assert resp.status_code == 200
    results = resp.get_json()["results"]
    assert set(results) == {"1", "2", "3", "4"}
    for payload in results.values():
        assert "metrics" in payload and "equity" in payload
        assert payload["config"]["engine"] == "backtest_driver"


def test_run_many_mixed_modes(client):
    body = dict(_MANY)
    body["slots"] = [
        {
            "id": 1,
            "strategy": "sma_crossover",
            "timeframe": "1D",
            "params": {"fast": 10, "slow": 30},
        },
        {
            "id": 2,
            "strategy": "sma_crossover",
            "timeframe": "1D",
            "params": {"fast": 10, "slow": 30},
            "mode": "quick_screen",
        },
    ]
    resp = client.post("/api/backtest/run-many", json=body)
    assert resp.status_code == 200
    results = resp.get_json()["results"]
    assert results["1"]["config"]["engine"] == "backtest_driver"
    assert results["2"]["config"]["engine"] == "quick_screen"
    assert "metrics" in results["1"] and "metrics" in results["2"]


def test_run_many_broken_slot_isolated(client):
    body = dict(_MANY)
    body["slots"] = _MANY["slots"][:3] + [
        {"id": 4, "strategy": "broken_one", "timeframe": "1D", "params": {}}
    ]
    resp = client.post("/api/backtest/run-many", json=body)
    assert resp.status_code == 200
    results = resp.get_json()["results"]
    assert "error" in results["4"]
    assert "metrics" in results["1"] and "metrics" in results["3"]


def test_run_many_too_many_slots_rejected(client):
    body = dict(_MANY)
    body["slots"] = [{"id": i, "strategy": "buy_and_hold", "timeframe": "1D"} for i in range(5)]
    resp = client.post("/api/backtest/run-many", json=body)
    assert resp.status_code == 400


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"
