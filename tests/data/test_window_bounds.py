"""The backtest window's end date, and why it used to lose a day.

Reported symptom: a September backtest of a 1-minute RELIANCE export returned
4817 stored bars at 1min but the engine only ever saw 4447 — exactly one
session short. A single-day run (`from_date == to_date`) failed outright with
"Symbol not found in database". The cause was one clause::

    AND ts BETWEEN :start AND :end

``:end`` is a plain date (``2026-09-30``) from an ``<input type="date">``, so it
resolves to midnight. Every bar of the final session is *after* midnight on the
end date, so the last day was dropped — silently, because a shorter frame is a
valid answer to the query. It happened on PostgreSQL and SQLite alike, and on
every timeframe: 1min lost 370 bars, 1hour lost 7, 1day lost 1, 1week lost 0.

The rule these tests pin: **``end`` includes the day it names.** A caller that
passes a timestamp instead of a date is asking for a real instant and gets one.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest
from sqlalchemy import create_engine, text

from backtest.data.db_source import DbSource, window_bounds

_MDC_DDL = """
CREATE TABLE market_data_cache (
    data_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol    TEXT NOT NULL,
    exchange  TEXT,
    timeframe TEXT NOT NULL,
    ts        TEXT NOT NULL,
    open      REAL NOT NULL,
    high      REAL NOT NULL,
    low       REAL NOT NULL,
    close     REAL NOT NULL,
    volume    REAL NOT NULL,
    source    TEXT,
    UNIQUE (symbol, exchange, timeframe, ts)
)
"""


# ---------------------------------------------------------------------------
# window_bounds — the rule itself
# ---------------------------------------------------------------------------


def test_a_date_only_end_widens_to_the_next_midnight():
    lo, hi = window_bounds("2026-09-02", "2026-09-30")
    assert lo == pd.Timestamp("2026-09-02 00:00")
    assert hi == pd.Timestamp("2026-10-01 00:00")


def test_the_final_midnight_bar_of_the_end_date_is_not_the_cutoff():
    """The exact failure: ``ts < 2026-09-30`` excludes the whole 30th."""
    _, hi = window_bounds("2026-09-02", "2026-09-30")
    assert pd.Timestamp("2026-09-30 09:15") < hi
    assert pd.Timestamp("2026-09-30 15:28") < hi
    assert not (pd.Timestamp("2026-10-01 09:15") < hi)


def test_the_first_bar_of_a_single_day_window_is_included():
    lo, hi = window_bounds("2026-09-30", "2026-09-30")
    assert lo <= pd.Timestamp("2026-09-30 09:15") < hi


def test_an_end_that_carries_a_clock_time_stays_an_instant():
    """A mid-session cut-off must not be silently widened by a whole day."""
    _, hi = window_bounds("2026-09-02", "2026-09-30 12:00")
    assert pd.Timestamp("2026-09-30 12:00") < hi
    assert not (pd.Timestamp("2026-09-30 12:01") < hi)


def test_an_iso_timestamp_end_is_also_an_instant():
    _, hi = window_bounds("2026-09-02", "2026-09-30T15:29:00")
    assert pd.Timestamp("2026-09-30 15:29") < hi
    assert not (pd.Timestamp("2026-09-30 15:30") < hi)


def test_a_window_crossing_a_month_boundary():
    lo, hi = window_bounds("2026-09-30", "2026-09-30")
    assert lo < hi
    _, hi2 = window_bounds("2026-09-01", "2026-09-30")
    assert hi2 == pd.Timestamp("2026-10-01")


# ---------------------------------------------------------------------------
# Through a real database — the read path the backtest actually uses
# ---------------------------------------------------------------------------


@pytest.fixture()
def seeded(tmp_path):
    """400 1-minute bars on 2026-09-30, the day the old query dropped."""
    engine = create_engine(f"sqlite:///{tmp_path / 'bars.db'}")
    with engine.begin() as conn:
        conn.execute(text(_MDC_DDL))
        conn.execute(
            text(
                "INSERT INTO market_data_cache "
                "(symbol, exchange, timeframe, ts, open, high, low, close, volume, source) "
                "VALUES ('RELIANCE', 'NSE', '1min', :ts, 100, 101, 99, 100.5, 1000, 'test')"
            ),
            [
                {"ts": f"2026-09-30 {9 + (m // 60):02d}:{m % 60:02d}:00"}
                for m in range(15, 15 + 400)
            ],
        )
    yield engine
    engine.dispose()


def _source(engine) -> DbSource:
    src = DbSource.__new__(DbSource)  # no engine/URL resolution needed
    src._engine = engine
    return src


def test_the_end_dates_bars_are_returned(seeded):
    out = _source(seeded).get_candles("RELIANCE", "2026-09-30", "2026-09-30", "1min")
    assert len(out) == 400


def test_a_later_end_date_returns_the_same_bars(seeded):
    """Asking through 1 October cannot invent data, and must not lose any."""
    out = _source(seeded).get_candles("RELIANCE", "2026-09-30", "2026-10-01", "1min")
    assert len(out) == 400


def test_the_last_bar_is_present_and_is_the_latest_one(seeded):
    out = _source(seeded).get_candles("RELIANCE", "2026-09-30", "2026-09-30", "1min")
    assert out.index[-1] == pd.Timestamp("2026-09-30 15:54")
    assert out.index[0] == pd.Timestamp("2026-09-30 09:15")


def test_a_window_starting_on_the_end_date_alone_is_not_empty(seeded):
    """``from_date == to_date`` used to raise "Symbol not found in database"."""
    out = _source(seeded).get_candles("RELIANCE", "2026-09-30", "2026-09-30", "1min")
    assert len(out) == 400


def test_a_genuinely_absent_window_still_raises(seeded):
    """Widening the end date must not turn a real gap into a silent empty frame.

    The match used to be ``"not found in database"``. The wording changed
    deliberately (2026-10-06): the message now reports the window that failed
    and the dates the symbol DOES hold, instead of echoing the query's own
    bounds back at the user. The behaviour this guards — a real gap raises
    rather than returning an empty frame — is what the ``raises`` pins.
    """
    with pytest.raises(ValueError, match="has no 1min data"):
        _source(seeded).get_candles("RELIANCE", "2026-09-25", "2026-09-25", "1min")


def test_the_error_message_reports_the_window_and_what_is_stored(seeded):
    """The bug was invisible for months; the message must name the window.

    It used to assert the resolved exclusive upper bound (``2026-09-26
    00:00:00``) appeared verbatim. That was the right assertion when the
    message echoed the query's internals, which is exactly what the user asked
    to stop doing: they typed the window, so repeating it back is noise, and
    the half-open bound is an implementation detail (the resolved bounds are
    still logged by ``get_candles`` for diagnosis).

    What must survive is that a same-day window is understood as ONE inclusive
    day and that the message says so — so the assertion now checks the day
    that was asked for and the range that actually exists.
    """
    with pytest.raises(ValueError) as excinfo:
        _source(seeded).get_candles("RELIANCE", "2026-09-25", "2026-09-25", "1min")
    message = str(excinfo.value)
    assert "2026-09-25" in message, "the day the user asked for"
    assert "30 Sep 2026" in message, "and the dates that ARE stored"


def test_bars_before_the_start_date_are_not_returned(seeded):
    out = _source(seeded).get_candles("RELIANCE", "2026-09-30 12:00", "2026-09-30", "1min")
    assert out.index[0] == pd.Timestamp("2026-09-30 12:00")


def test_the_exclusive_upper_bound_never_leaks_the_next_day():
    """Guard the other direction: widening must stop at the end of the day."""
    _, hi = window_bounds("2026-09-01", "2026-09-30")
    assert not (pd.Timestamp("2026-10-01 00:00") < hi)
    assert datetime(2026, 9, 30, 23, 59, 59) < hi.to_pydatetime()


# ---------------------------------------------------------------------------
# The same rule under the OTHER storage convention
#
# The database the fetch path writes holds UTC stamps (a known defect — the
# offsets it causes are the diagnostic script's job, not this file's), while
# exports of it are converted to IST. So the day rule has to hold for both, and
# the IST fixtures above cannot show that. NSE's session stays inside a single
# UTC calendar day (09:15 IST = 03:45 UTC, 15:29 IST = 09:59 UTC), so the end
# date keeps meaning "that trading day" either way. Pinned, not assumed.
# ---------------------------------------------------------------------------


def _utc_minutes(count: int, first_hour: int = 3, first_minute: int = 45):
    """UTC clock stamps for ``count`` minutes starting 03:45 (09:15 IST)."""
    start = first_hour * 60 + first_minute
    return [
        f"{total // 60:02d}:{total % 60:02d}:00" for total in range(start, start + count)
    ]


@pytest.fixture()
def two_utc_days(tmp_path):
    """Day 1 carries 400 minutes (03:45–10:24 UTC), day 2 carries 10."""
    engine = create_engine(f"sqlite:///{tmp_path / 'utc.db'}")
    with engine.begin() as conn:
        conn.execute(text(_MDC_DDL))
        conn.execute(
            text(
                "INSERT INTO market_data_cache "
                "(symbol, exchange, timeframe, ts, open, high, low, close, volume, source) "
                "VALUES ('RELIANCE', 'NSE', '1min', :ts, 100, 101, 99, 100.5, 1000, 'test')"
            ),
            [{"ts": f"2026-09-30 {t}"} for t in _utc_minutes(400)]
            + [{"ts": f"2026-10-01 {t}"} for t in _utc_minutes(10)],
        )
    yield engine
    engine.dispose()


def test_the_end_date_includes_the_whole_session_when_bars_are_utc(two_utc_days):
    out = _source(two_utc_days).get_candles("RELIANCE", "2026-09-30", "2026-09-30", "1min")
    assert len(out) == 400
    assert out.index[0] == pd.Timestamp("2026-09-30 03:45")
    assert out.index[-1] == pd.Timestamp("2026-09-30 10:24")


def test_the_widened_end_date_still_stops_at_the_next_utc_day(two_utc_days):
    """Widening to the next midnight must not sweep in 1 October's bars."""
    out = _source(two_utc_days).get_candles("RELIANCE", "2026-09-30", "2026-09-30", "1min")
    assert not any(ts >= pd.Timestamp("2026-10-01") for ts in out.index)


def test_a_window_ending_on_the_second_utc_day_carries_both(two_utc_days):
    out = _source(two_utc_days).get_candles("RELIANCE", "2026-09-30", "2026-10-01", "1min")
    assert len(out) == 410


def test_a_utc_session_that_would_be_empty_on_a_stricter_bound(two_utc_days):
    """`ts <= 2026-09-30` alone drops all 400 — the original bug, in UTC."""
    out = _source(two_utc_days).get_candles("RELIANCE", "2026-09-30", "2026-09-30", "1min")
    assert len(out) == 400, "every UTC bar of the day sits after midnight on it"


def test_window_bounds_are_convention_free():
    """window_bounds() reads only the caller's dates, never the stored stamps."""
    lo, hi = window_bounds("2026-09-02", "2026-09-30")
    assert lo == pd.Timestamp("2026-09-02") and hi == pd.Timestamp("2026-10-01")
    # the UTC session's first and last bars both survive those bounds
    assert pd.Timestamp("2026-09-30 03:45") >= lo
    assert pd.Timestamp("2026-09-30 09:59") < hi
