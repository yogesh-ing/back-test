"""Time synchronization and market calendar management (Step 12).

The plan document specifies NYSE hours and a US holiday calendar; this repo
trades NSE through mStock, so the shipped calendar is **NSE with IST
sessions** (deviation #3 in the task tracker). The exchange registry is
data-driven (``config/calendar.yaml``) and ships an NYSE calendar too, so
"multiple exchange support" is a config entry, not a code change.

Time conventions
----------------
* All internal *now* handling is timezone-aware UTC; market-local times are
  produced by converting through :mod:`zoneinfo` (DST handled for free —
  IST has no DST, ``America/New_York`` does).
* Naive datetimes are **rejected**, same rule as the bar aligner: guessing
  a zone is how sessions silently shift by 5h30m.
* The wall clock is injectable (``clock=``) so every "what time is it"
  behaviour is testable at any frozen instant, and NTP sync applies a
  measured offset on top of it.

Session phases (NSE)
--------------------
``09:00–09:15`` pre-open auction → ``09:15–15:30`` continuous trading →
``15:40–16:00`` closing session. Boundaries are half-open ``[start, end)``:
at exactly 15:30:00 the market is **not** open.
"""

from __future__ import annotations

import logging
import socket
import statistics
import struct
import time as time_module
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

from backtest.marketdata.bars import Timeframe, align_to_boundary
from backtest.marketdata.errors import TimeSyncError

logger = logging.getLogger("backtest.marketdata.timesync")

__all__ = [
    "MarketPhase",
    "ExchangeCalendar",
    "TimeManager",
    "load_calendars",
    "DEFAULT_CALENDAR_CONFIG_PATH",
]

DEFAULT_CALENDAR_CONFIG_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "calendar.yaml"
)

_WEEKDAY_NAMES = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

#: Seconds between the NTP epoch (1900) and the Unix epoch (1970).
_NTP_UNIX_DELTA = 2_208_988_800


class MarketPhase(str, Enum):
    CLOSED = "closed"
    PRE_OPEN = "pre_open"
    OPEN = "open"
    POST_CLOSE = "post_close"

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.value


def _parse_time(value: Any, field_name: str) -> dtime:
    if isinstance(value, dtime):
        return value
    try:
        hour, minute = str(value).strip().split(":")
        return dtime(int(hour), int(minute))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{field_name} must look like '09:15', got {value!r}") from exc


def _parse_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip())


@dataclass(frozen=True)
class ExchangeCalendar:
    """Sessions, weekend and holidays for one exchange.

    ``holidays`` must be maintained annually — exchanges publish next
    year's list each December. The shipped ``config/calendar.yaml`` carries
    NSE 2025–2026 and NYSE 2026.
    """

    code: str
    timezone: str = "Asia/Kolkata"
    session_open: dtime = dtime(9, 15)
    session_close: dtime = dtime(15, 30)
    #: Optional extended windows, half-open [start, end).
    pre_open: tuple[dtime, dtime] | None = (dtime(9, 0), dtime(9, 15))
    post_close: tuple[dtime, dtime] | None = (dtime(15, 40), dtime(16, 0))
    weekend: frozenset[int] = frozenset({5, 6})  # Saturday, Sunday
    holidays: frozenset[date] = frozenset()

    def __post_init__(self) -> None:
        ZoneInfo(self.timezone)  # fail fast on a typo'd zone
        if self.session_open >= self.session_close:
            raise ValueError(
                f"{self.code}: session_open {self.session_open} must precede "
                f"session_close {self.session_close}"
            )
        if self.pre_open is not None:
            start, end = self.pre_open
            if start >= end:
                raise ValueError(f"{self.code}: pre_open window is empty or inverted")
            if end > self.session_open:
                raise ValueError(f"{self.code}: pre_open must end by session_open")
        if self.post_close is not None:
            start, end = self.post_close
            if start >= end:
                raise ValueError(f"{self.code}: post_close window is empty or inverted")
            if start < self.session_close:
                raise ValueError(f"{self.code}: post_close must start at/after session_close")
        for day in self.weekend:
            if not 0 <= day <= 6:
                raise ValueError(f"{self.code}: weekend day {day} out of range 0-6")

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def is_trading_day(self, day: date) -> bool:
        """True when the exchange trades on ``day`` (not weekend/holiday)."""
        return day.weekday() not in self.weekend and day not in self.holidays

    def phase_at(self, local: datetime) -> MarketPhase:
        """Session phase at a *market-local* aware datetime."""
        if not self.is_trading_day(local.date()):
            return MarketPhase.CLOSED
        moment = local.timetz().replace(tzinfo=None)
        if self.session_open <= moment < self.session_close:
            return MarketPhase.OPEN
        if self.pre_open is not None and self.pre_open[0] <= moment < self.pre_open[1]:
            return MarketPhase.PRE_OPEN
        if self.post_close is not None and self.post_close[0] <= moment < self.post_close[1]:
            return MarketPhase.POST_CLOSE
        return MarketPhase.CLOSED


def _build_calendar(code: str, spec: Mapping[str, Any]) -> ExchangeCalendar:
    weekend_raw = spec.get("weekend", ["saturday", "sunday"])
    weekend: set[int] = set()
    for entry in weekend_raw:
        if isinstance(entry, int):
            weekend.add(entry)
        else:
            name = str(entry).strip().lower()
            if name not in _WEEKDAY_NAMES:
                raise ValueError(f"{code}: unknown weekend day {entry!r}")
            weekend.add(_WEEKDAY_NAMES[name])

    holidays: set[date] = set()
    holidays_raw = spec.get("holidays") or {}
    if isinstance(holidays_raw, Mapping):  # keyed by year for maintainability
        entries: Iterable[Any] = (d for year in holidays_raw.values() for d in (year or []))
    else:
        entries = holidays_raw
    for entry in entries:
        holidays.add(_parse_date(entry))

    def _window(key: str) -> tuple[dtime, dtime] | None:
        value = spec.get(key)
        if value in (None, "", []):
            return None
        start, end = value
        return _parse_time(start, f"{key}[0]"), _parse_time(end, f"{key}[1]")

    return ExchangeCalendar(
        code=code,
        timezone=str(spec.get("timezone", "Asia/Kolkata")),
        session_open=_parse_time(spec.get("session_open", "09:15"), "session_open"),
        session_close=_parse_time(spec.get("session_close", "15:30"), "session_close"),
        pre_open=_window("pre_open"),
        post_close=_window("post_close"),
        weekend=frozenset(weekend),
        holidays=frozenset(holidays),
    )


def load_calendars(
    path: str | Path | None = None,
) -> tuple[str, dict[str, ExchangeCalendar]]:
    """Load ``(default_exchange, calendars)`` from ``config/calendar.yaml``."""
    import yaml

    config_path = Path(path) if path is not None else DEFAULT_CALENDAR_CONFIG_PATH
    with open(config_path, "r", encoding="utf-8") as fh:
        document = yaml.safe_load(fh) or {}

    exchanges = document.get("exchanges") or {}
    if not exchanges:
        raise ValueError(f"{config_path}: no exchanges defined")
    calendars = {
        str(code).upper(): _build_calendar(str(code).upper(), spec or {})
        for code, spec in exchanges.items()
    }
    default = str(document.get("default_exchange", next(iter(calendars)))).upper()
    if default not in calendars:
        raise ValueError(f"default_exchange {default!r} is not among {sorted(calendars)}")
    return default, calendars


def _sntp_query(server: str, timeout: float) -> float:
    """One SNTP round trip; returns the clock offset in seconds.

    Positive offset means the local clock is *behind* the server.
    """
    packet = b"\x1b" + 47 * b"\x00"  # LI=0, VN=3, Mode=3 (client)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        t0 = time_module.time()
        sock.sendto(packet, (server, 123))
        data, _ = sock.recvfrom(48)
        t1 = time_module.time()
    if len(data) < 48:
        raise ValueError(f"short NTP response ({len(data)} bytes) from {server}")
    seconds, fraction = struct.unpack("!II", data[40:48])  # transmit timestamp
    server_time = seconds - _NTP_UNIX_DELTA + fraction / 2**32
    midpoint = (t0 + t1) / 2
    return server_time - midpoint


class TimeManager:
    """Market-aware clock: sessions, holidays, alignment, NTP, latency.

    Parameters
    ----------
    calendars:
        Exchange registry. Defaults to :func:`load_calendars` on
        ``config/calendar.yaml``.
    default_exchange:
        Used when a method's ``exchange`` argument is omitted.
    clock:
        Injectable source of *aware UTC* now. Tests freeze time here.
    """

    #: Give up scanning for the next trading day beyond this horizon —
    #: a calendar where a year has no sessions is misconfigured.
    _SCAN_LIMIT_DAYS = 400

    def __init__(
        self,
        calendars: Mapping[str, ExchangeCalendar] | None = None,
        default_exchange: str | None = None,
        clock: Callable[[], datetime] | None = None,
        calendar_path: str | Path | None = None,
    ) -> None:
        if calendars is None:
            loaded_default, loaded = load_calendars(calendar_path)
            self.calendars: dict[str, ExchangeCalendar] = dict(loaded)
            self.default_exchange = (default_exchange or loaded_default).upper()
        else:
            self.calendars = {code.upper(): cal for code, cal in calendars.items()}
            if not self.calendars:
                raise ValueError("at least one calendar is required")
            self.default_exchange = (
                default_exchange or next(iter(self.calendars))
            ).upper()
        if self.default_exchange not in self.calendars:
            raise ValueError(
                f"default exchange {self.default_exchange!r} not among {sorted(self.calendars)}"
            )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.ntp_offset = timedelta(0)
        self.last_ntp_sync: datetime | None = None
        self.ntp_server: str | None = None
        self._latencies: deque[float] = deque(maxlen=1000)

    # ------------------------------------------------------------------
    # Clock
    # ------------------------------------------------------------------

    def calendar(self, exchange: str | None = None) -> ExchangeCalendar:
        code = (exchange or self.default_exchange).upper()
        try:
            return self.calendars[code]
        except KeyError:
            raise KeyError(
                f"unknown exchange {code!r}; configured: {sorted(self.calendars)}"
            ) from None

    def now_utc(self) -> datetime:
        """Aware UTC now, with the measured NTP offset applied."""
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("clock must return an aware datetime")
        return now.astimezone(timezone.utc) + self.ntp_offset

    def get_current_time(self, exchange: str | None = None) -> datetime:
        """Now in the exchange's timezone (market time)."""
        return self.now_utc().astimezone(self.calendar(exchange).tzinfo)

    def to_market(self, ts: datetime, exchange: str | None = None) -> datetime:
        """Convert an aware datetime to market-local."""
        if ts.tzinfo is None:
            raise ValueError(f"timestamp must be timezone-aware, got naive {ts!r}")
        return ts.astimezone(self.calendar(exchange).tzinfo)

    @staticmethod
    def to_utc(ts: datetime) -> datetime:
        """Convert an aware datetime to UTC."""
        if ts.tzinfo is None:
            raise ValueError(f"timestamp must be timezone-aware, got naive {ts!r}")
        return ts.astimezone(timezone.utc)

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def market_phase(
        self, at: datetime | None = None, exchange: str | None = None
    ) -> MarketPhase:
        """Session phase at ``at`` (default: now)."""
        calendar = self.calendar(exchange)
        local = (
            self.get_current_time(exchange)
            if at is None
            else self.to_market(at, exchange)
        )
        return calendar.phase_at(local)

    def is_market_open(
        self,
        exchange: str | None = None,
        at: datetime | None = None,
        include_extended: bool = False,
    ) -> bool:
        """Is the exchange trading at ``at`` (default: now)?

        ``include_extended`` also counts the pre-open and closing sessions.
        """
        phase = self.market_phase(at=at, exchange=exchange)
        if phase is MarketPhase.OPEN:
            return True
        return include_extended and phase in (MarketPhase.PRE_OPEN, MarketPhase.POST_CLOSE)

    def is_trading_day(self, day: date | datetime, exchange: str | None = None) -> bool:
        if isinstance(day, datetime):
            day = self.to_market(day, exchange).date()
        return self.calendar(exchange).is_trading_day(day)

    def _next_session_day(
        self, calendar: ExchangeCalendar, start: date, include_start: bool
    ) -> date:
        day = start if include_start else start + timedelta(days=1)
        for _ in range(self._SCAN_LIMIT_DAYS):
            if calendar.is_trading_day(day):
                return day
            day += timedelta(days=1)
        raise ValueError(
            f"no trading day within {self._SCAN_LIMIT_DAYS} days of {start} "
            f"for {calendar.code} — calendar misconfigured?"
        )

    def get_next_market_open(
        self, after: datetime | None = None, exchange: str | None = None
    ) -> datetime:
        """The next session open strictly after ``after`` (default: now).

        Returned in the exchange timezone.
        """
        calendar = self.calendar(exchange)
        local = (
            self.get_current_time(exchange)
            if after is None
            else self.to_market(after, exchange)
        )
        moment = local.timetz().replace(tzinfo=None)
        same_day = calendar.is_trading_day(local.date()) and moment < calendar.session_open
        day = self._next_session_day(calendar, local.date(), include_start=same_day)
        return datetime.combine(day, calendar.session_open, tzinfo=calendar.tzinfo)

    def get_next_market_close(
        self, after: datetime | None = None, exchange: str | None = None
    ) -> datetime:
        """The next session close strictly after ``after`` (default: now)."""
        calendar = self.calendar(exchange)
        local = (
            self.get_current_time(exchange)
            if after is None
            else self.to_market(after, exchange)
        )
        moment = local.timetz().replace(tzinfo=None)
        same_day = calendar.is_trading_day(local.date()) and moment < calendar.session_close
        day = self._next_session_day(calendar, local.date(), include_start=same_day)
        return datetime.combine(day, calendar.session_close, tzinfo=calendar.tzinfo)

    def seconds_to_open(
        self, at: datetime | None = None, exchange: str | None = None
    ) -> float:
        """Seconds until the next open (0 when the market is open now)."""
        if self.is_market_open(exchange=exchange, at=at):
            return 0.0
        reference = self.now_utc() if at is None else self.to_utc(at)
        return (self.get_next_market_open(after=at, exchange=exchange) - reference).total_seconds()

    # ------------------------------------------------------------------
    # Alignment and trading days
    # ------------------------------------------------------------------

    def align_to_timeframe(
        self,
        timestamp: datetime,
        timeframe: str | Timeframe,
        exchange: str | None = None,
    ) -> datetime:
        """Floor ``timestamp`` to the bar boundary in the exchange's clock.

        Delegates to the Step 10 aligner, anchored at the session open so
        NSE hourly bars run 09:15–10:15 like the real candles.
        """
        calendar = self.calendar(exchange)
        return align_to_boundary(
            timestamp, timeframe, tz=calendar.timezone, anchor=calendar.session_open
        )

    def get_trading_days_between(
        self,
        start: date | datetime,
        end: date | datetime,
        exchange: str | None = None,
    ) -> list[date]:
        """Trading days in ``[start, end]`` (inclusive both ends)."""
        calendar = self.calendar(exchange)
        start_day = start.date() if isinstance(start, datetime) else start
        end_day = end.date() if isinstance(end, datetime) else end
        if start_day > end_day:
            raise ValueError(f"start {start_day} is after end {end_day}")
        days: list[date] = []
        day = start_day
        while day <= end_day:
            if calendar.is_trading_day(day):
                days.append(day)
            day += timedelta(days=1)
        return days

    # ------------------------------------------------------------------
    # NTP synchronisation
    # ------------------------------------------------------------------

    def sync_with_ntp(
        self,
        server: str = "pool.ntp.org",
        timeout: float = 2.0,
        query: Callable[[str, float], float] | None = None,
    ) -> float:
        """Measure the local clock offset against an NTP server.

        Stores the offset (applied by :meth:`now_utc` from then on) and
        returns it in seconds. ``query`` is injectable for tests; the
        default does one SNTP round trip.

        Raises
        ------
        TimeSyncError
            On network failure or a malformed response. The previous
            offset is kept — a failed sync must not un-sync a good clock.
        """
        try:
            offset = (query or _sntp_query)(server, timeout)
        except TimeSyncError:
            raise
        except Exception as exc:  # socket errors, timeouts, parse failures
            raise TimeSyncError(f"NTP sync against {server} failed: {exc}") from exc
        self.ntp_offset = timedelta(seconds=offset)
        self.last_ntp_sync = self._clock().astimezone(timezone.utc)
        self.ntp_server = server
        logger.info("NTP sync against %s: offset %+.4fs", server, offset)
        return offset

    # ------------------------------------------------------------------
    # Latency tracking
    # ------------------------------------------------------------------

    def record_latency(self, seconds: float) -> None:
        """Record one feed/API round-trip latency sample."""
        if seconds < 0:
            raise ValueError(f"latency must be >= 0, got {seconds}")
        self._latencies.append(float(seconds))

    def latency_stats(self) -> dict[str, float | int | None]:
        """Rolling latency statistics over the last ≤1000 samples."""
        if not self._latencies:
            return {"count": 0, "last": None, "mean": None, "p95": None, "max": None}
        ordered = sorted(self._latencies)
        p95_index = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
        return {
            "count": len(ordered),
            "last": self._latencies[-1],
            "mean": statistics.fmean(ordered),
            "p95": ordered[p95_index],
            "max": ordered[-1],
        }
