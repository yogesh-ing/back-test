"""PRD Task 4.3 — Forward API endpoint tests.

Task 4.2 added a server-side auth guard to POST /api/forward/start:
without an authenticated broker session the endpoint returns 403.
The existing tests inject an authenticated stub broker so they keep passing.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any

import pytest

from backtest.api import forward as fwd
from backtest.brokers.base import (
    STATUS_AUTHENTICATED,
    STATUS_EXPIRING_SOON,
    STATUS_EXPIRED,
    STATUS_UNAUTHENTICATED,
    BrokerAuthBase,
)
from backtest.brokers.session_manager import get_session_manager, reset_default_manager
from backtest.web.app import create_app


class _ForwardStubBroker(BrokerAuthBase):
    """Scriptable broker for forward-endpoint tests."""

    broker_name = "stub"
    broker_display_name = "Stub Broker"

    def __init__(self, status: str = STATUS_UNAUTHENTICATED) -> None:
        self._status = status
        self._expires_at: str | None = (
            (datetime.now() + timedelta(hours=2)).isoformat()
            if status in (STATUS_AUTHENTICATED, STATUS_EXPIRING_SOON)
            else None
        )

    def login(self, username: str, password: str) -> dict[str, Any]:
        return {"success": True, "message": "", "requires_totp": True}

    def verify_totp(self, totp_code: str) -> dict[str, Any]:
        self._status = STATUS_AUTHENTICATED
        self._expires_at = (datetime.now() + timedelta(hours=2)).isoformat()
        return {"success": True, "message": "", "expires_at": self._expires_at}

    def get_session_status(self) -> dict[str, Any]:
        return {
            "status": self._status,
            "expires_at": self._expires_at,
            "broker": self.broker_name,
        }

    def get_session_token(self) -> str | None:
        return "tok" if self._status in (STATUS_AUTHENTICATED, STATUS_EXPIRING_SOON) else None

    def logout(self) -> None:
        self._status = STATUS_UNAUTHENTICATED
        self._expires_at = None

    # Helper for tests to change state
    def set_status(self, status: str) -> None:
        self._status = status
        if status in (STATUS_AUTHENTICATED, STATUS_EXPIRING_SOON):
            self._expires_at = (datetime.now() + timedelta(hours=2)).isoformat()
        elif status == STATUS_EXPIRED:
            self._expires_at = None
        else:
            self._expires_at = None


@pytest.fixture()
def stub_authenticated():
    """An authenticated stub broker (existing tests need this to pass the 403 guard)."""
    return _ForwardStubBroker(status=STATUS_AUTHENTICATED)


@pytest.fixture()
def client(stub_authenticated):
    """Authenticated app client with the replay clock FROZEN.

    ``replay_speed=0`` means nothing moves until a test calls
    ``ForwardSession.advance()`` — so assertions about the cursor are exact
    instead of racing a timer.
    """
    reset_default_manager()
    get_session_manager().set_broker(stub_authenticated)
    app = create_app(source="synthetic", replay_speed=0)
    try:
        yield app.test_client()
    finally:
        reset_default_manager()


def _session(client, state_id=None):
    """The live ForwardSession object behind an id (or the active one)."""
    return fwd._get_session(state_id)


def _start(client, **overrides):
    resp = client.post("/api/forward/start", json={**_CFG, "bars_per_second": 0, **overrides})
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


@pytest.fixture()
def client_unauthenticated():
    """App client with an UN-authenticated broker (for 403 guard tests)."""
    reset_default_manager()
    stub = _ForwardStubBroker(status=STATUS_UNAUTHENTICATED)
    get_session_manager().set_broker(stub)
    app = create_app(source="synthetic")
    try:
        yield app.test_client(), stub
    finally:
        reset_default_manager()


@pytest.fixture(autouse=True)
def _reset_forward_session():
    fwd._reset_session()
    yield
    fwd._reset_session()


_CFG = {
    "strategy": "sma_crossover", "symbol": "DEMO", "timeframe": "1D",
    "from_date": "2024-01-01", "to_date": "2024-12-31",
    "capital": 10_000, "params": {"fast": 10, "slow": 30},
}


def test_status_idle_before_start(client):
    assert client.get("/api/forward/status").get_json()["status"] == "idle"


def test_start_valid_returns_running(client):
    resp = client.post("/api/forward/start", json=_CFG)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "running"
    assert body["total"] > 50 and body["revealed"] <= body["total"]


def test_start_unknown_strategy_returns_400(client):
    resp = client.post("/api/forward/start", json={**_CFG, "strategy": "nope"})
    assert resp.status_code == 400 and "error" in resp.get_json()


# --- date-range contract (gap G5) -------------------------------------------
# PRD Task 4.3 defines the start body as {strategy, symbol, params}, so the
# window is optional. Optional must mean "explicitly defaulted", never silent:
# the response reports what it filled in, and a range that cannot work is 400.


def test_start_without_dates_defaults_and_reports_it(client):
    body = {k: v for k, v in _CFG.items() if k not in ("from_date", "to_date")}
    resp = client.post("/api/forward/start", json=body)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["defaults_applied"] == ["from_date", "to_date"]
    assert data["config"]["from_date"] == fwd.DEFAULT_FROM_DATE
    assert data["config"]["to_date"] >= "2024-12-31"


def test_start_with_dates_reports_no_defaults(client):
    resp = client.post("/api/forward/start", json=_CFG)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["defaults_applied"] == []
    assert data["config"]["from_date"] == "2024-01-01"
    assert data["config"]["to_date"] == "2024-12-31"


@pytest.mark.parametrize(
    "overrides",
    [
        {"from_date": "01-01-2024"},                       # wrong format
        {"from_date": "not-a-date"},                      # nonsense
        {"to_date": "2024-13-45"},                        # impossible date
        {"from_date": "2024-12-31", "to_date": "2024-01-01"},   # inverted
    ],
)
def test_start_rejects_unusable_dates(client, overrides):
    body = {**_CFG, **overrides}
    resp = client.post("/api/forward/start", json=body)
    assert resp.status_code == 400
    assert "date" in resp.get_json()["error"] or "to_date" in resp.get_json()["error"]


def test_start_date_errors_are_logged(client, caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="backtest.api.forward"):
        resp = client.post("/api/forward/start", json={**_CFG, "to_date": "yesterday"})
    assert resp.status_code == 400
    assert "YYYY-MM-DD" in caplog.text


def test_status_shape_matches_adapter_plus_live_fields(client):
    client.post("/api/forward/start", json=_CFG)
    body = client.get("/api/forward/status").get_json()
    # adapter shape (reusable components) + forward-specific fields
    for key in ("metrics", "equity", "drawdown", "trades", "signals", "config",
                "positions", "progress", "status"):
        assert key in body
    assert body["status"] == "running"
    assert 0 <= body["progress"]["pct"] <= 100
    assert isinstance(body["positions"], list)
    assert {"total_pnl", "win_rate_pct", "sharpe", "total_trades"} <= set(body["metrics"])
    # config metadata is carried onto the live snapshot (used by the dashboard)
    assert body["config"]["strategy"] == "sma_crossover"
    assert body["config"]["symbol"] == "DEMO"


def test_polling_never_advances_the_clock(client):
    """The replay is clock-driven now (gap G4).

    Poll-advance meant two open tabs (or the Dashboard's 3 s refresh) each pushed
    the same run forward, and closing the browser froze the bot.
    """
    started = _start(client)
    sid = started["state_id"]

    def cursor():
        body = client.get(f"/api/forward/status?state_id={sid}").get_json()
        return body["progress"]["revealed"]

    first = cursor()
    for _ in range(5):
        assert cursor() == first, "a poll must not reveal bars — that is the server clock's job"
    # …and the cursor only moves when the clock ticks.
    _session(client, sid).advance(6)
    assert cursor() == first + 6


def test_clock_advances_without_any_polling(client):
    """With the clock running, the replay progresses while nobody watches."""
    started = client.post("/api/forward/start", json={**_CFG, "bars_per_second": 40}).get_json()
    sid = started["state_id"]
    before = client.get(f"/api/forward/status?state_id={sid}").get_json()["progress"]["revealed"]
    time.sleep(0.5)          # 40 bars/s ⇒ ≥ 10 more bars revealed, no /status in between
    after = client.get(f"/api/forward/status?state_id={sid}").get_json()["progress"]["revealed"]
    assert after > before, "server-side clock must advance on its own"


def test_replay_completes_and_auto_stops(client):
    started = _start(client)
    sid = started["state_id"]
    session = _session(client, sid)
    session.advance(session.total)          # run the clock to the end
    final = client.get(f"/api/forward/status?state_id={sid}").get_json()
    assert final["progress"]["pct"] == 100.0
    assert final["status"] == "stopped"      # auto-stop at the last bar


# ---------------------------------------------------------------------------
# No-lookahead in the live payload (gap G4)
# ---------------------------------------------------------------------------


def test_signals_never_leak_beyond_the_revealed_prefix(client):
    started = _start(client)
    sid = started["state_id"]
    session = _session(client, sid)
    for _ in range(3):
        session.advance(4)
        snap = client.get(f"/api/forward/status?state_id={sid}").get_json()
        cutoff = snap["last_bar_ts"]
        future = [x for x in snap["signals"]["buys"] + snap["signals"]["sells"]
                  if x["date"] > cutoff]
        assert not future, f"at bar {snap['progress']['revealed']}, payload exposed {future}"
        assert len(snap["signals"]["candles"]) == snap["progress"]["revealed"]


# ---------------------------------------------------------------------------
# Live panel payloads the forward page renders (gap G3)
# ---------------------------------------------------------------------------


def test_metrics_are_real_numbers_not_zeros(client):
    """The prefix snapshot used to carry an empty metrics dict, so every card read 0."""
    started = _start(client)
    sid = started["state_id"]
    session = _session(client, sid)
    session.advance(60)                       # enough for fast/slow 10/30 to trade
    metrics = client.get(f"/api/forward/status?state_id={sid}").get_json()["metrics"]
    assert metrics["total_trades"] >= 1
    assert metrics["final_equity"] != _CFG["capital"], "equity must move as the replay runs"
    assert {"total_pnl", "total_return_pct", "win_rate_pct", "max_drawdown_pct", "sharpe",
            "closed_trades", "open_trades"} <= set(metrics)


def test_positions_are_marked_to_market_as_the_replay_runs(client):
    """A freshly opened trade legitimately has entry == current; the point is that
    the numbers then *diverge* and P&L moves (gap G3: they were welded together, so
    the live panel could never show anything but 0).
    """
    started = _start(client)
    sid = started["state_id"]
    session = _session(client, sid)
    moved, seen_open = None, False
    for _ in range(40):
        session.advance(2)
        positions = client.get(f"/api/forward/status?state_id={sid}").get_json()["positions"]
        if not positions:
            continue
        seen_open = True
        pos = positions[0]
        if pos["bars_held"] >= 1 and pos["entry"] != pos["current"]:
            moved = pos
            break
    assert seen_open, "expected an open position somewhere in the replay"
    assert moved, "entry never differed from the current price — the mark is frozen"
    assert {"exposure_pct", "price_change_pct", "unrealized_pnl_pct", "bars_held"} <= set(moved)
    assert abs(moved["unrealized_pnl"]) > 0


def test_equity_payload_is_component_compatible(client):
    """renderEquityChart wants {dates, values, benchmark} — an array silently no-ops."""
    started = _start(client)
    sid = started["state_id"]
    session = _session(client, sid)
    session.advance(12)
    snap = client.get(f"/api/forward/status?state_id={sid}").get_json()
    eq = snap["equity"]
    assert isinstance(eq, dict) and {"dates", "values", "benchmark"} <= set(eq)
    # one bar is revealed at /start, so the curve length tracks the cursor exactly
    assert len(eq["dates"]) == len(eq["values"]) == len(eq["benchmark"]) == session.revealed
    assert all(isinstance(v, (int, float)) for v in eq["values"])


def test_drawdown_payload_matches(client):
    started = _start(client)
    sid = started["state_id"]
    _session(client, sid).advance(20)
    dd = client.get(f"/api/forward/status?state_id={sid}").get_json()["drawdown"]
    assert {"dates", "values", "worst_dd_pct", "worst_dd_date"} <= set(dd)
    assert dd["worst_dd_pct"] <= 0.0


# ---------------------------------------------------------------------------
# Sessions are keyed (gap G4) — no more single hidden global
# ---------------------------------------------------------------------------


def test_start_returns_a_usable_state_id(client):
    started = _start(client)
    assert started["state_id"] and started["state_id"] != "None"
    sid = started["state_id"]
    assert client.get(f"/api/forward/status?state_id={sid}").get_json()["state_id"] == sid


def test_two_sessions_are_independent(client):
    a = _start(client, symbol="DEMO")
    b = _start(client, symbol="INFY")
    assert a["state_id"] != b["state_id"]

    def revealed(state_id):
        body = client.get(f"/api/forward/status?state_id={state_id}").get_json()
        return body["progress"]["revealed"], body["status"]

    _session(client, a["state_id"]).advance(10)
    (pa, _), (pb, _) = revealed(a["state_id"]), revealed(b["state_id"])
    assert pa == 11 and pb == 1, f"cross-talk between sessions: {pa} vs {pb}"
    # Stopping one leaves the other running.
    stopped = client.post("/api/forward/stop", json={"state_id": a["state_id"]})
    assert stopped.get_json()["status"] == "stopped"
    assert revealed(a["state_id"])[1] == "stopped"
    assert revealed(b["state_id"])[1] == "running"


def test_unknown_state_id_is_404(client):
    started = _start(client)
    assert client.get("/api/forward/status?state_id=nope").status_code == 404
    assert client.post("/api/forward/stop", json={"state_id": "nope"}).status_code == 404
    # No id at all → the active session (Dashboard keeps working without one).
    assert client.get("/api/forward/status").get_json()["state_id"] == started["state_id"]


def test_sessions_endpoint_lists_replays(client):
    a = _start(client, symbol="DEMO")
    b = _start(client, symbol="INFY")
    body = client.get("/api/forward/sessions").get_json()["sessions"]
    ids = {row["state_id"] for row in body}
    assert {a["state_id"], b["state_id"]} <= ids
    assert body[0]["active"] is True, "newest session is the active one"
    assert {"strategy", "symbol", "status", "pct", "bars_per_second"} <= set(body[0])


def test_speed_can_be_overridden_per_start(client):
    started = client.post("/api/forward/start", json={**_CFG, "bars_per_second": 7.5}).get_json()
    assert started["bars_per_second"] == 7.5
    _session(client, started["state_id"]).stop()
    frozen = client.post("/api/forward/start", json={**_CFG, "bars_per_second": 0}).get_json()
    assert frozen["bars_per_second"] == 0


def test_negative_speed_freezes_the_clock(client):
    started = client.post("/api/forward/start", json={**_CFG, "bars_per_second": -5}).get_json()
    assert started["bars_per_second"] == 0


def test_stop_halts_progress(client):
    started = _start(client)
    sid = started["state_id"]
    session = _session(client, sid)
    session.advance(5)
    before = client.get(f"/api/forward/status?state_id={sid}").get_json()["progress"]["pct"]
    stopped = client.post("/api/forward/stop", json={"state_id": sid})
    assert stopped.get_json()["status"] == "stopped"
    session.advance(5)          # a stopped session ignores the clock
    after = client.get(f"/api/forward/status?state_id={sid}").get_json()
    assert after["status"] == "stopped"
    assert after["progress"]["pct"] == before


def test_status_survives_page_refresh(client):
    """Server-side state persists across requests (refresh-safe), and is stable."""
    started = _start(client)
    sid = started["state_id"]
    a = client.get(f"/api/forward/status?state_id={sid}").get_json()["progress"]["revealed"]
    b = client.get(f"/api/forward/status?state_id={sid}").get_json()["progress"]["revealed"]
    assert a == b > 0


def test_trades_endpoint_marks_open_legs(client):
    started = _start(client)
    sid = started["state_id"]
    _session(client, sid).advance(60)
    rows = client.get(f"/api/forward/trades?state_id={sid}").get_json()
    assert rows
    assert {"id", "symbol", "side", "entry", "exit", "pnl", "status", "result",
            "date", "exit_date"} <= set(rows[0])
    assert any(r["status"] in {"open", "closed"} for r in rows)


def test_equity_endpoint_is_the_compatible_curve(client):
    started = _start(client)
    sid = started["state_id"]
    session = _session(client, sid)
    session.advance(8)
    rows = client.get(f"/api/forward/equity?state_id={sid}").get_json()
    assert len(rows) == session.revealed
    assert {"ts", "equity"} <= set(rows[0])
    assert all(isinstance(r["equity"], (int, float)) for r in rows)


# ---------------------------------------------------------------------------
# Task 4.2 — server-side authentication guard
# ---------------------------------------------------------------------------


def test_start_without_auth_returns_403(client_unauthenticated):
    """No broker session → /start must return 403."""
    client, _ = client_unauthenticated
    resp = client.post("/api/forward/start", json=_CFG)
    assert resp.status_code == 403
    body = resp.get_json()
    assert body["success"] is False
    assert body["error"] == "broker_not_authenticated"
    assert "Valid broker session required" in body["message"]


def test_start_with_expired_session_returns_403(client_unauthenticated):
    """Expired session → /start must return 403."""
    client, stub = client_unauthenticated
    stub.set_status(STATUS_EXPIRED)
    resp = client.post("/api/forward/start", json=_CFG)
    assert resp.status_code == 403
    body = resp.get_json()
    assert body["error"] == "broker_not_authenticated"


def test_start_with_authenticated_session_succeeds(client):
    """Authenticated session → /start should proceed normally."""
    resp = client.post("/api/forward/start", json=_CFG)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "running"


def test_start_with_expiring_soon_session_succeeds(client_unauthenticated):
    """Expiring-soon session is still valid → /start should proceed."""
    client, stub = client_unauthenticated
    stub.set_status(STATUS_EXPIRING_SOON)
    resp = client.post("/api/forward/start", json=_CFG)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "running"


def test_start_guard_runs_before_strategy_validation(client_unauthenticated):
    """Auth check happens first — even invalid strategy returns 403, not 400."""
    client, _ = client_unauthenticated
    bad_cfg = {**_CFG, "strategy": "nonexistent_strategy"}
    resp = client.post("/api/forward/start", json=bad_cfg)
    assert resp.status_code == 403
    body = resp.get_json()
    assert body["error"] == "broker_not_authenticated"


def test_stop_does_not_require_auth(client_unauthenticated):
    """Stop endpoint does NOT require authentication (idempotent)."""
    client, _ = client_unauthenticated
    resp = client.post("/api/forward/stop")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "idle"


def test_status_does_not_require_auth(client_unauthenticated):
    """Status endpoint does NOT require authentication (polling)."""
    client, _ = client_unauthenticated
    resp = client.get("/api/forward/status")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "idle"
