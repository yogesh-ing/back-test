"""Backtest run ledger — store + endpoint tests (PRD BACKTEST-RUN-PERSISTENCE, Slice 1).

Covers the PRD's Slice-1 acceptance list: round-trip payload equality,
canonical-hash stability (two insertion orders, ``allow_nan=False``
rejection), sanitizer NaN→NULL, write-failure → 200 + ``persisted=false``
+ alert line, ``payload_version`` refusal (422), ``series_status``
transitions, retention evict-R2-keep-R1, and the read endpoints.

Every DB here is throwaway in-memory SQLite; nothing touches the
deployment database (the ledger only attaches lazily for non-synthetic
sources, and these tests inject an explicit ledger).
"""

from __future__ import annotations

import json
import logging

import pytest
from sqlalchemy import select, update

from backtest.api.backtest_run_store import (
    PAYLOAD_VERSION,
    BacktestRunLedger,
    LedgerError,
    PayloadVersionMismatch,
    code_fingerprint,
    config_hash,
)
from backtest.db import DatabaseManager
from backtest.db.models import BacktestCompareRun, BacktestRun, BacktestRunSeries, Base
from backtest.web.app import create_app

logging.getLogger("backtest").setLevel(logging.WARNING)

_CFG = {
    "strategy": "sma_crossover",
    "symbol": "DEMO",
    "timeframe": "1D",
    "from_date": "2021-01-01",
    "to_date": "2024-01-01",
    "engine": "backtest_driver",
    "capital": 100_000,
    "strategy_params": {"fast": 10, "slow": 30},
    "stop_loss": None,
    "take_profit": None,
    "bars": 750,
}

_PAYLOAD = {
    "config": _CFG,
    "metrics": {
        "total_pnl": 1234.5,
        "total_return_pct": 12.34,
        "cagr_pct": 3.9,
        "max_drawdown_pct": -8.0,
        "sharpe": 1.2,
        "sortino": 1.6,
        "calmar": 0.49,
        "profit_factor": 1.9,
        "win_rate_pct": 55.5,
        "total_trades": 18,
    },
    "trades": [{"n": 1, "entry": 100.0, "exit": 105.0, "pnl": 5.0}],
    "equity": [{"t": "2021-01-04", "v": 100100}],
    "drawdown": [{"t": "2021-01-04", "v": -0.4}],
    "signals": [{"t": "2021-02-01", "kind": "buy"}],
    "benchmark": {"total_return_pct": 5.0},
    "monte_carlo": {"p05": -20.0, "simulations": 200},
    "cost_shock": {"status": "ok", "scenarios": []},
    "readiness": {"status": "green"},
    "provenance": {
        "data_source": "db_candles",
        "engine_used": "backtest_driver",
        "bars_count": 750,
        "data_from": "2021-01-01T09:15:00+05:30",
        "data_to": "2024-01-01T15:30:00+05:30",
        "data_fetch_date": "2024-07-01",
    },
}


def _payload(**overrides):
    p = json.loads(json.dumps(_PAYLOAD))
    p["config"].update(overrides)
    return p


@pytest.fixture()
def manager():
    mgr = DatabaseManager.from_env(profile="testing", url="sqlite:///:memory:")
    mgr.connect()
    # backtest_runs references optimization_runs; with SQLite's
    # foreign_keys=ON pragma the parent table must exist even when the
    # column stays NULL. A real deployment gets this from the migration
    # chain (009-013 before 018).
    Base.metadata.create_all(mgr.engine)
    yield mgr
    mgr.disconnect()


@pytest.fixture()
def ledger(manager):
    led = BacktestRunLedger(manager)
    led.ensure_schema()
    return led


# --- canonical hash ----------------------------------------------------------


def test_config_hash_ignores_insertion_order():
    a = config_hash(_CFG)
    b = config_hash(dict(reversed(list(_CFG.items()))))
    assert a == b


def test_config_hash_rejects_nan_params():
    bad = dict(_CFG, strategy_params={"fast": float("nan")})
    with pytest.raises(ValueError):
        config_hash(bad)


def test_config_hash_sensitive_to_economic_inputs():
    base = config_hash(_CFG)
    assert config_hash(dict(_CFG, capital=99_999)) != base
    assert config_hash(dict(_CFG, engine="quick_screen")) != base
    assert config_hash(dict(_CFG, strategy_params={"fast": 10, "slow": 31})) != base


# --- fingerprint -------------------------------------------------------------


def test_code_fingerprint_hashes_strategy_source():
    from backtest.strategy.registry import get_strategy

    fp = code_fingerprint(get_strategy("sma_crossover"))
    assert fp["strategy_sha256"] and len(fp["strategy_sha256"]) == 64


# --- store writes / reads ------------------------------------------------------


def test_save_run_persists_flat_projection(ledger):
    run_id = ledger.save_run(_payload(), kind="single")
    with ledger.db.session() as s:
        row = s.get(BacktestRun, run_id)
        assert row.kind == "single"
        assert row.strategy_id == "sma_crossover"
        assert row.symbol == "DEMO"
        assert row.payload_version == PAYLOAD_VERSION
        assert row.series_status == "write_failed"  # until save_series
        assert row.code_fingerprint == {}
        # optimizer scale: percent metrics stored as fractions
        assert abs(float(row.total_return) - 0.1234) < 1e-9
        assert abs(float(row.max_drawdown) - (-0.08)) < 1e-9
        assert abs(float(row.win_rate) - 55.5) < 1e-9  # win_rate stays 0-100
        assert row.total_trades == 18
        assert row.data_source == "db_candles"
        assert row.bars_count == 750


def test_save_run_requires_dates(ledger):
    p = _payload()
    p["config"].pop("from_date")
    with pytest.raises(LedgerError):
        ledger.save_run(p)


def test_save_run_sanitizes_nan_metrics(ledger):
    p = _payload()
    p["metrics"]["sharpe"] = float("nan")
    p["metrics"]["total_pnl"] = float("inf")
    run_id = ledger.save_run(p)
    ledger.save_series(run_id, p)
    rec = ledger.get_run(run_id)
    assert rec["payload"]["metrics"]["sharpe"] is None
    assert rec["payload"]["metrics"]["total_pnl"] is None
    with ledger.db.session() as s:
        row = s.get(BacktestRun, run_id)
        assert row.sharpe is None  # flat column poisoned by the same NaN → NULL


def test_round_trip_is_byte_identical(ledger):
    p = _payload()
    run_id = ledger.save_run(p, kind="single")
    ledger.save_series(run_id, p)
    rec = ledger.get_run(run_id)
    assert rec["payload"]["config"] == json.loads(json.dumps(p["config"]))
    assert rec["payload"]["metrics"] == json.loads(json.dumps(p["metrics"]))
    assert rec["payload"]["trades"] == p["trades"]
    assert rec["payload"]["equity"] == p["equity"]
    assert rec["payload"]["drawdown"] == p["drawdown"]
    assert rec["payload"]["signals"] == p["signals"]
    assert rec["payload"]["benchmark"] == p["benchmark"]
    assert rec["payload"]["monte_carlo"] == p["monte_carlo"]
    assert rec["payload"]["cost_shock"] == p["cost_shock"]
    assert rec["payload"]["readiness"] == p["readiness"]
    assert rec["payload"]["provenance"] == p["provenance"]
    assert rec["ledger"]["series_status"] == "present"


def test_series_status_flips_in_same_transaction(ledger):
    run_id = ledger.save_run(_payload())
    with ledger.db.session() as s:
        assert s.get(BacktestRun, run_id).series_status == "write_failed"
    ledger.save_series(run_id, _payload())
    with ledger.db.session() as s:
        assert s.get(BacktestRun, run_id).series_status == "present"
        assert s.get(BacktestRunSeries, run_id) is not None


def test_get_run_refuses_foreign_payload_version(ledger):
    run_id = ledger.save_run(_payload())
    with ledger.db.session() as s:
        s.execute(
            update(BacktestRun)
            .where(BacktestRun.run_id == run_id)
            .values(payload_version=PAYLOAD_VERSION + 1)
        )
    with pytest.raises(PayloadVersionMismatch):
        ledger.get_run(run_id)


def test_get_run_missing_returns_none(ledger):
    assert ledger.get_run("00000000-0000-4000-8000-000000000000") is None


def test_retention_evicts_series_keeps_parent(ledger):
    run_id = ledger.save_run(_payload())
    ledger.save_series(run_id, _payload())
    assert ledger.sweep_series(cap=0) == 1
    with ledger.db.session() as s:
        row = s.get(BacktestRun, run_id)
        assert row is not None, "R1 must survive retention"
        assert row.series_status == "evicted"
        assert row.sharpe is not None, "flat metrics still listed"
        assert s.get(BacktestRunSeries, run_id) is None


def test_retention_only_touches_present_rows(ledger):
    run_id = ledger.save_run(_payload())  # series never written
    assert ledger.sweep_series(cap=0) == 0
    with ledger.db.session() as s:
        assert s.get(BacktestRun, run_id).series_status == "write_failed"


def test_list_runs_filters_and_pages(ledger):
    for i in range(3):
        ledger.save_run(_payload(symbol=f"SYM{i}"), kind="single")
    ledger.save_run(_payload(strategy="rsi_reversion"), kind="single")
    body = ledger.list_runs(strategy="sma_crossover")
    assert body["total"] == 3
    assert {r["symbol"] for r in body["runs"]} == {"SYM0", "SYM1", "SYM2"}
    page = ledger.list_runs(limit=2, offset=2)
    assert page["total"] == 4 and len(page["runs"]) == 2
    assert ledger.list_runs(symbol="sym0")["total"] == 1  # case-normalised
    assert ledger.list_runs(kind="compare_slot")["total"] == 0


def test_fail_persist_records_error_and_returns_fields(ledger):
    fields = ledger.fail_persist("backtest/run", RuntimeError("boom"))
    assert fields == {
        "persisted": False,
        "run_id": None,
        "persist_error": "RuntimeError: boom",
    }
    assert "backtest/run: RuntimeError: boom" in ledger.last_persist_error
    st = ledger.stats()
    assert st["last_persist_error"] == ledger.last_persist_error


# --- endpoints ----------------------------------------------------------------


@pytest.fixture()
def app_client(ledger):
    app = create_app(source="synthetic")
    # Injected ledger: the endpoint persists through it even though the app's
    # data source is synthetic (which alone would never attach a DB).
    app.config["BACKTEST_LEDGER"] = ledger
    return app, app.test_client()


_VALID_RUN = {
    "strategy": "sma_crossover",
    "symbol": "DEMO",
    "timeframe": "1D",
    "from_date": "2021-01-01",
    "to_date": "2022-01-01",
    "capital": 100_000,
    "params": {"fast": 10, "slow": 30},
}


def test_run_response_carries_persisted_fields(app_client):
    _app, client = app_client
    resp = client.post("/api/backtest/run", json=_VALID_RUN)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["persisted"] is True
    assert body["run_id"]
    assert body["persist_error"] is None


def test_synthetic_app_without_ledger_keeps_plain_shape():
    client = create_app(source="synthetic").test_client()
    resp = client.post("/api/backtest/run", json=_VALID_RUN)
    body = resp.get_json()
    assert "persisted" not in body and "run_id" not in body


def test_ledger_write_failure_still_returns_200(app_client, caplog):
    _app, client = app_client
    real = client.application.config["BACKTEST_LEDGER"]

    class Exploding(BacktestRunLedger):
        def save_run(self, *a, **k):
            raise RuntimeError("disk gone")

    client.application.config["BACKTEST_LEDGER"] = Exploding(real.db)
    with caplog.at_level(logging.ERROR, logger="backtest.ledger"):
        resp = client.post("/api/backtest/run", json=_VALID_RUN)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["persisted"] is False
    assert "disk gone" in body["persist_error"]
    assert "persist_failed" in caplog.text


def test_series_failure_keeps_persisted_true(app_client):
    _app, client = app_client
    real = client.application.config["BACKTEST_LEDGER"]

    class SeriesFails(BacktestRunLedger):
        def save_series(self, run_id, payload):
            raise RuntimeError("series blob rejected")

    flaky = SeriesFails(real.db)
    client.application.config["BACKTEST_LEDGER"] = flaky
    resp = client.post("/api/backtest/run", json=_VALID_RUN)
    body = resp.get_json()
    assert resp.status_code == 200 and body["persisted"] is True
    rec = flaky.get_run(body["run_id"])
    assert rec["ledger"]["series_status"] == "write_failed"
    assert rec["payload"]["config"]["strategy"] == "sma_crossover"


def test_runs_list_and_detail_endpoints(app_client):
    _app, client = app_client
    run_id = client.post("/api/backtest/run", json=_VALID_RUN).get_json()["run_id"]

    lst = client.get("/api/backtest/runs?strategy=sma_crossover").get_json()
    assert lst["total"] == 1
    entry = lst["runs"][0]
    assert entry["run_id"] == run_id
    assert entry["series_status"] == "present"
    assert "trades" not in entry and "config" not in entry  # list excludes blobs

    detail = client.get(f"/api/backtest/runs/{run_id}")
    assert detail.status_code == 200
    d = detail.get_json()
    assert isinstance(d["payload"]["trades"], list)
    assert d["payload"]["equity"] and d["payload"]["config"]["strategy"] == "sma_crossover"
    assert d["ledger"]["run_id"] == run_id

    assert client.get("/api/backtest/runs/00000000-0000-4000-8000-000000000000").status_code == 404


def test_run_detail_422_on_payload_version_mismatch(app_client):
    _app, client = app_client
    ledger = client.application.config["BACKTEST_LEDGER"]
    run_id = client.post("/api/backtest/run", json=_VALID_RUN).get_json()["run_id"]
    with ledger.db.session() as s:
        s.execute(
            update(BacktestRun)
            .where(BacktestRun.run_id == run_id)
            .values(payload_version=PAYLOAD_VERSION + 1)
        )
    resp = client.get(f"/api/backtest/runs/{run_id}")
    assert resp.status_code == 422
    assert resp.get_json()["code"] == "payload_version_mismatch"


def test_run_stats_endpoint(app_client):
    _app, client = app_client
    client.post("/api/backtest/run", json=_VALID_RUN)
    st = client.get("/api/backtest/runs/stats").get_json()
    assert st["total_runs"] == 1
    assert st["last_24h"] == 1
    assert st["series_rows"] == 1
    assert st["series_bytes_total"] > 0
    assert st["series_evicted"] == 0


def test_run_list_rejects_bad_pagination(app_client):
    _app, client = app_client
    assert client.get("/api/backtest/runs?limit=abc").status_code == 400
    assert client.get("/api/backtest/runs?min_sharpe=abc").status_code == 400


def test_read_endpoints_503_without_ledger():
    app = create_app(source="synthetic")
    app.config["BACKTEST_LEDGER"] = None
    client = app.test_client()
    assert client.get("/api/backtest/runs").status_code == 503
    assert client.get("/api/backtest/runs/stats").status_code == 503
    assert client.get("/api/backtest/runs/whatever").status_code == 503


# --- compare ledger (Slice 2, PRD R1b/R3) -------------------------------------


def _prov(**over):
    prov = {
        "data_source": "db_candles",
        "date_range": {"from": "2021-01-01", "to": "2022-01-01"},
        "data_from": "2021-01-01T09:15:00+05:30",
        "data_to": "2024-01-01T15:30:00+05:30",
        "symbols_used": ["DEMO"],
        "engines_used": ["backtest_driver"],
        "comparison_mode": "strategies",
    }
    prov.update(over)
    return prov


def _two_children():
    a = _payload(symbol="AAA")
    b = _payload(symbol="BBB", strategy_params={"fast": 5, "slow": 40})
    return a, b


def test_save_compare_writes_parent_and_children(ledger):
    a, b = _two_children()
    out = ledger.save_compare(
        config_snapshot={"shared": {"capital": 100000}, "slots": [{"id": 1}, {"id": 2}]},
        provenance=_prov(),
        comparison_block={"correlation": [[1.0]]},
        slot_errors={"3": {"error": "boom"}},
        children=[("1", a), ("2", b)],
    )
    assert set(out["child_run_ids"]) == {"1", "2"}
    with ledger.db.session() as s:
        parent = s.get(BacktestCompareRun, out["compare_id"])
        assert parent.slot_count == 3  # 2 children + 1 error slot
        assert parent.date_from.isoformat() == "2021-01-01"  # from date_range
        assert parent.symbols_used == ["DEMO"]
        assert parent.comparison_version == 1
        kids = (
            s.execute(
                select(BacktestRun).where(
                    BacktestRun.parent_compare_id == out["compare_id"]
                )
            )
            .scalars()
            .all()
        )
        assert {k.kind for k in kids} == {"compare_slot"}
        assert {k.run_id for k in kids} == set(out["child_run_ids"].values())


def test_save_compare_is_atomic(ledger):
    a, b = _two_children()
    b["config"].pop("from_date")  # child would raise LedgerError
    with pytest.raises(LedgerError):
        ledger.save_compare(
            config_snapshot={"shared": {}},
            provenance=_prov(),
            comparison_block=None,
            slot_errors={},
            children=[("1", a), ("2", b)],
        )
    with ledger.db.session() as s:
        assert s.execute(select(BacktestCompareRun)).scalars().all() == []
        assert s.execute(select(BacktestRun)).scalars().all() == []


def test_get_compare_round_trip_and_children_order(ledger):
    a, b = _two_children()
    out = ledger.save_compare(
        config_snapshot={"shared": {"capital": 1}, "slots": []},
        provenance=_prov(),
        comparison_block={"sharpe_spread": 0.4},
        slot_errors={"9": {"error": "nope"}},
        children=[("1", a), ("2", b)],
    )
    rec = ledger.get_compare(out["compare_id"])
    assert rec["ledger"]["compare_id"] == out["compare_id"]
    assert rec["payload"]["comparison"] == {"sharpe_spread": 0.4}
    assert rec["payload"]["slot_errors"] == {"9": {"error": "nope"}}
    assert rec["payload"]["config"]["shared"] == {"capital": 1}
    # created_at ties at SQLite second granularity, so membership not order.
    assert {c["run_id"] for c in rec["children"]} == set(out["child_run_ids"].values())
    assert ledger.get_compare("00000000-0000-4000-8000-000000000000") is None


def test_get_compare_refuses_foreign_comparison_version(ledger):
    a, _b = _two_children()
    out = ledger.save_compare(
        config_snapshot={"shared": {}},
        provenance=_prov(),
        comparison_block={},
        slot_errors={},
        children=[("1", a)],
    )
    with ledger.db.session() as s:
        row = s.get(BacktestCompareRun, out["compare_id"])
        row.comparison_version = 99
    with pytest.raises(PayloadVersionMismatch):
        ledger.get_compare(out["compare_id"])


def test_list_compares_pages_and_excludes_blobs(ledger):
    a, b = _two_children()
    for _ in range(3):
        ledger.save_compare(
            config_snapshot={"shared": {}, "slots": []},
            provenance=_prov(),
            comparison_block={"big": "blob"},
            slot_errors={},
            children=[("1", a)],
        )
    body = ledger.list_compares(limit=2)
    assert body["total"] == 3 and len(body["compares"]) == 2
    row = body["compares"][0]
    assert "comparison_block" not in row and "config_snapshot" not in row
    assert row["slot_count"] == 1


def test_run_many_response_carries_persisted_fields(app_client):
    _app, client = app_client
    body = {
        "shared": {
            "symbol": "DEMO",
            "from_date": "2021-01-01",
            "to_date": "2022-01-01",
            "capital": 100_000,
        },
        "slots": [
            {"id": 1, "strategy": "sma_crossover", "timeframe": "1D",
             "params": {"fast": 10, "slow": 30}},
            {"id": 2, "strategy": "buy_and_hold", "timeframe": "1D", "params": {}},
        ],
    }
    resp = client.post("/api/backtest/run-many", json=body)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["persisted"] is True and data["persist_error"] is None
    assert data["compare_id"]
    assert set(data["results"]) == {"1", "2"}

    lst = client.get("/api/backtest/compares").get_json()
    assert lst["total"] == 1 and lst["compares"][0]["compare_id"] == data["compare_id"]
    detail = client.get(f"/api/backtest/compares/{data['compare_id']}")
    assert detail.status_code == 200
    kids = detail.get_json()["children"]
    assert {k["kind"] for k in kids} == {"compare_slot"}
    assert {k["series_status"] for k in kids} == {"present"}
    missing = client.get("/api/backtest/compares/00000000-0000-4000-8000-000000000000")
    assert missing.status_code == 404
    client.application.config["BACKTEST_LEDGER"] = None
    assert client.get("/api/backtest/compares").status_code == 503


def test_run_many_without_ledger_keeps_plain_shape():
    client = create_app(source="synthetic").test_client()
    resp = client.post(
        "/api/backtest/run-many",
        json={
            "shared": {
                "symbol": "DEMO",
                "from_date": "2021-01-01",
                "to_date": "2022-01-01",
            },
            "slots": [
                {"id": 1, "strategy": "buy_and_hold", "timeframe": "1D", "params": {}}
            ],
        },
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert "persisted" not in body and "compare_id" not in body


# --- optimizer baseline (Slice 2, PRD R6) --------------------------------------

_OPT_METRICS = {
    "sharpe": 1.2,
    "sortino": 1.6,
    "calmar": 0.49,
    "total_return": 0.1234,  # optimizer namespace already stores fractions
    "cagr": 0.039,
    "max_drawdown": -0.08,
    "profit_factor": 1.9,
    "win_rate": 55.5,  # and win_rate already 0-100
    "total_trades": 18,
}


def _opt_parent_run(ledger):
    from backtest.db.models import OptimizationRun

    with ledger.db.session() as s:
        parent = OptimizationRun(
            strategy_id="sma_crossover",
            objective_function="sharpe",
            method="grid",
            param_space={},
            backtest_config={},
        )
        s.add(parent)
        s.flush()
        return parent.run_id


def test_save_optimizer_baseline_direct_scale(ledger):
    opt_id = _opt_parent_run(ledger)
    run_id = ledger.save_optimizer_baseline(
        optimization_run_id=opt_id,
        config=dict(_CFG),
        metrics=dict(_OPT_METRICS),
        provenance={"data_source": "db_candles", "bars_count": 750},
    )
    with ledger.db.session() as s:
        row = s.get(BacktestRun, run_id)
        assert row.kind == "optimizer_baseline"
        assert row.optimization_run_id == opt_id
        assert row.series_status == "write_failed"  # no series row by design
        assert abs(float(row.total_return) - 0.1234) < 1e-9  # NOT divided by 100
        assert abs(float(row.max_drawdown) - (-0.08)) < 1e-9
        assert abs(float(row.win_rate) - 55.5) < 1e-9
        assert row.total_trades == 18
    rec = ledger.get_run(run_id)
    assert rec["payload"]["config"]["strategy"] == "sma_crossover"
    assert rec["payload"]["metrics"]["sharpe"] == 1.2


def test_save_optimizer_baseline_requires_dates(ledger):
    cfg = dict(_CFG)
    cfg.pop("from_date")
    with pytest.raises(LedgerError):
        ledger.save_optimizer_baseline(
            optimization_run_id="00000000-0000-4000-8000-000000000000",
            config=cfg,
            metrics={},
        )


def test_service_records_baseline_only_on_fresh_path():
    from types import SimpleNamespace

    from backtest.optimization.service import OptimizationService

    calls = []

    class FakeLedger:
        def save_optimizer_baseline(self, **kw):
            calls.append(kw)
            return "f" * 32

        def fail_persist(self, where, err):
            calls.append({"fail": where, "err": str(err)})

    service = OptimizationService.__new__(OptimizationService)
    service._bt_ledger = FakeLedger()
    cfg = SimpleNamespace(
        strategy_id="sma_crossover",
        baseline_params={"fast": 10, "slow": 30},
        backtest=SimpleNamespace(
            symbol="DEMO",
            timeframe="1D",
            start_date="2021-01-01",
            end_date="2022-01-01",
            engine="backtest_driver",
            initial_capital=100_000,
        ),
    )
    fresh = {"metrics": dict(_OPT_METRICS), "score": 1.2, "error": None}
    service._record_baseline(cfg, "a" * 32, fresh, {})
    assert len(calls) == 1
    written = calls[0]
    assert written["optimization_run_id"] == "a" * 32
    assert written["config"]["strategy"] == "sma_crossover"
    assert written["config"]["from_date"] == "2021-01-01"
    assert written["config"]["strategy_params"] == {"fast": 10, "slow": 30}
    assert written["metrics"] == _OPT_METRICS

    class Exploding(FakeLedger):
        def save_optimizer_baseline(self, **kw):
            raise RuntimeError("db gone")

        def fail_persist(self, where, err):
            calls.append({"fail": where})

    service._bt_ledger = Exploding()
    service._record_baseline(cfg, "b" * 32, fresh, {})  # must not raise
    assert calls[-1] == {"fail": "optimizer-baseline/bbbbbbbb"}


# --- UI wiring + preset lineage (Slice 3, PRD R3/R4) --------------------------


def test_preset_lineage_records_backtest_run(ledger):
    """A preset saved from a plain backtest names the ledger row (R3)."""
    from backtest.optimization.store import OptimizationStore

    store = OptimizationStore(ledger.db)
    run_id = ledger.save_run(_payload())
    preset = store.create_preset(
        strategy_id="sma_crossover",
        name="From backtest",
        params={"fast": 10, "slow": 30},
        backtest_run_id=run_id,
    )
    assert preset["backtest_run_id"] == run_id
    plain = store.create_preset(
        strategy_id="sma_crossover", name="Manual", params={"fast": 11}
    )
    assert plain["backtest_run_id"] is None


def test_backtest_page_wires_ledger_ui():
    client = create_app(source="synthetic").test_client()
    page = client.get("/backtest")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    for element_id in ("recentRuns", "persistBadge", "seriesNotice", "chartCard"):
        assert f'id="{element_id}"' in html

    js = client.get("/static/js/backtest.js").get_data(as_text=True)
    # localStorage run cache is gone; history is server-backed (R4).
    assert "backtest_recent_runs" not in js
    assert "openStoredRun" in js and "/api/backtest/runs" in js


def test_compare_page_wires_history_ui():
    client = create_app(source="synthetic").test_client()
    html = client.get("/compare").get_data(as_text=True)
    assert 'id="compareHistory"' in html
    assert 'id="comparePersistBadge"' in html
    js = client.get("/static/js/compare.js").get_data(as_text=True)
    assert "openStoredCompare" in js and "/api/backtest/compares" in js
