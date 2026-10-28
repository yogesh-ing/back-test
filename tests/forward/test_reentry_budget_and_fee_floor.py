"""Ticket 1.2 — the day's re-entry budget, the take-profit fee floor, and
``BLOCKED_BY_RISK`` health (QA 2026-10-28: BUG-02, BUG-15).

The 28-Oct live-paper session watched one 1-hour short-strangle runner open and
stop out 205 times in an hour: ``max_reentries_per_day`` was declared but only
the deleted signal-flip re-entry path ever incremented it, ``reentry_cooldown_
seconds`` was read only on the tick path, and every ±₹100 target was booked as a
net loss because the structure's own round-trip fee is ₹193. Meanwhile the same
``MAX_LOSS_EXCEEDED`` refusal repeated 11,410 times per hour while the runner
card still read 🟢 RUNNING.

See ``docs/BUG-FIX-PLAN-2026-10-28.md`` Ticket 1.2 and
``docs/QA-LIVE-PAPER-BUG-REPORT-2026-10-28.md`` BUG-02 / BUG-15.
"""

from __future__ import annotations

from datetime import timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backtest.forward.options_bridge import OptionsBridge
from backtest.forward.paper_runner import OrderLedger, RunnerConfig, StrategyRunner
from backtest.options.execution_gate import RESEARCH_STRUCTURES, StructureNotAllowedError
from backtest.options.exit_policy import take_profit_fee_floor_violation
from backtest.options.quote_providers import (
    SyntheticChainGenerator,
    SyntheticQuoteProvider,
)
from backtest.plugins import discover_plugins
from backtest.strategy.execution import parse_exit_plan_strict
from backtest.strategy.registry import get_strategy
from backtest.timekeeping import from_market_clock

IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture(autouse=True)
def _testing_profile(monkeypatch):
    monkeypatch.setenv("BACKTEST_DATA_PROFILE", "testing")
    monkeypatch.delenv("MSTOCK_REENTRY_COOLDOWN_SECONDS", raising=False)
    discover_plugins()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _expression(exit_cfg: dict, structure: str = "long_call") -> dict:
    exit_cfg = dict(exit_cfg)
    if not any(k in exit_cfg for k in ("stop_loss_pct", "stop_loss_points", "stop_loss_price")):
        # A strict v2 plan needs exactly one hard stop; a long option's worst
        # case is its premium, so tests that do not declare a level say so.
        exit_cfg.setdefault("structure_capped", True)
    return {
        "expression_version": 2,
        "type": structure,
        "quantity": 1,
        "exit": exit_cfg,
    }


def _bridge(exit_cfg: dict | None = None, structure: str = "long_call") -> OptionsBridge:
    return OptionsBridge(
        capital=1_000_000.0,
        expression=_expression(exit_cfg or {}, structure),
        quote_provider=SyntheticQuoteProvider(SyntheticChainGenerator()),
        allowed_structures=RESEARCH_STRUCTURES,
    )


def _strangle_runner(name: str, *, max_reentries_per_day: int) -> StrategyRunner:
    """A live-shape 1-hour short-strangle runner: ticks drive both entry and exits."""
    expr = {
        **get_strategy("immediate_strangle").execution_spec().to_expression(),
        "reentry_cooldown_seconds": 0.0,
        "stale_after_seconds": 3600.0,
        "exit": {
            **get_strategy("immediate_strangle").execution_spec().to_expression().get(
                "exit", {}
            ),
            "max_reentries_per_day": max_reentries_per_day,
        },
    }
    runner = StrategyRunner(
        RunnerConfig(
            name=name,
            strategy_name="immediate_strangle",
            allocated_capital=500_000,
            symbols=["NIFTY"],
            timeframe="1hour",
            mode="paper",
            source="synthetic",
            instrument={"type": "option", "expression": expr},
        ),
        ledger=OrderLedger(),
    )
    runner.start()
    return runner


# ---------------------------------------------------------------------------
# BUG-02 — the entry budget is per session day, and every close spends it
# ---------------------------------------------------------------------------


class TestDayReentryBudget:
    def test_budget_is_the_first_entry_plus_the_declared_reentries(self):
        assert _bridge(
            {"reenter": True, "max_reentries_per_day": 2}
        )._max_entries_per_day() == 3
        assert _bridge(
            {"reenter": True, "max_reentries_per_day": 1}
        )._max_entries_per_day() == 2
        # ``0`` is a real setting (never re-enter), not "unset".
        assert _bridge(
            {"reenter": True, "max_reentries_per_day": 0}
        )._max_entries_per_day() == 1

    def test_twenty_stopouts_on_one_day_stop_at_the_declared_budget(self):
        """The 28-Oct churn: entries limited only by the tick stream, not by the plan.

        ``max_reentries_per_day = 1`` buys ONE re-entry: entry, stop, re-entry,
        stop — and then the day is spent no matter how many more bars arrive.
        Stamps are chosen so each entry crosses a fresh 1-hour IST bucket
        (09:15 IST + 5h30 = 04:00 UTC) — Ticket 1.1's latch owns the bucket, the
        day budget owns the session date.
        """
        runner = _strangle_runner("BUDGET-1", max_reentries_per_day=1)
        bridge = runner.options_bridge

        runner.on_tick("NIFTY", price=24500.0, ts="2026-10-26T04:50:00+00:00")
        assert bridge.executed_count == 1

        runner.on_tick("NIFTY", price=25200.0, ts="2026-10-26T05:00:00+00:00")
        runner.on_tick("NIFTY", price=24500.0, ts="2026-10-26T05:30:05+00:00")
        assert bridge.executed_count == 2, "the one declared re-entry must be allowed"
        assert bridge.closed_count == 1

        # Stop out again, then keep trading: every later hourly bucket of the
        # same session date is refused before the intent is priced.
        runner.on_tick("NIFTY", price=25200.0, ts="2026-10-26T06:00:00+00:00")
        assert bridge.closed_count == 2
        for hour in range(6, 12):
            out = runner.on_tick(
                "NIFTY", price=24500.0, ts=f"2026-10-26T{hour:02d}:30:05+00:00"
            )
            assert out is None, f"hour {hour} reopened past the day's budget"
        assert bridge.executed_count == 2
        assert bridge.last_block_code == "REENTRY_CAP_REACHED"

        summary = runner.options_summary()
        assert summary["structures_today"] == 2
        assert summary["max_entries_per_day"] == 2

    def test_the_budget_rolls_on_the_next_session_date(self):
        runner = _strangle_runner("BUDGET-2", max_reentries_per_day=1)
        bridge = runner.options_bridge

        runner.on_tick("NIFTY", price=24500.0, ts="2026-10-26T04:50:00+00:00")
        runner.on_tick("NIFTY", price=25200.0, ts="2026-10-26T05:00:00+00:00")
        runner.on_tick("NIFTY", price=24500.0, ts="2026-10-26T05:30:05+00:00")
        runner.on_tick("NIFTY", price=25200.0, ts="2026-10-26T06:00:00+00:00")
        assert bridge.closed_count == 2
        assert runner.on_tick("NIFTY", price=24500.0, ts="2026-10-26T06:30:05+00:00") is None

        # Tomorrow: the IST session date advances, the day's budget is fresh.
        res = runner.on_tick("NIFTY", price=24500.0, ts="2026-10-27T04:50:00+00:00")
        assert res is not None and res.get("structure_id")
        assert bridge.executed_count == 3
        assert bridge._closes_today == 0

    def test_a_standing_refusal_journals_once_and_logs_nothing_per_bar(self):
        """BUG-15's other half: a cap refusal must not become a signal-log row per bar."""
        runner = _strangle_runner("BUDGET-3", max_reentries_per_day=0)
        bridge = runner.options_bridge
        runner.on_tick("NIFTY", price=24500.0, ts="2026-10-26T04:50:00+00:00")
        runner.on_tick("NIFTY", price=25200.0, ts="2026-10-26T05:00:00+00:00")

        for hour in range(6, 12):
            assert runner.on_tick(
                "NIFTY", price=24500.0, ts=f"2026-10-26T{hour:02d}:30:05+00:00"
            ) is None
        assert bridge.executed_count == 1
        # ``max_reentries_per_day = 0`` → the first entry is the only one today.
        assert bridge._max_entries_per_day() == 1
        assert bridge.last_block_code == "REENTRY_CAP_REACHED"
        assert [s for s in runner.signal_log if s["kind"] == "OPTION_BLOCKED"] == []


# ---------------------------------------------------------------------------
# BUG-02 — the re-entry cooldown is a market-clock rule
# ---------------------------------------------------------------------------


class TestBarPathCooldown:
    def test_cooldown_measures_market_time_not_wall_time_on_the_bar_path(self):
        """A replay advances bars faster than the wall clock.

        Gating the bar path on ``time.monotonic()`` refused every re-entry after
        the first exit in any backfill — the guard has to read the bar clock it
        is pacing entries against.
        """
        bridge = _bridge({"reenter": True})
        bridge.reentry_cooldown_seconds = 300.0

        # Two bars a *market* minute apart, fed inside the same wall-clock second.
        bridge._bar_dt = from_market_clock("2026-09-11T09:16:00")
        bridge._last_exit_bar_dt = from_market_clock("2026-09-11T09:15:00")
        assert bridge._bar_cooldown_active() is True

        # A replayed day apart: the cooldown has elapsed in market time.
        bridge._bar_dt = from_market_clock("2026-09-12T09:15:00")
        assert bridge._bar_cooldown_active() is False

        # Past the window on the same day.
        bridge._bar_dt = from_market_clock("2026-09-11T09:21:00")
        assert bridge._bar_cooldown_active() is False

        # A knob of 0 disables the guard entirely.
        bridge.reentry_cooldown_seconds = 0.0
        bridge._bar_dt = from_market_clock("2026-09-11T09:15:30")
        bridge._last_exit_bar_dt = from_market_clock("2026-09-11T09:15:00")
        assert bridge._bar_cooldown_active() is False

    def test_a_close_stamps_the_market_clock_it_closed_on(self):
        bridge = _bridge({"reenter": True})
        bar_dt = from_market_clock("2026-09-11T09:15:00")
        bridge._bar_dt = bar_dt
        bridge._register_close()
        assert bridge._last_exit_bar_dt == bar_dt
        assert bridge._closes_today == 1


# ---------------------------------------------------------------------------
# BUG-02 — a declared take-profit has to clear the structure's own fees
# ---------------------------------------------------------------------------


class TestTakeProfitFeeFloor:
    @pytest.mark.parametrize(
        "target,fees,expected_violation",
        [
            (100.0, 193.0, True),  # the 28-Oct strangle: target < one round trip
            (231.0, 193.0, True),  # 1.2× ₹193 = ₹231.60 → still inside
            (232.0, 193.0, False),
            (1000.0, 193.0, False),
            (100.0, 0.0, False),  # no fee to compare against → silent
            (0.0, 193.0, False),  # no declared target → silent
            (None, 193.0, False),
            (100.0, None, False),
            (-100.0, 193.0, False),  # a nonsense target is not this gate's job
        ],
    )
    def test_the_floor_is_only_a_refusal_when_it_is_provable(
        self, target, fees, expected_violation
    ):
        result = take_profit_fee_floor_violation(target, fees)
        if expected_violation:
            assert result is not None and "BUG-02" in result
        else:
            assert result is None

    def test_the_multiplier_is_explicit(self):
        assert take_profit_fee_floor_violation(300.0, 193.0) is None
        assert take_profit_fee_floor_violation(300.0, 193.0, multiplier=2.0) is not None

    @staticmethod
    def _legs(price: Decimal) -> list:
        return [
            SimpleNamespace(side="SELL", quantity=1, lot_size=65, total_quantity=65),
            SimpleNamespace(side="SELL", quantity=1, lot_size=65, total_quantity=65),
        ]

    def _bridge_with_fees(self, take_profit_points: float, fees: float) -> OptionsBridge:
        bridge = _bridge(
            {"stop_loss_points": 2000, "take_profit_points": take_profit_points},
            structure="strangle",
        )
        bridge.option_broker = SimpleNamespace(
            fee_calculator=SimpleNamespace(
                calculate_structure=lambda legs, track_volume=False: SimpleNamespace(
                    total=fees
                )
            )
        )
        return bridge

    def test_the_bridge_refuses_a_target_inside_its_own_round_trip_fee(self):
        bridge = self._bridge_with_fees(100.0, 193.0)
        intent = SimpleNamespace(structure_type="strangle", legs=self._legs(Decimal("44.85")))
        with pytest.raises(StructureNotAllowedError) as exc:
            bridge._check_take_profit_fee_floor(
                intent, [(leg, Decimal("44.85")) for leg in intent.legs], Decimal("-5790.50")
            )
        assert exc.value.reason == "TAKE_PROFIT_BELOW_FEE_FLOOR"

    def test_a_target_above_the_floor_trades(self):
        bridge = self._bridge_with_fees(1000.0, 193.0)
        intent = SimpleNamespace(structure_type="strangle", legs=self._legs(Decimal("44.85")))
        bridge._check_take_profit_fee_floor(
            intent, [(leg, Decimal("44.85")) for leg in intent.legs], Decimal("-5790.50")
        )

    def test_a_percent_target_is_priced_off_the_premium_base(self):
        bridge = _bridge(
            {"stop_loss_pct": 0.35, "take_profit_pct": 0.05}, structure="long_call"
        )
        bridge.option_broker = SimpleNamespace(
            fee_calculator=SimpleNamespace(
                calculate_structure=lambda legs, track_volume=False: SimpleNamespace(
                    total=500.0
                )
            )
        )
        legs = self._legs(Decimal("100"))
        intent = SimpleNamespace(structure_type="long_call", legs=legs)
        # 5% of a ₹10,000 premium = ₹500 of target against ₹1,000 of fees → refused.
        with pytest.raises(StructureNotAllowedError) as exc:
            bridge._check_take_profit_fee_floor(
                intent, [(leg, Decimal("100")) for leg in legs], Decimal("10000")
            )
        assert exc.value.reason == "TAKE_PROFIT_BELOW_FEE_FLOOR"

    def test_no_fee_model_means_no_refusal(self):
        """Silence is safe: the gate must not refuse on a number it cannot state."""
        bridge = _bridge({"take_profit_points": 1.0}, structure="long_call")
        bridge.option_broker = SimpleNamespace(fee_calculator=None)
        legs = self._legs(Decimal("100"))
        intent = SimpleNamespace(structure_type="long_call", legs=legs)
        bridge._check_take_profit_fee_floor(
            intent, [(leg, Decimal("100")) for leg in legs], Decimal("10000")
        )

    def test_an_unpricable_leg_skips_the_gate_instead_of_refusing(self):
        bridge = self._bridge_with_fees(100.0, 193.0)
        legs = self._legs(Decimal("100"))
        intent = SimpleNamespace(structure_type="strangle", legs=legs)
        bridge._check_take_profit_fee_floor(
            intent, [(leg, Decimal("0")) for leg in legs], Decimal("0")
        )

    def test_the_strangle_default_bracket_clears_the_floor(self):
        """The shipped intent must not be refused by the gate it helped write."""
        plan = parse_exit_plan_strict(
            get_strategy("immediate_strangle").execution_spec().to_expression()["exit"]
        )
        assert plan.take_profit_points == 1000.0
        assert plan.stop_loss_points == 2000.0
        assert take_profit_fee_floor_violation(plan.take_profit_points, 193.0) is None


# ---------------------------------------------------------------------------
# BUG-15 — a stuck runner says so instead of spinning at 🟢 RUNNING
# ---------------------------------------------------------------------------


class TestBlockedByRiskHealth:
    def test_three_identical_refusals_flip_the_health_badge(self):
        bridge = _bridge({"reenter": True})
        bridge._last_attempted_contract = "NIFTY02NOV25000CE|NIFTY02NOV24000PE"

        first = bridge._register_entry_rejection("MAX_LOSS_EXCEEDED", "over the ceiling")
        assert first["blocked"] is False and first["suppress_log"] is False
        second = bridge._register_entry_rejection("MAX_LOSS_EXCEEDED", "over the ceiling")
        assert second["blocked"] is False and second["suppress_log"] is True
        third = bridge._register_entry_rejection("MAX_LOSS_EXCEEDED", "over the ceiling")
        assert third["blocked"] is True

        assert bridge.trade_health == "BLOCKED_BY_RISK"
        assert bridge.block_reason.startswith("MAX_LOSS_EXCEEDED×3")

        summary = bridge.summary()
        assert summary["trade_health"] == "BLOCKED_BY_RISK"
        assert summary["rejection_streak"] == 3

    def test_a_different_contract_starts_a_new_count(self):
        bridge = _bridge({"reenter": True})
        bridge._last_attempted_contract = "AAA"
        bridge._register_entry_rejection("MAX_LOSS_EXCEEDED", "x")
        bridge._register_entry_rejection("MAX_LOSS_EXCEEDED", "x")
        bridge._last_attempted_contract = "BBB"
        out = bridge._register_entry_rejection("MAX_LOSS_EXCEEDED", "x")
        assert bridge._rejection_streak == 1
        assert out["blocked"] is False

    def test_a_different_refusal_code_is_not_a_streak(self):
        bridge = _bridge({"reenter": True})
        bridge._last_attempted_contract = "AAA"
        bridge._register_entry_rejection("MAX_LOSS_EXCEEDED", "x")
        bridge._register_entry_rejection("MAX_LOSS_EXCEEDED", "x")
        bridge._register_entry_rejection("ORDER_VALUE_EXCEEDED", "y")
        assert bridge._rejection_streak == 1

    def test_a_fill_clears_the_blocked_state(self):
        bridge = _bridge({"reenter": True})
        bridge._last_attempted_contract = "AAA"
        for _ in range(3):
            bridge._register_entry_rejection("MAX_LOSS_EXCEEDED", "x")
        assert bridge.trade_health == "BLOCKED_BY_RISK"
        bridge._reset_blocked_state()
        assert bridge.trade_health == "OK"
        assert bridge.block_reason is None
        assert bridge._rejection_streak == 0

    def test_only_deterministic_refusals_are_counted(self):
        """A one-off market condition is not a stuck runner (BUG-15)."""
        from backtest.forward.options_bridge import _DETERMINISTIC_ENTRY_REJECTIONS

        assert "MAX_LOSS_EXCEEDED" in _DETERMINISTIC_ENTRY_REJECTIONS
        assert "MARK_STALE" not in _DETERMINISTIC_ENTRY_REJECTIONS

        bridge = _bridge({"reenter": True})
        bridge._last_attempted_contract = "AAA"
        bridge._register_entry_rejection("MAX_LOSS_EXCEEDED", "x")
        bridge._register_entry_rejection("MAX_LOSS_EXCEEDED", "x")
        assert bridge._rejection_streak == 2

        def _blip(*args, **kwargs):
            raise ValueError("quote feed blipped")

        bridge._execute = _blip
        view = SimpleNamespace(
            direction="BULLISH",
            confidence=0.9,
            underlying="NIFTY",
            spot_price=Decimal("24800"),
            bar_timestamp="2026-09-11T09:15:00",
        )
        result = bridge.on_intent(
            {"structure": "long_call", "strike_selection": "atm", "quantity": 1},
            view,
            "s",
        )
        assert result["rejected"] is True
        assert result["code"] not in _DETERMINISTIC_ENTRY_REJECTIONS
        assert bridge._rejection_streak == 0
        assert bridge.trade_health == "OK"

    def test_the_runner_stays_quiet_on_a_throttled_refusal(self):
        """The signal log is what multiplied 205 refusals into 11,410 rows."""
        runner = _strangle_runner("HEALTH-1", max_reentries_per_day=2)
        bridge = runner.options_bridge
        runner._log_signal = MagicMock()
        bridge.on_intent = MagicMock(
            return_value={
                "rejected": True,
                "reason": "MAX_LOSS_EXCEEDED: over the ceiling",
                "suppress_log": True,
            }
        )

        runner._process_option_tick_entry("NIFTY", 24500.0, "2026-10-26T04:50:00+00:00")
        runner._log_signal.assert_not_called()

        bridge.on_intent.return_value = {
            "rejected": True,
            "reason": "MAX_LOSS_EXCEEDED: over the ceiling",
        }
        runner._process_option_tick_entry("NIFTY", 24500.0, "2026-10-26T04:50:01+00:00")
        assert runner._log_signal.call_count == 1

    def test_health_is_mirrored_onto_the_runner_row(self):
        runner = _strangle_runner("HEALTH-2", max_reentries_per_day=2)
        state = runner.get_state()
        assert state["trade_health"] == "OK"
        assert state["block_reason"] is None
        runner.options_bridge.trade_health = "BLOCKED_BY_RISK"
        runner.options_bridge.block_reason = "MAX_LOSS_EXCEEDED×3 @ NIFTY"
        assert runner.get_state()["trade_health"] == "BLOCKED_BY_RISK"
        assert runner.get_state()["block_reason"] == "MAX_LOSS_EXCEEDED×3 @ NIFTY"
