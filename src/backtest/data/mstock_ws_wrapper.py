"""Wrapper around mStock MTicker for real-time market depth & coalesced tick bus."""
from __future__ import annotations

from datetime import datetime, timezone
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

TickBatchCallback = Callable[[Dict[str, dict]], None]


class MStockWebSocketWrapper:
    """Wraps MTicker to provide synchronous quote access and a coalesced push bus.

    Manages WebSocket connection lifecycle, stamps every incoming packet with a
    monotonic arrival clock (``recv_mono``) + socket-wide liveness watermark
    (``_last_any_tick_mono``), and fans out coalesced (`150 ms` default) tick
    batches on a dedicated daemon thread outside Twisted's reactor thread.
    """

    def __init__(
        self,
        mticker_instance: Any,
        *,
        coalesce_ms: int = 150,
    ) -> None:
        self._ticker = mticker_instance
        self._quotes: Dict[str, dict] = {}
        self._connected = threading.Event()
        self._lock = threading.Lock()
        self._last_mode_logged: Dict[str, str] = {}
        self._last_any_tick_mono: float = 0.0

        # Coalesced MStockTickBus state (lazy thread start on first listener)
        self._coalesce_s: float = max(0.0, float(coalesce_ms) / 1000.0)
        self._listeners: List[TickBatchCallback] = []
        self._dirty_tokens: Set[str] = set()
        self._dirty_event = threading.Event()
        self._stop_event = threading.Event()
        self._dispatcher_thread: Optional[threading.Thread] = None

        # Override MTicker callbacks to capture ticks
        original_on_ticks = getattr(mticker_instance, "on_ticks", None)

        def on_ticks(ws: Any, ticks: Any) -> None:
            """Capture ticks into cache synchronously, then signal coalesced bus."""
            logger.debug("Received %d ticks from MTicker", len(ticks) if ticks else 0)
            batch_mono = time.monotonic()
            batch_utc = datetime.now(timezone.utc).isoformat()
            notify = False
            with self._lock:
                for tick in ticks or []:
                    token = str(tick.get("instrument_token", ""))
                    if token:
                        normalized = self._normalize_tick(
                            tick,
                            recv_mono=batch_mono,
                            recv_utc=batch_utc,
                        )
                        self._quotes[token] = normalized
                        self._last_any_tick_mono = batch_mono
                        if self._listeners:
                            self._dirty_tokens.add(token)
                            notify = True
                        # Log mode/depth transitions per token (2026-10-23:
                        # app stayed quote-mode for option tokens while a raw
                        # harness got full depth on the same server).
                        mode = str(normalized.get("mode"))
                        has_depth = bool(normalized.get("depth"))
                        sig = f"{mode}/{'depth' if has_depth else 'flat'}"
                        if self._last_mode_logged.get(token) != sig:
                            self._last_mode_logged[token] = sig
                            logger.info(
                                "tick profile token=%s mode=%s depth=%s ltp=%s",
                                token,
                                mode,
                                has_depth,
                                normalized.get("ltp"),
                            )

            if notify:
                self._dirty_event.set()

            if original_on_ticks:
                original_on_ticks(ws, ticks)

        mticker_instance.on_ticks = on_ticks

        # Override on_connect to signal connection ready
        original_on_connect = getattr(mticker_instance, "on_connect", None)

        def on_connect(ws: Any, response: Any) -> None:
            self._connected.set()
            logger.info("WebSocket connection established")
            if original_on_connect:
                original_on_connect(ws, response)

        mticker_instance.on_connect = on_connect

        # Override on_close
        original_on_close = getattr(mticker_instance, "on_close", None)

        def on_close(ws: Any, code: Any, reason: Any) -> None:
            self._connected.clear()
            logger.info("WebSocket closed: %s", reason)
            if original_on_close:
                original_on_close(ws, code, reason)

        mticker_instance.on_close = on_close

    def wait_for_connection(self, timeout: float = 5.0) -> bool:
        """Wait for WebSocket to connect. Returns True if connected."""
        return self._connected.wait(timeout=timeout)

    def is_connected(self) -> bool:
        """Return True while the WebSocket session is actively connected."""
        return self._connected.is_set()

    def last_any_tick_mono(self) -> float:
        """Monotonic timestamp of the most recent packet across any subscribed token."""
        with self._lock:
            return self._last_any_tick_mono

    def get_quote(self, instrument_token: str) -> Optional[dict]:
        """Get latest cached quote for an instrument."""
        with self._lock:
            row = self._quotes.get(str(instrument_token))
            return dict(row) if row is not None else None

    # -- MStockTickBus listener registration & coalesced dispatch -----------

    def add_tick_listener(self, callback: TickBatchCallback) -> None:
        """Register a listener receiving coalesced ``{token: quote}`` batches."""
        if not callable(callback):
            raise TypeError("tick listener must be callable")
        with self._lock:
            if callback not in self._listeners:
                self._listeners.append(callback)
            self._ensure_dispatcher_locked()

    def remove_tick_listener(self, callback: TickBatchCallback) -> None:
        """Unregister a previously registered tick listener."""
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    # Plan §3.1 aliases
    register_listener = add_tick_listener
    unregister_listener = remove_tick_listener

    def _ensure_dispatcher_locked(self) -> None:
        if self._stop_event.is_set():
            return
        if self._dispatcher_thread is not None and self._dispatcher_thread.is_alive():
            return
        self._dispatcher_thread = threading.Thread(
            target=self._dispatch_loop,
            name="mstock-tick-bus",
            daemon=True,
        )
        self._dispatcher_thread.start()

    def _dispatch_loop(self) -> None:
        while not self._stop_event.is_set():
            self._dirty_event.wait()
            if self._stop_event.is_set():
                break
            if self._coalesce_s > 0.0:
                if self._stop_event.wait(timeout=self._coalesce_s):
                    break

            with self._lock:
                self._dirty_event.clear()
                if not self._dirty_tokens or not self._listeners:
                    self._dirty_tokens.clear()
                    continue
                tokens = list(self._dirty_tokens)
                self._dirty_tokens.clear()
                batch: Dict[str, dict] = {
                    tok: dict(self._quotes[tok])
                    for tok in tokens
                    if tok in self._quotes
                }
                listeners = list(self._listeners)

            if not batch:
                continue

            for listener in listeners:
                try:
                    listener(batch)
                except Exception:  # noqa: BLE001 — never crash bus or reactor
                    logger.exception("MStockTickBus listener error")

    def close(self) -> None:
        """Stop the coalesced tick dispatcher and close the underlying socket."""
        self._stop_event.set()
        self._dirty_event.set()
        self._connected.clear()
        thread = self._dispatcher_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        close_fn = getattr(self._ticker, "close", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception:  # noqa: BLE001
                logger.debug("Error closing underlying MTicker", exc_info=True)

    @staticmethod
    def _normalize_tick(
        tick: dict,
        *,
        recv_mono: Optional[float] = None,
        recv_utc: Optional[str] = None,
    ) -> dict:
        """Normalize MTicker tick to standard quote format with monotonic arrival."""
        last_price = float(tick.get("last_price", 0))

        bid = 0.0
        ask = 0.0
        depth = tick.get("depth")

        if depth and isinstance(depth, dict):
            bids = depth.get("bid", [])
            asks = depth.get("ask", [])

            if bids and len(bids) > 0:
                bid = float(bids[0].get("price", 0))
            if asks and len(asks) > 0:
                ask = float(asks[0].get("price", 0))

        mono = time.monotonic() if recv_mono is None else float(recv_mono)
        utc_iso = (
            datetime.now(timezone.utc).isoformat()
            if recv_utc is None
            else str(recv_utc)
        )

        return {
            "ltp": last_price,
            "bid": bid,
            "ask": ask,
            "volume": int(tick.get("volume_traded", 0)),
            "oi": float(tick.get("open_interest", 0)),
            "ohlc": tick.get("ohlc", {}),
            "depth": depth,
            "timestamp": tick.get("exchange_timestamp"),
            "recv_mono": mono,
            "recv_utc": utc_iso,
            "source": "ws",
            "mode": tick.get("mode", "unknown"),
        }


# Explicit alias matching Plan §3.1 terminology
MStockTickBus = MStockWebSocketWrapper
