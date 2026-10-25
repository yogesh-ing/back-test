"""Daily/weekly cache bars carry no time-of-day information, so the writer
anchors them to the NSE session open in exchange wall clock.

mStock returns both ``...T00:00:00Z`` (05:30 IST) and ``...T09:15:00+05:30``
for candles inside one symbol's own ``1day`` series. Stored that way, a UTC
``ts::date`` and an IST ``ts::date`` disagree for every early-anchored row, and
the nightly rollup meets a convention seam mid-series.
"""

from datetime import timezone

import pytest

from backtest.data.base import bar_timestamp

DATE_ONLY_UTC = "2025-01-01T00:00:00Z"  # arrives as 05:30 IST wall clock
SESSION_OPEN_IST = "2025-01-01T09:15:00+05:30"
MUHURAT_IST = "2025-10-21T13:45:00+05:30"  # a short session prints late


def _utc_wall(dt) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M") + "Z"


@pytest.mark.parametrize("raw", [DATE_ONLY_UTC, SESSION_OPEN_IST, "2025-01-01"])
def test_daily_bars_anchor_to_session_open(raw):
    ts = bar_timestamp(raw, "1day")
    assert ts.tzinfo == timezone.utc
    assert _utc_wall(ts) == "2025-01-01 03:45Z"  # 09:15 IST expressed in UTC


def test_daily_anchor_is_idempotent():
    once = bar_timestamp(DATE_ONLY_UTC, "1day")
    assert bar_timestamp(once, "1day") == once


def test_short_session_bar_snaps_back_to_the_session_open():
    assert _utc_wall(bar_timestamp(MUHURAT_IST, "1day")) == "2025-10-21 03:45Z"


def test_weekly_bars_use_the_same_anchor():
    assert _utc_wall(bar_timestamp(DATE_ONLY_UTC, "1week")) == "2025-01-01 03:45Z"


def test_intraday_bars_keep_their_own_clock():
    assert _utc_wall(bar_timestamp("2025-01-01T09:31:00+05:30", "1min")) == "2025-01-01 04:01Z"
    assert _utc_wall(bar_timestamp(DATE_ONLY_UTC, "1min")) == "2025-01-01 00:00Z"


def test_omitted_timeframe_is_unchanged_behaviour():
    assert _utc_wall(bar_timestamp(DATE_ONLY_UTC)) == "2025-01-01 00:00Z"


# --- the at-rest repair's planner -------------------------------------------------
#
# ``scripts/normalize_daily_bar_ts.py`` is not an importable module, so load it by
# path. Its job is to leave exactly one bar per session date, so a wrong collision
# decision either loses a bar or raises on the unique bar index mid-repair.

import importlib.util  # noqa: E402
from pathlib import Path  # noqa: E402

_REPAIR = Path(__file__).resolve().parents[2] / "scripts" / "normalize_daily_bar_ts.py"
_spec = importlib.util.spec_from_file_location("normalize_daily_bar_ts", _REPAIR)
repair = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(repair)


def _row(data_id: int, raw: str) -> dict:
    return {
        "data_id": data_id,
        "symbol": "TATA",
        "exchange": "NSE",
        "timeframe": "1day",
        "ts": bar_timestamp(raw),
    }


def test_anchor_of_keeps_the_ist_calendar_day():
    for raw in (DATE_ONLY_UTC, MUHURAT_IST, "2025-10-21T20:00:00+05:30"):
        ts = bar_timestamp(raw)
        ist_day = ts.astimezone(repair.IST).date()
        assert repair.anchor_of(ts).astimezone(repair.IST).date() == ist_day


def test_plan_moves_every_off_anchor_row():
    rows = [_row(1, DATE_ONLY_UTC), _row(2, "2025-01-02T00:00:00Z")]
    updates, deletes, notes = repair.plan(rows)
    assert deletes == [] and notes == []
    assert {u[1] for u in updates} == {r["ts"] for r in rows}
    assert all(repair.is_anchored(u[2]) for u in updates)


def test_plan_drops_the_stale_twin_of_an_already_anchored_bar():
    # The series seam: mStock sent 09:15 for the newer chunk and 05:30 for the
    # older one, both landing on the same session date.
    anchored = _row(1, SESSION_OPEN_IST)
    stale = _row(2, DATE_ONLY_UTC)
    updates, deletes, notes = repair.plan([anchored, stale])
    assert updates == []
    assert deletes == [(stale["data_id"], stale["ts"])]
    assert len(notes) == 1


def test_plan_keeps_one_bar_when_two_off_anchor_rows_share_a_date():
    a = _row(1, "2025-01-02T00:00:00Z")  # 05:30 IST on the 2nd
    b = _row(2, "2025-01-02T13:45:00+05:30")  # muhurat-clock twin
    updates, deletes, notes = repair.plan([a, b])
    assert len(updates) == 1 and len(deletes) == 1 and len(notes) == 1
    moved_id, moved_from, moved_to = updates[0]
    assert moved_id == b["data_id"]  # the latest stamp survives
    assert deletes == [(a["data_id"], a["ts"])]
    assert moved_to == repair.anchor_of(b["ts"]) != b["ts"]
