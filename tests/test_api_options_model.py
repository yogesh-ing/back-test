"""Options model backtest API (plan V3 Phase 3) — quarantine contract.

A model run expresses an equity strategy's signal as an option structure priced
by Black-Scholes off the underlying candles. It is research-only: the payload
carries ``certifiable=False`` at the source, a RED pricing-feed readiness check,
and ``tune_this_available=False``. These tests prove the server stamps that
truth (never a client flag) and that the kill switch / validation doors hold.

Every app here is built under pytest, so ``tests/conftest.py`` has set
``BACKTEST_DATA_PROFILE=testing`` — synthetic stays enabled and the DB firewall
redirects to sqlite. Nothing touches the deployment database. Ledger round-trip
tests inject an explicit in-memory sqlite ledger.
"""

from __future__ import annotations

import math
from datetime import datetime, time

import pytest

from backtest.api import options_model
from backtest.api.backtest import _candles
from backtest.api.backtest_run_store import BacktestRunLedger
from backtest.db import DatabaseManager
from backtest.db.models import BacktestRun, Base, OptionChainHistory
from backtest.engine.option_backtest_driver import next_monthly_expiry
from backtest.options.chain_history import ChainHistoryWriter
from backtest.timekeeping import EXCHANGE_TZ
from backtest.web.app import create_app


@pytest.fixture()
def app():
    return create_app(source="synthetic")


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def ledger_client():
    """A synthetic app with an injected in-memory sqlite ledger (read-back)."""
    mgr = DatabaseManager.from_env(profile="testing", url="sqlite:///:memory:")
    mgr.connect()
    Base.metadata.create_all(mgr.engine)
    led = BacktestRunLedger(mgr)
    led.ensure_schema()
    application = create_app(source="synthetic")
    application.config["BACKTEST_LEDGER"] = led
    yield application.test_client(), led, mgr
    mgr.disconnect()


@pytest.fixture()
def chain_client(tmp_path, monkeypatch):
    """A synthetic app whose chain-history reads hit an isolated sqlite DB.

    The API's ``DatabaseManager.from_env`` is pointed at this test manager so
    the loader reads exactly the rows seeded here — no shared-state leakage
    between the coverage-refusal and success cases.
    """
    mgr = DatabaseManager.from_env(url=f"sqlite:///{tmp_path / 'chain.db'}")
    mgr.connect()
    Base.metadata.create_all(mgr.engine, tables=[OptionChainHistory.__table__])
    monkeypatch.setattr(options_model.DatabaseManager, "from_env", lambda *a, **k: mgr)
    application = create_app(source="synthetic")
    yield application.test_client(), mgr, application
    mgr.disconnect()


def _seed_chain(mgr, app, symbol, from_date, to_date):
    """Record a clean ATM chain aligned to the synthetic candles the driver runs.

    Built from the same candles the API loads, at the driver's own monthly
    expiry and strike grid, with C − P = S − K (risk_free=0 screen) so every
    row survives §7.1. This is a test fixture, not a trading path.
    """
    with app.app_context():
        _source, candles = _candles(symbol, from_date, to_date, "1D")
    rows = []
    for ts, bar in candles.iterrows():
        close = float(bar["close"])
        expiry = next_monthly_expiry(ts.date())
        atm = round(close / 50) * 50
        for d in (-2, -1, 0, 1, 2):
            strike = atm + d * 50
            if strike <= 0:
                continue
            intrinsic = close - strike
            for otype, mid in (("CE", 100 + intrinsic / 2), ("PE", 100 - intrinsic / 2)):
                rows.append({
                    "underlying": symbol, "expiry": expiry, "strike": strike,
                    "option_type": otype, "bar_ts": ts, "bid": mid - 1, "ask": mid + 1,
                    "ltp": mid, "iv": 0.18, "spot": close, "oi": 5000, "volume": 1000,
                    "instrument_token": f"X-{strike}-{otype}",
                    "trading_symbol": f"{symbol}{strike}{otype}", "lot_size": 50,
                })
    writer = ChainHistoryWriter(mgr, risk_free=0.0)
    writer.ensure_schema()
    return writer.write_rows(rows, source="test")


_VALID = {
    "strategy": "sma_crossover",
    "symbol": "DEMO",
    "timeframe": "1D",
    "from_date": "2021-01-01",
    "to_date": "2024-01-01",
    "capital": 1_000_000,
    "params": {"fast": 10, "slow": 30},
    "pricing_feed": "model_bs",
    "exits": {"stop_loss_pct": 0.3, "take_profit_pct": 0.5, "exit_on_signal_flip": True},
}


# --- the quarantine payload --------------------------------------------------


def test_model_run_is_quarantined_at_source(client):
    resp = client.post("/api/backtest/options-model", json=_VALID)
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()

    # The flag rides at the source, not from a client claim.
    assert body["certifiable"] is False
    assert body["certification_reason"] == "model_pricing"
    assert body["pricing_feed"] == "model_bs"
    assert body["config"]["engine"] == "options"
    assert body["config"]["pricing_feed"] == "model_bs"
    assert body["disclaimer"]

    # Readiness agrees: a ninth RED check, never tunable.
    readiness = body["readiness"]
    assert readiness["certifiable"] is False
    assert readiness["certification_reason"] == "model_pricing"
    assert readiness["tune_this_available"] is False
    assert readiness["verdict"] == "fail"
    assert len(readiness["checks"]) == 9
    feed_check = next(c for c in readiness["checks"] if c["id"] == "pricing_feed")
    assert feed_check["status"] == "red"
    assert "Pricing feed" in readiness["red_flags"]


def test_model_run_carries_sensitivity_and_vol_regime(client):
    body = client.post("/api/backtest/options-model", json=_VALID).get_json()

    # IV sensitivity at 0 / 2 / 4 vol pts (plan §3 correction 2).
    sens = body["iv_sensitivity"]
    assert [s["iv_premium_pts"] for s in sens] == [0.0, 2.0, 4.0]
    for s in sens:
        assert "net_realized_pnl" in s or "error" in s

    regime = body["vol_regime"]
    assert regime["regime"] in ("low", "normal", "high")
    assert "avg_iv_pct" in regime and regime["note"]

    # Margin-aware metrics + greeks curve ride on the same payload.
    assert "return_on_premium" in body["metrics"]
    assert "peak_margin_used" in body["metrics"]
    assert "greeks_curve" in body


def test_model_params_round_trip_into_config(client):
    req = dict(_VALID, model_params={"iv_source": "constant", "iv_constant": 0.20})
    body = client.post("/api/backtest/options-model", json=req).get_json()
    assert body["model_params"]["iv_source"] == "constant"
    assert body["config"]["model_params"]["iv_constant"] == 0.20


# --- ledger round-trip (read path never re-runs) -----------------------------


def test_model_run_persists_and_reads_back(ledger_client):
    client, led, mgr = ledger_client
    body = client.post("/api/backtest/options-model", json=_VALID).get_json()
    assert body["persisted"] is True, body
    run_id = body["run_id"]

    # The columns are written, not just the config blob.
    with led.db.session() as s:
        row = s.get(BacktestRun, run_id)
        assert row.pricing_feed == "model_bs"
        assert row.model_params is not None
        assert row.engine == "options"

    got = client.get(f"/api/backtest/runs/{run_id}")
    assert got.status_code == 200
    payload = got.get_json()["payload"]
    assert payload["pricing_feed"] == "model_bs"
    assert payload["config"]["pricing_feed"] == "model_bs"
    assert payload["readiness"]["certifiable"] is False
    assert payload["readiness"]["tune_this_available"] is False
    assert payload["model_params"] is not None


# --- kill switch -------------------------------------------------------------


def test_kill_switch_off_returns_404(client, monkeypatch):
    monkeypatch.setenv("OPTIONS_MODEL_BACKTEST_ENABLED", "false")
    resp = client.post("/api/backtest/options-model", json=_VALID)
    assert resp.status_code == 404
    cfg = client.get("/api/config").get_json()
    assert cfg["options_model_backtest_enabled"] is False


def test_kill_switch_default_on(client):
    cfg = client.get("/api/config").get_json()
    assert cfg["options_model_backtest_enabled"] is True


@pytest.mark.parametrize("raw", ["false", "0", "no", "off", "FALSE", "Off"])
def test_kill_switch_falsey_values(client, monkeypatch, raw):
    monkeypatch.setenv("OPTIONS_MODEL_BACKTEST_ENABLED", raw)
    assert client.post("/api/backtest/options-model", json=_VALID).status_code == 404


# --- validation doors --------------------------------------------------------


def test_chain_history_no_coverage_refused(chain_client):
    client, mgr, app = chain_client
    req = dict(_VALID, pricing_feed="chain_history", from_date="2023-01-01", to_date="2024-01-01")
    resp = client.post("/api/backtest/options-model", json=req)
    assert resp.status_code == 400
    body = resp.get_json()
    assert "no recorded option chain" in body["error"]
    assert body["pricing_feed"] == "chain_history"
    assert body["coverage"]["count"] == 0
    # Never a silent fallback to the model: the refusal names capture, not BS.
    assert "model_bs" not in body["error"]


def test_chain_history_partial_coverage_names_stored_range(chain_client):
    client, mgr, app = chain_client
    _seed_chain(mgr, app, "DEMO", "2023-01-01", "2023-06-01")
    req = dict(_VALID, pricing_feed="chain_history", from_date="2024-01-01", to_date="2024-06-01")
    resp = client.post("/api/backtest/options-model", json=req)
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["coverage"]["count"] > 0
    assert "Stored coverage" in body["error"]
    assert body["coverage"]["from"] <= body["coverage"]["to"]


def test_chain_history_run_is_certifiable(chain_client):
    client, mgr, app = chain_client
    seeded = _seed_chain(mgr, app, "DEMO", "2023-01-01", "2024-01-01")
    assert seeded["written"] > 0
    req = dict(_VALID, pricing_feed="chain_history", from_date="2023-01-01", to_date="2024-01-01")
    resp = client.post("/api/backtest/options-model", json=req)
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()

    # Real premiums unlock the doors the model is quarantined from.
    assert body["pricing_feed"] == "chain_history"
    assert body["certifiable"] is True
    assert body["certification_reason"] is None
    assert body["config"]["pricing_feed"] == "chain_history"
    readiness = body["readiness"]
    assert readiness["certifiable"] is True
    assert readiness["certification_reason"] is None
    # No ninth model check; the eight equity checks stand on their own.
    assert len(readiness["checks"]) == 8
    assert all(c["id"] != "pricing_feed" for c in readiness["checks"])

    # Recorded IV is a market observation, not a knob: no model sensitivity ladder.
    assert body["iv_sensitivity"] == []
    assert body["vol_regime"]["regime"] in ("low", "normal", "high", "unknown")
    assert body["model_params"] == {}
    # The replay actually traded against the recorded chain.
    assert body["metrics"]["total_trades"] > 0


def test_chain_history_rejects_model_params(chain_client):
    client, mgr, app = chain_client
    req = dict(_VALID, pricing_feed="chain_history", model_params={"iv_premium": 0.03})
    resp = client.post("/api/backtest/options-model", json=req)
    assert resp.status_code == 400
    assert "model_params" in resp.get_json()["error"]


def test_unknown_pricing_feed_refused(client):
    resp = client.post("/api/backtest/options-model", json=dict(_VALID, pricing_feed="magic"))
    assert resp.status_code == 400
    assert "magic" in resp.get_json()["error"]


def test_option_strategy_allowed(client):
    """Option strategies are no longer refused at the gate; the sandbox
    expresses the strategy's own view as an option structure priced by BS."""
    resp = client.post(
        "/api/backtest/options-model",
        json=dict(_VALID, strategy="directional_options", params={}),
    )
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["pricing_feed"] == "model_bs"
    assert body["certifiable"] is False
    assert body["certification_reason"] == "model_pricing"
    assert body["config"]["strategy"] == "directional_options"


def test_unknown_model_param_refused(client):
    resp = client.post("/api/backtest/options-model", json=dict(_VALID, model_params={"bogus": 1}))
    assert resp.status_code == 400
    assert "model_params" in resp.get_json()["error"]


def test_missing_strategy_refused(client):
    req = dict(_VALID)
    req.pop("strategy")
    assert client.post("/api/backtest/options-model", json=req).status_code == 400


def test_missing_dates_refused(client):
    req = dict(_VALID)
    req.pop("from_date")
    req.pop("to_date")
    assert client.post("/api/backtest/options-model", json=req).status_code == 400


def test_unknown_strategy_refused(client):
    resp = client.post("/api/backtest/options-model", json=dict(_VALID, strategy="no_such_strategy"))
    assert resp.status_code == 400


# --- the Backtest tab ships the options-mode UI (plan V3 §4) ------------------


def test_backtest_page_ships_options_mode_ui(client):
    html = client.get("/backtest").get_data(as_text=True)
    for element_id in (
        "optionsModelSection", "optionsMode", "pricingFeed", "pricingFeedRow",
        "modelIv", "optionsModelBanner", "volRegimeNote",
    ):
        assert f'id="{element_id}"' in html, f"missing {element_id}"
    # The feed defaults to the Black-Scholes model; chain history is selectable.
    assert 'value="model_bs" selected' in html
    assert 'value="chain_history"' in html
    assert 'value="chain_history" disabled' not in html
    # The section starts hidden — the kill switch reveals it from /api/config.
    assert 'id="optionsModelSection" hidden' in html


def test_config_flag_drives_the_toggle(client, monkeypatch):
    assert client.get("/api/config").get_json()["options_model_backtest_enabled"] is True
    monkeypatch.setenv("OPTIONS_MODEL_BACKTEST_ENABLED", "false")
    assert client.get("/api/config").get_json()["options_model_backtest_enabled"] is False
