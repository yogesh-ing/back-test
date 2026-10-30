"""Sprint 1 / Ticket 1.5 — carry policy, wall-clock square-off, entry window, overnight
attribution (`BUG-39`, `BUG-18`, QA 2026-10-28).

Two failures this file pins:

* **BUG-39** — three monthly legs rode into the next session because the only
  route to the intraday square-off was a bar or a tick. An illiquid strike that
  stopped printing at 14:50 never closed. The fix is a declared ``carry_policy``
  plus a *clock*-driven sweep, so the square-off cannot depend on a quote.
* **BUG-18** — a bar stamped 15:30:30 IST opened a fresh structure after the
  venue had stopped trading, and an overnight carry booked yesterday's
  settlement into today's ``daily_pnl``. The fix is an entry window on the
  market clock and a per-structure prior-close anchor.

Scope honesty: the entry window governs stamps that *are* the exchange clock —
a live broker feed. A synthetic or replay bar carries the fixture's own clock,
and the window is a live-risk rule, not an accounting one (see
``LIVE_FEED_TAGS``). The sweep likewise owns only intraday option books; equity
paper has no square-off contract to sweep.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from backtest.data.source_tags import LIVE_FEED_TAGS, app_source_tag
from backtest.forward.options_bridge import (
    CARRY_INTRADAY,
    CARRY_POSITIONAL,
    EXIT_INTRADAY_SQUAREOFF,
    OptionsBridge,
)
from backtest.forward.paper_runner import (
    STATUS_PAUSED,
    STATUS_RUNNING,
    StrategyRunner,
)
from backtest.forward.portfolio_manager import PortfolioManager
from backtest.forward.risk_supervisor import GlobalRiskConfig
from backtest.forward.trade_persistence import _map_exit_reason
from backtest.options.paper_trading import (
    OptionPaperBroker,
    OptionPosition,
    StructurePosition,
)
from backtest.options.quote_providers import SyntheticChainGenerator, SyntheticQuoteProvider
from backtest.strategy.execution import ExecutionSpecError
from backtest.strategy.intent import Direction, MarketView
from backtest.timekeeping import (
    ENTRY_CUTOFF_TIME,
    EOD_SQUAREOFF_WINDOW_START,
    entry_window_refusal,
    in_eod_squareoff_window,
)
from options_support import wide_gate

_IST = timezone(timedelta(hours=5, minutes=30))

#: Monday 2026-10-05 — a normal NSE session, same day ``options_support`` pins.
SESSION_DAY = date(2026, 10, 5)


def _ist(hour: int, minute: int, second: int = 0, day: int = 5) -> datetime:
    """An exchange instant, handed over as the aware UTC value the app stores."""
    return datetime(2026, 10, day, hour, minute, second, tzinfo=_IST).astimezone(
        timezone.utc
    )


def _ist_text(hour: int, minute: int, second: int = 0, day: int = 5) -> str:
    return datetime(2026, 10, day, hour, minute, second).strftime("%Y-%m-%d %H:%M:%S")


V2: dict[str, Any] = {
    "expression_version": 2,
    "type": {
        "BULLISH": {"structure": "long_call", "strike_selection": "atm"},
        "BEARISH": {"structure": "long_put", "strike_selection": "atm"},
    },
    "quantity": 1,
    "exit": {"stop_loss_pct": 0.30, "signal_flip": False, "min_days_to_expiry": 2},
}


class _LiveLabelledProvider(SyntheticQuoteProvider):
    """A synthetic book wearing a live label — the only flag the window reads.

    ``_quotes_are_live`` keys off ``source_name.startswith("live:")``, exactly
    like ``_effective_max_spread_pct``, so the gate can be exercised without a
    broker session.
    """

    @property
    def source_name(self) -> str:  # noqa: D401 - label only
        return "live:testing"


def _bridge(expression: Optional[dict] = None, *, live: bool = False, **kwargs) -> OptionsBridge:
    provider = (
        _LiveLabelledProvider(SyntheticChainGenerator())
        if live
        else SyntheticQuoteProvider(SyntheticChainGenerator())
    )
    bridge = OptionsBridge(
        capital=2_000_000.0,
        expression={**V2, **(expression or {})},
        quote_provider=provider,
        risk_gate=wide_gate(),
        **kwargs,
    )
    return bridge


def _open_structure(
    bridge: OptionsBridge,
    hour: int = 10,
    minute: int = 0,
    second: int = 0,
    spot: float = 24_500.0,
) -> Optional[dict]:
    """One ATM long call, entered on a bar stamped inside the session."""
    ts = _ist_text(hour, minute, second)
    bridge.on_bar("NIFTY", spot, ts)
    view = MarketView(
        direction=Direction.BULLISH,
        confidence=1.0,
        underlying="NIFTY",
        spot_price=Decimal(str(spot)),
        bar_timestamp=ts,
    )
    return bridge.on_intent(
        {"structure": "long_call", "strike_selection": "atm", "quantity": 1},
        view,
        "ticket-1.5",
    )


def _manager() -> PortfolioManager:
    return PortfolioManager(
        risk_config=GlobalRiskConfig(daily_loss_limit=1_000_000, max_drawdown_pct=0.9),
        tick_seconds=1.0,
        warmup_bars=5,
        auto_start_feed=False,
    )


# --------------------------------------------------------------------------- #
# 1. Explicit carry policy (BUG-39 item 1)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("timeframe", ["1min", "5min", "15min", "30min", "1hour"])
def test_intraday_timeframes_default_to_an_intraday_carry(timeframe):
    bridge = _bridge(timeframe=timeframe)
    assert bridge.carry_policy == CARRY_INTRADAY
    # The 20-minute floor (OPT-5) is the default, so 15:10 IST is the deadline.
    assert bridge.intraday_squareoff_minutes_before == 20
    assert bridge.carries_intraday is True


@pytest.mark.parametrize("timeframe", ["1day", "1week", "1hour+", ""])
def test_swing_timeframes_default_to_a_positional_carry(timeframe):
    bridge = _bridge(timeframe=timeframe)
    assert bridge.carry_policy == CARRY_POSITIONAL
    assert bridge.intraday_squareoff_minutes_before is None
    assert bridge.carries_intraday is False


def test_declared_positional_overrides_an_intraday_timeframe():
    bridge = _bridge({"carry_policy": "positional"}, timeframe="5min")
    assert bridge.carry_policy == CARRY_POSITIONAL
    assert bridge.carries_intraday is False


def test_declared_intraday_overrides_a_swing_timeframe():
    bridge = _bridge({"carry_policy": "intraday"}, timeframe="1day")
    assert bridge.carry_policy == CARRY_INTRADAY
    assert bridge.intraday_squareoff_minutes_before == 20


def test_unknown_carry_policy_is_refused_at_construction():
    with pytest.raises(ExecutionSpecError, match="carry_policy must be one of"):
        _bridge({"carry_policy": "day"}, timeframe="5min")


def test_positional_cannot_also_declare_an_intraday_squareoff():
    with pytest.raises(ExecutionSpecError, match="cannot also declare"):
        _bridge(
            {"carry_policy": "positional", "intraday_squareoff_minutes_before": 25},
            timeframe="5min",
        )


def test_legacy_knobs_still_imply_an_intraday_carry_and_clamp_to_the_floor():
    """A deployment that asked for 10 minutes keeps working, never later than 20."""
    bridge = _bridge(
        {"intraday_squareoff_minutes_before": 10}, timeframe="1day"
    )
    assert bridge.carry_policy == CARRY_INTRADAY
    assert bridge.intraday_squareoff_minutes_before == 20


def test_carry_policy_is_visible_on_the_summary():
    bridge = _bridge(timeframe="5min")
    snap = bridge.summary()
    assert snap["carry_policy"] == CARRY_INTRADAY
    assert snap["intraday_squareoff_minutes_before"] == 20


# --------------------------------------------------------------------------- #
# 2. Hard entry window (BUG-18 item 3)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "instant, intraday, expected",
    [
        (_ist(9, 14, 59), False, "before the 09:15:00 open"),
        (_ist(9, 15, 0), False, None),
        (_ist(15, 25, 0), False, None),
        (_ist(15, 25, 1), False, "after the 15:25 entry cut-off"),
        (_ist(15, 30, 0), False, "after the 15:25 entry cut-off"),
        (_ist(15, 30, 30), False, "after the 15:30:00 close"),
        (_ist(15, 9, 59), True, None),
        (_ist(15, 10, 0), True, None),
        (_ist(15, 10, 1), True, "after the 15:10 intraday square-off"),
    ],
)
def test_entry_window_refusal(instant, intraday, expected):
    refusal = entry_window_refusal(
        instant, intraday=intraday, squareoff_minutes=20 if intraday else None
    )
    if expected is None:
        assert refusal is None
    else:
        assert expected in refusal


def test_entry_cutoff_and_sweep_window_are_the_declared_minutes():
    assert ENTRY_CUTOFF_TIME == time(15, 25)
    assert EOD_SQUAREOFF_WINDOW_START == time(15, 10)


@pytest.mark.parametrize(
    "instant, expected",
    [
        (_ist(15, 9, 59), False),
        (_ist(15, 10, 0), True),
        (_ist(15, 29, 59), True),
        (_ist(15, 30, 0), False),
        (_ist(10, 0, 0), False),
    ],
)
def test_squareoff_window_predicate(instant, expected):
    assert in_eod_squareoff_window(instant) is expected


def test_live_bridge_refuses_an_entry_after_the_close():
    """The 2026-10-27 15:30:30 IST bar: judged on the bar's own clock."""
    bridge = _bridge(timeframe="1day", live=True)
    result = _open_structure(bridge, hour=15, minute=30, second=30)
    assert result.get("code") == "OUTSIDE_ENTRY_WINDOW"
    assert "15:30:00 close" in result["reason"]
    assert bridge._has_open_structure() is False


def test_live_bridge_refuses_an_entry_before_the_open():
    bridge = _bridge(timeframe="1day", live=True)
    result = _open_structure(bridge, hour=9, minute=14)
    assert result.get("code") == "OUTSIDE_ENTRY_WINDOW"
    assert bridge._has_open_structure() is False


def test_live_intraday_bridge_refuses_past_its_own_squareoff_deadline():
    bridge = _bridge(timeframe="5min", live=True)
    result = _open_structure(bridge, hour=15, minute=12)
    assert result.get("code") == "OUTSIDE_ENTRY_WINDOW"
    assert "square-off" in result["reason"]


def test_entry_still_opens_inside_the_window_on_a_live_feed():
    bridge = _bridge(timeframe="1day", live=True)
    result = _open_structure(bridge, hour=10)
    assert result and result.get("structure_id"), result
    assert bridge._has_open_structure() is True


def test_positional_deadline_is_the_session_cutoff_even_on_a_fast_timeframe():
    """A 5-minute strategy that *says* positional is refused at 15:25, not 15:10."""
    allowed = _bridge({"carry_policy": "positional"}, timeframe="5min", live=True)
    assert _open_structure(allowed, hour=15, minute=20).get("structure_id")

    refused = _bridge({"carry_policy": "positional"}, timeframe="5min", live=True)
    assert _open_structure(refused, hour=15, minute=26).get("code") == "OUTSIDE_ENTRY_WINDOW"


def test_synthetic_feed_is_not_governed_by_the_window():
    """Documented narrowing: a generated bar is scaffolding, not the exchange.

    Without this, every fixture that stamps bars at the wall clock becomes
    time-of-day dependent, and the rule would police numbers no venue can see.
    """
    bridge = _bridge(timeframe="1day")
    assert app_source_tag("synthetic") not in LIVE_FEED_TAGS
    result = _open_structure(bridge, hour=15, minute=30, second=30)
    assert result and result.get("structure_id"), result


# --------------------------------------------------------------------------- #
# 3. Wall-clock square-off, no tick required (BUG-39 item 2)
# --------------------------------------------------------------------------- #


def test_eod_squareoff_closes_the_book_with_no_tick_at_1510():
    bridge = _bridge(timeframe="5min")
    assert _open_structure(bridge, hour=10).get("structure_id")
    structure_id = bridge.open_structure_id

    # Nothing prints between 10:00 and the sweep — the illiquid-strike case.
    event = bridge.eod_squareoff(_ist(15, 10, 30))
    assert event is not None
    assert event["reason"] == EXIT_INTRADAY_SQUAREOFF
    assert event["structure_id"] == structure_id
    assert bridge._has_open_structure() is False
    closed = bridge.option_broker.get_closed_structures()
    assert closed[-1].exit_reason == EXIT_INTRADAY_SQUAREOFF


def test_eod_squareoff_is_idempotent_and_skips_positional_books():
    bridge = _bridge(timeframe="5min")
    assert _open_structure(bridge, hour=10).get("structure_id")
    assert bridge.eod_squareoff(_ist(15, 10, 30)) is not None
    assert bridge.eod_squareoff(_ist(15, 11, 0)) is None

    positional = _bridge({"carry_policy": "positional"}, timeframe="5min")
    assert _open_structure(positional, hour=10).get("structure_id")
    assert positional.eod_squareoff(_ist(15, 25, 0)) is None
    assert positional._has_open_structure() is True


def test_squareoff_before_the_window_does_nothing():
    bridge = _bridge(timeframe="5min")
    assert _open_structure(bridge, hour=10).get("structure_id")
    assert bridge.eod_squareoff(_ist(15, 9, 59)) is None
    assert bridge._has_open_structure() is True


# --------------------------------------------------------------------------- #
# 3b. The status contract on every door (B4, live gate 2026-10-30)
# --------------------------------------------------------------------------- #


def _squareoff_fired(bridge: OptionsBridge) -> bool:
    """Did a door force this book flat as an obligation (not as a strategy exit)?"""
    return any(
        structure.exit_reason == EXIT_INTRADAY_SQUAREOFF
        for structure in bridge.option_broker.get_closed_structures()
    )


def test_tick_at_the_cutoff_honours_pause_and_still_squares_off_a_running_book():
    """PAUSE is a reliable opt-out — the symptom that broke the 30-Oct negative control.

    Book ``2201907e`` was deliberately PAUSED as the gate's negative control and a
    15:10:00 IST tick flattened it anyway, because the tick/bar door read only the
    clock while the sweep next door required ``RUNNING``. Both doors now consult
    ``runner_running``, which the runner syncs from its own status.
    """
    bridge = _bridge(timeframe="5min")
    assert _open_structure(bridge, hour=10).get("structure_id")

    bridge.runner_running = False
    bridge.on_tick("NIFTY", 24_500.0, _ist_text(15, 10, 5))
    assert _squareoff_fired(bridge) is False
    assert bridge.last_unrealized_pnl is not None  # a paused book is still marked

    # Same bridge, same minute, permission restored: the obligation is enforced.
    bridge.runner_running = True
    bridge.on_tick("NIFTY", 24_500.0, _ist_text(15, 10, 30))
    assert _squareoff_fired(bridge) is True


def test_bar_at_the_cutoff_honours_pause():
    bridge = _bridge(timeframe="5min")
    assert _open_structure(bridge, hour=10).get("structure_id")
    bridge.runner_running = False
    bridge.on_bar("NIFTY", 24_500.0, _ist_text(15, 11, 0))
    assert _squareoff_fired(bridge) is False


def test_public_squareoff_seam_honours_pause_too():
    """The gate lives in the shared choke point, so no door can forget it."""
    bridge = _bridge(timeframe="5min")
    assert _open_structure(bridge, hour=10).get("structure_id")
    bridge.runner_running = False
    assert bridge.eod_squareoff(_ist(15, 10, 30)) is None
    assert _squareoff_fired(bridge) is False


class _StubRunner:
    """Minimal shape the sweep reads: status, bridge, seam, identity."""

    def __init__(self, name: str, *, carries_intraday: bool, event: Any = None):
        self.instance_id = name
        self.config = SimpleNamespace(name=name, mode="paper", symbols=[])
        self.status = STATUS_RUNNING
        self.options_bridge = SimpleNamespace(carries_intraday=carries_intraday)
        self.event = event
        self.calls: list = []

    def eod_squareoff(self, now: Optional[datetime] = None) -> Any:
        self.calls.append(now)
        return self.event

    def stop(self) -> None:  # manager shutdown walks its runners
        return None


def test_manager_sweep_dispatches_only_intraday_running_books():
    mgr = _manager()
    intraday = _StubRunner("INTRADAY", carries_intraday=True)
    positional = _StubRunner("POSITIONAL", carries_intraday=False)
    mgr._runners = {"INTRADAY": intraday, "POSITIONAL": positional}

    result = mgr.run_eod_squareoff_sweep(_ist(15, 10, 5))
    assert result["skipped"] is False
    assert intraday.calls == [_ist(15, 10, 5)]
    assert positional.calls == []
    assert result["squared_off"] == []
    mgr.shutdown()


def test_manager_sweep_stays_outside_its_window_and_honours_the_kill_switch(monkeypatch):
    mgr = _manager()
    intraday = _StubRunner("INTRADAY", carries_intraday=True)
    mgr._runners = {"INTRADAY": intraday}

    out = mgr.run_eod_squareoff_sweep(_ist(14, 59, 0))
    assert out["skipped"] is True and out["reason"] == "outside_squareoff_window"

    monkeypatch.setenv("EOD_SQUAREOFF_SWEEP", "off")
    disabled = mgr.run_eod_squareoff_sweep(_ist(15, 10, 0))
    assert disabled["skipped"] is True and disabled["reason"] == "sweep_disabled"
    assert intraday.calls == []
    mgr.shutdown()


def test_sweep_reports_and_audits_a_squared_off_book():
    mgr = _manager()
    event = {
        "structure_id": "abcd1234-0000",
        "structure_type": "long_call",
        "reason": EXIT_INTRADAY_SQUAREOFF,
        "pnl": -1234.5,
        "underlying": "NIFTY",
    }
    runner = _StubRunner("INTRADAY", carries_intraday=True, event=event)
    mgr._runners = {"INTRADAY": runner}

    result = mgr.run_eod_squareoff_sweep(_ist(15, 10, 5))
    assert result["squared_off"] == [
        {
            "instance_id": "INTRADAY",
            "name": "INTRADAY",
            "structure_id": "abcd1234-0000",
            "reason": EXIT_INTRADAY_SQUAREOFF,
            "pnl": -1234.5,
        }
    ]
    audit = [entry["action"] for entry in mgr.get_audit_log()]
    assert "EOD_SQUAREOFF" in audit
    mgr.shutdown()


def test_sweep_reports_a_paused_book_instead_of_flattening_it(caplog):
    """The sweep skips a paused book and says so — once per session, not once a second."""
    mgr = _manager()
    paused = _StubRunner("PAUSED-BOOK", carries_intraday=True)
    paused.status = STATUS_PAUSED
    mgr._runners = {"PAUSED-BOOK": paused}

    with caplog.at_level(logging.WARNING, logger="backtest.forward.portfolio_manager"):
        first = mgr.run_eod_squareoff_sweep(_ist(15, 10, 5))
        second = mgr.run_eod_squareoff_sweep(_ist(15, 10, 6))

    assert first["held_paused"] == [
        {
            "instance_id": "PAUSED-BOOK",
            "name": "PAUSED-BOOK",
            "status": STATUS_PAUSED,
            "scope": "paper",
        }
    ]
    assert first["squared_off"] == [] and second["squared_off"] == []
    assert paused.calls == [], "a paused book must never be asked to flatten"
    notices = [r for r in caplog.records if "obligation NOT enforced" in r.getMessage()]
    assert len(notices) == 1
    carry = [e for e in mgr.get_audit_log() if e["action"] == "EOD_CARRY_HELD"]
    assert len(carry) == 1
    mgr.shutdown()


def test_paused_carry_warning_re_arms_for_the_next_session(caplog):
    mgr = _manager()
    paused = _StubRunner("PAUSED-BOOK", carries_intraday=True)
    paused.status = STATUS_PAUSED
    mgr._runners = {"PAUSED-BOOK": paused}

    def notices() -> list:
        return [r for r in caplog.records if "obligation NOT enforced" in r.getMessage()]

    with caplog.at_level(logging.WARNING, logger="backtest.forward.portfolio_manager"):
        mgr.run_eod_squareoff_sweep(_ist(15, 10, 5))
        assert len(notices()) == 1
        mgr.run_eod_squareoff_sweep(_ist(14, 59, 0))  # window closes → latch re-arms
        mgr.run_eod_squareoff_sweep(_ist(15, 10, 5))
        assert len(notices()) == 2
    mgr.shutdown()


# --------------------------------------------------------------------------- #
# 4. Overnight attribution (BUG-18 item 4)
# --------------------------------------------------------------------------- #


def _bare_structure(
    entry_session: str,
    *,
    net_open: Decimal = Decimal("0"),
    **overrides,
) -> StructurePosition:
    """A one-leg structure marked at ``net_open`` — enough to exercise the anchor."""
    leg = OptionPosition(
        structure_id="s-1",
        underlying="NIFTY",
        option_type="CE",
        strike=Decimal("24500"),
        side="BUY",
        quantity=1,
        entry_price=Decimal("100"),
        current_price=Decimal("100"),
        unrealized_pnl=net_open,
    )
    fields = dict(
        structure_id="s-1",
        structure_type="long_call",
        strategy_name="ticket-1.5",
        underlying="NIFTY",
        expiry=date(2026, 10, 29),
        legs=[leg],
        entry_session_date=entry_session,
        last_mark_session=entry_session,
        last_mark_net_pnl=Decimal("500"),
    )
    fields.update(overrides)
    return StructurePosition(**fields)


def test_session_anchor_rolls_forward_only():
    """The prior-close anchor is the number today's P&L starts from."""
    structure = _bare_structure("2026-10-05")
    structure.record_session_mark("2026-10-06")
    assert structure.prior_close_net_pnl == Decimal("500")
    assert structure.last_mark_session == "2026-10-06"
    assert structure.carried_overnight is True

    # A repair reaching back to an older session must not move it.
    structure.record_session_mark("2026-10-05")
    assert structure.prior_close_net_pnl == Decimal("500")
    assert structure.last_mark_session == "2026-10-06"


def test_same_day_structure_is_not_marked_as_carried():
    today = datetime.now(_IST).date().isoformat()
    structure = _bare_structure(today)
    assert structure.carried_overnight is False


def test_today_slice_is_lifetime_minus_the_anchor():
    """``net_open_pnl_today`` + anchor == lifetime: no double count, no gap."""
    structure = _bare_structure(
        "2026-10-05",
        net_open=Decimal("100"),
        prior_close_net_pnl=Decimal("-250"),
    )
    assert structure.net_open_pnl == Decimal("100")
    assert structure.net_open_pnl_today == Decimal("350")
    assert (
        structure.net_open_pnl_today + structure.prior_close_net_pnl
        == structure.net_open_pnl
    )


def test_restore_seeds_the_anchor_so_the_gap_is_not_today():
    """A restart must not book the overnight move as this session's (BUG-18)."""
    bridge = _bridge(timeframe="5min")
    assert _open_structure(bridge, hour=10).get("structure_id")
    structure = bridge.option_broker.get_open_structures()[0]
    lifetime = structure.net_open_pnl

    fresh = OptionPaperBroker(capital=2_000_000.0)
    fresh.restore_structure(structure, fees_paid=structure.statutory_fees)

    assert structure.last_mark_session == datetime.now(_IST).date().isoformat()
    # The entry session is a fixture date, so everything it earned is carried:
    # today starts flat, and ``daily_pnl`` cannot show yesterday's settlement.
    assert structure.prior_close_net_pnl == lifetime
    assert structure.net_open_pnl_today == Decimal("0")
    assert structure.carried_overnight is True


def test_the_anchor_survives_a_restart_written_by_the_new_writer():
    """Mid-session restart: today's earnings stay in today (BUG-18).

    Re-deriving the anchor from the current mark would zero the day after every
    restart, so the snapshot carries the mark pair and restore only rolls it.
    """
    from backtest.forward.state_store import _structure_from_snapshot, _structure_to_snapshot

    today = datetime.now(_IST).date().isoformat()
    carried = _bare_structure(
        "2026-10-05",
        net_open=Decimal("250"),
        last_mark_session=today,
        last_mark_net_pnl=Decimal("380"),
        prior_close_net_pnl=Decimal("-150"),
    )
    assert carried.net_open_pnl_today == Decimal("400")
    restored = _structure_from_snapshot(_structure_to_snapshot(carried))
    assert restored.entry_session_date == "2026-10-05"
    assert restored.last_mark_session == today
    assert restored.prior_close_net_pnl == Decimal("-150")

    book = OptionPaperBroker(capital=1_000_000.0)
    book.restore_structure(restored)
    # Nothing rolled: the mark pair says today is already under way, and the
    # day's ₹400 is still attributable to it after the restart.
    assert restored.prior_close_net_pnl == Decimal("-150")
    assert restored.net_open_pnl_today == Decimal("400")
    assert restored.last_mark_session == today


def test_a_snapshot_without_the_anchor_still_treats_the_trip_as_carried():
    """Pre-BUG-18 state files have no memory, so the entry session decides."""
    from backtest.forward.state_store import _structure_from_snapshot, _structure_to_snapshot

    legacy = _bare_structure("2026-10-05")
    payload = _structure_to_snapshot(legacy)
    payload.pop("last_mark_session")
    payload.pop("prior_close_net_pnl")
    restored = _structure_from_snapshot(payload)
    assert restored.last_mark_session is None

    book = OptionPaperBroker(capital=1_000_000.0)
    book.restore_structure(restored)
    assert restored.last_mark_session == datetime.now(_IST).date().isoformat()
    assert restored.net_open_pnl_today == Decimal("0")


def test_open_option_row_stamps_the_entry_session():
    bridge = _bridge({"carry_policy": "positional"}, timeframe="1day")
    assert _open_structure(bridge, hour=10).get("structure_id")
    row = bridge.open_structures_snapshot()[0]
    assert row["entry_session_date"] == "2026-10-05"
    assert row["carried_overnight"] is True  # entered on a fixture date, marked now
    assert row["structure_id"] == bridge.open_structure_id


def test_squareoff_exit_reason_is_not_booked_as_a_signal():
    """``intraday_squareoff`` must land in the ``trades`` history as eod_flat."""
    assert _map_exit_reason(EXIT_INTRADAY_SQUAREOFF) == "eod_flat"
    assert _map_exit_reason("auto_square_off") == "eod_flat"
    assert _map_exit_reason("expiry_settlement") == "eod_flat"
    assert _map_exit_reason("take_profit") == "take_profit"
    assert _map_exit_reason("stop") == "stop_loss"


# --------------------------------------------------------------------------- #
# 5. The equity seam reads the same policy
# --------------------------------------------------------------------------- #


def _equity_runner(tmp_path) -> StrategyRunner:
    from backtest.forward.paper_runner import OrderLedger, RunnerConfig

    config = RunnerConfig(
        name="EQ-15",
        strategy_name="sma_crossover",
        allocated_capital=100_000,
        symbols=["RELIANCE"],
        timeframe="1day",
        mode="paper",
        source="synthetic",
    )
    runner = StrategyRunner(config, ledger=OrderLedger())
    runner.start()
    return runner


@pytest.mark.parametrize(
    "stamp, refused",
    [
        ("2026-10-05 10:00:00", False),
        ("2026-10-05 09:14:00", True),
        ("2026-10-05 15:25:30", True),
        ("2026-10-05 15:30:30", True),
    ],
)
def test_equity_entry_window_follows_the_same_policy(tmp_path, stamp, refused):
    runner = _equity_runner(tmp_path)
    try:
        # The runner asks for a live broker feed, so its stamps are the venue's.
        runner.config.source = "mstock"
        refusal = runner._entry_window_block(stamp)
        assert (refusal is not None) is refused
    finally:
        runner.stop()


def test_equity_synthetic_feed_is_not_governed(tmp_path):
    runner = _equity_runner(tmp_path)
    try:
        assert runner._entry_window_block("2026-10-05 15:30:30") is None
    finally:
        runner.stop()
