"""Serviceable timeframes — what a stored set can actually answer.

The reported bug: an instrument fetched at 1-minute granularity offered
**1m and nothing else** in the Backtest timeframe dropdown, even though
``DbSource`` had resampled 1-minute bars up to 1day/1week since ticket P4.3.
The data layer could serve all nine canonical timeframes; the metadata the
picker was handed said one.

The rule these tests pin:

* resampling runs one way only — fine bars aggregate into coarse ones, never
  the reverse, so ``1min`` can answer ``1day`` but ``1day`` can never answer
  ``1min`` (that would be inventing prices, not aggregating them);
* a stored timeframe is ALWAYS offered, whatever the range says;
* a derived one is offered only when the base range can fill it, so a week of
  1-minute bars does not advertise ``1week``;
* the two consumers — the coverage endpoint (drop-down) and
  ``DbSource.list_symbols`` (which symbols exist) — agree, because they call
  the same function.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from backtest.data.base import (
    CANONICAL_TIMEFRAMES,
    derive_serviceable_timeframes,
    finest_timeframe,
)
from backtest.data.coverage import load_bar_coverage
from backtest.data.db_source import DbSource

_MDC_DDL = """
CREATE TABLE market_data_cache (
    data_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol    TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    ts        TEXT NOT NULL
)
"""


# ---------------------------------------------------------------------------
# The rule, in isolation
# ---------------------------------------------------------------------------


def test_one_minute_data_serves_every_canonical_timeframe():
    """The reported bug, at unit level: 48,750 one-minute bars, nine answers."""
    assert derive_serviceable_timeframes(["1min"], finest_bars=48_750) == list(
        CANONICAL_TIMEFRAMES
    )


def test_daily_data_can_never_answer_an_intraday_request():
    """Sub-daily bars cannot be built from daily ones — the one-way rule."""
    serviceable = derive_serviceable_timeframes(["1day"], finest_bars=1_000)
    assert serviceable == ["1day", "1week"]
    for intraday in ("1min", "5min", "10min", "15min", "30min", "1hour", "4hour"):
        assert intraday not in serviceable


def test_the_finest_stored_granularity_decides_the_reach():
    """A 30min cache answers 30min and up — not 1min, and not before 30min."""
    assert derive_serviceable_timeframes(["30min"], finest_bars=1_378) == [
        "30min",
        "1hour",
        "4hour",
        "1day",
        "1week",
    ]


def test_a_short_range_does_not_advertise_a_coarser_timeframe_it_cannot_fill():
    """~1 day of 1-minute bars reaches 30min (3 bars) but not 1hour (1 bar)."""
    assert derive_serviceable_timeframes(["1min"], finest_bars=100) == [
        "1min",
        "5min",
        "10min",
        "15min",
        "30min",
    ]


def test_a_stored_timeframe_is_offered_even_when_the_cap_bites():
    """Stored rows physically exist; the range cap must never hide them."""
    serviceable = derive_serviceable_timeframes(["1min", "1week"], finest_bars=3)
    assert "1week" in serviceable and "1min" in serviceable


def test_derivation_returns_canonical_order_finest_first():
    serviceable = derive_serviceable_timeframes(["1day", "1min"], finest_bars=5_000)
    assert serviceable == sorted(serviceable, key=CANONICAL_TIMEFRAMES.index)


def test_unknown_or_empty_stored_sets_derive_nothing():
    assert derive_serviceable_timeframes([]) == []
    assert derive_serviceable_timeframes(None) == []
    assert derive_serviceable_timeframes(["banana"]) == []


def test_aliases_are_accepted_like_everywhere_else():
    """Broker scripts write ``60min``/``day``; the vocabulary normalises them."""
    assert derive_serviceable_timeframes(["60min"], finest_bars=500)[0] == "1hour"
    assert finest_timeframe(["day", "5min"]) == "5min"


# ---------------------------------------------------------------------------
# Consumer 1 — the symbol picker's timeframe drop-down
# ---------------------------------------------------------------------------


@pytest.fixture()
def bar_engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'bars.db'}")
    with engine.begin() as conn:
        conn.execute(text(_MDC_DDL))
    yield engine
    engine.dispose()


def _seed(engine, rows):
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO market_data_cache (symbol, timeframe, ts) VALUES (:s, :t, :ts)"),
            [{"s": s, "t": t, "ts": ts} for s, t, ts in rows],
        )


def _minute_bars(symbol: str, count: int) -> list[tuple[str, str, str]]:
    """``count`` consecutive NSE-session 1-minute stamps (09:15 onward)."""
    from datetime import datetime, timedelta

    start = datetime(2024, 1, 1, 9, 15)
    return [
        (symbol, "1min", (start + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S"))
        for i in range(count)
    ]


def test_coverage_offers_derived_timeframes_and_reports_what_is_stored(bar_engine):
    """The exact fields the picker reads, plus the honest provenance beside it.

    ~2 weeks of 1-minute bars is enough base to build every coarser window.
    """
    _seed(bar_engine, _minute_bars("RELIANCE", 3_750))

    row = load_bar_coverage(bar_engine)["RELIANCE"].as_dict()

    assert row["timeframes_available"] == list(CANONICAL_TIMEFRAMES)
    # ...and the Data tab can still say what was actually downloaded.
    assert row["timeframes_stored"] == ["1min"]
    assert row["bars_by_timeframe"] == {"1min": 3_750}


def test_a_daily_only_symbol_still_offers_exactly_what_it_has(bar_engine):
    """Daily stays daily — plus 1week, which daily bars aggregate cleanly into."""
    _seed(
        bar_engine,
        [("TCS", "1day", f"2024-01-{d:02d}") for d in range(1, 16)],
    )

    row = load_bar_coverage(bar_engine)["TCS"].as_dict()

    assert row["timeframes_available"] == ["1day", "1week"]
    assert "1min" not in row["timeframes_available"]
    assert row["timeframes_stored"] == ["1day"]


# ---------------------------------------------------------------------------
# Consumer 2 — which symbols exist at all
# ---------------------------------------------------------------------------


def _source(engine) -> DbSource:
    """A DbSource with the test engine injected (no DB-URL resolution)."""
    src = DbSource.__new__(DbSource)
    src._engine = engine
    return src


def test_list_symbols_finds_a_one_minute_only_symbol_for_daily_requests(bar_engine):
    """The app logged "0 symbols available" for a cache holding 48,750 bars.

    ``list_symbols`` filtered on ``timeframe = '1day'`` literally, so a
    symbol cached only at 1min was invisible to the boot check, to
    ``/api/symbols`` and to the picker — while a 1day backtest on it worked.
    """
    _seed(bar_engine, _minute_bars("RELIANCE", 3_750))
    src = _source(bar_engine)

    assert src.list_symbols("1day") == ["RELIANCE"]
    assert src.list_symbols("1min") == ["RELIANCE"]


def test_list_symbols_respects_the_one_way_rule(bar_engine):
    _seed(bar_engine, [("TCS", "1day", f"2024-01-{d:02d}") for d in range(1, 16)])
    src = _source(bar_engine)

    assert src.list_symbols("1day") == ["TCS"]
    assert src.list_symbols("1min") == [], "daily bars cannot serve minutes"


def test_list_symbols_can_serve_a_thirty_minute_only_symbol(bar_engine):
    """``_SOURCE_TF_PRIORITY`` used to omit 10min/30min/4hour entirely.

    25 thirty-minute bars is a full session, enough base for a 1day window.
    """
    _seed(
        bar_engine,
        [("INFY", "30min", f"2024-01-01 {9 + i // 2:02d}:{(i % 2) * 30:02d}:00") for i in range(25)],
    )
    src = _source(bar_engine)

    assert src.list_symbols("30min") == ["INFY"]
    assert src.list_symbols("1hour") == ["INFY"]
    assert src.list_symbols("1day") == ["INFY"]


def test_list_symbols_without_a_filter_lists_everything(bar_engine):
    _seed(bar_engine, [("A", "1min", "2024-01-05 09:15"), ("B", "1day", "2024-01-05")])
    src = _source(bar_engine)

    assert src.list_symbols(None) == ["A", "B"]


def test_list_symbols_rejects_an_unknown_timeframe(bar_engine):
    _seed(bar_engine, [("A", "1min", "2024-01-05 09:15")])
    src = DbSource.__new__(DbSource)
    src._engine = bar_engine

    assert src.list_symbols("fortnightly") == []
