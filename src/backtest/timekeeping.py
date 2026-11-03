"""One clock, two faces: instants are aware UTC, exchange time is IST.

Written after the 2026-10-27 trade-record incident, where every option trade
row carried an ``entry_ts`` stamped in UTC and an ``exit_ts`` stamped in IST.
``trades.holding_period_minutes`` read 330 minutes for a one-bar scalp — the
5h30 offset — and the EOD square-off gate never fired because it compared a
UTC clock against an IST wall time. Neither bug was a math error. Both came
from a naive datetime whose clock nobody had declared, and from eight modules
each defining their own ``IST`` constant.

THE CONTRACT
------------
1. **Durability** — every datetime crossing a database or state-file boundary
   is timezone-*aware* UTC. Postgres columns are ``TIMESTAMPTZ``, so an aware
   value stores an unambiguous instant no matter what ``SHOW timezone`` says;
   a naive value is handed to the server's session timezone
   (``Asia/Calcutta`` on this deployment) and silently reinterpreted, which is
   exactly how the rows got corrupted.
2. **Presentation and calendar arithmetic** — always IST, the exchange clock.
   A trading day is 09:15–15:30 IST, so a session date must never be derived
   from a UTC calendar date.
3. **A naive datetime is an ambiguous one.** It is refused at the boundaries
   (:func:`ensure_utc`, :func:`parse_utc_iso`) rather than guessed. The single
   place that accepts one is :func:`from_market_clock`, which is the declared
   home of the convention "market feeds hand out naive IST".

Why a fixed +05:30 offset instead of ``ZoneInfo("Asia/Kolkata")``: India has
observed no daylight saving since 1962, so the offset is exact, it costs
nothing to construct on a hot path, and it does not depend on the ``tzdata``
package being present on a Windows host.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Optional, Union

UTC = timezone.utc
EXCHANGE_TZ = timezone(timedelta(hours=5, minutes=30), name="IST")

#: NSE regular-session bounds, in exchange time.
MARKET_OPEN_TIME = time(9, 15)
MARKET_CLOSE_TIME = time(15, 30)
#: Last minute a *positional* book may still open a new position. The 15:25–15:30
#: auction is settlement, not a place to build a risk you then cannot exit.
ENTRY_CUTOFF_TIME = time(15, 25)
#: First minute of the end-of-day square-off sweep. 20 minutes clear of the close
#: so a runner flattens itself before broker RMS auto-squareoff (~15:15–15:20).
EOD_SQUAREOFF_WINDOW_START = time(15, 10)


class AmbiguousTimeError(ValueError):
    """A naive datetime reached a boundary that requires a declared clock."""


def now_utc() -> datetime:
    """The canonical 'now': tz-aware UTC.

    ``datetime.utcnow()`` returns a *naive* UTC value, which is the form this
    module exists to eliminate — it is indistinguishable from a naive IST value
    the moment it reaches ``TIMESTAMPTZ``.
    """
    return datetime.now(UTC)


def now_exchange() -> datetime:
    """The canonical 'now' in exchange time (aware, +05:30)."""
    return datetime.now(EXCHANGE_TZ)


def ensure_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Coerce an aware datetime to aware UTC; refuse a naive one.

    Use this for values whose clock is already known to be UTC — internal
    bookkeeping, broker ticks converted upstream, restored state.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        raise AmbiguousTimeError(
            f"naive datetime {value.isoformat()} reached ensure_utc(); its clock "
            "is undeclared. Use from_market_clock() for exchange timestamps, or "
            "now_utc() for wall clock."
        )
    return value.astimezone(UTC)


def ensure_exchange(value: Optional[datetime]) -> Optional[datetime]:
    """Coerce an aware datetime to aware IST; refuse a naive one."""
    if value is None:
        return None
    if value.tzinfo is None:
        raise AmbiguousTimeError(f"naive datetime {value.isoformat()} reached ensure_exchange()")
    return value.astimezone(EXCHANGE_TZ)


def from_market_clock(value: Union[datetime, str, int, float, None]) -> Optional[datetime]:
    """The one door for timestamps produced by a market feed.

    Feeds hand out *exchange* time, usually naive: mStock formats the forming
    bar with ``strftime("%Y-%m-%d %H:%M:%S")`` after adding 5:30 to UTC, and
    historical candles arrive as naive IST index values. So a naive input here
    means IST, and the result is the true instant in aware UTC.

    Aware inputs are converted like any other. Epoch seconds/milliseconds are
    treated as absolute instants (they carry no timezone).
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _from_epoch(value)
    if isinstance(value, str):
        parsed = _parse(value)
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise ValueError(f"unsupported timestamp type {type(value).__name__}")
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC)
    return parsed.replace(tzinfo=EXCHANGE_TZ).astimezone(UTC)


def from_stored_utc(value: Union[datetime, str, None]) -> Optional[datetime]:
    """Read a timestamp this application wrote before the clock fix.

    Until 2026-10-27 the trade writers used ``datetime.utcnow()``, so every
    naive value they left behind — in ``portfolio_state.json``, in the
    ``trade_structures.legs`` JSONB, in old JSONL logs — is UTC. Values written
    after the fix carry an explicit offset and pass through untouched.

    This is the counterpart to :func:`from_market_clock`, which assumes the
    opposite ("naive means exchange time") because its inputs come from a
    *feed* rather than from our own writers. A caller must pick one door
    deliberately; silently splitting the difference on ``replace(tzinfo=None)``
    is what corrupted the trade records.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = _parse(str(value))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def to_exchange(value: Optional[datetime]) -> Optional[datetime]:
    """Render a stored instant for a human: aware IST. Never a naive value."""
    return ensure_exchange(value)


def session_date(value: Union[datetime, str, None]) -> Optional[date]:
    """The trading-calendar date of an instant (the IST date, not the UTC one).

    The UTC date is wrong on both ends of the session: a 09:15 IST bar is
    03:45 UTC of the same day, but a 15:30 IST settlement lands in UTC too, and
    anything stamped 00:00–05:30 IST belongs to the previous UTC day.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _as_exchange(value).date()
    if isinstance(value, date):
        return value
    parsed = from_market_clock(value)
    return parsed.astimezone(EXCHANGE_TZ).date() if parsed is not None else None


def current_session_date(value: Optional[datetime] = None) -> date:
    """Today's exchange-calendar date in IST (00:00 IST roll)."""
    return _as_exchange(value if value is not None else now_utc()).date()


def _coerce_date(value: Union[date, str]) -> date:
    if isinstance(value, datetime):
        return _as_exchange(value).date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip()[:10])


def ist_session_bounds_utc(
    start_date: Union[date, str],
    end_date: Union[date, str, None] = None,
) -> tuple[datetime, datetime]:
    """Half-open aware-UTC bounds ``[start_utc, end_utc)`` for IST session day(s).

    ``start_date`` and ``end_date`` (inclusive; defaults to ``start_date``) name
    IST calendar dates. Converting the ``00:00:00+05:30`` walls to aware UTC in
    Python lets every ``TIMESTAMPTZ`` range scan use ``ix_trades_portfolio_exit``
    directly without depending on PostgreSQL's session timezone or SQLite string
    casts.
    """
    d0 = _coerce_date(start_date)
    d1 = _coerce_date(end_date) if end_date is not None else d0
    if d1 < d0:
        raise ValueError(f"end_date {d1.isoformat()} precedes start_date {d0.isoformat()}")
    start_utc = datetime.combine(d0, time.min, tzinfo=EXCHANGE_TZ).astimezone(UTC)
    end_utc = datetime.combine(
        d1 + timedelta(days=1), time.min, tzinfo=EXCHANGE_TZ
    ).astimezone(UTC)
    return start_utc, end_utc


def format_session_scope(
    target_date: Union[date, str, None] = None,
    *,
    reference_now: Optional[datetime] = None,
) -> str:
    """Human scope badge for P&L tiles (E1: e.g. ``'Today · 30-Oct IST'``)."""
    today_ist = current_session_date(reference_now)
    day = _coerce_date(target_date) if target_date is not None else today_ist
    if day == today_ist:
        return f"Today · {day:%d-%b} IST"
    if day.year == today_ist.year:
        return f"{day:%d-%b} IST"
    return f"{day:%d-%b-%Y} IST"


def iso_utc(value: Optional[datetime]) -> Optional[str]:
    """Serialize an instant for storage or JSON: aware UTC, explicit offset.

    The explicit offset is what makes the browser correct by construction —
    ``new Date("2026-10-27T09:08:30")`` is parsed as *local* time while
    ``new Date("2026-10-27T09:08:30+00:00")`` is parsed as an instant, so a
    naive ISO string silently becomes whatever zone the viewer happens to be in
    and every client-side ``+5.5h`` correction then double-shifts.
    """
    aware = ensure_utc(value)
    return aware.isoformat() if aware is not None else None


def iso_exchange(value: Optional[datetime]) -> Optional[str]:
    """Serialize for display in exchange time, offset included."""
    aware = ensure_exchange(value)
    return aware.isoformat() if aware is not None else None


def parse_utc_iso(value: Union[str, datetime, None]) -> Optional[datetime]:
    """Parse a string written by :func:`iso_utc`; refuses a naive string.

    Refusing is deliberate: ``"2026-10-27 14:39:00"`` is the exact shape that
    produced the mixed trade rows. Read old rows through
    :func:`from_market_clock` instead, which declares what it assumes.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    parsed = _parse(str(value))
    if parsed.tzinfo is None:
        raise AmbiguousTimeError(
            f"naive ISO timestamp {value!r} has no declared clock; use "
            "from_market_clock() for legacy exchange-time rows"
        )
    return parsed.astimezone(UTC)


def duration_seconds(
    start: Union[datetime, str, None], end: Union[datetime, str, None]
) -> Optional[float]:
    """Elapsed whole-clock seconds between two instants, or ``None``.

    Both sides go through :func:`from_market_clock`, so a UTC entry and an IST
    exit stop being 5h30 apart and become the two instants they are.
    """
    a = from_market_clock(start)
    b = from_market_clock(end)
    if a is None or b is None:
        return None
    return (b - a).total_seconds()


def market_is_open(value: Optional[datetime] = None) -> bool:
    """Whether ``value`` (default: now) falls inside the regular NSE session."""
    reference = _as_exchange(value if value is not None else now_utc())
    return MARKET_OPEN_TIME <= reference.time() <= MARKET_CLOSE_TIME


def in_eod_squareoff_window(value: Optional[datetime] = None) -> bool:
    """Whether ``value`` (default: now) sits in the 15:10:00–15:29:59 IST sweep.

    The window an intraday book must be flat inside (BUG-39). It starts at the
    20-minute square-off floor and stops at the close, so a sweep can never
    square off after the session it belongs to has ended.
    """
    reference = _as_exchange(value if value is not None else now_utc())
    return EOD_SQUAREOFF_WINDOW_START <= reference.time() < MARKET_CLOSE_TIME


def entry_window_refusal(
    value: Optional[datetime] = None,
    *,
    intraday: bool = False,
    squareoff_minutes: Optional[int] = None,
) -> Optional[str]:
    """Why ``value`` may NOT open a new position — ``None`` means it may.

    BUG-18 (QA 2026-10-28): a bar stamped 15:30:30 IST opened a fresh structure
    after the market had closed, so the paper book traded against a session that
    was over. The exchange clock, not the wall clock, decides: a position opened
    at HH:MM has to be exitable in the same session.

    ``intraday`` + ``squareoff_minutes`` narrow the deadline to the runner's own
    EOD square-off (15:30 − N), so a structure cannot be opened *inside* the
    window where the book is already flattening itself.
    """
    reference = _as_exchange(value if value is not None else now_utc())
    moment = reference.time()
    if moment < MARKET_OPEN_TIME:
        return (
            f"{reference:%H:%M:%S} IST is before the 09:15:00 open — "
            "no entry outside market hours"
        )
    if moment > MARKET_CLOSE_TIME:
        return (
            f"{reference:%H:%M:%S} IST is after the 15:30:00 close — "
            "no entry outside market hours"
        )
    deadline = ENTRY_CUTOFF_TIME
    if intraday and squareoff_minutes:
        cutoff = (
            MARKET_CLOSE_TIME.hour * 60
            + MARKET_CLOSE_TIME.minute
            - int(squareoff_minutes)
        )
        deadline = time(cutoff // 60, cutoff % 60)
    if moment > deadline:
        label = "intraday square-off" if intraday and squareoff_minutes else "entry cut-off"
        return (
            f"{reference:%H:%M:%S} IST is after the {deadline:%H:%M} {label} — "
            "an entry here cannot be exited in this session"
        )
    return None


def _as_exchange(value: datetime) -> datetime:
    """Aware → IST; naive → read as exchange time (already IST wall clock)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=EXCHANGE_TZ)
    return value.astimezone(EXCHANGE_TZ)


def _from_epoch(value: Union[int, float]) -> datetime:
    epoch = float(value)
    if epoch > 1_000_000_000_000:  # milliseconds — typical broker tick payloads
        epoch /= 1000.0
    return datetime.fromtimestamp(epoch, UTC)


def _parse(text: str) -> datetime:
    cleaned = text.strip()
    if not cleaned:
        raise ValueError("empty timestamp")
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError as exc:
        raise ValueError(f"unparseable timestamp {text!r}") from exc


__all__ = [
    "AmbiguousTimeError",
    "ENTRY_CUTOFF_TIME",
    "EOD_SQUAREOFF_WINDOW_START",
    "EXCHANGE_TZ",
    "MARKET_CLOSE_TIME",
    "MARKET_OPEN_TIME",
    "UTC",
    "current_session_date",
    "duration_seconds",
    "ensure_exchange",
    "ensure_utc",
    "entry_window_refusal",
    "format_session_scope",
    "from_market_clock",
    "from_stored_utc",
    "in_eod_squareoff_window",
    "iso_exchange",
    "iso_utc",
    "ist_session_bounds_utc",
    "market_is_open",
    "now_exchange",
    "now_utc",
    "parse_utc_iso",
    "session_date",
    "to_exchange",
]
