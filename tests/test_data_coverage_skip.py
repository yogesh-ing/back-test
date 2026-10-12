"""Tests for the coverage-aware gap fill (2026-10-03).

A re-run over an already-fetched range must cross-check the user's requested
[from, to] against ``market_data_cache`` day by day and pull ONLY the windows
that still contain a missing day — a fully-covered symbol costs zero API
requests, and scattered 502 holes (ABCAPITAL) get gap-filled instead of being
blindly re-walked chunk-by-chunk.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from backtest.api import data_manager as dm


# ---------------------------------------------------------------------------
# Thresholds & the enable flag
# ---------------------------------------------------------------------------


def test_coverage_threshold_scales_with_timeframe():
    assert dm._coverage_threshold("1min") == 338   # ceil(375 * 0.9)
    assert dm._coverage_threshold("5min") == 68    # ceil(75  * 0.9)
    assert dm._coverage_threshold("15min") == 23   # ceil(25  * 0.9)
    assert dm._coverage_threshold("1hour") == 6    # ceil(6   * 0.9)
    assert dm._coverage_threshold("1day") == 1     # any single daily bar counts
    assert dm._coverage_threshold("mystery") == 1  # unknown tf -> never skip wrongly


def test_skip_enabled_defaults_on_and_respects_env(monkeypatch):
    monkeypatch.delenv("DATA_FETCH_SKIP_COVERED", raising=False)
    assert dm._coverage_skip_enabled() is True
    monkeypatch.setenv("DATA_FETCH_SKIP_COVERED", "0")
    assert dm._coverage_skip_enabled() is False
    monkeypatch.setenv("DATA_FETCH_SKIP_COVERED", "1")
    assert dm._coverage_skip_enabled() is True


# ---------------------------------------------------------------------------
# _build_windows — the range stepper, inclusive on both ends
# ---------------------------------------------------------------------------


def test_build_windows_covers_range_in_chunk_days_steps():
    wins = dm._build_windows("2026-06-01", "2026-06-07", 2)
    assert [(s.strftime("%m-%d"), e.strftime("%m-%d")) for s, e in wins] == [
        ("06-01", "06-03"),
        ("06-04", "06-06"),
        ("06-07", "06-07"),  # Sun tail — the last day is never dropped
    ]


def test_build_windows_single_day_yields_one_window():
    # Regression (Data-page test 2026-10-06): from == to built ZERO windows,
    # so a one-day fetch "completed" without ever asking mStock for bars.
    assert [(s.strftime("%m-%d"), e.strftime("%m-%d"))
            for s, e in dm._build_windows("2026-06-01", "2026-06-01", 2)] == [
        ("06-01", "06-01")
    ]


# ---------------------------------------------------------------------------
# _windows_needing_fetch — the day-level diff
# ---------------------------------------------------------------------------

_WINS = dm._build_windows("2026-06-01", "2026-06-07", 2)  # (01..03), (04..06), (07..07)


def test_nothing_covered_fetches_every_window():
    assert dm._windows_needing_fetch(_WINS, set()) == _WINS


def test_fully_covered_symbol_fetches_nothing():
    # 2026-06-01..06 = Mon..Sat; all five trading days covered.
    covered = {date(2026, 6, d) for d in range(1, 6)}
    assert dm._windows_needing_fetch(_WINS, covered) == []


def test_weekend_days_never_mark_a_window_missing():
    # Regression (live check 2026-10-03): without the weekday rule a fully
    # fetched symbol still re-fetches every window touching a Saturday, so the
    # 200-symbol resume never gets cheaper.
    # Window A = Mon 01..Wed 03, window B = Thu 04..Sat 06 (Sat never has bars).
    covered = {date(2026, 6, d) for d in range(1, 6)}  # Sat 06 absent by design
    assert dm._windows_needing_fetch(_WINS, covered) == []


def test_only_windows_with_a_missing_weekday_are_kept():
    # Window A's weekdays covered; window B missing Fri 05 -> only B kept.
    covered = {date(2026, 6, d) for d in (1, 2, 3, 4, 6)}
    kept = dm._windows_needing_fetch(_WINS, covered)
    assert [(s.strftime("%m-%d"), e.strftime("%m-%d")) for s, e in kept] == [
        ("06-04", "06-06")
    ]


def test_missing_weekday_requeues_whole_window():
    # A single missing weekday drags its whole window back (the upsert dedupes
    # the rest). Window A = 01..03 (Tue 03 missing) -> kept;
    # window B = 04..06 (all weekdays covered, Sat ignored) -> dropped.
    covered = {date(2026, 6, d) for d in (1, 2, 4, 5)}
    kept = dm._windows_needing_fetch(_WINS, covered)
    assert len(kept) == 1
    assert [(s.strftime("%m-%d"), e.strftime("%m-%d")) for s, e in kept] == [
        ("06-01", "06-03")
    ]


# ---------------------------------------------------------------------------
# _probe_covered_days — the batched, gap-aware DB probe (2026-10-04)
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows=None, exc=None):
        self._rows = rows or []
        self._exc = exc
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if self._exc is not None:
            raise self._exc
        self.params = params
        return _FakeResult(self._rows)


class _FakeEngine:
    def __init__(self, rows=None, exc=None):
        self._rows = rows
        self._exc = exc
        self.last_params = None
        self.last_sql = None

    def connect(self):
        if self._exc is not None:
            raise self._exc
        conn = _FakeConn(self._rows, self._exc)
        self._conn = conn
        return conn


def test_probe_returns_covered_days_per_symbol():
    engine = _FakeEngine(
        rows=[
            ("TCS", date(2026, 6, 1), 375),        # full day
            ("TCS", date(2026, 6, 2), 12),         # a few stray bars — below 338
            ("TCS", date(2026, 6, 3), 340),        # complete enough
            ("RELIANCE", date(2026, 6, 1), 370),   # covered for its own symbol
        ]
    )
    covered = dm._probe_covered_days(
        engine, ["TCS", "RELIANCE"], "1min", "2026-06-01", "2026-06-05"
    )
    assert covered == {
        "TCS": {date(2026, 6, 1), date(2026, 6, 3)},
        "RELIANCE": {date(2026, 6, 1)},
    }


def test_probe_treats_inbetween_hole_as_not_covered():
    """A day missing hours of bars (300 of 375 distinct minutes) must NOT
    count as fetched — it would feed a gappy session into backtests."""
    engine = _FakeEngine(rows=[("TCS", date(2026, 6, 1), 300)])
    covered = dm._probe_covered_days(engine, ["TCS"], "1min", "2026-06-01", "2026-06-01")
    assert covered == {}


def test_probe_error_returns_empty_never_skips():
    engine = _FakeEngine(exc=RuntimeError("table missing"))
    covered = dm._probe_covered_days(
        engine, ["TCS"], "1min", "2026-06-01", "2026-06-05"
    )
    assert covered == {}  # caller then does a full fetch


def test_probe_is_exchange_agnostic_and_binds_every_symbol():
    engine = _FakeEngine(rows=[])
    dm._probe_covered_days(
        engine, ["TCS", "RELIANCE"], "1min", "2022-01-03", "2026-10-03"
    )
    params = engine._conn.params
    assert params is not None
    # no exchange filter — bars stored under either exchange cover the symbol
    assert "exchange" not in params
    # one IN-placeholder per distinct symbol
    assert params["s0"] == "TCS" and params["s1"] == "RELIANCE"
    assert params["timeframe"] == "1min"


def test_probe_with_no_symbols_returns_empty_without_querying():
    engine = _FakeEngine(rows=[])
    covered = dm._probe_covered_days(engine, [], "1min", "2026-06-01", "2026-06-05")
    assert covered == {}
    assert engine.last_params is None  # never even connected


# ---------------------------------------------------------------------------
# The probe must run on the DEFAULT profile, not only on PostgreSQL
#
# `date_trunc('day', ts)::date` is PostgreSQL cast syntax. On SQLite the probe
# raised, its `except` returned {} — the documented "never skip wrongly" path —
# and so every symbol was re-fetched in full on every resume. Safe, but it meant
# the coverage skip never once fired on the default dev profile
# (config/database.yaml → sqlite, and 13 sqlite migrations ship).
# ---------------------------------------------------------------------------


class _DialectEngine:
    """Minimal engine carrying only what _day_expr reads."""

    def __init__(self, name):
        self.dialect = type("D", (), {"name": name})()


def test_day_expr_is_dialect_aware():
    assert dm._day_expr(_DialectEngine("sqlite")) == "date(ts)"
    assert dm._day_expr(_DialectEngine("postgresql")) == "date_trunc('day', ts)::date"
    # a named column is carried through
    assert dm._day_expr(_DialectEngine("sqlite"), "bar_ts") == "date(bar_ts)"


def test_day_expr_on_an_unknown_engine_does_not_raise():
    """It is called OUTSIDE the probe's except block — raising kills the job."""

    class _Opaque:  # no `.dialect` at all, like some proxies/stubs
        pass

    assert dm._day_expr(_Opaque()) == "date_trunc('day', ts)::date"


def _sqlite_cache_with_full_session(path):
    """A file-backed SQLite cache holding one complete 375-bar session.

    Full, because the threshold is what decides coverage: 338 of 375 minutes
    for 1min. A thin day is deliberately *not* covered (that is the point of
    the raised fraction), so a small fixture would prove nothing.
    """
    from sqlalchemy import create_engine, text

    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE market_data_cache ("
                " symbol TEXT, timeframe TEXT, ts TEXT, "
                " UNIQUE (symbol, timeframe, ts))"
            )
        )
        conn.execute(
            text("INSERT INTO market_data_cache (symbol, timeframe, ts) VALUES (:s, :t, :ts)"),
            [
                {
                    "s": "RELIANCE",
                    "t": "1min",
                    "ts": f"2026-09-30 {9 + m // 60:02d}:{m % 60:02d}:00",
                }
                for m in range(15, 15 + 375)
            ],
        )
    return engine


def test_the_probe_runs_on_sqlite(tmp_path):
    """The real thing: a file-backed SQLite cache, the default profile."""
    engine = _sqlite_cache_with_full_session(tmp_path / "probe.db")
    try:
        covered = dm._probe_covered_days(
            engine, ["RELIANCE"], "1min", "2026-09-01", "2026-09-30"
        )
    finally:
        engine.dispose()

    assert covered == {"RELIANCE": {date(2026, 9, 30)}}, (
        "a fully-stored day must be reported as covered on SQLite too"
    )


def test_the_skip_actually_fires_on_sqlite(tmp_path):
    """The end-to-end consequence: one probe + one comparison = zero refetches."""
    engine = _sqlite_cache_with_full_session(tmp_path / "probe2.db")
    try:
        covered = dm._probe_covered_days(
            engine, ["RELIANCE"], "1min", "2026-09-01", "2026-09-30"
        )
    finally:
        engine.dispose()

    window = [(datetime(2026, 9, 30), datetime(2026, 9, 30, 23, 59))]
    assert dm._windows_needing_fetch(window, covered["RELIANCE"]) == []


# ---------------------------------------------------------------------------
# _fetch_bars_chunked honours an explicit windows list
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, status: int = 200, payload: dict | None = None):
        self.status_code = status
        self._payload = payload if payload is not None else {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise dm.requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self) -> dict:
        return self._payload


def _candles(n: int = 1) -> dict:
    return {"data": {"candles": [["t", 1.0, 2.0, 3.0, 4.0, 5] for _ in range(n)]}}


def test_one_day_window_requests_the_full_session(monkeypatch):
    # mStock's `to` date is END-EXCLUSIVE (live 2026-10-06: from==to returned
    # zero bars; Sep 30..Oct 2 returned Sep 30 + Oct 1). An inclusive (X, X)
    # window must therefore be requested as from=X&to=X+1.
    seen: list[dict] = []

    def fake_get(url, headers=None, params=None, timeout=None):
        seen.append(dict(params))
        return _Resp(200, _candles(1))

    monkeypatch.setattr(dm.requests, "get", fake_get)
    monkeypatch.setattr(dm.time, "sleep", lambda s: None)

    wins = dm._build_windows("2026-06-01", "2026-06-01", 2)
    bars, errors = dm._fetch_bars_chunked(
        "key", "tok", "11536", "2026-06-01", "2026-06-01", "NSE", "minute", 2,
        windows=wins, workers=1,
    )
    assert errors == 0 and len(bars) == 1
    assert seen == [{"from": "2026-06-01", "to": "2026-06-02"}]


def test_explicit_windows_fetch_only_those(monkeypatch):
    requested: list[str] = []

    def fake_get(url, headers=None, params=None, timeout=None):
        requested.append(params["from"])
        return _Resp(200, _candles(1))

    monkeypatch.setattr(dm.requests, "get", fake_get)
    monkeypatch.setattr(dm.time, "sleep", lambda s: None)

    # Range spans 3 windows, but we hand the loop only the last two.
    all_wins = dm._build_windows("2026-06-01", "2026-06-07", 2)  # 01..03, 04..06, 07
    bars, errors = dm._fetch_bars_chunked(
        "key", "tok", "11536", "2026-06-01", "2026-06-07", "NSE", "minute", 2,
        windows=all_wins[1:],
        workers=1,
    )
    assert errors == 0
    assert len(bars) == 2
    assert sorted(requested) == ["2026-06-04", "2026-06-07"]  # skipped window never requested


# ---------------------------------------------------------------------------
# Holiday-aware skip (2026-10-04): weekday closures must not re-probe windows
# ---------------------------------------------------------------------------

def test_holiday_weekday_does_not_mark_window_missing():
    # Window B = Thu 04..Sat 06; Fri 05 is a market holiday, everything else
    # covered -> the whole range is "complete" and B is dropped.
    covered = {date(2026, 6, d) for d in (1, 2, 3, 4)}
    holidays = {date(2026, 6, 5)}
    assert dm._windows_needing_fetch(_WINS, covered, holidays) == []


def test_holidays_never_rescue_a_real_missing_weekday():
    # Mon 01 is a holiday and rescued (window A drops), but Fri 05 is a
    # genuine hole (not covered, not a holiday) → window B stays.
    covered = {date(2026, 6, d) for d in (2, 3, 4)}
    holidays = {date(2026, 6, 1)}
    kept = dm._windows_needing_fetch(_WINS, covered, holidays)
    assert [(s.strftime("%m-%d"), e.strftime("%m-%d")) for s, e in kept] == [
        ("06-04", "06-06")
    ]


def test_empty_coverage_fetches_every_window_even_with_holidays():
    # The full-fetch guard stays: with nothing stored we still walk everything
    # (holidays only shrink resume noise, never a genuine first fetch).
    assert dm._windows_needing_fetch(_WINS, set(), {date(2026, 6, 5)}) == _WINS


def test_legacy_two_arg_call_still_works():
    # Signature is backwards compatible (holidays default to empty).
    assert dm._windows_needing_fetch(_WINS, set()) == _WINS


# ---------------------------------------------------------------------------
# _load_market_holidays — the per-job calendar read
# ---------------------------------------------------------------------------

def test_load_market_holidays_normalises_rows_to_dates():
    engine = _FakeEngine(
        rows=[(datetime(2026, 1, 26),), (date(2026, 3, 4),)]
    )
    got = dm._load_market_holidays(engine, "2026-01-01", "2026-12-31")
    assert got == {date(2026, 1, 26), date(2026, 3, 4)}


def test_load_market_holidays_error_returns_empty_never_blocks_fetch():
    engine = _FakeEngine(exc=RuntimeError("relation market_holidays does not exist"))
    assert dm._load_market_holidays(engine, "2026-01-01", "2026-12-31") == set()
