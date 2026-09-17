"""U2.4 — Strategy adapter for the execution engine

Plug-and-play contract from user's point 3:
  Strategy triggers signal (option strike info or equity instrument/price)
  and execution engine executes checking live/paper.

`generate_market_view` already emits a MarketView → UnifiedSignal.option_view.
This module adds a thin adapter so any equity strategy (`generate_signals`)
can also feed the engine with normalized {direction, instrument_hint, confidence}
— the plug-and-play contract. Options vs swing is decided by playbook/runner
type, not by strategy code.

Architecture:
- Strategy owns WHAT (direction, confidence, underlying, instrument_hint)
- Engine owns HOW (quote source, lot_size, risk, routing)
- C2: Strategies NEVER call broker/quote APIs — engine feeds bars + chain snapshots

Usage:
    from backtest.engine.strategy_adapter import StrategyAdapter, adapt_strategy

    adapter = StrategyAdapter()
    signal = adapter.adapt(strategy, candles, underlying="NIFTY")
    result = engine.execute(signal, playbook, runner_config, mode, source)

    # Or functional:
    signal = adapt_strategy(strategy, candles, underlying="NIFTY")
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, List, Optional

import pandas as pd

from backtest.strategy.intent import Direction, MarketView
from backtest.strategy.signal import UnifiedSignal

logger = logging.getLogger("backtest.engine.strategy_adapter")


class StrategyAdapter:
    """Thin adapter: any strategy → UnifiedSignal (option_view or equity_entry).

    - If strategy has generate_market_view → use it directly → option_view signal
    - Else if has generate_signals → convert last signal to direction → option_view + equity_entry
    - Else if has entries/exits → convert via generate_signals → same

    The resulting UnifiedSignal carries:
      direction: BULLISH/BEARISH/NEUTRAL
      confidence: 0.0-1.0
      underlying: instrument_hint (e.g. NIFTY, RELIANCE)
      spot_price: last close
      instrument_hint / equity_info for equity path
      market_view for option path

    Options vs swing is decided by playbook/runner type, not by strategy code —
    the same signal can be routed to either path.
    """

    def __init__(self, default_underlying: str = "NIFTY") -> None:
        self.default_underlying = default_underlying

    def adapt(
        self,
        strategy: Any,
        candles: pd.DataFrame,
        underlying: Optional[str] = None,
        chain_snapshot: Optional[Any] = None,
        strategy_name: Optional[str] = None,
    ) -> Optional[UnifiedSignal]:
        """Adapt any strategy to UnifiedSignal.

        Parameters
        ----------
        strategy: Strategy instance with generate_market_view or generate_signals
        candles: OHLCV DataFrame
        underlying: Override underlying (else from strategy params or default)
        chain_snapshot: Optional chain snapshot for validation (engine owns feed)
        strategy_name: For logging/audit

        Returns
        -------
        UnifiedSignal or None (no conviction)
        """
        underlying = underlying or self._resolve_underlying(strategy)
        strat_name = strategy_name or getattr(strategy, "name", "unknown")

        # Try generate_market_view first (options-native)
        mv = self._try_market_view(strategy, candles)
        if mv is not None:
            # Normalize MarketView → UnifiedSignal.option_view
            return self._market_view_to_signal(mv, underlying, strat_name, candles)

        # Fall back to generate_signals (equity or generic)
        sig_series = self._try_generate_signals(strategy, candles)
        if sig_series is None:
            logger.debug("Strategy %s produced no signals", strat_name)
            return None

        return self._signals_to_unified(sig_series, candles, underlying, strat_name, chain_snapshot)

    def adapt_many(
        self,
        strategy: Any,
        candles: pd.DataFrame,
        underlying: Optional[str] = None,
        chain_snapshot: Optional[Any] = None,
        strategy_name: Optional[str] = None,
    ) -> List[UnifiedSignal]:
        """Adapt strategy to list of signals (one per bar) — for backtest loops."""
        underlying = underlying or self._resolve_underlying(strategy)
        strat_name = strategy_name or getattr(strategy, "name", "unknown")

        sig_series = self._try_generate_signals(strategy, candles)
        if sig_series is None:
            return []

        signals: List[UnifiedSignal] = []
        for idx in range(len(candles)):
            # Slice up to idx inclusive for view generation
            sub_candles = candles.iloc[: idx + 1]
            sub_series = sig_series.iloc[: idx + 1]
            if sub_series.empty:
                continue
            sig = self._signals_to_unified(
                sub_series, sub_candles, underlying, strat_name, chain_snapshot
            )
            if sig is not None:
                signals.append(sig)
        return signals

    # -- internals --

    def _resolve_underlying(self, strategy: Any) -> str:
        """Resolve underlying from strategy params or default."""
        try:
            # Try params dict
            params = getattr(strategy, "params", {})
            if isinstance(params, dict):
                # Schema form: {"underlying": {"default": "NIFTY"}}
                u = params.get("underlying")
                if isinstance(u, dict):
                    return str(u.get("default", self.default_underlying))
                if isinstance(u, str):
                    return u
                # Instance attribute
                if hasattr(strategy, "underlying"):
                    return str(getattr(strategy, "underlying"))
        except Exception as exc:  # noqa: BLE001 — logged; fall back to the default underlying
            logger.debug(
                "underlying resolution failed for %s: %s",
                getattr(strategy, "name", "unknown"),
                exc,
            )
        return self.default_underlying

    def _try_market_view(self, strategy: Any, candles: pd.DataFrame) -> Optional[MarketView]:
        """Try generate_market_view — returns MarketView or None."""
        if not hasattr(strategy, "generate_market_view"):
            return None
        try:
            mv = strategy.generate_market_view(candles)
            if mv is None:
                return None
            # Must be MarketView-like
            if hasattr(mv, "direction"):
                return mv
            return None
        except NotImplementedError:
            return None
        except Exception as exc:
            logger.debug(
                "generate_market_view failed for %s: %s",
                getattr(strategy, "name", "unknown"),
                exc,
            )
            return None

    def _try_generate_signals(self, strategy: Any, candles: pd.DataFrame) -> Optional[pd.Series]:
        """Try generate_signals or entries model."""
        if hasattr(strategy, "generate_signals"):
            try:
                sig = strategy.generate_signals(candles)
                if isinstance(sig, pd.Series):
                    return sig
                # Some strategies return DataFrame or list
                if isinstance(sig, (list, tuple)):
                    return pd.Series(sig, index=candles.index[: len(sig)])
            except NotImplementedError:
                logger.debug(
                    "generate_signals not implemented for %s — trying entries model",
                    getattr(strategy, "name", "unknown"),
                )
            except Exception as exc:
                logger.debug(
                    "generate_signals failed for %s: %s",
                    getattr(strategy, "name", "unknown"),
                    exc,
                )
                return None

        # Try entries/exits model
        if hasattr(strategy, "entries"):
            try:
                entries = strategy.entries(candles)
                if isinstance(entries, pd.Series):
                    # Convert entries to 1/0 signals
                    return entries.astype(int)
            except Exception as exc:
                logger.debug("entries failed for %s: %s", getattr(strategy, "name", "unknown"), exc)

        return None

    def _market_view_to_signal(
        self,
        mv: MarketView,
        underlying: str,
        strategy_name: str,
        candles: pd.DataFrame,
    ) -> Optional[UnifiedSignal]:
        """Convert MarketView to UnifiedSignal.option_view."""
        if mv.direction == Direction.NEUTRAL:
            return None

        spot = mv.spot_price
        if spot is None or spot == 0:
            # Fallback to last close
            try:
                spot = (
                    Decimal(str(candles["close"].iloc[-1]))
                    if "close" in candles.columns
                    else Decimal("0")
                )
            except Exception:
                spot = Decimal("0")

        return UnifiedSignal.option_view(
            direction=mv.direction,
            confidence=float(mv.confidence) if mv.confidence else 0.8,
            underlying=str(mv.underlying or underlying),
            spot_price=spot,
            metadata=dict(mv.metadata) if hasattr(mv, "metadata") and mv.metadata else {},
            timestamp=mv.bar_timestamp if hasattr(mv, "bar_timestamp") else None,
            strategy_name=strategy_name,
        )

    def _signals_to_unified(
        self,
        sig_series: pd.Series,
        candles: pd.DataFrame,
        underlying: str,
        strategy_name: str,
        chain_snapshot: Optional[Any],
    ) -> Optional[UnifiedSignal]:
        """Convert last signal in series to UnifiedSignal.

        Equity strategy signals: 1 = bullish/long, 0 = flat/neutral, -1 = bearish/short
        We produce BOTH option_view and equity_info so engine can route either way
        based on playbook/runner type (plug-and-play).
        """
        if sig_series.empty:
            return None

        last_sig = sig_series.iloc[-1]
        # Normalize: handle 1/0/-1, True/False, etc.
        try:
            val = int(last_sig) if not isinstance(last_sig, bool) else (1 if last_sig else 0)
        except Exception:
            val = 0

        # Direction mapping
        if val == 1 or val is True:
            direction = Direction.BULLISH
            side = "BUY"
        elif val == -1:
            direction = Direction.BEARISH
            side = "SELL"
        else:
            # 0 or neutral → no trade
            return None

        # Confidence: use absolute value or 0.8 default
        try:
            conf = abs(float(last_sig)) if abs(float(last_sig)) <= 1.0 else 0.8
            conf = max(0.0, min(1.0, conf))
            if conf == 0.0:
                conf = 0.8
        except Exception:
            conf = 0.8

        # Spot price from last close
        try:
            spot = (
                Decimal(str(candles["close"].iloc[-1]))
                if "close" in candles.columns
                else Decimal("0")
            )
        except Exception:
            spot = Decimal("0")

        ts = candles.index[-1] if len(candles.index) > 0 else None

        # Build option_view signal with equity_info attached for dual routing
        # This is the plug-and-play contract: same signal works for options or equity
        # depending on playbook/runner type
        mv = MarketView(
            direction=direction,
            confidence=conf,
            underlying=underlying,
            spot_price=spot,
            bar_timestamp=ts,
            metadata={"source": "equity_adapter", "raw_signal": val},
        )

        # Equity info for equity path
        equity_info = {
            "side": side,
            "quantity": 1,  # Default — engine's risk envelope caps it
            "price": float(spot),
            "instrument_hint": underlying,
        }

        signal = UnifiedSignal.option_view(
            direction=direction,
            confidence=conf,
            underlying=underlying,
            spot_price=spot,
            metadata={"equity_adapter": True, "raw_signal": val, "side": side},
            timestamp=ts,
            strategy_name=strategy_name,
        )
        # Attach equity_info for dual routing
        signal.equity_info = equity_info
        signal.market_view = mv

        return signal


# Functional wrapper for convenience
_default_adapter = StrategyAdapter()


def adapt_strategy(
    strategy: Any,
    candles: pd.DataFrame,
    underlying: Optional[str] = None,
    chain_snapshot: Optional[Any] = None,
    strategy_name: Optional[str] = None,
) -> Optional[UnifiedSignal]:
    """Functional wrapper — adapt any strategy to UnifiedSignal."""
    return _default_adapter.adapt(strategy, candles, underlying, chain_snapshot, strategy_name)


def adapt_strategy_many(
    strategy: Any,
    candles: pd.DataFrame,
    underlying: Optional[str] = None,
    chain_snapshot: Optional[Any] = None,
    strategy_name: Optional[str] = None,
) -> List[UnifiedSignal]:
    """Functional wrapper — adapt strategy to list of signals (one per bar)."""
    return _default_adapter.adapt_many(strategy, candles, underlying, chain_snapshot, strategy_name)
