"""Phase 2 proof: engine-tier exits + margin-aware metrics (plan V3).

- Stop-loss / take-profit fire at bar close on net structure premium, with
  the reason recorded on the trade log.
- Signal flip closes a directional structure when the view reverses.
- All exit knobs are opt-in: defaults reproduce the legacy hold behaviour.
- Metrics expose return-on-premium AND return-on-margin (net of fees).
- The greeks curve is captured on the feed path and empty on legacy.
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from backtest.engine.option_backtest_driver import (
    BacktestConfig,
    OptionBacktestDriver,
)
from backtest.options.model_pricing import ModelBSFeed, ModelPricingConfig
from backtest.strategy.intent import Direction, MarketView


def make_candles(closes, start="2024-01-01"):
    index = pd.date_range(start, periods=len(closes), freq="B")
    close = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {
            "open": close * 0.999,
            "high": close * 1.001,
            "low": close * 0.998,
            "close": close,
            "volume": 100_000,
        },
        index=index,
    )


class LastCloseBull:
    name = "last_close_bull"

    def generate_market_view(self, candles: pd.DataFrame) -> MarketView:
        return MarketView(
            direction=Direction.BULLISH,
            confidence=0.8,
            underlying="NIFTY",
            spot_price=Decimal(str(float(candles["close"].iloc[-1]))),
            bar_timestamp=None,
        )


class FlipAfter:
    """Bullish for the first ``n`` bars, bearish afterwards."""

    name = "flip_after"

    def __init__(self, n: int) -> None:
        self.n = n

    def generate_market_view(self, candles: pd.DataFrame) -> MarketView:
        direction = (
            Direction.BULLISH if len(candles) <= self.n else Direction.BEARISH
        )
        return MarketView(
            direction=direction,
            confidence=0.8,
            underlying="NIFTY",
            spot_price=Decimal(str(float(candles["close"].iloc[-1]))),
            bar_timestamp=None,
        )


def _feed():
    return ModelBSFeed(
        ModelPricingConfig(iv_source="constant", iv_constant=0.20, iv_premium=0.025)
    )


def _run(closes, config=None, strategy=None, feed=True):
    driver = OptionBacktestDriver(
        make_candles(closes),
        strategy=strategy or LastCloseBull(),
        config=config or BacktestConfig(),
        pricing_feed=_feed() if feed else None,
    )
    return driver, driver.run()


FALLING = list(np.linspace(24000, 23000, 30))
RISING = list(np.linspace(24000, 25000, 30))


class TestStopLoss:
    def test_stop_fires_and_records_reason(self):
        _, result = _run(FALLING, BacktestConfig(stop_loss_pct=0.10))
        stopped = [t for t in result.trade_log if t.exit_reason == "stop_loss"]
        assert stopped, "expected a stop_loss exit on a falling frame"
        for trade in stopped:
            assert not trade.is_open
            assert trade.realized_pnl < 0

    def test_stop_never_exceeds_defined_risk(self):
        # A long call's max loss is the debit. On daily bars a gap through
        # the stop (e.g. into expiry) can still ride to a full premium loss
        # — intrabar slippage is not modelled — but never beyond the debit.
        _, result = _run(FALLING, BacktestConfig(stop_loss_pct=0.10))
        stopped = [t for t in result.trade_log if t.exit_reason == "stop_loss"]
        assert stopped
        for trade in stopped:
            basis = abs(trade.net_entry_cost)
            assert trade.realized_pnl >= -basis

    def test_stop_works_on_legacy_path_too(self):
        _, result = _run(FALLING, BacktestConfig(stop_loss_pct=0.10), feed=False)
        assert any(t.exit_reason == "stop_loss" for t in result.trade_log)


class TestTakeProfit:
    def test_target_fires_and_records_reason(self):
        # 2% of premium: reachable on this grind before monthly theta and
        # expiry outrun the gain (a 20% target never fills on this frame).
        _, result = _run(RISING, BacktestConfig(take_profit_pct=0.02))
        took = [t for t in result.trade_log if t.exit_reason == "take_profit"]
        assert took, "expected a take_profit exit on a rising frame"
        for trade in took:
            assert not trade.is_open
            assert trade.realized_pnl > 0

    def test_target_produces_more_closed_trades_than_hold(self):
        _, with_tp = _run(RISING, BacktestConfig(take_profit_pct=0.02))
        _, without = _run(RISING, BacktestConfig())
        assert with_tp.metrics["closed_trades"] > without.metrics["closed_trades"]


class TestSignalFlip:
    def test_flip_closes_the_directional_structure(self):
        closes = list(np.linspace(24000, 24400, 10)) + list(
            np.linspace(24400, 24000, 10)
        )
        _, result = _run(
            closes,
            BacktestConfig(exit_on_signal_flip=True),
            strategy=FlipAfter(10),
        )
        flipped = [t for t in result.trade_log if t.exit_reason == "signal_flip"]
        assert flipped, "expected a signal_flip exit when the view reverses"
        assert all(t.structure_type in ("long_call", "bull_call_spread") for t in flipped)

    def test_neutral_view_is_not_a_flip(self):
        class BullThenNeutral:
            name = "bull_then_neutral"

            def generate_market_view(self, candles):
                if len(candles) > 5:
                    return None
                return MarketView(
                    direction=Direction.BULLISH,
                    confidence=0.8,
                    underlying="NIFTY",
                    spot_price=Decimal(str(float(candles["close"].iloc[-1]))),
                    bar_timestamp=None,
                )

        _, result = _run(
            RISING[:15],
            BacktestConfig(exit_on_signal_flip=True),
            strategy=BullThenNeutral(),
        )
        assert not any(t.exit_reason == "signal_flip" for t in result.trade_log)


class TestOptIn:
    def test_defaults_never_produce_engine_exits(self):
        # Same frames that trigger every exit type when enabled.
        for closes, strategy in ((FALLING, None), (RISING, None),
                                 (RISING[:10] + FALLING[:10], FlipAfter(10))):
            _, result = _run(closes, BacktestConfig(), strategy=strategy)
            assert not any(
                t.exit_reason in ("stop_loss", "take_profit", "signal_flip")
                for t in result.trade_log
            )

    def test_explicit_off_matches_default_run(self):
        off = BacktestConfig(
            stop_loss_pct=None, take_profit_pct=None, exit_on_signal_flip=False
        )
        _, a = _run(RISING, BacktestConfig())
        _, b = _run(RISING, off)
        assert a.to_dict() == b.to_dict()

    def test_determinism_with_exits_enabled(self):
        cfg = BacktestConfig(stop_loss_pct=0.10, take_profit_pct=0.02,
                             exit_on_signal_flip=True)
        _, a = _run(RISING, cfg)
        _, b = _run(RISING, cfg)
        assert a.to_dict() == b.to_dict()


class TestMarginMetrics:
    def test_returns_are_net_and_consistent(self):
        driver, result = _run(RISING, BacktestConfig(take_profit_pct=0.02))
        closed = driver.broker.get_closed_structures()
        assert closed
        net = sum((s.net_pnl for s in closed), Decimal("0"))
        premium = sum((abs(s.total_entry_cost) for s in closed), Decimal("0"))
        m = result.metrics
        assert Decimal(m["net_realized_pnl"]) == net
        assert Decimal(m["premium_deployed"]) == premium
        assert m["return_on_premium"] == pytest.approx(float(net / premium))
        peak = Decimal(m["peak_margin_used"])
        assert peak > 0
        assert m["return_on_margin"] == pytest.approx(float(net / peak))

    def test_net_is_gross_minus_commission_and_fees(self):
        # Identity holds whatever the broker charges. The driver's broker
        # has no fee_calculator attached (statutory stack wiring is a
        # Phase 3 decision — the forward path double-counts brokerage),
        # so fees are 0 here and net = gross - commission.
        driver, result = _run(RISING, BacktestConfig(take_profit_pct=0.02))
        closed = driver.broker.get_closed_structures()
        gross = sum((s.total_realized_pnl for s in closed), Decimal("0"))
        commission = sum((s.total_commission for s in closed), Decimal("0"))
        fees = sum((s.statutory_fees for s in closed), Decimal("0"))
        assert Decimal(result.metrics["net_realized_pnl"]) == gross - commission - fees
        assert commission > 0

    def test_no_trades_no_returns(self):
        class Neutral:
            name = "neutral"

            def generate_market_view(self, candles):
                return None

        _, result = _run(RISING[:5], strategy=Neutral())
        m = result.metrics
        assert m["return_on_premium"] is None
        assert m["return_on_margin"] is None
        assert Decimal(m["peak_margin_used"]) == 0

    def test_spread_margin_is_recorded(self):
        class MildBull(LastCloseBull):
            name = "mild_bull"

            def generate_market_view(self, candles):
                view = super().generate_market_view(candles)
                return MarketView(
                    direction=view.direction,
                    confidence=0.5,  # below 0.7 → bull_call_spread
                    underlying=view.underlying,
                    spot_price=view.spot_price,
                    bar_timestamp=None,
                )

        driver, result = _run(RISING, strategy=MildBull())
        assert any(t.structure_type == "bull_call_spread" for t in result.trade_log)
        assert Decimal(result.metrics["peak_margin_used"]) > 0
        assert result.metrics["return_on_margin"] is not None


class TestGreeksCurve:
    def test_feed_path_captures_signed_greeks(self):
        _, result = _run(RISING)
        assert result.greeks_curve, "feed path must capture a greeks curve"
        assert len(result.greeks_curve) <= len(result.equity_curve)
        for point in result.greeks_curve:
            assert set(point) == {"timestamp", "delta", "gamma", "theta", "vega"}
        # Long calls: positive delta/gamma/vega, negative theta.
        holding = [p for p in result.greeks_curve if p["delta"] != 0]
        assert holding
        for point in holding:
            assert point["delta"] > 0
            assert point["gamma"] > 0
            assert point["theta"] < 0
            assert point["vega"] > 0

    def test_legacy_path_curve_is_empty(self):
        _, result = _run(RISING, feed=False)
        assert result.greeks_curve == []
        assert result.to_dict()["greeks_curve"] == []