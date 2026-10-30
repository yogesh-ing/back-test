"""Daily-loss accounting: closed trades must stamp session fields and roll at 00:00 IST.

The instance daily-loss breaker reads ``StrategyRunner.daily_pnl()``. Two
seams made its realized portion structurally zero (2026-10-22 recon):

* ``_record_closed_trade`` read ``closing_fill.fill_price`` — an attribute
  ``FillEvent`` does not have — so the append died inside ``on_fill``'s
  try/except and ``_closed_trades`` stayed empty forever; and even if it had
  survived, the record stored only ``timestamp`` (UTC) with no ``exit_time``,
  ``session_date`` or ``net_pnl``.
* the readers filtered those missing keys against ``date.today()``, mixing
  UTC-stamped instants with IST wall-clock dates (the 00:00–05:30 IST seam).

Architecture decision (2026-10-22): the day resets at **00:00 IST calendar
date** — nothing in the system rolls at 09:15, so no Trading-Session-Date
mapping is needed. The writer stamps the audit record with tz-aware
``exit_time``, ``session_date`` and Decimal ``net_pnl``.

Sign convention (writer docstring is the contract): ``net_pnl`` > 0 is a
profit, < 0 a loss, net of every commission charged on the round trip.

Update (BUG-04, 2026-10-30): the *audit writer* still stamps ``session_date``
from the close instant, but the daily READER no longer filters those stamps.
``daily_pnl()`` is now the NET move of (realized + open) between session
anchors, rolled on the bar clock — see ``tests/forward/test_net_pnl_contract.py``
for the contract and its invariants.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

from backtest.forward.paper_runner import (
    STATUS_PAUSED,
    STATUS_RUNNING,
    FillEvent,
    OrderLedger,
    RunnerConfig,
    StrategyRunner,
)

from options_support import research_profile

IST = timezone(timedelta(hours=5, minutes=30))

BASE_DAY = date(2026, 9, 10)


def _bars(closes, start_day: int = 0):
    base = BASE_DAY + timedelta(days=start_day)
    return [
        {
            "ts": f"{(base + timedelta(days=i)).isoformat()}T09:15:00",
            "open": close - 1,
            "high": close + 5,
            "low": close - 5,
            "close": float(close),
            "volume": 1000,
        }
        for i, close in enumerate(closes)
    ]


def _equity_runner(**overrides) -> StrategyRunner:
    kwargs = dict(
        name="EQ",
        strategy_name="buy_and_hold",
        allocated_capital=1_000_000,
        symbols=["RELIANCE"],
        timeframe="1day",
        mode="paper",
        source="synthetic",
    )
    kwargs.update(overrides)
    runner = StrategyRunner(RunnerConfig(**kwargs), ledger=OrderLedger())
    runner.start()
    research_profile(runner)
    return runner


def _long_then_close(runner: StrategyRunner, exit_price: float):
    """Enter long RELIANCE @100 (14-bar warmup), mark, and close fully."""
    _feed(runner, [100.0] * 14)
    assert runner.positions, "fixture failed to open a position"
    runner.last_price["RELIANCE"] = exit_price
    runner.close_position("RELIANCE")
    closed = [p for p in runner.portfolio.closed_positions if p.symbol == "RELIANCE"]
    assert closed, "close did not land in closed_positions"
    return closed[-1]


def _feed(runner: StrategyRunner, closes, start_day: int = 0) -> None:
    for bar in _bars(closes, start_day=start_day):
        runner.process_candle_event("RELIANCE", bar)


def _ist_today() -> date:
    return datetime.now(timezone.utc).astimezone(IST).date()


def _fill(runner: StrategyRunner, price: float = 80.0) -> FillEvent:
    return FillEvent(
        client_order_id="PRT-test-0-0",
        instance_id=runner.instance_id,
        symbol="RELIANCE",
        side="SELL",
        quantity=100.0,
        price=price,
        ts=datetime.now(timezone.utc).isoformat(),
    )


# ---------------------------------------------------------------------------
# Writer: _record_closed_trade must stamp exit_time / session_date / net_pnl
# ---------------------------------------------------------------------------


class TestClosedTradeWriter:
    def test_closed_round_trip_is_recorded(self):
        """Regression: the fill_price AttributeError silently killed appends."""
        runner = _equity_runner()
        _long_then_close(runner, exit_price=95.0)
        records = [t for t in runner._closed_trades if t.get("symbol") == "RELIANCE"]
        assert records, "closing fill produced no _closed_trades record"

    def test_record_carries_session_fields_and_decimal_net_pnl(self):
        runner = _equity_runner()
        closed = _long_then_close(runner, exit_price=95.0)
        rec = [t for t in runner._closed_trades if t.get("symbol") == "RELIANCE"][-1]

        exit_time = rec["exit_time"]
        assert isinstance(exit_time, datetime) and exit_time.tzinfo is not None
        assert rec["session_date"] == exit_time.astimezone(IST).date().isoformat()
        # Decimal-exact, string-safe for JSON, matches the book's own math.
        assert Decimal(str(rec["net_pnl"])) == Decimal(str(closed.net_pnl))

    def test_net_pnl_sign_convention(self):
        """Positive net_pnl = profit, negative = loss (net of commissions)."""
        winner = _equity_runner(name="WIN")
        winner_closed = _long_then_close(winner, exit_price=130.0)
        win_rec = [t for t in winner._closed_trades if t["symbol"] == "RELIANCE"][-1]
        assert Decimal(str(win_rec["net_pnl"])) > 0
        assert winner_closed.net_pnl > 0

        loser = _equity_runner(name="LOSE")
        _long_then_close(loser, exit_price=70.0)
        lose_rec = [t for t in loser._closed_trades if t["symbol"] == "RELIANCE"][-1]
        assert Decimal(str(lose_rec["net_pnl"])) < 0

    def test_session_date_maps_out_of_hours_fill_to_ist_calendar_date(self):
        """A 00:20 IST close stamps TODAY's date even though UTC says yesterday."""
        runner = _equity_runner()
        closed = _long_then_close(runner, exit_price=95.0)
        ist_now = _ist_today()
        # 00:20 IST == 18:50 UTC on the PREVIOUS calendar day.
        closed.closed_at = datetime.combine(ist_now, time(0, 20), tzinfo=IST)
        runner._closed_trades.clear()
        runner._record_closed_trade(_fill(runner))
        rec = runner._closed_trades[-1]
        assert rec["session_date"] == ist_now.isoformat()
        assert rec["exit_time"].astimezone(timezone.utc).date() == ist_now - timedelta(days=1)

    def test_append_is_idempotent_for_the_same_close(self):
        """Crash-replay must not double-count: same identity → one record."""
        runner = _equity_runner()
        _long_then_close(runner, exit_price=95.0)
        before = len(runner._closed_trades)
        closed = runner.portfolio.closed_positions[-1]
        runner._record_closed_trade(_fill(runner, price=float(closed.current_price or 95.0)))
        assert len(runner._closed_trades) == before, "duplicate close record appended"


# ---------------------------------------------------------------------------
# Reader: today's P&L is the NET move of the book, rolled on the session clock
# ---------------------------------------------------------------------------


class TestDailyPnlSessionDate:
    def test_realized_today_is_nonzero_after_a_close(self):
        """Regression: realized was structurally 0 (missing keys / dead writer)."""
        runner = _equity_runner()
        closed = _long_then_close(runner, exit_price=80.0)
        expected = float(closed.net_pnl)
        assert expected < 0
        # BUG-04: the reader is the NET half, and ``daily_pnl`` is the move of
        # (realized + open) between session captures — the entry brokerage was
        # already counted on the bar that paid it, so ``daily_pnl`` and
        # ``realized_pnl_today`` differ by exactly that fee.
        assert runner.realized_pnl_today == pytest.approx(expected, rel=1e-6)
        assert runner.daily_pnl() != 0.0
        assert runner.daily_pnl() == pytest.approx(
            runner.realized_pnl_today + runner.open_pnl() - runner.prev_close_open_pnl,
            abs=0.02,
        )

    def test_day_rolls_on_the_next_session_bar(self):
        """Yesterday's banked P&L leaves today when the session advances.

        BUG-04 moved the roll from a ``closed_at`` date filter to the runner's
        own session anchors, so it rolls on the same bar clock the option book
        uses (BUG-18). Between midnight and the first new bar yesterday's
        number is still shown — deliberately fail-safe for the daily-loss
        breaker, which must not get a free reset before the session starts.
        """
        runner = _equity_runner()
        closed = _long_then_close(runner, exit_price=80.0)
        assert runner.realized_pnl_today == pytest.approx(
            float(closed.net_pnl), rel=1e-6
        )

        _feed(runner, [80.0], start_day=14)
        assert runner.realized_pnl_today == pytest.approx(0.0, abs=1e-9)
        # Nothing banked carries over: what the new session shows is the move
        # of its own book (the strategy re-entered, so its entry cost is
        # today's, charged against the mark — not yesterday's loss).
        assert runner.prev_close_open_pnl == pytest.approx(0.0, abs=0.02)
        assert runner.daily_pnl() == pytest.approx(runner.open_pnl(), abs=0.02)

    def test_paper_runner_reader_uses_the_book_not_closed_trades(self):
        """PaperRunner has no _closed_trades producer — its reader must not rely on it."""
        from backtest.forward.paper_runner import PaperRunner

        runner = _equity_runner()
        closed = _long_then_close(runner, exit_price=80.0)

        replay = PaperRunner.__new__(PaperRunner)  # bypass the bar-replay __init__
        replay.portfolio = runner.portfolio  # the book StrategyRunner actually traded
        assert replay.daily_pnl() == pytest.approx(float(closed.net_pnl), rel=1e-6)


# ---------------------------------------------------------------------------
# Guard B (architect): a catastrophic REALIZED loss trips the instance breaker
# ---------------------------------------------------------------------------


class TestInstanceBreakerOnRealizedLoss:
    def test_catastrophic_realized_loss_pauses_runner(self):
        """MTM already zeroed by the close: the daily-loss breaker must still fire."""
        runner = _equity_runner(
            name="RUIN",
            allocated_capital=1_000_000,
            daily_loss_limit_pct=0.05,
            max_drawdown_pct=0.0,  # isolate the daily-loss fuse
        )
        _feed(runner, [1000.0] * 14)
        assert runner.positions, "fixture failed to open a position"
        runner.last_price["RELIANCE"] = 100.0
        runner.close_position("RELIANCE")
        assert runner.daily_pnl() < -40_000  # ≈ -90% of allocation, realized

        runner._check_instance_risk()
        assert runner.status == STATUS_PAUSED
        assert "daily loss" in (runner.error or "")


# ---------------------------------------------------------------------------
# The fuse must read the SAME day the card reads (daily-pnl continuation)
# ---------------------------------------------------------------------------


class TestInstanceBreakerReadsTheSessionDay:
    """The instance fuse was fed by a spawn anchor that never rolls.

    ``_check_instance_risk`` gated on ``allocated − equity`` and divided by
    ``_day_start_equity`` — the equity at the first bar the runner ever saw,
    baselined once by ``_roll_trading_day`` and never moved again. So it
    measured the book's lifetime, while ``daily_pnl()`` (the BUG-04 session
    anchors) measures today. Two wrong directions from one stale number:

    * a good day raises equity above the anchor, so the next day's crash —
      which the card shows in full — never reaches the gate and the book keeps
      trading (the money-losing one, live capital);
    * yesterday's loss stays in the number forever, so a flat today trips.

    The drawdown fuse already owns the cumulative direction (its baseline is
    ``peak_equity``); the daily fuse owns today.
    """

    def test_crash_after_a_profitable_day_still_trips(self):
        """Yesterday's +₹9L must not buy today's −₹9L a free pass."""
        runner = _equity_runner(
            name="UPDOWN",
            allocated_capital=1_000_000,
            daily_loss_limit_pct=0.05,
            max_drawdown_pct=0.0,
        )
        _feed(runner, [100.0] * 14)
        assert runner.positions, "fixture failed to open a position"

        _feed(runner, [190.0], start_day=14)  # the good session
        assert runner.daily_pnl() > 100_000
        _feed(runner, [190.0], start_day=15)  # rolls: yesterday is banked

        assert runner.status != STATUS_PAUSED, "fixture tripped before the crash"
        assert runner.equity() > runner.config.allocated_capital, (
            "the profitable session must leave equity above the spawn anchor, "
            "which is exactly what blinded the old gate"
        )
        _feed(runner, [100.0], start_day=16)  # today: all of it back off

        day_move = runner.daily_pnl()
        assert day_move < -50_000, f"today's move should be the crash, got {day_move:,.2f}"

        runner._check_instance_risk()
        assert runner.status == STATUS_PAUSED, (
            "the daily fuse read the spawn anchor: equity had returned to "
            "allocation, so a -"
            f"{-day_move / runner.config.allocated_capital:.0%} day tripped nothing"
        )
        assert "daily loss" in (runner.error or "")

    def test_three_small_days_do_not_trip_a_five_percent_daily_limit(self):
        """Three 1.8% days are three clean days, however long the book has lived."""
        runner = _equity_runner(
            name="DRIFT",
            allocated_capital=1_000_000,
            daily_loss_limit_pct=0.05,
            max_drawdown_pct=0.0,
        )
        _feed(runner, [1000.0] * 14)
        assert runner.positions, "fixture failed to open a position"

        for price, day in ((981.0, 14), (962.0, 15), (943.0, 16)):
            _feed(runner, [price], start_day=day)
            day_move = runner.daily_pnl()
            assert -50_000 < day_move < 0, (
                f"the session on day {day} should be a small loss, got {day_move:,.2f}"
            )
            runner._check_instance_risk()
            assert runner.status != STATUS_PAUSED, (
                f"paused on day {day} for a {day_move:,.2f} day against a ₹50,000 "
                "(5%) limit — the fuse was counting the life, not the day"
            )

        # What the OLD fuse divided by: the loss since the spawn anchor. It is
        # over the limit here, which is exactly why it fired on a day the book
        # did not lose. The cumulative direction belongs to the drawdown fuse.
        assert runner._day_start_equity - runner.equity() > (
            runner.config.allocated_capital * runner.config.daily_loss_limit_pct
        )

    def test_manual_anchor_reset_clears_the_day_for_the_fuse(self):
        """reset_daily_anchor must forgive the day, not just move a stored number."""
        runner = _equity_runner(
            name="RESET",
            allocated_capital=1_000_000,
            daily_loss_limit_pct=0.05,
            max_drawdown_pct=0.0,
        )
        _feed(runner, [1000.0] * 14)
        _feed(runner, [800.0], start_day=14)  # −19% in one session
        assert runner.daily_pnl() < -50_000

        runner.reset_daily_anchor()
        assert runner.daily_pnl() == pytest.approx(0.0, abs=1.0)
        runner.status = STATUS_RUNNING  # clearing the day is the resume gesture too
        runner._check_instance_risk()
        assert runner.status == STATUS_RUNNING, (
            "the operator reset the anchors but the fuse still owed the day"
        )


