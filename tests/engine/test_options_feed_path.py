"""Phase 2 proof: the driver runs on any OptionPricingFeed (plan V3).

- The feed path trades, settles and tags every result ``model_bs``.
- The legacy path is untouched (its own suite runs unchanged elsewhere).
- Import-graph rule: the driver imports the interface, never a concrete
  feed, ``bs_price`` or ``quote_providers`` — a "temporary" hardcoded price
  call fails this test, not review.
"""

from __future__ import annotations

import ast
from datetime import date
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.engine.option_backtest_driver import (
    BacktestConfig,
    OptionBacktestDriver,
)
from backtest.options.model_pricing import ModelBSFeed, ModelPricingConfig
from backtest.strategy.intent import Direction, MarketView

SRC = Path(__file__).resolve().parents[2] / "src" / "backtest"


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
    """Bullish every bar, spot = the bar's own close (chain ATM tracks it)."""

    name = "last_close_bull"

    def generate_market_view(self, candles: pd.DataFrame) -> MarketView:
        return MarketView(
            direction=Direction.BULLISH,
            confidence=0.8,
            underlying="NIFTY",
            spot_price=Decimal(str(float(candles["close"].iloc[-1]))),
            bar_timestamp=None,
        )


def _feed():
    return ModelBSFeed(
        ModelPricingConfig(iv_source="constant", iv_constant=0.20, iv_premium=0.025)
    )


class TestFeedPath:
    def test_run_trades_and_tags_the_feed(self):
        candles = make_candles(list(np.linspace(24000, 25000, 40)))
        result = OptionBacktestDriver(
            candles,
            strategy=LastCloseBull(),
            config=BacktestConfig(),
            pricing_feed=_feed(),
        ).run()
        assert result.pricing_feed == "model_bs"
        assert result.to_dict()["pricing_feed"] == "model_bs"
        assert result.metrics["total_trades"] >= 1
        assert len(result.equity_curve) == 40

    def test_neutral_frame_zero_trades_flat_equity(self):
        class Neutral:
            name = "neutral"

            def generate_market_view(self, candles):
                return None

        candles = make_candles([24000, 24050, 24000])
        result = OptionBacktestDriver(
            candles, strategy=Neutral(), pricing_feed=_feed()
        ).run()
        assert result.metrics["total_trades"] == 0
        assert all(float(p.equity) == 1_000_000.0 for p in result.equity_curve)

    def test_expiry_settlement_closes_positions(self):
        candles = make_candles(list(np.linspace(24000, 26000, 50)))
        driver = OptionBacktestDriver(
            candles,
            strategy=LastCloseBull(),
            config=BacktestConfig(),
            pricing_feed=_feed(),
        )
        driver.run()
        closed = driver.broker.get_closed_structures()
        assert closed, "expected expiry settlement to close the position"
        assert any(
            s.exit_reason in ("expiry_settlement", "auto_square_off") for s in closed
        )

    def test_same_inputs_same_output(self):
        closes = list(np.linspace(24000, 24800, 30))
        a = OptionBacktestDriver(
            make_candles(closes), strategy=LastCloseBull(), pricing_feed=_feed()
        ).run()
        b = OptionBacktestDriver(
            make_candles(closes), strategy=LastCloseBull(), pricing_feed=_feed()
        ).run()
        assert a.to_dict() == b.to_dict()

    def test_quotes_carry_the_feed_tag(self):
        candles = make_candles([24000])
        driver = OptionBacktestDriver(
            candles, strategy=LastCloseBull(), pricing_feed=_feed()
        )
        driver.run()
        chain = driver.quote_provider.generate_chain(
            "NIFTY", expiry=date(2024, 1, 25), option_type="CE"
        )
        contract = chain[Decimal("24000")]
        quote = driver.quote_provider.get_quote(contract.instrument_token)
        assert quote["pricing_feed"] == "model_bs"
        assert quote["model"] is True
        assert quote["ask"] >= quote["ltp"] >= quote["bid"] > 0

    def test_legacy_result_still_tags_its_feed(self):
        candles = make_candles([24000, 24050])

        class Neutral:
            name = "neutral"

            def generate_market_view(self, candles):
                return None

        result = OptionBacktestDriver(candles, strategy=Neutral()).run()
        assert result.pricing_feed == "synthetic:bs"


class TestImportGraph:
    """V3 §3 rule 1, mechanically enforced."""

    FORBIDDEN_MODULES = (
        "backtest.options.quote_providers",
        "backtest.options.model_pricing",
    )
    FORBIDDEN_NAMES = ("bs_price", "ModelBSFeed", "SyntheticQuoteProvider",
                       "SyntheticChainGenerator")

    def _imports(self, path: Path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    yield alias.name, alias.name
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    yield module, alias.name

    def test_driver_imports_only_the_interface(self):
        for module, name in self._imports(SRC / "engine" / "option_backtest_driver.py"):
            assert module not in self.FORBIDDEN_MODULES, (
                f"driver imports {module} — pricing must go through OptionPricingFeed"
            )
            assert name not in self.FORBIDDEN_NAMES, (
                f"driver imports {name} — concrete pricing leaked into the driver"
            )

    def test_adapter_holds_no_pricing_math(self):
        for module, name in self._imports(SRC / "options" / "feed_adapter.py"):
            assert module != "backtest.options.model_pricing"
            assert name not in ("bs_price", "ModelBSFeed", "BlackScholes")

    def test_scanner_is_not_vacuous(self):
        # synthetic_wiring DOES import quote_providers — the scanner must
        # see it, otherwise the driver test above proves nothing.
        hits = [
            module
            for module, _ in self._imports(SRC / "options" / "synthetic_wiring.py")
            if module in self.FORBIDDEN_MODULES
        ]
        assert hits, "scanner failed to flag a known forbidden import"