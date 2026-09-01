"""Synthetic bar feed for the portfolio engine (V1, no broker credentials).

Produces deterministic random-walk OHLC bars for every symbol a
:class:`~backtest.forward.portfolio_manager.PortfolioManager` needs, on a fixed
tick interval. Bars are dispatched to the manager as "closed candle" events —
the same event a live mStock feed would emit — so swapping in the real gateway
is a single callback change.

Determinism: each symbol's walk is seeded from the symbol name, so reruns are
reproducible. The first warmup batch of bars is replayed quickly so strategies
have history immediately (the demo doesn't wait 60 ticks for signals).
"""

from __future__ import annotations

import logging
import random
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

logger = logging.getLogger("backtest.forward.feed")


def _seed_for(symbol: str) -> int:
    total = 0
    for ch in symbol:
        total = (total * 31 + ord(ch)) & 0xFFFFFFFF
    return total or 1


class SyntheticFeed:
    """Background thread generating synthetic closed-candle events."""

    def __init__(
        self,
        on_bar: Callable[[str, Dict], None],
        tick_seconds: float = 1.0,
        warmup_bars: int = 30,
        crash_symbols: Optional[List[str]] = None,
        on_tick_end: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.on_bar = on_bar
        # Invoked once after every subscribed symbol has received its bar for
        # the current tick — lets pool runners scan the basket once per tick
        # instead of re-scanning after every symbol event (O(n^2) → O(n)).
        self.on_tick_end = on_tick_end
        self.tick_seconds = float(tick_seconds)
        self.warmup_bars = int(warmup_bars)
        self.crash_symbols = {s.upper() for s in (crash_symbols or [])}

        self._symbols: List[str] = []
        self._state: Dict[str, Dict] = {}
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._bar_index = 0
        self._warmed_up = False

    # -- subscription -----------------------------------------------------

    def add_symbols(self, symbols: List[str]) -> None:
        with self._lock:
            for symbol in symbols:
                symbol = symbol.upper()
                if symbol in self._state:
                    continue
                rng = random.Random(_seed_for(symbol))
                base = rng.uniform(80, 450)
                self._state[symbol] = {"rng": rng, "close": base}
                self._symbols.append(symbol)
                logger.debug("Feed subscribed %s (base=%.2f)", symbol, base)

    def remove_symbols(self, symbols: List[str]) -> None:
        with self._lock:
            for symbol in symbols:
                symbol = symbol.upper()
                self._state.pop(symbol, None)
                if symbol in self._symbols:
                    self._symbols.remove(symbol)

    # -- bar generation ---------------------------------------------------

    def _make_bar(self, symbol: str, ts: datetime, crash: bool = False) -> Dict:
        st = self._state[symbol]
        rng = st["rng"]
        base = st["close"]

        if crash and symbol in self.crash_symbols:
            change_pct = -abs(rng.uniform(0.04, 0.09))  # sharp crash
        else:
            change_pct = rng.uniform(-0.006, 0.006)

        close = max(1.0, base * (1 + change_pct))
        open_price = base
        spread = abs(close - open_price) * rng.uniform(0.2, 1.5)
        high = max(open_price, close) + spread * 0.5
        low = min(open_price, close) - spread * 0.5
        volume = rng.randint(500, 50_000)
        st["close"] = close

        return {
            "ts": ts.strftime("%Y-%m-%d %H:%M:%S"),
            "open": round(open_price, 4),
            "high": round(high, 4),
            "low": round(max(0.01, low), 4),
            "close": round(close, 4),
            "volume": volume,
        }

    def emit_one(self, ts: Optional[datetime] = None, crash: bool = False) -> int:
        """Emit one bar per subscribed symbol. Returns bar count."""
        ts = ts or datetime.now(timezone.utc)
        count = 0
        with self._lock:
            symbols = list(self._symbols)
        tick_ts = None
        for i, symbol in enumerate(symbols):
            bar = self._make_bar(symbol, ts + timedelta(milliseconds=i), crash=crash)
            if tick_ts is None:
                tick_ts = bar["ts"]
            try:
                self.on_bar(symbol, bar)
            except Exception:  # noqa: BLE001 — feed never dies on consumer error
                logger.exception("Feed consumer failed for %s", symbol)
            count += 1
        if self.on_tick_end is not None and tick_ts is not None:
            try:
                self.on_tick_end(tick_ts)
            except Exception:  # noqa: BLE001
                logger.exception("Feed tick-end callback failed")
        self._bar_index += 1
        return count

    def warmup(self) -> int:
        """Replay ``warmup_bars`` ticks quickly to give strategies history.

        Runs at most once per feed lifetime — spawning additional runners must
        not replay the warmup batch again (that would double-count bars).
        """
        if self._warmed_up:
            return 0
        self._warmed_up = True
        total = 0
        start = datetime.now(timezone.utc) - timedelta(minutes=self.warmup_bars)
        for i in range(self.warmup_bars):
            total += self.emit_one(ts=start + timedelta(minutes=i))
        return total

    # -- thread lifecycle -------------------------------------------------

    def start(self, warmup: bool = True) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if warmup:
            n = self.warmup()
            logger.info("Feed warmup: %d bars across %d symbols", n, len(self._symbols))
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="portfolio-feed")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._thread = None

    def _loop(self) -> None:
        logger.info(
            "Synthetic feed started: %d symbols @ %.1fs ticks",
            len(self._symbols),
            self.tick_seconds,
        )
        while not self._stop.is_set():
            self.emit_one()
            self._stop.wait(self.tick_seconds)
        logger.info("Synthetic feed stopped")
