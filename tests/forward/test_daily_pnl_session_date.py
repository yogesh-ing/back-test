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
mapping is needed. The realized reader sources
``portfolio.closed_positions`` (the book round-trips across restarts; the
legacy ``_closed_trades`` list does not), and the writer stamps the audit
record with tz-aware ``exit_time``, ``session_date`` and Decimal ``net_pnl``.

Sign convention (writer docstring is the contract): ``net_pnl`` > 0 is a
profit, < 0 a loss, net of every commission charged on the round trip.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

from backtest.forward.paper_runner import (
    STATUS_PAUSED,
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


def _feed(runner: StrategyRunner, closes) -> None:
    for bar in _bars(closes):
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
# Reader: daily_pnl realized portion = trades whose session date is today
# ---------------------------------------------------------------------------


class TestDailyPnlSessionDate:
    def test_realized_today_is_nonzero_after_a_close(self):
        """Regression: realized was structurally 0 (missing keys / dead writer)."""
        runner = _equity_runner()
        closed = _long_then_close(runner, exit_price=80.0)
        expected = float(closed.net_pnl)
        assert expected < 0
        assert runner.daily_pnl() == pytest.approx(expected, rel=1e-6)
        assert runner.daily_pnl() != 0.0

    @pytest.mark.parametrize(
        "moment_utc,hour_ist,minute_ist,expected_included",
        [
            # (UTC instant, IST clock to combine onto the IST-today date)
            (None, 15, 30, True),   # NSE close: same UTC calendar date
            (None, 0, 20, True),    # after-midnight fill: UTC date is yesterday
            ("yesterday", 23, 30, False),  # last night's close: not today's loss
        ],
        ids=["nse_close_1530_ist", "after_midnight_0020_ist", "prev_day_2330_ist"],
    )
    def test_day_rolls_at_midnight_ist(
        self, moment_utc, hour_ist, minute_ist, expected_included
    ):
        runner = _equity_runner()
        closed = _long_then_close(runner, exit_price=80.0)
        ist_now = _ist_today()
        if moment_utc == "yesterday":
            stamp = datetime.combine(ist_now - timedelta(days=1), time(23, 30), tzinfo=IST)
        else:
            stamp = datetime.combine(ist_now, time(hour_ist, minute_ist), tzinfo=IST)
        closed.closed_at = stamp
        realized = float(closed.net_pnl)
        got = runner.daily_pnl()
        if expected_included:
            assert got == pytest.approx(realized, rel=1e-6)
        else:
            assert got == pytest.approx(0.0, abs=1e-9)

    def test_paper_runner_reader_uses_closed_positions(self):
        """PaperRunner has no _closed_trades producer — its reader must not rely on it."""
        from backtest.forward.paper_runner import PaperRunner
        from backtest.simulator.portfolio import Portfolio

        runner = _equity_runner()
        closed = _long_then_close(runner, exit_price=80.0)

        replay = PaperRunner.__new__(PaperRunner)  # bypass the bar-replay __init__
        replay.portfolio = Portfolio(name="t", initial_capital=100_000)
        replay.portfolio.closed_positions = [closed]
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
