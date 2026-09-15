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
from the exit policy / expiry pipeline (see
``docs/OPTIONS-FORWARD-TESTING.md`` tasks B1–B2), keeping the runner loop
side-effect light.

Per-bar pricing (task A1): the bridge is also the forward loop's **market
clock**. :meth:`OptionsBridge.on_bar` moves the synthetic spot to each bar
close, pins the quote provider's pricing reference to the bar timestamp (so
theta follows the replay clock instead of the wall clock) and marks the book
to market. Without it a runner's legs keep their entry premium forever and
``unrealized_pnl`` stays pinned at zero.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
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
        #: Underlying of the most recent structure — the symbol whose spot
        #: drives pricing (set at entry, defaulted for the first bar).
        self.underlying: str = "NIFTY"
        #: Last mark-to-market book value, for runner heartbeats/UI.
        self.last_unrealized_pnl: Decimal = Decimal("0")
        self.last_spot: Optional[float] = None
        self.last_mtm_ts: Optional[str] = None

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
            "unrealized_pnl": float(self.last_unrealized_pnl),
            "last_spot": self.last_spot,
            "last_mtm_ts": self.last_mtm_ts,
            "quote_source": str(getattr(self.quote_provider, "source_name", "unknown")),
        }

    # ------------------------------------------------------------------ #
    # Per-bar pricing (task A1)
    # ------------------------------------------------------------------ #

    def on_bar(
        self, symbol: str, price: float, ts: Any = None
    ) -> Optional[Decimal]:
        """Mark the option book to market at one bar close.

        Called by :class:`~backtest.forward.paper_runner.StrategyRunner` for
        every closed bar (single **and** pool runners). Three things happen:

        1. the synthetic spot moves to ``price`` so premiums track the
           underlying the strategy just saw;
        2. the quote provider's pricing reference is pinned to ``ts`` — the
           bar's timestamp, not ``datetime.now()`` — so time decay advances at
           replay speed;
        3. every open leg is re-priced and its unrealized P&L recomputed.

        Returns the book's total unrealized P&L, or ``None`` when there is
        nothing open (or the book is empty). Pricing failures are logged and
        swallowed: a bad bar must never kill the runner.
        """
        if not self._has_open_structure():
            return None

        try:
            underlying = str(symbol or self.underlying or "NIFTY").upper()
            self.underlying = underlying
            self._sync_market(underlying, price, ts)
            unrealized = self.option_broker.update_mtm(
                self.quote_provider, self._bar_datetime(ts)
            )
            self.last_unrealized_pnl = Decimal(str(unrealized))
            self.last_spot = float(price)
            self.last_mtm_ts = str(ts) if ts else None
            return self.last_unrealized_pnl
        except Exception as exc:  # noqa: BLE001 — pricing must never kill the runner
            logger.warning(
                "[options-bridge] MTM failed for %s @ %s: %s", symbol, price, exc
            )
            return None

    def _sync_market(self, underlying: str, spot: float, ts: Any = None) -> None:
        """Point the quote feed at a new spot / bar clock.

        ``set_reference`` exists on :class:`SyntheticQuoteProvider` (and wraps
        through ``CachedQuoteProvider.inner``); live providers have no clock
        to pin, so the call is skipped for them.
        """
        generator = self._generator()
        generator.set_spot(underlying, float(spot))
        provider = self.quote_provider
        set_reference = getattr(provider, "set_reference", None)
        if set_reference is None:
            set_reference = getattr(getattr(provider, "inner", None), "set_reference", None)
        if set_reference is not None:
            set_reference(self._bar_datetime(ts))

    @staticmethod
    def _bar_datetime(ts: Any) -> Optional[datetime]:
        """Parse a bar timestamp into a **naive UTC** datetime.

        Naive because ``SyntheticChainGenerator.price_contract`` builds its
        expiry as ``datetime.combine(expiry_date, midnight)`` — mixing a
        tz-aware reference with that would raise. ``None`` (unparseable or
        missing) tells the provider to fall back to the wall clock.
        """
        if not ts:
            return None
        text = str(ts).strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            logger.debug("[options-bridge] unparseable bar timestamp %r", ts)
            return None
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed

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
        self.underlying = underlying

        # Sync the synthetic market to the view's spot so premiums and
        # strikes line up with what the strategy saw. The entry is priced on
        # the view's own bar clock (when it carries one) so the premium is
        # consistent with the MTM that follows on the next bar (A1).
        if view.spot_price is not None:
            self._sync_market(underlying, float(view.spot_price), view.bar_timestamp)
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
