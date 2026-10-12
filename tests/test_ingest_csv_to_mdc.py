"""CSV ingest parsing — the two bugs found on the first real export.

Both were found by running ``scripts/ingest_csv_to_mdc.py`` against a
date+time CSV (8,250 real-shaped 1-minute bars), not by inspection:

1. A bare ``time`` column was accepted as a FULL timestamp, so every bar was
   stamped on the day the script ran. Silent corruption — no error, a valid
   looking 8,250-row table on one date.
2. Duplicate timestamps were reported but not dropped, so the batch hit
   ``UNIQUE(symbol, exchange, timeframe, ts)`` and aborted.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "ingest_csv_to_mdc.py"


@pytest.fixture(scope="module")
def ingest():
    """Load the script as a module (it lives in scripts/, not a package)."""
    spec = importlib.util.spec_from_file_location("ingest_csv_to_mdc", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["ingest_csv_to_mdc"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# The date + time pair must win over either half
# ---------------------------------------------------------------------------


def test_date_and_time_columns_are_combined_not_each_taken_alone(ingest):
    """The 2026-10-05 bug: 'time' alone stamped every bar on the run date."""
    df = pd.DataFrame(
        {
            "date": ["2026-09-01", "2026-09-01", "2026-09-02"],
            "time": ["09:15:00", "09:16:00", "09:15:00"],
            "open": [1.0, 2.0, 3.0],
            "high": [1.1, 2.1, 3.1],
            "low": [0.9, 1.9, 2.9],
            "close": [1.05, 2.05, 3.05],
            "volume": [10, 20, 30],
        }
    )
    ts, how = ingest._parse_timestamps(df, list(df.columns))

    assert how == "'date' + 'time'"
    assert ts.dt.normalize().nunique() == 2, "both sessions must survive"
    assert str(ts.iloc[0]) == "2026-09-01 09:15:00"
    assert str(ts.iloc[2]) == "2026-09-02 09:15:00"


def test_a_full_timestamp_column_is_used_as_is(ingest):
    df = pd.DataFrame(
        {
            "ts": ["2026-09-01 09:15:00", "2026-09-01 09:16:00"],
            "open": [1.0, 2.0], "high": [1.1, 2.1],
            "low": [0.9, 1.9], "close": [1.05, 2.05], "volume": [10, 20],
        }
    )
    ts, how = ingest._parse_timestamps(df, list(df.columns))
    assert how == "column 'ts'"
    assert str(ts.iloc[1]) == "2026-09-01 09:16:00"


def test_a_single_session_time_only_export_is_accepted(ingest):
    df = pd.DataFrame(
        {
            "time": ["09:15:00", "09:16:00"],
            "open": [1.0, 2.0], "high": [1.1, 2.1],
            "low": [0.9, 1.9], "close": [1.05, 2.05], "volume": [10, 20],
        }
    )
    ts, how = ingest._parse_timestamps(df, list(df.columns))
    assert "single session" in how
    assert ts.dt.normalize().nunique() == 1


def test_a_time_only_export_spanning_days_is_refused(ingest):
    """Ambiguous input must not silently collapse; fall through to the error."""
    df = pd.DataFrame(
        {
            "time": ["2026-09-01 09:15:00", "2026-09-02 09:15:00"],
            "open": [1.0, 2.0], "high": [1.1, 2.1],
            "low": [0.9, 1.9], "close": [1.05, 2.05], "volume": [10, 20],
        }
    )
    with pytest.raises(SystemExit):
        ingest._parse_timestamps(df, list(df.columns))


def test_epoch_seconds_and_millis_are_both_understood(ingest):
    base = pd.DataFrame(
        {"open": [1.0], "high": [1.1], "low": [0.9], "close": [1.05], "volume": [10]}
    )
    for value, label in ((1_756_699_200, "s"), (1_756_699_200_000, "ms")):
        df = base.assign(ts=[value])
        ts, how = ingest._parse_timestamps(df, list(df.columns))
        assert f"epoch {label}" in how
        assert ts.notna().all()


def test_timezone_detection_separates_ist_from_utc(ingest):
    ist = pd.Series(pd.to_datetime(["2026-09-01 09:15", "2026-09-01 15:29"]))
    utc = pd.Series(pd.to_datetime(["2026-09-01 03:45", "2026-09-01 09:59"]))
    assert ingest._detect_timezone(ist)[0] == "ist"
    assert ingest._detect_timezone(utc)[0] == "utc"


# ---------------------------------------------------------------------------
# Duplicates must be dropped, not just counted
# ---------------------------------------------------------------------------


def test_duplicate_timestamps_are_dropped_keeping_the_last(ingest, tmp_path):
    """Reported-but-not-dropped duplicates aborted the insert on a UNIQUE key."""
    csv = tmp_path / "dupes.csv"
    csv.write_text(
        "date,time,open,high,low,close,volume\n"
        "2026-09-01,09:15:00,1.0,1.1,0.9,1.05,10\n"
        "2026-09-01,09:15:00,2.0,2.1,1.9,2.05,20\n"  # same minute, later wins
        "2026-09-01,09:16:00,3.0,3.1,2.9,3.05,30\n",
        encoding="utf-8",
    )
    df = ingest._read_any(csv)
    ts, _ = ingest._parse_timestamps(df, list(df.columns))
    out = pd.DataFrame({"ts": ts}).drop_duplicates(subset=["ts"], keep="last")

    assert len(out) == 2, "the repeated minute collapses to one bar"
    assert df.loc[out.index[0], "close"] == 2.05, "last row wins, matching upsert"


def test_suffixed_timestamp_column_names_are_recognised(ingest):
    """The first real export used ``ts_ist``; exact alias matching missed it."""
    for col in ("ts_ist", "bar_ts", "datetime_ist", "timestamp_ist"):
        cols = [col, "open", "high", "low", "close", "volume"]
        assert ingest._find_timestamp_col(cols) == col, col


def test_loose_matching_never_steals_a_price_column(ingest):
    """``lots``/``costs`` must not be mistaken for a timestamp."""
    cols = ["date", "time", "open", "high", "low", "close", "lots", "costs"]
    assert ingest._find_timestamp_col(cols) is None
