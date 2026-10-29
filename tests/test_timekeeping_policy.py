"""Timekeeping policy — the guard against the 2026-10-27 mixed-clock trade rows.

Three jobs, in order:

1. **Contract** (:class:`TestTimekeepingContract`) — what
   :mod:`backtest.timekeeping` promises: aware UTC for durability, IST for the
   calendar, and a hard refusal of any undeclared naive clock.
2. **Regression proofs** (:class:`TestIncidentRegressions`) — the three
   symptoms that made the bug visible: a bar clock read as UTC, a square-off
   gate that could never fire inside a session, and a one-bar trade recorded as
   a 330-minute hold.
3. **The ratchet** (:class:`TestClockRatchet`) — an AST sweep of
   ``src/backtest`` that fails when a call site re-introduces one of the four
   patterns this incident came from. Counts are frozen at the post-fix baseline,
   so the test tolerates a decrease (drop the entry, don't leave it stale) and
   fails on an increase.
"""

from __future__ import annotations

import ast
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from backtest.options.expiry import ExpiryManager
from backtest.timekeeping import (
    EXCHANGE_TZ,
    AmbiguousTimeError,
    duration_seconds,
    ensure_exchange,
    ensure_utc,
    from_market_clock,
    from_stored_utc,
    iso_utc,
    market_is_open,
    parse_utc_iso,
    session_date,
)

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "backtest"

#: 09:15 IST on 2026-10-27 expressed the two ways the feed and the wall clock
#: spell the same instant before the fix conflated them.
BAR_IST = "2026-10-27 09:15:00"
BAR_UTC = datetime(2026, 10, 27, 3, 45, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 1. Module contract
# ---------------------------------------------------------------------------

class TestTimekeepingContract:
    def test_now_utc_is_aware(self):
        from backtest.timekeeping import now_utc

        assert now_utc().tzinfo is not None

    def test_ensure_utc_refuses_naive(self):
        """The whole incident is one un-declared naive datetime; boundaries say no."""
        with pytest.raises(AmbiguousTimeError):
            ensure_utc(datetime(2026, 10, 27, 9, 15))

    def test_ensure_utc_converts_without_shifting_the_instant(self):
        ist = datetime(2026, 10, 27, 9, 15, tzinfo=EXCHANGE_TZ)
        assert ensure_utc(ist) == BAR_UTC

    def test_ensure_exchange_refuses_naive(self):
        with pytest.raises(AmbiguousTimeError):
            ensure_exchange(datetime(2026, 10, 27, 9, 15))

    def test_from_market_clock_reads_naive_as_exchange_time(self):
        assert from_market_clock(BAR_IST) == BAR_UTC

    def test_from_market_clock_passes_aware_values_through(self):
        aware = datetime(2026, 10, 27, 9, 15, tzinfo=EXCHANGE_TZ)
        assert from_market_clock(aware) == BAR_UTC

    def test_from_market_clock_handles_epoch_seconds_and_millis(self):
        assert from_market_clock(1_761_342_900) == datetime.fromtimestamp(
            1_761_342_900, timezone.utc
        )
        assert from_market_clock(1_761_342_900_000) == datetime.fromtimestamp(
            1_761_342_900, timezone.utc
        )

    def test_the_two_doors_differ_by_exactly_the_ist_offset(self):
        """from_market_clock / from_stored_utc must stay the only two ways in."""
        naive = datetime(2026, 10, 27, 9, 15)
        assert from_stored_utc(naive) - from_market_clock(naive) == timedelta(
            hours=5, minutes=30
        )

    def test_session_date_is_the_exchange_date(self):
        # 00:30 IST belongs to the 27th; the UTC date would say the 26th.
        assert session_date(datetime(2026, 10, 26, 19, 0, tzinfo=timezone.utc)) == date(
            2026, 10, 27
        )
        assert session_date(BAR_UTC) == date(2026, 10, 27)

    def test_iso_utc_carries_an_explicit_offset(self):
        """A naive ISO string is parsed as *local* time by the browser, so every
        client-side +5:30 correction double-shifts. The offset is the fix."""
        text = iso_utc(BAR_UTC)
        assert text.endswith("+00:00")
        assert parse_utc_iso(text) == BAR_UTC

    def test_parse_utc_iso_refuses_naive_text(self):
        with pytest.raises(AmbiguousTimeError):
            parse_utc_iso("2026-10-27 09:15:00")

    def test_duration_seconds_spans_the_two_clocks(self):
        entry = "2026-10-27T03:45:00+00:00"  # UTC-stamped entry
        exit_ = "2026-10-27 09:16:00"  # IST-stamped exit, one bar later
        assert duration_seconds(entry, exit_) == 60.0

    def test_market_is_open_uses_the_exchange_clock(self):
        assert market_is_open(BAR_UTC) is True  # 09:15 IST, session opens
        assert market_is_open(datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc)) is True
        assert market_is_open(datetime(2026, 10, 27, 10, 1, tzinfo=timezone.utc)) is False
        assert market_is_open(datetime(2026, 10, 27, 1, 0, tzinfo=timezone.utc)) is False


# ---------------------------------------------------------------------------
# 2. The three symptoms, proved fixed at the writer
# ---------------------------------------------------------------------------

class TestIncidentRegressions:
    def test_bar_clock_is_the_true_instant_not_utc_wall_time(self):
        """The bridge used to hand back the IST string as if it were UTC,
        labelling a 09:15 bar as 09:15Z — five and a half hours in the future."""
        from backtest.forward.options_bridge import OptionsBridge

        bar_dt = OptionsBridge._bar_datetime(BAR_IST)
        assert bar_dt == BAR_UTC
        assert bar_dt.tzinfo is not None
        assert session_date(bar_dt) == date(2026, 10, 27)

    @pytest.mark.parametrize(
        "now",
        [
            datetime(2026, 10, 27, 15, 20, tzinfo=EXCHANGE_TZ),  # spelled IST
            datetime(2026, 10, 27, 9, 50, tzinfo=timezone.utc),  # spelled UTC
        ],
        ids=["ist-clock", "utc-clock"],
    )
    def test_eod_squareoff_gate_fires_inside_the_session(self, now):
        """Pre-fix the cutoff was a naive 15:30 compared against a UTC ``now``,
        reachable only at 21:00 IST — so no structure was ever squared off."""
        assert ExpiryManager._is_expiring(date(2026, 10, 27), now, 20) is True

    def test_eod_squareoff_gate_stays_closed_before_the_window(self):
        early = datetime(2026, 10, 27, 9, 9, tzinfo=timezone.utc)  # 14:39 IST
        assert ExpiryManager._is_expiring(date(2026, 10, 27), early, 20) is False

    def test_one_bar_trade_holds_for_one_minute_end_to_end(self):
        """The headline number: ``holding_period_minutes`` said 330 for a trade
        that existed for 60 seconds."""
        row = _capture_inserted_trade(
            {
                "symbol": "NIFTY 23400 CE",
                "entry_ts": iso_utc(BAR_UTC),
                "exit_ts": iso_utc(BAR_UTC + timedelta(minutes=1)),
            }
        )
        assert row.holding_period_minutes == 1

    def test_trade_writers_hand_the_db_aware_datetimes(self):
        """``trades`` columns are TIMESTAMPTZ under a Asia/Calcutta session
        timezone: a naive value is reinterpreted, an aware one is not."""
        row = _capture_inserted_trade(
            {
                "symbol": "NIFTY 23400 CE",
                "entry_ts": "2026-10-27T03:45:00+00:00",
                "exit_ts": "2026-10-27T03:46:00+00:00",
            }
        )
        assert row.entry_time.tzinfo is not None
        assert row.exit_time.tzinfo is not None
        assert row.entry_time.utcoffset() == timedelta(0)

    def test_legacy_naive_rows_still_read_as_utc(self):
        """Pre-fix writers used utcnow(), so old JSON/state values are naive UTC.
        Refusing them would drop closed trades from the permanent history."""
        assert from_stored_utc("2026-09-21T06:31:06") == datetime(
            2026, 9, 21, 6, 31, 6, tzinfo=timezone.utc
        )


def _capture_inserted_trade(trade: dict):
    """Run LiveTradePersister._insert_trade against a recording fake DB."""
    from backtest.forward.trade_persistence import LiveTradePersister

    captured: list = []

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def add(self, obj):
            captured.append(obj)

    class _Db:
        def session(self):
            return _Session()

    class _Cfg:
        strategy_name = "test"

    class _Runner:
        instance_id = "inst"
        config = _Cfg()

    LiveTradePersister(_Db())._insert_trade("portfolio-id", _Runner(), trade)
    assert len(captured) == 1
    return captured[0]


# ---------------------------------------------------------------------------
# 3. The ratchet — no new sites may re-introduce an undeclared clock
# ---------------------------------------------------------------------------

#: Frozen at the post-fix baseline. ``datetime.utcnow()`` is at zero and must
#: stay there; the rest are grandfathered debt, listed so a new use is a
#: deliberate edit to this file rather than an accident.
BARE_NOW_BASELINE = {
    "api/forward.py": 4,
    "brokers/dhan.py": 1,
    "brokers/mstock.py": 1,
    "brokers/upstox.py": 1,
    "cli.py": 4,
    "data/coverage.py": 1,
    "data/upstox_live_feed.py": 1,
    "forward/state_store.py": 1,
    "pine/converter.py": 1,
    "reporting/email_report.py": 1,
    "simulator/fees.py": 2,
    "simulator/trade_analyzer.py": 1,
    "validate_paper_trading.py": 1,
}

#: Broker session-expiry bookkeeping only — these compare a token's lifetime
#: against the operator's local wall clock and never reach a trade record.
STRIP_TZ_BASELINE = {
    "brokers/dhan.py": 2,
    "brokers/mstock.py": 1,
    "brokers/upstox.py": 3,
}

#: Duplicated IST definitions that should be imported from timekeeping.
LOCAL_IST_BASELINE = {
    "alerts/notifier.py": 1,
    "analytics/portfolio.py": 1,
    "api/data_manager.py": 1,
    "brokers/upstox.py": 1,
    "forward/paper_runner.py": 1,
    "forward/watch_export.py": 1,
}


_MISSING = object()


def _call_sites(rel_path: str) -> dict[str, list[int]]:
    """Classify the clock-hazard call nodes in one module."""
    tree = ast.parse((SRC_ROOT / rel_path).read_text(encoding="utf-8"))
    found: dict[str, list[int]] = {"utcnow": [], "bare_now": [], "strip_tz": [], "local_ist": []}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        attr = getattr(func, "attr", None)
        owner = getattr(func, "value", None)
        owner_name = getattr(owner, "id", None) or getattr(owner, "attr", None)
        if attr == "utcnow" and owner_name in ("datetime", "DateTime"):
            found["utcnow"].append(node.lineno)
        elif attr == "now" and owner_name in ("datetime", "DateTime"):
            if not node.args and not node.keywords:
                found["bare_now"].append(node.lineno)
        if attr == "replace" and any(
            k.arg == "tzinfo" and getattr(k.value, "value", _MISSING) is None
            for k in node.keywords
        ):
            found["strip_tz"].append(node.lineno)
        if _defines_ist(node):
            found["local_ist"].append(node.lineno)
    return found


def _defines_ist(node: ast.Call) -> bool:
    """Match ``timezone(timedelta(hours=5, minutes=30))`` — the shape that
    duplicates :data:`backtest.timekeeping.EXCHANGE_TZ`. Plain
    ``+ timedelta(hours=5, minutes=30)`` arithmetic on a UTC value is a
    conversion, not a definition, and is not counted."""
    if getattr(node.func, "attr", None) != "timezone" and getattr(
        node.func, "id", None
    ) != "timezone":
        return False
    inner = node.args[0] if node.args else next(
        (k.value for k in node.keywords if k.arg == "offset"), None
    )
    if not isinstance(inner, ast.Call):
        return False
    name = getattr(inner.func, "id", None) or getattr(inner.func, "attr", None)
    kw = {k.arg: getattr(k.value, "value", _MISSING) for k in inner.keywords}
    return name == "timedelta" and kw.get("hours") == 5 and kw.get("minutes") == 30


@pytest.fixture(scope="module")
def sweep() -> dict[str, dict[str, list[int]]]:
    """Every module under ``src/backtest`` except the policy module itself."""
    result = {}
    for path in sorted(SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(SRC_ROOT).as_posix()
        if rel == "timekeeping.py" or "__pycache__" in path.parts:
            continue
        result[rel] = _call_sites(rel)
    return result


def _exceeds(baseline: dict[str, int], sweep: dict, key: str) -> list[str]:
    offenders = []
    for rel, sites in sweep.items():
        count = len(sites[key])
        if count > baseline.get(rel, 0):
            offenders.append(f"{rel}: {count} (allowed {baseline.get(rel, 0)}) lines {sites[key]}")
    return offenders


class TestClockRatchet:
    def test_no_utcnow_anywhere(self, sweep):
        """``datetime.utcnow()`` returns a *naive* value — the exact form that
        corrupted the trade rows. Zero tolerance."""
        offenders = [rel for rel, sites in sweep.items() if sites["utcnow"]]
        assert offenders == []

    def test_bare_datetime_now_stays_at_baseline(self, sweep):
        """Local-clock reads are fine for filenames and display, never for a
        trade record. New sites need a deliberate entry in the baseline."""
        assert _exceeds(BARE_NOW_BASELINE, sweep, "bare_now") == []

    def test_tzinfo_stripping_is_confined_to_broker_sessions(self, sweep):
        offenders = _exceeds(STRIP_TZ_BASELINE, sweep, "strip_tz")
        assert offenders == []

    def test_ist_is_not_redefined_in_new_modules(self, sweep):
        offenders = _exceeds(LOCAL_IST_BASELINE, sweep, "local_ist")
        assert offenders == []

    def test_sweep_actually_ran(self, sweep):
        """A silently-empty sweep would make every ratchet above vacuous."""
        assert len(sweep) > 100
        assert "options/expiry.py" in sweep
        assert "forward/options_bridge.py" in sweep
