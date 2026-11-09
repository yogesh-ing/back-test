"""Wrapper around mStock MTicker for real-time market depth & coalesced tick bus."""
from __future__ import annotations

from datetime import datetime, timezone
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set

from backtest.data.feed_depth_recorder import get_recorder, record_depth_packet

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
        #: Frames that arrived with a usable header but no usable price ladder,
        #: per token. The 2026-11-03 fabricated-straddle incident turned exactly
        #: these into booked P&L, so they are counted and logged, never merged
        #: into a live ladder silently (``blank_ladder_frame``).
        self._blank_ladder_counts: Dict[str, int] = {}

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
            capturing = get_recorder() is not None
            captured: list[tuple[str, dict, Any]] = []
            with self._lock:
                for tick in ticks or []:
                    token = str(tick.get("instrument_token", ""))
                    if not token:
                        continue
                    normalized = self._normalize_tick(
                        tick,
                        recv_mono=batch_mono,
                        recv_utc=batch_utc,
                    )
                    row = self._retain_ladder(token, normalized)
                    self._quotes[token] = row
                    self._last_any_tick_mono = batch_mono
                    if self._listeners:
                        self._dirty_tokens.add(token)
                        notify = True
                    # Log mode/ladder transitions per token. 2026-10-23: the app
                    # stayed quote-mode for option tokens while a raw harness got
                    # full depth on the same server. 2026-11-03: ``bool(depth)``
                    # was True for ``{"bid": [], "ask": []}``, so this line lied
                    # for four weeks — the signature is the sanitized status now.
                    mode = str(row.get("mode"))
                    ladder_status = str(row.get("ladder_status"))
                    sig = f"{mode}/{ladder_status}"
                    if self._last_mode_logged.get(token) != sig:
                        self._last_mode_logged[token] = sig
                        logger.info(
                            "tick profile token=%s mode=%s ladder=%s ltp=%s "
                            "blank_frames=%s",
                            token,
                            mode,
                            ladder_status,
                            row.get("ltp"),
                            self._blank_ladder_counts.get(token, 0),
                        )
                    if capturing:
                        captured.append((token, row, tick.get("depth")))

            for token, row, raw_depth in captured:
                record_depth_packet("mstock", row, token=token, raw_depth=raw_depth)

            if notify:
                self._dirty_event.set()

            if original_on_ticks:
                original_on_ticks(ws, ticks)

        mticker_instance.on_ticks = on_ticks

        # Override on_connect to signal connection ready
        original_on_connect = getattr(mticker_instance, "on_connect", None)

        def on_connect(ws: Any, response: Any) -> None:
            self._purge_all()
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

    def forget_token(self, instrument_token: str) -> bool:
        """Drop every cached trace of a token (unsubscribe / re-login).

        A token evicted from the subscription registry and later re-subscribed
        must not be able to re-serve its pre-eviction row — that was the second
        way garbage reached the execution seam on 2026-11-03. Purging here is
        what makes the broker's cold-start gate meaningful.
        """
        token = str(instrument_token)
        with self._lock:
            existed = self._quotes.pop(token, None) is not None
            self._last_mode_logged.pop(token, None)
            self._blank_ladder_counts.pop(token, None)
            self._dirty_tokens.discard(token)
        return existed

    def blank_ladder_count(self, instrument_token: str) -> int:
        """How many frames for this token arrived without a usable ladder."""
        with self._lock:
            return int(self._blank_ladder_counts.get(str(instrument_token), 0))

    def _purge_all(self) -> None:
        """Start a cache generation: no row may outlive the socket that wrote it.

        MTicker re-subscribes after every (re)connect, so a row cached before a
        drop would otherwise become servable again the instant the liveness
        watermark moved — the stale-registry failure mode of 2026-10-26, seen
        from the cache side.
        """
        with self._lock:
            self._quotes.clear()
            self._last_mode_logged.clear()
            self._blank_ladder_counts.clear()
            self._dirty_tokens.clear()
            self._last_any_tick_mono = 0.0

    def _retain_ladder(self, token: str, incoming: dict) -> dict:
        """Last-valid-write-wins for the price ladder (§5.1 of the 2026-11-03 doc).

        mStock publishes packets with a correct header, correct ``ltp`` and a
        *blank* five-level book — proven from the socket log. Overwriting the
        cached row with such a frame destroyed the last honest ladder and left
        the execution seam reading a number nobody was willing to trade at. A
        frame without a usable ladder therefore keeps the previous row's ladder
        *and its stamp*, and takes only the fields that frame genuinely carries
        (LTP, volume, OI, OHLC) under their own ``ltp_mono``. A retained row is
        labelled ``kept:<reason>`` so nothing downstream can mistake it for a
        freshly quoted book.

        Caller holds ``self._lock``.
        """
        incoming_status = str(incoming.get("ladder_status") or "none")
        if incoming_status == "ok":
            return incoming

        self._blank_ladder_counts[token] = self._blank_ladder_counts.get(token, 0) + 1
        streak = self._blank_ladder_counts[token]
        previous = self._quotes.get(token)
        if previous is None or not str(previous.get("ladder_status", "")).startswith("ok"):
            # Nothing honest to retain: serve the frame as it came in.
            return incoming

        merged = dict(previous)
        merged["ltp"] = incoming.get("ltp")
        merged["ltp_mono"] = incoming.get("recv_mono")
        merged["recv_mono"] = incoming.get("recv_mono")
        merged["recv_utc"] = incoming.get("recv_utc")
        merged["volume"] = incoming.get("volume")
        merged["oi"] = incoming.get("oi")
        merged["ohlc"] = incoming.get("ohlc")
        merged["timestamp"] = incoming.get("timestamp")
        merged["mode"] = incoming.get("mode")
        merged["ladder_status"] = f"kept:{incoming_status}"
        if streak == 1 or streak % 50 == 0:
            stamp = merged.get("ladder_mono") or merged.get("recv_mono")
            age = 0.0 if stamp is None else max(0.0, time.monotonic() - float(stamp))
            logger.warning(
                "blank_ladder_frame token=%s streak=%s ltp=%s kept_bid=%s kept_ask=%s "
                "ladder_age_s=%.1f reason=%s",
                token,
                streak,
                merged.get("ltp"),
                merged.get("bid"),
                merged.get("ask"),
                age,
                incoming_status,
            )
        return merged

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

    #: Ladder quality labels — see ``_sanitize_depth``. Only ``ok`` publishes a
    #: fresh two-sided book; everything else is retained-or-refused.
    LADDER_OK = "ok"
    LADDER_BLANK = "blank"
    LADDER_NONE = "none"
    LADDER_ONE_SIDED = "one_sided"
    LADDER_CROSSED = "crossed"

    @classmethod
    def _sanitize_depth(cls, depth: Any) -> tuple[Dict[str, list], float, float, str]:
        """Level hygiene on the raw ladder: ``(depth, bid, ask, status)``.

        ``price <= 0`` was the only guard between mStock's bytes and the books,
        and a packet with a correct header but an *empty* five-level book still
        satisfied it — the unfilled levels are the ₹0.05/₹4 fills the 2026-11-03
        straddle booked as +₹71,639. So each level must carry a positive price
        and positive quantity, and the side is priced by the *first surviving*
        level (``bids[0]`` blind is §8a's first defect, while picking max/min
        would invent a better price than the venue put at level 0 if the array
        ever arrives out of order). Statuses:

        - ``ok``         both sides valid, best bid below best ask
        - ``one_sided``  only one side has a valid level (the other is zeroed —
                         mStock genuinely does this, so the real side stays)
        - ``crossed`` / ``blank`` / ``none``  no side survives

        Caller holds no lock; this is pure.
        """
        if not isinstance(depth, dict):
            return {}, 0.0, 0.0, cls.LADDER_NONE

        cleaned: Dict[str, list] = {}
        bids = cls._clean_levels(depth.get("bid"))
        asks = cls._clean_levels(depth.get("ask"))
        cleaned["bid"], cleaned["ask"] = bids, asks
        for key, value in depth.items():
            cleaned.setdefault(key, value)

        if not bids and not asks:
            return cleaned, 0.0, 0.0, cls.LADDER_BLANK

        best_bid = bids[0]["price"] if bids else 0.0
        best_ask = asks[0]["price"] if asks else 0.0

        if not bids or not asks:
            return cleaned, best_bid, best_ask, cls.LADDER_ONE_SIDED
        if best_bid >= best_ask:
            return cleaned, 0.0, 0.0, cls.LADDER_CROSSED
        return cleaned, best_bid, best_ask, cls.LADDER_OK

    @staticmethod
    def _clean_levels(levels: Any) -> List[Dict[str, Any]]:
        """Keep only levels with a positive price and positive visible size."""
        if not isinstance(levels, (list, tuple)):
            return []
        out: List[Dict[str, Any]] = []
        for level in levels:
            if not isinstance(level, dict):
                continue
            try:
                price = float(level.get("price", 0) or 0)
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            size = 0.0
            for key in ("quantity", "qty", "size"):
                raw = level.get(key)
                if raw is None:
                    continue
                try:
                    size = float(raw)
                except (TypeError, ValueError):
                    continue
                break
            if size <= 0:
                continue
            entry = dict(level)
            entry["price"] = price
            out.append(entry)
        return out

    @classmethod
    def _normalize_tick(
        cls,
        tick: dict,
        *,
        recv_mono: Optional[float] = None,
        recv_utc: Optional[str] = None,
    ) -> dict:
        """Normalize MTicker tick to standard quote format with monotonic arrival."""
        last_price = float(tick.get("last_price", 0))

        depth, bid, ask, ladder_status = cls._sanitize_depth(tick.get("depth"))

        mono = time.monotonic() if recv_mono is None else float(recv_mono)
        utc_iso = (
            datetime.now(timezone.utc).isoformat()
            if recv_utc is None
            else str(recv_utc)
        )

        row: dict = {
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
            "ladder_status": ladder_status,
            #: LTP has its own clock: a frame with a blank ladder still refreshes
            #: the last price, and the serve gate must not charge that ladder's
            #: age to it (or vice versa).
            "ltp_mono": mono,
        }
        if ladder_status == cls.LADDER_OK:
            row["ladder_mono"] = mono
        return row


# Explicit alias matching Plan §3.1 terminology
MStockTickBus = MStockWebSocketWrapper
