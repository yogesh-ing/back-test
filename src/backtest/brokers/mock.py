"""Mock broker (Gap-PRD G4.3 / P5) — a zero-credential dry-run venue.

The whole options flow becomes testable with NO broker account:

* auth always succeeds (``login``/``verify_totp`` accept anything, including
  empty strings) and the session never expires;
* option chains come from the deterministic
  :class:`~backtest.options.quote_providers.SyntheticChainGenerator`
  (real lot sizes / strike steps, Black-Scholes premiums);
* orders are **logged, never submitted anywhere** — each gets a
  deterministic ``MOCK000001``-style id (monotonic counter, not random:
  A6 determinism rule) and sits OPEN in a local order book. ``poll_fill``
  always answers "still open", so nothing ever fake-fills: a dry run that
  invented fills would be a lie about execution.

Opt-in only: the class is NOT in the default broker registry — a mock
venue must never silently appear in a production login UI. Enable it with
``backtest.brokers.session_manager.enable_mock_broker()`` or by booting
the app with ``--source mock_broker``.
"""

from __future__ import annotations

import itertools
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from backtest.brokers.base import (
    STATUS_AUTHENTICATED,
    STATUS_UNAUTHENTICATED,
    BrokerAuthBase,
    BrokerOrder,
    BrokerOrderBase,
    BrokerOrderId,
    MarginInfo,
)

logger = logging.getLogger(__name__)


class MockBroker(BrokerAuthBase, BrokerOrderBase):
    """Auto-authenticating broker that logs orders instead of placing them."""

    broker_name = "mock"
    broker_display_name = "Mock (dry-run)"

    def __init__(self) -> None:
        self._authenticated = True  # zero-credential by design
        self._orders: list[BrokerOrder] = []
        self._order_seq = itertools.count(1)
        self._generator: Any = None  # lazy — avoids an import cycle at boot

    # -- auth contract (BrokerAuthBase) -----------------------------------

    def login(self, username: str, password: str) -> dict[str, Any]:
        # Never store/echo the credentials — they are not even looked at.
        self._authenticated = True
        return {
            "success": True,
            "message": "mock broker — no credentials required (dry-run)",
            "requires_totp": False,
        }

    def verify_totp(self, totp_code: str) -> dict[str, Any]:
        self._authenticated = True
        return {"success": True, "message": "mock broker — TOTP not required"}

    def get_session_status(self) -> dict[str, Any]:
        if not self._authenticated:
            return {
                "status": STATUS_UNAUTHENTICATED,
                "expires_at": None,
                "broker": self.broker_name,
            }
        expires = datetime.now(timezone.utc) + timedelta(days=365)
        return {
            "status": STATUS_AUTHENTICATED,
            "expires_at": expires.isoformat(),
            "broker": self.broker_name,
        }

    def logout(self) -> None:
        self._authenticated = False
        self._orders.clear()

    def is_authenticated(self) -> bool:
        return self._authenticated

    # -- market data (generator duck type helpers) ------------------------

    def _chain_generator(self) -> Any:
        if self._generator is None:
            from backtest.options.quote_providers import SyntheticChainGenerator

            self._generator = SyntheticChainGenerator()
        return self._generator

    def get_option_chain(self, underlying: str) -> list[Any]:
        """Synthetic CE+PE contracts for the nearest monthly expiry."""
        generator = self._chain_generator()
        contracts: list[Any] = []
        for option_type in ("CE", "PE"):
            chain = generator.generate_chain(underlying, option_type=option_type)
            contracts.extend(chain.values())
        return contracts

    def get_option_quote(self, instrument_token: str) -> dict[str, Any]:
        """Black-Scholes quote for a chain contract; flat default otherwise."""
        generator = self._chain_generator()
        for underlying in generator.spots:
            for option_type in ("CE", "PE"):
                chain = generator.generate_chain(underlying, option_type=option_type)
                for contract in chain.values():
                    if str(contract.instrument_token) == str(instrument_token):
                        ltp = round(generator.price_contract(contract, option_type), 2)
                        return {
                            "ltp": ltp,
                            "bid": max(round(ltp - 0.5, 2), 0.05),
                            "ask": round(ltp + 0.5, 2),
                            "volume": 0,
                            "oi": 0,
                            "mock": True,
                        }
        return {"ltp": 150.0, "bid": 149.5, "ask": 150.5, "volume": 0, "oi": 0, "mock": True}

    # -- order contract (BrokerOrderBase): log, never submit --------------

    def place_order(self, order: BrokerOrder | dict[str, Any]) -> BrokerOrderId:
        """Log the order and file it OPEN locally. Nothing leaves the process."""
        order_id = BrokerOrderId(f"MOCK{next(self._order_seq):06d}")
        if isinstance(order, dict):  # PRD-sketch payload form
            logger.info("[mock-broker] DRY-RUN order (payload): %s -> %s", order, order_id)
            order = BrokerOrder(
                symbol=str(order.get("symbol", order.get("trading_symbol", ""))),
                side=str(order.get("side", "BUY")),
                quantity=int(order.get("quantity", 0) or 0),
                order_type=str(order.get("order_type", "MARKET")),
                limit_price=order.get("limit_price"),
                client_order_id=order.get("client_order_id"),
            )
        else:
            logger.info(
                "[mock-broker] DRY-RUN order: %s %s x%s (%s) -> %s",
                order.side,
                order.symbol,
                order.quantity,
                order.order_type,
                order_id,
            )
        order.broker_order_id = order_id
        order.status = "OPEN"
        order.created_at = datetime.now(timezone.utc).isoformat()
        self._orders.append(order)
        return order_id

    def modify_order(self, order: BrokerOrder) -> None:
        logger.info("[mock-broker] DRY-RUN modify: %s", order.broker_order_id)
        for existing in self._orders:
            if existing.broker_order_id == order.broker_order_id:
                existing.quantity = order.quantity
                existing.limit_price = order.limit_price
                existing.order_type = order.order_type
                return
        raise ValueError(f"unknown mock order: {order.broker_order_id}")

    def cancel_order(self, order: BrokerOrder) -> None:
        logger.info("[mock-broker] DRY-RUN cancel: %s", order.broker_order_id)
        for existing in self._orders:
            if existing.broker_order_id == order.broker_order_id:
                existing.status = "CANCELLED"
                return
        raise ValueError(f"unknown mock order: {order.broker_order_id}")

    def get_order_book(self) -> list[BrokerOrder]:
        return list(self._orders)

    def calculate_order_margin(self, order: BrokerOrder) -> MarginInfo:
        return MarginInfo(initial_margin=0.0, maintenance_margin=0.0, is_funded=True)

    def poll_fill(self, broker_order_id: Any) -> dict[str, Any] | None:
        """Dry-run orders never fill — inventing fills would fake execution."""
        return None


def next_monthly_expiry(reference: date | None = None) -> date:
    """Convenience passthrough matching the generator's calendar."""
    from backtest.options.quote_providers import SyntheticChainGenerator

    return SyntheticChainGenerator().next_monthly_expiry(reference)
