"""Time Synchronization Manager for NSE (Step 12).

Handles market hours, holidays, timezone conversion, bar alignment, and
controllable mock time for testing.

NSE hours: 09:15-15:30 IST (Asia/Kolkata), Monday-Friday, with holiday calendar.
The plan document says NYSE 9:30-16:00 ET, but the repo trades NSE via mStock,
so NSE is the default (deviation #3 in TASK-TRACKER).

Features
--------
* Timezone handling: UTC, IST, ET, local
* DST awareness (IST has no DST, but ET does)
* Market open/close checks, next open/close, trading days
* Bar alignment to timeframe boundaries (1min, 5min, 15min, 1hr, 1day)
* Holiday calendar (NSE + NYSE, configurable)
* Mock time for testing (controllable clock)
* NTP sync placeholder + latency tracking
* Latency measurement

Example
-------
>>> from backtest.live.time_manager import TimeManager
>>> tm = TimeManager(market="NSE")
>>> tm.is_market_open()
True
>>> tm.align_to_timeframe("2024-01-02T09:17:32+05:30", "5min")
datetime(2024, 1, 2, 9, 15, tzinfo=...)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from datetime import time as dtime
from datetime import timedelta, timezone
from typing import Any, List, Optional, Union
from zoneinfo import ZoneInfo

import pandas as pd

logger = logging.getLogger("backtest.live.time_manager")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NSE_TIMEZONE = "Asia/Kolkata"
UTC_TIMEZONE = "UTC"
ET_TIMEZONE = "America/New_York"

# NSE market hours (IST)
NSE_OPEN = dtime(9, 15)
NSE_CLOSE = dtime(15, 30)
NSE_PRE_OPEN = dtime(9, 0)
NSE_POST_CLOSE = dtime(16, 0)

# NYSE hours (ET) for reference
NYSE_OPEN = dtime(9, 30)
NYSE_CLOSE = dtime(16, 0)


# ---------------------------------------------------------------------------
# Holiday calendars (minimal, expandable)
# ---------------------------------------------------------------------------

# NSE holidays 2024-2025 (partial list, for testing – a full calendar
# would use pandas_market_calendars)
NSE_HOLIDAYS_2024 = [
    date(2024, 1, 26),  # Republic Day
    date(2024, 3, 8),  # Mahashivratri
    date(2024, 3, 25),  # Holi
    date(2024, 3, 29),  # Good Friday
    date(2024, 4, 11),  # Id-Ul-Fitr
    date(2024, 4, 17),  # Ram Navami
    date(2024, 5, 1),  # Maharashtra Day
    date(2024, 6, 17),  # Bakri Id
    date(2024, 7, 17),  # Moharram
    date(2024, 8, 15),  # Independence Day
    date(2024, 10, 2),  # Gandhi Jayanti
    date(2024, 11, 1),  # Diwali
    date(2024, 11, 15),  # Gurunanak Jayanti
    date(2024, 12, 25),  # Christmas
]

NYSE_HOLIDAYS_2024 = [
    date(2024, 1, 1),  # New Year
    date(2024, 1, 15),  # MLK
    date(2024, 2, 19),  # Presidents
    date(2024, 3, 29),  # Good Friday
    date(2024, 5, 27),  # Memorial
    date(2024, 6, 19),  # Juneteenth
    date(2024, 7, 4),  # Independence
    date(2024, 9, 2),  # Labor
    date(2024, 11, 28),  # Thanksgiving
    date(2024, 12, 25),  # Christmas
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_timestamp(value: Any, tz: Optional[str] = None) -> datetime:
    """Parse many timestamp shapes into aware datetime."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            # Assume IST if market is NSE, else UTC
            tz_name = tz or NSE_TIMEZONE
            try:
                return value.replace(tzinfo=ZoneInfo(tz_name))
            except Exception:
                return value.replace(tzinfo=timezone.utc)
        return value

    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)

    try:
        dt = pd.to_datetime(value, utc=True)
        if isinstance(dt, pd.Timestamp):
            py_dt = dt.to_pydatetime()
            if tz:
                try:
                    return py_dt.astimezone(ZoneInfo(tz))
                except Exception:
                    pass
            return py_dt
        return datetime.now(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def _to_ist(dt: datetime) -> datetime:
    try:
        return dt.astimezone(ZoneInfo(NSE_TIMEZONE))
    except Exception:
        return dt


def _to_et(dt: datetime) -> datetime:
    try:
        return dt.astimezone(ZoneInfo(ET_TIMEZONE))
    except Exception:
        return dt


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# TimeManager
# ---------------------------------------------------------------------------


@dataclass
class MarketHours:
    open: dtime = NSE_OPEN
    close: dtime = NSE_CLOSE
    pre_open: dtime = NSE_PRE_OPEN
    post_close: dtime = NSE_POST_CLOSE
    timezone: str = NSE_TIMEZONE
    holidays: List[date] = field(default_factory=list)

    def __post_init__(self):
        if isinstance(self.holidays, (set, tuple)):
            self.holidays = list(self.holidays)


class TimeManager:
    """Manages market time, holidays, and bar alignment.

    Parameters
    ----------
    market:
        Market name: NSE (default), NYSE, or custom
    timezone:
        Timezone for market time (default: market's native timezone)
    holidays:
        List of holiday dates (datetime.date). If None, uses built-in list for market.
    mock_time:
        Optional fixed time for testing (controllable clock).
        If set, get_current_time() returns this.
    ntp_sync:
        Whether to sync with NTP (placeholder, logs warning if enabled but not implemented)

    Example
    -------
    >>> tm = TimeManager(market="NSE")
    >>> tm.is_market_open(datetime(2024, 1, 2, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata")))
    True
    """

    def __init__(
        self,
        market: str = "NSE",
        timezone: Optional[str] = None,
        holidays: Optional[List[date]] = None,
        mock_time: Optional[datetime] = None,
        ntp_sync: bool = False,
    ):
        self.market = str(market).strip().upper()
        self._mock_time = mock_time
        self._ntp_sync = ntp_sync
        self._latency_samples: List[float] = []

        # Configure market hours
        if self.market == "NSE":
            tz = timezone or NSE_TIMEZONE
            hols = holidays if holidays is not None else NSE_HOLIDAYS_2024
            self.hours = MarketHours(
                open=NSE_OPEN,
                close=NSE_CLOSE,
                pre_open=NSE_PRE_OPEN,
                post_close=NSE_POST_CLOSE,
                timezone=tz,
                holidays=hols,
            )
        elif self.market == "NYSE":
            tz = timezone or ET_TIMEZONE
            hols = holidays if holidays is not None else NYSE_HOLIDAYS_2024
            self.hours = MarketHours(
                open=NYSE_OPEN,
                close=NYSE_CLOSE,
                pre_open=dtime(4, 0),
                post_close=dtime(20, 0),
                timezone=tz,
                holidays=hols,
            )
        else:
            # Custom market, use NSE as fallback but allow override
            tz = timezone or NSE_TIMEZONE
            self.hours = MarketHours(
                open=NSE_OPEN,
                close=NSE_CLOSE,
                timezone=tz,
                holidays=holidays or [],
            )

        self.timezone = self.hours.timezone

        logger.info(
            "TimeManager initialized: market=%s tz=%s holidays=%s mock=%s",
            self.market,
            self.timezone,
            len(self.hours.holidays),
            bool(mock_time),
        )

        if ntp_sync:
            self.sync_with_ntp()

    # -- current time ------------------------------------------------------

    def get_current_time(self, tz: Optional[str] = None) -> datetime:
        """Return current time in market timezone (or specified tz).

        If mock_time is set, returns mock_time (controllable clock for testing).
        """
        if self._mock_time is not None:
            dt = self._mock_time
        else:
            dt = datetime.now(timezone.utc)

        target_tz = tz or self.timezone
        try:
            return dt.astimezone(ZoneInfo(target_tz))
        except Exception:
            return dt

    def set_mock_time(self, mock_time: Optional[datetime]):
        """Set controllable clock for testing. Pass None to use real time."""
        self._mock_time = mock_time
        logger.info("Mock time set to %s", mock_time)

    def advance_mock_time(self, delta: timedelta):
        """Advance mock time by delta (for testing)."""
        if self._mock_time is None:
            self._mock_time = datetime.now(timezone.utc)
        self._mock_time = self._mock_time + delta

    # -- market open/close -------------------------------------------------

    def is_market_open(self, when: Optional[datetime] = None, symbol: Optional[str] = None) -> bool:
        """Check if market is open at given time.

        Parameters
        ----------
        when:
            Time to check (default: now). Should be timezone-aware.
        symbol:
            Optional symbol for exchange-specific check (not used in placeholder)

        Returns
        -------
        bool
        """
        dt = when or self.get_current_time()
        dt = _parse_timestamp(dt, self.timezone)

        # Convert to market timezone
        try:
            local = dt.astimezone(ZoneInfo(self.hours.timezone))
        except Exception:
            local = dt

        # Weekend check
        if local.weekday() >= 5:  # Saturday=5, Sunday=6
            return False

        # Holiday check
        if local.date() in self.hours.holidays:
            return False

        # Time check
        current_time = local.time()
        # For simplicity, compare only hour/minute, ignore seconds for open/close
        open_minutes = self.hours.open.hour * 60 + self.hours.open.minute
        close_minutes = self.hours.close.hour * 60 + self.hours.close.minute
        current_minutes = current_time.hour * 60 + current_time.minute

        return open_minutes <= current_minutes <= close_minutes

    def is_pre_market(self, when: Optional[datetime] = None) -> bool:
        dt = when or self.get_current_time()
        dt = _parse_timestamp(dt, self.timezone)
        try:
            local = dt.astimezone(ZoneInfo(self.hours.timezone))
        except Exception:
            local = dt

        if local.weekday() >= 5 or local.date() in self.hours.holidays:
            return False

        pre_minutes = self.hours.pre_open.hour * 60 + self.hours.pre_open.minute
        open_minutes = self.hours.open.hour * 60 + self.hours.open.minute
        curr_minutes = local.time().hour * 60 + local.time().minute

        return pre_minutes <= curr_minutes < open_minutes

    def is_after_hours(self, when: Optional[datetime] = None) -> bool:
        dt = when or self.get_current_time()
        dt = _parse_timestamp(dt, self.timezone)
        try:
            local = dt.astimezone(ZoneInfo(self.hours.timezone))
        except Exception:
            local = dt

        if local.weekday() >= 5 or local.date() in self.hours.holidays:
            return False

        close_minutes = self.hours.close.hour * 60 + self.hours.close.minute
        post_minutes = self.hours.post_close.hour * 60 + self.hours.post_close.minute
        curr_minutes = local.time().hour * 60 + local.time().minute

        return close_minutes < curr_minutes <= post_minutes

    def get_next_market_open(self, from_time: Optional[datetime] = None) -> datetime:
        """Get next market open time."""
        dt = from_time or self.get_current_time()
        dt = _parse_timestamp(dt, self.timezone)

        try:
            local = dt.astimezone(ZoneInfo(self.hours.timezone))
        except Exception:
            local = dt

        # Start from next minute
        candidate = local + timedelta(minutes=1)
        # If already open and candidate is still today open hours, return candidate if it's at open?
        # Simpler: iterate day by day

        for _ in range(365):  # max 1 year search
            # If weekend, jump to Monday
            if candidate.weekday() >= 5:
                # Days to Monday
                days_ahead = 7 - candidate.weekday()
                candidate = candidate + timedelta(days=days_ahead)
                candidate = candidate.replace(
                    hour=self.hours.open.hour,
                    minute=self.hours.open.minute,
                    second=0,
                    microsecond=0,
                )
                continue

            if candidate.date() in self.hours.holidays:
                candidate = candidate + timedelta(days=1)
                candidate = candidate.replace(
                    hour=self.hours.open.hour,
                    minute=self.hours.open.minute,
                    second=0,
                    microsecond=0,
                )
                continue

            open_minutes = self.hours.open.hour * 60 + self.hours.open.minute
            close_minutes = self.hours.close.hour * 60 + self.hours.close.minute
            curr_minutes = candidate.time().hour * 60 + candidate.time().minute

            if curr_minutes <= open_minutes:
                # Before or at open – today open is next
                candidate = candidate.replace(
                    hour=self.hours.open.hour,
                    minute=self.hours.open.minute,
                    second=0,
                    microsecond=0,
                )
                return candidate
            elif curr_minutes <= close_minutes:
                # Currently open, next open is tomorrow
                candidate = candidate + timedelta(days=1)
                candidate = candidate.replace(
                    hour=self.hours.open.hour,
                    minute=self.hours.open.minute,
                    second=0,
                    microsecond=0,
                )
                continue
            else:
                # After close, next day open
                candidate = candidate + timedelta(days=1)
                candidate = candidate.replace(
                    hour=self.hours.open.hour,
                    minute=self.hours.open.minute,
                    second=0,
                    microsecond=0,
                )
                continue

        raise ValueError("Could not find next market open within 1 year")

    def get_next_market_close(self, from_time: Optional[datetime] = None) -> datetime:
        """Get next market close time."""
        dt = from_time or self.get_current_time()
        dt = _parse_timestamp(dt, self.timezone)

        try:
            local = dt.astimezone(ZoneInfo(self.hours.timezone))
        except Exception:
            local = dt

        candidate = local

        for _ in range(365):
            if candidate.weekday() >= 5 or candidate.date() in self.hours.holidays:
                candidate = candidate + timedelta(days=1)
                candidate = candidate.replace(
                    hour=self.hours.close.hour,
                    minute=self.hours.close.minute,
                    second=0,
                    microsecond=0,
                )
                continue

            open_minutes = self.hours.open.hour * 60 + self.hours.open.minute
            close_minutes = self.hours.close.hour * 60 + self.hours.close.minute
            curr_minutes = candidate.time().hour * 60 + candidate.time().minute

            if curr_minutes < open_minutes:
                # Before open, close is today
                candidate = candidate.replace(
                    hour=self.hours.close.hour,
                    minute=self.hours.close.minute,
                    second=0,
                    microsecond=0,
                )
                return candidate
            elif curr_minutes <= close_minutes:
                # Currently open, close today
                candidate = candidate.replace(
                    hour=self.hours.close.hour,
                    minute=self.hours.close.minute,
                    second=0,
                    microsecond=0,
                )
                return candidate
            else:
                # After close, next day close
                candidate = candidate + timedelta(days=1)
                candidate = candidate.replace(
                    hour=self.hours.close.hour,
                    minute=self.hours.close.minute,
                    second=0,
                    microsecond=0,
                )
                continue

        raise ValueError("Could not find next market close within 1 year")

    def get_trading_days_between(
        self, start: Union[str, datetime, date], end: Union[str, datetime, date]
    ) -> List[date]:
        """Get list of trading days between start and end inclusive."""
        start_date = _parse_timestamp(start).date() if not isinstance(start, date) else start
        end_date = _parse_timestamp(end).date() if not isinstance(end, date) else end

        if isinstance(start, date) and not isinstance(start, datetime):
            start_date = start
        if isinstance(end, date) and not isinstance(end, datetime):
            end_date = end

        trading_days = []
        current = start_date

        while current <= end_date:
            if current.weekday() < 5 and current not in self.hours.holidays:
                trading_days.append(current)
            current += timedelta(days=1)

        return trading_days

    # -- bar alignment -----------------------------------------------------

    def align_to_timeframe(self, timestamp: Any, timeframe: str) -> datetime:
        """Align timestamp to timeframe boundary (floor).

        Parameters
        ----------
        timestamp:
            Time to align
        timeframe:
            One of: 1min, 3min, 5min, 15min, 30min, 60min, 1hour, day, etc.

        Returns
        -------
        datetime
            Aligned timestamp in market timezone
        """
        dt = _parse_timestamp(timestamp, self.timezone)

        try:
            local = dt.astimezone(ZoneInfo(self.hours.timezone))
        except Exception:
            local = dt

        tf = str(timeframe).strip().lower()

        # Parse timeframe
        if tf in ("1min", "1m", "1"):
            # Floor to minute
            aligned = local.replace(second=0, microsecond=0)
        elif tf in ("3min", "3m"):
            minute = (local.minute // 3) * 3
            aligned = local.replace(minute=minute, second=0, microsecond=0)
        elif tf in ("5min", "5m"):
            minute = (local.minute // 5) * 5
            aligned = local.replace(minute=minute, second=0, microsecond=0)
        elif tf in ("15min", "15m"):
            minute = (local.minute // 15) * 15
            aligned = local.replace(minute=minute, second=0, microsecond=0)
        elif tf in ("30min", "30m"):
            minute = (local.minute // 30) * 30
            aligned = local.replace(minute=minute, second=0, microsecond=0)
        elif tf in ("60min", "60m", "1hour", "1h", "1hr"):
            aligned = local.replace(minute=0, second=0, microsecond=0)
        elif tf in ("day", "1day", "1d", "d"):
            aligned = local.replace(hour=0, minute=0, second=0, microsecond=0)
        elif tf in ("week", "1week", "1w"):
            # Monday as start of week
            days_since_monday = local.weekday()
            aligned = (local - timedelta(days=days_since_monday)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        elif tf in ("month", "1month", "1mo", "m"):
            aligned = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            # Try to parse numeric minutes: e.g. "10min"
            try:
                if "min" in tf:
                    mins = int(tf.replace("min", "").strip())
                    minute = (local.minute // mins) * mins
                    aligned = local.replace(minute=minute, second=0, microsecond=0)
                else:
                    aligned = local.replace(second=0, microsecond=0)
            except Exception:
                aligned = local.replace(second=0, microsecond=0)

        return aligned

    def is_bar_closed(
        self, bar_timestamp: Any, timeframe: str, current_time: Optional[datetime] = None
    ) -> bool:
        """Check if a bar is closed (i.e., current time is past bar's close).

        For example, a 5min bar starting at 09:15 is closed at 09:20.
        """
        bar_start = self.align_to_timeframe(bar_timestamp, timeframe)
        now = current_time or self.get_current_time()

        # Calculate bar end
        tf = str(timeframe).strip().lower()
        if tf in ("1min", "1m"):
            bar_end = bar_start + timedelta(minutes=1)
        elif tf in ("3min", "3m"):
            bar_end = bar_start + timedelta(minutes=3)
        elif tf in ("5min", "5m"):
            bar_end = bar_start + timedelta(minutes=5)
        elif tf in ("15min", "15m"):
            bar_end = bar_start + timedelta(minutes=15)
        elif tf in ("30min", "30m"):
            bar_end = bar_start + timedelta(minutes=30)
        elif tf in ("60min", "60m", "1hour", "1h"):
            bar_end = bar_start + timedelta(hours=1)
        elif tf in ("day", "1day"):
            bar_end = bar_start + timedelta(days=1)
        else:
            bar_end = bar_start + timedelta(minutes=1)

        return now >= bar_end

    # -- NTP and latency ---------------------------------------------------

    def sync_with_ntp(self, ntp_server: str = "pool.ntp.org") -> bool:
        """Sync with NTP server (placeholder).

        In production, would use ntplib to sync. For now, logs and returns True.
        """
        logger.info("NTP sync requested (server=%s) – placeholder, using system time", ntp_server)
        # Placeholder: in real implementation, use ntplib
        # import ntplib
        # client = ntplib.NTPClient()
        # response = client.request(ntp_server)
        # etc.
        return True

    def measure_latency(self, sample_ms: float):
        """Record a latency sample for monitoring."""
        self._latency_samples.append(float(sample_ms))
        # Keep last 1000 samples
        if len(self._latency_samples) > 1000:
            self._latency_samples = self._latency_samples[-1000:]

    def get_latency_stats(self) -> dict:
        if not self._latency_samples:
            return {"count": 0, "mean_ms": 0, "p95_ms": 0}
        import statistics

        sorted_samples = sorted(self._latency_samples)
        mean = statistics.mean(sorted_samples)
        # p95
        idx = int(len(sorted_samples) * 0.95)
        p95 = sorted_samples[min(idx, len(sorted_samples) - 1)]

        return {
            "count": len(sorted_samples),
            "mean_ms": round(mean, 2),
            "p95_ms": round(p95, 2),
            "min_ms": round(min(sorted_samples), 2),
            "max_ms": round(max(sorted_samples), 2),
        }

    # -- helpers -----------------------------------------------------------

    def to_market_time(self, dt: Any) -> datetime:
        """Convert any timestamp to market timezone."""
        parsed = _parse_timestamp(dt, self.timezone)
        try:
            return parsed.astimezone(ZoneInfo(self.hours.timezone))
        except Exception:
            return parsed

    def to_utc(self, dt: Any) -> datetime:
        """Convert any timestamp to UTC."""
        parsed = _parse_timestamp(dt, self.timezone)
        return _to_utc(parsed)

    def __repr__(self):
        return f"<TimeManager market={self.market} tz={self.timezone} mock={bool(self._mock_time)}>"
