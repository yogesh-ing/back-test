"""Task B1 — exit policy for forward option structures.

Before B1 the bridge could open a structure but never leave one: while it was
open every later view was ignored, so the only exits were a human on the
dashboard (which holds a different book) or the expiry pipeline — and the
expiry pipeline never ran inside a runner. A stop-loss that cannot stop out is
not a forward test.

Covers the rules (stop / target / DTE / time / flip / neutral), their
precedence, `reenter`, the same-bar re-entry guard, the entry-side DTE guard,
and the runner wiring that turns a close into an `OPTION_EXIT` signal.

See ``docs/OPTIONS-FORWARD-TESTING.md`` → task B1.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from backtest.forward.options_bridge import OptionsBridge
from backtest.forward.paper_runner import OrderLedger, RunnerConfig, StrategyRunner
from backtest.options.exit_policy import (
    EXIT_DTE,
    EXIT_SIGNAL_FLIP,
    EXIT_SIGNAL_NEUTRAL,
    EXIT_STOP_LOSS,
    EXIT_TAKE_PROFIT,
    EXIT_TIME_STOP,
    ExitConfig,
    ExitPolicy,
)
from backtest.options.quote_providers import (
    SyntheticChainGenerator,
    SyntheticQuoteProvider,
)
from backtest.strategy.intent import Direction, MarketView

EXPIRY = date(2027, 1, 28)  # far from any bar used here, unless the test says otherwise


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bars(closes, start_day=1):
    """Daily bars from 2026-09-01; day offsets roll into later months."""
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


def _view(direction: Direction, spot: float = 24_800.0) -> MarketView:
    return MarketView(
        direction=direction,
        confidence=0.9,
        underlying="NIFTY",
        spot_price=Decimal(str(spot)),
    )


def _bridge(expression: dict | None = None, capital: float = 1_000_000.0) -> OptionsBridge:
    return OptionsBridge(
        capital=capital,
        expression=expression or {"type": "long_call"},
        quote_provider=SyntheticQuoteProvider(SyntheticChainGenerator()),
    )


def _open(bridge: OptionsBridge, direction: Direction = Direction.BULLISH, spot: float = 24_800.0):
    result = bridge.on_market_view(_view(direction, spot), "directional_options")
    assert result is not None and not result.get("rejected")
    return bridge.option_broker.get_open_structures()[0]


def _runner(exit_cfg: dict, expression_extra: dict | None = None, **overrides):
    expression = {"type": {"BULLISH": "bull_call_spread", "BEARISH": "bear_put_spread"}}
    expression["exit"] = exit_cfg
    expression.update(expression_extra or {})
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


# ---------------------------------------------------------------------------
# ExitConfig plumbing
# ---------------------------------------------------------------------------


class TestExitConfig:
    def test_defaults_are_conservative(self):
        cfg = ExitConfig()
        assert cfg.signal_flip is True
        assert cfg.neutral_bars == 0
        assert cfg.stop_loss_pct is None and cfg.take_profit_pct is None
        assert cfg.min_days_to_expiry == 1  # don't ride into settlement
        assert cfg.reenter is False

    def test_expression_overrides(self):
        cfg = ExitConfig.from_expression(
            {
                "signal_flip": False,
                "neutral_bars": 3,
                "stop_loss_pct": 0.5,
                "take_profit_points": 3000,
                "max_bars": 10,
                "min_days_to_expiry": 2,
                "reenter": True,
            }
        )
        assert cfg.signal_flip is False
        assert cfg.neutral_bars == 3
        assert cfg.stop_loss_pct == 0.5
        assert cfg.take_profit_points == 3000
        assert cfg.max_bars == 10
        assert cfg.min_days_to_expiry == 2
        assert cfg.reenter is True

    def test_omitting_min_days_keeps_the_default(self):
        """Adding *any* exit key must not silently disable the square-off."""
        assert ExitConfig.from_expression({"signal_flip": False}).min_days_to_expiry == 1
        assert ExitConfig.from_expression({"stop_loss_pct": 0.5}).min_days_to_expiry == 1

    def test_zero_min_days_means_square_off_on_expiry_day(self):
        """0 is a real setting (close on expiry day), not "disabled"."""
        assert ExitConfig.from_expression({"min_days_to_expiry": 0}).min_days_to_expiry == 0
        # ...while None explicitly rides into settlement.
        assert ExitConfig.from_expression({"min_days_to_expiry": None}).min_days_to_expiry is None

    def test_garbage_values_degrade_to_disabled(self):
        cfg = ExitConfig.from_expression(
            {"stop_loss_pct": "lots", "take_profit_pct": -1, "max_bars": 0}
        )
        assert cfg.stop_loss_pct is None
        assert cfg.take_profit_pct is None
        assert cfg.max_bars is None

    def test_non_dict_expression_warns_and_defaults(self):
        assert ExitConfig.from_expression("nope") == ExitConfig()

    def test_unknown_keys_are_ignored_not_fatal(self):
        cfg = ExitConfig.from_expression({"stop_loss_pct": 0.5, "typo_key": 1})
        assert cfg.stop_loss_pct == 0.5

    def test_round_trips_to_dict(self):
        cfg = ExitConfig(stop_loss_pct=0.25, reenter=True)
        assert ExitConfig.from_expression(cfg.to_dict()) == cfg


# ---------------------------------------------------------------------------
# Rule evaluation (pure policy)
# ---------------------------------------------------------------------------


class TestPolicyRules:
    policy = ExitPolicy(
        ExitConfig(
            stop_loss_pct=0.5,
            take_profit_pct=1.0,
            max_bars=10,
            min_days_to_expiry=1,
            neutral_bars=2,
        )
    )

    def _evaluate(self, **overrides):
        kwargs = dict(
            view=_view(Direction.BULLISH),
            structure_direction=Direction.BULLISH,
            unrealized_pnl=Decimal("0"),
            basis=Decimal("2000"),
            bars_held=1,
            bars_without_view=0,
            bar_date=date(2026, 9, 10),
            expiry=date(2026, 10, 29),
        )
        kwargs.update(overrides)
        return self.policy.evaluate(**kwargs)

    def test_holds_when_nothing_fires(self):
        assert self._evaluate() is None

    def test_stop_loss_by_percent(self):
        decision = self._evaluate(unrealized_pnl=Decimal("-1000"))
        assert decision.reason == EXIT_STOP_LOSS
        assert "50% of premium" in decision.detail

    def test_take_profit_by_percent(self):
        decision = self._evaluate(unrealized_pnl=Decimal("2000"))
        assert decision.reason == EXIT_TAKE_PROFIT

    def test_percent_rules_skipped_for_credit_structures(self):
        """A % of a negative/zero premium base is meaningless — don't fire."""
        assert self._evaluate(unrealized_pnl=Decimal("-5000"), basis=Decimal("0")) is None

    def test_points_rules(self):
        policy = ExitPolicy(
            ExitConfig(stop_loss_points=1500, take_profit_points=2500, min_days_to_expiry=None)
        )
        assert policy.evaluate(unrealized_pnl=Decimal("-1500")).reason == EXIT_STOP_LOSS
        assert policy.evaluate(unrealized_pnl=Decimal("2500")).reason == EXIT_TAKE_PROFIT
        assert policy.evaluate(unrealized_pnl=Decimal("100")) is None

    def test_time_stop(self):
        assert self._evaluate(bars_held=9) is None
        assert self._evaluate(bars_held=10).reason == EXIT_TIME_STOP

    def test_dte_stop(self):
        decision = self._evaluate(bar_date=date(2026, 10, 28), expiry=date(2026, 10, 29))
        assert decision.reason == EXIT_DTE
        assert "1d to expiry" in decision.detail

    def test_signal_flip(self):
        decision = self._evaluate(view=_view(Direction.BEARISH))
        assert decision.reason == EXIT_SIGNAL_FLIP
        assert "bullish → bearish" in decision.detail

    def test_neutral_view_counts_as_silence(self):
        assert self._evaluate(view=_view(Direction.NEUTRAL), bars_without_view=1) is None
        decision = self._evaluate(view=_view(Direction.NEUTRAL), bars_without_view=2)
        assert decision.reason == EXIT_SIGNAL_NEUTRAL

    def test_no_view_counts_as_silence(self):
        decision = self._evaluate(view=None, bars_without_view=2)
        assert decision.reason == EXIT_SIGNAL_NEUTRAL

    def test_holding_view_does_not_exit(self):
        assert self._evaluate(view=_view(Direction.BULLISH)) is None

    def test_risk_beats_opinion(self):
        """A stop plus a flipped view reports the stop — risk first."""
        decision = self._evaluate(
            view=_view(Direction.BEARISH), unrealized_pnl=Decimal("-1000")
        )
        assert decision.reason == EXIT_STOP_LOSS

    def test_flip_disabled_holds_through_a_reversal(self):
        policy = ExitPolicy(ExitConfig(signal_flip=False, min_days_to_expiry=None))
        assert policy.evaluate(
            view=_view(Direction.BEARISH), structure_direction=Direction.BULLISH
        ) is None


# ---------------------------------------------------------------------------
# Bridge behaviour
# ---------------------------------------------------------------------------


class TestBridgeExits:
    def test_flip_closes_the_structure(self):
        bridge = _bridge({"type": "long_call", "exit": {"min_days_to_expiry": None}})
        _open(bridge)
        assert bridge.open_structure_id is not None

        result = bridge.on_market_view(_view(Direction.BEARISH), "s")

        assert result["exited"] is True
        assert result["reason"] == EXIT_SIGNAL_FLIP
        assert bridge.open_structure_id is None
        assert bridge.closed_count == 1
        assert bridge.option_broker.get_open_structures() == []
        assert bridge.option_broker.get_closed_structures()[0].exit_reason == EXIT_SIGNAL_FLIP

    def test_flip_no_longer_returns_none_silently(self):
        """The pre-B1 behaviour was to ignore the view entirely."""
        bridge = _bridge({"type": "long_call", "exit": {"min_days_to_expiry": None}})
        _open(bridge)
        assert bridge.on_market_view(_view(Direction.BEARISH), "s") is not None

    def test_stop_loss_closes_on_the_bar_hook(self):
        bridge = _bridge(
            {"type": "long_call", "exit": {"stop_loss_pct": 0.2, "min_days_to_expiry": None}}
        )
        _open(bridge)
        bridge.on_bar("NIFTY", 24_800.0, "2026-09-10T09:15:00")

        # Crater the underlying: the long call must stop out.
        bridge.on_bar("NIFTY", 21_000.0, "2026-09-11T09:15:00")

        event = bridge.pop_exit_event()
        assert event is not None and event["exited"] and event["reason"] == EXIT_STOP_LOSS
        assert bridge.pop_exit_event() is None  # drained exactly once
        assert bridge.open_structure_id is None
        assert event["pnl"] < 0

    def test_time_stop_closes_after_n_bars(self):
        bridge = _bridge(
            {"type": "long_call", "exit": {"max_bars": 3, "min_days_to_expiry": None}}
        )
        _open(bridge)
        for i in range(2):
            assert bridge.on_bar("NIFTY", 24_800.0, f"2026-09-{11 + i:02d}T09:15:00") is not None
            assert bridge.pop_exit_event() is None

        bridge.on_bar("NIFTY", 24_800.0, "2026-09-13T09:15:00")
        event = bridge.pop_exit_event()
        assert event["reason"] == EXIT_TIME_STOP
        assert event["bars_held"] == 3

    def test_dte_square_off(self):
        bridge = _bridge({"type": "long_call", "exit": {"min_days_to_expiry": 1}})
        structure = _open(bridge)
        expiry = structure.expiry

        bridge.on_bar("NIFTY", 24_800.0, f"{(expiry - timedelta(days=3)).isoformat()}T09:15:00")
        assert bridge.pop_exit_event() is None  # 3 DTE — still fine

        bridge.on_bar("NIFTY", 24_800.0, f"{(expiry - timedelta(days=1)).isoformat()}T09:15:00")
        event = bridge.pop_exit_event()
        assert event["reason"] == EXIT_DTE
        assert "1d to expiry" in event["detail"]

    def test_neutral_bars_close_the_structure(self):
        bridge = _bridge(
            {"type": "long_call", "exit": {"neutral_bars": 2, "min_days_to_expiry": None}}
        )
        _open(bridge)

        assert bridge.on_market_view(None, "s") is None
        result = bridge.on_market_view(None, "s")
        assert result["exited"] is True
        assert result["reason"] == EXIT_SIGNAL_NEUTRAL

    def test_viewless_bar_keeps_an_open_structure_by_default(self):
        """neutral_bars is opt-in — a quiet strategy is not an exit signal."""
        bridge = _bridge({"type": "long_call", "exit": {"min_days_to_expiry": None}})
        _open(bridge)
        for _ in range(5):
            assert bridge.on_market_view(None, "s") is None
        assert bridge.open_structure_id is not None

    def test_exit_does_not_double_log_signal_exits(self):
        bridge = _bridge({"type": "long_call", "exit": {"min_days_to_expiry": None}})
        _open(bridge)
        bridge.on_market_view(_view(Direction.BEARISH), "s")  # signal exit
        assert bridge.pop_exit_event() is None  # not queued for the bar drain

    def test_reentry_opens_the_reverse_structure(self):
        bridge = _bridge(
            {
                "type": {"BULLISH": "bull_call_spread", "BEARISH": "bear_put_spread"},
                "exit": {"reenter": True, "min_days_to_expiry": None},
            }
        )
        structure = _open(bridge)
        assert structure.structure_type == "bull_call_spread"

        result = bridge.on_market_view(_view(Direction.BEARISH), "s")

        assert result.get("exited") is None  # this is an entry, not an exit
        assert result["structure_type"] == "bear_put_spread"
        assert bridge.closed_count == 1
        assert len(bridge.option_broker.get_open_structures()) == 1
        assert bridge.option_broker.get_open_structures()[0].structure_type == "bear_put_spread"

    def test_no_same_bar_reentry_after_a_stop(self):
        """A stop-out must not immediately re-open the same trade."""
        bridge = _bridge(
            {
                "type": "long_call",
                "exit": {"stop_loss_pct": 0.2, "min_days_to_expiry": None},
            }
        )
        _open(bridge)
        bridge.on_bar("NIFTY", 21_000.0, "2026-09-11T09:15:00")
        assert bridge.pop_exit_event()["reason"] == EXIT_STOP_LOSS

        # Still bullish on the same bar → the bridge must stay flat.
        assert bridge.on_market_view(_view(Direction.BULLISH, 21_000.0), "s") is None
        assert bridge.executed_count == 1

    def test_flat_and_viewless_stays_flat(self):
        bridge = _bridge()
        assert bridge.on_market_view(None, "s") is None
        assert bridge.executed_count == 0

    def test_entry_paused_too_close_to_expiry(self):
        """No fresh structure the expiry rule would close on the next bar."""
        bridge = _bridge({"type": "long_call", "exit": {"min_days_to_expiry": 3}})
        generator = bridge._generator()
        bridge.on_bar("NIFTY", 24_800.0, "2026-09-15T09:15:00")
        expiry = bridge._select_expiry(generator, "NIFTY")
        bridge.on_bar("NIFTY", 24_800.0, f"{(expiry - timedelta(days=1)).isoformat()}T09:15:00")

        result = bridge.on_market_view(_view(Direction.BULLISH), "s")

        assert result is not None and result.get("rejected")
        assert "entries paused" in result["reason"]
        assert bridge.executed_count == 0

    def test_summary_reports_exits_and_policy(self):
        bridge = _bridge({"type": "long_call", "exit": {"max_bars": 2}})
        _open(bridge)
        summary = bridge.summary()
        assert summary["closed_count"] == 0
        assert summary["bars_in_trade"] == 0
        assert summary["exit_policy"]["max_bars"] == 2
        assert summary["last_exit"] is None

        bridge.on_bar("NIFTY", 24_800.0, "2026-09-11T09:15:00")
        bridge.on_bar("NIFTY", 24_800.0, "2026-09-12T09:15:00")

        summary = bridge.summary()
        assert summary["closed_count"] == 1
        assert summary["bars_in_trade"] == 0
        assert summary["last_exit"]["reason"] == EXIT_TIME_STOP


# ---------------------------------------------------------------------------
# Runner wiring: an exit is a first-class event in the runner's log
# ---------------------------------------------------------------------------


class TestExpiryCalendar:
    """The synthetic expiry calendar must survive a year rollover.

    Found while wiring the DTE rule: ``next_monthly_expiry`` used
    ``ref.month + 2``, which raised ``month must be in 1..12`` for a November
    reference and skipped January for a December one. It was unreachable while
    the calendar was pinned to ``date.today()``; the moment the replay clock
    drives it (B1), a forward test replaying into November dies.
    """

    generator = SyntheticChainGenerator()

    def test_november_reference_rolls_into_december(self):
        """Nov's expiry (Nov 26) has passed on Nov 30 → December's, not January's."""
        assert self.generator.next_monthly_expiry(date(2026, 11, 30)) == date(2026, 12, 31)

    def test_december_reference(self):
        assert self.generator.next_monthly_expiry(date(2026, 12, 1)) == date(2026, 12, 31)
        # Expiry day itself is still the nearest expiry...
        assert self.generator.next_monthly_expiry(date(2026, 12, 31)) == date(2026, 12, 31)
        # ...and the next day rolls the year over into January.
        assert self.generator.next_monthly_expiry(date(2027, 1, 1)) == date(2027, 1, 28)

    def test_every_month_of_a_year_is_computable(self):
        for month in range(1, 13):
            for day in (1, 15, 28):
                expiry = self.generator.next_monthly_expiry(date(2026, month, day))
                assert expiry.weekday() == 3  # Thursday
                assert expiry >= date(2026, month, day)

    def test_expiries_follow_the_reference_date(self):
        """A replay reference gets that era's expiries, not the wall clock's."""
        late = self.generator.available_expiries("NIFTY", reference=date(2026, 12, 20))
        assert late == sorted(late)
        assert late[0] == date(2026, 12, 31)
        assert late[1] == date(2027, 1, 28)
        # default = today
        assert self.generator.available_expiries("NIFTY")[0] == self.generator.next_monthly_expiry()


RALLY = [24_800 + i * 40 for i in range(14)]


def _feed_until_first_exit(runner, bars):
    """Feed bars one at a time; return ``(exit_signal, bars_fed)``.

    Asserting at the *exit bar* matters: the runner is free to take the next
    signal afterwards (correct behaviour), so "flat" is only true for that bar
    — the same-bar re-entry guard holds it flat until the next signal.
    """
    for fed, bar in enumerate(bars, start=1):
        runner.process_candle_event("NIFTY", bar)
        found = [s for s in runner.signal_log if s["kind"] == "OPTION_EXIT"]
        if found:
            return found[0], fed
    return None, len(bars)


class TestRunnerExits:
    def test_crash_produces_an_option_exit_signal(self):
        runner = _runner({"stop_loss_pct": 0.3, "min_days_to_expiry": None})
        for bar in _bars(RALLY):
            runner.process_candle_event("NIFTY", bar)
        assert runner.options_summary()["open_structures"] == 1

        exit_signal, _ = _feed_until_first_exit(
            runner, _bars([24_800 - i * 900 for i in range(1, 10)], start_day=15)
        )

        assert exit_signal is not None, "a stopped-out structure must be logged"
        assert exit_signal["kind"] == "OPTION_EXIT"
        assert exit_signal["signal"] == 0  # flat again
        assert "closed after" in exit_signal["reason"]
        assert "-" in exit_signal["reason"]  # the loss is in the message
        # Flat on the exit bar, and the book knows it closed.
        summary = runner.options_summary()
        assert summary["open_structures"] == 0
        assert summary["closed_count"] == 1

    def test_flip_exit_appears_in_the_log(self):
        """Rally opens a bull spread; a gentle slide flips the view out of it."""
        runner = _runner({"min_days_to_expiry": None})
        for bar in _bars(RALLY):
            runner.process_candle_event("NIFTY", bar)

        exit_signal, _ = _feed_until_first_exit(
            runner, _bars([25_400 - i * 120 for i in range(1, 14)], start_day=15)
        )

        assert exit_signal is not None
        assert "flipped" in exit_signal["reason"]
        assert runner.options_summary()["closed_count"] == 1

    def test_stop_reports_the_stop_not_the_flip(self):
        """Risk rules are evaluated first — a gap-down reports the stop."""
        runner = _runner({"stop_loss_pct": 0.3, "min_days_to_expiry": None})
        for bar in _bars(RALLY):
            runner.process_candle_event("NIFTY", bar)

        exit_signal, _ = _feed_until_first_exit(
            runner, _bars([25_400, 20_000.0], start_day=15)
        )

        assert exit_signal is not None and "stop" in exit_signal["reason"]

    def test_runner_can_take_the_next_signal_after_an_exit(self):
        """An exit frees the runner — the opposite view opens the reverse trade.

        The slide stays inside the expiry cycle: a series that reaches the
        expiry would (correctly) settle the reverse structure too, and this
        test is about the hand-off, not about settlement.
        """
        runner = _runner({"min_days_to_expiry": None})
        for bar in _bars(RALLY):
            runner.process_candle_event("NIFTY", bar)

        for bar in _bars([25_400 - i * 120 for i in range(1, 10)], start_day=15):
            runner.process_candle_event("NIFTY", bar)

        summary = runner.options_summary()
        assert summary["closed_count"] == 1
        assert summary["executed_count"] == 2  # bull closed, bear opened
        assert summary["open_structures"] == 1
        assert summary["settled_count"] == 0
        entries = [s for s in runner.signal_log if s["kind"] == "OPTION_ENTRY"]
        assert "bear_put_spread" in entries[-1]["reason"]

    def test_replay_past_the_expiry_keeps_trading(self):
        """Regression: the expiry calendar must follow the bar clock.

        Expiry selection used to be anchored to ``date.today()``, so every bar
        after the wall-clock expiry looked "already expired": the DTE rule
        squared off correctly, then ``entries paused`` forever and the runner
        silently stopped trading. On the bar clock the next month's expiry
        takes over instead.
        """
        runner = _runner({"min_days_to_expiry": 2})
        # 90 daily bars starting 2026-09-01 run well past the September expiry.
        closes = [24_800 + i * 25 for i in range(90)]
        for bar in _bars(closes):
            runner.process_candle_event("NIFTY", bar)

        summary = runner.options_summary()
        assert summary["executed_count"] > 1, "the runner must keep entering trades"
        assert summary["closed_count"] >= 1
        # Every entry it took was on a live expiry, and the last one is later
        # than the expiry the first entry used.
        expiries = [
            s["reason"].split("expiry=")[1].split(" ")[0]
            for s in runner.signal_log
            if s["kind"] == "OPTION_ENTRY"
        ]
        assert expiries == sorted(expiries)
        assert expiries[-1] > expiries[0]

    def test_runner_state_exposes_the_last_exit(self):
        runner = _runner({"max_bars": 3, "min_days_to_expiry": None})
        for bar in _bars(RALLY):
            runner.process_candle_event("NIFTY", bar)
        for bar in _bars([25_400 + i * 40 for i in range(6)], start_day=15):
            runner.process_candle_event("NIFTY", bar)

        state = runner.get_state()
        assert state["options"]["last_exit"]["reason"] == EXIT_TIME_STOP
        assert state["options"]["closed_count"] >= 1
        assert state["options"]["exit_policy"]["max_bars"] == 3

    def test_realized_pnl_moves_on_exit(self):
        """Close → the book's P&L is realized, not unrealized (A2 + B1)."""
        runner = _runner({"stop_loss_pct": 0.3, "min_days_to_expiry": None})
        for bar in _bars(RALLY):
            runner.process_candle_event("NIFTY", bar)

        exit_signal, _ = _feed_until_first_exit(
            runner, _bars([24_800 - i * 900 for i in range(1, 10)], start_day=15)
        )
        assert exit_signal is not None

        # On the exit bar the book is flat and the loss is booked.
        assert runner.unrealized_pnl() == 0.0
        assert runner.realized_pnl < 0
        assert runner.option_pnl() < 0
        assert runner.equity() == pytest.approx(1_000_000 + runner.option_pnl())
        assert runner.get_state()["equity"] == pytest.approx(
            runner.get_state()["options"]["equity"]
        )
