"""PRD Task 6.4 — Strategy API endpoint tests."""

import pytest

from backtest.web.app import create_app


@pytest.fixture()
def client():
    return create_app(source="synthetic").test_client()


def test_list_strategies_returns_catalogue(client):
    resp = client.get("/api/strategies")
    assert resp.status_code == 200
    body = resp.get_json()
    names = {s["name"] for s in body}
    assert {"sma_crossover", "rsi_reversion", "buy_and_hold", "donchian_breakout"} <= names
    for entry in body:
        assert set(entry) == {"name", "description", "version", "author"}


def test_list_strategies_sorted_alphabetically(client):
    names = [s["name"] for s in client.get("/api/strategies").get_json()]
    assert names == sorted(names)


def test_params_returns_schema(client):
    resp = client.get("/api/strategies/rsi_reversion/params")
    assert resp.status_code == 200
    schema = resp.get_json()
    assert "period" in schema
    for spec in schema.values():
        assert {"default", "min", "max", "type", "label", "tooltip"} <= set(spec)


def test_params_unknown_strategy_returns_404(client):
    resp = client.get("/api/strategies/does_not_exist/params")
    assert resp.status_code == 404
    assert "error" in resp.get_json()
