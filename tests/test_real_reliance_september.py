"""End-to-end checks against a REAL broker export, not a fixture.

``tools/out/reliance_1min_sept2026.csv`` is a 1-minute RELIANCE export for
September 2026 — 4,817 bars over 13 sessions, naive IST timestamps, the column
``ts_ist``. It exists in this repo because a fixture cannot catch the bugs that
matter here: a fixture is written by the same person who wrote the parser, with
the same assumptions, so it agrees with the parser by construction. This file
did not. It is what found the ``ts_ist`` alias gap and the "bare ``time`` column
is not a timestamp" corruption.

The tests are skipped when the CSV is absent, so a checkout without the data
file is not a red suite.

What is asserted, and why it is the *right* thing to assert:

* the export's own column names parse without being renamed first;
* the timestamps are IST, i.e. the UTC bucket-offset risk does NOT apply — a
  1day-only check would pass either way, which is precisely why the intraday
  and per-session checks below are the ones that carry weight;
* every stored bar survives the round trip intact;
* the 126 single-price closing-auction bars are stored, not "rejected" as
  corrupt (they are real: o == h == l == c after the 15:15 close);
* resampling 1min -> 1day through the product's own ``DbSource._resample``
  reproduces each session exactly (open 09:15, close 15:28, true high/low,
  summed volume).
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CSV = REPO_ROOT / "tools" / "out" / "reliance_1min_sept2026.csv"
SCRIPT = REPO_ROOT / "scripts" / "ingest_csv_to_mdc.py"
SQL_001 = REPO_ROOT / "db" / "migrations" / "001_initial_schema.sqlite.sql"

pytestmark = pytest.mark.skipif(
    not CSV.exists(), reason="real RELIANCE export not present in this checkout"
)

SYMBOL = "RELIANCE"
EXPECTED_BARS = 4817
EXPECTED_SESSIONS = 13


# ---------------------------------------------------------------------------
# The export as it arrives
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ingest_module():
    """The ingest script, loaded by path (it lives in scripts/, not a package)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("ingest_csv_to_mdc", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["ingest_csv_to_mdc"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def raw(ingest_module):
    """The parsed export: (dataframe with ts+ohlcv, how-the-ts-was-built)."""
    df = ingest_module._read_any(CSV)
    cols = [str(c) for c in df.columns]
    ts, how = ingest_module._parse_timestamps(df, cols)
    out = pd.DataFrame({"ts": ts})
    for field, aliases in ingest_module._OHLCV_ALIASES.items():
        col = ingest_module._find(cols, aliases)
        out[field] = pd.to_numeric(df[col], errors="coerce") if col else 0
    return out, how, cols


def test_the_export_parses_with_its_own_column_names(raw):
    out, how, cols = raw
    assert "ts_ist" in cols, f"fixture drift: columns are now {cols}"
    assert "ts_ist" in how, f"timestamp column not recognised: {how}"
    assert len(out) == EXPECTED_BARS
    assert out["ts"].isna().sum() == 0, "unparsable timestamps in a real export"


def test_the_export_is_ist_so_utc_bucketing_does_not_apply(raw, ingest_module):
    out, _, _ = raw
    verdict, shift, label = ingest_module._detect_timezone(out["ts"])
    assert verdict == "ist", f"expected a naive-IST export, got {label}"
    assert shift == 0


def test_the_export_covers_thirteen_sessions(raw):
    out, _, _ = raw
    sessions = out["ts"].dt.normalize().nunique()
    assert sessions == EXPECTED_SESSIONS
    first = out["ts"].min()
    last = out["ts"].max()
    assert first.strftime("%Y-%m-%d %H:%M") == "2026-09-02 09:15"
    assert last.strftime("%Y-%m-%d %H:%M") == "2026-09-30 15:28"


def test_an_intraday_session_starts_at_the_nse_open(raw):
    """The intraday evidence, which 1day/1week resampling cannot provide.

    A UTC-stored file would put this at 03:45 — and a 1day resample would still
    look perfect, because the UTC day boundary happens to sit outside the
    session. So assert the minute-level shape directly.
    """
    out, _, _ = raw
    by_session = out["ts"].dt.strftime("%H:%M").groupby(out["ts"].dt.normalize()).min()
    assert set(by_session.unique()) == {"09:15"}, f"session starts: {sorted(set(by_session))}"


# ---------------------------------------------------------------------------
# Ingest into a real SQLite database, through the real CLI
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ingested(tmp_path_factory):
    """Run the ingest CLI as a user would, against a fresh SQLite database."""
    db = tmp_path_factory.mktemp("reliance") / "reliance.db"
    conn = sqlite3.connect(db)
    try:
        conn.executescript(SQL_001.read_text())
    finally:
        conn.close()

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--csv",
            str(CSV),
            "--symbol",
            SYMBOL,
            "--timeframe",
            "1min",
            "--source",
            "mstock",
        ],
        cwd=REPO_ROOT,
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(REPO_ROOT / "src"),
            "FORWARD_TEST_DB_URL": f"sqlite:///{db}",
        },
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "storage timezone" in proc.stdout
    return db, proc.stdout


def _load(db: Path) -> pd.DataFrame:
    conn = sqlite3.connect(db)
    try:
        return pd.read_sql_query(
            "SELECT ts, open, high, low, close, volume FROM market_data_cache "
            "WHERE symbol = ? AND timeframe = '1min' ORDER BY ts",
            conn,
            params=(SYMBOL,),
        )
    finally:
        conn.close()


def test_every_row_in_the_export_lands_in_the_database(ingested, raw):
    db, _ = ingested
    stored = _load(db)
    assert len(stored) == EXPECTED_BARS
    assert stored["ts"].nunique() == EXPECTED_BARS, "primary key collision on ts"


def test_stored_values_match_the_file_bar_for_bar(ingested, raw):
    """No rounding, no re-filling, no silent reordering."""
    db, _ = ingested
    out, _, _ = raw
    stored = _load(db)
    expected = out.dropna(subset=["ts"]).sort_values("ts").reset_index(drop=True)
    assert list(stored["ts"]) == list(expected["ts"].dt.strftime("%Y-%m-%d %H:%M:%S"))
    for col in ("open", "high", "low", "close", "volume"):
        diff = (stored[col] - expected[col].to_numpy()).abs().max()
        assert diff < 1e-6, f"{col} differs by {diff}"


def test_the_flat_closing_auction_bars_are_stored_not_treated_as_corrupt(ingested):
    """126 bars have o == h == l == c. They are real, and all after 15:15.

    This is a regression guard in both directions: a validator that rejects
    flat bars deletes real data, and one that "repairs" them invents prices.
    """
    db, _ = ingested
    stored = _load(db)
    flat = stored[(stored.open == stored.high) & (stored.high == stored.low)]
    assert len(flat) > 0, "expected the single-price closing auction to be present"
    minutes = pd.to_datetime(flat["ts"]).dt.strftime("%H:%M")
    assert (minutes >= "15:15").all(), (
        f"flat bars outside the closing auction window: {sorted(set(minutes))}"
    )


def test_ohlc_invariants_hold_for_every_stored_bar(ingested):
    db, _ = ingested
    s = _load(db)
    assert (s["high"] >= s[["open", "close"]].max(axis=1)).all()
    assert (s["low"] <= s[["open", "close"]].min(axis=1)).all()
    assert (s["high"] >= s["low"]).all()
    assert (s[["open", "high", "low", "close"]] > 0).all().all()


# ---------------------------------------------------------------------------
# 1min -> 1day through the product's own resampler
# ---------------------------------------------------------------------------


def test_resampled_daily_bars_reconstruct_each_session(ingested):
    """The acceptance criterion: one month of 1-minute bars -> correct daily bars.

    Uses ``DbSource._resample`` — the code the backtest actually calls — rather
    than pandas directly, so a divergence between "what the DB holds" and "what
    the strategy sees" cannot hide here.
    """
    db, _ = ingested
    from backtest.data.db_source import DbSource

    stored = _load(db)
    stored["ts"] = pd.to_datetime(stored["ts"])
    stored = stored.set_index("ts").sort_index()

    src = DbSource.__new__(DbSource)  # no engine needed: _resample is pure
    daily = src._resample(stored, "1day").reset_index()
    daily = daily.rename(columns={daily.columns[0]: "session"})

    assert len(daily) == EXPECTED_SESSIONS, f"got {len(daily)} daily bars"

    # Independent recomputation straight from the stored minutes.
    grouped = stored.groupby(stored.index.normalize())
    expected = pd.DataFrame(
        {
            "open": grouped["open"].first(),
            "high": grouped["high"].max(),
            "low": grouped["low"].min(),
            "close": grouped["close"].last(),
            "volume": grouped["volume"].sum(),
        }
    ).reset_index(drop=True)

    for col in ("open", "high", "low", "close", "volume"):
        diff = (daily[col].to_numpy(dtype=float) - expected[col].to_numpy(dtype=float))
        assert abs(diff).max() < 1e-6, f"daily {col} differs by {abs(diff).max()}"


def test_the_first_daily_bar_has_the_expected_shape(ingested):
    """A hand-checkable bar, so a failure says which number is wrong."""
    db, _ = ingested
    stored = _load(db)
    day1 = stored[pd.to_datetime(stored["ts"]).dt.normalize() == pd.Timestamp("2026-09-02")]
    assert len(day1) == 371
    assert day1["open"].iloc[0] == pytest.approx(1298.00, abs=1e-6)
    assert day1["high"].max() == pytest.approx(1321.80, abs=1e-6)
    assert day1["low"].min() == pytest.approx(1293.60, abs=1e-6)
    assert day1["close"].iloc[-1] == pytest.approx(1313.10, abs=1e-6)
    assert day1["volume"].sum() == pytest.approx(26013588.0, abs=1e-6)


#: The whole month, recomputed from the stored minutes with pandas alone:
#: session -> (open, high, low, close, volume). Independent of the resampler.
GOLDEN_DAILY = [
    ("2026-09-02", 1298.0, 1321.8, 1293.6, 1313.1, 26013588.0),
    ("2026-09-03", 1313.1, 1316.7, 1302.5, 1302.5, 18046121.0),
    ("2026-09-08", 1304.1, 1306.8, 1288.0, 1294.9, 18015417.0),
    ("2026-09-09", 1283.5, 1294.7, 1277.2, 1279.0, 22850620.0),
    ("2026-09-11", 1267.0, 1267.4, 1253.3, 1257.5, 16068444.0),
    ("2026-09-15", 1252.5, 1259.1, 1235.3, 1235.3, 25173543.0),
    ("2026-09-17", 1244.8, 1253.4, 1238.5, 1243.9, 14543457.0),
    ("2026-09-18", 1245.0, 1247.3, 1226.4, 1226.4, 18142655.0),
    ("2026-09-21", 1234.1, 1248.9, 1232.5, 1247.4, 18916485.0),
    ("2026-09-23", 1242.0, 1252.8, 1239.2, 1248.0, 15574299.0),
    ("2026-09-24", 1236.6, 1241.6, 1219.2, 1219.2, 27009182.0),
    ("2026-09-29", 1193.8, 1198.3, 1182.0, 1182.0, 35254204.0),
    ("2026-09-30", 1182.0, 1196.5, 1181.7, 1187.0, 29722953.0),
]


def test_resample_matches_an_independently_computed_month_of_daily_bars(ingested):
    """The resampler's output, against numbers computed by pandas directly.

    ``test_resampled_daily_bars_reconstruct_each_session`` proves the resampler
    agrees with a groupby *inside this repo*. This one pins the resulting bars
    to a literal table, so a fault in both would still have to coincide with
    the numbers below to pass.
    """
    db, _ = ingested
    from backtest.data.db_source import DbSource

    stored = _load(db)
    stored["ts"] = pd.to_datetime(stored["ts"])
    stored = stored.set_index("ts").sort_index()

    daily = DbSource.__new__(DbSource)._resample(stored, "1day").reset_index()
    daily = daily.rename(columns={daily.columns[0]: "session"})

    assert [str(s)[:10] for s in daily["session"]] == [r[0] for r in GOLDEN_DAILY]
    for row, (session, o, h, low, c, v) in zip(daily.itertuples(), GOLDEN_DAILY):
        assert row.open == pytest.approx(o, abs=1e-6), f"{session} open"
        assert row.high == pytest.approx(h, abs=1e-6), f"{session} high"
        assert row.low == pytest.approx(low, abs=1e-6), f"{session} low"
        assert row.close == pytest.approx(c, abs=1e-6), f"{session} close"
        assert row.volume == pytest.approx(v, abs=1e-3), f"{session} volume"
