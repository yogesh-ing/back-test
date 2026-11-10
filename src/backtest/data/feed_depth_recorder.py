"""Packet-level market-depth recorder, gated by the ``ENABLE_FEED`` flag.

Built after the 2026-11-08 Muhurat incident
(``docs/OPTION-JUNK-ASK-INCIDENT-2026-11-08.md``): an offer of ₹493.65 on a put whose
last trade was ₹367.00 stopped a straddle out for −₹11,794, and we could not prove
whether the broker published that offer side or we misread a good one — the app keeps
only best bid, best ask and last trade per contract, in RAM, and writes none of them
down. This module keeps what the broker actually sent: all five levels on each side,
with quantities, under both the exchange's stamp and ours.

Two rules govern everything below.

* **Off unless asked.** Nothing is captured, no thread starts and no directory is
  created unless ``ENABLE_FEED`` is truthy in the environment. Because ``backtest/__init__``
  loads ``.env`` into the environment at import, ``ENABLE_FEED=true`` in ``.env`` is the
  operator's switch; anything else, or absent, stays off.
* **It may never trade.** Feed threads and the execution seam call this on their hot
  path, so every entry point is a non-blocking queue hand-off wrapped in a guard: a
  full queue, an unserialisable field or a read-only disk costs a line of log and a
  drop counter, never a missed fill.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from datetime import date
from typing import Any, Optional

from backtest.timekeeping import iso_utc, now_utc, session_date

logger = logging.getLogger("backtest.data.feed_depth_recorder")

#: The flag. Truthy values turn packet capture on for every broker's wrapper.
ENABLE_FEED_ENV = "ENABLE_FEED"
#: Optional capture directory override; defaults to ``logs/feed_depth`` (gitignored).
ENABLE_FEED_DIR_ENV = "ENABLE_FEED_DIR"
#: Optional ceiling on one session day's capture, in bytes.
ENABLE_FEED_MAX_DAY_BYTES_ENV = "ENABLE_FEED_MAX_DAY_BYTES"

DEFAULT_DIR = os.path.join("logs", "feed_depth")
_TRUTHY = {"1", "true", "yes", "on"}
_MAX_QUEUE = 20_000
#: 512 MB: a full NSE session of five-level packets measured 1.09 MB/min on
#: 2026-11-09 (09:15→15:30 ≈ 410 MB), so the old 64 MB stopped the capture around
#: 10:15 and the whole point was to have the tape at the exits.
_MAX_DAY_BYTES = 512 * 1024 * 1024
_DAY_CACHE_S = 30.0

_SCHEMA_VERSION = 1


def flag_enabled(env: Optional[dict[str, str]] = None) -> bool:
    """True when ``ENABLE_FEED`` asks for packet capture."""
    source = os.environ if env is None else env
    return str(source.get(ENABLE_FEED_ENV, "")).strip().lower() in _TRUTHY


def capture_dir(env: Optional[dict[str, str]] = None) -> str:
    source = os.environ if env is None else env
    return str(source.get(ENABLE_FEED_DIR_ENV, "").strip() or DEFAULT_DIR)


def day_cap(env: Optional[dict[str, str]] = None) -> int:
    """Bytes one session day may write; a bad override falls back, not off."""
    source = os.environ if env is None else env
    raw = str(source.get(ENABLE_FEED_MAX_DAY_BYTES_ENV, "")).strip()
    if not raw:
        return _MAX_DAY_BYTES
    try:
        value = int(float(raw))
    except ValueError:
        logger.warning("ignoring %s=%r (not a byte count)", ENABLE_FEED_MAX_DAY_BYTES_ENV, raw)
        return _MAX_DAY_BYTES
    return value if value > 0 else _MAX_DAY_BYTES


def _scalar(value: Any) -> Any:
    """Coerce a price/qty field to something JSON can hold without a custom encoder."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)


def _levels(depth: Any, side: str) -> list[list[Any]]:
    """``[[price, qty], …]`` for one side of a sanitized ladder, in broker order."""
    if not isinstance(depth, dict):
        return []
    out: list[list[Any]] = []
    for level in depth.get(side) or []:
        if isinstance(level, dict):
            out.append([_scalar(level.get("price")), _scalar(
                level.get("quantity", level.get("qty", level.get("size")))
            )])
    return out


def _slug(broker: str) -> str:
    """Filesystem-safe broker name."""
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in broker)


def _broker_word(broker: str) -> str:
    """``live:mstock+bidask`` → ``mstock``: one socket's lines share one file."""
    return str(broker).split("+")[0].rsplit(":", 1)[-1].strip().lower() or "unknown"


class FeedDepthRecorder:
    """Append-only JSONL per broker per trading day, written on its own thread."""

    def __init__(
        self,
        directory: str = DEFAULT_DIR,
        *,
        max_day_bytes: int = _MAX_DAY_BYTES,
    ) -> None:
        self.directory = str(directory)
        self.max_day_bytes = int(max_day_bytes)
        self._q: "queue.Queue[Optional[dict]]" = queue.Queue(maxsize=_MAX_QUEUE)
        self._thread: Optional[threading.Thread] = None
        self._start_lock = threading.Lock()
        self._files: dict[str, Any] = {}
        self._written: dict[str, int] = {}
        self._day = ""
        self._day_cached = ""
        self._day_mono = 0.0
        self.dropped = 0
        self._dropped_logged = 0.0
        self._stop = threading.Event()

    # -- public (hot path) ---------------------------------------------------

    def record(self, payload: dict) -> None:
        """Hand one line to the writer. Never blocks, never raises."""
        try:
            self._ensure_thread()
            self._q.put_nowait(payload)
        except queue.Full:
            self._note_drop("queue full")
        except Exception:  # noqa: BLE001 — capture must not reach the trading path
            logger.debug("feed capture failed", exc_info=True)

    def close(self) -> None:
        """Drain and close — used by tests and by a clean shutdown."""
        self._stop.set()
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._close_files()

    # -- writer thread -------------------------------------------------------

    def _ensure_thread(self) -> None:
        if self._thread is not None:
            return
        with self._start_lock:
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="feed-depth-recorder", daemon=True
                )
                self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                break
            self._write(item)
        while True:  # flush whatever is already queued
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                self._write(item)
        self._close_files()

    def _write(self, payload: dict) -> None:
        broker = _broker_word(str(payload.get("broker") or "unknown"))
        try:
            path = self._path_for(broker)
            if path is None:
                return
            line = json.dumps(payload, separators=(",", ":"), default=str) + "\n"
            handle = self._files.get(broker)
            if handle is None:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                handle = self._files[broker] = open(path, "a", encoding="utf-8")  # noqa: SIM115
            handle.write(line)
            handle.flush()
            written = self._written.get(broker, 0) + len(line.encode("utf-8"))
            self._written[broker] = written
            if written > self.max_day_bytes:
                self._note_drop(f"daily cap {self.max_day_bytes} bytes reached")
                self._close_files()
        except Exception as exc:  # noqa: BLE001 — a disk fault must not outlive itself
            self._note_drop(f"write failed: {exc}")

    def _path_for(self, broker: str) -> Optional[str]:
        today = self._session_day()
        if not today:
            return None
        if today != self._day:  # new session day: roll every broker onto a new file
            self._day = today
            self._close_files()
        return os.path.join(self.directory, f"feed_{_slug(broker)}_{today}.jsonl")

    def _session_day(self) -> str:
        now = time.monotonic()
        if self._day_cached and now - self._day_mono < _DAY_CACHE_S:
            return self._day_cached
        stamp: Optional[date] = session_date(now_utc())
        self._day_cached = stamp.isoformat() if stamp else ""
        self._day_mono = now
        return self._day_cached

    def _close_files(self) -> None:
        for handle in self._files.values():
            try:
                handle.close()
            except OSError:
                pass
        self._files.clear()
        self._written.clear()

    def _note_drop(self, why: str) -> None:
        self.dropped += 1
        now = time.monotonic()
        if now - self._dropped_logged > 60.0:
            self._dropped_logged = now
            logger.warning("feed depth capture dropping lines (%s), dropped=%d", why, self.dropped)


# -- module facade ---------------------------------------------------------

_RECORDER: Optional[FeedDepthRecorder] = None
_STATE_LOCK = threading.Lock()


def get_recorder(env: Optional[dict[str, str]] = None) -> Optional[FeedDepthRecorder]:
    """The shared recorder, or None while the flag is off."""
    global _RECORDER
    if _RECORDER is not None:
        return _RECORDER
    if not flag_enabled(env):
        return None
    with _STATE_LOCK:
        if _RECORDER is None:
            _RECORDER = FeedDepthRecorder(capture_dir(env), max_day_bytes=day_cap(env))
            logger.info("ENABLE_FEED on: capturing depth packets to %s", _RECORDER.directory)
    return _RECORDER


def reset_recorder() -> None:
    """Tear the singleton down (tests, and a flag change needs a restart anyway)."""
    global _RECORDER
    with _STATE_LOCK:
        if _RECORDER is not None:
            _RECORDER.close()
        _RECORDER = None


def _row_line(
    broker: str, row: dict, kind: str, *, token: Any = None, raw_depth: Any = None
) -> dict:
    depth = row.get("depth")
    line = {
        "v": _SCHEMA_VERSION,
        "kind": kind,
        "broker": _broker_word(broker),
        "source_label": broker,
        "token": _scalar(token if token is not None else row.get("instrument_token")),
        "symbol": row.get("symbol") or row.get("trading_symbol"),
        "mode": row.get("mode"),
        "ladder_status": row.get("ladder_status"),
        "ltp": _scalar(row.get("ltp")),
        "bid": _scalar(row.get("bid")),
        "ask": _scalar(row.get("ask")),
        "bids": _levels(depth, "bid"),
        "asks": _levels(depth, "ask"),
        "oi": _scalar(row.get("oi")),
        "volume": _scalar(row.get("volume")),
        "exchange_ts": _scalar(row.get("timestamp")),
        "recv_utc": row.get("recv_utc") or iso_utc(now_utc()),
        "recv_mono": _scalar(row.get("recv_mono")),
        "ltp_mono": _scalar(row.get("ltp_mono")),
        "ladder_mono": _scalar(row.get("ladder_mono")),
    }
    if raw_depth is not None:
        line["raw_depth"] = raw_depth
    return line


def record_depth_packet(
    broker: str, row: dict, *, token: Any = None, raw_depth: Any = None
) -> None:
    """Capture one broker packet (all five levels per side). No-op when the flag is off."""
    recorder = get_recorder()
    if recorder is None:
        return
    recorder.record(_row_line(broker, row, "packet", token=token, raw_depth=raw_depth))


def record_price_used(quote: dict, *, broker: str, token: Any, side: str, field: str) -> None:
    """Capture the packet a fill (or a refused fill) was priced from.

    ``field`` is the ladder slot the engine turned into a trade price — ``ask`` for a
    buy, ``bid`` for a sell — so a disputed fill is traceable to the levels that
    produced it instead of surviving only as one number on an order row.
    """
    recorder = get_recorder()
    if recorder is None:
        return
    line = _row_line(broker, quote, "price_used", token=token)
    line["side"] = side
    line["field"] = field
    line["price_used"] = _scalar(quote.get(field))
    recorder.record(line)


def record_sanity_flag(
    quote: dict, *, broker: str, token: Any, reason: str, mode: str, field: str,
    price: float, detail: str,
) -> None:
    """Capture one seam rejection (or shadow disagreement) with its evidence.

    A ``quote_sanity`` log line names the price but not the book it came from, and
    the thresholds for ``pair_parity`` have to be derived from a session of these
    rows rather than hand-picked. The line carries the packet the check ran on and
    the check's own numbers, so the tape answers "how often would a shadow gate
    have fired, and against what reference".
    """
    recorder = get_recorder()
    if recorder is None:
        return
    line = _row_line(broker, quote, "sanity_flag", token=token)
    line["reason"] = reason
    line["mode"] = mode
    line["field"] = field
    line["price_used"] = _scalar(price)
    line["detail"] = detail
    recorder.record(line)


def record_shadow_diff(payload: dict, *, kind: str = "shadow_diff") -> None:
    """Capture one cross-broker comparison (item F, shadow mode).

    ``payload`` carries both brokers' views of the same contract; it is written to
    its own ``feed_shadow_<day>.jsonl`` so a reader can tell a witness from a
    packet. Nothing downstream may gate on it — the thresholds come from a session
    of these rows, not from a hand-picked number.
    """
    recorder = get_recorder()
    if recorder is None:
        return
    line = {"v": _SCHEMA_VERSION, "kind": kind, "broker": "shadow"}
    line.update(payload)
    line["recv_utc"] = iso_utc(now_utc())
    recorder.record(line)


__all__ = [
    "ENABLE_FEED_DIR_ENV",
    "ENABLE_FEED_ENV",
    "ENABLE_FEED_MAX_DAY_BYTES_ENV",
    "FeedDepthRecorder",
    "capture_dir",
    "day_cap",
    "flag_enabled",
    "get_recorder",
    "record_depth_packet",
    "record_price_used",
    "record_sanity_flag",
    "record_shadow_diff",
    "reset_recorder",
]
