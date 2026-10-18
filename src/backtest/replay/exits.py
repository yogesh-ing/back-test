"""OHLC-conservative exit resolution for replayed option structures."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from backtest.options.exit_policy import EXIT_STOP_LOSS, EXIT_TAKE_PROFIT, ExitConfig, ExitDecision


@dataclass(frozen=True)
class ReplayExitResolution:
    """One replay exit decision and the candle prices used to close its legs."""

    decision: ExitDecision
    prices: dict[str, float]
    pnl_at_open: Decimal
    pnl_adverse: Decimal
    pnl_favorable: Decimal


class OHLCConservativeExitResolver:
    """Resolve stop/target touches from option-leg OHLC candles.

    * Stop/target thresholds are tested against aggregate per-leg OHLC ranges.
    * When both levels are touched in one candle, stop wins.
    * A stop gapped through at the candle open exits at that open; otherwise
      the stop uses each leg's adverse candle extreme (conservative).
    * A target gapped through exits at the open. An intrabar target touch uses
      the candle close, not the favorable high/low, avoiding a best-case fill
      fabricated from independent per-leg extremes.

    This is a mechanics rule, not a claim about executable multi-leg prices.
    """

    def resolve(
        self,
        *,
        structure: Any,
        quote_provider: Any,
        basis: Decimal,
        exit_config: ExitConfig,
    ) -> ReplayExitResolution | None:
        legs = [leg for leg in structure.legs if getattr(leg.status, "value", leg.status) == "open"]
        if not legs:
            return None

        pnl_open = Decimal("0")
        pnl_adverse = Decimal("0")
        pnl_favorable = Decimal("0")
        opens: dict[str, float] = {}
        closes: dict[str, float] = {}
        adverse_prices: dict[str, float] = {}

        for leg in legs:
            quote = quote_provider.quote_ohlc(leg.instrument_token)
            open_price = Decimal(str(quote["open"]))
            close_price = Decimal(str(quote["close"]))
            low_price = Decimal(str(quote["low"]))
            high_price = Decimal(str(quote["high"]))
            units = Decimal(str(leg.total_quantity))
            sign = Decimal("1") if leg.is_long else Decimal("-1")
            entry = Decimal(str(leg.entry_price))
            pnl_open += (open_price - entry) * units * sign
            opens[leg.instrument_token] = float(open_price)
            closes[leg.instrument_token] = float(close_price)
            if leg.is_long:
                adverse_price, favorable_price = low_price, high_price
            else:
                adverse_price, favorable_price = high_price, low_price
            pnl_adverse += (adverse_price - entry) * units * sign
            pnl_favorable += (favorable_price - entry) * units * sign
            adverse_prices[leg.instrument_token] = float(adverse_price)

        stop_threshold = self._stop_threshold(exit_config, basis)
        target_threshold = self._target_threshold(exit_config, basis)
        stop_hit = stop_threshold is not None and (
            pnl_open <= stop_threshold or pnl_adverse <= stop_threshold
        )
        target_hit = target_threshold is not None and (
            pnl_open >= target_threshold or pnl_favorable >= target_threshold
        )

        # Risk-first precedence is explicit even when the same candle's range
        # spans both thresholds. The journal detail makes this observable.
        if stop_hit:
            gap = pnl_open <= stop_threshold
            prices = opens if gap else adverse_prices
            detail = (
                f"OHLC stop {'gap-through at open' if gap else 'hit at adverse range'} "
                f"(aggregate open P&L {pnl_open:,.2f}, adverse P&L {pnl_adverse:,.2f})"
            )
            if target_hit:
                detail += "; stop assumed before target (same-candle ambiguity)"
            return ReplayExitResolution(
                ExitDecision(EXIT_STOP_LOSS, detail),
                prices,
                pnl_open,
                pnl_adverse,
                pnl_favorable,
            )

        if target_hit:
            gap = pnl_open >= target_threshold
            prices = opens if gap else closes
            detail = (
                "OHLC target "
                f"{'gap-through at open' if gap else 'touched intrabar; closed at candle close'} "
                f"(aggregate open P&L {pnl_open:,.2f}, favorable P&L {pnl_favorable:,.2f})"
            )
            return ReplayExitResolution(
                ExitDecision(EXIT_TAKE_PROFIT, detail),
                prices,
                pnl_open,
                pnl_adverse,
                pnl_favorable,
            )
        return None

    @staticmethod
    def _stop_threshold(config: ExitConfig, basis: Decimal) -> Decimal | None:
        thresholds: list[Decimal] = []
        if config.stop_loss_points is not None:
            thresholds.append(-Decimal(str(config.stop_loss_points)))
        if config.stop_loss_pct is not None and basis > 0:
            thresholds.append(-basis * Decimal(str(config.stop_loss_pct)))
        # Either configured stop can fire; the closer-to-zero loss is first.
        return max(thresholds) if thresholds else None

    @staticmethod
    def _target_threshold(config: ExitConfig, basis: Decimal) -> Decimal | None:
        thresholds: list[Decimal] = []
        if config.take_profit_points is not None:
            thresholds.append(Decimal(str(config.take_profit_points)))
        if config.take_profit_pct is not None and basis > 0:
            thresholds.append(basis * Decimal(str(config.take_profit_pct)))
        # Either configured target can fire; the nearer gain is first.
        return min(thresholds) if thresholds else None


__all__ = ["OHLCConservativeExitResolver", "ReplayExitResolution"]
