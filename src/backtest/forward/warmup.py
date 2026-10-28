"""Unified historical + intraday warmup engine and timeframe bar aggregator (Phase 1).

Solves three structural forward-engine requirements:

1. **Cold-start indicator blindness (Q-6, POOL-3, POOL-4)** — :class:`WarmupService`
   bulk-loads prior sessions from ``market_data_cache`` in a single SQL query
   (sharing :meth:`DatabaseManager.from_env`'s pool), stitches today's
   ``09:15 IST -> last_completed_minute`` session bars from the in-memory
   :class:`SessionBarRecorder` or rate-paced broker intraday endpoints
   (``< 2.5 req/s``), resamples to the runner's timeframe, and seeds
   :meth:`StrategyRunner.seed_history` **silently** (zero phantom orders).
2. **Timeframe-blind live bar fan-out (Recon Finding F1 / Caveat Q-1)** —
   :class:`TimeframeBarAggregator` aggregates incoming ``1min`` broker bars
   into true ``5min`` / ``10min`` / ``15min`` / ``30min`` / ``1hour`` /
   ``4hour`` / ``1day`` candles aligned to exact ``DbSource._resample`` clock
   buckets before invoking strategy evaluation, while updating live MTM on
   every intermediate minute.
3. **Zero redundant intraday HTTP calls on mid-day spawns** —
   :class:`SessionBarRecorder` retains today's streaming ``1min`` bars in RAM
   per ``(source, symbol)`` so newly spawned runners warm up instantaneously
   for already-active symbols (such as ``NIFTY`` and ``BANKNIFTY``).
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence

import pandas as pd

from backtest.data.base import (
    BARS_PER_TRADING_DAY,
    EXCHANGE_TZ,
    normalize_timeframe,
)
from backtest.data.db_source import _INTERVAL_TO_RULE, DbSource
from backtest.timekeeping import EXCHANGE_TZ, to_exchange

logger = logging.getLogger("backtest.forward.warmup")

__all__ = [
    "IST_OFFSET",
    "LOOKBACK_PARAM_HINTS",
    "SessionBarRecorder",
    "TimeframeBarAggregator",
    "WarmupReport",
    "WarmupService",
    "bucket_start_ts",
    "infer_warmup_bars",
    "normalize_ist_bar_ts",
    "requires_timeframe_aggregation",
]

IST_OFFSET = EXCHANGE_TZ

#: Parameter names that represent bar-count lookbacks on strategies.
LOOKBACK_PARAM_HINTS: tuple[str, ...] = (
    "ema_regime",
    "regime_ema",
    "slow_period",
    "fast_period",
    "slow_ema",
    "fast_ema",
    "ema_slow",
    "ema_fast",
    "ema_period",
    "sma_slow",
    "sma_fast",
    "sma_period",
    "rsi_period",
    "atr_period",
    "supertrend_period",
    "donchian_period",
    "bb_period",
    "bollinger_period",
    "macd_slow",
    "macd_signal",
    "roc_period",
    "vwap_period",
    "adx_period",
    "cci_period",
    "period",
    "lookback",
    "window",
    "length",
)


# ---------------------------------------------------------------------------
# Timestamp & bucket alignment helpers (IST exchange session clock)
# ---------------------------------------------------------------------------


def normalize_ist_bar_ts(value: Any) -> str:
    """Convert any broker or DB timestamp into canonical naive IST ``%Y-%m-%d %H:%M:%S``.

    Handles:
    * PostgreSQL ``timestamptz`` UTC/IST instants -> converted to ``Asia/Kolkata``
    * mStock intraday truncated ``+05`` offset (e.g. ``2026-10-01T12:10:00+05``
      where ``12:10:00`` is already IST wall-clock) -> repaired to ``+05:30``
    * Naive timestamps -> treated as IST exchange wall-clock per
      ``docs/DATA-INGEST-AND-TIMEFRAMES.md``
    """
    if isinstance(value, str):
        raw = value.strip()
        # mStock's intraday endpoint emits IST wall-clock times with a truncated
        # "+05" suffix instead of "+05:30" (verified in tests/data/test_mstock_feed.py).
        if len(raw) >= 3 and (raw.endswith("+05") or raw.endswith("-05")):
            raw = f"{raw}:30"
        ts = pd.Timestamp(raw)
    elif isinstance(value, (int, float)) and value > 1_000_000_000_000:
        ts = pd.Timestamp(value, unit="ms", tz="UTC")
    elif isinstance(value, (int, float)):
        ts = pd.Timestamp(value, unit="s", tz="UTC")
    else:
        ts = pd.Timestamp(value)

    if ts.tzinfo is not None:
        ts = ts.tz_convert(EXCHANGE_TZ).tz_localize(None)
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def bucket_start_ts(ts_str: str, timeframe: str) -> str:
    """Return the ``DbSource._resample`` bucket start timestamp for ``ts_str``.

    Matches pandas ``df.resample(rule)`` on naive IST timestamps so historical
    warmup bars and live aggregated bars share identical ``ts`` keys.
    """
    tf = normalize_timeframe(timeframe) or "1min"
    ts = pd.Timestamp(normalize_ist_bar_ts(ts_str))
    if tf == "1week":
        monday = ts.normalize() - pd.Timedelta(days=int(ts.weekday()))
        return monday.strftime("%Y-%m-%d %H:%M:%S")
    rule = _INTERVAL_TO_RULE.get(tf, "1min")
    floored = ts.floor(rule)
    return floored.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Strategy lookback inference
# ---------------------------------------------------------------------------


def _extract_max_lookback(strategy: Any) -> int:
    """Best-effort maximum indicator period declared on ``strategy``."""
    if strategy is None:
        return 0
    max_lb = 0
    schema = getattr(strategy, "params", None)
    keys: set[str] = set(LOOKBACK_PARAM_HINTS)
    if isinstance(schema, dict):
        for k in schema:
            kl = str(k).lower()
            if any(
                tok in kl
                for tok in ("period", "ema", "sma", "rsi", "atr", "lookback", "window", "len")
            ):
                keys.add(str(k))

    for key in keys:
        val = getattr(strategy, key, None)
        if isinstance(val, bool) or val is None:
            continue
        if isinstance(val, (int, float)) and 1 <= val <= 2000:
            max_lb = max(max_lb, int(val))
    return max_lb


def infer_warmup_bars(
    strategy: Any,
    timeframe: str = "1min",
    *,
    max_bars: int = 600,
) -> int:
    """Determine how many historical timeframe bars :class:`WarmupService` should seed.

    * Explicit ``strategy.warmup_bars`` (int >= 0) always wins.
    * Unconditional instant templates (``instant_long_option``, ``structure``)
      with no indicator lookback params need ``0`` historical bars.
    * Indicator strategies receive ``max(60, 3 * max_lookback)`` bars so IIR
      filters (EMA, RSI, Supertrend) converge before the first live signal;
      daily swing strategies with long MAs receive at least ``260`` bars (~1 yr).
    """
    explicit = getattr(strategy, "warmup_bars", None)
    if isinstance(explicit, int) and not isinstance(explicit, bool) and explicit >= 0:
        return min(explicit, max_bars)

    max_lb = _extract_max_lookback(strategy)
    template = str(
        getattr(strategy, "template", "") or getattr(strategy, "name", "") or ""
    ).strip().lower()
    if template in ("instant_long_option", "structure") and max_lb == 0:
        return 0

    tf = normalize_timeframe(timeframe) or "1min"
    target = max(60, 3 * max_lb) if max_lb > 0 else 60
    if tf == "1day" and max_lb >= 50:
        target = max(target, 260)
    return min(target, max_bars)


def requires_timeframe_aggregation(runner_or_tf: Any) -> bool:
    """True when ``1min`` live broker bars must aggregate into ``runner.config.timeframe``.

    Resolves Recon Finding F1 (``15min`` runner previously firing on ``1min``
    bars). QA-2026-10-28 BUG-01 removed the exception for the unconditional
    option templates (``CE_instant_buy``, ``PE_instant_buy``,
    ``immediate_strangle``): "enter now" is a one-shot deploy decision, not a
    licence to re-evaluate entries every minute on an hourly runner. Their
    intra-bar behaviour now lives in :attr:`OptionsBridge.entry_bucket_seconds`,
    which allows at most one entry per ``timeframe`` bucket while protective
    exits still run on every tick.
    """
    if isinstance(runner_or_tf, str):
        tf = normalize_timeframe(runner_or_tf) or "1min"
        return tf != "1min"
    cfg = getattr(runner_or_tf, "config", None)
    tf = normalize_timeframe(getattr(cfg, "timeframe", "1min")) or "1min"
    return tf != "1min"


# ---------------------------------------------------------------------------
# SessionBarRecorder — zero-HTTP intraday cache for active streaming symbols
# ---------------------------------------------------------------------------


class SessionBarRecorder:
    """Thread-safe in-memory ring buffer of today's ``1min`` bars per ``(source, symbol)``."""

    def __init__(self, maxlen: int = 450) -> None:
        self.maxlen = maxlen
        self._lock = threading.Lock()
        self._bars: Dict[tuple[str, str], Deque[Dict[str, Any]]] = {}

    def record(self, source: str, symbol: str, bar: Dict[str, Any]) -> None:
        if not source or not symbol or not isinstance(bar, dict):
            return
        raw_ts = bar.get("ts") or bar.get("timestamp")
        if not raw_ts:
            return
        try:
            ts = normalize_ist_bar_ts(raw_ts)
            close = float(bar["close"])
            clean = {
                "ts": ts,
                "open": float(bar.get("open", close)),
                "high": float(bar.get("high", close)),
                "low": float(bar.get("low", close)),
                "close": close,
                "volume": float(bar.get("volume", 0) or 0),
            }
        except (KeyError, TypeError, ValueError):
            return

        key = (str(source).strip().lower(), str(symbol).strip().upper())
        day = ts[:10]
        with self._lock:
            buf = self._bars.get(key)
            if buf is None:
                buf = deque(maxlen=self.maxlen)
                self._bars[key] = buf
            elif buf and buf[-1]["ts"][:10] != day and day > buf[-1]["ts"][:10]:
                # New IST trading session — roll yesterday's intraday buffer.
                buf.clear()

            if buf and buf[-1]["ts"] == ts:
                buf[-1] = clean
            elif not buf or ts > buf[-1]["ts"]:
                buf.append(clean)

    record_bar = record

    def get_bars(
        self, source: str, symbol: str, session_date: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        key = (str(source).strip().lower(), str(symbol).strip().upper())
        with self._lock:
            buf = self._bars.get(key)
            if not buf:
                return []
            rows = list(buf)
        if session_date:
            rows = [r for r in rows if r["ts"][:10] == session_date]
        return rows

    def clear(self) -> None:
        with self._lock:
            self._bars.clear()


# ---------------------------------------------------------------------------
# TimeframeBarAggregator — live 1min -> 5m / 15m / 1h / 1d candle sealer
# ---------------------------------------------------------------------------


class TimeframeBarAggregator:
    """Per-``(runner_id, symbol)`` stateful aggregator for ``1min`` live bars.

    Emits a sealed bar strictly when:
    1. An incoming ``1min`` bar crosses into a new timeframe bucket (sealing
       the prior bucket at its exact bucket-start ``ts``); OR
    2. An NSE closing print (``>= 15:30 IST``) arrives on an Indian broker
       source, sealing the active session bucket immediately so ``15min``,
       ``1hour``, and ``1day`` strategies evaluate the session close at 15:30
       instead of waiting overnight until tomorrow 09:15.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # (instance_id, symbol) -> forming state dict
        self._forming: Dict[tuple[str, str], Dict[str, Any]] = {}

    def ingest_minute_bar(
        self,
        instance_id: str,
        symbol: str,
        timeframe: str,
        bar: Dict[str, Any],
        *,
        nse_session_close: bool = True,
    ) -> List[Dict[str, Any]]:
        tf = normalize_timeframe(timeframe) or "1min"
        if tf == "1min":
            return [dict(bar)]

        ts = normalize_ist_bar_ts(bar.get("ts") or bar.get("timestamp"))
        close = float(bar["close"])
        o = float(bar.get("open", close))
        h = float(bar.get("high", close))
        low = float(bar.get("low", close))
        vol = float(bar.get("volume", 0) or 0)

        b_start = bucket_start_ts(ts, tf)
        hm = ts[11:16]  # "HH:MM"
        is_closing_print = nse_session_close and hm >= "15:30"
        key = (str(instance_id), str(symbol).strip().upper())

        emitted: List[Dict[str, Any]] = []
        with self._lock:
            cur = self._forming.get(key)
            if cur is not None:
                if ts <= cur["last_minute_ts"]:
                    return []
                if b_start > cur["bucket_ts"]:
                    # Crossed into a newer bucket -> seal the previous bucket!
                    emitted.append(self._to_bar(cur))
                    cur = None

            if cur is None:
                # At >= 15:30 on sub-hour intervals (5min/15min/30min), 15:30
                # is the closing bell tick that just sealed 15:15..15:29, not
                # the start of a new 15:30..15:45 trading candle.
                if is_closing_print and tf in ("5min", "10min", "15min", "30min") and emitted:
                    self._forming.pop(key, None)
                    return emitted
                cur = {
                    "bucket_ts": b_start,
                    "open": o,
                    "high": h,
                    "low": low,
                    "close": close,
                    "volume": vol,
                    "last_minute_ts": ts,
                }
                self._forming[key] = cur
            else:
                cur["high"] = max(cur["high"], h)
                cur["low"] = min(cur["low"], low)
                cur["close"] = close
                cur["volume"] += vol
                cur["last_minute_ts"] = ts

            # For 1hour / 4hour / 1day (or single 15:30 bar), 15:30 IST is the
            # final print of the NSE trading day — seal immediately.
            if is_closing_print:
                emitted.append(self._to_bar(cur))
                self._forming.pop(key, None)

        return emitted

    @staticmethod
    def _to_bar(state: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "ts": state["bucket_ts"],
            "open": float(state["open"]),
            "high": float(state["high"]),
            "low": float(state["low"]),
            "close": float(state["close"]),
            "volume": float(state["volume"]),
        }

    def drop_runner(self, instance_id: str) -> None:
        prefix = str(instance_id)
        with self._lock:
            dead = [k for k in self._forming if k[0] == prefix]
            for k in dead:
                self._forming.pop(k, None)

    def drop_symbol(self, instance_id: str, symbol: str) -> None:
        key = (str(instance_id), str(symbol).strip().upper())
        with self._lock:
            self._forming.pop(key, None)


# ---------------------------------------------------------------------------
# WarmupService — 1-SQL-query DB history + paced intraday gap-fill + resample
# ---------------------------------------------------------------------------


class _RatePacer:
    """Thread-safe minimum-spacing gate keeping HTTP bursts below ~2.2 req/s."""

    def __init__(
        self,
        min_interval_s: float = 0.45,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_interval_s = max(0.0, float(min_interval_s))
        self._sleep = sleep_fn
        self._lock = threading.Lock()
        self._next_allowed: float = 0.0

    def wait(self) -> None:
        if self.min_interval_s <= 0:
            return
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_allowed - now)
            self._next_allowed = max(now, self._next_allowed) + self.min_interval_s
        if delay > 0:
            self._sleep(delay)


DEFAULT_WARMUP_QUORUM_RATIO = 0.80


@dataclass
class WarmupReport:
    """Outcome summary returned by :meth:`WarmupService.warmup_runner`."""

    instance_id: str
    target_bars: int
    quorum_bars: int = 0
    seeded_symbols: Dict[str, int] = field(default_factory=dict)
    degraded_symbols: List[str] = field(default_factory=list)
    db_queries: int = 0
    db_symbols_hit: int = 0
    db_error: Optional[str] = None
    warmup_reason: Optional[str] = None
    recorder_symbols_hit: int = 0
    intraday_http_calls: int = 0
    elapsed_ms: float = 0.0

    @property
    def total_bars_seeded(self) -> int:
        return sum(self.seeded_symbols.values())


class WarmupService:
    """Seeds historical + today-intraday bars into a :class:`StrategyRunner` silently."""

    def __init__(
        self,
        *,
        db_source: Optional[DbSource] = None,
        session_recorder: Optional[SessionBarRecorder] = None,
        intraday_workers: int = 4,
        min_request_interval_s: float = 0.45,
        quorum_ratio: float = DEFAULT_WARMUP_QUORUM_RATIO,
        now_fn: Optional[Callable[[], datetime]] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self._db_source = db_source
        self.session_recorder = session_recorder
        self.intraday_workers = max(1, int(intraday_workers))
        self.quorum_ratio = min(1.0, max(0.05, float(quorum_ratio)))
        self._pacer = _RatePacer(min_interval_s=min_request_interval_s, sleep_fn=sleep_fn)
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))

    def _get_db_source(self) -> Optional[DbSource]:
        if self._db_source is not None:
            return self._db_source
        try:
            self._db_source = DbSource()
            return self._db_source
        except Exception as exc:  # noqa: BLE001 — warmup degrades cleanly if DB absent
            logger.debug("WarmupService: DbSource unavailable (%s)", exc)
            return None

    def warmup_runner(
        self,
        runner: Any,
        *,
        intraday_fetcher: Optional[Callable[[str], Sequence[Dict[str, Any]]]] = None,
        bar_aggregator: Optional[TimeframeBarAggregator] = None,
        symbols: Optional[Sequence[str]] = None,
    ) -> WarmupReport:
        """Fetch, stitch, resample, and silently seed warmup bars for ``runner``."""
        started = time.monotonic()
        tf = normalize_timeframe(getattr(runner.config, "timeframe", "1min")) or "1min"
        max_cap = int(getattr(runner, "max_bars_per_symbol", 600) or 600)
        target = infer_warmup_bars(getattr(runner, "strategy", None), tf, max_bars=max_cap)
        target_symbols = [
            str(s).strip().upper()
            for s in (symbols if symbols is not None else getattr(runner.config, "symbols", []))
            if str(s).strip()
        ]

        quorum_bars = (
            0 if target <= 0 else max(12, min(target, math.ceil(target * self.quorum_ratio)))
        )
        report = WarmupReport(
            instance_id=runner.instance_id,
            target_bars=target,
            quorum_bars=quorum_bars,
        )
        if target <= 0 or not target_symbols:
            runner.seed_history(
                {},
                degraded_symbols=[],
                target_bars=0,
                quorum_bars=0,
                warmup_reason=None,
            )
            report.elapsed_ms = round((time.monotonic() - started) * 1000.0, 2)
            return report

        runner.warmup_status = "WARMING_UP"
        now_utc = self._now_fn()
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)
        now_ist = to_exchange(now_utc)
        today_str = now_ist.strftime("%Y-%m-%d")
        current_minute_floor = now_ist.replace(second=0, microsecond=0).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        current_bucket_floor = bucket_start_ts(current_minute_floor, tf)

        # 1. Layer 1 — Strictly ONE ts-bounded bulk query against market_data_cache
        per_day = max(1.0, float(BARS_PER_TRADING_DAY.get(tf, 1.0)))
        trading_days_needed = math.ceil(target / per_day)
        # Multiply by 2x + 14 calendar days to span weekends & NSE holidays safely.
        calendar_days = max(10, int(trading_days_needed * 2.0) + 14)
        start_str = (now_ist.date() - timedelta(days=calendar_days)).isoformat()

        db_frames: Dict[str, pd.DataFrame] = {}
        db_src = self._get_db_source()
        if db_src is not None:
            report.db_queries = 1
            try:
                db_frames = db_src.get_candles_bulk(
                    target_symbols, start_str, today_str, interval=tf
                )
                report.db_symbols_hit = len(db_frames)
            except Exception as exc:  # noqa: BLE001 — surface DB failure in quorum reason
                report.db_error = f"{type(exc).__name__}: {exc}"
                logger.warning("WarmupService: bulk DB read failed (%s)", report.db_error)

        # 2. Layer 2 — Today's session gap-fill (intraday timeframes only).
        # Before 09:16 IST today has zero closed 1-minute candles, so skip HTTP
        # intraday fetches during the 09:00-09:15 pre-open rush.
        intraday_rows: Dict[str, List[Dict[str, Any]]] = {}
        can_fetch_today_http = (
            intraday_fetcher is not None and now_ist.strftime("%H:%M") >= "09:16"
        )
        if tf not in ("1day", "1week"):
            source_name = str(getattr(runner.config, "source", "") or "").lower()
            need_http: List[str] = []
            for sym in target_symbols:
                rec_bars = (
                    self.session_recorder.get_bars(source_name, sym, session_date=today_str)
                    if self.session_recorder is not None
                    else []
                )
                # Drop any unclosed current minute
                rec_closed = [b for b in rec_bars if b["ts"] < current_minute_floor]
                if rec_closed and rec_closed[0]["ts"][11:16] <= "09:20":
                    intraday_rows[sym] = rec_closed
                    report.recorder_symbols_hit += 1
                else:
                    if rec_closed:
                        intraday_rows[sym] = rec_closed
                    if can_fetch_today_http:
                        need_http.append(sym)

            if need_http and can_fetch_today_http and intraday_fetcher is not None:
                fetched = self._fetch_intraday_paced(
                    need_http, intraday_fetcher, current_minute_floor
                )
                report.intraday_http_calls = len(need_http)
                for sym, rows in fetched.items():
                    if rows:
                        existing = {b["ts"]: b for b in intraday_rows.get(sym, [])}
                        for b in rows:
                            existing[b["ts"]] = b
                        intraday_rows[sym] = [existing[k] for k in sorted(existing)]

        # 3. Stitch Layer 1 + Layer 2, resample to runner timeframe, trim & seed
        bars_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
        degraded: List[str] = []

        is_market_hours = "09:15" <= now_ist.strftime("%H:%M") < "15:30"
        for sym in target_symbols:
            sym_intraday = intraday_rows.get(sym, [])
            merged = self._stitch_and_resample(
                db_frame=db_frames.get(sym),
                intraday=sym_intraday,
                target_tf=tf,
                current_bucket_floor=current_bucket_floor,
                is_market_hours=is_market_hours,
                today_str=today_str,
            )
            if len(merged) > target:
                merged = merged[-target:]
            if merged:
                bars_by_symbol[sym] = merged
                report.seeded_symbols[sym] = len(merged)
            if len(merged) < quorum_bars:
                degraded.append(sym)
            # Prime partial current bucket in TimeframeBarAggregator so the very
            # next sealed timeframe candle includes earlier minutes of its bucket.
            if (
                bar_aggregator is not None
                and is_market_hours
                and requires_timeframe_aggregation(tf)
                and sym_intraday
            ):
                for m_bar in sym_intraday:
                    if bucket_start_ts(m_bar["ts"], tf) == current_bucket_floor:
                        bar_aggregator.ingest_minute_bar(
                            runner.instance_id, sym, tf, m_bar
                        )

        report.degraded_symbols = degraded
        reason_parts: List[str] = []
        if report.db_error:
            reason_parts.append(f"DB warmup error ({report.db_error})")
        if degraded:
            min_cnt = min(
                (report.seeded_symbols.get(s, 0) for s in target_symbols),
                default=0,
            )
            sample = ", ".join(degraded[:5])
            if len(degraded) > 5:
                sample += f" (+{len(degraded) - 5} more)"
            reason_parts.append(
                f"{len(degraded)}/{len(target_symbols)} symbol(s) below quorum "
                f"(min {min_cnt}/{target} bars, need >={quorum_bars}: {sample})"
            )
        report.warmup_reason = "; ".join(reason_parts) if degraded else None

        runner.seed_history(
            bars_by_symbol,
            degraded_symbols=degraded,
            target_bars=target,
            quorum_bars=quorum_bars,
            warmup_reason=report.warmup_reason,
        )
        report.elapsed_ms = round((time.monotonic() - started) * 1000.0, 2)
        logger.info(
            "WARMUP_COMPLETE runner=%s (%s) tf=%s target=%d quorum=%d "
            "seeded_symbols=%d/%d total_bars=%d degraded=%d db_queries=%d "
            "db_hits=%d http_calls=%d elapsed=%.1fms",
            getattr(runner.config, "name", runner.instance_id[:8]),
            runner.instance_id[:8],
            tf,
            target,
            quorum_bars,
            len(bars_by_symbol),
            len(target_symbols),
            report.total_bars_seeded,
            len(degraded),
            report.db_queries,
            report.db_symbols_hit,
            report.intraday_http_calls,
            report.elapsed_ms,
        )
        return report

    def _fetch_intraday_paced(
        self,
        symbols: Sequence[str],
        fetcher: Callable[[str], Sequence[Dict[str, Any]]],
        current_minute_floor: str,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Fetch today's minute bars across a rate-paced worker pool (<2.5 req/s)."""
        out: Dict[str, List[Dict[str, Any]]] = {}

        def _task(sym: str) -> tuple[str, List[Dict[str, Any]]]:
            self._pacer.wait()
            raw_rows = fetcher(sym) or []
            cleaned: List[Dict[str, Any]] = []
            for row in raw_rows:
                if not isinstance(row, dict):
                    continue
                raw_ts = row.get("ts") or row.get("timestamp")
                if not raw_ts:
                    continue
                try:
                    ts = normalize_ist_bar_ts(raw_ts)
                    if ts >= current_minute_floor:
                        # Never seed the still-forming current minute as a closed bar
                        continue
                    close = float(row["close"])
                    cleaned.append(
                        {
                            "ts": ts,
                            "open": float(row.get("open", close)),
                            "high": float(row.get("high", close)),
                            "low": float(row.get("low", close)),
                            "close": close,
                            "volume": float(row.get("volume", 0) or 0),
                        }
                    )
                except (KeyError, TypeError, ValueError):
                    continue
            cleaned.sort(key=lambda b: b["ts"])
            return sym, cleaned

        workers = min(self.intraday_workers, len(symbols))
        if workers <= 1:
            for s in symbols:
                try:
                    sym, rows = _task(s)
                    out[sym] = rows
                except Exception as exc:  # noqa: BLE001 — per-symbol quorum isolation
                    logger.warning("WarmupService: intraday fetch failed for %s: %s", s, exc)
            return out

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="warmup-intraday") as pool:
            futs = {pool.submit(_task, s): s for s in symbols}
            for fut in as_completed(futs):
                sym = futs[fut]
                try:
                    _, rows = fut.result()
                    out[sym] = rows
                except Exception as exc:  # noqa: BLE001 — one bad symbol never kills pool warmup
                    logger.warning("WarmupService: intraday fetch failed for %s: %s", sym, exc)
        return out

    @staticmethod
    def _stitch_and_resample(
        *,
        db_frame: Optional[pd.DataFrame],
        intraday: List[Dict[str, Any]],
        target_tf: str,
        current_bucket_floor: str,
        is_market_hours: bool,
        today_str: str,
    ) -> List[Dict[str, Any]]:
        by_ts: Dict[str, Dict[str, Any]] = {}
        if db_frame is not None and not db_frame.empty:
            for raw_idx, row in db_frame.iterrows():
                try:
                    ts = normalize_ist_bar_ts(raw_idx)
                    by_ts[ts] = {
                        "ts": ts,
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "volume": float(row.get("volume", 0) or 0),
                    }
                except (KeyError, TypeError, ValueError):
                    continue

        for b in intraday:
            by_ts[b["ts"]] = b

        if not by_ts:
            return []

        ordered = [by_ts[k] for k in sorted(by_ts)]
        if target_tf in ("1min", "1day", "1week"):
            if is_market_hours and target_tf in ("1day", "1week"):
                # During live market hours, today's daily bar is not yet sealed.
                ordered = [b for b in ordered if b["ts"][:10] < today_str]
            return ordered

        rule = _INTERVAL_TO_RULE.get(target_tf)
        if not rule:
            return ordered

        df = pd.DataFrame(ordered)
        df["ts"] = pd.to_datetime(df["ts"])
        df = df.set_index("ts").sort_index()
        resampled = (
            df.resample(rule)
            .agg(
                {
                    "open": "first",
                    "high": "max",
                    "low": "min",
                    "close": "last",
                    "volume": "sum",
                }
            )
            .dropna(subset=["close"])
        )

        result: List[Dict[str, Any]] = []
        for idx, row in resampled.iterrows():
            b_ts = idx.strftime("%Y-%m-%d %H:%M:%S")
            # Drop the currently forming unclosed timeframe bucket during market hours
            if is_market_hours and b_ts >= current_bucket_floor:
                continue
            result.append(
                {
                    "ts": b_ts,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row["volume"]),
                }
            )
        return result
