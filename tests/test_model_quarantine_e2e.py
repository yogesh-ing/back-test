"""End-to-end quarantine proof for a model-priced backtest (plan V3 §5, D3).

One model run is created, then walked through every certify/deploy door to prove
the quarantine holds at the API boundary — not just in the payload. The six steps
map the plan's spec onto the real endpoints:

1. POST /api/backtest/options-model            → run_id, certifiable False
2. GET  /api/backtest/runs/<id>                → stored readiness.certifiable False
3. POST /api/optimize/runs (sourceBacktestId)  → 403 + reason
4. POST /api/portfolio/runner/create (lineage) → 403 + reason
5. compare labels                              → both curves carry pricing_feed
6. render_pnl_pdf of a model-priced book       → research-only banner present

Every app is built under pytest, so ``tests/conftest.py`` has set
``BACKTEST_DATA_PROFILE=testing``: synthetic stays enabled and the DB firewall
redirects to sqlite. The ledger is an injected in-memory sqlite store, so the
doors read a real stored row — never the deployment database, never a client flag.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from backtest.api.backtest import _slot_label
from backtest.api.backtest_run_store import BacktestRunLedger
from backtest.db import DatabaseManager
from backtest.db.models import Base
from backtest.reporting.consolidator import ConsolidatedPnL
from backtest.reporting.exports import render_pnl_pdf
from backtest.reporting.records import TradeRecord
from backtest.simulator.fees import TradeSegment
from backtest.web.app import create_app


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
    yield application.test_client(), led
    mgr.disconnect()


_MODEL_REQ = {
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

_REAL_REQ = {
    "strategy": "sma_crossover",
    "symbol": "DEMO",
    "timeframe": "1D",
    "from_date": "2021-01-01",
    "to_date": "2024-01-01",
    "capital": 1_000_000,
    "params": {"fast": 10, "slow": 30},
}


def test_model_run_quarantine_e2e(ledger_client):
    client, _led = ledger_client

    # -- Step 1: a model run is created and stamped uncertifiable at source ----
    resp = client.post("/api/backtest/options-model", json=_MODEL_REQ)
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["persisted"] is True, body
    run_id = body["run_id"]
    assert body["certifiable"] is False
    assert body["certification_reason"] == "model_pricing"
    assert body["pricing_feed"] == "model_bs"

    # -- Step 2: the STORED readiness agrees (read path never re-runs) ---------
    got = client.get(f"/api/backtest/runs/{run_id}")
    assert got.status_code == 200
    stored = got.get_json()["payload"]
    assert stored["pricing_feed"] == "model_bs"
    assert stored["readiness"]["certifiable"] is False
    assert stored["readiness"]["tune_this_available"] is False

    # -- Step 3: Optimize refuses a model baseline (403 + reason) --------------
    opt = client.post(
        "/api/optimize/runs",
        json={"backtestConfig": {"sourceBacktestId": run_id}},
    )
    assert opt.status_code == 403, opt.get_json()
    opt_body = opt.get_json()
    assert opt_body["code"] == "model_priced_uncertifiable"
    assert opt_body["certifiable"] is False
    assert opt_body["certification_reason"] == "model_pricing"
    assert "model-priced" in opt_body["error"]

    # -- Step 4: a paper runner descended from the model run is refused --------
    # The plan's field is preset_from_run; backtest_run_id is the same door.
    for field in ("preset_from_run", "backtest_run_id"):
        spawn = client.post(
            "/api/portfolio/runner/create",
            json={field: run_id, "strategy": "sma_crossover", "symbol": "NIFTY"},
        )
        assert spawn.status_code == 403, (field, spawn.get_json())
        spawn_body = spawn.get_json()
        assert spawn_body["code"] == "model_priced_uncertifiable"
        assert spawn_body["certifiable"] is False
        assert "model-priced" in spawn_body["error"]

    # -- Step 5: compare labels carry each curve's pricing_feed ----------------
    # Decision (a): equity and option curves are never compared on one axis.
    # Where a curve is labelled, its feed is explicit so a model curve is never
    # mistaken for an observed-chain one. The compare page reads these payloads.
    real = client.post("/api/backtest/run", json=_REAL_REQ)
    assert real.status_code == 200, real.get_json()
    real_id = real.get_json()["run_id"]
    real_payload = client.get(f"/api/backtest/runs/{real_id}").get_json()["payload"]
    assert real_payload.get("pricing_feed") in (None, "")

    job = {"strategy": "sma_crossover", "symbol": "DEMO", "params": {}}
    assert "[model BS]" in _slot_label(job, stored)
    assert "[model BS]" not in _slot_label(job, real_payload)
    # A real chain curve would be labelled too (Phase 4 feed).
    assert "[chain]" in _slot_label(job, {"pricing_feed": "chain_history"})

    # -- Step 6: a model-priced statement banners itself as research-only ------
    model_trade = TradeRecord(
        broker="mstock",
        mode="paper",
        segment=TradeSegment.OPTIONS,
        symbol="NIFTY26JUN25000CE",
        quantity=Decimal(50),
        entry_price=Decimal(100),
        exit_price=Decimal(120),
        entry_time=datetime(2026, 6, 1, 10, 0),
        exit_time=datetime(2026, 6, 1, 15, 0),
        gross_pnl=Decimal(1000),
        pricing_feed="model_bs",
    )
    report = ConsolidatedPnL(sources=[_StaticSource([model_trade])]).generate_report(
        "2026-05-01", "2026-06-30"
    )
    assert report.model_priced is True
    blob = render_pnl_pdf(report)
    assert blob.startswith(b"%PDF")
    assert b"MODEL-PRICED" in blob
    assert b"Research only" in blob

    # A clean book carries no such banner (no false alarm on real statements).
    clean_trade = TradeRecord(
        broker="mstock",
        mode="live",
        segment=TradeSegment.EQUITY_DELIVERY,
        symbol="INFY",
        quantity=Decimal(10),
        entry_price=Decimal(100),
        exit_price=Decimal(110),
        entry_time=datetime(2026, 6, 1, 10, 0),
        exit_time=datetime(2026, 6, 1, 15, 0),
        gross_pnl=Decimal(1000),
    )
    clean = ConsolidatedPnL(sources=[_StaticSource([clean_trade])]).generate_report(
        "2026-05-01", "2026-06-30"
    )
    assert clean.model_priced is False
    assert b"MODEL-PRICED" not in render_pnl_pdf(clean)


class _StaticSource:
    name = "static"

    def __init__(self, records):
        self.records = list(records)

    def fetch(self, period):
        return [r for r in self.records if period.contains(r.exit_time)]

    def describe(self):
        return {"kind": "static", "name": self.name, "note": f"{len(self.records)} record(s)"}


def test_preset_lineage_resolves_to_origin_run():
    """preset_id resolves through the optimization store to its backtest run."""
    from backtest.api import model_quarantine as mq

    class _Store:
        def get_preset(self, preset_id):
            return {"backtest_run_id": "run-123"} if preset_id == "p1" else None

    class _Svc:
        store = _Store()

    import backtest.api.optimize as opt

    original = opt._service
    opt._service = lambda: _Svc()
    try:
        assert mq.resolve_lineage_run_id({"preset_id": "p1"}) == "run-123"
        assert mq.resolve_lineage_run_id({"preset_id": "missing"}) is None
        assert mq.resolve_lineage_run_id({"preset_from_run": "direct"}) == "direct"
        assert mq.resolve_lineage_run_id({}) is None
    finally:
        opt._service = original