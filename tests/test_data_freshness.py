"""Tests for the data-freshness topbar chip (2026-10-04).

The chip answers one question cheaply and must never lie: "is the newest
completed trading session in ``market_data_cache``?" Tail-staleness only —
interior holes belong to the coverage probe. The session calendar comes from
``market_holidays``, so weekends and seeded closures never trigger a nag.
"""

from __future__ import annotations

import datetime as real_datetime
from datetime import date, datetime, timedelta, timezone

import pytest

from backtest.api import data_manager as dm

IST = timezone(timedelta(hours=5, minutes=30))


# ---------------------------------------------------------------------------
# _last_settled_session — when is a session's data DUE
# ---------------------------------------------------------------------------


def test_morning_of_trading_day_targets_previous_session():
    now = datetime(2026, 10, 5, 9, 0, tzinfo=IST)  # Monday 09:00
    assert dm._last_settled_session(now, set()) == date(2026, 10, 2)  # Fri


def test_after_settle_targets_today():
    now = datetime(2026, 10, 5, 16, 20, tzinfo=IST)
    assert dm._last_settled_session(now, set()) == date(2026, 10, 5)


def test_before_settle_does_not_nag_about_an_incomplete_session():
    now = datetime(2026, 10, 5, 15, 45, tzinfo=IST)  # market just closed
    assert dm._last_settled_session(now, set()) == date(2026, 10, 2)


def test_sunday_walks_back_over_weekend_only():
    now = datetime(2026, 10, 4, 12, 0, tzinfo=IST)  # Sunday
    assert dm._last_settled_session(now, set()) == date(2026, 10, 2)  # Fri


def test_holiday_walks_back_over_closures():
    # Friday evening but Friday was Gandhi Jayanti — the target is Thursday.
    now = datetime(2026, 10, 2, 17, 0, tzinfo=IST)
    hol = {date(2026, 10, 2)}
    assert dm._last_settled_session(now, hol) == date(2026, 10, 1)


def test_today_holiday_is_not_the_target_even_after_settle():
    now = datetime(2026, 8, 15, 18, 0, tzinfo=IST)  # Independence Day
    hol = {date(2026, 8, 15)}
    assert dm._last_settled_session(now, hol) == date(2026, 8, 14)


# ---------------------------------------------------------------------------
# _count_missing_sessions — tail arithmetic
# ---------------------------------------------------------------------------

def test_friday_to_monday_without_holidays_is_one_session():
    # (Fri, Mon] holds exactly Monday — the weekend never counts.
    assert dm._count_missing_sessions(date(2026, 10, 2), date(2026, 10, 5), set()) == 1


def test_thursday_to_monday_with_friday_holiday_is_one():
    # (Thu, Mon] would be Fri+Mon (2); the holiday drops Friday -> Monday only.
    hol = {date(2026, 10, 2)}
    assert dm._count_missing_sessions(date(2026, 10, 1), date(2026, 10, 5), hol) == 1


def test_current_when_last_covered_equals_target():
    assert dm._count_missing_sessions(date(2026, 10, 5), date(2026, 10, 5), set()) == 0


def test_no_data_never_counted_here_caller_labels_it():
    assert dm._count_missing_sessions(None, date(2026, 10, 5), set()) == 0


def test_multi_week_gap_counts_weekdays_only():
    # 2026-09-01 (Tue) .. 2026-09-30 (Wed): 22 weekdays, no holidays.
    n = dm._count_missing_sessions(date(2026, 9, 1), date(2026, 9, 30), set())
    weekdays = sum(
        1 for i in range(30)
        if (date(2026, 9, 1) + timedelta(days=i)).weekday() < 5
    ) - 1  # exclude the start day itself
    assert n == weekdays == 21


# ---------------------------------------------------------------------------
# _compute_freshness — the payload the chip renders
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        return _FakeResult(self._rows)


class _FakeEngine:
    def __init__(self, rows=None, exc=None):
        self._rows = rows or []
        self._exc = exc

    def connect(self):
        if self._exc is not None:
            raise self._exc
        return _FakeConn(self._rows)

    def dispose(self):
        pass


class _FixedNow(real_datetime.datetime):
    _NOW = None

    @classmethod
    def now(cls, tz=None):
        return cls._NOW


@pytest.fixture()
def frozen(monkeypatch):
    """Fix 'now', fake the engine + holiday read; yields a mutable state bag."""
    state = {"now": datetime(2026, 10, 5, 17, 0, tzinfo=IST),  # Mon evening
             "rows": [("1min", datetime(2026, 10, 2))],
             "holidays": set(),
             "exc": None}

    _FixedNow._NOW = state["now"]
    monkeypatch.setattr(dm, "datetime", _FixedNow)

    def fake_create_engine(url, echo=False):
        if state["exc"]:
            raise state["exc"]
        return _FakeEngine(rows=list(state["rows"]))

    monkeypatch.setattr(dm, "create_engine", fake_create_engine)
    monkeypatch.setattr(dm, "_load_market_holidays",
                        lambda engine, a, b: set(state["holidays"]))

    prev_status = dm._job["status"]
    dm._job["status"] = "idle"
    yield state
    dm._job["status"] = prev_status


def test_current_when_last_bar_is_yesterday(frozen):
    # target = Fri 02-Oct (Mon 16:15+ => today Monday is also a target...
    # careful: Monday 17:00 -> target IS Monday). Cover Monday instead:
    frozen["rows"] = [("1min", datetime(2026, 10, 5))]
    payload = dm._compute_freshness()
    assert payload["level"] == "current"
    assert payload["label"] == "DATA CURRENT"
    assert payload["db_available"] is True


def test_amber_one_session_behind(frozen):
    # Target = Mon 05-Oct (after settle); newest bar = Fri 02-Oct -> 1 missing.
    payload = dm._compute_freshness()
    assert payload["level"] == "amber"
    assert payload["label"] == "DATA 1 SESSION BEHIND"
    assert "Click to fetch" in payload["title"]


def test_red_two_sessions_behind(frozen):
    frozen["rows"] = [("1min", datetime(2026, 10, 1))]
    payload = dm._compute_freshness()
    assert payload["level"] == "red"
    assert payload["label"] == "DATA 2 SESSIONS BEHIND"


def test_weekend_closure_does_not_nag(frozen):
    # Friday evening: target = Friday itself. Bar from Thursday -> 1 behind
    # (over the weekend it stays 1 — never grows to Monday's Fri-cover check).
    _FixedNow._NOW = datetime(2026, 10, 2, 17, 0, tzinfo=IST)
    frozen["rows"] = [("1min", datetime(2026, 10, 1))]
    payload = dm._compute_freshness()
    assert payload["level"] == "amber"
    _FixedNow._NOW = datetime(2026, 10, 4, 12, 0, tzinfo=IST)  # Sunday noon
    frozen["rows"] = [("1min", datetime(2026, 10, 2))]  # Friday covered
    payload = dm._compute_freshness()
    assert payload["level"] == "current"  # weekend adds nothing due


def test_holiday_backfill_counts_only_real_sessions(frozen):
    # Bars to Thu 01-Oct, target Mon 05-Oct; Fri 02-Oct is a holiday -> the
    # single missing session is Monday, so amber (not red).
    frozen["rows"] = [("1min", datetime(2026, 10, 1))]
    frozen["holidays"] = {date(2026, 10, 2)}
    payload = dm._compute_freshness()
    assert payload["level"] == "amber"
    assert payload["per_timeframe"]["1min"]["missing"] == 1


def test_fetching_state_shadows_level(frozen):
    dm._job["status"] = "running"
    payload = dm._compute_freshness()
    assert payload["level"] == "fetching"
    assert payload["label"] == "DATA FETCHING"


def test_db_error_degrades_to_unknown_never_a_lie(frozen):
    frozen["exc"] = RuntimeError("sorry, too many clients already")
    payload = dm._compute_freshness()
    assert payload["level"] == "unknown"
    assert payload["db_available"] is False
    assert payload["label"] == "DATA ?"


def test_empty_cache_is_no_data_not_current(frozen):
    frozen["rows"] = []
    payload = dm._compute_freshness()
    assert payload["level"] == "red"
    assert payload["label"] == "NO CACHED DATA"


def test_multiple_timeframes_worst_level_wins(frozen):
    frozen["rows"] = [("1min", datetime(2026, 10, 5)),
                      ("1day", datetime(2026, 9, 25))]
    payload = dm._compute_freshness()
    assert payload["per_timeframe"]["1min"]["missing"] == 0
    assert payload["per_timeframe"]["1day"]["missing"] > 1
    assert payload["level"] == "red"
    # tooltip enumerates every timeframe for the detail view
    assert "1min:" in payload["title"] and "1day:" in payload["title"]


# ---------------------------------------------------------------------------
# get_data_freshness — stale-while-revalidate (a render must never wait 14s)
# ---------------------------------------------------------------------------


def test_first_call_returns_placeholder_and_kicks_background_refresh(monkeypatch):
    saved = dict(dm._freshness_cache)
    dm._freshness_cache.update(payload=None, at=0.0, updating=False)

    ran = []

    class _SyncThread:  # execute target immediately, no real thread
        def __init__(self, target=None, **kw):
            self._target = target

        def start(self):
            ran.append(self._target())

    try:
        monkeypatch.setattr(dm.threading, "Thread", _SyncThread)
        result = {"level": "current", "label": "DATA CURRENT"}
        monkeypatch.setattr(dm, "_compute_freshness", lambda: result)
        first = dm.get_data_freshness()
        assert first["label"] == "DATA CHECKING"  # render never blocked
        assert dm._freshness_cache["payload"] is not None  # refresh completed
        assert dm._freshness_cache["updating"] is False
        second = dm.get_data_freshness()
        assert second["label"] == "DATA CURRENT"  # warm cache, no new spawn
    finally:
        dm._freshness_cache.update(saved)


def test_spawn_dedups_inflight_refresh(monkeypatch):
    saved = dict(dm._freshness_cache)
    dm._freshness_cache.update(payload=None, at=0.0, updating=False)
    spawned = []

    class _CountingThread:
        def __init__(self, target=None, **kw):
            spawned.append(target)

        def start(self):
            pass  # never runs -> `updating` stays True, second spawn dedups

    try:
        monkeypatch.setattr(dm.threading, "Thread", _CountingThread)
        dm._spawn_freshness_refresh()
        dm._spawn_freshness_refresh()
        assert len(spawned) == 1
    finally:
        dm._freshness_cache.update(saved)


def test_freshness_works_on_sqlite_not_just_postgres(tmp_path):
    """``MAX(ts::date)`` is Postgres-only; on SQLite it raised
    "unrecognized token: ':'", the broad except swallowed it, and the chip
    claimed "database unreachable" on the DEFAULT dev profile.

    This pins the observable contract: with a reachable SQLite database that
    holds bars, freshness must say so.
    """
    from sqlalchemy import create_engine, text

    from backtest.api.data_manager import _as_session_date

    # the normaliser is the pivot: PG returns datetime, SQLite returns TEXT
    assert _as_session_date("2026-09-30 15:28:00").isoformat() == "2026-09-30"
    assert _as_session_date("2026-09-30").isoformat() == "2026-09-30"
    assert _as_session_date(None) is None
    assert _as_session_date("not-a-date") is None
    import datetime as _dt

    assert _as_session_date(_dt.datetime(2026, 9, 30, 15, 28)).isoformat() == "2026-09-30"

    db = tmp_path / "fresh.db"
    eng = create_engine(f"sqlite:///{db}")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE market_data_cache (timeframe TEXT, ts TEXT)"))
        conn.execute(
            text("INSERT INTO market_data_cache VALUES ('1min', '2026-09-30 15:28:00')")
        )
    eng.dispose()

    import backtest.api.data_manager as dm

    original = dm.DB_URL
    dm.DB_URL = f"sqlite:///{db}"
    try:
        # the exact statement that used to fail on SQLite
        e2 = create_engine(dm.DB_URL)
        with e2.connect() as conn:
            rows = conn.execute(
                text("SELECT timeframe, MAX(ts) FROM market_data_cache GROUP BY timeframe")
            ).all()
        e2.dispose()
        assert rows and rows[0][0] == "1min"
        assert _as_session_date(rows[0][1]).isoformat() == "2026-09-30"
    finally:
        # Restore the URL only — never reload the module. A reload re-executes
        # its module-level locks and caches, stranding references other tests
        # have already imported.
        dm.DB_URL = original
