"""Market-status awareness and trading-holiday calendar (NSE).

Implements the contract specified in docs/MARKET-STATUS-SPEC-2026-10-02.md:
  * day_state ∈ { HOLIDAY, WEEKEND, TRADING_DAY }
  * session_state ∈ { PRE_OPEN, OPEN, CLOSED_TODAY, CLOSED_WEEKEND, CLOSED_HOLIDAY }
  * calendar gap warning when current year has no DB holiday entries
  * fail-open: calendar read failure defaults to TRADING_DAY / OPEN during hours
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("backtest.live.market_status")

IST = timezone(timedelta(hours=5, minutes=30))
PRE_OPEN_TIME = time(9, 0)
MARKET_OPEN_TIME = time(9, 15)
MARKET_CLOSE_TIME = time(15, 30)

# Verbatim 2026 NSE equity trading holiday seed (weekday trading holidays)
NSE_HOLIDAYS_2026: list[tuple[str, str, str]] = [
    ("2026-01-15", "equity", "Municipal Corporation Election - Maharashtra"),
    ("2026-01-26", "equity", "Republic Day"),
    ("2026-03-03", "equity", "Holi"),
    ("2026-03-26", "equity", "Shri Ram Navami"),
    ("2026-03-31", "equity", "Shri Mahavir Jayanti"),
    ("2026-04-03", "equity", "Good Friday"),
    ("2026-04-14", "equity", "Dr. Baba Saheb Ambedkar Jayanti"),
    ("2026-05-01", "equity", "Maharashtra Day"),
    ("2026-05-28", "equity", "Bakri Id"),
    ("2026-06-26", "equity", "Muharram"),
    ("2026-09-14", "equity", "Ganesh Chaturthi"),
    ("2026-10-02", "equity", "Mahatma Gandhi Jayanti"),
    ("2026-10-20", "equity", "Dussehra"),
    ("2026-11-08", "equity", "Diwali Laxmi Pujan"),
    ("2026-11-24", "equity", "Prakash Gurpurb Sri Guru Nanak Dev"),
    ("2026-12-25", "equity", "Christmas"),
]

# In-memory day cache: (date, segment) -> dict
_DAY_CACHE: dict[Tuple[date, str], dict[str, Any]] = {}


def _get_holidays_from_db(
    year: int, segment: str = "equity", db_manager: Any = None
) -> Tuple[Dict[date, str], bool]:
    """Fetch holidays for the given year from the database.

    Returns:
        (holiday_map, has_year_data)
        If the year has 0 rows, has_year_data is False.
    """
    if db_manager is None:
        try:
            from backtest.db.manager import DatabaseManager
            db_manager = DatabaseManager.from_env()
        except Exception as exc:  # noqa: BLE001
            logger.debug("could not instantiate DatabaseManager: %s", exc)
            return {}, False

    try:
        from backtest.db.models import MarketHoliday

        start_date = date(year, 1, 1)
        end_date = date(year, 12, 31)
        with db_manager.session() as session:
            try:
                rows = (
                    session.query(MarketHoliday)
                    .filter(
                        MarketHoliday.holiday_date >= start_date,
                        MarketHoliday.holiday_date <= end_date,
                        MarketHoliday.segment == segment,
                        MarketHoliday.is_trading_holiday.is_(True),
                    )
                    .all()
                )
            except Exception:
                session.rollback()
                MarketHoliday.ensure_schema(db_manager)
                rows = (
                    session.query(MarketHoliday)
                    .filter(
                        MarketHoliday.holiday_date >= start_date,
                        MarketHoliday.holiday_date <= end_date,
                        MarketHoliday.segment == segment,
                        MarketHoliday.is_trading_holiday.is_(True),
                    )
                    .all()
                )

            if not rows and year == 2026:
                MarketHoliday.seed_2026(session)
                rows = (
                    session.query(MarketHoliday)
                    .filter(
                        MarketHoliday.holiday_date >= start_date,
                        MarketHoliday.holiday_date <= end_date,
                        MarketHoliday.segment == segment,
                        MarketHoliday.is_trading_holiday.is_(True),
                    )
                    .all()
                )

            if not rows:
                return {}, False
            return {r.holiday_date: r.description for r in rows}, True
    except Exception as exc:  # noqa: BLE001 - fail-open trap guard
        logger.warning("failed to query market_holidays: %s", exc)
        raise


def get_market_day_state(
    now_ist: Optional[datetime] = None,
    segment: str = "equity",
    db_manager: Any = None,
) -> dict[str, Any]:
    """Resolve current market day-state and session-state per NSE rules.

    Args:
        now_ist: Target timestamp (defaults to current wall-clock in IST).
        segment: Market segment ('equity', 'derivative', etc.).
        db_manager: Optional DatabaseManager instance.

    Returns:
        Dict conforming to docs/MARKET-STATUS-SPEC-2026-10-02.md §4.
    """
    is_realtime = now_ist is None
    if now_ist is None:
        now_utc = datetime.now(timezone.utc)
        now_ist = now_utc.astimezone(IST)
    elif now_ist.tzinfo is None:
        now_ist = now_ist.replace(tzinfo=IST)
    else:
        now_ist = now_ist.astimezone(IST)

    target_date = now_ist.date()

    # Use cache only for real-time evaluations on the current day
    cache_key = (target_date, segment)
    if is_realtime and cache_key in _DAY_CACHE:
        cached = _DAY_CACHE[cache_key]
        # Recompute dynamic time-of-day session_state from cached day_state
        return _resolve_session(now_ist, cached["day_state"], cached["holiday_name"],
                                cached["holidays_map"], cached["gap_warning"])

    day_state: str
    holiday_name: Optional[str] = None
    gap_warning: Optional[str] = None
    holidays_map: Dict[date, str] = {}

    if target_date.weekday() >= 5:
        day_state = "WEEKEND"
        # Try loading holidays quietly for next_open_ts calculation
        try:
            hols, has_data = _get_holidays_from_db(target_date.year, segment, db_manager)
            if has_data:
                holidays_map = hols
        except Exception:
            pass
    else:
        try:
            hols, has_data = _get_holidays_from_db(target_date.year, segment, db_manager)
            if not has_data:
                # Calendar gap rule: table has no rows for this year
                day_state = "TRADING_DAY"
                gap_warning = f"⚠ No holiday data for {target_date.year} — weekday rule only"
            elif target_date in hols:
                day_state = "HOLIDAY"
                holiday_name = hols[target_date]
                holidays_map = hols
            else:
                day_state = "TRADING_DAY"
                holidays_map = hols
        except Exception:
            # Fail-open rule: DB error degrades to TRADING_DAY (open during hours)
            day_state = "TRADING_DAY"
            gap_warning = "⚠ Holiday calendar unreadable — fail-open to trading day"

    res = _resolve_session(now_ist, day_state, holiday_name, holidays_map, gap_warning)

    if is_realtime:
        _DAY_CACHE[cache_key] = {
            "day_state": day_state,
            "holiday_name": holiday_name,
            "holidays_map": holidays_map,
            "gap_warning": gap_warning,
        }

    return res


def _resolve_session(
    now_ist: datetime,
    day_state: str,
    holiday_name: Optional[str],
    holidays_map: Dict[date, str],
    gap_warning: Optional[str],
) -> dict[str, Any]:
    target_date = now_ist.date()
    target_time = now_ist.time()

    if day_state == "WEEKEND":
        session_state = "POST_CLOSE"
        is_open = False
    elif day_state == "HOLIDAY":
        session_state = "POST_CLOSE"
        is_open = False
    else:
        # TRADING_DAY
        if target_time < PRE_OPEN_TIME:
            session_state = "POST_CLOSE"
            is_open = False
        elif PRE_OPEN_TIME <= target_time < MARKET_OPEN_TIME:
            session_state = "PRE_OPEN"
            is_open = False
        elif MARKET_OPEN_TIME <= target_time <= MARKET_CLOSE_TIME:
            session_state = "OPEN"
            is_open = True
        else:
            session_state = "POST_CLOSE"
            is_open = False

    # Calculate next_open_ts and formatted string
    next_open_dt = _calculate_next_open_dt(now_ist, day_state, holidays_map)
    next_open_ts = next_open_dt.isoformat() if next_open_dt else None
    next_open_formatted = next_open_dt.strftime("%a %d %b %H:%M IST") if next_open_dt else None

    return {
        "day_state": day_state,
        "session_state": session_state,
        "holiday_name": holiday_name,
        "next_open_ts": next_open_ts,
        "next_open_formatted": next_open_formatted,
        "gap_warning": gap_warning,
        "is_open": is_open,
        "date": target_date.isoformat(),
        "time": target_time.strftime("%H:%M:%S"),
    }


def _calculate_next_open_dt(
    now_ist: datetime,
    day_state: str,
    holidays_map: Dict[date, str],
) -> datetime:
    current_date = now_ist.date()
    current_time = now_ist.time()

    # If it is a trading day and market hasn't opened yet today
    if day_state == "TRADING_DAY" and current_time < MARKET_OPEN_TIME:
        return datetime.combine(current_date, MARKET_OPEN_TIME, tzinfo=IST)

    # Otherwise search forward starting tomorrow
    candidate = current_date + timedelta(days=1)
    while candidate.weekday() >= 5 or candidate in holidays_map:
        candidate += timedelta(days=1)

    return datetime.combine(candidate, MARKET_OPEN_TIME, tzinfo=IST)
