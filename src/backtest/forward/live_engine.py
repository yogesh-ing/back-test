"""Live Forward Test Engine.

Modes:
  - ``live``: polls mStock API every 60 seconds for 1-minute bars.
  - ``synthetic``: generates random-walk OHLC bars every 60 seconds.

Both modes feed bars to a strategy, execute paper trades, and persist
state to PostgreSQL.

Usage::

    engine = LiveForwardEngine(state_id=1, mode="synthetic")
    engine.start()   # blocks in a background thread
    engine.stop()
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from datetime import datetime, timezone, timedelta, date
from decimal import Decimal
from typing import Any

import pandas as pd
import requests
from sqlalchemy import text

from backtest.data.base import normalize_candles


class PaperPortfolio:
    """Simple paper trading portfolio tracker."""

    def __init__(self, initial_capital: float = 100000.0):
        self.initial_capital = initial_capital
        self.open_positions: list[dict] = []


from backtest.live.auth import get_session_token
from backtest.runner import build_source
from backtest.strategy.registry import get_strategy
from backtest.api.backtest import _interval

logger = logging.getLogger("backtest.forward.live")

# NSE market hours (IST = UTC+5:30)
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 15
MARKET_CLOSE_HOUR = 15
MARKET_CLOSE_MINUTE = 30

# How many bars to keep in memory for strategy calculation
LOOKBACK_BARS = 200

# mStock API config
MSTOCK_BASE_URL = os.getenv("MSTOCK_BASE_URL", "https://api.mstock.trade").rstrip("/")


def _is_market_open(now_utc: datetime | None = None) -> bool:
    """Check if NSE market is currently open."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    ist = now_utc + timedelta(hours=5, minutes=30)
    if ist.weekday() >= 5:
        return False
    market_open = ist.replace(
        hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MINUTE, second=0, microsecond=0
    )
    market_close = ist.replace(
        hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MINUTE, second=0, microsecond=0
    )
    return market_open <= ist <= market_close


def _fetch_latest_bar(
    token: str,
    api_key: str,
    security_token: str,
    segment: str = "NSE",
) -> dict | None:
    """Fetch the latest 1-min bar from mStock historical API for today."""
    today = date.today().strftime("%Y-%m-%d")
    yesterday = (date.today() - timedelta(days=3)).strftime("%Y-%m-%d")

    headers = {
        "X-Mirae-Version": "1",
        "Authorization": f"token {api_key}:{token}",
    }
    url = f"{MSTOCK_BASE_URL}/openapi/typea/instruments/historical/{segment}/{security_token}/minute"
    logger.debug("Fetching bar: %s?from=%s&to=%s", url, yesterday, today)
    params = {"from": yesterday, "to": today}

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        payload = resp.json()

        candles = []
        if isinstance(payload, dict):
            data = payload.get("data", payload)
            if isinstance(data, dict):
                candles = data.get("candles", [])
            elif isinstance(data, list):
                candles = data

        if not candles:
            return None

        last = candles[-1]
        if isinstance(last, list) and len(last) >= 6:
            return {
                "ts": last[0],
                "open": float(last[1]),
                "high": float(last[2]),
                "low": float(last[3]),
                "close": float(last[4]),
                "volume": int(last[5]),
            }
        return None
    except Exception as e:
        logger.error("Failed to fetch latest bar: %s", e)
        return None


def _resolve_security_token(api_key: str, token: str, symbol: str) -> str:
    """Resolve symbol to mStock security token via scriptmaster."""
    headers = {"X-Mirae-Version": "1", "Authorization": f"token {api_key}:{token}"}
    try:
        resp = requests.get(
            f"{MSTOCK_BASE_URL}/openapi/typea/instruments/scriptmaster",
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        import io
        frame = pd.read_csv(io.StringIO(resp.text), low_memory=False)
        if frame.empty:
            raise ValueError("scriptmaster empty")

        lower = frame.rename(columns=lambda c: str(c).strip().lower())
        symbol_key = next(
            (c for c in ["tradingsymbol", "symbol", "name"] if c in lower.columns), None
        )
        token_key = next(
            (c for c in ["instrument_token", "token", "securitytoken"] if c in lower.columns), None
        )
        if not symbol_key or not token_key:
            raise ValueError(f"scriptmaster missing columns: {list(lower.columns)[:10]}")

        matches = lower[lower[symbol_key].astype(str).str.lower() == symbol.lower()]
        if matches.empty:
            raise ValueError(f"symbol {symbol} not in scriptmaster")
        return str(matches.iloc[0][token_key])
    except Exception as e:
        logger.error("Failed to resolve security token for %s: %s", symbol, e)
        raise


def _generate_synthetic_bar(last_bar: dict | None, ts: datetime) -> dict:
    """Generate a synthetic OHLCV bar using random walk.

    If last_bar is None, starts from a random price between 100-500.
    Otherwise walks from the last close with ±0.5% random movement.
    """
    if last_bar is None:
        base = random.uniform(100, 500)
    else:
        base = last_bar["close"]

    # Random walk: -0.5% to +0.5%
    change_pct = random.uniform(-0.005, 0.005)
    open_price = base
    close_price = base * (1 + change_pct)

    # High/low within the bar
    spread = abs(close_price - open_price) * random.uniform(0.2, 1.5)
    high = max(open_price, close_price) + spread * 0.5
    low = min(open_price, close_price) - spread * 0.5

    volume = random.randint(500, 50000)

    return {
        "ts": ts.strftime("%Y-%m-%d %H:%M:%S%z") or ts.isoformat(),
        "open": round(open_price, 2),
        "high": round(high, 2),
        "low": round(low, 2),
        "close": round(close_price, 2),
        "volume": volume,
    }


class LiveForwardEngine:
    """Live forward test engine.

    Runs in a background thread, polls mStock API (or generates synthetic bars)
    every 60 seconds, feeds bars to strategy, executes paper trades, saves to DB.
    """

    def __init__(
        self,
        state_id: int,
        db_url: str | None = None,
        mode: str = "live",
    ):
        self.state_id = state_id
        self.db_url = db_url or os.getenv("FORWARD_TEST_DB_URL", "")
        self.mode = mode  # "live" or "synthetic"

        # Runtime state
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        # Strategy + portfolio
        self._strategy = None
        self._portfolio: PaperPortfolio | None = None
        self._symbol = ""
        self._timeframe = "1min"
        self._capital = 100000.0
        self._params: dict[str, Any] = {}

        # Bars buffer (rolling window)
        self._bars: list[dict] = []
        self._last_bar_ts: str | None = None

        # mStock auth (only needed for live mode)
        self._token = ""
        self._api_key = ""
        self._security_token = ""

        # Status (exposed to API)
        self.status = "idle"
        self.error: str | None = None
        self.total_bars = 0
        self.total_trades = 0

    def _get_engine(self):
        """Create SQLAlchemy engine."""
        from sqlalchemy import create_engine
        return create_engine(self.db_url)

    def _load_state(self):
        """Load state from DB."""
        engine = self._get_engine()
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT * FROM forward_test_state WHERE id = :id"),
                {"id": self.state_id},
            ).fetchone()
            if row is None:
                raise ValueError(f"State {self.state_id} not found")

            params = json.loads(row.params) if isinstance(row.params, str) else (row.params or {})
            self._strategy = get_strategy(row.strategy)(**params)

            self._symbol = row.symbol
            self._timeframe = row.timeframe or "1min"
            self._capital = float(row.capital) if row.capital else 100000.0
            self._params = params

            if row.last_bar_ts:
                self._last_bar_ts = str(row.last_bar_ts)

            # Restore open trades
            trades = conn.execute(
                text("SELECT * FROM forward_test_trades WHERE state_id = :sid AND status = 'open' ORDER BY id"),
                {"sid": self.state_id},
            ).fetchall()

            for t in trades:
                self._portfolio.open_positions.append({
                    "symbol": t.symbol,
                    "side": t.side,
                    "entry_price": float(t.entry_price),
                    "quantity": float(t.quantity),
                    "entry_ts": str(t.entry_ts),
                    "trade_id": t.id,
                })

    def _load_historical_bars(self):
        """Load recent bars from DB to warm up the strategy."""
        engine = self._get_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT ts, open, high, low, close, volume
                    FROM market_data_cache
                    WHERE symbol = :symbol AND timeframe IN ('1min', 'minute')
                    ORDER BY ts DESC
                    LIMIT :limit
                """),
                {"symbol": self._symbol, "limit": LOOKBACK_BARS},
            ).fetchall()

            self._bars = [
                {
                    "ts": str(r.ts),
                    "open": float(r.open),
                    "high": float(r.high),
                    "low": float(r.low),
                    "close": float(r.close),
                    "volume": int(r.volume),
                }
                for r in reversed(rows)
            ]

            if self._bars:
                self._last_bar_ts = self._bars[-1]["ts"]
                logger.info("Loaded %d historical bars, last: %s", len(self._bars), self._last_bar_ts)

    def _save_state(self):
        """Save engine state to DB."""
        engine = self._get_engine()
        with engine.connect() as conn:
            conn.execute(
                text("""
                    UPDATE forward_test_state
                    SET status = :status,
                        last_bar_ts = :last_bar_ts,
                        strategy_state = :strategy_state,
                        updated_at = NOW()
                    WHERE id = :id
                """),
                {
                    "id": self.state_id,
                    "status": self.status,
                    "last_bar_ts": self._last_bar_ts,
                    "strategy_state": json.dumps(self._strategy_state()),
                },
            )
            conn.commit()

    def _strategy_state(self) -> dict:
        """Extract strategy internal state for persistence."""
        state = {}
        if hasattr(self._strategy, "fast"):
            state["fast_period"] = self._strategy.fast
        if hasattr(self._strategy, "slow"):
            state["slow_period"] = self._strategy.slow
        if hasattr(self._strategy, "period"):
            state["rsi_period"] = self._strategy.period
        if hasattr(self._strategy, "lookback"):
            state["lookback"] = self._strategy.lookback
        state["bars_seen"] = len(self._bars)
        return state

    def _save_trade(self, trade: dict):
        """Save a trade to DB."""
        engine = self._get_engine()
        with engine.connect() as conn:
            result = conn.execute(
                text("""
                    INSERT INTO forward_test_trades
                        (state_id, symbol, side, entry_price, quantity, entry_ts, status)
                    VALUES
                        (:state_id, :symbol, :side, :entry_price, :quantity, :entry_ts, 'open')
                    RETURNING id
                """),
                {
                    "state_id": self.state_id,
                    "symbol": trade["symbol"],
                    "side": trade["side"],
                    "entry_price": trade["entry_price"],
                    "quantity": trade["quantity"],
                    "entry_ts": trade["entry_ts"],
                },
            )
            trade_id = result.fetchone()[0]
            conn.commit()
            return trade_id

    def _close_trade(self, trade_id: int, exit_price: float, exit_ts: str):
        """Close a trade in DB and record PnL."""
        engine = self._get_engine()
        with engine.connect() as conn:
            trade = conn.execute(
                text("SELECT * FROM forward_test_trades WHERE id = :id"),
                {"id": trade_id},
            ).fetchone()

            if trade:
                entry = float(trade.entry_price)
                qty = float(trade.quantity)
                side = trade.side

                if side == "LONG":
                    pnl = (exit_price - entry) * qty
                    pnl_pct = ((exit_price / entry) - 1) * 100 if entry else 0
                else:
                    pnl = (entry - exit_price) * qty
                    pnl_pct = ((entry / exit_price) - 1) * 100 if exit_price else 0

                conn.execute(
                    text("""
                        UPDATE forward_test_trades
                        SET exit_price = :exit_price,
                            exit_ts = :exit_ts,
                            pnl = :pnl,
                            pnl_pct = :pnl_pct,
                            status = 'closed'
                        WHERE id = :id
                    """),
                    {
                        "id": trade_id,
                        "exit_price": exit_price,
                        "exit_ts": exit_ts,
                        "pnl": pnl,
                        "pnl_pct": pnl_pct,
                    },
                )
                conn.commit()

                self._capital += pnl
                logger.info("Closed trade %d: PnL ₹%.2f (%.2f%%)", trade_id, pnl, pnl_pct)

    def _save_equity(self):
        """Save current equity snapshot to DB."""
        unrealized = 0.0
        for pos in self._portfolio.open_positions:
            if self._bars:
                last_close = self._bars[-1]["close"]
                if pos["side"] == "LONG":
                    unrealized += (last_close - pos["entry_price"]) * pos["quantity"]
                else:
                    unrealized += (pos["entry_price"] - last_close) * pos["quantity"]

        equity = self._capital + unrealized

        engine = self._get_engine()
        with engine.connect() as conn:
            conn.execute(
                text("""
                    INSERT INTO forward_test_equity (state_id, ts, equity, unrealized_pnl)
                    VALUES (:state_id, NOW(), :equity, :unrealized)
                """),
                {
                    "state_id": self.state_id,
                    "equity": equity,
                    "unrealized": unrealized,
                },
            )
            conn.commit()

    def _process_bar(self, bar: dict):
        """Process a single bar: feed to strategy, execute trades."""
        ts = bar["ts"]

        if self._last_bar_ts and ts <= self._last_bar_ts:
            return

        self._bars.append(bar)
        if len(self._bars) > LOOKBACK_BARS:
            self._bars = self._bars[-LOOKBACK_BARS:]

        self._last_bar_ts = ts
        self.total_bars += 1

        if len(self._bars) < 10:
            logger.info("Accumulating bars: %d/%d", len(self._bars), 10)
            return

        df = pd.DataFrame(self._bars)
        df["ts"] = pd.to_datetime(df["ts"])
        df = df.set_index("ts")[["open", "high", "low", "close", "volume"]]
        df = df.sort_index()

        try:
            signals = self._strategy.generate_signals(df)
            current_signal = int(signals.iloc[-1])
        except Exception as e:
            logger.warning("Strategy signal error: %s", e)
            current_signal = 0

        current_price = bar["close"]

        has_long = any(p["side"] == "LONG" for p in self._portfolio.open_positions)
        has_short = any(p["side"] == "SHORT" for p in self._portfolio.open_positions)

        if current_signal == 1 and not has_long:
            for pos in list(self._portfolio.open_positions):
                if pos["side"] == "SHORT":
                    self._close_trade(pos["trade_id"], current_price, ts)
                    self._portfolio.open_positions.remove(pos)

            qty = max(1, int(self._capital * 0.95 / current_price))
            trade = {
                "symbol": self._symbol,
                "side": "LONG",
                "entry_price": current_price,
                "quantity": qty,
                "entry_ts": ts,
            }
            trade_id = self._save_trade(trade)
            trade["trade_id"] = trade_id
            self._portfolio.open_positions.append(trade)
            self.total_trades += 1
            logger.info("BUY %s @ ₹%.2f x %d", self._symbol, current_price, qty)

        elif current_signal == 0 and has_long:
            for pos in list(self._portfolio.open_positions):
                if pos["side"] == "LONG":
                    self._close_trade(pos["trade_id"], current_price, ts)
                    self._portfolio.open_positions.remove(pos)

        self._save_equity()

    def _run_loop(self):
        """Main polling loop (runs in background thread)."""
        logger.info(
            "Engine started: symbol=%s strategy=%s mode=%s capital=%.0f",
            self._symbol,
            self._strategy.name if self._strategy else "?",
            self.mode,
            self._capital,
        )

        while self._running:
            try:
                if self.mode == "synthetic":
                    self._tick_synthetic()
                else:
                    self._tick_live()

                time.sleep(60)

            except Exception as e:
                logger.error("Engine loop error: %s", e, exc_info=True)
                self.error = str(e)
                time.sleep(30)

        self._close_all_positions()
        self.status = "stopped"
        self._save_state()
        logger.info("Engine stopped")

    def _tick_live(self):
        """One tick of the live engine: fetch bar from mStock API."""
        if not _is_market_open():
            now_ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
            logger.info("Market closed (IST %s). Sleeping 60s...", now_ist.strftime("%H:%M"))
            return

        bar = _fetch_latest_bar(self._token, self._api_key, self._security_token)
        if bar:
            self._process_bar(bar)
            self._save_state()
        else:
            logger.debug("No new bar returned")

    def _tick_synthetic(self):
        """One tick of the synthetic engine: generate a random-walk bar."""
        now = datetime.now(timezone.utc)
        last_bar = self._bars[-1] if self._bars else None

        # Generate bar with timestamp 1 minute after the last bar
        if last_bar:
            try:
                last_ts = pd.to_datetime(last_bar["ts"])
                new_ts = last_ts + timedelta(minutes=1)
            except Exception:
                new_ts = now
        else:
            new_ts = now

        bar = _generate_synthetic_bar(last_bar, new_ts)
        self._process_bar(bar)
        self._save_state()
        logger.debug("Synthetic bar: close=%.2f ts=%s", bar["close"], bar["ts"])

    def _close_all_positions(self):
        """Close all open positions at last known price."""
        if not self._bars:
            return
        last_price = self._bars[-1]["close"]
        last_ts = self._bars[-1]["ts"]

        for pos in list(self._portfolio.open_positions):
            self._close_trade(pos["trade_id"], last_price, last_ts)
            self._portfolio.open_positions.clear()

    def start(self):
        """Start the engine in a background thread."""
        if self._running:
            logger.warning("Engine already running")
            return

        self._load_state()
        self._portfolio = PaperPortfolio(initial_capital=self._capital)

        if self.mode == "live":
            # Live mode: need mStock auth
            self._token = get_session_token()
            self._api_key = os.getenv("MSTOCK_API_KEY", "")
            self._security_token = _resolve_security_token(self._api_key, self._token, self._symbol)
            self._load_historical_bars()
        else:
            # Synthetic mode: load historical bars for warmup, no API auth needed
            self._load_historical_bars()
            logger.info("Synthetic mode — no mStock auth needed")

        self._running = True
        self.status = "running"
        self._save_state()

        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="live-forward")
        self._thread.start()
        logger.info("Engine thread started (mode=%s)", self.mode)

    def stop(self):
        """Stop the engine."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=30)
        self.status = "stopped"
        logger.info("Engine stop requested")

    def get_status(self) -> dict:
        """Get current engine status for API response."""
        unrealized = 0.0
        positions = []

        if self._portfolio and self._bars:
            last_price = self._bars[-1]["close"]
            for pos in self._portfolio.open_positions:
                if pos["side"] == "LONG":
                    pnl = (last_price - pos["entry_price"]) * pos["quantity"]
                    pnl_pct = ((last_price / pos["entry_price"]) - 1) * 100 if pos["entry_price"] else 0
                else:
                    pnl = (pos["entry_price"] - last_price) * pos["quantity"]
                    pnl_pct = ((pos["entry_price"] / last_price) - 1) * 100 if last_price else 0

                unrealized += pnl
                positions.append({
                    "symbol": pos["symbol"],
                    "side": pos["side"],
                    "entry": pos["entry_price"],
                    "current": last_price,
                    "unrealized_pnl_pct": round(pnl_pct, 2),
                    "entry_date": pos["entry_ts"],
                    "quantity": pos["quantity"],
                })

        equity = self._capital + unrealized

        return {
            "status": self.status,
            "symbol": self._symbol,
            "strategy": self._strategy.name if self._strategy else "",
            "timeframe": self._timeframe,
            "mode": self.mode,
            "capital": self._capital,
            "equity": round(equity, 2),
            "unrealized_pnl": round(unrealized, 2),
            "total_bars": self.total_bars,
            "total_trades": self.total_trades,
            "bars_in_memory": len(self._bars),
            "last_bar_ts": self._last_bar_ts,
            "market_open": _is_market_open() if self.mode == "live" else True,
            "positions": positions,
            "error": self.error,
        }


# Singleton instances per state_id
_engines: dict[int, LiveForwardEngine] = {}
_engines_lock = threading.Lock()


def get_engine(state_id: int, db_url: str | None = None, mode: str = "live") -> LiveForwardEngine:
    """Get or create a LiveForwardEngine for a state_id."""
    with _engines_lock:
        if state_id not in _engines:
            _engines[state_id] = LiveForwardEngine(state_id=state_id, db_url=db_url, mode=mode)
        return _engines[state_id]


def start_engine(state_id: int, db_url: str | None = None, mode: str = "live") -> LiveForwardEngine:
    """Start a live forward engine."""
    engine = get_engine(state_id, db_url, mode=mode)
    engine.start()
    return engine


def stop_engine(state_id: int) -> LiveForwardEngine | None:
    """Stop a live forward engine."""
    with _engines_lock:
        engine = _engines.get(state_id)
        if engine:
            engine.stop()
        return engine
