"""Phase B — segments API + runner-create-with-segment (API-SPEC-001).

* ``GET /api/segments`` returns the configured segments + data routing;
* ``POST /api/segments`` validates (and only persists when asked);
* runner creation accepts ``segment`` and derives mode/broker from it;
* unknown segment / mode conflict → 400 (fail-closed, never guessed).
"""

from __future__ import annotations

import textwrap

import pytest

from backtest.brokers.segments import reset_segments_config
from backtest.brokers.session_manager import reset_default_manager
from backtest.web.app import create_app

YAML = textwrap.dedent(
    """
    segments:
      options_index:
        display_name: "Index Options"
        broker: mstock
        mode: paper
        allocated_capital: 1200000
      equity_intraday:
        display_name: "Equity Intraday"
        broker: dhan
        mode: paper
        allocated_capital: 800000
    data:
      primary: mstock
    """
)


@pytest.fixture()
def api(tmp_path, monkeypatch):
    path = tmp_path / "segments.yaml"
    path.write_text(YAML, encoding="utf-8")
    monkeypatch.setenv("SEGMENTS_CONFIG_PATH", str(path))
    monkeypatch.setenv("BROKER_REMEMBER_SESSION_PATH", str(tmp_path))
    monkeypatch.delenv("BROKER_REMEMBER_SESSION", raising=False)
    reset_segments_config()
    reset_default_manager()
    app = create_app(source="synthetic")
    try:
        yield app.test_client()
    finally:
        reset_segments_config()
        reset_default_manager()


def test_get_segments(api):
    body = api.get("/api/segments").get_json()
    assert body["success"] is True
    names = {s["name"] for s in body["segments"]}
    assert names == {"options_index", "equity_intraday"}
    seg = next(s for s in body["segments"] if s["name"] == "options_index")
    assert seg["broker"] == "mstock"
    assert seg["mode"] == "paper"
    assert seg["allocated_capital"] == 1200000
    assert body["data"]["primary"] == "mstock"


def test_post_segments_validates(api):
    ok = api.post(
        "/api/segments",
        json={"segment_id": "swing", "broker": "dhan", "mode": "paper",
              "allocated_capital": 500000},
    )
    assert ok.status_code == 200
    assert ok.get_json()["success"] is True
    assert ok.get_json()["persisted"] is False

    bad = api.post(
        "/api/segments",
        json={"segment_id": "bad", "broker": "zerodha", "mode": "live"},
    )
    assert bad.status_code == 400
    assert "unknown broker" in bad.get_json()["message"].lower()


def test_create_runner_with_segment(api):
    resp = api.post(
        "/api/portfolio/runner/create",
        json={
            "name": "seg-runner",
            "strategy": "sma_crossover",
            "symbol": "RELIANCE",
            "allocated_capital": 100000,
            "segment": "equity_intraday",
            "source": "synthetic",
        },
    )
    body = resp.get_json()
    assert resp.status_code in (200, 201), body
    runner = body.get("runner") or body
    assert runner["segment"] == "equity_intraday"
    # Segment mode is paper → runner is paper; paper never binds a broker.
    assert runner["mode"] == "paper"
    assert runner["broker"] == "paper"


def test_create_runner_unknown_segment_400(api):
    resp = api.post(
        "/api/portfolio/runner/create",
        json={
            "name": "bad-seg",
            "strategy": "sma_crossover",
            "symbol": "RELIANCE",
            "allocated_capital": 100000,
            "segment": "does_not_exist",
        },
    )
    assert resp.status_code == 400


def test_create_runner_segment_mode_conflict_400(api):
    resp = api.post(
        "/api/portfolio/runner/create",
        json={
            "name": "conflict",
            "strategy": "sma_crossover",
            "symbol": "RELIANCE",
            "allocated_capital": 100000,
            "segment": "equity_intraday",
            "mode": "live",
        },
    )
    assert resp.status_code == 400


def test_create_runner_live_execution_broker_refused_without_session(api):
    """Live + explicit broker with no authenticated session → 409 refused.

    The runner is never half-armed: fail-closed at the gateway (never a
    paper fill under a live label, never a reroute to another broker).
    """
    resp = api.post(
        "/api/portfolio/runner/create",
        json={
            "name": "live-dhan",
            "strategy": "sma_crossover",
            "symbol": "RELIANCE",
            "allocated_capital": 100000,
            "mode": "live",
            "execution_broker": "dhan",
            "source": "dhan",
        },
    )
    assert resp.status_code == 409
    assert "dhan" in resp.get_json()["error"].lower()


def test_create_runner_live_execution_broker_routes_to_dhan(api, monkeypatch):
    """Live + explicit broker with an authenticated session → broker = dhan."""
    from backtest.brokers.base import STATUS_AUTHENTICATED
    from backtest.brokers.session_manager import get_session_manager
    from backtest.forward.portfolio_manager import reset_portfolio_manager

    class _StubDhan:
        broker_name = "dhan"
        broker_display_name = "Dhan"

        def is_authenticated(self):
            return True

        def get_session_status(self):
            return {"status": STATUS_AUTHENTICATED, "expires_at": None}

        def get_session_token(self):
            return "stub-token"

    stub = _StubDhan()
    get_session_manager().set_broker(stub)
    # The live-order confirm gate is a manager-level flag (two-tier live
    # gate): arm it the same way tests/test_position_management.py does.
    monkeypatch.setenv("ALLOW_LIVE_ORDERS", "1")
    # This test is about broker ROUTING, not the certified panel: pretend no
    # kill-switch sentinel row exists so the env gate stays the sole control
    # (a developer's live Cost & Risk Settings would otherwise refuse arming
    # and make this test environment-dependent).
    monkeypatch.setattr(
        "backtest.api.segments_store._panel_configured", lambda: False
    )
    mgr = reset_portfolio_manager(
        auto_start_feed=False,
        tick_seconds=1.0,
        live_broker=stub,
        confirm_live_orders=True,
    )
    try:
        resp = api.post(
            "/api/portfolio/runner/create",
            json={
                "name": "live-dhan-ok",
                "strategy": "sma_crossover",
                "symbol": "RELIANCE",
                "allocated_capital": 100000,
                "mode": "live",
                "execution_broker": "dhan",
                "source": "dhan",
            },
        )
        body = resp.get_json()
        assert resp.status_code in (200, 201), body
        runner = body.get("runner") or body
        assert runner["execution_broker"] == "dhan"
        assert runner["broker"] == "dhan"
    finally:
        mgr.shutdown()
        reset_portfolio_manager(auto_start_feed=False, tick_seconds=1.0)


def test_create_runner_paper_with_dhan_source(api):
    """source=dhan is a first-class runner source — a paper runner bars off
    the Dhan feed while fills stay simulated (2026-10-01 unlock)."""
    resp = api.post(
        "/api/portfolio/runner/create",
        json={
            "name": "paper-dhan",
            "strategy": "sma_crossover",
            "symbol": "RELIANCE",
            "allocated_capital": 100000,
            "mode": "paper",
            "source": "dhan",
        },
    )
    body = resp.get_json()
    assert resp.status_code in (200, 201), body
    runner = body.get("runner") or body
    assert runner["source"] == "dhan"
    assert runner["broker"] == "paper"


def test_create_runner_unknown_execution_broker_400(api):
    resp = api.post(
        "/api/portfolio/runner/create",
        json={
            "name": "bad-broker",
            "strategy": "sma_crossover",
            "symbol": "RELIANCE",
            "allocated_capital": 100000,
            "mode": "live",
            "execution_broker": "zerodha",
        },
    )
    assert resp.status_code == 400
