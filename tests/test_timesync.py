"""Tests for the time synchronization manager (Step 12).

The important behaviours under test:

* **NSE sessions in IST** — pre-open 09:00–09:15, continuous 09:15–15:30,
  closing session 15:40–16:00, half-open boundaries (15:30:00 is closed).
* **holiday awareness** — next-open/next-close scan over weekends and the
  shipped NSE holiday calendar; trading-day ranges exclude both.
* **timezone discipline** — naive datetimes are rejected; UTC input is
  converted to the exchange clock before any session logic; NYSE DST is
  handled by zoneinfo (09:30 ET is 14:30 UTC in winter, 13:30 in summer).
* **testable clock** — the wall clock is injected, NTP sync applies a
  measured offset on top, and a failed sync keeps the previous offset.
"""

from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from backtest.marketdata import (
    ExchangeCalendar,
    MarketPhase,
    TimeManager,
    TimeSyncError,
    load_calendars,
)

IST = ZoneInfo("Asia/Kolkata")
NY = ZoneInfo("America/New_York")
UTC = timezone.utc

# 2026-08-20 is a Thursday and an NSE trading day.
def ist(hour: int, minute: int, second: int = 0, day: int = 20, month: int = 8) -> datetime:
    return datetime(2026, month, day, hour, minute, second, tzinfo=IST)


def manager(now: datetime | None = None, **kw) -> TimeManager:
    """TimeManager on the shipped config with a frozen clock."""
    frozen = now or ist(12, 0)
    return TimeManager(clock=lambda: frozen.astimezone(UTC), **kw)


# ===========================================================================
# ExchangeCalendar
# ===========================================================================


class TestExchangeCalendar:
    def test_defaults_are_nse(self):
        cal = ExchangeCalendar(code="NSE")
        assert cal.timezone == "Asia/Kolkata"
        assert cal.session_open == dtime(9, 15)
        assert cal.session_close == dtime(15, 30)

    def test_weekday_is_a_trading_day(self):
        assert ExchangeCalendar(code="X").is_trading_day(date(2026, 8, 20))  # Thursday

    def test_weekend_is_not(self):
        cal = ExchangeCalendar(code="X")
        assert not cal.is_trading_day(date(2026, 8, 22))  # Saturday
        assert not cal.is_trading_day(date(2026, 8, 23))  # Sunday

    def test_holiday_is_not(self):
        cal = ExchangeCalendar(code="X", holidays=frozenset({date(2026, 8, 20)}))
        assert not cal.is_trading_day(date(2026, 8, 20))

    def test_custom_weekend(self):
        cal = ExchangeCalendar(code="X", weekend=frozenset({4, 5}))  # Fri, Sat
        assert cal.is_trading_day(date(2026, 8, 23))  # Sunday trades
        assert not cal.is_trading_day(date(2026, 8, 21))  # Friday does not

    def test_open_must_precede_close(self):
        with pytest.raises(ValueError, match="must precede"):
            ExchangeCalendar(code="X", session_open=dtime(16, 0), session_close=dtime(9, 15))

    def test_pre_open_must_end_by_session_open(self):
        with pytest.raises(ValueError, match="pre_open"):
            ExchangeCalendar(code="X", pre_open=(dtime(9, 0), dtime(9, 30)))

    def test_post_close_must_start_after_close(self):
        with pytest.raises(ValueError, match="post_close"):
            ExchangeCalendar(code="X", post_close=(dtime(15, 0), dtime(16, 0)))

    def test_inverted_windows_rejected(self):
        with pytest.raises(ValueError, match="pre_open"):
            ExchangeCalendar(code="X", pre_open=(dtime(9, 10), dtime(9, 0)))

    def test_bad_timezone_rejected(self):
        with pytest.raises(Exception):
            ExchangeCalendar(code="X", timezone="Mars/Olympus")

    def test_bad_weekend_day_rejected(self):
        with pytest.raises(ValueError, match="out of range"):
            ExchangeCalendar(code="X", weekend=frozenset({7}))


# ===========================================================================
# Market phases (NSE session structure)
# ===========================================================================


class TestMarketPhase:
    @pytest.mark.parametrize(
        "hour, minute, second, expected",
        [
            (8, 59, 59, MarketPhase.CLOSED),
            (9, 0, 0, MarketPhase.PRE_OPEN),
            (9, 14, 59, MarketPhase.PRE_OPEN),
            (9, 15, 0, MarketPhase.OPEN),
            (12, 0, 0, MarketPhase.OPEN),
            (15, 29, 59, MarketPhase.OPEN),
            (15, 30, 0, MarketPhase.CLOSED),  # half-open boundary
            (15, 35, 0, MarketPhase.CLOSED),  # between close and closing session
            (15, 40, 0, MarketPhase.POST_CLOSE),
            (15, 59, 59, MarketPhase.POST_CLOSE),
            (16, 0, 0, MarketPhase.CLOSED),
        ],
    )
    def test_nse_session_boundaries(self, hour, minute, second, expected):
        tm = manager()
        assert tm.market_phase(at=ist(hour, minute, second)) is expected

    def test_weekend_is_closed_all_day(self):
        tm = manager()
        assert tm.market_phase(at=ist(12, 0, day=23)) is MarketPhase.CLOSED  # Sunday

    def test_holiday_is_closed_all_day(self):
        tm = manager()
        # 2026-10-20 Dussehra — from the shipped NSE calendar.
        assert tm.market_phase(at=ist(12, 0, day=20, month=10)) is MarketPhase.CLOSED

    def test_phase_defaults_to_now(self):
        tm = manager(now=ist(12, 0))
        assert tm.market_phase() is MarketPhase.OPEN

    def test_utc_input_converted_to_market_clock(self):
        tm = manager()
        # 06:30 UTC == 12:00 IST — open.
        assert tm.market_phase(at=datetime(2026, 8, 20, 6, 30, tzinfo=UTC)) is MarketPhase.OPEN

    def test_naive_input_rejected(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            manager().market_phase(at=datetime(2026, 8, 20, 12, 0))


class TestIsMarketOpen:
    def test_open_during_continuous_session(self):
        assert manager().is_market_open(at=ist(12, 0))

    def test_closed_before_open(self):
        assert not manager().is_market_open(at=ist(9, 5))

    def test_extended_includes_pre_open(self):
        tm = manager()
        assert not tm.is_market_open(at=ist(9, 5))
        assert tm.is_market_open(at=ist(9, 5), include_extended=True)

    def test_extended_includes_closing_session(self):
        tm = manager()
        assert tm.is_market_open(at=ist(15, 45), include_extended=True)

    def test_extended_still_closed_on_weekend(self):
        assert not manager().is_market_open(at=ist(9, 5, day=23), include_extended=True)

    def test_defaults_to_now(self):
        assert manager(now=ist(12, 0)).is_market_open()
        assert not manager(now=ist(18, 0)).is_market_open()

    def test_exchange_argument(self):
        tm = manager()
        # 12:00 IST == 02:30 ET — NYSE closed, NSE open.
        at = ist(12, 0)
        assert tm.is_market_open("NSE", at=at)
        assert not tm.is_market_open("NYSE", at=at)

    def test_unknown_exchange_raises(self):
        with pytest.raises(KeyError, match="unknown exchange"):
            manager().is_market_open("LSE")


class TestNyseDst:
    def test_winter_0930_et_is_1430_utc(self):
        tm = manager()
        # 2026-01-14 is a Wednesday, not an NYSE holiday.
        assert tm.is_market_open("NYSE", at=datetime(2026, 1, 14, 14, 30, tzinfo=UTC))
        assert not tm.is_market_open("NYSE", at=datetime(2026, 1, 14, 13, 30, tzinfo=UTC))

    def test_summer_0930_et_is_1330_utc(self):
        tm = manager()
        # 2026-07-15 is a Wednesday during DST.
        assert tm.is_market_open("NYSE", at=datetime(2026, 7, 15, 13, 30, tzinfo=UTC))

    def test_summer_pre_market_is_extended(self):
        tm = manager()
        at = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)  # 08:00 EDT
        assert not tm.is_market_open("NYSE", at=at)
        assert tm.is_market_open("NYSE", at=at, include_extended=True)

    def test_nyse_holiday_from_config(self):
        tm = manager()
        # Thanksgiving 2026-11-26, a Thursday.
        assert not tm.is_market_open("NYSE", at=datetime(2026, 11, 26, 15, 0, tzinfo=UTC))


# ===========================================================================
# Next open / next close
# ===========================================================================


class TestNextOpen:
    def test_before_open_same_day(self):
        tm = manager()
        assert tm.get_next_market_open(after=ist(8, 0)) == ist(9, 15)

    def test_during_session_goes_to_next_day(self):
        tm = manager()
        assert tm.get_next_market_open(after=ist(12, 0)) == ist(9, 15, day=21)

    def test_exactly_at_open_goes_to_next_day(self):
        tm = manager()
        assert tm.get_next_market_open(after=ist(9, 15)) == ist(9, 15, day=21)

    def test_friday_evening_skips_to_monday(self):
        tm = manager()
        assert tm.get_next_market_open(after=ist(16, 0, day=21)) == ist(9, 15, day=24)

    def test_holiday_skipped(self):
        tm = manager()
        # Wed 2026-01-14 evening: Thu Jan 15 is a holiday → Fri Jan 16.
        after = ist(18, 0, day=14, month=1)
        assert tm.get_next_market_open(after=after) == ist(9, 15, day=16, month=1)

    def test_weekend_then_holiday_skipped(self):
        tm = manager()
        # Sat 2026-01-24: Mon Jan 26 is Republic Day → Tue Jan 27.
        after = ist(10, 0, day=24, month=1)
        assert tm.get_next_market_open(after=after) == ist(9, 15, day=27, month=1)

    def test_defaults_to_now(self):
        tm = manager(now=ist(8, 0))
        assert tm.get_next_market_open() == ist(9, 15)

    def test_result_is_in_exchange_timezone(self):
        result = manager().get_next_market_open(after=ist(8, 0))
        assert result.utcoffset() == timedelta(hours=5, minutes=30)

    def test_never_trading_calendar_raises(self):
        cal = ExchangeCalendar(code="X", weekend=frozenset(range(7)))
        tm = TimeManager(calendars={"X": cal}, clock=lambda: ist(12, 0).astimezone(UTC))
        with pytest.raises(ValueError, match="misconfigured"):
            tm.get_next_market_open()


class TestNextClose:
    def test_before_open_closes_same_day(self):
        tm = manager()
        assert tm.get_next_market_close(after=ist(8, 0)) == ist(15, 30)

    def test_during_session_closes_same_day(self):
        tm = manager()
        assert tm.get_next_market_close(after=ist(12, 0)) == ist(15, 30)

    def test_exactly_at_close_goes_to_next_day(self):
        tm = manager()
        assert tm.get_next_market_close(after=ist(15, 30)) == ist(15, 30, day=21)

    def test_after_hours_goes_to_next_day(self):
        tm = manager()
        assert tm.get_next_market_close(after=ist(15, 45)) == ist(15, 30, day=21)

    def test_holiday_skipped(self):
        tm = manager()
        # Mon 2026-11-09 evening: Tue Nov 10 is Diwali-Balipratipada → Wed Nov 11.
        after = ist(18, 0, day=9, month=11)
        assert tm.get_next_market_close(after=after) == ist(15, 30, day=11, month=11)


class TestSecondsToOpen:
    def test_zero_when_open(self):
        assert manager().seconds_to_open(at=ist(12, 0)) == 0.0

    def test_counts_down_before_open(self):
        assert manager().seconds_to_open(at=ist(9, 0)) == 900.0

    def test_defaults_to_now(self):
        assert manager(now=ist(9, 0)).seconds_to_open() == 900.0


# ===========================================================================
# Alignment and trading days
# ===========================================================================


class TestAlignToTimeframe:
    def test_nse_hourly_is_session_anchored(self):
        tm = manager()
        assert tm.align_to_timeframe(ist(10, 14), "60min") == ist(9, 15)
        assert tm.align_to_timeframe(ist(10, 15), "60min") == ist(10, 15)

    def test_day_aligns_to_ist_midnight(self):
        result = manager().align_to_timeframe(ist(15, 29), "day")
        assert result == datetime(2026, 8, 20, tzinfo=IST)

    def test_nyse_hourly_is_anchored_at_0930(self):
        tm = manager()
        at = datetime(2026, 7, 15, 10, 20, tzinfo=NY)
        assert tm.align_to_timeframe(at, "60min", exchange="NYSE") == datetime(
            2026, 7, 15, 9, 30, tzinfo=NY
        )

    def test_five_minute(self):
        assert manager().align_to_timeframe(ist(9, 17, 33), "5min") == ist(9, 15)


class TestTradingDaysBetween:
    def test_full_week(self):
        days = manager().get_trading_days_between(date(2026, 8, 17), date(2026, 8, 21))
        assert days == [date(2026, 8, 17 + i) for i in range(5)]

    def test_weekend_excluded(self):
        days = manager().get_trading_days_between(date(2026, 8, 21), date(2026, 8, 24))
        assert days == [date(2026, 8, 21), date(2026, 8, 24)]

    def test_holiday_excluded(self):
        # Week of 2026-01-12: Thu Jan 15 is a holiday.
        days = manager().get_trading_days_between(date(2026, 1, 12), date(2026, 1, 18))
        assert days == [date(2026, 1, 12), date(2026, 1, 13),
                        date(2026, 1, 14), date(2026, 1, 16)]

    def test_inclusive_single_day(self):
        assert manager().get_trading_days_between(
            date(2026, 8, 20), date(2026, 8, 20)
        ) == [date(2026, 8, 20)]

    def test_all_closed_range_is_empty(self):
        assert manager().get_trading_days_between(
            date(2026, 8, 22), date(2026, 8, 23)
        ) == []

    def test_datetime_inputs_accepted(self):
        days = manager().get_trading_days_between(ist(10, 0, day=17), ist(10, 0, day=21))
        assert len(days) == 5

    def test_reversed_range_rejected(self):
        with pytest.raises(ValueError, match="after end"):
            manager().get_trading_days_between(date(2026, 8, 21), date(2026, 8, 20))

    def test_is_trading_day_with_datetime_uses_market_date(self):
        tm = manager()
        # 20:00 UTC on Thu Aug 20 is already Fri Aug 21 in IST — a trading day.
        assert tm.is_trading_day(datetime(2026, 8, 20, 20, 0, tzinfo=UTC))
        # 20:00 UTC on Fri Aug 21 is Sat in IST — not a trading day.
        assert not tm.is_trading_day(datetime(2026, 8, 21, 20, 0, tzinfo=UTC))


# ===========================================================================
# Clock, NTP, latency
# ===========================================================================


class TestClock:
    def test_get_current_time_is_market_local(self):
        tm = manager(now=ist(12, 0))
        current = tm.get_current_time()
        assert current == ist(12, 0)
        assert current.utcoffset() == timedelta(hours=5, minutes=30)

    def test_get_current_time_other_exchange(self):
        tm = manager(now=ist(12, 0))
        ny_time = tm.get_current_time("NYSE")
        assert ny_time == ist(12, 0)  # same instant
        assert ny_time.tzname() in ("EDT", "EST")

    def test_now_utc_is_utc(self):
        assert manager(now=ist(12, 0)).now_utc().tzinfo == UTC

    def test_naive_clock_rejected(self):
        tm = TimeManager(clock=lambda: datetime(2026, 8, 20, 12, 0))
        with pytest.raises(ValueError, match="aware"):
            tm.now_utc()

    def test_to_utc_and_to_market_round_trip(self):
        tm = manager()
        utc = tm.to_utc(ist(12, 0))
        assert utc.tzinfo == UTC
        assert tm.to_market(utc) == ist(12, 0)

    def test_to_utc_rejects_naive(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            TimeManager.to_utc(datetime(2026, 8, 20, 12, 0))


class TestNtpSync:
    def test_offset_applied_to_now(self):
        tm = manager(now=ist(12, 0))
        offset = tm.sync_with_ntp(query=lambda server, timeout: 1.5)
        assert offset == 1.5
        assert tm.now_utc() == ist(12, 0, 1).astimezone(UTC) + timedelta(microseconds=500_000)

    def test_negative_offset_supported(self):
        tm = manager(now=ist(12, 0))
        tm.sync_with_ntp(query=lambda server, timeout: -2.0)
        assert tm.now_utc() == ist(11, 59, 58).astimezone(UTC)

    def test_sync_metadata_recorded(self):
        tm = manager()
        tm.sync_with_ntp(server="time.example.com", query=lambda s, t: 0.25)
        assert tm.ntp_server == "time.example.com"
        assert tm.last_ntp_sync is not None
        assert tm.ntp_offset == timedelta(seconds=0.25)

    def test_failed_sync_raises_and_keeps_previous_offset(self):
        tm = manager()
        tm.sync_with_ntp(query=lambda s, t: 1.0)

        def broken(server, timeout):
            raise OSError("network unreachable")

        with pytest.raises(TimeSyncError, match="network unreachable"):
            tm.sync_with_ntp(query=broken)
        assert tm.ntp_offset == timedelta(seconds=1.0)  # not reset

    def test_session_logic_respects_the_offset(self):
        # Clock says 09:14:59 but NTP knows we are 2s behind: actually open.
        tm = manager(now=ist(9, 14, 59))
        assert not tm.is_market_open()
        tm.sync_with_ntp(query=lambda s, t: 2.0)
        assert tm.is_market_open()


class TestLatency:
    def test_empty_stats(self):
        stats = manager().latency_stats()
        assert stats == {"count": 0, "last": None, "mean": None, "p95": None, "max": None}

    def test_basic_stats(self):
        tm = manager()
        for value in (0.1, 0.2, 0.3):
            tm.record_latency(value)
        stats = tm.latency_stats()
        assert stats["count"] == 3
        assert stats["last"] == 0.3
        assert stats["mean"] == pytest.approx(0.2)
        assert stats["max"] == 0.3

    def test_p95(self):
        tm = manager()
        for i in range(1, 101):
            tm.record_latency(i / 1000)
        assert tm.latency_stats()["p95"] == pytest.approx(0.095)

    def test_negative_latency_rejected(self):
        with pytest.raises(ValueError, match=">= 0"):
            manager().record_latency(-0.1)

    def test_window_is_bounded(self):
        tm = manager()
        for i in range(1500):
            tm.record_latency(0.001)
        assert tm.latency_stats()["count"] == 1000


# ===========================================================================
# Calendar config loading
# ===========================================================================


class TestLoadCalendars:
    def test_default_file_loads_nse_and_nyse(self):
        default, calendars = load_calendars()
        assert default == "NSE"
        assert set(calendars) >= {"NSE", "NYSE"}

    def test_nse_holidays_from_config(self):
        _, calendars = load_calendars()
        nse = calendars["NSE"]
        assert not nse.is_trading_day(date(2026, 10, 20))  # Dussehra
        assert not nse.is_trading_day(date(2025, 10, 21))  # Diwali 2025
        assert nse.is_trading_day(date(2026, 11, 9))  # Monday before Diwali

    def test_nse_sessions_from_config(self):
        _, calendars = load_calendars()
        nse = calendars["NSE"]
        assert nse.session_open == dtime(9, 15)
        assert nse.pre_open == (dtime(9, 0), dtime(9, 15))
        assert nse.post_close == (dtime(15, 40), dtime(16, 0))

    def test_manager_defaults_to_config_file(self):
        tm = manager()
        assert tm.default_exchange == "NSE"
        assert "NYSE" in tm.calendars

    def test_default_exchange_override(self):
        tm = TimeManager(default_exchange="NYSE",
                         clock=lambda: ist(12, 0).astimezone(UTC))
        assert tm.calendar().code == "NYSE"

    def test_no_exchanges_rejected(self, tmp_path):
        doc = tmp_path / "cal.yaml"
        doc.write_text("default_exchange: NSE\n")
        with pytest.raises(ValueError, match="no exchanges"):
            load_calendars(path=doc)

    def test_bad_default_exchange_rejected(self, tmp_path):
        doc = tmp_path / "cal.yaml"
        doc.write_text("default_exchange: LSE\nexchanges:\n  NSE: {}\n")
        with pytest.raises(ValueError, match="default_exchange"):
            load_calendars(path=doc)

    def test_bad_weekend_name_rejected(self, tmp_path):
        doc = tmp_path / "cal.yaml"
        doc.write_text("exchanges:\n  NSE:\n    weekend: [caturday]\n")
        with pytest.raises(ValueError, match="unknown weekend day"):
            load_calendars(path=doc)

    def test_flat_holiday_list_supported(self, tmp_path):
        doc = tmp_path / "cal.yaml"
        doc.write_text("exchanges:\n  X:\n    holidays: [2026-08-20]\n")
        _, calendars = load_calendars(path=doc)
        assert not calendars["X"].is_trading_day(date(2026, 8, 20))

    def test_windows_can_be_disabled(self, tmp_path):
        doc = tmp_path / "cal.yaml"
        doc.write_text("exchanges:\n  X:\n    pre_open: null\n    post_close: null\n")
        _, calendars = load_calendars(path=doc)
        assert calendars["X"].pre_open is None
        tm = TimeManager(calendars=calendars, clock=lambda: ist(12, 0).astimezone(UTC))
        assert not tm.is_market_open("X", at=ist(9, 5), include_extended=True)

    def test_custom_calendars_bypass_the_file(self):
        cal = ExchangeCalendar(code="TEST")
        tm = TimeManager(calendars={"test": cal}, clock=lambda: ist(12, 0).astimezone(UTC))
        assert tm.default_exchange == "TEST"
        assert tm.calendar("test").code == "TEST"

    def test_empty_custom_calendars_rejected(self):
        with pytest.raises(ValueError, match="at least one calendar"):
            TimeManager(calendars={})
