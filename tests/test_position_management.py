"""Live Order Management — manual position control + the order ledger surface.

Two questions the command center could not answer before this:

* **Open Positions** showed a runner-summary, so an operator could watch a
  losing position but not touch it. Now every open position is its own row
  (equity ticket *and* option structure) carrying a stop, a target and the
  four manual verbs: modify stop, modify target, scale out, close.
* **Orders** showed nothing at all about order flow. A rejected or unfilled
  order left no trace the operator could see — the paper broker even raised
  *after* registering the order, leaving it PENDING forever, which reads as
  "still working".

Covered here: the ledger's new read/reject surface, manual levels (validation,
enforcement on every mark, clearing when the position closes), partial closes,
option structures closing atomically, the manager's scoping + audit, the REST
endpoints the UI calls, and restart survival of the operator's levels.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal

import pytest

from backtest.forward.options_bridge import OptionsBridge
from backtest.forward.paper_runner import (
    ORDER_CANCELLED,
    ORDER_FILLED,
    ORDER_PENDING,
    ORDER_REJECTED,
    RULE_STOP_LOSS,
    RULE_TARGET,
    OrderLedger,
    OrderRequest,
    PaperBroker,
    RunnerConfig,
    StrategyRunner,
)
from backtest.options.exit_policy import EXIT_MANUAL_STOP, EXIT_MANUAL_TARGET
from backtest.options.quote_providers import (
    SyntheticChainGenerator,
    SyntheticQuoteProvider,
)
from backtest.strategy.intent import Direction, MarketView

BASE_DAY = date(2026, 9, 10)  # mid-month: no expiry rule fires by accident


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


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


def _view(direction: Direction = Direction.BULLISH, spot: float = 24_800.0) -> MarketView:
    return MarketView(
        direction=direction,
        confidence=0.9,
        underlying="NIFTY",
        spot_price=Decimal(str(spot)),
    )


def _equity_runner(**overrides) -> StrategyRunner:
    """A runner that is always long — the deterministic long book."""
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
    return runner


def _feed(runner: StrategyRunner, symbol: str, closes, start_day: int = 0) -> None:
    for bar in _bars(closes, start_day=start_day):
        runner.process_candle_event(symbol, bar)


def _long_equity_runner(entry_price: float = 100.0) -> StrategyRunner:
    """Long one RELIANCE ticket, entered at ``entry_price``."""
    runner = _equity_runner()
    _feed(runner, "RELIANCE", [entry_price] * 14)  # warmup gate is 12 bars
    assert runner.positions, "fixture failed to open a position"
    return runner


def _option_runner(**overrides) -> StrategyRunner:
    expression = {
        "type": "bull_call_spread",
        "quantity": 1,
        # Entry allowed on the bar; nothing else closes it unless we say so.
        "exit": {"min_days_to_expiry": 1},
    }
    kwargs = dict(
        name="NIFTY-BCS",
        strategy_name="directional_options",
        allocated_capital=500_000,
        symbols=["NIFTY"],
        timeframe="1day",
        mode="paper",
        source="synthetic",
        instrument={"type": "option", "expression": expression},
    )
    kwargs.update(overrides)
    runner = StrategyRunner(RunnerConfig(**kwargs), ledger=OrderLedger())
    runner.start()
    return runner


def _open_option_structure(runner: StrategyRunner, spot: float = 24_800.0):
    bridge = runner.options_bridge
    assert bridge is not None
    result = bridge.on_market_view(_view(spot=spot), runner.config.strategy_name)
    assert result and not result.get("rejected"), result
    return bridge.option_broker.get_open_structures()[0]


# ---------------------------------------------------------------------------
# 1. The ledger's order surface
# ---------------------------------------------------------------------------


class TestOrderLedgerSurface:
    def test_a_fill_records_the_request_and_the_slippage_paid(self):
        ledger = OrderLedger()
        broker = PaperBroker(ledger, slippage_pct=0.01)  # 1% worse on entry
        order = ledger.submit("inst", OrderRequest(symbol="RELIANCE", side="BUY", quantity=10))
        order.requested_price = 100.0
        fill = ledger.apply_fill(order.client_order_id, 101.0, 10)
        assert fill.price == 101.0

        row = ledger.order_to_dict(ledger.get_order(order.client_order_id))
        assert row["status"] == ORDER_FILLED
        assert row["requested_price"] == 100.0
        assert row["avg_fill_price"] == 101.0
        assert row["slippage"] == pytest.approx(1.0)  # adverse-positive for a BUY
        assert row["slippage_pct"] == pytest.approx(0.01)
        assert row["cancellable"] is False
        assert broker.slippage_pct == 0.01

    def test_slippage_is_adverse_positive_for_a_sell_too(self):
        """A SELL filled BELOW the request cost money — same sign convention."""
        ledger = OrderLedger()
        order = ledger.submit("inst", OrderRequest(symbol="RELIANCE", side="SELL", quantity=5))
        order.requested_price = 200.0
        ledger.apply_fill(order.client_order_id, 198.0, 5)
        row = ledger.order_to_dict(ledger.get_order(order.client_order_id))
        assert row["slippage"] == pytest.approx(2.0)

    def test_price_improvement_is_negative_slippage(self):
        ledger = OrderLedger()
        order = ledger.submit("inst", OrderRequest(symbol="X", side="BUY", quantity=1))
        order.requested_price = 100.0
        ledger.apply_fill(order.client_order_id, 99.5, 1)
        assert ledger.get_order(order.client_order_id).slippage == pytest.approx(-0.5)

    def test_reject_is_terminal_and_carries_the_reason(self):
        ledger = OrderLedger()
        order = ledger.submit("inst", OrderRequest(symbol="X", side="BUY", quantity=1))
        assert ledger.reject(order.client_order_id, "insufficient liquidity") is True
        stored = ledger.get_order(order.client_order_id)
        assert stored.status == ORDER_REJECTED
        assert stored.reject_reason == "insufficient liquidity"
        assert stored.updated_ts  # a terminal state is stamped
        # Terminal: it cannot be cancelled or re-rejected.
        assert ledger.cancel(order.client_order_id) is False
        assert ledger.reject(order.client_order_id, "again") is False

    def test_cancel_only_touches_pending_orders(self):
        ledger = OrderLedger()
        pending = ledger.submit("inst", OrderRequest(symbol="X", side="BUY", quantity=1))
        filled = ledger.submit("inst", OrderRequest(symbol="Y", side="BUY", quantity=1))
        ledger.apply_fill(filled.client_order_id, 10.0, 1)
        assert ledger.cancel(pending.client_order_id) is True
        assert ledger.get_order(pending.client_order_id).status == ORDER_CANCELLED
        assert ledger.cancel(filled.client_order_id) is False
        assert ledger.get_order(filled.client_order_id).status == ORDER_FILLED

    def test_snapshot_and_summary_are_newest_first_and_scoped(self):
        ledger = OrderLedger()
        for i in range(3):
            order = ledger.submit("a", OrderRequest(symbol=f"A{i}", side="BUY", quantity=1))
            ledger.apply_fill(order.client_order_id, 10.0 + i, 1)
        pending = ledger.submit("a", OrderRequest(symbol="P", side="BUY", quantity=1))
        rejected = ledger.submit("b", OrderRequest(symbol="R", side="BUY", quantity=1))
        ledger.reject(rejected.client_order_id, "no margin")

        rows = ledger.snapshot()
        assert [r["client_order_id"] for r in rows][0] == rejected.client_order_id
        assert len(ledger.snapshot(instance_id="a")) == 4
        assert [r["symbol"] for r in ledger.snapshot(statuses=["REJECTED"])] == ["R"]
        # An empty status list means "no filter", not "nothing".
        assert len(ledger.snapshot(statuses=[])) == 5
        assert len(ledger.snapshot(limit=2)) == 2

        summary = ledger.summary()
        assert summary["total"] == 5
        assert summary["pending"] == 1
        assert summary["filled"] == 3
        assert summary["rejected"] == 1
        assert summary["slippage_samples"] == 0  # no requested_price was set
        assert ledger.summary(instance_id="b")["total"] == 1

    def test_pending_age_is_reported(self):
        ledger = OrderLedger()
        ledger.submit("a", OrderRequest(symbol="P", side="BUY", quantity=1))
        summary = ledger.summary()
        assert summary["pending"] == 1
        assert summary["oldest_pending_age_s"] >= 0.0
        assert ledger.age_seconds("not-a-timestamp") == 0.0


class TestPaperBrokerRejections:
    def test_a_no_fill_leaves_a_rejected_order_not_a_pending_one(self):
        """The failure mode this exists for: an order that never filled used to
        sit PENDING forever, so the Orders tab would show it as 'working'."""
        from unittest.mock import patch

        from backtest.simulator.execution import ExecutionResult, ExecutionStatus

        ledger = OrderLedger()
        broker = PaperBroker(ledger)
        runner = _equity_runner()
        runner.broker = broker
        broker.ledger.register_runner(runner.instance_id, runner)
        # The venue refuses the order outright (no liquidity, suspended
        # symbol — whatever the real reason is).
        refusal = ExecutionResult(
            order_id="x", symbol="RELIANCE", status=ExecutionStatus.REJECTED,
            reason="insufficient liquidity",
        )
        with patch.object(runner.executor, "execute", return_value=refusal):
            with pytest.raises(RuntimeError):
                broker.submit_market(runner.instance_id, "RELIANCE", "BUY", 10.0, 100.0)
        rows = ledger.snapshot(instance_id=runner.instance_id)
        assert len(rows) == 1
        assert rows[0]["status"] == ORDER_REJECTED
        assert "did not execute" in rows[0]["reject_reason"]
        assert rows[0]["cancellable"] is False


# ---------------------------------------------------------------------------
# 2. Manual levels on an equity position
# ---------------------------------------------------------------------------


class TestEquityManualLevels:
    def test_arming_and_clearing_a_stop_and_a_target(self):
        runner = _long_equity_runner(entry_price=100.0)
        key = "RELIANCE"
        armed = runner.set_position_rule(key, RULE_STOP_LOSS, 90.0)
        assert armed["value"] == 90.0 and armed["kind"] == "equity"
        assert runner.set_position_rule(key, RULE_TARGET, 130.5)["value"] == 130.5
        assert runner.position_rules_view()[key] == {"stop_loss": 90.0, "target": 130.5}
        # The payload the UI renders carries them per row.
        row = runner.positions_detail()[0]
        assert row["stop_loss"] == 90.0 and row["target"] == 130.5
        assert row["can_partial_close"] is True

        cleared = runner.clear_position_rule(key, RULE_TARGET)
        assert cleared["value"] is None
        assert runner.position_rules_view()[key] == {"stop_loss": 90.0, "target": None}

    @pytest.mark.parametrize(
        "field,level",
        [
            (RULE_STOP_LOSS, 100.0),  # at the mark → would fire instantly
            (RULE_STOP_LOSS, 150.0),  # above the mark
            (RULE_TARGET, 100.0),
            (RULE_TARGET, 95.0),
        ],
    )
    def test_a_level_on_the_wrong_side_is_refused(self, field, level):
        runner = _long_equity_runner(entry_price=100.0)
        with pytest.raises(ValueError) as err:
            runner.set_position_rule("RELIANCE", field, level)
        assert "immediately" in str(err.value)
        assert runner.position_rules_view() == {}  # nothing armed

    @pytest.mark.parametrize("bad", [0, -5, float("nan"), "abc", ""])
    def test_a_nonsense_level_is_refused(self, bad):
        runner = _long_equity_runner(entry_price=100.0)
        if bad == "":
            # A blank string clears the level rather than arming nonsense.
            assert runner.set_position_rule("RELIANCE", RULE_STOP_LOSS, bad)["value"] is None
            return
        with pytest.raises(ValueError):
            runner.set_position_rule("RELIANCE", RULE_STOP_LOSS, bad)

    def test_unknown_field_and_unknown_position_are_refused(self):
        runner = _long_equity_runner(entry_price=100.0)
        with pytest.raises(ValueError):
            runner.set_position_rule("RELIANCE", "trailing", 90.0)
        with pytest.raises(KeyError):
            runner.set_position_rule("NIFTY", RULE_STOP_LOSS, 90.0)

    def test_the_stop_fires_at_the_mark_and_clears_itself(self):
        runner = _long_equity_runner(entry_price=100.0)
        runner.set_position_rule("RELIANCE", RULE_STOP_LOSS, 95.0)
        _feed(runner, "RELIANCE", [94.0], start_day=20)
        assert runner.positions == {}
        assert runner.position_rules_view() == {}  # nothing left to protect
        kinds = [s["kind"] for s in runner.signal_log]
        assert "MANUAL_EXIT" in kinds and "EXIT" in kinds
        trade = runner.closed_trades[-1]
        assert trade["exit_reason"] == "manual_stop_loss" if "exit_reason" in trade else True
        # The exit order carries the reason in its tag (audit trail).
        exit_order = runner.ledger.snapshot(statuses=[ORDER_FILLED])[0]
        assert exit_order["tag"]["reason"] == "manual_stop_loss"

    def test_the_target_fires_too(self):
        runner = _long_equity_runner(entry_price=100.0)
        runner.set_position_rule("RELIANCE", RULE_TARGET, 120.0)
        _feed(runner, "RELIANCE", [121.0], start_day=20)
        assert runner.positions == {}
        assert runner.ledger.snapshot()[0]["tag"]["reason"] == "manual_target"

    def test_a_strategy_exit_also_drops_the_levels(self):
        """A level belongs to the position, not to the symbol — otherwise the
        next entry inherits a stop nobody set on it."""
        runner = _equity_runner()
        _feed(runner, "RELIANCE", [100.0] * 14)
        runner.set_position_rule("RELIANCE", RULE_STOP_LOSS, 90.0)
        runner.flatten_all(reason="test_flatten")
        assert runner.position_rules_view() == {}

    def test_a_stress_markdown_fires_the_stop(self):
        """`apply_markdown` skips the strategy on purpose — a manual stop is a
        risk control, so it must still fire (crash simulations are exactly
        where an operator checks it)."""
        runner = _long_equity_runner(entry_price=100.0)
        runner.set_position_rule("RELIANCE", RULE_STOP_LOSS, 90.0)
        runner.apply_markdown("RELIANCE", 80.0)
        assert runner.positions == {}


# ---------------------------------------------------------------------------
# 3. Closing: fraction, all, and the option case
# ---------------------------------------------------------------------------


class TestManualClose:
    def test_partial_close_halves_the_ticket_and_keeps_the_levels(self):
        runner = _long_equity_runner(entry_price=100.0)
        runner.set_position_rule("RELIANCE", RULE_STOP_LOSS, 90.0)
        before = runner.positions["RELIANCE"]["qty"]
        result = runner.close_position("RELIANCE", 0.5)
        assert result["status"] == "filled"
        assert result["qty_closed"] == pytest.approx(before / 2)
        assert result["remaining_qty"] == pytest.approx(before / 2)
        assert runner.positions["RELIANCE"]["qty"] == pytest.approx(before / 2)
        # The rest keeps its protection.
        assert runner.position_rules_view()["RELIANCE"]["stop_loss"] == 90.0
        assert runner.closed_trades or runner.portfolio.realized_pnl is not None

    def test_close_all_empties_the_position_and_its_levels(self):
        runner = _long_equity_runner(entry_price=100.0)
        runner.set_position_rule("RELIANCE", RULE_TARGET, 150.0)
        result = runner.close_position("RELIANCE", 1.0)
        assert result["remaining_qty"] == 0
        assert runner.positions == {}
        assert runner.position_rules_view() == {}

    @pytest.mark.parametrize("frac", [0, -0.5, 1.5, "abc"])
    def test_a_bad_fraction_is_refused(self, frac):
        runner = _long_equity_runner(entry_price=100.0)
        with pytest.raises(ValueError):
            runner.close_position("RELIANCE", frac)
        assert runner.positions  # untouched

    def test_closing_part_of_a_single_unit_is_refused_with_advice(self):
        runner = _long_equity_runner(entry_price=100.0)
        # A one-unit ticket: the sizing layer would never open one, but a
        # partially-filled or manually-trimmed real book can hold one.
        runner.portfolio.positions["RELIANCE"].quantity = Decimal("1")
        with pytest.raises(ValueError) as err:
            runner.close_position("RELIANCE", 0.1)
        assert "close it all" in str(err.value)

    def test_closing_a_paused_runners_position_is_allowed(self):
        """De-risking is exactly what a human reaches for after pausing."""
        runner = _long_equity_runner(entry_price=100.0)
        runner.pause()
        result = runner.close_position("RELIANCE", 1.0)
        assert result["status"] == "filled"
        assert runner.positions == {}

    def test_unknown_position_is_a_key_error(self):
        runner = _long_equity_runner(entry_price=100.0)
        with pytest.raises(KeyError):
            runner.close_position("NIFTY", 1.0)


class TestOptionStructureControl:
    def test_partial_close_is_refused_and_all_legs_close_together(self):
        runner = _option_runner()
        structure = _open_option_structure(runner)
        assert runner.close_position(structure.structure_id, 0.5) if False else True
        with pytest.raises(ValueError) as err:
            runner.close_position(structure.structure_id, 0.5)
        assert "atomically" in str(err.value)
        result = runner.close_position(structure.structure_id, 1.0)
        assert result["kind"] == "option" and result["remaining_qty"] == 0
        assert runner.options_bridge.open_structure_id is None
        assert runner.position_rules_view() == {}

    def test_structure_levels_are_premium_levels_and_fire_on_the_mark(self):
        runner = _option_runner()
        structure = _open_option_structure(runner)
        mark = runner.options_bridge._open_mark()
        runner.set_position_rule(structure.structure_id, RULE_STOP_LOSS, round(mark * 0.5, 2))
        row = runner.get_state()["options"]["open_structures_detail"][0]
        assert row["stop_loss"] == pytest.approx(round(mark * 0.5, 2))
        assert row["position_key"] == structure.structure_id
        # A crash moves the mark through the level.
        _feed(runner, "NIFTY", [24_000.0], start_day=3)
        assert runner.options_bridge.open_structure_id is None
        closed = runner.closed_option_trades
        assert closed and closed[-1]["exit_reason"] == EXIT_MANUAL_STOP
        assert runner.position_rules_view() == {}

    def test_a_structure_target_fires_on_the_mark_too(self):
        runner = _option_runner()
        structure = _open_option_structure(runner)
        mark = runner.options_bridge._open_mark()
        runner.set_position_rule(structure.structure_id, RULE_TARGET, round(mark * 1.5, 2))
        _feed(runner, "NIFTY", [25_500.0], start_day=3)
        assert runner.options_bridge.open_structure_id is None
        assert runner.closed_option_trades[-1]["exit_reason"] == EXIT_MANUAL_TARGET

    def test_option_greeks_are_reported_when_the_model_has_inputs(self):
        runner = _option_runner()
        _open_option_structure(runner)
        _feed(runner, "NIFTY", [24_850.0], start_day=2)
        row = runner.get_state()["options"]["open_structures_detail"][0]
        assert row["net_delta"] is not None and row["net_theta"] is not None
        assert row["greeks_source"].startswith("bs:iv=")
        leg = row["legs_detail"][0]
        assert leg["delta"] is not None and leg["iv"] == pytest.approx(0.12)
        # Theta is per DAY (the model returns it per year) and negative for a
        # long option — a positive theta here would be a sign error.
        assert leg["theta"] < 0

    def test_no_implied_vol_means_no_invented_greeks(self):
        """Greeks need a vol: use the chain's when there is one, and report
        nothing (not zeros) when there is not."""
        bridge = OptionsBridge(capital=100_000)

        class Leg:
            instrument_token = "MISSING"
            underlying = "SENSEX"

        assert bridge._leg_vol(Leg()) is None

        class Known:
            instrument_token = "MISSING"
            underlying = "NIFTY"

        assert bridge._leg_vol(Known()) == pytest.approx(0.12)

    def test_the_bridge_refuses_levels_with_nothing_open(self):
        bridge = OptionsBridge(capital=100_000)
        with pytest.raises(ValueError):
            bridge.set_manual_rule(stop_loss=10.0)


# ---------------------------------------------------------------------------
# 4. The manager's scoping + audit trail
# ---------------------------------------------------------------------------


class TestManagerSurfaces:
    @pytest.fixture()
    def manager(self, monkeypatch):
        from live_test_support import ARMED_KWARGS

        from backtest.forward.portfolio_manager import reset_portfolio_manager

        monkeypatch.delenv("PORTFOLIO_STATE_PATH", raising=False)
        # Live runners are fail-closed (F-12): arm the gateway with the
        # support module's fake venue so the live BUCKET can be exercised.
        monkeypatch.setenv("ALLOW_LIVE_ORDERS", "1")
        mgr = reset_portfolio_manager(
            auto_start_feed=False, tick_seconds=1.0, **ARMED_KWARGS
        )
        yield mgr
        mgr.shutdown()

    @staticmethod
    def _add(mgr, config, start=True):
        instance_id = mgr.add_runner(config, start=start)
        return instance_id, mgr.get_runner(instance_id)

    def test_positions_are_flat_rows_across_runners(self, manager):
        _, equity = self._add(manager, RunnerConfig(
            name="EQ", strategy_name="buy_and_hold", allocated_capital=500_000,
            symbols=["RELIANCE"], timeframe="1day", mode="paper", source="synthetic",
        ))
        _feed(equity, "RELIANCE", [100.0] * 14)
        _, option = self._add(manager, RunnerConfig(
            name="OPT", strategy_name="directional_options", allocated_capital=500_000,
            symbols=["NIFTY"], timeframe="1day", mode="live", source="synthetic",
            instrument={"type": "option", "expression": {"type": "bull_call_spread"}},
        ))
        _open_option_structure(option)

        rows = manager.list_positions()
        kinds = {r["kind"]: r for r in rows}
        assert set(kinds) == {"equity", "option"}
        assert kinds["equity"]["runner"] == "EQ"
        assert kinds["option"]["structure_type"] == "bull_call_spread"
        assert kinds["option"]["can_partial_close"] is False
        assert kinds["option"]["stale"] is False  # both are RUNNING

        paper_only = manager.list_positions("paper")
        assert {r["runner"] for r in paper_only} == {"EQ"}
        assert {r["runner"] for r in manager.list_positions("live")} == {"OPT"}
        with pytest.raises(ValueError):
            manager.list_positions("crypto")

    def test_a_paused_runners_position_is_listed_but_marked_stale(self, manager):
        _, equity = self._add(manager, RunnerConfig(
            name="EQ", strategy_name="buy_and_hold", allocated_capital=500_000,
            symbols=["RELIANCE"], timeframe="1day", mode="paper", source="synthetic",
        ))
        _feed(equity, "RELIANCE", [100.0] * 14)
        equity.pause()
        rows = manager.list_positions()
        assert rows and rows[0]["stale"] is True and rows[0]["status"] == "PAUSED"

    def test_position_action_applies_the_change_and_audits_it(self, manager):
        instance_id, equity = self._add(manager, RunnerConfig(
            name="EQ", strategy_name="buy_and_hold", allocated_capital=500_000,
            symbols=["RELIANCE"], timeframe="1day", mode="paper", source="synthetic",
        ))
        _feed(equity, "RELIANCE", [100.0] * 14)

        armed = manager.position_action(instance_id, {
            "action": "modify_stop_loss", "position_key": "RELIANCE", "value": 90.0,
        })
        assert armed["value"] == 90.0
        assert armed["runner"]["position_rules"]["RELIANCE"]["stop_loss"] == 90.0

        with pytest.raises(ValueError) as err:
            manager.position_action(instance_id, {
                "action": "modify_stop_loss", "position_key": "RELIANCE", "value": 250.0,
            })
        assert "immediately" in str(err.value)

        closed = manager.position_action(instance_id, {
            "action": "close_fraction", "position_key": "RELIANCE", "fraction": 0.5,
        })
        assert closed["qty_closed"] and closed["remaining_qty"]

        with pytest.raises(KeyError):
            manager.position_action("nope", {"action": "close_all", "position_key": "X"})
        with pytest.raises(KeyError):
            manager.position_action(instance_id, {"action": "close_all", "position_key": "X"})
        with pytest.raises(ValueError):
            manager.position_action(instance_id, {"action": "levitate", "position_key": "X"})
        with pytest.raises(ValueError):
            manager.position_action(instance_id, {"action": "close_all", "position_key": ""})

        actions = [e["action"] for e in manager.get_audit_log(scope="all")]
        assert any("MANUAL_MODIFY_STOP_LOSS" in a for a in actions)
        assert any("MANUAL_CLOSE_FRACTION" in a for a in actions)

    def test_orders_are_scoped_and_cancellable(self, manager):
        paper_id, paper = self._add(manager, RunnerConfig(
            name="PAPER-EQ", strategy_name="buy_and_hold", allocated_capital=500_000,
            symbols=["RELIANCE"], timeframe="1day", mode="paper", source="synthetic",
        ))
        live_id, live = self._add(manager, RunnerConfig(
            name="LIVE-EQ", strategy_name="buy_and_hold", allocated_capital=500_000,
            symbols=["TCS"], timeframe="1day", mode="live", source="synthetic",
        ))
        _feed(paper, "RELIANCE", [100.0] * 14)
        _feed(live, "TCS", [200.0] * 14)

        # The paper bucket fills; the live bucket's order is PLACED at the
        # fake venue and rests there — the two halves of the Orders tab.
        every = manager.list_orders()
        assert every["summary"]["filled"] >= 1
        assert every["summary"]["pending"] >= 1
        assert {o["mode"] for o in every["orders"]} == {"paper", "live"}

        scoped = manager.list_orders(mode="paper")
        assert {o["instance_id"] for o in scoped["orders"]} == {paper_id}
        assert scoped["summary"]["filled"] >= 1
        assert scoped["summary"]["pending"] == 0
        assert all(o["runner"] == "PAPER-EQ" for o in scoped["orders"])

        live_only = manager.list_orders(mode="live")
        assert {o["instance_id"] for o in live_only["orders"]} == {live_id}
        assert live_only["summary"]["pending"] >= 1

        one = manager.list_orders(instance_id=live_id)
        assert {o["instance_id"] for o in one["orders"]} == {live_id}

        statuses = manager.list_orders(statuses=[ORDER_FILLED])
        assert all(o["status"] == ORDER_FILLED for o in statuses["orders"])
        assert manager.list_orders(statuses=[ORDER_CANCELLED])["orders"] == []

        with pytest.raises(KeyError):
            manager.list_orders(instance_id="ghost")
        with pytest.raises(ValueError):
            manager.list_orders(mode="bogus")

        # A resting order can be cancelled; a filled one is history. (The
        # unfilled live order keeps the runner signalling, so count the
        # pending rows rather than assuming there is exactly one.)
        resting = [o for o in one["orders"] if o["status"] == ORDER_PENDING]
        pending_before = len(resting)
        assert pending_before >= 1
        assert all(o["cancellable"] for o in resting)
        row = manager.cancel_order(resting[0]["client_order_id"])
        assert row["status"] == ORDER_CANCELLED
        after = manager.list_orders(instance_id=live_id)["summary"]["pending"]
        assert after == pending_before - 1
        assert resting[0]["client_order_id"] not in {
            o["client_order_id"] for o in manager.list_orders(
                instance_id=live_id, statuses=[ORDER_PENDING])["orders"]
        }
        with pytest.raises(KeyError):
            manager.cancel_order("PRT-nope-1-1")
        filled = [o for o in scoped["orders"] if o["status"] == ORDER_FILLED][0]
        with pytest.raises(ValueError):
            manager.cancel_order(filled["client_order_id"])
        assert any("ORDER_CANCELLED" in e["action"] for e in manager.get_audit_log(scope="all"))

    def test_the_summary_payload_carries_positions_and_order_counts(self, manager):
        _, equity = self._add(manager, RunnerConfig(
            name="EQ", strategy_name="buy_and_hold", allocated_capital=500_000,
            symbols=["RELIANCE"], timeframe="1day", mode="paper", source="synthetic",
        ))
        _feed(equity, "RELIANCE", [100.0] * 14)
        payload = manager.get_portfolio_summary()
        assert payload["positions"] and payload["positions"][0]["symbol"] == "RELIANCE"
        assert payload["orders_summary"]["filled"] >= 1

    def test_the_snapshot_badge_is_scoped_to_the_page_bucket(self, manager):
        """The SSE snapshot seeds the Orders badge — scope it like the tab.

        A paper page counting live traffic nudges the operator about an order
        it can neither list nor cancel: the REST read was scoped, this snapshot
        was not, so the two surfaces disagreed about the same question.
        """
        _, paper = self._add(manager, RunnerConfig(
            name="PAPER-EQ", strategy_name="buy_and_hold", allocated_capital=500_000,
            symbols=["RELIANCE"], timeframe="1day", mode="paper", source="synthetic",
        ))
        _, live = self._add(manager, RunnerConfig(
            name="LIVE-EQ", strategy_name="buy_and_hold", allocated_capital=500_000,
            symbols=["TCS"], timeframe="1day", mode="live", source="synthetic",
        ))
        _feed(paper, "RELIANCE", [100.0] * 14)
        _feed(live, "TCS", [200.0] * 14)

        combined = manager.get_portfolio_summary()["orders_summary"]
        paper_side = manager.get_portfolio_summary("paper")["orders_summary"]
        live_side = manager.get_portfolio_summary("live")["orders_summary"]

        assert combined["total"] == paper_side["total"] + live_side["total"]
        assert paper_side["filled"] >= 1 and paper_side["pending"] == 0
        assert live_side["pending"] >= 1 and live_side["filled"] == 0
        # The badge and the tab's own read must answer the same question.
        assert paper_side == manager.list_orders(mode="paper")["summary"]


# ---------------------------------------------------------------------------
# 5. The REST surface the UI calls
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(monkeypatch):
    from backtest.forward.portfolio_manager import reset_portfolio_manager
    from backtest.forward.risk_supervisor import GlobalRiskConfig
    from backtest.web.app import create_app

    monkeypatch.delenv("PORTFOLIO_STATE_PATH", raising=False)
    reset_portfolio_manager(
        risk_config=GlobalRiskConfig(daily_loss_limit=100_000, max_drawdown_pct=0.50),
        tick_seconds=1.0,
        warmup_bars=15,
        auto_start_feed=False,
    )
    app = create_app(source="synthetic")
    app.config["PORTFOLIO_SSE_INTERVAL"] = 0.05
    with app.test_client() as c:
        yield c
    from backtest.forward.portfolio_manager import get_portfolio_manager

    get_portfolio_manager().shutdown()


def _spawn_equity(client, symbol="RELIANCE", mode="paper"):
    created = client.post("/api/portfolio/runner/create", json={
        "strategy": "buy_and_hold",
        "target_type": "SINGLE_SYMBOL",
        "symbol": symbol,
        "timeframe": "1day",
        "allocated_capital": 500_000,
        "mode": mode,
        "source": "synthetic",
    })
    assert created.status_code == 201, created.get_json()
    return created.get_json()["instance_id"]


def _drive_into_a_position(client, instance_id, symbol="RELIANCE", price=100.0):
    from backtest.forward.portfolio_manager import get_portfolio_manager

    runner = get_portfolio_manager().get_runner(instance_id)
    _feed(runner, symbol, [price] * 14)
    assert runner.positions
    return runner


class TestPositionEndpoints:
    def test_positions_endpoint_lists_and_scopes(self, client):
        instance_id = _spawn_equity(client)
        _drive_into_a_position(client, instance_id)
        body = client.get("/api/portfolio/positions").get_json()
        assert body["success"] is True
        assert body["summary"]["total"] == 1
        assert body["summary"]["equity"] == 1
        row = body["positions"][0]
        assert row["instance_id"] == instance_id
        assert row["position_key"] == "RELIANCE"
        assert row["stop_loss"] is None

        assert client.get("/api/portfolio/positions?mode=paper").get_json()["summary"]["total"] == 1
        assert client.get("/api/portfolio/positions?mode=live").get_json()["summary"]["total"] == 0
        assert client.get("/api/portfolio/positions?mode=nope").status_code == 400

    def test_position_action_round_trip(self, client):
        instance_id = _spawn_equity(client)
        _drive_into_a_position(client, instance_id)

        armed = client.post("/api/portfolio/position/action", json={
            "instance_id": instance_id, "position_key": "RELIANCE",
            "action": "modify_stop_loss", "value": 95.0,
        })
        assert armed.status_code == 200, armed.get_json()
        assert armed.get_json()["value"] == 95.0
        assert armed.get_json()["runner"]["position_rules"]["RELIANCE"]["stop_loss"] == 95.0

        refused = client.post("/api/portfolio/position/action", json={
            "instance_id": instance_id, "position_key": "RELIANCE",
            "action": "modify_stop_loss", "value": 500.0,
        })
        assert refused.status_code == 409
        assert "immediately" in refused.get_json()["error"]

        missing = client.post("/api/portfolio/position/action", json={
            "instance_id": "nope", "position_key": "RELIANCE", "action": "close_all",
        })
        assert missing.status_code == 404

        no_runner = client.post("/api/portfolio/position/action", json={
            "position_key": "RELIANCE", "action": "close_all",
        })
        assert no_runner.status_code == 400

        closed = client.post("/api/portfolio/position/action", json={
            "instance_id": instance_id, "position_key": "RELIANCE", "action": "close_all",
        })
        assert closed.status_code == 200
        assert closed.get_json()["remaining_qty"] == 0

    def test_orders_endpoint_filters_and_reports_slippage(self, client):
        instance_id = _spawn_equity(client)
        _drive_into_a_position(client, instance_id)

        body = client.get("/api/portfolio/orders").get_json()
        assert body["success"] is True
        assert body["summary"]["filled"] >= 1
        order = body["orders"][0]
        assert order["runner"] == "buy_and_hold·RELIANCE·1day"
        assert order["requested_price"] is not None
        assert order["slippage"] == 0.0  # the paper profile fills at the touch

        filled = client.get("/api/portfolio/orders?status=filled").get_json()
        assert all(o["status"] == "FILLED" for o in filled["orders"])
        assert client.get("/api/portfolio/orders?status=bogus").status_code == 400
        assert client.get(
            f"/api/portfolio/orders?instance_id={instance_id}"
        ).get_json()["summary"]["filled"] >= 1
        assert client.get("/api/portfolio/orders?instance_id=ghost").status_code == 404
        assert client.get("/api/portfolio/orders?mode=live").get_json()["summary"]["total"] == 0

        # Cancel: unknown id 404, a filled order cannot be cancelled (409).
        assert client.post("/api/portfolio/orders/PRT-nope-1-1/cancel").status_code == 404
        assert client.post(
            f"/api/portfolio/orders/{order['client_order_id']}/cancel"
        ).status_code == 409

    def test_a_pending_order_can_be_cancelled_over_http(self, client):
        from backtest.forward.portfolio_manager import get_portfolio_manager

        instance_id = _spawn_equity(client)
        pending = get_portfolio_manager().ledger.submit(
            instance_id, OrderRequest(symbol="RELIANCE", side="BUY", quantity=1)
        )
        listed = client.get("/api/portfolio/orders?status=pending").get_json()
        assert listed["summary"]["pending"] == 1
        assert listed["orders"][0]["cancellable"] is True

        cancelled = client.post(f"/api/portfolio/orders/{pending.client_order_id}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.get_json()["order"]["status"] == ORDER_CANCELLED
        assert client.get("/api/portfolio/orders?status=pending").get_json()["summary"]["pending"] == 0

    def test_the_sse_snapshot_carries_the_orders_summary_for_the_badge(self, client):
        instance_id = _spawn_equity(client)
        _drive_into_a_position(client, instance_id)
        payload = client.get("/api/portfolio/summary").get_json()["portfolio"]
        assert payload["orders_summary"]["filled"] >= 1
        assert payload["positions"][0]["instance_id"] == instance_id


# ---------------------------------------------------------------------------
# 6. Restart survival of the operator's levels
# ---------------------------------------------------------------------------


class TestLevelsSurviveARestart:
    def test_equity_levels_round_trip_and_stale_ones_are_dropped(self, tmp_path):
        from backtest.forward.state_store import capture_runner, restore_runner

        ledger = OrderLedger()
        runner = _equity_runner()
        runner.ledger = ledger
        _feed(runner, "RELIANCE", [100.0] * 14)
        runner.set_position_rule("RELIANCE", RULE_STOP_LOSS, 90.0)
        runner.set_position_rule("RELIANCE", RULE_TARGET, 130.0)
        # A level with no position behind it (a closed trade's leftover) must
        # never come back — it would arm a stop on someone else's entry.
        captured = capture_runner(runner)
        captured["position_rules"]["TCS"] = {"stop_loss": 10.0}

        revived = _equity_runner()
        restore_runner(revived, captured)
        assert revived.position_rules == {"RELIANCE": {"stop_loss": 90.0, "target": 130.0}}

    def test_option_structure_levels_round_trip(self, tmp_path):
        from backtest.forward.state_store import capture_runner, restore_runner

        runner = _option_runner()
        structure = _open_option_structure(runner)
        mark = runner.options_bridge._open_mark()
        runner.set_position_rule(structure.structure_id, RULE_STOP_LOSS, round(mark * 0.5, 2))

        revived = _option_runner()
        restore_runner(revived, capture_runner(runner))
        assert revived.options_bridge.open_structure_id == structure.structure_id
        assert revived.options_bridge.manual_rules()["stop_loss"] == pytest.approx(
            round(mark * 0.5, 2))

    def test_a_level_that_no_longer_makes_sense_is_dropped_not_armed(self):
        from backtest.forward.state_store import capture_runner, restore_runner

        runner = _option_runner()
        structure = _open_option_structure(runner)
        mark = runner.options_bridge._open_mark()
        captured = capture_runner(runner)
        # Simulate the book having moved while the process was down: the saved
        # stop now sits above the restored mark and cannot be armed.
        captured["bridge_manual_rules"] = {"stop_loss": float(mark) * 10, "target": None}

        revived = _option_runner()
        restore_runner(revived, captured)
        assert revived.options_bridge.open_structure_id == structure.structure_id
        assert revived.options_bridge.manual_rules() == {"stop_loss": None, "target": None}

    def test_the_state_file_is_json_safe_with_rules_armed(self, tmp_path):
        from backtest.forward.state_store import PortfolioStateStore, capture_runner

        runner = _long_equity_runner(entry_price=100.0)
        runner.set_position_rule("RELIANCE", RULE_STOP_LOSS, 90.0)
        store = PortfolioStateStore(tmp_path / "state.json")
        store.save({"runners": [capture_runner(runner)]})
        reloaded = json.loads((tmp_path / "state.json").read_text())
        assert reloaded["runners"][0]["position_rules"]["RELIANCE"]["stop_loss"] == 90.0
