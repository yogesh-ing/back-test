"""Task A1 — per-bar mark-to-market for forward option books.

Before A1, ``OptionsBridge`` synced the synthetic market to a strategy's spot
**once**, at entry. Every leg then kept ``current_price == entry_price`` and
``unrealized_pnl == 0`` forever, so an option runner held a "position" that
could neither win nor lose — a 7,000-point NIFTY collapse moved nothing but
the fee stack.

These tests pin the fix:

* every closed bar re-prices the book (single **and** pool runners, plus the
  breaker stress path),
* P&L moves with the underlying and in the right direction,
* theta follows the **bar** clock, not the wall clock,
* a failure inside pricing never kills the runner,
* equity runners are bit-for-bit unaffected.

See ``docs/OPTIONS-FORWARD-TESTING.md`` → task A1.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from backtest.forward.options_bridge import OptionsBridge
from backtest.forward.paper_runner import (
    OPTION_MTM_LOG_EVERY,
    OrderLedger,
    RunnerConfig,
    StrategyRunner,
)
from backtest.options.quote_providers import (
    SyntheticChainGenerator,
    SyntheticQuoteProvider,
)
from backtest.strategy.intent import Direction, MarketView


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bars(closes, start_day=1):
    """Closed candle dicts, one per close (daily bars from 2026-09-01).

    ``start_day`` is an offset in **days from 2026-09-01**, so a series longer
    than 30 bars rolls into October instead of producing impossible dates like
    ``2026-09-44`` (which ``datetime.fromisoformat`` rejects, silently pinning
    the bridge's bar clock to the last valid bar).
    """
    base = date(2026, 9, 1) + timedelta(days=start_day - 1)
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


def _option_config(**overrides):
    kwargs = dict(
        name="opt-runner",
        strategy_name="directional_options",
        allocated_capital=1_000_000,
        symbols=["NIFTY"],
        timeframe="1day",
        instrument={
            "type": "option",
            # Exits are task B1; these tests isolate pricing, so hold forever.
            "expression": {
                "type": {"BULLISH": "bull_call_spread", "BEARISH": "bear_put_spread"},
                "exit": {"signal_flip": False, "min_days_to_expiry": None},
            },
        },
    )
    kwargs.update(overrides)
    return RunnerConfig(**kwargs)


def _running_option_runner(**overrides):
    runner = StrategyRunner(_option_config(**overrides), ledger=OrderLedger())
    runner.start()
    return runner


RISING = [24800 + i * 40 for i in range(20)]  # steady rally -> bull call spread
CRASH = [RISING[-1] - i * 300 for i in range(1, 25)]  # then a ~7,000 pt collapse


@pytest.fixture()
def open_structure_runner():
    """A runner that has opened one bullish spread and is still holding it."""
    runner = _running_option_runner()
    for bar in _bars(RISING):
        runner.process_candle_event("NIFTY", bar)
    assert runner.options_summary()["open_structures"] == 1
    return runner


# ---------------------------------------------------------------------------
# Bridge-level behaviour
# ---------------------------------------------------------------------------


class TestBridgeOnBar:
    def test_no_open_structure_is_a_noop(self):
        bridge = OptionsBridge(capital=1_000_000)
        assert bridge.on_bar("NIFTY", 24800.0, "2026-09-15T09:15:00") is None

    def test_entry_bar_starts_at_zero_pnl(self):
        """MTM runs *before* entry, so the entry bar itself cannot show P&L."""
        runner = _running_option_runner()
        bridge = runner.options_bridge
        for bar in _bars(RISING):
            runner.process_candle_event("NIFTY", bar)
            if bridge.open_structure_id is not None:
                break
        assert bridge.open_structure_id is not None
        assert bridge.last_unrealized_pnl == Decimal("0")

    def test_spot_move_moves_structure_pnl(self, open_structure_runner):
        """A collapse must hurt the long call spread."""
        bridge = open_structure_runner.options_bridge
        structure = bridge.option_broker.get_open_structures()[0]
        before = structure.total_unrealized_pnl
        assert before > 0  # the rally that opened the spread kept helping it

        for bar in _bars(CRASH, start_day=21):
            open_structure_runner.process_candle_event("NIFTY", bar)

        structure = bridge.option_broker.get_open_structures()[0]
        assert structure.total_unrealized_pnl < 0
        # A 50-point-wide spread on a 75-lot risks ~₹3,750 net of premium —
        # the swing must be a real spread P&L, not rounding noise.
        assert structure.total_unrealized_pnl <= Decimal("-1000")
        assert before - structure.total_unrealized_pnl > Decimal("2000")
        assert bridge.last_unrealized_pnl < 0

    def test_rally_helps_a_long_call_spread(self, open_structure_runner):
        bridge = open_structure_runner.options_bridge
        for bar in _bars([RISING[-1] + i * 60 for i in range(1, 10)], start_day=21):
            open_structure_runner.process_candle_event("NIFTY", bar)
        structure = bridge.option_broker.get_open_structures()[0]
        assert structure.total_unrealized_pnl > 0

    def test_legs_are_repriced_not_frozen(self, open_structure_runner):
        bridge = open_structure_runner.options_bridge
        structure = bridge.option_broker.get_open_structures()[0]
        entry_prices = [leg.current_price for leg in structure.legs]

        for bar in _bars(CRASH, start_day=21):
            open_structure_runner.process_candle_event("NIFTY", bar)

        structure = bridge.option_broker.get_open_structures()[0]
        assert [leg.current_price for leg in structure.legs] != entry_prices

    def test_book_equity_moves_more_than_the_fee_stack(self, open_structure_runner):
        """Equity must reflect the market, not just brokerage.

        This is the review finding in number form: before A1 the only thing
        that ever moved the option book was the ₹40 of statutory fees.
        """
        bridge = open_structure_runner.options_bridge
        broker = bridge.option_broker
        equity_before = broker.total_equity
        fees = broker.total_costs_paid

        for bar in _bars(CRASH, start_day=21):
            open_structure_runner.process_candle_event("NIFTY", bar)

        drawdown = equity_before - broker.total_equity
        assert drawdown > fees
        assert drawdown > 1_000
        assert bridge.summary()["equity"] == pytest.approx(float(broker.total_equity))

    def test_summary_exposes_the_book_clock(self, open_structure_runner):
        summary = open_structure_runner.options_summary()
        assert summary["unrealized_pnl"] > 0  # rally kept helping the spread
        assert summary["last_spot"] == pytest.approx(float(RISING[-1]))
        assert summary["last_mtm_ts"] == f"2026-09-{len(RISING):02d}T09:15:00"
        assert summary["quote_source"] == "synthetic:bs"

    def test_unparseable_timestamp_still_marks(self, open_structure_runner):
        bridge = open_structure_runner.options_bridge
        assert bridge.on_bar("NIFTY", 24_000.0, "not-a-timestamp") is not None

    def test_pricing_failure_is_swallowed(self, open_structure_runner):
        """A quote provider that raises must not take the runner down."""
        bridge = open_structure_runner.options_bridge
        bridge.quote_provider = _ExplodingQuoteProvider()
        assert bridge.on_bar("NIFTY", 24_000.0, "2026-09-25T09:15:00") is None
        open_structure_runner.process_candle_event(
            "NIFTY", _bars([24_000.0], start_day=25)[0]
        )
        assert open_structure_runner.status == "RUNNING"
        assert open_structure_runner.error is None


class _ExplodingQuoteProvider:
    """Quote provider double whose every lookup fails."""

    source_name = "exploding"

    def get_quote(self, instrument_token: str) -> dict:
        raise RuntimeError("feed down")


# ---------------------------------------------------------------------------
# Theta: decay follows the bar clock, not the wall clock
# ---------------------------------------------------------------------------


class TestBarClockTheta:
    def _long_call_bridge(self):
        bridge = OptionsBridge(
            capital=1_000_000,
            expression={"type": "long_call"},
            quote_provider=SyntheticQuoteProvider(SyntheticChainGenerator()),
        )
        view = MarketView(
            direction=Direction.BULLISH,
            confidence=0.9,
            underlying="NIFTY",
            spot_price=Decimal("24800"),
        )
        result = bridge.on_market_view(view, "directional_options")
        assert result is not None
        return bridge, bridge.option_broker.get_open_structures()[0]

    def test_premium_decays_as_bars_advance_on_a_flat_spot(self):
        """Flat spot + advancing bar timestamps → the option loses time value.

        The bar timestamps are derived from the structure's own expiry, so the
        assertion holds whenever the suite runs (the synthetic chain prices the
        next monthly expiry relative to today).
        """
        bridge, structure = self._long_call_bridge()
        expiry = structure.expiry
        leg = structure.legs[0]

        early = expiry - timedelta(days=6)
        later = expiry - timedelta(days=3)
        bridge.on_bar("NIFTY", 24_800.0, f"{early.isoformat()}T09:15:00")
        price_early = leg.current_price

        bridge.on_bar("NIFTY", 24_800.0, f"{later.isoformat()}T09:15:00")
        price_later = leg.current_price

        assert price_later < price_early
        assert structure.total_unrealized_pnl < 0  # flat market, bleeding theta

    def test_reference_is_pinned_to_the_bar_not_the_wall_clock(self):
        bridge, structure = self._long_call_bridge()
        expiry = structure.expiry
        bar_day = expiry - timedelta(days=4)

        bridge.on_bar("NIFTY", 24_800.0, f"{bar_day.isoformat()}T09:15:00")
        provider = bridge.quote_provider
        assert provider._reference is not None
        assert provider._reference.date() == bar_day
        assert provider._reference.tzinfo is None  # naive → price_contract is happy

    def test_timezone_aware_stamps_are_normalised(self):
        bridge, structure = self._long_call_bridge()
        day = structure.expiry - timedelta(days=5)
        assert bridge.on_bar("NIFTY", 24_800.0, f"{day.isoformat()}T03:45:00Z") is not None
        assert bridge.quote_provider._reference.tzinfo is None


# ---------------------------------------------------------------------------
# Runner wiring
# ---------------------------------------------------------------------------


class TestRunnerWiring:
    def test_every_bar_reaches_the_bridge(self):
        runner = _running_option_runner()
        calls = []
        original = runner.options_bridge.on_bar

        def spy(symbol, price, ts=None):
            calls.append((symbol, price, ts))
            return original(symbol, price, ts)

        runner.options_bridge.on_bar = spy
        bars = _bars(RISING[:14])
        for bar in bars:
            runner.process_candle_event("NIFTY", bar)

        assert [c[0] for c in calls] == ["NIFTY"] * len(bars)
        assert [c[1] for c in calls] == [b["close"] for b in bars]
        assert [c[2] for c in calls] == [b["ts"] for b in bars]

    def test_pool_runner_also_marks_the_book(self):
        """Pool runners defer the scan to on_tick_end but must still re-price."""
        runner = _running_option_runner(
            target_type="SYMBOL_UNIVERSE", symbols=["NIFTY", "BANKNIFTY"]
        )
        assert runner.options_bridge is not None
        bridge = runner.options_bridge
        # Open a structure directly (pool runners don't route views to options).
        bridge.on_market_view(
            MarketView(
                direction=Direction.BULLISH,
                confidence=0.9,
                underlying="NIFTY",
                spot_price=Decimal("24800"),
            ),
            "directional_options",
        )
        for bar in _bars(CRASH, start_day=21):
            runner.process_candle_event("NIFTY", bar)

        assert bridge.last_unrealized_pnl < 0

    def test_stress_markdown_reprices_the_book(self, open_structure_runner):
        runner = open_structure_runner
        runner.apply_markdown("NIFTY", 18_000.0, "2026-09-25T09:15:00")
        assert runner.options_bridge.last_unrealized_pnl < 0
        assert runner.last_option_pnl < 0

    def test_state_exposes_option_pnl(self, open_structure_runner):
        state = open_structure_runner.get_state()
        assert state["options"]["unrealized_pnl"] > 0
        assert state["option_pnl"] == pytest.approx(state["options"]["unrealized_pnl"])

        for bar in _bars(CRASH, start_day=21):
            open_structure_runner.process_candle_event("NIFTY", bar)

        state = open_structure_runner.get_state()
        assert state["option_pnl"] < 0
        assert state["options"]["unrealized_pnl"] < 0

    def test_throttled_mtm_heartbeat(self, open_structure_runner):
        runner = open_structure_runner
        runner.signal_log.clear()
        bars = _bars([RISING[-1]] * 5, start_day=21)
        for bar in bars:
            runner.process_candle_event("NIFTY", bar)

        heartbeats = [s for s in runner.signal_log if s["kind"] == "OPTION_MTM"]
        assert len(heartbeats) == 1  # one per OPTION_MTM_LOG_EVERY bars
        assert heartbeats[0]["reason"].startswith("option MTM")
        assert OPTION_MTM_LOG_EVERY == 5


# ---------------------------------------------------------------------------
# No regression for the classic equity flow
# ---------------------------------------------------------------------------


class TestEquityRunnersUnaffected:
    def test_equity_runner_has_no_bridge_and_no_extra_state(self):
        config = RunnerConfig(
            name="eq-runner",
            strategy_name="sma_crossover",
            allocated_capital=100_000,
            symbols=["NIFTY"],
            timeframe="1day",
        )
        runner = StrategyRunner(config, ledger=OrderLedger())
        runner.start()
        for bar in _bars(RISING):
            runner.process_candle_event("NIFTY", bar)

        state = runner.get_state()
        assert runner.options_bridge is None
        assert runner.options_summary() is None
        assert state["options"] is None
        assert state["option_pnl"] == 0.0
        assert not [s for s in runner.signal_log if s["kind"] == "OPTION_MTM"]

    def test_equity_equity_equals_portfolio_equity(self):
        """A1 must not move the classic numbers."""
        config = RunnerConfig(
            name="eq-runner",
            strategy_name="sma_crossover",
            allocated_capital=100_000,
            symbols=["NIFTY"],
            timeframe="1day",
        )
        runner = StrategyRunner(config, ledger=OrderLedger())
        runner.start()
        for bar in _bars(RISING):
            runner.process_candle_event("NIFTY", bar)
        assert runner.equity() == float(runner.portfolio.calculate_total_equity())
        assert runner.unrealized_pnl() == float(runner.portfolio.unrealized_pnl)
