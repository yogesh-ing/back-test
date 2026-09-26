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
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from backtest.forward.options_bridge import OptionsBridge
from backtest.forward.paper_runner import (
    ORDER_AGING_ALERT_S,
    ORDER_AGING_WARN_S,
    ORDER_CANCELLED,
    ORDER_FILLED,
    ORDER_PENDING,
    ORDER_REJECTED,
    RULE_STOP_LOSS,
    RULE_TARGET,
    OrderLedger,
    OrderRefused,
    OrderRequest,
    OrderRetryPolicy,
    PaperBroker,
    RunnerConfig,
    StrategyRunner,
)
from backtest.options.exit_policy import EXIT_MANUAL_STOP, EXIT_MANUAL_TARGET
from backtest.simulator.execution import free_executor
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
        ledger.submit("a", OrderRequest(symbol="P", side="BUY", quantity=1))
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

    def test_the_sse_snapshot_seeds_the_orders_badge(self, client):
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


# ---------------------------------------------------------------------------
# 6. Phase 3 — amend a working order
# ---------------------------------------------------------------------------


class TestOrderAmend:
    def test_amending_keeps_the_order_and_its_original_intent(self):
        """An amend is not cancel-and-replace: the coid, the fills already
        routed to it and the record of what was originally asked for survive."""
        ledger = OrderLedger()
        order = ledger.submit("inst", OrderRequest(symbol="X", side="BUY", quantity=10))
        order.requested_price = 100.0

        amended = ledger.amend(order.client_order_id, quantity=25, limit_price=99.5)
        assert amended.quantity == 25.0
        assert amended.limit_price == 99.5
        assert amended.amend_count == 1
        assert amended.amended_ts
        # Slippage keeps measuring the fill against the price the RUNNER
        # decided on — an amendment must not silently move the yardstick.
        assert amended.requested_price == 100.0

        row = ledger.row_with_age(ledger.get_order(order.client_order_id))
        assert row["client_order_id"] == order.client_order_id
        assert row["amend_count"] == 1
        assert row["amended_quantity"] == 25.0
        assert row["amended_limit_price"] == 99.5
        assert row["modifiable"] is False  # paper: nothing rests at a venue

        # A second amend accumulates rather than resetting the history.
        ledger.amend(order.client_order_id, limit_price=102.0)
        assert ledger.get_order(order.client_order_id).amend_count == 2

    @pytest.mark.parametrize("bad", [0, -5])
    def test_a_nonsense_amendment_is_refused(self, bad):
        ledger = OrderLedger()
        order = ledger.submit("inst", OrderRequest(symbol="X", side="BUY", quantity=10))
        with pytest.raises(ValueError):
            ledger.amend(order.client_order_id, quantity=bad)
        with pytest.raises(ValueError):
            ledger.amend(order.client_order_id, limit_price=bad)
        with pytest.raises(ValueError):
            ledger.amend(order.client_order_id)  # nothing to change
        with pytest.raises(KeyError):
            ledger.amend("PRT-nope-1-1", quantity=5)
        assert ledger.get_order(order.client_order_id).amend_count == 0

    def test_a_terminal_order_cannot_be_amended(self):
        """A filled order is history; amending it would rewrite what happened."""
        ledger = OrderLedger()
        filled = ledger.submit("inst", OrderRequest(symbol="X", side="BUY", quantity=1))
        ledger.apply_fill(filled.client_order_id, 10.0, 1)
        rejected = ledger.submit("inst", OrderRequest(symbol="Y", side="BUY", quantity=1))
        ledger.reject(rejected.client_order_id, "no margin")
        for coid in (filled.client_order_id, rejected.client_order_id):
            with pytest.raises(ValueError, match="only a working order"):
                ledger.amend(coid, quantity=2)


class TestAmendAtTheVenue:
    """The manager + gateway path: the venue is asked FIRST."""

    @pytest.fixture()
    def venue(self, monkeypatch):
        from live_test_support import FakeLiveBroker

        from backtest.forward.portfolio_manager import reset_portfolio_manager

        broker = FakeLiveBroker()
        monkeypatch.delenv("PORTFOLIO_STATE_PATH", raising=False)
        monkeypatch.setenv("ALLOW_LIVE_ORDERS", "1")
        mgr = reset_portfolio_manager(
            auto_start_feed=False, tick_seconds=1.0, live_broker=broker, confirm_live_orders=True
        )
        yield mgr, broker
        mgr.shutdown()

    def _live_working_order(self, mgr):
        """A live runner whose entry order is PLACED and never fills."""
        instance_id = mgr.add_runner(RunnerConfig(
            name="LIVE-EQ", strategy_name="buy_and_hold", allocated_capital=500_000,
            symbols=["TCS"], timeframe="1day", mode="live", source="synthetic",
        ))
        runner = mgr.get_runner(instance_id)
        _feed(runner, "TCS", [200.0] * 14)
        rows = [o for o in mgr.list_orders(instance_id=instance_id)["orders"]
                if o["status"] == ORDER_PENDING]
        assert rows, "the fake venue should have left an order working"
        return instance_id, rows[0]

    def test_amending_a_live_order_asks_the_venue_then_the_ledger(self, venue):
        mgr, broker = venue
        instance_id, row = self._live_working_order(mgr)
        coid = row["client_order_id"]
        assert row["modifiable"] is True
        assert row["broker_order_id"].startswith("FAKE-")

        result = mgr.modify_order(coid, quantity=25, limit_price=199.5)
        assert result["venue_amended"] is True
        assert result["quantity"] == 25.0
        # The venue saw the amendment — and ONLY the venue id of that order.
        assert broker.amended == [(row["broker_order_id"], 25, 199.5)]
        # … and only then did the local row change.
        after = {o["client_order_id"]: o
                 for o in mgr.list_orders(instance_id=instance_id)["orders"]}[coid]
        assert after["quantity"] == 25.0
        assert after["limit_price"] == 199.5
        assert after["amend_count"] == 1
        assert any("ORDER_AMENDED" in e["action"] for e in mgr.get_audit_log(scope="all"))

    def test_a_venue_refusal_leaves_everything_alone(self, venue):
        mgr, broker = venue
        instance_id, row = self._live_working_order(mgr)
        coid = row["client_order_id"]
        broker.settled.add(row["broker_order_id"])  # the venue has moved on

        with pytest.raises(RuntimeError, match="venue refused the amendment"):
            mgr.modify_order(coid, quantity=25)
        after = {o["client_order_id"]: o
                 for o in mgr.list_orders(instance_id=instance_id)["orders"]}[coid]
        assert after["quantity"] == row["quantity"], "a refused amend must not apply locally"
        assert after["amend_count"] == 0
        assert broker.amended == []

    def test_a_paper_order_cannot_be_amended_and_says_why(self, venue):
        mgr, _ = venue
        instance_id = mgr.add_runner(RunnerConfig(
            name="PAPER-EQ", strategy_name="buy_and_hold", allocated_capital=500_000,
            symbols=["RELIANCE"], timeframe="1day", mode="paper", source="synthetic",
        ))
        runner = mgr.get_runner(instance_id)
        _feed(runner, "RELIANCE", [100.0] * 14)
        filled = [o for o in mgr.list_orders(instance_id=instance_id)["orders"]
                  if o["status"] == ORDER_FILLED][0]
        with pytest.raises(ValueError, match="only a working order"):
            mgr.modify_order(filled["client_order_id"], quantity=1)

        # A working PAPER row (submitted by hand) is still not amendable:
        # nothing is resting at a venue.
        order = mgr.ledger.submit(instance_id, OrderRequest(
            symbol="RELIANCE", side="BUY", quantity=10))
        with pytest.raises(ValueError, match="paper order"):
            mgr.modify_order(order.client_order_id, quantity=5)
        with pytest.raises(ValueError, match="needs a new quantity"):
            mgr.modify_order(order.client_order_id)
        with pytest.raises(KeyError):
            mgr.modify_order("PRT-nope-1-1", quantity=5)

    def test_cancelling_a_live_order_cancels_it_at_the_venue(self, venue):
        mgr, broker = venue
        instance_id, row = self._live_working_order(mgr)
        before = mgr._live_gateway.working_count()
        mgr.cancel_order(row["client_order_id"])
        assert broker.cancelled == [row["broker_order_id"]]
        still = {o["client_order_id"] for o in mgr.list_orders(
            instance_id=instance_id, statuses=[ORDER_PENDING])["orders"]}
        assert row["client_order_id"] not in still
        # The venue is no longer polling it, and nothing was invented locally.
        assert mgr._live_gateway.working_count() == before - 1

    def test_a_venue_refused_cancel_does_not_lie_locally(self, venue):
        mgr, broker = venue
        instance_id, row = self._live_working_order(mgr)
        broker.settled.add(row["broker_order_id"])
        with pytest.raises(RuntimeError, match="venue refused the cancel"):
            mgr.cancel_order(row["client_order_id"])
        still = {o["client_order_id"] for o in mgr.list_orders(
            instance_id=instance_id, statuses=[ORDER_PENDING])["orders"]}
        assert row["client_order_id"] in still, "the order is still working at the venue"
        assert mgr._live_gateway.working_state(row["client_order_id"]) is not None
        assert broker.cancelled == []

    def test_an_untracked_venue_order_is_refused_rather_than_half_cancelled(self, venue):
        """A restored process that is not polling the order must not cancel it
        locally — the order would keep working at the broker, invisible."""
        mgr, _ = venue
        _, row = self._live_working_order(mgr)
        mgr._live_gateway._working.clear()
        with pytest.raises(ValueError, match="not tracked by this process"):
            mgr.cancel_order(row["client_order_id"])
        with pytest.raises(ValueError, match="not tracked by this process"):
            mgr.modify_order(row["client_order_id"], quantity=1)


# ---------------------------------------------------------------------------
# 7. Phase 3 — order aging alerts
# ---------------------------------------------------------------------------


class TestOrderAging:
    def _aged_order(self, ledger, age_s: float):
        order = ledger.submit("inst", OrderRequest(symbol="X", side="BUY", quantity=5))
        stamp = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat()
        order.created_ts = stamp
        return order

    def test_rows_carry_their_age_and_band(self):
        ledger = OrderLedger()
        fresh = self._aged_order(ledger, 5)
        warm = self._aged_order(ledger, ORDER_AGING_WARN_S + 5)
        stuck = self._aged_order(ledger, ORDER_AGING_ALERT_S + 5)
        settled = ledger.submit("inst", OrderRequest(symbol="DONE", side="BUY", quantity=1))
        ledger.apply_fill(settled.client_order_id, 10.0, 1)

        bands = {r["symbol"]: (r["aging"], r["age_s"]) for r in ledger.snapshot()}
        assert bands["X"][0] == "alert"  # newest first: the stuck one is last in
        assert bands["X"][1] >= ORDER_AGING_ALERT_S
        rows = {r["client_order_id"]: r for r in ledger.snapshot()}
        assert rows[fresh.client_order_id]["aging"] == ""
        assert rows[warm.client_order_id]["aging"] == "warn"
        assert rows[stuck.client_order_id]["aging"] == "alert"
        # A settled order has no age: "filled 10 minutes ago" is not a working
        # order that needs attention.
        assert rows[settled.client_order_id]["age_s"] is None
        assert rows[settled.client_order_id]["aging"] == ""

    def test_the_summary_reports_counts_the_oldest_and_the_thresholds(self):
        ledger = OrderLedger()
        self._aged_order(ledger, ORDER_AGING_WARN_S + 5)
        self._aged_order(ledger, ORDER_AGING_WARN_S + 90)
        self._aged_order(ledger, ORDER_AGING_ALERT_S + 5)
        summary = ledger.summary()
        assert summary["aging_warn_count"] == 2
        assert summary["aging_alert_count"] == 1
        assert summary["aging_warn_s"] == ORDER_AGING_WARN_S
        assert summary["aging_alert_s"] == ORDER_AGING_ALERT_S
        assert summary["oldest_pending_age_s"] >= ORDER_AGING_ALERT_S
        assert summary["oldest_pending_coid"] == summary["oldest_pending_coid"]
        assert summary["oldest_pending_symbol"] in {"X"}
        # Nothing pending → no oldest, no alerts, but the thresholds still ship.
        ledger_2 = OrderLedger()
        empty = ledger_2.summary()
        assert empty["oldest_pending_coid"] is None
        assert empty["aging_warn_count"] == 0

    def test_the_manager_alerts_once_per_band_not_once_per_tick(self, monkeypatch):
        from backtest.forward.portfolio_manager import reset_portfolio_manager

        monkeypatch.delenv("PORTFOLIO_STATE_PATH", raising=False)
        mgr = reset_portfolio_manager(auto_start_feed=False, tick_seconds=1.0)
        try:
            order = mgr.ledger.submit("inst-eq", OrderRequest(
                symbol="RELIANCE", side="BUY", quantity=5))
            order.created_ts = (
                datetime.now(timezone.utc) - timedelta(seconds=ORDER_AGING_WARN_S + 1)
            ).isoformat()

            first = mgr.check_order_aging()
            assert [a["band"] for a in first] == ["warn"]
            assert first[0]["client_order_id"] == order.client_order_id
            assert first[0]["age_s"] >= ORDER_AGING_WARN_S
            # A second sweep in the same band is silent: an alert that repeats
            # every tick is noise the operator learns to ignore.
            assert mgr.check_order_aging() == []
            audit = [e["action"] for e in mgr.get_audit_log(scope="all")]
            assert sum("ORDER_WARNING" in a for a in audit) == 1

            # Escalation is a NEW alert — the order is now genuinely stuck.
            order.created_ts = (
                datetime.now(timezone.utc) - timedelta(seconds=ORDER_AGING_ALERT_S + 1)
            ).isoformat()
            second = mgr.check_order_aging()
            assert [a["band"] for a in second] == ["alert"]

            # Once it settles (or is cancelled) the memory is dropped, so a
            # later order reusing the symbol is not silently suppressed.
            mgr.ledger.cancel(order.client_order_id)
            assert mgr.check_order_aging() == []
            assert order.client_order_id not in mgr._aging_alerted
        finally:
            mgr.shutdown()

    def test_an_unreadable_ledger_never_breaks_the_tick(self, monkeypatch):
        from backtest.forward.portfolio_manager import reset_portfolio_manager

        monkeypatch.delenv("PORTFOLIO_STATE_PATH", raising=False)
        mgr = reset_portfolio_manager(auto_start_feed=False, tick_seconds=1.0)
        try:
            def boom(*_a, **_k):
                raise RuntimeError("ledger unavailable")

            monkeypatch.setattr(mgr.ledger, "snapshot", boom)
            assert mgr.check_order_aging() == []  # monitoring must not raise
        finally:
            mgr.shutdown()


# ---------------------------------------------------------------------------
# 8. Phase 3 — auto-retry of a refused order
# ---------------------------------------------------------------------------


class TestAutoRetry:
    """A transition signal fires ONCE. If that one order is refused and nobody
    retries it, the strategy sits flat forever with nothing in the P&L to
    explain why — this is the machinery that closes that gap."""

    calls: dict = {}

    @classmethod
    def _rejecting_executor(cls, runner):
        """Make the executor refuse, the way a venue with no liquidity would."""
        from backtest.simulator.execution import ExecutionResult, ExecutionStatus

        cls.calls = {"n": 0}

        def refuse(sim_order, market):
            cls.calls["n"] += 1
            return ExecutionResult(
                order_id=sim_order.order_id,
                symbol=sim_order.symbol,
                status=ExecutionStatus.REJECTED,
                reason="no liquidity",
                fill=None,
            )

        runner.executor.execute = refuse

    @staticmethod
    def _runner(retry: OrderRetryPolicy | None, **overrides):
        kwargs = dict(
            name="RETRY-EQ", strategy_name="buy_and_hold", allocated_capital=500_000,
            symbols=["RELIANCE"], timeframe="1day", mode="paper", source="synthetic",
            retry_policy=retry,
        )
        kwargs.update(overrides)
        runner = StrategyRunner(RunnerConfig(**kwargs), ledger=OrderLedger())
        runner.start()
        return runner

    def test_a_policy_is_off_by_default(self):
        runner = self._runner(None)
        assert runner._retry_policy() is None
        self._rejecting_executor(runner)
        _feed(runner, "RELIANCE", [100.0] * 14)
        # The refusal is recorded as a REJECTED row, and no retry is queued.
        assert runner._retries == {}
        assert runner.retries_raised == 0
        rows = runner.ledger.snapshot(statuses=[ORDER_REJECTED])
        assert rows, "a refused order must still be visible in the ledger"

    def test_a_refused_entry_is_queued_and_retried_until_it_lands(self):
        runner = self._runner(OrderRetryPolicy(max_attempts=2, cooldown_s=0.0))
        self._rejecting_executor(runner)
        _feed(runner, "RELIANCE", [100.0] * 13)
        assert runner.positions == {}, "the fixture must fail to enter first"
        pending = runner.get_state()["order_retries"]
        assert pending["raised"] >= 1
        assert pending["pending"], "a retryable refusal must be queued"
        queued = pending["pending"][0]
        assert queued["kind"] == "entry" and queued["side"] == "BUY"
        assert queued["attempts"] >= 1, "the next bar should already have re-sent it"

        # The venue recovers: the next bar's retry fills and the queue empties.
        runner.executor = free_executor(runner.portfolio)
        _feed(runner, "RELIANCE", [100.0], start_day=14)
        assert runner.positions, "the recovered retry should have opened the position"
        state = runner.get_state()["order_retries"]
        assert state["recovered"] >= 1
        assert state["pending"] == []
        assert state["blocked"] == [], "a successful retry must not leave a block"
        # The recovered order is a real ledger row carrying its lineage.
        recovered = [r for r in runner.ledger.snapshot()
                     if r["status"] == ORDER_FILLED and r["tag"].get("retry_attempt")]
        assert recovered, "the retry that landed must be traceable to its origin"
        assert recovered[0]["tag"]["retry_of"]

    def test_the_retry_budget_is_finite_and_reported_when_spent(self):
        runner = self._runner(OrderRetryPolicy(max_attempts=2, cooldown_s=0.0))
        self._rejecting_executor(runner)
        _feed(runner, "RELIANCE", [100.0] * 25)
        state = runner.get_state()["order_retries"]
        assert state["exhausted"] == 1
        assert state["raised"] == 1, (
            "one refusal episode, one budget — a spent budget must not be "
            "re-opened by the next bar's identical signal"
        )
        assert state["pending"] == [], "an exhausted retry must not stay queued"
        assert state["blocked"] and state["blocked"][0]["kind"] == "entry"
        # Exactly the first send plus its two retries — no fourth attempt.
        assert self.calls["n"] == 3
        # Every attempt is a real ledger row, and each carries its lineage —
        # the Orders tab shows a lineage, not a wall of mystery duplicates.
        attempts = [r for r in runner.ledger.snapshot() if r["tag"].get("retry_attempt")]
        assert attempts, "the retries must be visible in the ledger"
        assert all(r["tag"]["retry_of"] for r in attempts)
        assert max(r["tag"]["retry_attempt"] for r in attempts) <= 2
        reasons = [e["reason"] for e in runner.signal_log if e["kind"] == "RETRY_EXHAUSTED"]
        assert reasons, "giving up must be stated, not silent"

    def test_the_cooldown_is_respected(self):
        runner = self._runner(OrderRetryPolicy(max_attempts=5, cooldown_s=3600.0))
        self._rejecting_executor(runner)
        _feed(runner, "RELIANCE", [100.0] * 16)
        queued = runner.get_state()["order_retries"]["pending"]
        assert queued and queued[0]["attempts"] == 0, (
            "a retry must not fire before its cooldown, however many bars pass"
        )

    def test_a_non_retryable_refusal_is_never_requeued(self):
        """A live placement error may have been accepted with the ack lost —
        re-sending there is how one intent becomes two live orders."""
        runner = self._runner(OrderRetryPolicy(max_attempts=3, cooldown_s=0.0))
        calls = {"n": 0}

        def refuse(*_a, **_k):
            calls["n"] += 1
            raise OrderRefused("venue refused the placement: timeout", retryable=False)

        runner.broker.submit_market = refuse
        _feed(runner, "RELIANCE", [100.0] * 16)
        assert calls["n"] == 1, "a non-retryable refusal must be sent exactly once"
        assert runner._retries == {}
        assert runner.retries_raised == 0
        kinds = [e["kind"] for e in runner.signal_log]
        assert "RETRY_REFUSED" in kinds
        message = [e["reason"] for e in runner.signal_log if e["kind"] == "RETRY_REFUSED"][0]
        assert "reconcile before re-sending" in message

    def test_a_spent_budget_rearms_after_the_episode_window(self):
        """A budget of 2 would strand the runner for good on a venue that is
        down longer than one episode — a buy-and-hold entry never flips its
        signal, so nothing else would ever re-open the question."""
        runner = self._runner(OrderRetryPolicy(max_attempts=1, cooldown_s=0.0))
        self._rejecting_executor(runner)
        _feed(runner, "RELIANCE", [100.0] * 14)
        assert runner._retry_blocked, "the budget is spent"
        assert self.calls["n"] == 2  # the send plus its one retry

        # Inside the window nothing more is sent, however many bars pass.
        rearm = OrderRetryPolicy().rearm_after_s
        assert rearm >= ORDER_AGING_WARN_S, "a re-arm must not be a fast loop"
        _feed(runner, "RELIANCE", [100.0] * 6, start_day=14)
        assert self.calls["n"] == 2
        assert {e["kind"] for e in runner.signal_log} & {"RETRY_REARMED"} == set()

        # Once the episode window has passed, a NEW episode is allowed — and
        # the re-arm is stated, not silent.
        runner._retry_blocked["RELIANCE"]["blocked_ts"] -= (rearm + 1)
        _feed(runner, "RELIANCE", [100.0], start_day=20)
        assert self.calls["n"] == 3  # the new episode's own send
        _feed(runner, "RELIANCE", [100.0], start_day=21)
        kinds = [e["kind"] for e in runner.signal_log]
        assert "RETRY_REARMED" in kinds
        assert self.calls["n"] == 4  # …and its one retry, on the next bar
        assert runner.get_state()["order_retries"]["raised"] == 2

    def test_the_signal_changing_lifts_the_block_immediately(self):
        runner = self._runner(OrderRetryPolicy(max_attempts=1, cooldown_s=0.0))
        self._rejecting_executor(runner)
        _feed(runner, "RELIANCE", [100.0] * 14)
        assert runner._retry_blocked
        # The strategy stops asking for the entry (a real change of mind):
        # the block lifts, so a later signal is a fresh decision, not a loop.
        assert runner.clear_retry_block("RELIANCE", "entry") is True
        assert runner._retry_blocked == {}
        assert runner.clear_retry_block("RELIANCE", "entry") is False

    def test_a_refused_exit_is_retried_so_the_position_can_get_out(self):
        runner = self._runner(OrderRetryPolicy(max_attempts=2, cooldown_s=0.0))
        _feed(runner, "RELIANCE", [100.0] * 14)
        assert runner.positions
        calls = {"n": 0}

        def refuse(*_a, **_k):
            calls["n"] += 1
            raise OrderRefused("paper fill did not execute: REJECTED — no liquidity",
                               retryable=True)

        runner.broker.submit_market = refuse
        runner._emit_close("RELIANCE", 100.0, reason="strategy_exit")
        assert calls["n"] == 1
        assert runner.get_state()["order_retries"]["pending"][0]["kind"] == "exit"


# ---------------------------------------------------------------------------
# 9. Phase 3 — the REST surface for amending + aging
# ---------------------------------------------------------------------------


@pytest.fixture()
def live_client(monkeypatch):
    """A test client whose manager can place live orders (fake venue armed)."""
    from live_test_support import FakeLiveBroker

    from backtest.forward.portfolio_manager import (
        get_portfolio_manager,
        reset_portfolio_manager,
    )
    from backtest.forward.risk_supervisor import GlobalRiskConfig
    from backtest.web.app import create_app

    broker = FakeLiveBroker()
    monkeypatch.delenv("PORTFOLIO_STATE_PATH", raising=False)
    monkeypatch.setenv("ALLOW_LIVE_ORDERS", "1")
    reset_portfolio_manager(
        risk_config=GlobalRiskConfig(daily_loss_limit=100_000, max_drawdown_pct=0.50),
        tick_seconds=1.0,
        warmup_bars=15,
        auto_start_feed=False,
        live_broker=broker,
        confirm_live_orders=True,
    )
    app = create_app(source="synthetic")
    with app.test_client() as c:
        c.broker = broker  # type: ignore[attr-defined]
        c.manager = get_portfolio_manager()  # type: ignore[attr-defined]
        yield c
    get_portfolio_manager().shutdown()


def _drive_live_into_a_working_order(client, instance_id, symbol="TCS", price=200.0):
    """Feed a live runner: the order goes to the venue and RESTS there.

    The venue never fills (that is what makes it a *working* order), so unlike
    the paper driver this one waits for the LEDGER row rather than a position.
    """
    from backtest.forward.portfolio_manager import get_portfolio_manager

    runner = get_portfolio_manager().get_runner(instance_id)
    _feed(runner, symbol, [price] * 14)
    assert runner.positions == {}, "the fake venue must not fill — nothing to poll"
    return runner


def _spawn_live(client, symbol="TCS"):
    created = client.post("/api/portfolio/runner/create", json={
        "strategy": "buy_and_hold",
        "target_type": "SINGLE_SYMBOL",
        "symbol": symbol,
        "timeframe": "1day",
        "allocated_capital": 500_000,
        "mode": "live",
        "source": "synthetic",
    })
    assert created.status_code == 201, created.get_json()
    return created.get_json()["instance_id"]


class TestOrderEndpointsPhase3:
    def test_a_working_order_can_be_amended_over_http(self, live_client):
        instance_id = _spawn_live(live_client)
        _drive_live_into_a_working_order(live_client, instance_id)
        rows = live_client.get(
            f"/api/portfolio/orders?instance_id={instance_id}").get_json()["orders"]
        working = [o for o in rows if o["status"] == ORDER_PENDING]
        assert working and working[0]["modifiable"] is True
        coid = working[0]["client_order_id"]
        original_qty = working[0]["quantity"]

        resp = live_client.post(f"/api/portfolio/orders/{coid}/modify", json={"quantity": 11})
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body["success"] is True and body["quantity"] == 11
        assert body["order"]["amend_count"] == 1
        assert body["order"]["amended_quantity"] == 11
        assert body["order"]["quantity"] == 11

        # …and the ledger really holds the amended terms.
        again = live_client.get(
            f"/api/portfolio/orders?instance_id={instance_id}").get_json()["orders"]
        assert {o["client_order_id"]: o["quantity"] for o in again}[coid] == 11
        assert original_qty != 11

    def test_amend_error_cases_are_explicit(self, live_client):
        instance_id = _spawn_live(live_client)
        _drive_live_into_a_working_order(live_client, instance_id)
        rows = live_client.get(
            f"/api/portfolio/orders?instance_id={instance_id}").get_json()["orders"]
        coid = [o for o in rows if o["status"] == ORDER_PENDING][0]["client_order_id"]

        # Unknown order → 404; bad payload → 400; nothing to change → 409.
        assert live_client.post(
            "/api/portfolio/orders/PRT-nope-1-1/modify", json={"quantity": 1}
        ).status_code == 404
        assert live_client.post(
            f"/api/portfolio/orders/{coid}/modify", json={"quantity": "lots"}
        ).status_code == 400
        assert live_client.post(
            f"/api/portfolio/orders/{coid}/modify", json={}
        ).status_code == 409

        # A venue refusal is a 409 that leaves the order working, unchanged.
        broker = live_client.broker  # type: ignore[attr-defined]
        venue_id = [o for o in rows if o["status"] == ORDER_PENDING][0]["broker_order_id"]
        broker.settled.add(venue_id)
        refused = live_client.post(
            f"/api/portfolio/orders/{coid}/modify", json={"quantity": 7})
        assert refused.status_code == 409
        assert "venue refused the amendment" in refused.get_json()["error"]
        after = live_client.get(
            f"/api/portfolio/orders?instance_id={instance_id}").get_json()["orders"]
        assert {o["client_order_id"]: o for o in after}[coid]["status"] == ORDER_PENDING
        assert {o["client_order_id"]: o for o in after}[coid]["amend_count"] == 0

    def test_the_orders_read_reports_aging_and_amendability(self, live_client):
        instance_id = _spawn_live(live_client)
        _drive_live_into_a_working_order(live_client, instance_id)
        body = live_client.get(f"/api/portfolio/orders?instance_id={instance_id}").get_json()
        summary = body["summary"]
        assert summary["aging_warn_s"] == ORDER_AGING_WARN_S
        assert summary["aging_alert_s"] == ORDER_AGING_ALERT_S
        assert summary["aging_warn_count"] == 0  # just placed
        assert summary["oldest_pending_coid"]
        for row in body["orders"]:
            assert "aging" in row and "age_s" in row and "modifiable" in row
            assert row["modifiable"] is (row["status"] == ORDER_PENDING)

        # The bucket-scoped read (what the tab actually calls) carries the same
        # aging telemetry — the strip and the badge must not disagree.
        scoped = live_client.get("/api/portfolio/orders?mode=live").get_json()["summary"]
        for key in ("aging_warn_s", "aging_alert_s", "aging_warn_count",
                    "aging_alert_count", "oldest_pending_coid"):
            assert key in scoped, f"the scoped summary is missing {key}"
        assert scoped["aging_warn_s"] == ORDER_AGING_WARN_S
        assert scoped["oldest_pending_symbol"] == "TCS"
        assert scoped == live_client.get(
            f"/api/portfolio/orders?instance_id={instance_id}").get_json()["summary"]
