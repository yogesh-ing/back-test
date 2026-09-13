"""Options bridge for the forward engine (Gap-Analysis G3.2).

Connects a strategy's :class:`~backtest.strategy.intent.MarketView` to the
options expression layer so an automated runner can trade structures — the
piece that was missing between ``DirectionalOptions.generate_market_view()``
(G3.1) and a live paper book:

    strategy view  →  bridge  →  chain + selector + structure
                              →  OptionPaperBroker.execute_structure()

Each runner owns an isolated bridge (its own
:class:`~backtest.options.paper_trading.OptionPaperBroker` and synthetic
quote feed), mirroring how equity runners get isolated portfolios, so
option and equity flows coexist without touching each other's books.

Expression config (``RunnerConfig.instrument["expression"]``)::

    {
        "type": "bull_call_spread",          # or {"BULLISH": ..., "BEARISH": ...}
        "strike_selection": "atm" | "delta",
        "delta_target": 0.35,
        "quantity": 1,                        # lots per leg
    }

V1 policy: one open structure per bridge — while a structure is open the
bridge ignores new views (no pyramiding, no flip-to-opposite). Exits come
from the dashboard / expiry pipeline, keeping the runner loop side-effect
light.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Optional

from backtest.options.expiry_policy import NearestExpiryPolicy
from backtest.options.paper_trading import OptionPaperBroker
from backtest.options.quote_providers import (
    SyntheticChainGenerator,
    SyntheticQuoteProvider,
)
from backtest.options.selector import ATMSelector, DeltaSelector
from backtest.options.structures import (
    BearPutSpread,
    BullCallSpread,
    LongCall,
    LongPut,
)
from backtest.strategy.intent import Direction, MarketView

logger = logging.getLogger("backtest.forward.options_bridge")

#: structure_type → (structure instance, option side of the chain)
STRUCTURES: dict[str, tuple[Any, str]] = {
    "long_call": (LongCall(), "CE"),
    "long_put": (LongPut(), "PE"),
    "bull_call_spread": (BullCallSpread(), "CE"),
    "bear_put_spread": (BearPutSpread(), "PE"),
}

#: One- vs two-strike structures.
_STRICKES_NEEDED = {"long_call": 1, "long_put": 1, "bull_call_spread": 2, "bear_put_spread": 2}

#: Default expression: direction-aware spreads, ATM strikes, one lot.
DEFAULT_EXPRESSION: dict[str, Any] = {
    "type": {"BULLISH": "bull_call_spread", "BEARISH": "bear_put_spread"},
    "strike_selection": "atm",
    "quantity": 1,
}


class OptionsBridge:
    """Turns market views into executed option structures on a paper book.

    Parameters
    ----------
    capital:
        Starting capital for the isolated option paper book.
    expression:
        Structure selection config (see module docstring). ``None`` gives
        the direction-aware default.
    option_broker / quote_provider:
        Injection points for tests; default to a fresh paper book and a
        synthetic Black-Scholes feed.
    """

    def __init__(
        self,
        capital: float,
        expression: Optional[dict[str, Any]] = None,
        option_broker: Optional[OptionPaperBroker] = None,
        quote_provider: Optional[Any] = None,
    ) -> None:
        self.expression: dict[str, Any] = {**DEFAULT_EXPRESSION, **(expression or {})}
        self.option_broker = option_broker or OptionPaperBroker(capital=float(capital))
        self.quote_provider: Any = quote_provider or SyntheticQuoteProvider()
        if getattr(self.quote_provider, "generator", None) is None:
            self.quote_provider.generator = SyntheticChainGenerator()
        self.open_structure_id: Optional[str] = None
        self.executed_count: int = 0

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def on_market_view(self, view: MarketView | None, strategy_name: str) -> dict[str, Any] | None:
        """Consume one strategy view; execute a structure if one is warranted.

        Returns a summary dict when a structure was opened, ``None`` when
        the view produced no trade (no conviction, one already open, or a
        soft rejection).
        """
        if view is None:
            return None
        if self._has_open_structure():
            return None  # V1: one structure at a time per runner

        structure_type = self._structure_type_for(view)
        if structure_type not in STRUCTURES:
            logger.warning("[options-bridge] unknown structure type %r", structure_type)
            return None

        try:
            return self._execute(view, structure_type, strategy_name)
        except Exception as exc:  # noqa: BLE001 — a bad bar must not kill the runner
            logger.warning(
                "[options-bridge] %s execution failed: %s", structure_type, exc
            )
            return {"rejected": True, "reason": str(exc)}

    def summary(self) -> dict[str, Any]:
        """Compact book state for runner state payloads and tests."""
        broker = self.option_broker
        return {
            "open_positions": len(broker.get_open_positions()),
            "open_structures": len(broker.get_open_structures()),
            "executed_count": self.executed_count,
            "capital": float(broker.capital),
            "equity": float(broker.total_equity),
            "realized_pnl": float(broker.total_realized_pnl),
        }

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _has_open_structure(self) -> bool:
        if self.open_structure_id is None:
            return False
        structure = self.option_broker.get_structure(self.open_structure_id)
        if structure is None or not structure.is_open:
            self.open_structure_id = None
            return False
        return True

    def _structure_type_for(self, view: MarketView) -> str:
        type_cfg = self.expression.get("type", DEFAULT_EXPRESSION["type"])
        if isinstance(type_cfg, dict):
            if view.direction == Direction.BEARISH:
                return str(type_cfg.get("BEARISH", "bear_put_spread"))
            return str(type_cfg.get("BULLISH", "bull_call_spread"))
        return str(type_cfg)

    def _generator(self) -> SyntheticChainGenerator:
        generator = getattr(self.quote_provider, "generator", None) or getattr(
            getattr(self.quote_provider, "inner", None), "generator", None
        )
        if generator is None:
            generator = SyntheticChainGenerator()
            self.quote_provider.generator = generator
        return generator

    def _execute(
        self, view: MarketView, structure_type: str, strategy_name: str
    ) -> dict[str, Any]:
        from backtest.options.paper_trading import InsufficientMarginError

        structure, option_type = STRUCTURES[structure_type]
        underlying = str(view.underlying or "NIFTY")
        generator = self._generator()

        # Sync the synthetic market to the view's spot so premiums and
        # strikes line up with what the strategy saw.
        if view.spot_price is not None:
            generator.set_spot(underlying, float(view.spot_price))
        spot = Decimal(str(generator.get_spot(underlying)))

        expiry = NearestExpiryPolicy().select_expiry(generator.available_expiries(underlying))
        if expiry is None:
            raise ValueError(f"no available expiry for {underlying}")
        chain = generator.generate_chain(underlying, expiry=expiry, option_type=option_type)
        if hasattr(self.quote_provider, "register_chain"):
            self.quote_provider.register_chain(chain)

        strikes = self._pick_strikes(structure_type, spot, chain, view.direction)
        if not strikes:
            raise ValueError("no suitable strike found in the chain")

        intent = structure.build(view, strikes, chain, expiry, strategy_name=strategy_name)
        quantity = int(self.expression.get("quantity", 1) or 1)
        if quantity > 1:
            intent = self._scale_intent(intent, quantity)

        try:
            positions = self.option_broker.execute_structure(intent, self.quote_provider)
        except InsufficientMarginError as exc:
            return {"rejected": True, "reason": str(exc)}

        self.open_structure_id = positions[0].structure_id if positions else None
        self.executed_count += 1
        logger.info(
            "[options-bridge] opened %s on %s strikes=%s expiry=%s (view %s conf=%.2f)",
            structure_type,
            underlying,
            [str(s) for s in strikes],
            expiry,
            view.direction.value,
            float(view.confidence),
        )
        return {
            "structure_id": self.open_structure_id,
            "structure_type": structure_type,
            "underlying": underlying,
            "strikes": [str(s) for s in strikes],
            "expiry": str(expiry),
            "positions": len(positions),
            "direction": view.direction.value,
        }

    def _pick_strikes(
        self,
        structure_type: str,
        spot: Decimal,
        chain: dict[Decimal, Any],
        direction: Direction,
    ) -> list[Decimal]:
        strikes_sorted = sorted(chain.keys())
        selection = str(self.expression.get("strike_selection", "atm")).lower()
        if selection == "delta":
            target = float(self.expression.get("delta_target", 0.35))
            selector: Any = DeltaSelector(delta_target=target)
        else:
            selector = ATMSelector()

        count = _STRICKES_NEEDED.get(structure_type, 1)
        if count == 1:
            picked = selector.pick_strike(spot, strikes_sorted, direction)
            return [picked] if picked is not None else []
        return selector.pick_strikes(spot, strikes_sorted, direction, count=count)

    @staticmethod
    def _scale_intent(intent: Any, quantity: int) -> Any:
        from backtest.strategy.intent import OptionLeg, TradeIntent

        legs = tuple(
            OptionLeg(
                instrument_token=leg.instrument_token,
                trading_symbol=leg.trading_symbol,
                side=leg.side,
                quantity=leg.quantity * quantity,
                lot_size=leg.lot_size,
            )
            for leg in intent.legs
        )
        return TradeIntent(
            view=intent.view,
            structure_type=intent.structure_type,
            legs=legs,
            expiry=intent.expiry,
            strategy_name=intent.strategy_name,
            metadata=intent.metadata,
        )
