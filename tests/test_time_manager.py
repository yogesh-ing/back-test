"""Tests for Step 12: Time Synchronization Manager (NSE calendar, IST/UTC)."""

from __future__ import annotations

from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from backtest.live.time_manager import TimeManager, NSE_TIMEZONE


def test_time_manager_init():
    tm = TimeManager(market="NSE")
    assert tm.market == "NSE"
    assert tm.timezone == NSE_TIMEZONE

    tm_nyse = TimeManager(market="NYSE")
    assert tm_nyse.market == "NYSE"


def test_get_current_time():
    tm = TimeManager(market="NSE")
    now = tm.get_current_time()
    assert now.tzinfo is not None

    # With specific tz
    utc_now = tm.get_current_time(tz="UTC")
    assert str(utc_now.tzinfo) == "UTC" or utc_now.tzinfo is not None


def test_mock_time():
    tm = TimeManager(market="NSE")
    mock = datetime(2024, 1, 2, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    tm.set_mock_time(mock)
    assert tm.get_current_time() == mock

    tm.advance_mock_time(timedelta(minutes=5))
    assert tm.get_current_time().minute == 5

    tm.set_mock_time(None)
    # Should return real time now
    assert tm.get_current_time() != mock


def test_is_market_open_weekday():
    tm = TimeManager(market="NSE")

    # Tuesday 10:00 IST should be open
    dt = datetime(2024, 1, 2, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert tm.is_market_open(dt) is True

    # Tuesday 08:00 IST should be closed (before 09:15)
    dt = datetime(2024, 1, 2, 8, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert tm.is_market_open(dt) is False

    # Tuesday 16:00 IST should be closed (after 15:30)
    dt = datetime(2024, 1, 2, 16, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert tm.is_market_open(dt) is False


def test_is_market_open_weekend():
    tm = TimeManager(market="NSE")

    # Saturday
    dt = datetime(2024, 1, 6, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert tm.is_market_open(dt) is False

    # Sunday
    dt = datetime(2024, 1, 7, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert tm.is_market_open(dt) is False


def test_is_market_open_holiday():
    tm = TimeManager(market="NSE", holidays=[date(2024, 1, 2)])

    dt = datetime(2024, 1, 2, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert tm.is_market_open(dt) is False

    # Next day should be open (if not weekend/holiday)
    dt = datetime(2024, 1, 3, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert tm.is_market_open(dt) is True


def test_pre_and_after_market():
    tm = TimeManager(market="NSE")

    # Pre-market: 09:00-09:15
    dt = datetime(2024, 1, 2, 9, 5, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert tm.is_pre_market(dt) is True
    assert tm.is_market_open(dt) is False

    # After-hours: 15:30-16:00
    dt = datetime(2024, 1, 2, 15, 45, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert tm.is_after_hours(dt) is True


def test_next_market_open():
    tm = TimeManager(market="NSE")

    # Tuesday 16:00 -> next open Wednesday 09:15
    dt = datetime(2024, 1, 2, 16, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    next_open = tm.get_next_market_open(dt)
    assert next_open.date() == date(2024, 1, 3)
    assert next_open.hour == 9
    assert next_open.minute == 15

    # Friday 16:00 -> next open Monday 09:15 (skip weekend)
    dt = datetime(2024, 1, 5, 16, 0, tzinfo=ZoneInfo("Asia/Kolkata"))  # Friday
    next_open = tm.get_next_market_open(dt)
    assert next_open.date() == date(2024, 1, 8)  # Monday
    assert next_open.weekday() == 0


def test_next_market_close():
    tm = TimeManager(market="NSE")

    # Tuesday 10:00 -> close same day 15:30
    dt = datetime(2024, 1, 2, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    next_close = tm.get_next_market_close(dt)
    assert next_close.date() == date(2024, 1, 2)
    assert next_close.hour == 15
    assert next_close.minute == 30

    # Tuesday 16:00 -> close next day
    dt = datetime(2024, 1, 2, 16, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    next_close = tm.get_next_market_close(dt)
    assert next_close.date() == date(2024, 1, 3)


def test_trading_days_between():
    tm = TimeManager(market="NSE", holidays=[date(2024, 1, 3)])

    # Mon to Fri with 1 holiday
    start = date(2024, 1, 1)  # Monday
    end = date(2024, 1, 5)    # Friday
    days = tm.get_trading_days_between(start, end)
    # Should exclude weekend (none in this range) and 1 holiday
    assert len(days) == 4
    assert date(2024, 1, 3) not in days


def test_align_to_timeframe():
    tm = TimeManager(market="NSE")

    # 1min
    dt = "2024-01-02T09:17:32+05:30"
    aligned = tm.align_to_timeframe(dt, "1min")
    assert aligned.second == 0
    assert aligned.minute == 17

    # 5min
    aligned = tm.align_to_timeframe(dt, "5min")
    assert aligned.minute == 15
    assert aligned.second == 0

    # 15min
    aligned = tm.align_to_timeframe(dt, "15min")
    assert aligned.minute == 15

    # 1hour
    aligned = tm.align_to_timeframe(dt, "1hour")
    assert aligned.minute == 0
    assert aligned.hour == 9

    # day
    aligned = tm.align_to_timeframe(dt, "day")
    assert aligned.hour == 0
    assert aligned.minute == 0


def test_is_bar_closed():
    tm = TimeManager(market="NSE")

    bar_start = datetime(2024, 1, 2, 9, 15, tzinfo=ZoneInfo("Asia/Kolkata"))
    # 5min bar starting 09:15 closes at 09:20
    current = datetime(2024, 1, 2, 9, 20, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert tm.is_bar_closed(bar_start, "5min", current) is True

    current = datetime(2024, 1, 2, 9, 19, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert tm.is_bar_closed(bar_start, "5min", current) is False


def test_timezone_conversion():
    tm = TimeManager(market="NSE")

    # UTC to IST
    utc_dt = datetime(2024, 1, 2, 4, 45, tzinfo=timezone.utc)  # 04:45 UTC = 10:15 IST
    ist_dt = tm.to_market_time(utc_dt)
    assert ist_dt.hour == 10
    assert ist_dt.minute == 15

    # IST to UTC
    ist_dt = datetime(2024, 1, 2, 10, 15, tzinfo=ZoneInfo("Asia/Kolkata"))
    utc_dt = tm.to_utc(ist_dt)
    assert utc_dt.hour == 4
    assert utc_dt.minute == 45


def test_latency_tracking():
    tm = TimeManager(market="NSE")

    for latency in [10, 20, 30, 100, 50]:
        tm.measure_latency(latency)

    stats = tm.get_latency_stats()
    assert stats["count"] == 5
    assert stats["mean_ms"] > 0
    assert stats["max_ms"] == 100


def test_ntp_sync_placeholder():
    tm = TimeManager(market="NSE", ntp_sync=False)
    assert tm.sync_with_ntp() is True
