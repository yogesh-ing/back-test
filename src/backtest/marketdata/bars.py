"""Timeframe alignment and tick→bar aggregation (Step 10).

Boundary convention
-------------------
A bar's ``ts`` is its **open** time, floored to the timeframe boundary in the
*exchange* timezone (IST for NSE). Flooring in UTC would be wrong for any
timeframe the UTC offset does not divide: IST is +05:30, so UTC-floored
hourly bars would open at 30 minutes past the local hour.

Session anchor
--------------
NSE trades 09:15–15:30, and real NSE hourly candles run 09:15–10:15, not
09:00–10:00. With ``anchor=time(9, 15)`` boundaries are floored relative to
the session open instead of midnight. Ticks before the anchor (pre-open)
fall back to midnight-based flooring.

Gaps and late data
------------------
* A tick that lands past the current bar's window closes that bar. If it
  skipped whole periods, the gap is counted; with ``fill_gaps=True`` the
  aggregator emits flat synthetic bars (``synthetic=True``, volume 0, OHLC
  pinned to the previous close) for up to ``max_gap_bars`` missing periods.
  Synthetic bars are never persisted to the database.
* A tick *older* than the current window is late. Within
  ``late_grace_seconds`` it still contributes volume/extremes (never the
  close); older than that it is dropped and counted.
"""

from __future__ import annotations

import calendar
import logging
from datetime import datetime, time as dtime, timedelta
from enum import Enum
from typing import Any, Callable, Sequence
from zoneinfo import ZoneInfo

from backtest.marketdata.ticks import Bar, Tick

logger = logging.getLogger("backtest.marketdata.bars")

__all__ = [
    "Timeframe",
    "INTRADAY_MINUTES",
    "align_to_boundary",
    "next_boundary",
    "BarAggregator",
    "AggregatorStats",
]


class Timeframe(str, Enum):
    """Bar timeframes. Values match ``ck_mdc_timeframe`` in the schema."""

    M1 = "1min"
    M3 = "3min"
    M5 = "5min"
    M15 = "15min"
    M30 = "30min"
    M60 = "60min"
    H1 = "1hour"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.value


#: Minutes per intraday timeframe. ``1hour`` is an alias of ``60min``.
INTRADAY_MINUTES: dict[str, int] = {
    Timeframe.M1.value: 1,
    Timeframe.M3.value: 3,
    Timeframe.M5.value: 5,
    Timeframe.M15.value: 15,
    Timeframe.M30.value: 30,
    Timeframe.M60.value: 60,
    Timeframe.H1.value: 60,
}

_VALID_TIMEFRAMES = {tf.value for tf in Timeframe}


def _coerce_timeframe(timeframe: str | Timeframe) -> str:
    value = timeframe.value if isinstance(timeframe, Timeframe) else str(timeframe)
    if value not in _VALID_TIMEFRAMES:
        raise ValueError(
            f"unknown timeframe {value!r}; expected one of {sorted(_VALID_TIMEFRAMES)}"
        )
    return value


def align_to_boundary(
    ts: datetime,
    timeframe: str | Timeframe,
    tz: str = "Asia/Kolkata",
    anchor: dtime | None = None,
) -> datetime:
    """Floor ``ts`` to the open time of the bar containing it.

    Returns an aware datetime in the exchange timezone ``tz``.

    Parameters
    ----------
    ts:
        Timezone-aware instant. Naive input raises — guessing a zone here
        is how bars silently shift by 5h30m.
    timeframe:
        One of :class:`Timeframe`.
    tz:
        Exchange timezone in which boundaries are meaningful.
    anchor:
        Optional session-open anchor for intraday flooring (NSE: 09:15).
        Ignored for day/week/month.
    """
    if ts.tzinfo is None:
        raise ValueError(f"timestamp must be timezone-aware, got naive {ts!r}")
    value = _coerce_timeframe(timeframe)

    local = ts.astimezone(ZoneInfo(tz))
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)

    if value in INTRADAY_MINUTES:
        size = INTRADAY_MINUTES[value]
        total_minutes = int((local - midnight).total_seconds() // 60)
        if anchor is not None:
            offset = anchor.hour * 60 + anchor.minute
            if total_minutes >= offset:
                floored = ((total_minutes - offset) // size) * size + offset
            else:  # pre-open ticks fall back to midnight-based flooring
                floored = (total_minutes // size) * size
        else:
            floored = (total_minutes // size) * size
        return midnight + timedelta(minutes=floored)

    if value == Timeframe.DAY.value:
        return midnight
    if value == Timeframe.WEEK.value:
        return midnight - timedelta(days=midnight.weekday())  # Monday
    if value == Timeframe.MONTH.value:
        return midnight.replace(day=1)
    raise ValueError(f"unhandled timeframe {value!r}")  # pragma: no cover


def next_boundary(
    start: datetime,
    timeframe: str | Timeframe,
    tz: str = "Asia/Kolkata",
    anchor: dtime | None = None,
) -> datetime:
    """The open time of the bar immediately after the one opening at ``start``."""
    value = _coerce_timeframe(timeframe)
    if value in INTRADAY_MINUTES:
        size = INTRADAY_MINUTES[value]
        candidate = start + timedelta(minutes=size)
        return align_to_boundary(candidate, value, tz=tz, anchor=anchor)
    local = start.astimezone(ZoneInfo(tz))
    if value == Timeframe.DAY.value:
        return align_to_boundary(local + timedelta(days=1), value, tz=tz)
    if value == Timeframe.WEEK.value:
        return align_to_boundary(local + timedelta(days=7), value, tz=tz)
    if value == Timeframe.MONTH.value:
        days = calendar.monthrange(local.year, local.month)[1]
        return align_to_boundary(local.replace(day=1) + timedelta(days=days), value, tz=tz)
    raise ValueError(f"unhandled timeframe {value!r}")  # pragma: no cover


class AggregatorStats:
    """Counters the aggregator maintains; surfaced by the handler."""

    __slots__ = ("late_applied", "late_dropped", "gaps_detected", "synthetic_bars", "bars_closed")

    def __init__(self) -> None:
        self.late_applied = 0
        self.late_dropped = 0
        self.gaps_detected = 0
        self.synthetic_bars = 0
        self.bars_closed = 0

    def to_dict(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in self.__slots__}


class BarAggregator:
    """Builds aligned OHLCV bars from a stream of normalized ticks.

    One aggregator serves any number of symbols and timeframes
    simultaneously; state is keyed by ``(symbol, timeframe)``.

    Parameters
    ----------
    timeframes:
        Timeframes to build (e.g. ``["1min", "5min", "day"]``).
    tz:
        Exchange timezone for boundary alignment.
    anchor:
        Optional intraday session anchor (NSE: ``time(9, 15)``).
    fill_gaps:
        Emit flat synthetic bars for empty periods inside a gap.
    max_gap_bars:
        Gaps wider than this many periods are logged but not filled —
        an overnight gap on 1-minute bars is ~1,000 periods of noise.
    late_grace_seconds:
        Late ticks within this age are folded into the current bar
        (volume/extremes only); older ones are dropped.
    """

    def __init__(
        self,
        timeframes: Sequence[str | Timeframe],
        tz: str = "Asia/Kolkata",
        anchor: dtime | None = None,
        fill_gaps: bool = False,
        max_gap_bars: int = 16,
        late_grace_seconds: int = 60,
    ) -> None:
        if not timeframes:
            raise ValueError("at least one timeframe is required")
        self.timeframes = tuple(dict.fromkeys(_coerce_timeframe(tf) for tf in timeframes))
        ZoneInfo(tz)  # validate eagerly; a typo should fail at construction
        self.tz = tz
        self.anchor = anchor
        self.fill_gaps = bool(fill_gaps)
        if max_gap_bars < 0:
            raise ValueError("max_gap_bars must be >= 0")
        self.max_gap_bars = int(max_gap_bars)
        if late_grace_seconds < 0:
            raise ValueError("late_grace_seconds must be >= 0")
        self.late_grace_seconds = int(late_grace_seconds)
        self.stats = AggregatorStats()
        self._current: dict[tuple[str, str], Bar] = {}
        self._bar_callbacks: list[Callable[[Bar], None]] = []

    # -- observers ----------------------------------------------------------

    def on_bar_closed(self, callback: Callable[[Bar], None]) -> Callable[[Bar], None]:
        """Register a callback fired once per closed bar. Returns it for removal."""
        if not callable(callback):
            raise ValueError("callback must be callable")
        self._bar_callbacks.append(callback)
        return callback

    def remove_bar_callback(self, callback: Callable[[Bar], None]) -> None:
        try:
            self._bar_callbacks.remove(callback)
        except ValueError:
            pass

    def _emit(self, bar: Bar) -> None:
        bar.complete = True
        self.stats.bars_closed += 1
        for callback in list(self._bar_callbacks):
            try:  # a broken observer must not break aggregation
                callback(bar)
            except Exception:  # noqa: BLE001
                logger.exception("bar-closed callback %r failed for %s", callback, bar)

    # -- aggregation --------------------------------------------------------

    def current_bar(self, symbol: str, timeframe: str | Timeframe) -> Bar | None:
        """The in-progress (incomplete) bar, or None."""
        return self._current.get((symbol.upper(), _coerce_timeframe(timeframe)))

    def add_tick(self, tick: Tick) -> list[Bar]:
        """Fold one tick into every configured timeframe.

        Returns the bars *closed* by this tick (possibly including
        synthetic gap-fill bars), in chronological order per timeframe.
        """
        closed: list[Bar] = []
        for timeframe in self.timeframes:
            closed.extend(self._add_to_timeframe(tick, timeframe))
        return closed

    def _add_to_timeframe(self, tick: Tick, timeframe: str) -> list[Bar]:
        key = (tick.symbol, timeframe)
        boundary = align_to_boundary(tick.timestamp, timeframe, tz=self.tz, anchor=self.anchor)
        current = self._current.get(key)

        if current is None:
            self._current[key] = Bar.from_tick(tick, timeframe, boundary)
            return []

        if boundary == current.ts:
            current.update(tick)
            return []

        if boundary < current.ts:  # late tick
            age = (current.ts - tick.timestamp).total_seconds()
            if age <= self.late_grace_seconds:
                current.update_late(tick)
                self.stats.late_applied += 1
            else:
                self.stats.late_dropped += 1
                logger.warning(
                    "dropped late tick for %s %s: %.0fs before current bar %s",
                    tick.symbol, timeframe, age, current.ts,
                )
            return []

        # boundary > current.ts — the current bar is done.
        closed: list[Bar] = []
        self._emit(current)
        closed.append(current)
        closed.extend(self._fill_gap(current, boundary, timeframe))
        self._current[key] = Bar.from_tick(tick, timeframe, boundary)
        return closed

    def _fill_gap(self, previous: Bar, boundary: datetime, timeframe: str) -> list[Bar]:
        """Synthetic flat bars for periods skipped between two real bars."""
        expected = next_boundary(previous.ts, timeframe, tz=self.tz, anchor=self.anchor)
        if expected >= boundary:
            return []  # adjacent periods, no gap

        # Count missing periods first so a huge gap costs nothing to skip.
        missing = 0
        cursor = expected
        while cursor < boundary:
            missing += 1
            if missing > self.max_gap_bars:
                break
            cursor = next_boundary(cursor, timeframe, tz=self.tz, anchor=self.anchor)

        self.stats.gaps_detected += 1
        if not self.fill_gaps:
            logger.info(
                "gap of >=%d %s period(s) for %s after %s (fill_gaps off)",
                missing, timeframe, previous.symbol, previous.ts,
            )
            return []
        if missing > self.max_gap_bars:
            logger.warning(
                "gap of >%d %s periods for %s after %s exceeds max_gap_bars=%d; not filling",
                self.max_gap_bars, timeframe, previous.symbol, previous.ts, self.max_gap_bars,
            )
            return []

        synthetic: list[Bar] = []
        cursor = expected
        while cursor < boundary:
            bar = Bar(
                symbol=previous.symbol,
                timeframe=timeframe,
                ts=cursor,
                open=previous.close,
                high=previous.close,
                low=previous.close,
                close=previous.close,
                volume=0,
                tick_count=0,
                synthetic=True,
                source=previous.source,
            )
            self.stats.synthetic_bars += 1
            self._emit(bar)
            synthetic.append(bar)
            cursor = next_boundary(cursor, timeframe, tz=self.tz, anchor=self.anchor)
        return synthetic

    def force_close(
        self,
        symbol: str | None = None,
        timeframe: str | Timeframe | None = None,
    ) -> list[Bar]:
        """Close in-progress bars now (end of session / shutdown flush).

        Filters by ``symbol`` and/or ``timeframe`` when given; closes
        everything otherwise. Returns the closed bars.
        """
        wanted_tf = _coerce_timeframe(timeframe) if timeframe is not None else None
        wanted_sym = symbol.upper() if symbol is not None else None
        closed: list[Bar] = []
        for key in sorted(self._current):
            sym, tf = key
            if wanted_sym is not None and sym != wanted_sym:
                continue
            if wanted_tf is not None and tf != wanted_tf:
                continue
            bar = self._current.pop(key)
            self._emit(bar)
            closed.append(bar)
        return closed
