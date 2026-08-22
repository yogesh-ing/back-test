"""Tests for the order execution simulator (Step 9).

The important distinctions under test:

* **no-fill vs rejection** — a limit away from the market must keep resting,
  a halted symbol must die. Conflating them either strands orders or kills
  live ones.
* **liquidity caps** — a large order fills in pieces, it does not absorb the
  whole book.
* **queue position** — a limit that is merely touched does not always fill.
* **determinism** — the same seed must give the same answers, or two
  strategies cannot be compared.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from backtest.simulator import (
    CommissionCalculator,
    ExecutionConfig,
    ExecutionEvent,
    ExecutionStatus,
    Order,
    OrderExecutor,
    OrderStatus,
    Portfolio,
    PortfolioLimits,
    RealismLevel,
    RejectionCode,
    SlippageCalculator,
    TradeSegment,
    ValidationError,
    load_execution_config,
)

D = Decimal
IST = ZoneInfo("Asia/Kolkata")

# volume 10,000 → 10% participation gives 1,000 shares of capacity
MD = {"bid": 999.5, "ask": 1000.5, "last": 1000, "volume": 10_000,
      "avg_volume": 1_000_000, "atr": 15}


def executor(**kw) -> OrderExecutor:
    kw.setdefault("fees", CommissionCalculator.for_broker("zerodha"))
    config = kw.pop("config", None) or ExecutionConfig(seed=7)
    return OrderExecutor(config=config, **kw)


def buy(qty=100, **kw) -> Order:
    o = Order.market("INFY", "buy", qty, **kw)
    o.submit()
    return o


# ===========================================================================
# Configuration
# ===========================================================================


class TestExecutionConfig:
    @pytest.mark.parametrize("level", RealismLevel.ALL)
    def test_presets_build(self, level):
        assert ExecutionConfig.preset(level).realism == level

    def test_presets_are_ordered_by_pessimism(self):
        opt = ExecutionConfig.preset("optimistic")
        real = ExecutionConfig.preset("realistic")
        pess = ExecutionConfig.preset("pessimistic")
        assert opt.max_participation > real.max_participation > pess.max_participation
        assert opt.touch_fill_probability > real.touch_fill_probability > pess.touch_fill_probability
        assert opt.max_latency_ms < real.max_latency_ms < pess.max_latency_ms

    def test_unknown_level_rejected(self):
        with pytest.raises(ValidationError, match="unknown realism level"):
            ExecutionConfig.preset("magical")

    @pytest.mark.parametrize("kwargs, match", [
        (dict(min_latency_ms=100, max_latency_ms=50), "max_latency_ms"),
        (dict(max_participation=0), "max_participation must be positive"),
        (dict(touch_fill_probability=D("1.5")), "probability"),
        (dict(price_improvement_probability=D("2")), "probability"),
        (dict(min_latency_ms=-1), "must not be negative"),
    ])
    def test_invalid_config_rejected(self, kwargs, match):
        with pytest.raises(ValidationError, match=match):
            ExecutionConfig(**kwargs)

    def test_halted_symbols_normalised(self):
        assert "INFY" in ExecutionConfig(halted_symbols={"infy"}).halted_symbols

    def test_ships_a_loadable_file(self):
        assert load_execution_config().realism in RealismLevel.ALL

    @pytest.mark.parametrize("profile", RealismLevel.ALL)
    def test_every_shipped_profile_loads(self, profile):
        assert load_execution_config(profile=profile).realism == profile

    def test_unknown_profile_lists_options(self):
        with pytest.raises(ValidationError, match="unknown execution profile"):
            load_execution_config(profile="wishful")

    def test_unknown_keys_rejected(self, tmp_path):
        bad = tmp_path / "e.yaml"
        bad.write_text("default:\n  nonsense: 1\n")
        with pytest.raises(ValidationError, match="unknown execution config keys"):
            load_execution_config(path=str(bad))

    def test_missing_explicit_file_is_an_error(self):
        with pytest.raises(ValidationError, match="not found"):
            load_execution_config(path="/nonexistent/execution.yaml")

    def test_session_times_parsed(self, tmp_path):
        cfg = tmp_path / "e.yaml"
        cfg.write_text('default:\n  session_open: "10:00"\n')
        assert load_execution_config(path=str(cfg)).session_open.hour == 10


# ===========================================================================
# Market orders
# ===========================================================================


class TestMarketOrders:
    def test_fills_completely_when_liquidity_allows(self):
        ex = executor()
        order = buy(500)
        result = ex.execute(order, MD)
        assert result.status == ExecutionStatus.FILLED
        assert result.filled_quantity == D("500.00000000")
        assert order.status is OrderStatus.FILLED

    def test_buy_lifts_the_ask(self):
        ex = executor(slippage=SlippageCalculator.disabled())
        result = ex.execute(buy(100), MD)
        assert result.fill.fill_price >= D("1000.5")

    def test_sell_hits_the_bid(self):
        ex = executor(slippage=SlippageCalculator.disabled())
        order = Order.market("INFY", "sell", 100)
        order.submit()
        result = ex.execute(order, MD)
        assert result.fill.fill_price <= D("999.5")

    def test_fill_carries_slippage_reference(self):
        result = executor().execute(buy(100), MD)
        assert result.fill.reference_price is not None
        assert result.fill.slippage_bps >= 0

    def test_fill_carries_fees(self):
        ex = executor(fees=CommissionCalculator.for_broker("india_zero"))
        result = ex.execute(buy(100), MD)
        assert result.fill.total_fees > D("0")

    def test_market_orders_are_takers(self):
        assert executor().execute(buy(100), MD).fill.liquidity_flag == "taker"

    def test_wrong_type_rejected_by_the_helper(self):
        order = Order.limit("INFY", "buy", 100, 1000)
        order.submit()
        with pytest.raises(ValidationError, match="wrong_order_type|process_market_order"):
            executor().process_market_order(order, MD)


# ===========================================================================
# Liquidity and partial fills
# ===========================================================================


class TestLiquidity:
    def test_large_order_fills_partially(self):
        """10% of 10,000 volume = 1,000 shares of capacity."""
        ex = executor()
        order = buy(5000)
        result = ex.execute(order, MD)
        assert result.status == ExecutionStatus.PARTIAL
        assert result.filled_quantity == D("1000.00000000")
        assert result.remaining_quantity == D("4000.00000000")
        assert order.status is OrderStatus.PARTIAL

    def test_remainder_keeps_working(self):
        ex = executor()
        order = buy(5000)
        ex.execute(order, MD)
        assert order.is_working

    def test_repeated_ticks_fill_the_rest(self):
        ex = executor()
        order = buy(3000)
        for _ in range(3):
            ex.execute(order, MD)
        assert order.status is OrderStatus.FILLED

    def test_available_liquidity_reported(self):
        result = executor().execute(buy(5000), MD)
        assert result.available_liquidity == D("1000.00000000")

    def test_participation_cap_is_configurable(self):
        ex = executor(config=ExecutionConfig(max_participation=D("0.5"), seed=7))
        assert ex.execute(buy(50_000), MD).filled_quantity == D("5000.00000000")

    def test_partial_fills_can_be_disabled(self):
        ex = executor(config=ExecutionConfig(allow_partial_fills=False, seed=7))
        result = ex.execute(buy(5000), MD)
        assert result.status == ExecutionStatus.NO_FILL
        assert "partial fills disabled" in result.reason

    def test_zero_volume_is_rejected(self):
        result = executor().execute(buy(100), {**MD, "volume": 0})
        assert result.rejection_code == RejectionCode.NO_LIQUIDITY

    def test_missing_volume_assumed_plentiful_by_default(self):
        md = {k: v for k, v in MD.items() if k != "volume"}
        assert executor().execute(buy(5000), md).status == ExecutionStatus.FILLED

    def test_missing_volume_rejected_when_required(self):
        ex = executor(config=ExecutionConfig(require_volume=True, seed=7))
        md = {k: v for k, v in MD.items() if k != "volume"}
        result = ex.execute(buy(100), md)
        assert result.rejection_code == RejectionCode.NO_LIQUIDITY


# ===========================================================================
# Limit orders and queue position
# ===========================================================================


class TestLimitOrders:
    def test_away_from_market_rests(self):
        """Not a rejection — the order must stay alive."""
        ex = executor()
        order = Order.limit("INFY", "buy", 100, 900)
        order.submit()
        result = ex.execute(order, MD)
        assert result.status == ExecutionStatus.NO_FILL
        assert result.rejection_code is None
        assert order.is_working

    def test_traded_through_always_fills(self):
        """Certain fill when the market moves past the limit."""
        ex = executor(config=ExecutionConfig(touch_fill_probability=D("0"), seed=7))
        order = Order.limit("INFY", "buy", 100, 1100)
        order.submit()
        assert ex.execute(order, MD).status == ExecutionStatus.FILLED

    def test_touch_never_fills_at_zero_probability(self):
        ex = executor(config=ExecutionConfig(touch_fill_probability=D("0"), seed=7))
        order = Order.limit("INFY", "buy", 100, 1000.5)   # exactly the ask
        order.submit()
        result = ex.execute(order, MD)
        assert result.status == ExecutionStatus.NO_FILL
        assert "queue position" in result.reason

    def test_touch_always_fills_at_probability_one(self):
        ex = executor(config=ExecutionConfig(touch_fill_probability=D("1"), seed=7))
        order = Order.limit("INFY", "buy", 100, 1000.5)
        order.submit()
        assert ex.execute(order, MD).status == ExecutionStatus.FILLED

    def test_touch_probability_is_respected_statistically(self):
        """Roughly half of touched limits should fill at p=0.5."""
        ex = executor(config=ExecutionConfig(touch_fill_probability=D("0.5"), seed=1))
        fills = 0
        for _ in range(200):
            order = Order.limit("INFY", "buy", 100, 1000.5)
            order.submit()
            if ex.execute(order, MD).did_trade:
                fills += 1
        assert 70 <= fills <= 130          # generous band around 100

    def test_never_fills_worse_than_the_limit(self):
        ex = executor()
        order = Order.limit("INFY", "buy", 1000, 1000.6)
        order.submit()
        result = ex.execute(order, MD)
        if result.did_trade:
            assert result.fill.fill_price <= D("1000.6")

    def test_limit_orders_are_makers(self):
        ex = executor(config=ExecutionConfig(touch_fill_probability=D("1"), seed=7))
        order = Order.limit("INFY", "buy", 100, 1100)
        order.submit()
        assert ex.execute(order, MD).fill.liquidity_flag == "maker"

    def test_sell_limit_traded_through(self):
        ex = executor(config=ExecutionConfig(touch_fill_probability=D("0"), seed=7))
        order = Order.limit("INFY", "sell", 100, 900)
        order.submit()
        assert ex.execute(order, MD).status == ExecutionStatus.FILLED

    def test_wrong_type_rejected_by_the_helper(self):
        with pytest.raises(ValidationError):
            executor().process_limit_order(buy(100), MD)


# ===========================================================================
# Stop orders
# ===========================================================================


class TestStopOrders:
    def test_untriggered_stop_does_not_fill(self):
        order = Order.stop("INFY", "sell", 100, stop_price=900)
        order.submit()
        result = executor().execute(order, MD)
        assert result.status == ExecutionStatus.NO_FILL
        assert order.is_working

    def test_triggered_stop_becomes_a_market_order(self):
        order = Order.stop("INFY", "sell", 100, stop_price=1010)
        order.submit()
        result = executor().execute(order, MD)
        assert result.status == ExecutionStatus.FILLED
        assert order.triggered

    def test_buy_stop_triggers_upward(self):
        order = Order.stop("INFY", "buy", 100, stop_price=990)
        order.submit()
        assert executor().execute(order, MD).status == ExecutionStatus.FILLED

    def test_stop_limit_needs_both_conditions(self):
        ex = executor(config=ExecutionConfig(touch_fill_probability=D("1"), seed=7))
        order = Order.stop_limit("INFY", "sell", 100, stop_price=1010, limit_price=995)
        order.submit()
        assert ex.execute(order, MD).status == ExecutionStatus.FILLED

    def test_stop_limit_below_limit_does_not_fill(self):
        ex = executor(config=ExecutionConfig(touch_fill_probability=D("1"), seed=7))
        order = Order.stop_limit("INFY", "sell", 100, stop_price=1010, limit_price=1005)
        order.submit()
        assert ex.execute(order, MD).status == ExecutionStatus.NO_FILL

    def test_trailing_stop_executes_after_ratcheting(self):
        ex = executor()
        order = Order.trailing_stop("INFY", "sell", 100, trailing_amount=20)
        order.submit()
        ex.execute(order, {**MD, "last": 1050, "bid": 1049.5, "ask": 1050.5})
        result = ex.execute(order, {**MD, "last": 1025, "bid": 1024.5, "ask": 1025.5})
        assert result.status == ExecutionStatus.FILLED

    def test_wrong_type_rejected_by_the_helper(self):
        with pytest.raises(ValidationError):
            executor().process_stop_order(buy(100), MD)


# ===========================================================================
# Rejections
# ===========================================================================


class TestRejections:
    def test_halted_symbol(self):
        ex = executor()
        ex.halt("INFY")
        order = buy(100)
        result = ex.execute(order, MD)
        assert result.rejection_code == RejectionCode.SYMBOL_HALTED
        assert order.status is OrderStatus.REJECTED

    def test_halt_can_be_lifted(self):
        ex = executor()
        ex.halt("INFY")
        ex.resume("INFY")
        assert ex.execute(buy(100), MD).status == ExecutionStatus.FILLED

    def test_market_closed(self):
        ex = executor(config=ExecutionConfig(enforce_market_hours=True, seed=7))
        md = {**MD, "timestamp": datetime(2026, 1, 5, 18, 0, tzinfo=IST)}
        result = ex.execute(buy(100), md)
        assert result.rejection_code == RejectionCode.MARKET_CLOSED

    def test_inside_session_is_accepted(self):
        ex = executor(config=ExecutionConfig(enforce_market_hours=True, seed=7))
        md = {**MD, "timestamp": datetime(2026, 1, 5, 11, 0, tzinfo=IST)}
        assert ex.execute(buy(100), md).status == ExecutionStatus.FILLED

    def test_market_hours_off_by_default(self):
        """Daily-bar backtests have no meaningful clock."""
        md = {**MD, "timestamp": datetime(2026, 1, 5, 3, 0, tzinfo=IST)}
        assert executor().execute(buy(100), md).status == ExecutionStatus.FILLED

    def test_unsubmitted_order(self):
        result = executor().execute(Order.market("INFY", "buy", 100), MD)
        assert result.rejection_code == RejectionCode.ORDER_NOT_WORKING

    def test_terminal_order(self):
        order = buy(100)
        order.cancel("done")
        assert executor().execute(order, MD).rejection_code == RejectionCode.ORDER_NOT_WORKING

    def test_rejection_reason_stored_on_the_order(self):
        ex = executor()
        ex.halt("INFY")
        order = buy(100)
        ex.execute(order, MD)
        assert RejectionCode.SYMBOL_HALTED in order.reason_for_rejection

    def test_all_codes_are_stable_strings(self):
        for code in RejectionCode.ALL:
            assert isinstance(code, str) and code.islower()


# ===========================================================================
# Time in force
# ===========================================================================


class TestTimeInForce:
    def test_fok_that_cannot_fill_whole_is_cancelled(self):
        ex = executor()
        order = Order.market("INFY", "buy", 5000, time_in_force="fok")
        order.submit()
        result = ex.execute(order, MD)
        assert result.status == ExecutionStatus.CANCELLED
        assert result.rejection_code == RejectionCode.FOK_UNFILLABLE
        assert order.status is OrderStatus.CANCELLED
        assert result.filled_quantity == D("0")

    def test_result_and_order_status_agree(self):
        """Regression: the result said REJECTED while the order was CANCELLED."""
        ex = executor()
        order = Order.market("INFY", "buy", 5000, time_in_force="fok")
        order.submit()
        result = ex.execute(order, MD)
        assert result.status == order.status.value

    def test_fok_that_can_fill_whole_does(self):
        ex = executor()
        order = Order.market("INFY", "buy", 500, time_in_force="fok")
        order.submit()
        assert ex.execute(order, MD).status == ExecutionStatus.FILLED

    def test_ioc_fills_what_it_can_and_cancels_the_rest(self):
        ex = executor()
        order = Order.market("INFY", "buy", 5000, time_in_force="ioc")
        order.submit()
        result = ex.execute(order, MD)
        assert result.status == ExecutionStatus.CANCELLED
        assert result.filled_quantity == D("1000.00000000")
        assert order.status is OrderStatus.CANCELLED

    def test_day_order_keeps_resting(self):
        ex = executor()
        order = Order.market("INFY", "buy", 5000, time_in_force="day")
        order.submit()
        ex.execute(order, MD)
        assert order.is_working


# ===========================================================================
# Latency, price improvement, determinism
# ===========================================================================


class TestLatency:
    def test_within_the_configured_range(self):
        ex = executor(config=ExecutionConfig(
            min_latency_ms=D("50"), max_latency_ms=D("500"), seed=7))
        for _ in range(50):
            value = ex.simulate_latency()
            assert D("50") <= value <= D("500")

    def test_zero_range_is_exact(self):
        ex = executor(config=ExecutionConfig(
            min_latency_ms=D("0"), max_latency_ms=D("0"), seed=7))
        assert ex.simulate_latency() == D("0")

    def test_explicit_bounds(self):
        assert D("10") <= executor().simulate_latency(10, 20) <= D("20")

    def test_inverted_range_rejected(self):
        with pytest.raises(ValidationError, match="max_ms must be >="):
            executor().simulate_latency(100, 50)

    def test_reported_on_the_result(self):
        assert executor().execute(buy(100), MD).latency_ms > D("0")


class TestPriceImprovement:
    def test_never_improves_at_zero_probability(self):
        ex = executor(config=ExecutionConfig(
            price_improvement_probability=D("0"), seed=7),
            slippage=SlippageCalculator.disabled())
        prices = {ex.execute(buy(10), MD).fill.fill_price for _ in range(20)}
        assert prices == {D("1000.50000000")}

    def test_always_improves_at_probability_one(self):
        ex = executor(config=ExecutionConfig(
            price_improvement_probability=D("1"),
            price_improvement_bps=D("10"), seed=7),
            slippage=SlippageCalculator.disabled())
        assert ex.execute(buy(10), MD).fill.fill_price < D("1000.5")

    def test_improvement_favours_the_seller_upward(self):
        ex = executor(config=ExecutionConfig(
            price_improvement_probability=D("1"),
            price_improvement_bps=D("10"), seed=7),
            slippage=SlippageCalculator.disabled())
        order = Order.market("INFY", "sell", 10)
        order.submit()
        assert ex.execute(order, MD).fill.fill_price > D("999.5")


class TestDeterminism:
    def test_same_seed_gives_identical_results(self):
        def run():
            ex = executor(config=ExecutionConfig(seed=99))
            out = []
            for _ in range(10):
                order = Order.limit("INFY", "buy", 100, 1000.5)
                order.submit()
                r = ex.execute(order, MD)
                out.append((r.status, str(r.filled_quantity), str(r.latency_ms)))
            return out

        assert run() == run()

    def test_different_seeds_diverge(self):
        def run(seed):
            ex = executor(config=ExecutionConfig(seed=seed))
            return [str(ex.simulate_latency()) for _ in range(20)]

        assert run(1) != run(2)

    def test_reset_replays_identically(self):
        ex = executor(config=ExecutionConfig(seed=5))
        first = [str(ex.simulate_latency()) for _ in range(10)]
        ex.reset()
        assert [str(ex.simulate_latency()) for _ in range(10)] == first


# ===========================================================================
# Realism levels
# ===========================================================================


class TestRealismLevels:
    @pytest.mark.parametrize("level", RealismLevel.ALL)
    def test_each_level_executes(self, level):
        ex = OrderExecutor.for_realism(level, fees=CommissionCalculator.for_broker("zerodha"))
        assert ex.execute(buy(100), MD).did_trade

    def test_more_pessimism_means_less_fill(self):
        filled = {}
        for level in RealismLevel.ALL:
            ex = OrderExecutor.for_realism(
                level, fees=CommissionCalculator.for_broker("zerodha")
            )
            order = buy(5000)
            filled[level] = ex.execute(order, MD).filled_quantity
        assert filled["optimistic"] > filled["realistic"] > filled["pessimistic"]

    def test_optimistic_fills_everything_instantly(self):
        ex = OrderExecutor.for_realism("optimistic")
        result = ex.execute(buy(5000), MD)
        assert result.status == ExecutionStatus.FILLED
        assert result.latency_ms == D("0")


# ===========================================================================
# Events, batching, statistics
# ===========================================================================


class TestEvents:
    def test_fill_and_partial_events_distinguished(self):
        ex = executor()
        seen = []
        ex.add_callback(ExecutionEvent.FILL, lambda *_: seen.append("fill"))
        ex.add_callback(ExecutionEvent.PARTIAL_FILL, lambda *_: seen.append("partial"))
        ex.execute(buy(100), MD)
        ex.execute(buy(5000), MD)
        assert seen == ["fill", "partial"]

    def test_reject_and_no_fill_events(self):
        ex = executor()
        seen = []
        ex.add_callback(ExecutionEvent.REJECT, lambda *_: seen.append("reject"))
        ex.add_callback(ExecutionEvent.NO_FILL, lambda *_: seen.append("no_fill"))
        ex.halt("INFY")
        ex.execute(buy(100), MD)
        ex.resume("INFY")
        order = Order.limit("INFY", "buy", 100, 900)
        order.submit()
        ex.execute(order, MD)
        assert seen == ["reject", "no_fill"]

    def test_unknown_event_rejected(self):
        with pytest.raises(ValidationError, match="unknown execution event"):
            executor().add_callback("on_vibes", lambda: None)

    def test_failing_callback_does_not_break_execution(self):
        ex = executor()
        ex.add_callback(
            ExecutionEvent.FILL, lambda *_: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        assert ex.execute(buy(100), MD).status == ExecutionStatus.FILLED


class TestBatchExecution:
    def test_multiple_orders_one_tick(self):
        ex = executor()
        orders = [buy(100) for _ in range(5)]
        results = ex.execute_all(orders, MD)
        assert len(results) == 5
        assert all(r.did_trade for r in results)

    def test_per_symbol_market_data(self):
        ex = executor()
        a = buy(100)
        b = Order.market("TCS", "buy", 100)
        b.submit()
        feed = {"INFY": MD, "TCS": {**MD, "bid": 3799, "ask": 3801, "last": 3800}}
        results = ex.execute_all([a, b], feed, by_symbol=True)
        assert all(r.did_trade for r in results)
        assert results[1].fill.fill_price > D("3000")

    def test_missing_symbol_does_not_break_the_batch(self):
        """A feed dropping one symbol must not stop the others trading."""
        ex = executor()
        a = buy(100)
        b = Order.market("MISSING", "buy", 100)
        b.submit()
        results = ex.execute_all([a, b], {"INFY": MD}, by_symbol=True)
        assert results[0].did_trade
        assert results[1].status == ExecutionStatus.NO_FILL
        assert b.is_working


class TestStatistics:
    def test_empty(self):
        assert executor().statistics() == {"count": 0}

    def test_counts_and_rates(self):
        ex = executor()
        ex.execute(buy(100), MD)          # filled
        ex.execute(buy(5000), MD)         # partial
        resting = Order.limit("INFY", "buy", 100, 900)
        resting.submit()
        ex.execute(resting, MD)           # no fill
        ex.halt("INFY")
        ex.execute(buy(100), MD)          # rejected

        stats = ex.statistics()
        assert stats["count"] == 4
        assert stats["filled"] == 1 and stats["partial"] == 1
        assert stats["no_fill"] == 1 and stats["rejected"] == 1
        assert stats["fill_rate"] == 0.5

    def test_quantity_fill_rate(self):
        ex = executor()
        ex.execute(buy(5000), MD)
        stats = ex.statistics()
        assert stats["quantity_requested"] == D("5000.00000000")
        assert stats["quantity_filled"] == D("1000.00000000")
        assert stats["quantity_fill_rate"] == 0.2

    def test_rejection_codes_counted(self):
        ex = executor()
        ex.halt("INFY")
        ex.execute(buy(100), MD)
        ex.execute(buy(100), MD)
        assert ex.statistics()["rejections_by_code"] == {RejectionCode.SYMBOL_HALTED: 2}

    def test_cancellation_codes_are_visible(self):
        """FOK cancels rather than rejects; it must still show up."""
        ex = executor()
        order = Order.market("INFY", "buy", 5000, time_in_force="fok")
        order.submit()
        ex.execute(order, MD)
        assert ex.statistics()["rejections_by_code"] == {RejectionCode.FOK_UNFILLABLE: 1}

    def test_result_to_dict_is_json_safe(self):
        payload = executor().execute(buy(100), MD).to_dict()
        assert json.loads(json.dumps(payload))["status"] == "filled"


# ===========================================================================
# Portfolio integration
# ===========================================================================


class TestPortfolioIntegration:
    def test_fill_updates_the_portfolio(self):
        p = Portfolio(name="p", initial_capital=D("10000000"))
        ex = executor(portfolio=p)
        order = p.add_order(Order.market("INFY", "buy", 500, portfolio_id=p.portfolio_id))
        order.submit()
        ex.execute(order, MD)
        assert p.get_position("INFY").quantity == D("500.00000000")
        assert p.current_cash < D("10000000")

    def test_round_trip_reconciles(self):
        p = Portfolio(name="p", initial_capital=D("10000000"))
        ex = executor(portfolio=p, slippage=SlippageCalculator.disabled(),
                      fees=CommissionCalculator.for_broker("zero"))
        b = Order.market("INFY", "buy", 500, portfolio_id=p.portfolio_id)
        b.submit()
        ex.execute(b, MD)
        s = Order.market("INFY", "sell", 500, portfolio_id=p.portfolio_id)
        s.submit()
        ex.execute(s, MD)
        assert p.positions == {}
        # Bought at the ask 1000.5, sold at the bid 999.5: the spread is the loss.
        assert p.realized_pnl == D("-500.0000")

    def test_partial_fills_accumulate_into_one_position(self):
        p = Portfolio(name="p", initial_capital=D("10000000"))
        ex = executor(portfolio=p)
        order = p.add_order(Order.market("INFY", "buy", 3000, portfolio_id=p.portfolio_id))
        order.submit()
        for _ in range(3):
            ex.execute(order, MD)
        assert p.get_position("INFY").quantity == D("3000.00000000")
        assert order.status is OrderStatus.FILLED

    def test_portfolio_limits_still_apply(self):
        from backtest.simulator import InsufficientFundsError

        p = Portfolio(name="p", initial_capital=D("1000"))
        ex = executor(portfolio=p)
        order = Order.market("INFY", "buy", 500, portfolio_id=p.portfolio_id)
        order.submit()
        with pytest.raises(InsufficientFundsError):
            ex.execute(order, MD)

    def test_sync_orders_after_a_sweep(self):
        p = Portfolio(name="p", initial_capital=D("10000000"))
        ex = executor(portfolio=p)
        for _ in range(3):
            p.add_order(Order.market("INFY", "buy", 100, portfolio_id=p.portfolio_id)).submit()
        ex.execute_all(p.pending_orders, MD)
        assert p.sync_orders() == 3
        assert p.pending_orders == []
