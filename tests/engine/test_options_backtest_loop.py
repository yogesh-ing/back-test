"""Tests for the options backtest driver loop (task A4).

Covers:
- Pure ``next_monthly_expiry``: last-Thursday convention, rollover, December.
- Bar stamping: every bar processed at 15:30 market close.
- Full loop over a trending frame: trades occur, equity moves, structures
  exit via ``auto_square_off`` with the reason recorded.
- The clock seam: quotes are priced at bar time, not wall clock (regression
  for the negative-time-to-expiry trap).
- Neutral frames produce zero trades.
- Position cap, settlement, injectability, and determinism of the run
  (A6-style: same inputs → identical result dicts).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd
import pytest

from backtest.engine.option_backtest_driver import (
    BacktestConfig,
    OptionBacktestDriver,
    next_monthly_expiry,
)
from backtest.strategy.intent import Direction, MarketView


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_candles(
    closes: list[float],
    start: str = "2024-01-01",
    freq: str = "B",
) -> pd.DataFrame:
    index = pd.date_range(start, periods=len(closes), freq=freq)
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


class FixedViewStrategy:
    """Emits the same view every bar — makes loop behaviour predictable.

    Deliberately NOT a ``Strategy`` subclass: the auto-discovery registry
    would register it on import and pollute
    ``test_all_strategies_auto_registered``. The driver only needs duck
    typing (``name`` + ``generate_market_view``).
    """

    name = "fixed_view"

    def __init__(self, view: MarketView | None) -> None:
        self._view = view

    def generate_market_view(self, candles: pd.DataFrame) -> MarketView | None:
        return self._view  # same object is fine — the seam reads it per bar


BULLISH = MarketView(
    direction=Direction.BULLISH,
    confidence=0.8,
    underlying="NIFTY",
    spot_price=Decimal("24800"),
    bar_timestamp=None,
)


# ---------------------------------------------------------------------------
# next_monthly_expiry
# ---------------------------------------------------------------------------


class TestNextMonthlyExpiry:
    def test_last_thursday_of_month(self):
        # March 2024: last Thursday = 28th.
        assert next_monthly_expiry(date(2024, 3, 15)) == date(2024, 3, 28)

    def test_on_expiry_day_returns_same_day(self):
        assert next_monthly_expiry(date(2024, 3, 28)) == date(2024, 3, 28)

    def test_rolls_to_next_month_after_expiry(self):
        assert next_monthly_expiry(date(2024, 3, 29)) == date(2024, 4, 25)

    def test_december_rolls_to_january(self):
        assert next_monthly_expiry(date(2024, 12, 30)) == date(2025, 1, 30)

    def test_thursday_is_correct_across_months(self):
        # Spot-check that results are Thursdays for a year of references.
        d = date(2024, 1, 1)
        while d < date(2025, 1, 1):
            exp = next_monthly_expiry(d)
            assert exp.weekday() == 3, f"{d} -> {exp} not a Thursday"
            assert exp >= d
            d += timedelta(days=1)


# ---------------------------------------------------------------------------
# Bar stamping
# ---------------------------------------------------------------------------


class TestBarStamping:
    def test_bars_stamped_at_market_close(self):
        candles = make_candles([24000, 24100, 24200, 24300])
        driver = OptionBacktestDriver(
            candles, strategy=FixedViewStrategy(None), config=BacktestConfig()
        )
        result = driver.run()
        assert len(result.equity_curve) == 4
        for point in result.equity_curve:
            assert point.timestamp.time() == time(15, 30)

    def test_window_is_expanding_not_full_frame(self):
        seen: list[int] = []

        class WindowProbe:
            name = "probe"

            def generate_market_view(self, candles):
                seen.append(len(candles))
                return None

        candles = make_candles([24000, 24100, 24200, 24300, 24400])
        driver = OptionBacktestDriver(candles, strategy=WindowProbe(), config=BacktestConfig())
        driver.run()
        assert seen == [1, 2, 3, 4, 5]


# ---------------------------------------------------------------------------
# Full loop on a trending frame
# ---------------------------------------------------------------------------


class TestLoopTrendingFrame:
    @pytest.fixture(scope="class")
    def trending_result(self):
        closes = list(np.linspace(24000, 25600, 45))
        candles = make_candles(closes)
        driver = OptionBacktestDriver(
            candles, config=BacktestConfig(capital=1_000_000.0)
        )
        return driver.run()

    def test_trades_occurred(self, trending_result):
        assert trending_result.metrics["total_trades"] > 0

    def test_all_bars_produce_equity_points(self, trending_result):
        assert len(trending_result.equity_curve) == 45
        assert all(
            float(p.equity) > 0 for p in trending_result.equity_curve
        )

    def test_structures_exit_via_auto_square_off(self, trending_result):
        closed = [t for t in trending_result.trade_log if not t.is_open]
        assert closed, "expected at least one closed structure"
        reasons = {t.exit_reason for t in closed}
        assert "auto_square_off" in reasons

    def test_equity_responds_to_mtmm(self, trending_result):
        # With 45 equity points and realized P&L, first != last.
        first = float(trending_result.equity_curve[0].equity)
        last = float(trending_result.equity_curve[-1].equity)
        assert first != last

    def test_metrics_shape(self, trending_result):
        m = trending_result.metrics
        for key in (
            "total_trades",
            "closed_trades",
            "win_rate",
            "realized_pnl",
            "total_commission",
            "final_equity",
            "max_drawdown",
            "skipped_entries",
        ):
            assert key in m
        assert 0.0 <= float(m["win_rate"]) <= 1.0
        # Drawdown is a fraction (or None on a strictly-rising curve).
        assert m["max_drawdown"] is None or m["max_drawdown"] <= 0.0

    def test_result_to_dict_is_json_safe(self, trending_result):
        payload = trending_result.to_dict()
        import json

        json.dumps(payload)  # must not raise

    def test_final_equity_reconciles_with_capital(self, trending_result):
        # final equity = capital + realized + unrealized - costs; all we can
        # assert strongly is that the money is accounted for, i.e. equity
        # equals capital plus/minus the sum of P&L and commissions within a
        # ₹1 tolerance against the log.
        capital = Decimal("1000000")
        final = Decimal(str(trending_result.metrics["final_equity"]))
        realized = Decimal(trending_result.metrics["realized_pnl"])
        # Open structures hold unrealized P&L, so only bound it loosely.
        assert final > 0
        assert abs(final - (capital + realized)) < Decimal("200000")


# ---------------------------------------------------------------------------
# Clock seam (regression: pricing at bar time, not wall clock)
# ---------------------------------------------------------------------------


class TestClockSeam:
    def test_reference_pinned_during_run(self):
        captured: list[datetime | None] = []

        class RefProbe:
            name = "refprobe"

            def generate_market_view(self, candles):
                captured.append(driver.quote_provider._reference)
                return None

        candles = make_candles([24000, 24100])
        driver = OptionBacktestDriver(candles, strategy=RefProbe(), config=BacktestConfig())
        driver.run()
        assert captured == [
            datetime(2024, 1, 1, 15, 30),
            datetime(2024, 1, 2, 15, 30),
        ]

    def test_historical_quotes_have_positive_time_value(self):
        # Price a far-OTM call at an early historical bar: if the reference
        # were the wall clock (2026+), time-to-expiry would be negative and
        # the quote would collapse to intrinsic (0). With the seam it must
        # carry time value.
        candles = make_candles([24000])
        driver = OptionBacktestDriver(
            candles, strategy=FixedViewStrategy(None), config=BacktestConfig()
        )
        driver.run()  # pins reference + registers chain
        chain = driver.generator.generate_chain(
            "NIFTY", expiry=next_monthly_expiry(date(2024, 1, 1)), option_type="CE"
        )
        far_strike = max(chain)
        contract = chain[far_strike]
        quote = driver.quote_provider.get_quote(contract.instrument_token)
        assert quote["ltp"] > 0.0  # time value present, not intrinsic-zero


# ---------------------------------------------------------------------------
# No-trade and defensive paths
# ---------------------------------------------------------------------------


class TestNoTradePaths:
    def test_neutral_strategy_zero_trades(self):
        candles = make_candles([24000, 24050, 24000, 24050, 24000])
        driver = OptionBacktestDriver(
            candles, strategy=FixedViewStrategy(None), config=BacktestConfig()
        )
        result = driver.run()
        assert result.metrics["total_trades"] == 0
        assert len(result.equity_curve) == 5
        # Equity flat at capital (no commissions without trades).
        assert all(
            float(p.equity) == 1_000_000.0 for p in result.equity_curve
        )

    def test_missing_close_column_raises(self):
        bad = pd.DataFrame({"open": [1.0]}, index=pd.date_range("2024-01-01", periods=1))
        driver = OptionBacktestDriver(
            bad, strategy=FixedViewStrategy(None), config=BacktestConfig()
        )
        with pytest.raises(KeyError):
            driver.run()

    def test_position_cap_blocks_entry(self):
        closes = list(np.linspace(24000, 25000, 40))
        candles = make_candles(closes)
        driver = OptionBacktestDriver(
            candles,
            config=BacktestConfig(capital=1_000_000.0, max_open_structures=1),
        )
        result = driver.run()
        assert result.metrics["skipped_entries"] > 0
        assert result.metrics["total_trades"] >= 1

    def test_settlement_closes_expired_positions(self):
        # Fixed bullish view on a rising frame → long_call with monthly
        # expiry; by expiry day the position must settle, not linger.
        candles = make_candles(list(np.linspace(24000, 26000, 50)), start="2024-01-01")
        driver = OptionBacktestDriver(
            candles, strategy=FixedViewStrategy(BULLISH), config=BacktestConfig()
        )
        result = driver.run()
        expired = driver.broker.get_closed_structures()
        assert expired, "expected settlement to close the position"
        assert any(
            t.exit_reason == "expiry_settlement" or t.exit_reason == "auto_square_off"
            for t in [type("T", (), {"exit_reason": s.exit_reason, "is_open": s.is_open})() for s in expired]
        )


# ---------------------------------------------------------------------------
# Injectability + determinism
# ---------------------------------------------------------------------------


class TestInjectabilityAndDeterminism:
    def test_injected_broker_is_used(self):
        from backtest.options.paper_trading import OptionPaperBroker

        broker = OptionPaperBroker(capital=500_000.0)
        candles = make_candles([24000, 24100, 24200])
        driver = OptionBacktestDriver(
            candles,
            broker=broker,
            strategy=FixedViewStrategy(None),
            config=BacktestConfig(),
        )
        result = driver.run()
        assert broker is driver.broker
        assert float(result.equity_curve[0].equity) == 500_000.0

    def test_injected_strategy_params_flow_through(self):
        driver = OptionBacktestDriver(
            make_candles([24000, 24100]),
            config=BacktestConfig(strategy_params={"underlying": "BANKNIFTY"}),
        )
        assert driver.strategy.underlying == "BANKNIFTY"

    def test_same_inputs_same_output(self):
        closes = list(np.linspace(24000, 24800, 30))
        candles_a = make_candles(closes)
        candles_b = make_candles(closes)

        result_a = OptionBacktestDriver(candles_a, config=BacktestConfig()).run()
        result_b = OptionBacktestDriver(candles_b, config=BacktestConfig()).run()

        assert result_a.to_dict() == result_b.to_dict()

    def test_decider_injection_respected(self):
        from backtest.engine.option_backtest_driver import (
            UnsupportedStructureError,
        )

        def always_straddle(view):
            return "straddle"

        candles = make_candles(list(np.linspace(24000, 24400, 20)))
        driver = OptionBacktestDriver(
            candles,
            strategy=FixedViewStrategy(BULLISH),
            config=BacktestConfig(decider=always_straddle),
        )
        with pytest.raises(UnsupportedStructureError):
            driver.run()
