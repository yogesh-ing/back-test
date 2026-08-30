"""Forward test endpoints — bar-by-bar paper-trading replay.

* ``POST /api/forward/start``  {strategy, symbol, timeframe, capital, params,
                                from_date, to_date}
* ``POST /api/forward/stop``
* ``GET  /api/forward/status`` → {status, progress, metrics, equity, drawdown,
                                 trades, signals, config, positions, …}
* ``GET  /api/forward/trades`` → round-trip trade list
* ``GET  /api/forward/equity`` → [{ts, equity}]

A forward session replays a historical backtest **one bar at a time** — the
strategy sees bars gradually, as it would live. The clock runs on the server:
a daemon thread reveals bars at ``bars_per_second`` whether or not anyone polls,
and ``/status`` is a pure read (so two open tabs cannot double-advance one run).
Each snapshot recomputes the metrics on the revealed prefix through
:class:`BacktestAdapter`, i.e. the same payload shape as the Backtest/Compare
pages, so their chart/table components are reusable.

Sessions are keyed by ``state_id`` (``GET /api/forward/sessions`` lists them);
omitting the id addresses the most recently started one. State lives in-process,
so it survives a page refresh but not a restart (V2 persistence). The broker auth
guard blocks ``/start`` in live mode unless a session is authenticated.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any, Optional

import pandas as pd
from flask import Blueprint, current_app, jsonify, request

from backtest.adapters.backtest_adapter import BacktestAdapter
from backtest.brokers.session_manager import get_session_manager
from backtest.engine.backtester import BacktestConfig, BacktestResult
from backtest.engine.metrics import compute_metrics
from backtest.logging_config import get_logger, timed
from backtest.runner import build_source, run_on_candles

forward_bp = Blueprint("forward_api", __name__)
log = get_logger(__name__)

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

#: Bars revealed per second of wall-clock time by the replay clock. One year of
#: daily bars therefore plays in ~4 minutes at the default, instantly at 2000.
DEFAULT_BARS_PER_SECOND = 1.0
#: How often the clock thread wakes up.
TICK_SECONDS = 0.25
#: Finished/stopped sessions kept readable for late pollers (refresh-safe UI).
MAX_SESSIONS = 20
#: Guard rail for a client-supplied speed — a typo must not spin the CPU.
MAX_BARS_PER_SECOND = 5000.0


class ForwardSession:
    """One forward-test paper-trading replay.

    **The clock is server-side** (gap G4): a daemon thread reveals bars at
    ``bars_per_second`` whether or not anyone polls, and ``/status`` only *reads*
    state. Poll-driven advance meant two open tabs (or a Dashboard refresh)
    advanced one run twice as fast, and a closed browser froze the bot.

    ``bars_per_second=0`` freezes the clock so tests (and a "step through it"
    UI) can drive :meth:`tick` deterministically.

    The strategy still only ever sees the revealed prefix — the same
    no-lookahead rule as the paper-trade loop — and the payload is built with
    :class:`BacktestAdapter` so the Backtest/Compare components are reusable.
    """

    def __init__(
        self,
        *,
        state_id: str,
        result: BacktestResult,
        strategy: str,
        symbol: str,
        timeframe: str,
        capital: float,
        params: dict[str, Any],
        from_date: str,
        to_date: str,
        bars_per_second: float = DEFAULT_BARS_PER_SECOND,
    ) -> None:
        self.state_id = state_id
        self.strategy = strategy
        self.symbol = symbol
        self.timeframe = timeframe
        self.capital = float(capital)
        self.params = dict(params)
        self.from_date = from_date
        self.to_date = to_date

        self.lock = threading.RLock()
        self.status: str = "running"
        self.error: Optional[str] = None
        self.created_at = datetime.now()
        self.stopped_at: Optional[datetime] = None

        # Trimmed, in-range frames: the replay only ever covers what was asked for.
        self.candles = result.candles
        self.equity_s = result.equity
        self.returns_s = result.returns
        self.position_s = result.position.fillna(0)
        self.config = result.config

        # Views over the whole run, prepared once: each snapshot slices these by
        # date instead of recomputing markers per poll.
        full = BacktestAdapter(result)
        signals = full.to_signals()
        self.candles_rows = signals["candles"]
        self.buy_signals = signals["buys"]
        self.sell_signals = signals["sells"]
        self._dates = [c["date"] for c in self.candles_rows]

        self.bars_per_second = max(0.0, min(float(bars_per_second), MAX_BARS_PER_SECOND))

        # Cursor over the revealed prefix. One bar is visible immediately so the
        # first /status after /start already renders something.
        self.total = len(self.candles)
        self._revealed = min(1, self.total)
        self._pending = 0.0

        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None

        log.info("[forward:%s] session created for %s/%s — %d bars @ %s",
                 self.short_id, symbol, strategy, self.total,
                 f"{self.bars_per_second:g} bars/s" if self.bars_per_second else "manual clock")
        if self.bars_per_second > 0 and self.total > 1:
            self._start_clock()

    # -- identity helpers -------------------------------------------------- #

    @property
    def short_id(self) -> str:
        return self.state_id[:8]

    @property
    def revealed(self) -> int:
        with self.lock:
            return self._revealed

    @property
    def running(self) -> bool:
        with self.lock:
            return self.status == "running"

    # -- the clock --------------------------------------------------------- #

    def _start_clock(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name=f"forward-replay-{self.short_id}", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        """Reveal bars on wall-clock time, then retire the thread at completion."""
        while not self._wake.wait(TICK_SECONDS):
            self.tick(TICK_SECONDS)
            if not self.running:
                return

    def tick(self, seconds: float) -> int:
        """Reveal whatever ``seconds`` of wall-clock time is worth. Returns the cursor.

        Fractional bars accumulate in ``_pending`` so a slow clock (1 bar/s) is
        never starved by rounding, and a fast one can reveal many bars per wake-up.
        """
        with self.lock:
            if self.bars_per_second <= 0:
                return self._revealed          # frozen clock: only advance() steps it
            self._pending += max(0.0, float(seconds)) * self.bars_per_second
            whole = int(self._pending)
            if whole <= 0:
                return self._revealed
            self._pending -= whole
        return self.advance(whole)

    def advance(self, bars: int) -> int:
        """Reveal exactly ``bars`` more bars. Returns the new cursor.

        The thread path goes through :meth:`tick`; this is the direct door, which
        is what tests (and any future "step one bar" control) use — with a frozen
        clock (``bars_per_second=0``) it is the only way to move.
        """
        with self.lock:
            if self.status != "running" or self.total == 0 or bars <= 0:
                return self._revealed
            before = self._revealed
            self._revealed = min(self.total, self._revealed + int(bars))
            if self._revealed >= self.total:
                self._revealed = self.total
                self.status = "stopped"
                self.stopped_at = datetime.now()
                log.info("[forward:%s] replay complete — %d/%d bars for %s/%s, auto-stopped",
                         self.short_id, self.total, self.total, self.symbol, self.strategy)
            elif self._revealed != before:
                log.debug("[forward:%s] revealed %d/%d (%.1f%%) — %s/%s", self.short_id,
                          self._revealed, self.total,
                          100.0 * self._revealed / self.total, self.symbol, self.strategy)
            return self._revealed

    def stop(self) -> None:
        with self.lock:
            already = self.status != "running"
            if not already:
                self.status = "stopped"
                self.stopped_at = datetime.now()
        self._wake.set()
        if already:
            log.debug("[forward:%s] stop ignored — status is already %s", self.short_id,
                      self.status)
        else:
            log.info("[forward:%s] stopped at bar %d/%d (%s/%s)", self.short_id,
                     self._revealed, self.total, self.symbol, self.strategy)

    # -- state derivation --------------------------------------------------- #

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
        prefix = BacktestResult(
            equity=equity,
            returns=returns,
            position=position,
            candles=candles,
            config=self.config,
            metrics={},
        )
        # BacktestAdapter reads result.metrics for the cards — an empty dict here
        # used to make every live metric (P&L, return, Sharpe, win rate, trade
        # count) report zero no matter how the replay was doing. Compute the
        # metrics for the revealed prefix, and stamp the run metadata the adapter
        # surfaces on the config block.
        prefix.metrics = compute_metrics(prefix)
        prefix.metrics.update({
            "strategy": self.strategy,
            "symbol": self.symbol,
            "strategy_params": self.params,
            "stop_loss": self.config.stop_loss,
            "take_profit": self.config.take_profit,
        })
        return prefix

    def _signals_upto(self, cutoff: Any) -> dict[str, Any]:
        """Signals the strategy could actually have seen by ``cutoff``.

        The previous version filtered with ``if b in self.signals["buys"]`` —
        always true — and then sliced by *count*, so a replay at 4% progress was
        already advertising entries months ahead (a lookahead leak in the payload,
        gap G4). Filtering on the bar date is the actual rule.
        """
        cutoff_str = _fmt_ts(cutoff) if cutoff is not None else ""
        candles = [c for c in self.candles_rows if c["date"] <= cutoff_str]
        return {
            "candles": candles,
            "buys": [b for b in self.buy_signals if b["date"] <= cutoff_str],
            "sells": [s for s in self.sell_signals if s["date"] <= cutoff_str],
        }

    def _positions(self, n: int, prefix_trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The position still open at the revealed bar, marked to its close.

        Previously ``entry`` and ``current`` were both the last close, so the
        unrealised P&L column was always 0 — a live panel that could never move.
        """
        if n <= 0:
            return []
        last_idx = self.candles.index[n - 1]
        cur_pos = float(self.position_s.loc[last_idx]) if last_idx in self.position_s.index else 0.0
        if cur_pos == 0:
            return []

        # The open leg of the *revealed* prefix — not of the full run.
        open_trade = next((t for t in prefix_trades if t.get("is_open")), None)
        last_close = float(self.candles.loc[last_idx, "close"])
        entry = float(open_trade["entry"]) if open_trade and open_trade["entry"] else last_close
        entry_equity = (self.capital + float(open_trade["pnl"])) if open_trade else self.capital
        direction = 1.0 if cur_pos > 0 else -1.0
        price_change = (last_close / entry - 1.0) * direction if entry else 0.0
        pnl = float(open_trade["pnl"]) if open_trade else 0.0
        entry_date = open_trade["date"] if open_trade else _fmt_ts(last_idx)
        bars_held = max(0, (n - 1) - self._index_of(entry_date)) if open_trade else 0
        return [{
            "symbol": self.symbol,
            "side": "LONG" if cur_pos > 0 else "SHORT",
            # This engine sizes in exposure units (1.0 = fully invested), not lots.
            "qty": abs(cur_pos),
            "exposure_pct": round(abs(cur_pos) * 100.0, 2),
            "entry": _f(entry, 2),
            "current": _f(last_close, 2),
            "price_change_pct": round(price_change * 100.0, 2),
            "unrealized_pnl": _f(pnl, 2),
            "unrealized_pnl_pct": round((pnl / entry_equity * 100.0) if entry_equity else 0.0, 2),
            "entry_date": entry_date,
            "bars_held": bars_held,
        }]

    def _index_of(self, date_str: str) -> int:
        try:
            return [c["date"] for c in self.candles_rows].index(date_str)
        except ValueError:
            return 0

    # -- payload ------------------------------------------------------------ #

    def snapshot(self) -> dict[str, Any]:
        """The current state — pure read, never advances the replay."""
        with self.lock:
            n = self._revealed
            pct = round(100.0 * n / self.total, 2) if self.total else 100.0
            prefix = self._prefix_result(n)
            adapter = BacktestAdapter(prefix)

            metrics = adapter.to_metrics()
            equity = adapter.to_equity()
            drawdown = adapter.to_drawdown()
            trades = adapter.to_trades()
            positions = self._positions(n, trades)
            last_idx = self.candles.index[n - 1] if n else None

            return {
                "state_id": self.state_id,
                "status": self.status,
                "progress": {"revealed": n, "total": self.total, "pct": pct},
                "metrics": {
                    "total_pnl": metrics["total_pnl"],
                    "total_return_pct": metrics["total_return_pct"],
                    "win_rate_pct": metrics["win_rate_pct"],
                    "max_drawdown_pct": metrics["max_drawdown_pct"],
                    "sharpe": metrics["sharpe"],
                    "total_trades": metrics["total_trades"],
                    # win_rate_pct covers closed trades only, so the UI can say
                    # "—" instead of a misleading 0% before anything has closed.
                    "closed_trades": metrics["closed_trades"],
                    "open_trades": metrics["open_trades"],
                    "final_equity": metrics["final_equity"],
                },
                "equity": equity,
                "drawdown": drawdown,
                "trades": trades,
                "signals": self._signals_upto(last_idx),
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
                # Live-feed style fields (forward.js / dashboard.js read these).
                "total_bars": n,
                "bars_in_memory": n,
                "total_trades": metrics["total_trades"],
                "last_bar_ts": _fmt_ts(last_idx) if last_idx is not None else None,
                "market_open": True,
                "unrealized_pnl": round(sum(p["unrealized_pnl"] for p in positions), 2),
                "error": self.error,
            }

    def summary(self) -> dict[str, Any]:
        """Row for the session list — no heavy computation."""
        with self.lock:
            return {
                "state_id": self.state_id,
                "short_id": self.short_id,
                "strategy": self.strategy,
                "symbol": self.symbol,
                "status": self.status,
                "revealed": self._revealed,
                "total": self.total,
                "pct": round(100.0 * self._revealed / self.total, 2) if self.total else 100.0,
                "bars_per_second": self.bars_per_second,
                "created_at": self.created_at.isoformat(timespec="seconds"),
            }

    def equity_series(self) -> list[dict[str, Any]]:
        eq = self.snapshot()["equity"]
        return [{"ts": d, "equity": v} for d, v in zip(eq.get("dates", []), eq.get("values", []))]


# ---------------------------------------------------------------------------
# Process-wide session registry
# ---------------------------------------------------------------------------

_SESSIONS: "OrderedDict[str, ForwardSession]" = OrderedDict()
_ACTIVE_ID: Optional[str] = None
_session_lock = threading.Lock()


def _register(session: ForwardSession) -> None:
    global _ACTIVE_ID
    with _session_lock:
        _SESSIONS[session.state_id] = session
        _ACTIVE_ID = session.state_id
        # Keep the map bounded: drop the oldest finished session.
        while len(_SESSIONS) > MAX_SESSIONS:
            for key, existing in list(_SESSIONS.items()):
                if existing is not session and existing.status != "running":
                    _SESSIONS.pop(key, None)
                    break
            else:
                _SESSIONS.pop(next(iter(_SESSIONS)), None)
    log.info("[forward:%s] started (%d session(s) in memory, active id is now this one)",
             session.short_id, len(_SESSIONS))


def _reset_session() -> None:
    """Stop and forget every session (tests / restart)."""
    global _ACTIVE_ID
    with _session_lock:
        sessions = list(_SESSIONS.values())
        _SESSIONS.clear()
        _ACTIVE_ID = None
    for session in sessions:
        session.stop()


def _get_session(state_id: Optional[str] = None) -> Optional[ForwardSession]:
    """Look a session up by id, or return the most recently started one.

    The fallback keeps ``GET /api/forward/status`` working for callers that do
    not track ids (the Dashboard), while an *unknown explicit* id resolves to
    ``None`` so the endpoint can answer 404 instead of lying about another run.
    """
    with _session_lock:
        if state_id:
            return _SESSIONS.get(state_id)
        if _ACTIVE_ID and _ACTIVE_ID in _SESSIONS:
            return _SESSIONS[_ACTIVE_ID]
        for session in reversed(list(_SESSIONS.values())):
            if session.status == "running":
                return session
        return None


def _list_sessions() -> list[dict[str, Any]]:
    with _session_lock:
        rows = [s.summary() for s in _SESSIONS.values()]
        active = _ACTIVE_ID
    for row in rows:
        row["active"] = row["state_id"] == active
    return list(reversed(rows))


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


#: First date a replay may fall back to when the caller supplies no range.
#: PRD Task 4.3 defines the start body as ``{strategy, symbol, params}`` — the
#: date range is optional, so "everything we have" is the intended fallback.
#: What is *not* allowed is doing that silently (see gap G5).
DEFAULT_FROM_DATE = "2020-01-01"


def _normalise_date(value: Any, field: str) -> str:
    """Validate a ``YYYY-MM-DD`` date, returning it unchanged.

    Raises :class:`ValueError` (→ 400) instead of letting a data source fail
    later with an opaque parse error three layers away.
    """
    text = str(value).strip()
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a YYYY-MM-DD date, got {value!r}") from None
    return parsed.strftime("%Y-%m-%d")


def _resolve_dates(data: dict) -> tuple[str, str, list[str]]:
    """Return ``(from_date, to_date, defaults_applied)`` for a /start body.

    Missing dates are not an error — a forward test is often "start from
    whatever data exists". But every value we fill in is reported back as
    ``defaults_applied``, so a caller can never mistake a defaulted window for
    one they chose, and a bad range is rejected before any work happens.
    """
    raw_from = data.get("from_date") or data.get("from")
    raw_to = data.get("to_date") or data.get("to")
    defaults: list[str] = []

    if raw_from:
        from_date = _normalise_date(raw_from, "from_date")
    else:
        from_date = DEFAULT_FROM_DATE
        defaults.append("from_date")
    if raw_to:
        to_date = _normalise_date(raw_to, "to_date")
    else:
        to_date = datetime.now().strftime("%Y-%m-%d")
        defaults.append("to_date")

    if from_date > to_date:
        raise ValueError(f"from_date ({from_date}) must be <= to_date ({to_date})")
    if defaults:
        log.info("[forward] /start defaulted %s → replay window %s..%s",
                 ", ".join(defaults), from_date, to_date)
    return from_date, to_date, defaults


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
    trimmed.metrics = compute_metrics(trimmed)
    for key in ("strategy", "strategy_params", "symbol", "stop_loss", "take_profit"):
        if key in result.metrics:
            trimmed.metrics[key] = result.metrics[key]
    return trimmed


# ---------------------------------------------------------------------------
# POST /api/forward/start
# ---------------------------------------------------------------------------


def _resolve_speed(data: dict, cfg: Any) -> float:
    """Bars/second for this replay: body override, then app config, then default."""
    raw = data.get("bars_per_second")
    if raw in (None, ""):
        raw = cfg.get("FORWARD_REPLAY_BARS_PER_SECOND", DEFAULT_BARS_PER_SECOND)
    try:
        speed = float(raw)
    except (TypeError, ValueError):
        log.warning("[forward] bars_per_second=%r is not a number — using default %s",
                    raw, DEFAULT_BARS_PER_SECOND)
        return DEFAULT_BARS_PER_SECOND
    if speed < 0:
        log.warning("[forward] bars_per_second=%s is negative — clock starts frozen (manual)",
                    speed)
        return 0.0
    return speed


@forward_bp.post("/api/forward/start")
def start() -> tuple:
    """Start a forward-test replay.

    Server-side auth guard: without an authenticated broker session live mode
    returns 403 (checked before any other validation).
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
    try:
        from_date, to_date, date_defaults = _resolve_dates(data)
    except ValueError as exc:
        log.warning("[forward] /start rejected: %s", exc)
        return jsonify({"error": str(exc)}), 400

    try:
        capital = float(data.get("capital", 100_000))
    except (TypeError, ValueError):
        log.warning("[forward] /start rejected: capital=%r", data.get("capital"))
        return jsonify({"error": "capital must be a number"}), 400
    if capital <= 0:
        log.warning("[forward] /start rejected: capital=%s must be positive", capital)
        return jsonify({"error": "capital must be positive"}), 400

    params = data.get("params") or {}
    speed = _resolve_speed(data, current_app.config)
    log.info(
        "[forward] /start strategy=%s symbol=%s mode=%s timeframe=%s range=%s..%s "
        "capital=%s speed=%s params=%s", strategy, symbol, mode, timeframe, from_date,
        to_date, capital, speed, params,
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

    session = ForwardSession(
        state_id=uuid.uuid4().hex, strategy=strategy, symbol=symbol, timeframe=timeframe,
        capital=capital, params=params, result=result, from_date=from_date, to_date=to_date,
        bars_per_second=speed,
    )
    _register(session)
    snap = session.snapshot()
    total = snap["progress"]["total"]
    if speed > 0:
        log.info("[forward:%s] replay running: %d bars @ %g/s ≈ %.0fs — poll "
                 "GET /api/forward/status?state_id=%s", session.short_id, total, speed,
                 total / speed, session.state_id)
    else:
        log.info("[forward:%s] replay paused (%d bars, clock frozen) — step it with "
                 "session.advance(bars) or start with bars_per_second > 0",
                 session.short_id, total)

    return jsonify({
        "status": "running",
        "total": total,
        "revealed": snap["progress"]["revealed"],
        "state_id": session.state_id,
        "symbol": symbol,
        "strategy": strategy,
        "bars_per_second": session.bars_per_second,
        # What the replay actually runs on, including anything we filled in for
        # the caller (see _resolve_dates / gap G5).
        "config": snap["config"],
        "defaults_applied": date_defaults,
    }), 200


# ---------------------------------------------------------------------------
# POST /api/forward/stop
# ---------------------------------------------------------------------------


@forward_bp.post("/api/forward/stop")
def stop() -> tuple:
    """Stop one replay (``state_id`` in the body or query), or the active one."""
    data = request.get_json(silent=True) or {}
    state_id = data.get("state_id") or request.args.get("state_id")
    session = _get_session(state_id)
    if state_id and session is None:
        log.warning("[forward] /stop for unknown session %r", state_id)
        return jsonify({"error": f"unknown session: {state_id}"}), 404
    if session is None:
        log.info("[forward] /stop with no active session — nothing to do")
        return jsonify({"status": "idle"}), 200
    session.stop()
    return jsonify({"status": "stopped", "state_id": session.state_id,
                    "progress": session.snapshot()["progress"]}), 200


# ---------------------------------------------------------------------------
# GET /api/forward/status  ·  /sessions  ·  /trades  ·  /equity
# ---------------------------------------------------------------------------


def _session_or_404():
    """Resolve the requested session; ``(None, (payload, status))`` when unknown."""
    state_id = request.args.get("state_id")
    session = _get_session(state_id)
    if session is None:
        if state_id:
            log.warning("[forward] status/trades/equity for unknown session %r", state_id)
            return None, (jsonify({"error": f"unknown session: {state_id}"}), 404)
        return None, (jsonify({"status": "idle",
                               "progress": {"revealed": 0, "total": 0, "pct": 0.0}}), 200)
    return session, None


@forward_bp.get("/api/forward/status")
def status() -> tuple:
    """Read the current replay state. Never advances the clock."""
    session, early = _session_or_404()
    if early is not None:
        return early
    return jsonify(session.snapshot()), 200


@forward_bp.get("/api/forward/sessions")
def sessions() -> tuple:
    """Every replay in memory, newest first (the active one flagged)."""
    return jsonify({"sessions": _list_sessions()}), 200


@forward_bp.get("/api/forward/trades")
def trades() -> tuple:
    session, early = _session_or_404()
    if early is not None:
        return early
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
            "status": "open" if t.get("is_open") else "closed",
            "result": t.get("result"),
            "date": t.get("date"),
            "exit_date": t.get("exit_date"),
        })
    return jsonify(out), 200


@forward_bp.get("/api/forward/equity")
def equity() -> tuple:
    """``[{ts, equity}]`` — the shape ``/status`` already carries as ``equity``.

    Kept for any caller that only wants the curve; the forward page reads the
    ``/status`` payload instead so one poll costs one snapshot.
    """
    session, early = _session_or_404()
    if early is not None:
        return early
    return jsonify(session.equity_series()), 200
