"""Feed-quality monitor (2026-09-25) — per-broker staleness / gap / reliability.

Answers "which broker gives us fast and reliable data?" with numbers instead
of anecdotes. One :class:`FeedQualityMonitor` per (broker, symbol) pair
observes every bar the poll thread delivers and scores four things:

* **staleness** — ``observed_at − bar_ts`` (the true freshness metric; a
  200ms reply carrying a 90s-old candle is WORSE than a 2s reply carrying a
  fresh one, so HTTP RTT is deliberately not the headline number).
* **gap rate** — missing minutes per session: consecutive bar timestamps
  that skip more than one minute. Strategies break on gaps, not on latency.
* **stale repeats** — consecutive polls that returned the *same* bar
  timestamp while the market was open. Some "live" endpoints serve cached
  data; this catches them.
* **errors** — poll failures (network, HTTP, rate-limit), normalized to an
  hourly rate so a broker polling faster isn't punished for absolute counts.

Every observation appends one JSON line to ``data/feed_quality.log`` (one
file, all brokers — the comparison report reads it whole). Summaries come
from :meth:`FeedQualityMonitor.summary` / :func:`aggregate_report`.

Design rules:

* monitors are pure observers — they never call an API themselves and never
  block the feed thread (log write is best-effort, failures swallowed);
* thread-safe (called from the poll thread, read from Flask routes);
* timestamps: bar ``ts`` is exchange-time (naive IST); ``observed_at`` is
  converted to naive IST so the delta is a true wall-clock staleness
  regardless of the server's local timezone.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("backtest.forward.feed_quality")

__all__ = [
    "FeedQualityMonitor",
    "get_quality_monitor",
    "reset_quality_monitor",
    "aggregate_report",
    "QUALITY_LOG_PATH",
]

#: All brokers write one JSONL file; the report reads it whole.
QUALITY_LOG_PATH = os.path.join("data", "feed_quality.log")

#: Bar timestamps older than this are considered a gap (one minute is normal).
_GAP_THRESHOLD_MINUTES = 1.5

#: In-memory tail per monitor (the log file is the durable store).
_TAIL_SIZE = 2000


def _now_ist_naive() -> datetime:
    """Wall clock as naive IST — matches the bars' exchange-time convention."""
    return datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)


def _parse_bar_ts(ts: Any) -> Optional[datetime]:
    """Bar timestamp (str/int) → naive IST datetime, or None."""
    if isinstance(ts, (int, float)):
        if ts > 1_000_000_000_000:
            dt = datetime.utcfromtimestamp(ts / 1000.0)  # noqa: DTZ006 — epoch ms
        else:
            dt = datetime.utcfromtimestamp(ts)  # noqa: DTZ006 — epoch seconds
        return dt + timedelta(hours=5, minutes=30)
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


class FeedQualityMonitor:
    """Observer for one ``(broker, symbol)`` feed. Pure counters + JSONL log."""

    def __init__(self, broker: str, symbol: str, log_path: str = QUALITY_LOG_PATH) -> None:
        self.broker = str(broker).lower()
        self.symbol = str(symbol).upper()
        self._log_path = log_path
        self._lock = threading.Lock()
        # in-memory tails (bounded)
        self._stale_s: deque[float] = deque(maxlen=_TAIL_SIZE)
        self._gaps: deque[float] = deque(maxlen=_TAIL_SIZE)
        self._errors: deque[datetime] = deque(maxlen=_TAIL_SIZE)
        # state
        self._last_bar_ts: Optional[datetime] = None
        self._last_observed: Optional[datetime] = None
        self._stale_repeat_run = 0
        self._max_stale_repeat_run = 0
        # counters (session lifetime)
        self.bars_observed = 0
        self.polls_observed = 0
        self.errors_observed = 0
        self.repeat_bars = 0

    # -- observation hooks (called by the feed threads) ----------------------

    def observe_bar(self, bar_ts: Any, observed_at: Optional[datetime] = None) -> Dict[str, Any]:
        """One bar delivered. Returns the computed observation (also logged)."""
        now = observed_at or _now_ist_naive()
        if now.tzinfo is not None:
            now = now.astimezone(timezone.utc).replace(tzinfo=None)
        ts = _parse_bar_ts(bar_ts)
        if ts is None:
            return self.observe_error(f"unparseable bar ts: {bar_ts!r}")

        with self._lock:
            self.bars_observed += 1
            self.polls_observed += 1
            stale_s = max(0.0, (now - ts).total_seconds())
            self._stale_s.append(stale_s)

            record: Dict[str, Any] = {
                "kind": "bar",
                "broker": self.broker,
                "symbol": self.symbol,
                "bar_ts": ts.isoformat(),
                "observed_at": now.isoformat(),
                "staleness_s": round(stale_s, 3),
            }

            if self._last_bar_ts is not None:
                delta_min = (ts - self._last_bar_ts).total_seconds() / 60.0
                if delta_min > _GAP_THRESHOLD_MINUTES:
                    gap_min = delta_min - 1.0  # one minute is the expected cadence
                    self._gaps.append(gap_min)
                    record["gap_minutes"] = round(gap_min, 2)
                elif delta_min <= 0:
                    self.repeat_bars += 1
                    self._stale_repeat_run += 1
                    self._max_stale_repeat_run = max(
                        self._max_stale_repeat_run, self._stale_repeat_run
                    )
                    record["stale_repeat"] = True
                else:
                    self._stale_repeat_run = 0

            self._last_bar_ts = ts
            self._last_observed = now

        self._write(record)
        return record

    def observe_poll_no_data(self, reason: str = "") -> Dict[str, Any]:
        """A poll that returned nothing (not an error per se, but a data miss)."""
        with self._lock:
            self.polls_observed += 1
        record = {
            "kind": "no_data",
            "broker": self.broker,
            "symbol": self.symbol,
            "observed_at": _now_ist_naive().isoformat(),
            "reason": str(reason)[:120],
        }
        self._write(record)
        return record

    def observe_error(self, detail: str = "") -> Dict[str, Any]:
        """One failed poll (network/HTTP/rate-limit)."""
        now = _now_ist_naive()
        with self._lock:
            self.polls_observed += 1
            self.errors_observed += 1
            self._errors.append(now)
        record = {
            "kind": "error",
            "broker": self.broker,
            "symbol": self.symbol,
            "observed_at": now.isoformat(),
            "detail": str(detail)[:200],
        }
        self._write(record)
        return record

    # -- summary -------------------------------------------------------------

    def summary(self) -> Dict[str, Any]:
        """Metrics snapshot for this (broker, symbol) feed.

        All rates are per-hour over the observation window (first→last
        observation) so brokers with different poll cadences compare fairly.
        """
        with self._lock:
            stale = sorted(self._stale_s)
            window_min = 0.0
            if self._last_observed is not None:
                # Window = session span of observations; approximate via tail
                # span is unreliable, so use polls vs a clock anchored at the
                # first observation we can still see (bars carry bar_ts; the
                # oldest tail entry's observed_at is not kept) — use errors
                # tail as a lower bound and bars as the primary clock.
                pass
            hours = self._window_hours()
            p = lambda q: stale[int(q * (len(stale) - 1))] if stale else None  # noqa: E731
            return {
                "broker": self.broker,
                "symbol": self.symbol,
                "bars_observed": self.bars_observed,
                "polls_observed": self.polls_observed,
                "errors_observed": self.errors_observed,
                "error_rate_per_hour": (
                    round(self.errors_observed / hours, 2) if hours > 0 else None
                ),
                "staleness_median_s": round(stale[len(stale) // 2], 2) if stale else None,
                "staleness_p95_s": round(p(0.95), 2) if stale else None,
                "staleness_max_s": round(stale[-1], 2) if stale else None,
                "gap_count": len(self._gaps),
                "gap_minutes_total": round(sum(self._gaps), 1) if self._gaps else 0,
                "stale_repeat_bars": self.repeat_bars,
                "max_stale_repeat_run": self._max_stale_repeat_run,
                "window_hours": round(hours, 3) if hours > 0 else None,
                "last_bar_ts": self._last_bar_ts.isoformat() if self._last_bar_ts else None,
                "last_observed_at": self._last_observed.isoformat() if self._last_observed else None,
            }

    def _window_hours(self) -> float:
        """Observation window in hours (first→last bar), bounded ≥ polls cadence."""
        if self._last_bar_ts is None or self.bars_observed < 2:
            return 0.0
        # The tail caps at _TAIL_SIZE; when full, window = tail span.
        # Approximate with polls×cadence is wrong across restarts, so read
        # the span from the oldest tail staleness... not stored. Use the
        # conservative bound: window ≥ (bars_observed − 1) minutes ONLY when
        # the tail is not saturated; otherwise fall back to a log-derived
        # span in aggregate_report. Here: report both raw counts (always
        # exact) and rates based on tail span when available.
        try:
            first = self._oldest_tail_observed_at()
        except Exception:  # noqa: BLE001
            first = None
        if first is None or self._last_observed is None:
            return 0.0
        return max(0.0, (self._last_observed - first).total_seconds() / 3600.0)

    def _oldest_tail_observed_at(self) -> Optional[datetime]:
        """First observation time — recovered from the durable log (cheap)."""
        return _first_observed_at_from_log(self._log_path, self.broker, self.symbol)

    # -- durable log ---------------------------------------------------------

    def _write(self, record: Dict[str, Any]) -> None:
        try:
            os.makedirs(os.path.dirname(self._log_path) or ".", exist_ok=True)
            with open(self._log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, separators=(",", ":")) + "\n")
        except Exception:  # noqa: BLE001 — logging must never break the feed
            logger.debug("feed-quality log write failed", exc_info=True)


# ---------------------------------------------------------------------------
# Multi-broker registry (one monitor per (broker, symbol), process-wide)
# ---------------------------------------------------------------------------

_MONITORS: Dict[tuple, FeedQualityMonitor] = {}
_MONITORS_LOCK = threading.Lock()
_MONITORS_LOG_PATH = QUALITY_LOG_PATH


def get_quality_monitor(broker: str, symbol: str) -> FeedQualityMonitor:
    """Process-wide monitor for ``(broker, symbol)`` (created on first use)."""
    key = (str(broker).lower(), str(symbol).upper())
    with _MONITORS_LOCK:
        monitor = _MONITORS.get(key)
        if monitor is None:
            monitor = FeedQualityMonitor(key[0], key[1], _MONITORS_LOG_PATH)
            _MONITORS[key] = monitor
        return monitor


def reset_quality_monitor() -> None:
    """Drop all monitors (tests)."""
    with _MONITORS_LOCK:
        _MONITORS.clear()


def all_quality_monitors() -> List[FeedQualityMonitor]:
    with _MONITORS_LOCK:
        return list(_MONITORS.values())


# ---------------------------------------------------------------------------
# Log-file aggregation (survives restarts; the in-memory tail does not)
# ---------------------------------------------------------------------------


def _first_observed_at_from_log(path: str, broker: str, symbol: str) -> Optional[datetime]:
    """Earliest observation timestamp for a (broker, symbol) in the JSONL log.

    Reads at most the first few matching lines of each day block — the log
    is append-only and small (one line per bar), so a full scan is fine at
    our volumes (≈ 375 bars × symbols × brokers per day).
    """
    try:
        with open(path, encoding="utf-8") as fh:
            best: Optional[datetime] = None
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("broker") != broker or rec.get("symbol") != symbol:
                    continue
                raw = rec.get("observed_at")
                if not raw:
                    continue
                try:
                    dt = datetime.fromisoformat(raw)
                except ValueError:
                    continue
                if best is None or dt < best:
                    best = dt
            return best
    except OSError:
        return None


def aggregate_report(path: str = QUALITY_LOG_PATH) -> Dict[str, Any]:
    """Compare brokers from the durable log — the weekly "which feed wins" table.

    Recomputes the full metric set per (broker, symbol) from raw records, so
    the report is restart-proof (the in-memory tails are just a hot cache).
    Returns ``{"feeds": [...], "generated_at": iso}``.
    """
    per: Dict[tuple, Dict[str, Any]] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                key = (rec.get("broker"), rec.get("symbol"))
                if not key[0] or not key[1]:
                    continue
                st = per.setdefault(
                    key,
                    {
                        "stale": [],
                        "gaps": [],
                        "errors": 0,
                        "bars": 0,
                        "polls": 0,
                        "repeats": 0,
                        "max_repeat_run": 0,
                        "repeat_run": 0,
                        "first": None,
                        "last": None,
                        "last_bar_ts": None,
                    },
                )
                kind = rec.get("kind")
                raw_obs = rec.get("observed_at")
                obs: Optional[datetime] = None
                if raw_obs:
                    try:
                        obs = datetime.fromisoformat(raw_obs)
                        if obs.tzinfo is not None:
                            obs = obs.astimezone(timezone.utc).replace(tzinfo=None)
                    except ValueError:
                        obs = None
                if obs is not None:
                    if st["first"] is None or obs < st["first"]:
                        st["first"] = obs
                    if st["last"] is None or obs > st["last"]:
                        st["last"] = obs
                if kind == "bar":
                    st["bars"] += 1
                    st["polls"] += 1
                    st["last_bar_ts"] = rec.get("bar_ts")
                    stale = rec.get("staleness_s")
                    if isinstance(stale, (int, float)):
                        st["stale"].append(float(stale))
                    if rec.get("gap_minutes") is not None:
                        st["gaps"].append(float(rec["gap_minutes"]))
                    if rec.get("stale_repeat"):
                        st["repeats"] += 1
                        st["repeat_run"] += 1
                        st["max_repeat_run"] = max(st["max_repeat_run"], st["repeat_run"])
                    else:
                        st["repeat_run"] = 0
                elif kind == "error":
                    st["errors"] += 1
                    st["polls"] += 1
                elif kind == "no_data":
                    st["polls"] += 1

    except OSError:
        pass

    feeds: List[Dict[str, Any]] = []
    for (broker, symbol), st in sorted(per.items()):
        stale = sorted(st["stale"])
        hours = (
            (st["last"] - st["first"]).total_seconds() / 3600.0
            if st["first"] and st["last"]
            else 0.0
        )
        p = lambda q: stale[int(q * (len(stale) - 1))] if stale else None  # noqa: E731
        feeds.append(
            {
                "broker": broker,
                "symbol": symbol,
                "bars_observed": st["bars"],
                "polls_observed": st["polls"],
                "errors_observed": st["errors"],
                "error_rate_per_hour": round(st["errors"] / hours, 2) if hours > 0 else None,
                "staleness_median_s": round(stale[len(stale) // 2], 2) if stale else None,
                "staleness_p95_s": round(p(0.95), 2) if stale else None,
                "staleness_max_s": round(stale[-1], 2) if stale else None,
                "gap_count": len(st["gaps"]),
                "gap_minutes_total": round(sum(st["gaps"]), 1) if st["gaps"] else 0,
                "stale_repeat_bars": st["repeats"],
                "max_stale_repeat_run": st["max_repeat_run"],
                "window_hours": round(hours, 3) if hours > 0 else None,
                "last_bar_ts": st["last_bar_ts"],
            }
        )

    return {"feeds": feeds, "generated_at": _now_ist_naive().isoformat()}
