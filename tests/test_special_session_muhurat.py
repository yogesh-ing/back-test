"""Declared abridged sessions (NSE/BSE Diwali Muhurat, 2026-11-08).

Muhurat trading is a real session the app used to sit out: the holiday master
carries 2026-11-08 as ``Diwali Laxmi Pujan / is_trading_holiday=True`` because
the *regular* session is off, and the weekday rule rejects Sunday as well. Both
have to lose to a declared session, and every window must move with it — a book
flattened by the 15:10 floor of a session it is not trading is a fabricated exit.
"""

from datetime import date, datetime, time, timezone

import pytest

from backtest.timekeeping import (
    EXCHANGE_TZ,
    MARKET_CLOSE_TIME,
    MARKET_OPEN_TIME,
    SPECIAL_SESSIONS,
    entry_window_refusal,
    in_eod_squareoff_window,
    is_trading_day,
    market_is_open,
    session_window,
    special_session,
)

MUHURAT = date(2026, 11, 8)
ORDINARY_SUNDAY = date(2026, 11, 15)
ORDINARY_TUESDAY = date(2026, 11, 10)
#: What the Postgres ``market_holidays`` row for the Muhurat day actually says.
HOLIDAY_MASTER = (MUHURAT,)


def ist(hhmm: str) -> datetime:
    """An instant on the Muhurat day, stated in exchange time."""
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime(MUHURAT.year, MUHURAT.month, MUHURAT.day, hour, minute, tzinfo=EXCHANGE_TZ)


def test_declared_session_outranks_the_holiday_row_and_the_weekday_rule():
    assert MUHURAT.weekday() == 6  # Sunday
    assert is_trading_day(MUHURAT, holidays=HOLIDAY_MASTER) is True
    assert is_trading_day(ist("14:25"), holidays=HOLIDAY_MASTER) is True


def test_an_ordinary_sunday_is_still_closed():
    assert is_trading_day(ORDINARY_SUNDAY) is False
    assert is_trading_day(ORDINARY_TUESDAY) is True
    assert special_session(ORDINARY_TUESDAY) is None


def test_a_weekday_holiday_is_still_closed():
    assert is_trading_day(ORDINARY_TUESDAY, holidays=[ORDINARY_TUESDAY.isoformat()]) is False


def test_session_window_values():
    assert session_window(MUHURAT) == (
        SPECIAL_SESSIONS[MUHURAT].open,
        SPECIAL_SESSIONS[MUHURAT].close,
    )
    assert session_window(ORDINARY_TUESDAY) == (MARKET_OPEN_TIME, MARKET_CLOSE_TIME)


@pytest.mark.parametrize(
    "hhmm,expected",
    [
        ("09:20", False),   # the regular open means nothing on a 14:20 session
        ("14:04", False),
        ("14:20", True),
        ("14:25", True),
        ("15:35", True),    # equity has closed; derivatives are still trading
        ("15:40", True),
        ("15:41", False),
    ],
)
def test_market_is_open_follows_the_short_session(hhmm, expected):
    assert market_is_open(ist(hhmm)) is expected


@pytest.mark.parametrize(
    "hhmm,expected",
    [
        ("15:05", False),   # the 15:10 floor belongs to a different session
        ("15:15", False),
        ("15:20", True),
        ("15:35", True),
        ("15:40", False),   # at the close the sweep is over
    ],
)
def test_squareoff_window_is_measured_from_this_sessions_close(hhmm, expected):
    assert in_eod_squareoff_window(ist(hhmm)) is expected


def test_normal_day_squareoff_window_is_unchanged():
    tuesday = datetime(2026, 11, 10, 15, 11, tzinfo=EXCHANGE_TZ)
    assert in_eod_squareoff_window(tuesday) is True
    assert in_eod_squareoff_window(tuesday.replace(minute=5)) is False


def test_no_entry_before_the_muhurat_open():
    refusal = entry_window_refusal(ist("14:10"))
    assert refusal is not None and "before the 14:20:00 open" in refusal


def test_no_entry_after_the_muhurat_close():
    refusal = entry_window_refusal(ist("15:45"))
    assert refusal is not None and "after the 15:40:00 close" in refusal


def test_entry_cutoff_sits_five_minutes_clear_of_the_close():
    assert entry_window_refusal(ist("15:30")) is None
    refusal = entry_window_refusal(ist("15:36"))
    assert refusal is not None and "after the 15:35 entry cut-off" in refusal


def test_intraday_entry_deadline_is_the_sessions_own_squareoff():
    refusal = entry_window_refusal(ist("15:25"), intraday=True, squareoff_minutes=20)
    assert refusal is not None and "after the 15:20 intraday square-off" in refusal


def test_the_intraday_deadline_moves_later_with_a_short_session():
    """Same wall clock, opposite verdict: 15:15 is flat-time at 15:30 close only."""
    tuesday = datetime(2026, 11, 10, 15, 15, tzinfo=EXCHANGE_TZ)
    assert entry_window_refusal(tuesday, intraday=True, squareoff_minutes=20) is not None
    assert entry_window_refusal(ist("15:15"), intraday=True, squareoff_minutes=20) is None


def test_normal_day_messages_are_byte_identical():
    """The regression that matters: nothing on an ordinary day changes wording."""
    tuesday = datetime(2026, 11, 10, 15, 25, 1, tzinfo=EXCHANGE_TZ)
    assert entry_window_refusal(tuesday) == (
        "15:25:01 IST is after the 15:25 entry cut-off — "
        "an entry here cannot be exited in this session"
    )
    early = datetime(2026, 11, 10, 9, 14, tzinfo=EXCHANGE_TZ)
    assert "before the 09:15:00 open" in entry_window_refusal(early)


# -- consumers of the authority ------------------------------------------------


def test_market_status_reports_the_declared_window():
    from backtest.live.market_status import _resolve_session

    res = _resolve_session(ist("14:25"), "TRADING_DAY", "Diwali Muhurat", {}, None)
    assert res["session_state"] == "OPEN" and res["is_open"] is True
    assert res["pre_open"] == "14:05:00"
    assert res["session_open"] == "14:20:00"
    assert res["session_close"] == "15:40:00"


def test_market_status_bounds_match_a_regular_session():
    from backtest.live.market_status import _resolve_session

    tuesday = datetime(2026, 11, 10, 10, 0, tzinfo=EXCHANGE_TZ)
    res = _resolve_session(tuesday, "TRADING_DAY", None, {}, None)
    assert (res["pre_open"], res["session_open"], res["session_close"]) == (
        "09:00:00",
        "09:15:00",
        "15:30:00",
    )


def test_feed_fail_open_fallback_follows_the_declared_session(monkeypatch):
    """The mStock feed's last-resort hours check must not call a Muhurat shut."""
    import backtest.data.mstock_live_feed as feed
    import backtest.live.market_status as market_status

    def boom(*_a, **_k):
        raise RuntimeError("calendar unreadable")

    monkeypatch.setattr(market_status, "get_market_day_state", boom)
    assert feed._market_open(ist("15:35").astimezone(timezone.utc)) is True
    assert feed._market_open(ist("14:10").astimezone(timezone.utc)) is False


def test_option_session_is_open_runs_to_the_declared_close():
    from backtest.options.paper_trading import session_is_open

    assert session_is_open(ist("15:35").astimezone(timezone.utc)) is True
    assert session_is_open(ist("15:45").astimezone(timezone.utc)) is False
    assert session_is_open(ist("13:00").astimezone(timezone.utc)) is False


def test_diary_regime_bucket_uses_the_short_session():
    from backtest.diary.regimes import classify_ist_session_regime

    assert classify_ist_session_regime(ist("14:25")) == "afternoon_gamma"
    assert classify_ist_session_regime(ist("15:25")) == "closing_squareoff"
    assert classify_ist_session_regime(ist("13:00")) == "off_hours"


def test_time_manager_gates_the_declared_session():
    from backtest.live.time_manager import TimeManager

    manager = TimeManager(market="NSE")
    assert manager.is_market_open(ist("15:35")) is True
    assert manager.is_market_open(ist("14:10")) is False
    assert manager.get_next_market_close(ist("14:25")).time() == time(15, 40)
    assert manager.get_trading_days_between(MUHURAT, MUHURAT) == [MUHURAT]
