"""Forward test endpoints — bar-by-bar paper-trading replay.

* ``POST /api/forward/start``  {strategy, symbol, timeframe, capital, params,
                                from_date, to_date}
* ``POST /api/forward/stop``
* ``GET  /api/forward/status`` → {status, progress, metrics, equity, drawdown,
                                 trades, signals, config, positions, …}
* ``GET  /api/forward/trades`` → round-trip trade list
* ``GET  /api/forward/equity`` → [{ts, equity}]

A forward session replays a historical backtest **one bar at a time** — the
strategy sees bars gradually, as it would live. Each ``/status`` poll reveals
the next slice of bars and recomputes the metrics on the revealed prefix via
:class:`BacktestAdapter` (same payload shape as the Backtest/Compare pages, so
their chart/table components are reusable), until it runs to completion and
auto-stops.

The server-side state lives in-process (refresh-safe); the broker auth guard
blocks ``/start`` unless a session is authenticated.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Optional

import pandas as pd
from flask import Blueprint, current_app, jsonify, request

from backtest.adapters.backtest_adapter import BacktestAdapter
from backtest.brokers.session_manager import get_session_manager
from backtest.engine.backtester import BacktestConfig, BacktestResult
from backtest.logging_config import get_logger, timed
from backtest.runner import build_source, run_on_candles

forward_bp = Blueprint("forward_api", __name__)
log = get_logger(__name__)

# Bars revealed on each /status poll — mimics the real-time cadence while
# keeping a full year of daily bars replayable within a reasonable poll count.
BARS_PER_POLL = 6
# Extra bars before from_date for indicator warmup. 0 keeps the replay over
# exactly the requested range (matching a standalone backtest/forward run).
WARMUP_BARS = 0

_TIMEFRAME_TO_INTERVAL = {
    "1D": "day", "D": "day", "DAY": "day",
    "1W": "week", "W": "week",
    "1H": "hour", "H": "hour",
    "4H": "4hour",
    "15M": "15minute",
    "5M": "5minute",
}


def _f(value: Any, ndigits: int = 4) -> float:
    try:
        if value is None:
            return 0.0
        return round(float(value), ndigits)
    except (TypeError, ValueError):
        return 0.0


def _fmt_ts(ts: Any) -> str:
    t = pd.Timestamp(ts)
    if t.hour == 0 and t.minute == 0 and t.second == 0:
        return t.strftime("%Y-%m-%d")
    return t.strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# In-memory replay session
# ---------------------------------------------------------------------------


class ForwardSession:
    """A single forward-test paper-trading replay."""

    def __init__(
        self,
        candles: pd.DataFrame,
        strategy: str,
        symbol: str,
        timeframe: str,
        capital: float,
        params: dict[str, Any],
        result: BacktestResult,
        from_date: str,
        to_date: str,
    ) -> None:
        self.strategy = strategy
        self.symbol = symbol
        self.timeframe = timeframe
        self.capital = float(capital)
        self.params = dict(params)
        self.from_date = from_date
        self.to_date = to_date
        self.lock = threading.Lock()

        self.status: str = "running"
        self.candles = result.candles           # trimmed, in-range frames
        self.equity_s = result.equity
        self.returns_s = result.returns
        self.position_s = result.position.fillna(0)
        self.config = result.config

        self.adapter = BacktestAdapter(result)
        self.signals = self.adapter.to_signals()
        self.all_trades = self.adapter.to_trades()

        # Replay cursor over the trimmed (in-range) frames.
        self.total = len(self.candles)
        self.revealed = min(BARS_PER_POLL, self.total)

    # ------------------------------------------------------------------ #

    def _prefix_result(self, n: int) -> BacktestResult:
        candles = self.candles.iloc[:n]
        position = self.position_s.reindex(candles.index).fillna(0)
        equity = self.equity_s.reindex(candles.index).ffill()
        if len(equity) and pd.notna(equity.iloc[0]):
            # Re-base the prefix to start at initial capital for a smooth ramp.
            base = equity.iloc[0]
            if base:
                equity = equity / base * self.capital
        returns = self.returns_s.reindex(candles.index).fillna(0.0)
        return BacktestResult(
            equity=equity,
            returns=returns,
            position=position,
            candles=candles,
            config=self.config,
            metrics={},
        )

    def advance(self) -> None:
        """Reveal the next slice of bars and auto-stop at completion."""
        with self.lock:
            if self.status != "running":
                return
            self.revealed = min(self.total, self.revealed + BARS_PER_POLL)
            if self.revealed >= self.total:
                self.revealed = self.total
                self.status = "stopped"
                log.info("[forward] replay complete: %s/%s %s bars — auto-stopped",
                         self.symbol, self.strategy, self.total)
            else:
                log.debug("[forward] replay %s/%s → revealed %d/%d (%.1f%%)",
                          self.symbol, self.strategy, self.revealed, self.total,
                          100.0 * self.revealed / self.total if self.total else 100.0)

    def stop(self) -> None:
        with self.lock:
            if self.status != "running":
                log.debug("[forward] stop ignored: status is already %s", self.status)
            self.status = "stopped"
            log.info("[forward] stopped at bar %d/%d (%s/%s)", self.revealed, self.total,
                     self.symbol, self.strategy)

    # ------------------------------------------------------------------ #

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            n = self.revealed
            pct = round(100.0 * n / self.total, 2) if self.total else 100.0
            prefix = self._prefix_result(n)
            adapter = BacktestAdapter(prefix)

            metrics = adapter.to_metrics()
            equity = adapter.to_equity()
            drawdown = adapter.to_drawdown()
            trades = adapter.to_trades()

            # Live (open) positions from the most recent bar.
            positions: list[dict[str, Any]] = []
            if n > 0:
                last_idx = self.candles.index[n - 1]
                cur_pos = float(self.position_s.loc[last_idx]) if last_idx in self.position_s.index else 0.0
                if cur_pos != 0:
                    last_close = float(self.candles.loc[last_idx, "close"])
                    positions.append({
                        "symbol": self.symbol,
                        "side": "LONG" if cur_pos > 0 else "SHORT",
                        "qty": abs(cur_pos),
                        "entry": last_close,
                        "current": last_close,
                        "unrealized_pnl_pct": 0.0,
                        "entry_date": _fmt_ts(last_idx),
                    })

            return {
                "status": self.status,
                "progress": {
                    "revealed": n,
                    "total": self.total,
                    "pct": pct,
                },
                "metrics": {
                    "total_pnl": metrics["total_pnl"],
                    "total_return_pct": metrics["total_return_pct"],
                    "win_rate_pct": metrics["win_rate_pct"],
                    "max_drawdown_pct": metrics["max_drawdown_pct"],
                    "sharpe": metrics["sharpe"],
                    "total_trades": metrics["total_trades"],
                    "final_equity": metrics["final_equity"],
                },
                "equity": equity,
                "drawdown": drawdown,
                "trades": trades,
                "signals": self._signals_upto(n),
                "positions": positions,
                "config": {
                    "strategy": self.strategy,
                    "symbol": self.symbol,
                    "timeframe": self.timeframe,
                    "capital": self.capital,
                    "params": self.params,
                    "from_date": self.from_date,
                    "to_date": self.to_date,
                },
                # Live-feed style fields (forward.js/dashboard use these).
                "total_bars": n,
                "bars_in_memory": n,
                "total_trades": metrics["total_trades"],
                "last_bar_ts": _fmt_ts(self.candles.index[n - 1]) if n else None,
                "market_open": True,
                "unrealized_pnl": 0.0,
                "error": None,
            }

    def _signals_upto(self, n: int) -> dict[str, Any]:
        if not self.signals.get("candles"):
            return {"candles": [], "buys": [], "sells": []}
        return {
            "candles": self.signals["candles"][:n],
            "buys": [b for b in self.signals["buys"] if b in self.signals["buys"]][: n],
            "sells": [s for s in self.signals["sells"] if s in self.signals["sells"]][: n],
        }

    def equity_series(self) -> list[dict[str, Any]]:
        snap = self.snapshot()
        eq = snap["equity"]
        dates = eq.get("dates", [])
        values = eq.get("values", [])
        return [{"ts": d, "equity": v} for d, v in zip(dates, values)]


# ---------------------------------------------------------------------------
# Process-wide session registry
# ---------------------------------------------------------------------------

_SESSION: Optional[ForwardSession] = None
_session_lock = threading.Lock()


def _reset_session() -> None:
    """Clear the in-memory forward session (tests / restart)."""
    global _SESSION
    with _session_lock:
        _SESSION = None


def _get_session() -> Optional[ForwardSession]:
    return _SESSION


def _source() -> Any:
    name = current_app.config.get("BACKTEST_SOURCE", "synthetic")
    return build_source(name)


def _interval(timeframe: Optional[str]) -> str:
    if not timeframe:
        return "day"
    key = str(timeframe).upper()
    if key not in _TIMEFRAME_TO_INTERVAL:
        log.warning(
            "[forward] unsupported timeframe %r — falling back to 'day' (supported: %s)",
            timeframe, ", ".join(sorted(_TIMEFRAME_TO_INTERVAL)),
        )
    return _TIMEFRAME_TO_INTERVAL.get(key, "day")


def _load_candles(symbol: str, from_date: str, to_date: str, timeframe: str):
    try:
        from_dt = datetime.strptime(from_date, "%Y-%m-%d")
        warmup_start = (from_dt - timedelta(days=WARMUP_BARS * 2)).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        log.warning("[forward] unparseable from_date %r — no warmup applied", from_date)
        warmup_start = from_date
    interval = _interval(timeframe)
    candles = _source().get_candles(symbol, warmup_start, to_date, interval)
    log.debug("[forward] fetched %d bars for %s @ %s (%s..%s)", len(candles), symbol,
              interval, warmup_start, to_date)
    if len(candles) < 2:
        log.warning("[forward] only %d bar(s) available for %s in %s..%s — the replay will "
                    "complete instantly", len(candles), symbol, from_date, to_date)
    return candles


def _trim_to_range(result: BacktestResult, from_date: str, to_date: str) -> BacktestResult:
    """Trim a backtest result to the requested date range (strip warmup)."""
    idx_dates = result.candles.index.strftime("%Y-%m-%d")
    mask = (idx_dates >= from_date) & (idx_dates <= to_date)
    candles = result.candles.loc[mask]
    if candles.empty:
        return result
    returns = result.returns.loc[mask].copy()
    position = result.position.loc[mask].copy()
    # Warmup bars only seed indicators — force the first visible bar flat so
    # a warmup-spanning position doesn't manufacture a phantom trade.
    if len(position) > 0:
        position.iloc[0] = 0
        returns.iloc[0] = 0.0
    equity = result.equity.loc[mask].copy()
    if len(equity) > 0:
        initial = result.config.initial_capital
        cum_returns = (1 + returns).cumprod()
        equity = pd.Series(initial * cum_returns.values, index=equity.index)
    trimmed = BacktestResult(
        equity=equity, returns=returns, position=position,
        candles=candles, config=result.config, metrics={},
    )
    # Recompute metrics on the trimmed frames so counts match the visible range.
    from backtest.engine.metrics import compute_metrics

    trimmed.metrics = compute_metrics(trimmed)
    for key in ("strategy", "strategy_params", "symbol", "stop_loss", "take_profit"):
        if key in result.metrics:
            trimmed.metrics[key] = result.metrics[key]
    return trimmed


# ---------------------------------------------------------------------------
# POST /api/forward/start
# ---------------------------------------------------------------------------


@forward_bp.post("/api/forward/start")
def start() -> tuple:
    """Start a forward-test replay.

    Server-side auth guard: without an authenticated broker session the
    endpoint returns 403 (checked before any other validation).
    """
    data = request.get_json(silent=True) or {}

    strategy = str(data.get("strategy", "")).strip()
    if not strategy:
        log.warning("[forward] /start rejected: no strategy (body keys=%s)", sorted(data))
        return jsonify({"error": "strategy is required"}), 400

    mode = data.get("mode", "live")
    symbol = (data.get("symbol") or "").strip().upper()
    if not symbol:
        log.warning("[forward] /start rejected: no symbol (body keys=%s)", sorted(data))
        return jsonify({"error": "symbol is required"}), 400

    # Auth guard — only required for live mode (synthetic uses DB/API data)
    if mode == "live" and not get_session_manager().is_authenticated():
        log.warning("[forward] /start refused for %s/%s: broker session not authenticated "
                    "(client=%s) — open the broker auth modal or use mode=synthetic",
                    strategy, symbol, request.remote_addr)
        return jsonify({
            "success": False,
            "error": "broker_not_authenticated",
            "message": "Valid broker session required to start live forward test",
        }), 403
    timeframe = data.get("timeframe", "1D")
    from_date = data.get("from_date") or data.get("from")
    to_date = data.get("to_date") or data.get("to")
    # Forward testing can run without date range — defaults to all available data
    if not from_date:
        from_date = "2020-01-01"
        log.info("[forward] /start without from_date — defaulting to %s "
                 "(a promoted backtest should carry its own range)", from_date)
    if not to_date:
        to_date = datetime.now().strftime("%Y-%m-%d")
        log.info("[forward] /start without to_date — defaulting to today (%s)", to_date)

    try:
        capital = float(data.get("capital", 100_000))
    except (TypeError, ValueError):
        log.warning("[forward] /start rejected: capital=%r", data.get("capital"))
        return jsonify({"error": "capital must be a number"}), 400
    if capital <= 0:
        log.warning("[forward] /start rejected: capital=%s must be positive", capital)
        return jsonify({"error": "capital must be positive"}), 400

    params = data.get("params") or {}
    log.info(
        "[forward] /start strategy=%s symbol=%s mode=%s timeframe=%s range=%s..%s "
        "capital=%s params=%s", strategy, symbol, mode, timeframe, from_date, to_date,
        capital, params,
    )

    try:
        with timed(log, "[forward] load candles", logging.DEBUG):
            candles_full = _load_candles(symbol, from_date, to_date, timeframe)
    except Exception as exc:  # noqa: BLE001
        log.warning("[forward] /start data error: %s: %s", exc.__class__.__name__, exc)
        return jsonify({"error": f"data error: {exc}"}), 400

    try:
        result = run_on_candles(candles_full, strategy, params, symbol,
                                BacktestConfig(initial_capital=capital))
    except ValueError as exc:
        log.warning("[forward] /start rejected by strategy/engine: %s", exc)
        return jsonify({"error": str(exc)}), 400
    except KeyError as exc:
        log.warning("[forward] /start unknown strategy: %s", exc)
        return jsonify({"error": f"unknown strategy: {exc}"}), 400
    except Exception as exc:  # noqa: BLE001
        log.exception("[forward] /start failed for %s/%s", strategy, symbol)
        return jsonify({"error": f"forward test failed: {exc}"}), 500

    result = _trim_to_range(result, from_date, to_date)

    global _SESSION
    with _session_lock:
        if _SESSION is not None and _SESSION.status == "running":
            log.warning("[forward] replacing the running replay %s/%s (only one session at a "
                        "time is supported — gap G4)", _SESSION.strategy, _SESSION.symbol)
        _SESSION = ForwardSession(
            candles=result.candles, strategy=strategy, symbol=symbol,
            timeframe=timeframe, capital=capital, params=params,
            result=result, from_date=from_date, to_date=to_date,
        )
        snap = _SESSION.snapshot()

    log.info("[forward] replay running: %d bars, revealing %d per poll (≈%d polls to finish)",
             snap["progress"]["total"], BARS_PER_POLL,
             max(1, -(-snap["progress"]["total"] // BARS_PER_POLL)))
    return jsonify({
        "status": "running",
        "total": snap["progress"]["total"],
        "revealed": snap["progress"]["revealed"],
        "state_id": None,
        "symbol": symbol,
        "strategy": strategy,
    }), 200


# ---------------------------------------------------------------------------
# POST /api/forward/stop
# ---------------------------------------------------------------------------


@forward_bp.post("/api/forward/stop")
def stop() -> tuple:
    session = _get_session()
    if session is None:
        log.info("[forward] /stop with no active session — nothing to do")
        return jsonify({"status": "idle"}), 200
    session.stop()
    return jsonify({"status": "stopped"}), 200


# ---------------------------------------------------------------------------
# GET /api/forward/status
# ---------------------------------------------------------------------------


@forward_bp.get("/api/forward/status")
def status() -> tuple:
    session = _get_session()
    if session is None:
        return jsonify({"status": "idle", "progress": {"revealed": 0, "total": 0, "pct": 0.0}}), 200
    # Each poll advances the replay one slice (bar-by-bar reveal).
    session.advance()
    return jsonify(session.snapshot()), 200


# ---------------------------------------------------------------------------
# GET /api/forward/trades
# ---------------------------------------------------------------------------


@forward_bp.get("/api/forward/trades")
def trades() -> tuple:
    session = _get_session()
    if session is None:
        return jsonify([]), 200
    snap = session.snapshot()
    out = []
    for t in snap["trades"]:
        out.append({
            "id": t.get("id"),
            "symbol": session.symbol,
            "side": t.get("side"),
            "entry": t.get("entry"),
            "exit": t.get("exit"),
            "pnl": t.get("pnl"),
            "status": "closed",
            "result": t.get("result"),
            "date": t.get("date"),
            "exit_date": t.get("exit_date"),
        })
    return jsonify(out), 200


# ---------------------------------------------------------------------------
# GET /api/forward/equity
# ---------------------------------------------------------------------------


@forward_bp.get("/api/forward/equity")
def equity() -> tuple:
    session = _get_session()
    if session is None:
        return jsonify([]), 200
    return jsonify(session.equity_series()), 200
