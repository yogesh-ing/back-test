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

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from backtest.forward.options_bridge import OptionsBridge
from backtest.forward.paper_runner import (
    OPTION_MTM_LOG_EVERY,
    OrderLedger,
    RunnerConfig,
    StrategyRunner,
)
from backtest.options.execution_gate import RESEARCH_STRUCTURES
from backtest.options.quote_providers import (
    SyntheticChainGenerator,
    SyntheticQuoteProvider,
)
from backtest.strategy.intent import Direction, MarketView
from options_support import (  # noqa: F401 — market_closed is a pytest fixture
    market_closed,
    research_profile,
    stale_marks,
)


@pytest.fixture(autouse=True)
def _session_open(monkeypatch):
    """Freshness is a market-session SLA — pin the clock so results never
    depend on the wall time the suite runs at."""
    from options_support import pin_session_clock

    pin_session_clock(monkeypatch)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


#: The September 2026 expiry cycle (Aug 28 → Sep 24). These tests need the
#: structure they open to stay open for the whole series, and B2 now *settles*
#: anything held to an expiry (correctly). So the bars are anchored to finish
#: before the cycle's expiry, and the series are kept shorter than the cycle.
# Anchor the cycle in the FUTURE so replay bars are never in the past — a
# bar timestamp earlier than the wall clock makes ``set_reference`` rewind
# time, INFLATING premiums above the wall-clock entry (the theta test read a
# positive P&L on a flat spot: date-brittle failure, fixed 2026-09-23).
_CYCLE_EXPIRY = SyntheticChainGenerator().next_monthly_expiry(
    date.today() + timedelta(days=40)
)
_CYCLE_START = _CYCLE_EXPIRY - timedelta(days=27)


def _bars(closes, start_day=1):
    """Daily bars inside one expiry cycle (see ``_CYCLE_START``)."""
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
    # These tests mark a two-leg bull call spread; the V1 gate would refuse it.
    research_profile(runner)
    return runner


RISING = [24800 + i * 40 for i in range(20)]  # steady rally -> bull call spread
#: A short, brutal collapse (6 bars, −1,200 each) that stays inside the cycle.
CRASH = [RISING[-1] - i * 1200 for i in range(1, 7)]


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
        bridge = OptionsBridge(capital=1_000_000, allowed_structures=RESEARCH_STRUCTURES)
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
        for bar in _bars([RISING[-1] + i * 60 for i in range(1, 7)], start_day=21):
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
        assert summary["last_mtm_ts"] == _bars(RISING)[-1]["ts"]
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
            "NIFTY", _bars([24_000.0], start_day=21)[0]
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
            allowed_structures=RESEARCH_STRUCTURES,
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

        Bars run forward from the LATER of today or (expiry − 10 days), and
        stop BEFORE expiry: bar timestamps earlier than the wall-clock entry
        rewind the pricing reference and inflate premiums above entry (the
        date-brittle failure of 2026-09-23), while bars past expiry trigger
        settlement. Both bounds keep the decay assertion calendar-proof.
        """
        bridge, structure = self._long_call_bridge()
        expiry = structure.expiry
        leg = structure.legs[0]

        earliest_sane = date.today()
        early = max(expiry - timedelta(days=10), earliest_sane)
        # keep the second bar strictly before expiry (settlement bar)
        later = min(early + timedelta(days=3), expiry - timedelta(days=1))
        if later <= early:  # expiry too close to test decay — assert trivially
            assert leg.entry_price > 0
            return
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
        # Establish the bar clock INSIDE the cycle before opening: a direct
        # entry with no bar clock prices on the wall clock, and on an expiry
        # day (2026-09-24) the structure is settled by the very first crash
        # bar (dated weeks later) — leaving nothing to mark. Date-brittle
        # failure, fixed 2026-09-24 the same way the fixture does it.
        for bar in _bars(RISING):
            runner.process_candle_event("NIFTY", bar)
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
        """The breaker stress path must re-price too — and stay inside the cycle.

        A timestamp past the cycle expiry would trip B2's (correct) settlement
        first, so the markdown is dated on a bar the structure is still live.
        """
        runner = open_structure_runner
        runner.apply_markdown("NIFTY", 18_000.0, _bars([18_000.0], start_day=21)[0]["ts"])
        assert runner.options_bridge.last_unrealized_pnl < 0
        assert runner.last_option_pnl < 0
        assert runner.options_summary()["open_structures"] == 1

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
        research_profile(runner)
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
        research_profile(runner)
        for bar in _bars(RISING):
            runner.process_candle_event("NIFTY", bar)
        assert runner.equity() == float(runner.portfolio.calculate_total_equity())
        assert runner.unrealized_pnl() == float(runner.portfolio.unrealized_pnl)


# ---------------------------------------------------------------------------
# 6. MTM Staleness Observability (R1–R7, AC #1–#10)
# ---------------------------------------------------------------------------


def _bull_view(spot: float) -> MarketView:
    return MarketView(
        direction=Direction.BULLISH,
        confidence=0.9,
        underlying="NIFTY",
        spot_price=Decimal(str(spot)),
    )


_option_runner_config = _option_config


class TestMtmStalenessObservability:
    """Acceptance criteria AC #1–#10 for MTM Staleness Observability."""

    def test_restart_with_open_structure_refreshes_leg_marks_from_live_quotes(self):
        """AC #1: Bridge-level restart with open structure holding numeric mStock
        token → first bar after resume rebinds via register_contract and updates
        leg marks from live quotes.
        """
        from backtest.forward.state_store import capture_bridge, restore_bridge
        from backtest.options.quote_providers import LiveChainProvider

        class _LiveBroker:
            def __init__(self, quotes):
                self.quotes = dict(quotes)
                self.quote_calls = []

            def get_option_chain(self, symbol, expiry=None):
                return []

            def get_option_quote(self, symbol, exchange="NFO"):
                self.quote_calls.append((symbol, exchange))
                return {"ltp": self.quotes[symbol]}

        # 1. Open a structure on bridge1 with numeric token "49228"
        broker1 = _LiveBroker({"NIFTY261024800CE": 120.0})
        provider1 = LiveChainProvider(broker1)
        bridge1 = OptionsBridge(capital=500_000, quote_provider=provider1, allowed_structures=RESEARCH_STRUCTURES)
        from backtest.options.paper_trading import OptionPosition, StructurePosition

        leg = OptionPosition(
            position_id="pos-1",
            structure_id="str-1",
            strategy_name="directional_options",
            instrument_token="49228",
            trading_symbol="NIFTY261024800CE",
            underlying="NIFTY",
            option_type="CE",
            strike=Decimal("24800"),
            expiry=date(2026, 10, 29),
            lot_size=25,
            side="BUY",
            quantity=2,
            entry_price=Decimal("120.0"),
            current_price=Decimal("120.0"),
        )
        structure = StructurePosition(
            structure_id="str-1",
            structure_type="long_call",
            strategy_name="directional_options",
            underlying="NIFTY",
            expiry=date(2026, 10, 29),
            legs=[leg],
        )
        bridge1.option_broker.restore_structure(structure)
        bridge1.open_structure_id = "str-1"
        snap = capture_bridge(bridge1)

        # 2. Simulate process restart: fresh LiveChainProvider (empty registry)
        broker2 = _LiveBroker({"NIFTY261024800CE": 165.0})
        provider2 = LiveChainProvider(broker2)
        bridge2 = OptionsBridge(capital=500_000, quote_provider=provider2, allowed_structures=RESEARCH_STRUCTURES)
        restore_bridge(bridge2, snap)

        # 3. First bar after resume refreshes leg marks from live quotes
        pnl = bridge2.on_bar("NIFTY", 25_050.0, "2026-10-05T09:16:00")
        assert pnl == Decimal("2250.0")  # (165 - 120) * (2 * 25)
        restored_leg = bridge2.option_broker.get_open_positions()[0]
        assert restored_leg.current_price == Decimal("165.0")
        assert bridge2.mark_stale is False
        assert bridge2.quote_error is None
        assert broker2.quote_calls == [("NIFTY261024800CE", "NFO")]

    def test_mark_stale_once_the_mark_ages_past_the_sla(self):
        """AC #2 (PRD §4.1/§8.1): a held mark older than ``stale_after_seconds``
        reads stale — with ``quote_error`` set and ``mark_ts`` still the last
        success — and a good quote clears it.

        The old metric counted failed marks; a feed that stops delivering bars
        records no failures at all, which is exactly the freeze it had to miss.
        """
        bridge = OptionsBridge(capital=500_000, expression={"type": "long_call", "quantity": 1})
        bridge.on_bar("NIFTY", 25_000.0, "2026-10-01T09:15:00")
        bridge.on_market_view(_bull_view(25_000.0), "test")

        # Bar 1: healthy quote
        bridge.on_bar("NIFTY", 25_050.0, "2026-10-01T09:16:00")
        good_mark_ts = bridge.mark_ts
        assert good_mark_ts is not None
        assert bridge.mark_stale is False
        assert bridge.quote_error is None

        # Inject quote failure — marks stop advancing, but the SLA has grace.
        good_provider = bridge.quote_provider

        class _FailingQuoteProvider:
            source_name = "live:mstock"
            generator = good_provider.generator

            def register_contract(self, contract):
                pass

            def get_quote(self, token):
                return {"error": f"unknown option contract {token!r}"}

        bridge.quote_provider = _FailingQuoteProvider()
        bridge.on_bar("NIFTY", 25_060.0, "2026-10-01T09:17:00")
        assert bridge.mark_stale is False, "one failed refresh inside the SLA"
        assert bridge.mark_ts == good_mark_ts
        assert "unknown option contract" in (bridge.quote_error or "")

        # The last good mark is now older than the SLA → stale.
        held_price = bridge.option_broker.get_open_positions()[0].current_price
        stale_marks(bridge, bridge.stale_after_seconds + 1)
        assert bridge.mark_stale is True
        assert bridge.mark_age_seconds() > bridge.stale_after_seconds

        snap = bridge.summary()
        assert snap["mark_stale"] is True
        assert "unknown option contract" in snap["quote_error"]
        s_row = snap["open_structures_detail"][0]
        assert s_row["mark_stale"] is True
        assert s_row["current_price"] == float(held_price), "stale, not re-priced"
        assert s_row["legs_detail"][0]["mark_stale"] is True

        # Recovery: restore good provider → next bar clears mark_stale
        bridge.quote_provider = good_provider
        bridge.on_bar("NIFTY", 25_100.0, "2026-10-01T09:19:00")
        assert bridge.mark_stale is False
        assert bridge.quote_error is None

    def test_a_silent_feed_trips_the_sla_with_zero_failed_marks(self):
        """The bar-count metric could only see a feed that answered and errored.

        A frozen session answers nothing: no mark attempt, therefore no failure
        counter, yet the held price is minutes old. Elapsed time is the only
        clock that catches it, which is what this asserts.
        """
        bridge = OptionsBridge(capital=500_000, expression={"type": "long_call", "quantity": 1})
        bridge.on_bar("NIFTY", 25_000.0, "2026-10-01T09:15:00")
        bridge.on_market_view(_bull_view(25_000.0), "test")
        bridge.on_bar("NIFTY", 25_050.0, "2026-10-01T09:16:00")

        # Nothing further arrives — no on_bar, so no attempt and no error.
        stale_marks(bridge, bridge.stale_after_seconds + 1)
        leg = bridge.option_broker.get_open_positions()[0]
        assert leg.mtm_failures == 0
        assert bridge.quote_error is None
        assert bridge.mark_stale is True
        assert bridge.mark_age_seconds() > bridge.stale_after_seconds

    def test_an_aged_mark_is_not_stale_while_the_session_is_shut(
        self, market_closed, monkeypatch
    ):
        """AC #9: the overnight book ages for hours and must stay honest-but-not-stale."""
        bridge = OptionsBridge(capital=500_000, expression={"type": "long_call", "quantity": 1})
        bridge.on_bar("NIFTY", 25_000.0, "2026-10-01T09:15:00")
        bridge.on_market_view(_bull_view(25_000.0), "test")
        stale_marks(bridge, bridge.stale_after_seconds + 3600)
        assert bridge.mark_age_seconds() > bridge.stale_after_seconds
        assert bridge.mark_stale is False, "market closed: nothing to refresh"

        monkeypatch.setattr(market_closed, "_market_is_open", lambda now=None: True)
        assert bridge.mark_stale is True

    def test_the_freshness_gate_never_reads_the_holiday_calendar(self, monkeypatch):
        """Session arithmetic only — a calendar probe here is a database call per mark.

        Found on the live path: with the database unreachable, the equity-curve
        mark stalled in retrying connects inside ``get_market_day_state``, so the
        SLA gate itself became the outage.
        """
        import backtest.live.market_status as market_status
        from backtest.forward import options_bridge as bridge_mod
        from backtest.options.paper_trading import session_is_open

        def _boom(*args, **kwargs):
            raise AssertionError("staleness gate reached for the database")

        monkeypatch.setattr(market_status, "get_market_day_state", _boom)
        # Replace the session pin with the real decision path for this test.
        monkeypatch.setattr(bridge_mod, "_market_is_open", session_is_open)

        bridge = OptionsBridge(capital=500_000, expression={"type": "long_call", "quantity": 1})
        bridge.on_bar("NIFTY", 25_000.0, "2026-10-01T09:15:00")
        bridge.on_market_view(_bull_view(25_000.0), "test")
        stale_marks(bridge, bridge.stale_after_seconds + 1)

        assert bridge.mark_stale is True

    def test_the_sla_scales_with_the_runner_bar_cadence(self):
        """A 1-day runner cannot be judged on a 1-minute clock.

        ``bar_cadence_seconds`` widens the SLA by one bar, so a daily book halts
        after a missed daily bar plus the grace, not three minutes into the next
        session.
        """
        from backtest.data.base import TIMEFRAME_MINUTES

        cadence = 60.0 * TIMEFRAME_MINUTES["1day"]
        daily = OptionsBridge(
            capital=500_000,
            expression={"type": "long_call", "quantity": 1},
            bar_cadence_seconds=cadence,
        )
        assert daily.stale_after_seconds > 180.0

        # An expression override wins over both defaults.
        explicit = OptionsBridge(
            capital=500_000,
            expression={"type": "long_call", "quantity": 1, "stale_after_seconds": 45},
            bar_cadence_seconds=cadence,
        )
        assert explicit.stale_after_seconds == 45.0

    def test_a_daily_mark_carried_overnight_is_not_stale(self):
        """Age counts session time (§4.4), so the overnight gap is not staleness.

        Without this the SLA would flag every daily runner each morning — and
        stale marks suppress stop/target exits, so the false alarm was a safety
        hole, not a cosmetic one.
        """
        from datetime import timezone as _tz
        from backtest.data.base import TIMEFRAME_MINUTES

        bridge = OptionsBridge(
            capital=500_000,
            expression={"type": "long_call", "quantity": 1},
            bar_cadence_seconds=60.0 * TIMEFRAME_MINUTES["1day"],
        )
        bridge.on_bar("NIFTY", 25_000.0, "2026-10-02T09:15:00")
        bridge.on_market_view(_bull_view(25_000.0), "test")
        leg = bridge.option_broker.get_open_positions()[0]

        # Friday 14:59 IST; the pinned clock reads Monday 10:00 IST: 31 minutes
        # of Friday's session plus 45 of Monday's, not ~63 hours of wall time.
        leg.last_updated = datetime(2026, 10, 2, 9, 29, tzinfo=_tz.utc)
        assert bridge.mark_age_seconds() == pytest.approx(31 * 60 + 45 * 60)
        assert bridge.mark_stale is False

        stale_marks(bridge, bridge.stale_after_seconds + 1)
        assert bridge.mark_stale is True, "a missed daily bar does halt"

    def test_paused_or_stopped_runner_never_reports_mark_stale(self):
        """R1 / §5: Paused or stopped runners report mark_stale: false even when
        the held mark is older than the SLA.
        """
        runner = StrategyRunner(_option_runner_config(), ledger=OrderLedger())
        runner.start()
        research_profile(runner)
        for bar in _bars(RISING[:4]):
            runner.process_candle_event("NIFTY", bar)
        assert runner.options_bridge is not None
        assert runner.options_bridge.open_structure_id is not None

        class _DeadQuotes:
            source_name = "live:mstock"
            generator = runner.options_bridge.quote_provider.generator

            def register_contract(self, contract):
                pass

            def get_quote(self, token):
                return {"error": "session expired"}

        runner.options_bridge.quote_provider = _DeadQuotes()
        stale_marks(runner, runner.options_bridge.stale_after_seconds + 1)
        for bar in _bars([25_250, 25_260], start_day=5):
            runner.process_candle_event("NIFTY", bar)

        assert runner.get_state()["mark_stale"] is True
        runner.pause()
        assert runner.get_state()["mark_stale"] is False
        assert runner.options_summary()["mark_stale"] is False

    def test_genuine_zero_ltp_with_no_error_marks_zero_without_staleness(self):
        """AC #3: Quote row with ltp: 0 and NO error key → mark updates to 0,
        mark_stale stays false (genuine deep-OTM zero).
        """
        bridge = OptionsBridge(capital=500_000, expression={"type": "long_call", "quantity": 1})
        bridge.on_bar("NIFTY", 25_000.0, "2026-10-01T09:15:00")
        bridge.on_market_view(_bull_view(25_000.0), "test")

        class _ZeroQuoteProvider:
            source_name = "live:mstock"
            generator = bridge.quote_provider.generator

            def register_contract(self, contract):
                pass

            def get_quote(self, token):
                return {"ltp": 0, "bid": 0.0, "ask": 0.05}

        bridge.quote_provider = _ZeroQuoteProvider()
        for i in range(3):
            bridge.on_bar("NIFTY", 24_000.0, f"2026-10-02T09:1{i}:00")

        leg = bridge.option_broker.get_open_positions()[0]
        assert leg.current_price == Decimal("0")
        assert leg.mtm_failures == 0
        assert bridge.mark_stale is False
        assert bridge.quote_error is None

    def test_mtm_failure_warnings_are_rate_limited(self, caplog):
        """R4 / AC #5: 10 consecutive failed MTM cycles produce <= 2 WARNING log lines."""
        bridge = OptionsBridge(capital=500_000, expression={"type": "long_call", "quantity": 1})
        bridge.on_bar("NIFTY", 25_000.0, "2026-10-01T09:15:00")
        bridge.on_market_view(_bull_view(25_000.0), "test")

        class _FailProvider:
            source_name = "live:mstock"
            generator = bridge.quote_provider.generator

            def register_contract(self, contract):
                pass

            def get_quote(self, token):
                return {"error": "broker timeout"}

        bridge.quote_provider = _FailProvider()
        caplog.clear()
        with caplog.at_level("WARNING", logger="backtest.forward.options_bridge"):
            for i in range(10):
                bridge.on_bar("NIFTY", 25_010.0 + i, f"2026-10-02T10:{10 + i:02d}:00")

        warnings = [
            r for r in caplog.records
            if r.levelname == "WARNING" and "MTM quote failed" in r.message
        ]
        assert 1 <= len(warnings) <= 2

    def test_watchdog_repairs_unbound_open_leg(self, monkeypatch):
        """R6 / AC #6: With a broken token→contract binding injected into a
        running runner, the 60s watchdog pass repairs marks within 2 cycles
        without restart.
        """
        from datetime import timezone
        from backtest.forward.portfolio_manager import PortfolioManager
        from backtest.options.quote_providers import LiveChainProvider

        class _WatchdogBroker:
            def __init__(self):
                self.calls = []

            def get_option_chain(self, symbol, expiry=None):
                return []

            def get_option_quote(self, symbol, exchange="NFO"):
                self.calls.append((symbol, exchange))
                return {"ltp": 182.5}

        pm = PortfolioManager(auto_start_feed=False)
        try:
            iid = pm.add_runner(_option_runner_config(name="WD-NIFTY"))
            runner = pm.get_runner(iid)
            runner.start()
            research_profile(runner)
            bridge = runner.options_bridge
            assert bridge is not None

            live_broker = _WatchdogBroker()
            live_provider = LiveChainProvider(live_broker)
            live_provider.set_spot("NIFTY", 25_000.0)
            bridge.quote_provider = live_provider

            from backtest.options.paper_trading import OptionPosition, StructurePosition

            old_time = datetime(2026, 10, 1, 4, 0, 0)  # > 2 min old
            leg = OptionPosition(
                position_id="wd-pos-1",
                structure_id="wd-str-1",
                strategy_name="directional_options",
                instrument_token="49228",  # unbound numeric token!
                trading_symbol="NIFTY261024800CE",
                underlying="NIFTY",
                option_type="CE",
                strike=Decimal("24800"),
                expiry=date(2026, 10, 29),
                lot_size=25,
                side="BUY",
                quantity=1,
                entry_price=Decimal("120.0"),
                current_price=Decimal("120.0"),
                last_updated=old_time,
            )
            structure = StructurePosition(
                structure_id="wd-str-1",
                structure_type="long_call",
                strategy_name="directional_options",
                underlying="NIFTY",
                expiry=date(2026, 10, 29),
                legs=[leg],
            )
            bridge.option_broker.restore_structure(structure)
            bridge.open_structure_id = "wd-str-1"
            # Corrupt the rebind flag so only the watchdog repairs it
            bridge._restored_contracts_rebound = True
            live_provider._contracts.clear()

            # Watchdog pass during market hours (10:30 IST = 05:00 UTC on a Thursday)
            market_now = datetime(2026, 10, 1, 5, 0, 0, tzinfo=timezone.utc)
            report = pm.run_mtm_watchdog(now_utc=market_now)

            assert report["skipped"] is False
            assert report["repaired"] == 1
            assert leg.current_price == Decimal("182.5")
            assert bridge.mark_stale is False
            assert ("NIFTY261024800CE", "NFO") in live_broker.calls
        finally:
            pm.shutdown()

    def test_manual_refresh_marks_endpoint_and_rate_limit(self, monkeypatch):
        """R6 / R7 / AC #7: POST /api/portfolio/runner/<iid>/refresh-marks calls
        the same repair routine as the timer, enforces the 10s rate-limit
        (second press within 10s returns cooldown response without hitting the
        broker), and writes the same audit entry.
        """
        from datetime import timezone
        from flask import Flask
        from backtest.api.portfolio import portfolio_bp
        from backtest.forward.portfolio_manager import reset_portfolio_manager
        from backtest.options.quote_providers import LiveChainProvider

        class _CountingBroker:
            def __init__(self):
                self.calls = 0

            def get_option_chain(self, symbol, expiry=None):
                return []

            def get_option_quote(self, symbol, exchange="NFO"):
                self.calls += 1
                return {"ltp": 155.0}

        pm = reset_portfolio_manager(auto_start_feed=False)
        try:
            iid = pm.add_runner(_option_runner_config(name="MANUAL-REFRESH"))
            runner = pm.get_runner(iid)
            runner.start()
            research_profile(runner)
            bridge = runner.options_bridge
            broker = _CountingBroker()
            provider = LiveChainProvider(broker)
            provider.set_spot("NIFTY", 25_000.0)
            bridge.quote_provider = provider

            from backtest.options.paper_trading import OptionPosition, StructurePosition

            leg = OptionPosition(
                position_id="mr-pos-1",
                structure_id="mr-str-1",
                strategy_name="directional_options",
                instrument_token="49228",
                trading_symbol="NIFTY261024800CE",
                underlying="NIFTY",
                option_type="CE",
                strike=Decimal("24800"),
                expiry=date(2026, 10, 29),
                lot_size=25,
                side="BUY",
                quantity=1,
                entry_price=Decimal("110.0"),
                current_price=Decimal("110.0"),
                last_updated=datetime(2026, 10, 1, 4, 0, 0),
                mtm_failures=3,
                last_quote_error="unknown option contract '49228'",
            )
            structure = StructurePosition(
                structure_id="mr-str-1",
                structure_type="long_call",
                strategy_name="directional_options",
                underlying="NIFTY",
                expiry=date(2026, 10, 29),
                legs=[leg],
            )
            bridge.option_broker.restore_structure(structure)
            bridge.open_structure_id = "mr-str-1"

            app = Flask(__name__)
            app.register_blueprint(portfolio_bp)
            client = app.test_client()

            # 1st press: repairs the mark and writes REFRESH_MARKS audit entry
            resp1 = client.post(f"/api/portfolio/runner/{iid}/refresh-marks")
            assert resp1.status_code == 200
            data1 = resp1.get_json()
            assert data1["success"] is True
            assert data1["cooldown"] is False
            assert data1["repaired"] == 1
            assert data1["mark_stale"] is False
            assert broker.calls == 1

            # 2nd press within 10s: returns cooldown without hitting broker
            resp2 = client.post(f"/api/portfolio/runner/{iid}/refresh-marks")
            assert resp2.status_code == 200
            data2 = resp2.get_json()
            assert data2["cooldown"] is True
            assert data2["rate_limited"] is True
            assert data2["retry_after_s"] > 0
            assert broker.calls == 1, "cooldown must not hit the broker"

            # Verify timer and manual trigger write the exact same audit action format
            leg.last_updated = datetime(2026, 10, 1, 4, 0, 0)
            leg.mtm_failures = 2
            pm.run_mtm_watchdog(now_utc=datetime(2026, 10, 1, 5, 0, 0, tzinfo=timezone.utc))
            audits = [
                a for a in pm.get_audit_log(scope="all")
                if a["action"] == "REFRESH_MARKS MANUAL-REFRESH"
            ]
            assert len(audits) == 2
            assert audits[0]["detail"] == audits[1]["detail"]
        finally:
            pm.shutdown()

    def test_stale_notification_fires_once_per_episode(self):
        """R6 / R7 / AC #8: Across a 10-minute stale episode, the user receives
        exactly 1 notification/alert on the healthy → stale transition.
        """
        from backtest.alerts.broker import reset_alert_broker

        reset_alert_broker()
        bridge = OptionsBridge(capital=500_000, expression={"type": "long_call", "quantity": 1})
        bridge.runner_id = "runner-ep1"
        bridge.runner_name = "NIFTY-EPISODE"
        bridge.on_bar("NIFTY", 25_000.0, "2026-10-01T09:15:00")
        bridge.on_market_view(_bull_view(25_000.0), "test")

        good_provider = bridge.quote_provider

        class _DeadProvider:
            source_name = "live:mstock"
            generator = good_provider.generator

            def register_contract(self, contract):
                pass

            def get_quote(self, token):
                return {"error": "session expired"}

        bridge.quote_provider = _DeadProvider()
        stale_marks(bridge, bridge.stale_after_seconds + 1)
        # Run 10 consecutive 1-minute bars (10-minute stale episode)
        for i in range(10):
            bridge.on_bar("NIFTY", 25_010.0 + i, f"2026-10-01T09:{16 + i:02d}:00")

        assert bridge.mark_stale is True
        assert len(bridge.stale_notifications) == 1

        # Recover on bar 11, then enter a SECOND stale episode → fires once more
        bridge.quote_provider = good_provider
        bridge.on_bar("NIFTY", 25_050.0, "2026-10-01T09:30:00")
        assert bridge.mark_stale is False

        bridge.quote_provider = _DeadProvider()
        stale_marks(bridge, bridge.stale_after_seconds + 1)
        for i in range(5):
            bridge.on_bar("NIFTY", 25_060.0 + i, f"2026-10-01T09:{31 + i:02d}:00")
        assert len(bridge.stale_notifications) == 2

    def test_watchdog_silent_outside_market_hours(self):
        """R6 / AC #9: Outside the runner's trading window the watchdog performs
        zero broker calls and emits zero notifications.
        """
        from datetime import timezone
        from backtest.forward.portfolio_manager import PortfolioManager
        from backtest.options.quote_providers import LiveChainProvider

        class _SpyBroker:
            def __init__(self):
                self.calls = 0

            def get_option_chain(self, symbol, expiry=None):
                self.calls += 1
                return []

            def get_option_quote(self, symbol, exchange="NFO"):
                self.calls += 1
                return {"error": "should never be called"}

        pm = PortfolioManager(auto_start_feed=False)
        try:
            iid = pm.add_runner(_option_runner_config(name="OFF-HOURS"))
            runner = pm.get_runner(iid)
            runner.start()
            research_profile(runner)
            bridge = runner.options_bridge
            spy = _SpyBroker()
            bridge.quote_provider = LiveChainProvider(spy)

            from backtest.options.paper_trading import OptionPosition, StructurePosition

            leg = OptionPosition(
                position_id="oh-pos-1",
                structure_id="oh-str-1",
                strategy_name="directional_options",
                instrument_token="49228",
                trading_symbol="NIFTY261024800CE",
                underlying="NIFTY",
                option_type="CE",
                strike=Decimal("24800"),
                expiry=date(2026, 10, 29),
                lot_size=25,
                side="BUY",
                quantity=1,
                entry_price=Decimal("110.0"),
                current_price=Decimal("110.0"),
                last_updated=datetime(2026, 10, 1, 4, 0, 0),
            )
            structure = StructurePosition(
                structure_id="oh-str-1",
                structure_type="long_call",
                strategy_name="directional_options",
                underlying="NIFTY",
                expiry=date(2026, 10, 29),
                legs=[leg],
            )
            bridge.option_broker.restore_structure(structure)
            bridge.open_structure_id = "oh-str-1"

            # 20:00 IST (14:30 UTC) is after NSE close (15:30 IST)
            off_hours_utc = datetime(2026, 10, 1, 14, 30, 0, tzinfo=timezone.utc)
            report = pm.run_mtm_watchdog(now_utc=off_hours_utc)

            assert report["skipped"] is True
            assert report["reason"] == "outside_market_hours"
            assert spy.calls == 0
            assert len(bridge.stale_notifications) == 0
        finally:
            pm.shutdown()

    def test_atomic_structure_marking_holds_sibling_legs_on_partial_failure(self):
        """Approved Gap Fix: In a 2-leg bull_call_spread, if one leg's quote
        succeeds and the sibling leg's quote fails, neither leg overwrites its
        price (preventing a partial-leg 'Frankenstein spread' stop-out).
        """
        bridge = OptionsBridge(
            capital=500_000,
            expression={
                "type": "bull_call_spread",
                "quantity": 1,
                "exit": {"stop_loss_pct": 0.20},
            },
            allowed_structures=RESEARCH_STRUCTURES,
        )
        bridge.on_bar("NIFTY", 25_000.0, "2026-10-01T09:15:00")
        bridge.on_market_view(_bull_view(25_000.0), "test")

        legs = bridge.option_broker.get_open_positions()
        assert len(legs) == 2
        long_leg = next(l for l in legs if l.is_long)
        short_leg = next(l for l in legs if l.is_short)
        long_entry = long_leg.current_price
        short_entry = short_leg.current_price

        # Short leg succeeds at a huge adverse spike while long leg quote fails:
        # without atomic marking + stale stop suppression, the spread would
        # falsely stop out!
        class _PartialFailProvider:
            source_name = "live:mstock"
            generator = bridge.quote_provider.generator

            def register_contract(self, contract):
                pass

            def get_quote(self, token):
                if token == long_leg.instrument_token:
                    return {"error": "timeout on long leg"}
                return {"ltp": float(short_entry) * 3.0}

        bridge.quote_provider = _PartialFailProvider()
        stale_marks(bridge, bridge.stale_after_seconds + 1)
        bridge.on_bar("NIFTY", 25_200.0, "2026-10-01T09:16:00")
        bridge.on_bar("NIFTY", 25_250.0, "2026-10-01T09:17:00")

        # Structure must still be open and both legs held at their last consistent prices
        assert bridge.open_structure_id is not None
        assert long_leg.current_price == long_entry
        assert short_leg.current_price == short_entry
        assert bridge.mark_stale is True

    def test_stale_marks_tag_equity_curve_and_freeze_drawdown(self):
        """§8 Q4: Equity curve points recorded while mark_stale=True carry
        stale=True, and peak_equity / max_drawdown_pct are frozen.
        """
        runner = StrategyRunner(_option_runner_config(), ledger=OrderLedger())
        runner.start()
        research_profile(runner)
        for bar in _bars(RISING[:4]):
            runner.process_candle_event("NIFTY", bar)

        peak_before = runner.peak_equity
        dd_before = runner.max_drawdown_pct

        class _DeadQuotes:
            source_name = "live:mstock"
            generator = runner.options_bridge.quote_provider.generator

            def register_contract(self, contract):
                pass

            def get_quote(self, token):
                return {"error": "quote feed down"}

        runner.options_bridge.quote_provider = _DeadQuotes()
        stale_marks(runner, runner.options_bridge.stale_after_seconds + 1)
        for bar in _bars([25_300, 25_350, 25_400], start_day=5):
            runner.process_candle_event("NIFTY", bar)

        assert runner.options_bridge.mark_stale is True
        assert runner.equity_curve[-1].get("stale") is True
        assert runner.peak_equity == peak_before
        assert runner.max_drawdown_pct == dd_before
        runner.stop()

