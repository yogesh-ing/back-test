"""Gap G3.2 — options-aware strategies wired into the forward engine.

Acceptance criteria from the Gap PRD:

* a runner with ``instrument.type == "option"`` routes bars through the
  expression layer (view -> chain -> selector -> structure -> paper book);
* a runner with ``instrument.type == "equity"`` keeps the existing flow;
* both flows coexist without breaking each other.
"""

from __future__ import annotations

import pytest

from backtest.forward.options_bridge import DEFAULT_EXPRESSION, OptionsBridge
from backtest.forward.paper_runner import OrderLedger, RunnerConfig, StrategyRunner
from backtest.forward.portfolio_manager import PortfolioManager
from backtest.forward.risk_supervisor import GlobalRiskConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bars(closes, start_day=1):
    """Closed candle dicts, one per close."""
    out = []
    for i, close in enumerate(closes):
        out.append(
            {
                "ts": f"2026-09-{start_day + i:02d}T09:15:00",
                "open": close - 5,
                "high": close + 5,
                "low": close - 10,
                "close": close,
                "volume": 1000,
            }
        )
    return out


def _option_config(**overrides):
    kwargs = dict(
        name="opt-runner",
        strategy_name="directional_options",
        allocated_capital=1_000_000,
        symbols=["NIFTY"],
        timeframe="1day",
        instrument={
            "type": "option",
            "expression": {
                "type": {"BULLISH": "bull_call_spread", "BEARISH": "bear_put_spread"}
            },
        },
    )
    kwargs.update(overrides)
    return RunnerConfig(**kwargs)


def _feed(runner, closes, start_day=1):
    for bar in _bars(closes, start_day=start_day):
        runner.process_candle_event("NIFTY", bar)


RISING = [24800 + i * 15 for i in range(20)]     # steady rally -> bullish
FALLING = [24800 - i * 15 for i in range(20)]    # steady slide -> bearish
FLAT = [24800.0] * 20                            # no conviction


# ---------------------------------------------------------------------------
# RunnerConfig.instrument validation
# ---------------------------------------------------------------------------


class TestInstrumentConfig:
    def test_default_instrument_is_equity(self):
        cfg = RunnerConfig(
            name="x", strategy_name="sma_crossover",
            allocated_capital=1000, symbols=["AAA"],
        )
        assert cfg.instrument == {"type": "equity"}

    def test_option_instrument_accepted(self):
        cfg = _option_config()
        assert cfg.instrument["type"] == "option"

    def test_unknown_instrument_type_rejected(self):
        with pytest.raises(ValueError, match="instrument.type"):
            RunnerConfig(
                name="x", strategy_name="sma_crossover",
                allocated_capital=1000, symbols=["AAA"],
                instrument={"type": "crypto"},
            )

    def test_non_dict_instrument_rejected(self):
        with pytest.raises(ValueError, match="instrument must be a dict"):
            RunnerConfig(
                name="x", strategy_name="sma_crossover",
                allocated_capital=1000, symbols=["AAA"],
                instrument="option",
            )


# ---------------------------------------------------------------------------
# The automated flow: strategy view -> expression layer -> paper book
# ---------------------------------------------------------------------------


class TestOptionRunnerFlow:
    def _runner(self, config=None):
        runner = StrategyRunner(config or _option_config(), ledger=OrderLedger())
        runner.start()
        return runner

    def test_rally_opens_bull_call_spread(self):
        """DirectionalOptionsStrategy auto-trades when NIFTY rises."""
        runner = self._runner()
        _feed(runner, RISING)

        summary = runner.options_summary()
        assert summary is not None
        assert summary["open_structures"] == 1
        assert summary["open_positions"] == 2  # spread = 2 legs
        assert summary["executed_count"] == 1

        entries = [s for s in runner.signal_log if s["kind"] == "OPTION_ENTRY"]
        assert entries and "bull_call_spread" in entries[-1]["reason"]

    def test_slide_opens_bear_structure(self):
        runner = self._runner()
        _feed(runner, FALLING)

        summary = runner.options_summary()
        assert summary["open_structures"] == 1
        entries = [s for s in runner.signal_log if s["kind"] == "OPTION_ENTRY"]
        assert "bear_put_spread" in entries[-1]["reason"]

    def test_flat_market_trades_nothing(self):
        runner = self._runner()
        _feed(runner, FLAT)
        assert runner.options_summary()["open_structures"] == 0
        assert runner.options_summary()["executed_count"] == 0

    def test_one_structure_at_a_time(self):
        """While a structure is open, further bullish bars add nothing.

        Exits are disabled here (task B1): this test is about *pyramiding*, and
        the default ``min_days_to_expiry=1`` rule would otherwise square the
        spread off near the fixture expiry and let the next signal re-enter,
        which is correct behaviour but not what this assertion measures.
        """
        runner = self._runner(
            _option_config(
                instrument={
                    "type": "option",
                    "expression": {
                        "type": {
                            "BULLISH": "bull_call_spread",
                            "BEARISH": "bear_put_spread",
                        },
                        "exit": {"signal_flip": False, "min_days_to_expiry": None},
                    },
                }
            )
        )
        _feed(runner, RISING)
        # Three more bars, still short of the fixture expiry: crossing it would
        # (correctly) settle the spread and let the next signal re-enter, which
        # is what B2 does — not what this test measures.
        _feed(runner, [RISING[-1] + i * 20 for i in range(1, 4)], start_day=21)

        summary = runner.options_summary()
        assert summary["open_structures"] == 1
        assert summary["executed_count"] == 1
        assert summary["settled_count"] == 0

    def test_state_exposes_instrument_and_options(self):
        runner = self._runner()
        _feed(runner, RISING)
        state = runner.get_state()
        assert state["instrument"]["type"] == "option"
        assert state["options"]["open_structures"] == 1

    def test_insufficient_capital_soft_rejected(self):
        """A book too small for the premium logs OPTION_BLOCKED, not a crash."""
        cfg = _option_config(allocated_capital=1_000)
        runner = self._runner(cfg)
        _feed(runner, RISING)

        assert runner.options_summary()["open_structures"] == 0
        blocked = [s for s in runner.signal_log if s["kind"] == "OPTION_BLOCKED"]
        assert blocked


# ---------------------------------------------------------------------------
# Coexistence: equity and option runners side by side
# ---------------------------------------------------------------------------


class TestCoexistence:
    def test_equity_runner_unaffected(self):
        equity_cfg = RunnerConfig(
            name="eq-runner", strategy_name="sma_crossover",
            allocated_capital=100_000, symbols=["NIFTY"], timeframe="1day",
        )
        runner = StrategyRunner(equity_cfg, ledger=OrderLedger())
        runner.start()
        _feed(runner, RISING)

        assert runner.options_summary() is None  # classic flow, no bridge
        assert runner.get_state()["instrument"]["type"] == "equity"

    def test_manager_runs_both_buckets(self):
        manager = PortfolioManager(
            risk_config=GlobalRiskConfig(daily_loss_limit=1_000_000, max_drawdown_pct=0.9),
            auto_start_feed=False,
        )
        try:
            opt_id = manager.add_runner(_option_config(), start=True)
            eq_id = manager.add_runner(
                RunnerConfig(
                    name="eq-runner", strategy_name="sma_crossover",
                    allocated_capital=100_000, symbols=["NIFTY"], timeframe="1day",
                ),
                start=True,
            )

            for bar in _bars(RISING):
                manager._on_bar("NIFTY", bar)

            opt_runner = manager.get_runner(opt_id)
            eq_runner = manager.get_runner(eq_id)
            assert opt_runner.options_summary()["open_structures"] == 1
            # Equity runner processed the same bars through the classic flow.
            assert eq_runner.bars_processed == len(RISING)
            assert eq_runner.options_summary() is None
        finally:
            manager.shutdown()


# ---------------------------------------------------------------------------
# Bridge unit behaviour
# ---------------------------------------------------------------------------


class TestOptionsBridge:
    def test_default_expression_is_direction_aware(self):
        assert DEFAULT_EXPRESSION["type"]["BULLISH"] == "bull_call_spread"
        assert DEFAULT_EXPRESSION["type"]["BEARISH"] == "bear_put_spread"

    def test_none_view_is_a_noop(self):
        bridge = OptionsBridge(capital=100_000)
        assert bridge.on_market_view(None, "s") is None

    def test_fixed_structure_type_overrides_direction(self):
        from backtest.strategy.intent import Direction, MarketView

        bridge = OptionsBridge(
            capital=1_000_000, expression={"type": "long_put"}
        )
        view = MarketView(
            direction=Direction.BULLISH, confidence=0.9,
            underlying="NIFTY", spot_price=None,
        )
        result = bridge.on_market_view(view, "s")
        assert result is not None and result["structure_type"] == "long_put"

    def test_view_spot_moves_the_synthetic_market(self):
        from decimal import Decimal

        from backtest.strategy.intent import Direction, MarketView

        bridge = OptionsBridge(capital=1_000_000)
        view = MarketView(
            direction=Direction.BULLISH, confidence=0.9,
            underlying="NIFTY", spot_price=Decimal("25100"),
        )
        result = bridge.on_market_view(view, "s")
        assert result is not None
        # Strikes were built around the view's spot, not the default 24800.
        assert all(abs(int(s) - 25100) <= 500 for s in result["strikes"])
