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

import pytest

from backtest.api.backtest_run_store import BacktestRunLedger
from backtest.db import DatabaseManager
from backtest.db.models import BacktestRun, Base
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


def test_chain_history_not_available_yet(client):
    resp = client.post("/api/backtest/options-model", json=dict(_VALID, pricing_feed="chain_history"))
    assert resp.status_code == 400
    assert "chain_history" in resp.get_json()["error"]


def test_option_strategy_refused(client):
    resp = client.post("/api/backtest/options-model", json=dict(_VALID, strategy="straddle_short_atm"))
    assert resp.status_code == 400
    assert "EQUITY" in resp.get_json()["error"]


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
    # The feed is preselected to the Black-Scholes model; chain history is locked.
    assert 'value="model_bs" selected' in html
    assert 'value="chain_history" disabled' in html
    # The section starts hidden — the kill switch reveals it from /api/config.
    assert 'id="optionsModelSection" hidden' in html


def test_config_flag_drives_the_toggle(client, monkeypatch):
    assert client.get("/api/config").get_json()["options_model_backtest_enabled"] is True
    monkeypatch.setenv("OPTIONS_MODEL_BACKTEST_ENABLED", "false")
    assert client.get("/api/config").get_json()["options_model_backtest_enabled"] is False