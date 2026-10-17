"""Shared options-test support: the research structure profile + freshness clock.

Production construction defaults stay on the V1 execution whitelist
(``backtest/options/execution_gate.py``) — a runner, the bridge, the paper book
and the live trader all refuse short-option and multi-leg intents.

Tests that exercise multi-leg *mechanics* (leg repricing, spread exits,
rollback) need those structures, so they widen an already-built object with
:func:`research_profile`. Nothing in ``src/`` reads this module, so it cannot
widen a deployment.

:func:`stale_marks`, :func:`pin_session_clock` and :func:`market_closed` cover
the freshness SLA (PRD §4.1/§8.1): staleness is market-session time since the
last successful mark, evaluated only while the session is trading — so a test
pins the clock and back-dates the marks, and never depends on the wall time it
runs at.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Optional

import pytest

from backtest.options.execution_gate import RESEARCH_STRUCTURES


def wide_gate():
    """A PreTradeRiskGate wide enough for synthetic-chain premiums.

    The rulebook's ₹25k per-trade ceiling (pending the user's sign-off)
    sits below one ATM NIFTY premium (~₹28k), so suites exercising
    mechanics rather than the ceiling scaffold a wider gate — the same
    spirit as :func:`research_profile`. Nothing in ``src/`` reads this.
    """
    from backtest.options.execution_gate import PreTradeRiskGate
    from backtest.risk_limits import RiskLimits

    return PreTradeRiskGate(
        RiskLimits(
            per_trade_max_loss_inr=1_000_000.0,
            per_runner_max_lots=100,
            per_runner_max_order_value_inr=10_000_000.0,
            entitlement={"paper": frozenset(RESEARCH_STRUCTURES)},
            kill_switch_engaged_on_boot=False,
            sha256="x" * 64,
        ),
        mode="paper",
    )


def research_profile(target):
    """Widen a ``StrategyRunner``, ``OptionsBridge``, book or trader. Returns it."""
    bridge = getattr(target, "options_bridge", target)
    if hasattr(bridge, "allowed_structures"):
        bridge.allowed_structures = set(RESEARCH_STRUCTURES)
    book = getattr(bridge, "option_broker", None)
    if book is not None and hasattr(book, "allowed_structures"):
        book.allowed_structures = set(RESEARCH_STRUCTURES)
    return target


def _open_legs(target: Any) -> Iterator[Any]:
    book = getattr(target, "option_broker", None)
    if book is None:
        book = getattr(getattr(target, "options_bridge", None), "option_broker", None)
    if book is None:
        raise AssertionError(f"{target!r} has no option book")
    yield from book.get_open_positions()


#: A fixed instant inside an NSE session: Monday 2026-10-05, 10:00 IST.
SESSION_NOW = datetime(2026, 10, 5, 4, 30, tzinfo=timezone.utc)


def pin_session_clock(monkeypatch) -> datetime:
    """Pin the freshness clock to ``SESSION_NOW`` and declare the session open.

    Age is market-session time (PRD §4.4), so a test that back-dates marks has to
    say *when* "now" is — otherwise the same test passes before 15:30 IST and
    fails after it. Returns the pinned instant.
    """
    import backtest.forward.options_bridge as ob
    import backtest.options.paper_trading as pt

    monkeypatch.setattr(pt, "_now_utc", lambda: SESSION_NOW)
    monkeypatch.setattr(ob, "_market_is_open", lambda now=None: True)
    return SESSION_NOW


def _backdate_by_session_seconds(pt, reference: datetime, seconds: float) -> datetime:
    """Walk ``reference`` back until ``seconds`` of session time has passed.

    Uses the production arithmetic (``pt`` is ``backtest.options.paper_trading``),
    so "this mark is 22 700 session-seconds old" means the same thing in a test as
    on a running book — a wall-clock ``reference - timedelta(seconds)`` can land
    in the overnight gap and age by nothing at all.
    """
    step = timedelta(minutes=1)
    cursor = pt._utc(reference)
    counted = 0.0
    guard = 0
    while counted < seconds and guard < 20000:
        previous = cursor - step
        counted += pt._session_seconds_between(previous, cursor)
        cursor = previous
        guard += 1
    return cursor


def stale_marks(target: Any, seconds: float, now: Optional[datetime] = None) -> Any:
    """Back-date every open leg's last successful mark by ``seconds`` of session time.

    The SLA compares market-session elapsed time, so this is how a test says "the
    book was marked a long time ago and nothing has refreshed since".
    """
    from backtest.options import paper_trading as pt

    reference = now or pt._now_utc()
    stamp = _backdate_by_session_seconds(pt, reference, seconds)
    for leg in _open_legs(target):
        leg.last_updated = stamp
    return target


@pytest.fixture
def market_closed(monkeypatch):
    """Pin the session probe shut (the overnight case the SLA must not flag)."""
    import backtest.forward.options_bridge as ob

    monkeypatch.setattr(ob, "_market_is_open", lambda now=None: False)
    return ob
