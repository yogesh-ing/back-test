"""Task B3 — close plumbing: trade log, metrics, equity curve.

B1/B2 gave the runner the ability to close and settle; nothing reported it.
Concretely, before this task:

* ``closed_trades`` / ``get_detail()["trades"]`` listed only equity round-trips,
  so a full option lifecycle showed an empty Trades tab;
* ``win_rate`` / ``wins`` / ``losses`` counted equity trades only;
* the option book's own win rate and average P&L were not exposed anywhere;
* ``equity_curve`` was **always empty** — nothing in production code ever
  called ``_mark_to_market(record=True)``, so the deep-dive chart had no data
  for any runner, equity or option.

B3 wires all four, and gives settlements their own signal kind
(``OPTION_SETTLED``) so a log reader can tell a decision from an expiry.

See ``docs/OPTIONS-FORWARD-TESTING.md`` → task B3.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from backtest.forward.paper_runner import (
    MAX_EQUITY_POINTS,
    OrderLedger,
    RunnerConfig,
    StrategyRunner,
)
from backtest.options.quote_providers import SyntheticChainGenerator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_CYCLE_EXPIRY = SyntheticChainGenerator().next_monthly_expiry(date(2026, 9, 1))
_CYCLE_START = _CYCLE_EXPIRY - timedelta(days=27)


def _bars(closes, start_day=1):
    base = _CYCLE_START + timedelta(days=start_day - 1)
    return [
        {
            "ts": f"{(base + timedelta(days=i)).isoformat()}T09:15:00",
            "open": close - 10,
            "high": close + 30,
            "low": close - 30,
            "close": float(close),
            "volume": 1000,
        }
        for i, close in enumerate(closes)
    ]


def _runner(exit_cfg=None, **overrides):
    expression = {
        "type": {"BULLISH": "bull_call_spread", "BEARISH": "bear_put_spread"},
        "exit": exit_cfg
        if exit_cfg is not None
        else {"min_days_to_expiry": None, "signal_flip": False},
    }
    kwargs = dict(
        name="opt-runner",
        strategy_name="directional_options",
        allocated_capital=1_000_000,
        symbols=["NIFTY"],
        timeframe="1day",
        instrument={"type": "option", "expression": expression},
    )
    kwargs.update(overrides)
    runner = StrategyRunner(RunnerConfig(**kwargs), ledger=OrderLedger())
    runner.start()
    return runner


def _feed(runner, bars):
    for bar in bars:
        runner.process_candle_event("NIFTY", bar)


def _closed_option_runner(**kwargs):
    """A runner with exactly one closed structure: stop-out on a crash."""
    runner = _runner({"stop_loss_pct": 0.3, "min_days_to_expiry": None}, **kwargs)
    _feed(runner, _bars([24_800 + i * 40 for i in range(14)]))
    _feed(runner, _bars([24_800 - i * 900 for i in range(1, 10)], start_day=15))
    return runner


# ---------------------------------------------------------------------------
# Trade log
# ---------------------------------------------------------------------------


class TestOptionTradeLog:
    def test_closed_structure_appears_in_closed_trades(self):
        runner = _closed_option_runner()
        trades = [t for t in runner.closed_trades if t["kind"] == "option"]

        assert trades, "a closed structure must appear in the trade log"
        trade = trades[0]
        assert trade["kind"] == "option"
        assert trade["structure_type"] == "bull_call_spread"
        assert trade["underlying"] == "NIFTY"
        assert trade["symbol"] == "NIFTY bull_call_spread"
        assert trade["qty"] == 1  # lots per leg
        assert trade["units"] == 75  # contracts per leg
        assert trade["legs"] == 2
        assert trade["exit_reason"] == "stop_loss"
        assert trade["pnl"] < 0
        assert trade["win"] is False
        assert trade["entry_ts"] and trade["exit_ts"]

    def test_net_premium_is_signed_by_side(self):
        """A debit spread has a positive net premium; a credit structure negative."""
        runner = _closed_option_runner()
        trade = runner.closed_option_trades[0]
        assert trade["entry_price"] > 0  # bull call spread = net debit
        assert trade["exit_price"] < trade["entry_price"]  # it lost on the crash

    def test_trade_records_reach_the_deep_dive(self):
        """`get_detail()["trades"]` is what the deep-dive Trades tab renders."""
        runner = _closed_option_runner()
        detail = runner.get_detail()
        assert detail["trades"], "the repository detail view must list the close"
        assert any(t.get("kind") == "option" for t in detail["trades"])

    def test_open_structure_is_not_in_the_trade_log(self):
        runner = _runner()
        _feed(runner, _bars([24_800 + i * 40 for i in range(14)]))
        assert runner.options_summary()["open_structures"] == 1
        assert runner.closed_option_trades == []
        assert runner.closed_trades == []

    def test_settled_structure_is_logged_with_its_reason(self):
        runner = _runner()
        _feed(runner, _bars([24_800 + i * 30 for i in range(60)]))
        settled = [
            t for t in runner.closed_option_trades if t["exit_reason"] == "expiry_settlement"
        ]
        assert settled, "a settled structure must appear in the trade log"
        assert settled[0]["expiry"] is not None

    def test_option_log_is_chronological(self):
        runner = _runner()
        # 90 daily bars cross two monthly expiries → settle, roll, settle.
        _feed(runner, _bars([24_800 + i * 30 for i in range(90)]))

        trades = runner.closed_option_trades
        assert len(trades) >= 2
        assert [t["exit_ts"] for t in trades] == sorted(t["exit_ts"] for t in trades)
        assert all(t["kind"] == "option" for t in trades)

    def test_equity_records_keep_their_shape(self):
        """Compatibility guard: the classic flow only gains a `kind` tag."""
        runner = StrategyRunner(
            RunnerConfig(
                name="eq",
                strategy_name="sma_crossover",
                allocated_capital=100_000,
                symbols=["NIFTY"],
                timeframe="1day",
                strategy_params={"fast": 5, "slow": 20},
            ),
            ledger=OrderLedger(),
        )
        runner.start()
        # Rally opens the SMA crossover, the reversal closes it at signal 0.
        _feed(runner, _bars([20_000 + i * 80 for i in range(40)]))
        _feed(runner, _bars([23_200 - i * 120 for i in range(1, 30)], start_day=41))

        assert runner.closed_trades, "the equity flow should have closed something"
        for trade in runner.closed_trades:
            assert trade["kind"] == "equity"
            assert set(trade) >= {
                "symbol", "side", "qty", "entry_price", "exit_price",
                "entry_ts", "exit_ts", "pnl", "win", "coid", "exit_coid",
            }
        assert runner.closed_option_trades == []


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


class TestMetrics:
    def test_win_rate_counts_option_structures(self):
        runner = _closed_option_runner()
        assert runner.losses >= 1
        assert runner.wins + runner.losses == len(runner.closed_trades)
        assert runner.win_rate() == pytest.approx(
            runner.wins / (runner.wins + runner.losses)
        )

    def test_book_metrics_are_exposed_on_state(self):
        runner = _closed_option_runner()
        options = runner.get_state()["options"]

        assert options["closed_structures"] == 1
        assert options["wins"] + options["losses"] == 1
        assert options["wins"] == 0
        assert options["losses"] == 1
        assert options["win_rate"] == 0.0
        assert options["total_pnl"] < 0  # it stopped out
        assert options["avg_pnl"] == pytest.approx(options["total_pnl"])
        assert options["costs_paid"] > 0
        # Book equity = capital + realized − costs + open-leg mark. The runner
        # re-entered after the stop-out, so there is an open mark to include.
        assert options["unrealized_pnl"] != 0
        assert options["equity"] == pytest.approx(
            options["capital"]
            + options["realized_pnl"]
            - options["costs_paid"]
            + options["unrealized_pnl"]
        )

    def test_book_metrics_reconcile_with_closed_structures(self):
        runner = _closed_option_runner()
        broker = runner.options_bridge.option_broker
        summary = runner.options_summary()
        booked = sum(
            (s.total_realized_pnl for s in broker.get_closed_structures()), Decimal("0")
        )
        assert summary["total_pnl"] == pytest.approx(float(booked))
        assert summary["closed_structures"] == len(broker.get_closed_structures())

    def test_win_and_loss_books(self):
        """Both a winner and a loser land in the metrics."""
        runner = _runner({"take_profit_pct": 0.2, "min_days_to_expiry": None})
        _feed(runner, _bars([24_800 + i * 40 for i in range(14)]))
        # Rally further: the spread targets out.
        _feed(runner, _bars([25_400 + i * 60 for i in range(1, 8)], start_day=15))

        summary = runner.options_summary()
        assert summary["closed_structures"] >= 1
        assert summary["wins"] >= 1
        assert summary["win_rate"] > 0


# ---------------------------------------------------------------------------
# Equity curve
# ---------------------------------------------------------------------------


class TestEquityCurve:
    def test_curve_is_recorded_per_bar(self):
        runner = _runner()
        bars = _bars([24_800 + i * 40 for i in range(14)])
        _feed(runner, bars)
        assert len(runner.equity_curve) == len(bars)
        assert runner.equity_curve[-1]["equity"] == pytest.approx(runner.equity())

    def test_equity_runners_also_get_a_curve(self):
        """The chart was empty for every runner, not just option ones."""
        runner = StrategyRunner(
            RunnerConfig(
                name="eq",
                strategy_name="sma_crossover",
                allocated_capital=100_000,
                symbols=["NIFTY"],
                timeframe="1day",
            ),
            ledger=OrderLedger(),
        )
        runner.start()
        _feed(runner, _bars([24_800 + i * 40 for i in range(10)]))
        assert len(runner.equity_curve) == 10
        assert runner.equity_curve[-1]["equity"] == pytest.approx(runner.equity())

    def test_curve_tracks_the_option_book(self):
        """The curve must move with the option P&L, not just the equity book."""
        runner = _closed_option_runner()
        assert runner.equity_curve
        values = [p["equity"] for p in runner.equity_curve]
        assert max(values) != min(values)
        assert values[-1] == pytest.approx(runner.equity())

    def test_every_option_exit_adds_a_curve_point(self):
        runner = _closed_option_runner()
        exits = [
            s for s in runner.signal_log if s["kind"] in ("OPTION_EXIT", "OPTION_SETTLED")
        ]
        assert exits
        assert len(runner.equity_curve) > len(
            [s for s in runner.signal_log if s["kind"] == "OPTION_MTM"]
        )

    def test_curve_downsamples_instead_of_freezing(self):
        """Past the cap the curve keeps advancing at coarser resolution."""
        runner = _runner()
        total = MAX_EQUITY_POINTS * 3
        _feed(runner, _bars([24_800 + i for i in range(total)]))

        assert len(runner.equity_curve) <= MAX_EQUITY_POINTS
        assert len(runner.equity_curve) > MAX_EQUITY_POINTS // 2
        # The latest point is still the live equity — not a frozen old one.
        assert runner.equity_curve[-1]["equity"] == pytest.approx(runner.equity())

    def test_detail_exposes_the_curve(self):
        runner = _closed_option_runner()
        curve = runner.get_detail()["equity_curve"]
        assert curve and all("equity" in point for point in curve)


# ---------------------------------------------------------------------------
# Signal kinds
# ---------------------------------------------------------------------------


class TestSignalKinds:
    def test_stop_out_is_an_option_exit(self):
        runner = _closed_option_runner()
        kinds = {s["kind"] for s in runner.signal_log}
        assert "OPTION_ENTRY" in kinds
        assert "OPTION_EXIT" in kinds
        assert "OPTION_SETTLED" not in kinds  # a stop is a decision, not an expiry

    def test_expiry_is_an_option_settled(self):
        runner = _runner()
        _feed(runner, _bars([24_800 + i * 30 for i in range(60)]))
        kinds = {s["kind"] for s in runner.signal_log}
        assert "OPTION_SETTLED" in kinds
        settled = [s for s in runner.signal_log if s["kind"] == "OPTION_SETTLED"]
        assert all("cash settled" in s["reason"] for s in settled)
        # A settlement is still a flat signal for anything counting exits.
        assert all(s["signal"] == 0 for s in settled)
