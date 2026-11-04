"""Which clock a stored bar is read back on (finding BT-01).

``market_data_cache.ts`` is ``timestamptz`` and holds the bar's *instant*, which
is right: a 09:15 IST minute is stored as 03:45Z. Reading that column straight
into the engine is where it went wrong, because two things then follow, both
silent:

* pandas bins a tz-aware index on the **UTC** grid, so a ``1hour`` bar opens at
  08:30 IST and a ``4hour`` one is built out of quarter-hour stubs — 39 bars
  where the session holds 26 (the table in
  docs/DATA-INGEST-AND-TIMEFRAMES.md §"Two conventions were in play");
* every clock face downstream prints UTC, so the trade ledger's entry stamp read
  ``03:45`` for a trade taken at the open.

``1day``/``1week`` are unaffected either way — a session never crosses UTC
midnight — which is exactly why a daily-only check passed for months while the
intraday runs were offset. These tests therefore assert on the intraday buckets.

The fixtures store text *with* an explicit ``+00:00`` offset so that
``pd.to_datetime`` produces the aware index a ``timestamptz`` column yields in
production. The other fixtures in this directory write naive text, which is the
passthrough branch, and ``test_a_naive_series_is_left_alone`` pins that it stays
a passthrough: localising a value that never had a zone would invent one.
"""

from __future__ import annotations

import pandas as pd
import pytest
from sqlalchemy import create_engine, text

from backtest.data.db_source import DbSource, to_exchange_wall_clock

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


def _session_minutes() -> list[dict[str, object]]:
    """One NSE session, 09:15–15:29 IST, written the way Postgres holds it.

    375 minutes at a fixed price: a constant series keeps every assertion below
    about *when* the bars are, not what they say. The offset is what matters —
    03:45Z is the same instant as 09:15 IST, and only one of them is a clock an
    NSE participant reads.
    """
    rows = []
    for minute in range(375):
        total = 3 * 60 + 45 + minute  # 03:45Z onwards
        stamp = f"2026-09-30 {total // 60:02d}:{total % 60:02d}:00+00:00"
        rows.append({"ts": stamp, "tf": "1min"})
    # The same session as a native daily row, anchored at the session open.
    rows.append({"ts": "2026-09-30 03:45:00+00:00", "tf": "1day"})
    return rows


@pytest.fixture()
def aware_cache(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'utc_bars.db'}")
    with engine.begin() as conn:
        conn.execute(text(_MDC_DDL))
        conn.execute(
            text(
                "INSERT INTO market_data_cache "
                "(symbol, exchange, timeframe, ts, open, high, low, close, volume, source) "
                "VALUES ('RELIANCE', 'NSE', :tf, :ts, 100, 101, 99, 100.5, 1000, 'test')"
            ),
            _session_minutes(),
        )
    yield engine
    engine.dispose()


@pytest.fixture()
def naive_cache(tmp_path):
    """The same session written as naive IST text (an export, a CSV, SQLite)."""
    engine = create_engine(f"sqlite:///{tmp_path / 'ist_bars.db'}")
    with engine.begin() as conn:
        conn.execute(text(_MDC_DDL))
        conn.execute(
            text(
                "INSERT INTO market_data_cache "
                "(symbol, exchange, timeframe, ts, open, high, low, close, volume, source) "
                "VALUES ('RELIANCE', 'NSE', '1min', :ts, 100, 101, 99, 100.5, 1000, 'test')"
            ),
            [
                {
                    "ts": f"2026-09-30 {9 + ((15 + m) // 60):02d}:{(15 + m) % 60:02d}:00"
                }
                for m in range(375)
            ],
        )
    yield engine
    engine.dispose()


def _source(engine) -> DbSource:
    src = DbSource.__new__(DbSource)  # no engine/URL resolution needed
    src._engine = engine
    return src


# ---------------------------------------------------------------------------
# the rule itself
# ---------------------------------------------------------------------------


def test_an_aware_utc_series_is_stamped_on_the_exchange_clock():
    aware = pd.to_datetime(
        pd.Series(["2026-09-30 03:45:00+00:00", "2026-09-30 09:59:00+00:00"])
    )
    assert aware.dt.tz is not None, "the fixture must reproduce the timestamptz read"

    out = to_exchange_wall_clock(aware)

    assert out.dt.tz is None
    assert [str(t) for t in out] == ["2026-09-30 09:15:00", "2026-09-30 15:29:00"]


def test_a_naive_series_is_left_alone():
    """A value with no zone has no offset to convert; localising would invent one.

    This guard is load-bearing for every naive fixture in tests/data (SQLite
    TEXT columns), which is how those suites reach ``get_candles`` at all.
    """
    naive = pd.to_datetime(pd.Series(["2026-09-30 09:15:00", "2026-09-30 15:29:00"]))

    out = to_exchange_wall_clock(naive)

    assert [str(t) for t in out] == ["2026-09-30 09:15:00", "2026-09-30 15:29:00"]


# ---------------------------------------------------------------------------
# through the read path a backtest actually uses
# ---------------------------------------------------------------------------


def test_get_candles_returns_the_session_clock(aware_cache):
    out = _source(aware_cache).get_candles("RELIANCE", "2026-09-30", "2026-09-30", "1min")

    assert out.index.tz is None
    assert out.index[0] == pd.Timestamp("2026-09-30 09:15")
    assert out.index[-1] == pd.Timestamp("2026-09-30 15:29")
    assert len(out) == 375


def test_get_candles_leaves_a_naive_store_on_its_own_clock(naive_cache):
    """The identical session, already wall-clock text, must read identically."""
    out = _source(naive_cache).get_candles("RELIANCE", "2026-09-30", "2026-09-30", "1min")

    assert out.index.tz is None
    assert out.index[0] == pd.Timestamp("2026-09-30 09:15")
    assert len(out) == 375


def test_hourly_buckets_open_on_the_exchange_grid(aware_cache):
    """The headline of BT-01: 08:30 IST was a UTC bucket boundary wearing IST clothes."""
    out = _source(aware_cache).get_candles("RELIANCE", "2026-09-30", "2026-09-30", "1hour")

    assert [t.strftime("%H:%M") for t in out.index] == [
        "09:00",
        "10:00",
        "11:00",
        "12:00",
        "13:00",
        "14:00",
        "15:00",
    ], "the first bar of a session is short (45 min) — that is clock alignment, not a stub"


def test_four_hour_buckets_do_not_gain_stub_bars(aware_cache):
    """The worse half of the finding: 39 "4-hour" bars built from 26 real windows.

    A UTC-grid resample cuts the session at 05:30 and 09:30 IST, so the session
    opens and closes with a fragment labelled as a four-hour bar.
    """
    out = _source(aware_cache).get_candles("RELIANCE", "2026-09-30", "2026-09-30", "4hour")

    assert [t.strftime("%H:%M") for t in out.index] == ["08:00", "12:00"]


def test_both_read_paths_stamp_the_same_session_identically(aware_cache):
    """The divergence *was* the bug: bulk converted, single-symbol did not.

    One session, one query each, and any difference in the index means a warmup
    frame and a backtest frame disagree about when the same minute happened —
    which is how ``forward/warmup.py``'s documented "buckets on naive IST"
    contract went stale against the loader.
    """
    single = _source(aware_cache).get_candles("RELIANCE", "2026-09-30", "2026-09-30", "1hour")
    bulk = _source(aware_cache).get_candles_bulk(
        ["RELIANCE"], "2026-09-30", "2026-09-30", "1hour"
    )["RELIANCE"]

    assert list(single.index) == list(bulk.index)
    assert single.index.tz is None and bulk.index.tz is None


def test_a_resampled_daily_bar_is_the_trading_date(aware_cache):
    """Roll 375 minutes up and the bar must belong to the session it came from.

    On a UTC grid this is the one case that *looks* right — the session fits
    inside a UTC day either way — so the assertion is on the date label, which
    is what the ledger, the coverage probe and the annualiser all read.
    """
    out = _source(aware_cache).get_candles("RELIANCE", "2026-09-30", "2026-09-30", "1day")

    assert len(out) == 1
    assert str(out.index[0].date()) == "2026-09-30"
