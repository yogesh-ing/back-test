"""Generic broker authentication layer (mStock Authentication UI epic).

Broker-agnostic contract layer:

* ``BrokerAuthBase`` — two-step auth contract (Task 1.1);
  ``MStockBroker`` implements it for mStock (Task 1.2).
* ``BrokerOrderBase`` — order lifecycle contract (ticket P3.1):
  place / modify / cancel / book / margin, with the shared value types
  ``BrokerOrderId`` / ``BrokerOrder`` / ``MarginInfo``. A live broker
  composes both ABCs (P3.2).
* ``BrokerSessionManager`` holds the single active broker instance,
  exposes the session token to the Forward Engine and status to the API
  routes, and runs the session-expiry background monitor (Tasks 1.3 + 2.2).
  Sessions are consumed exclusively by the Forward Testing engine; raw
  session tokens never reach the browser.
"""

from __future__ import annotations

from backtest.brokers.base import (
    ORDER_STATUSES,
    SESSION_STATUSES,
    STATUS_AUTHENTICATED,
    STATUS_EXPIRED,
    STATUS_EXPIRING_SOON,
    STATUS_UNAUTHENTICATED,
    BrokerAuthBase,
    BrokerOrder,
    BrokerOrderBase,
    BrokerOrderId,
    MarginInfo,
)
from backtest.brokers.mstock import MStockBroker, MStockOrderError
from backtest.brokers.session_manager import (
    BrokerSessionManager,
    get_session_manager,
    reset_default_manager,
)

__all__ = [
    "BrokerAuthBase",
    "BrokerOrderBase",
    "BrokerOrderId",
    "BrokerOrder",
    "MarginInfo",
    "ORDER_STATUSES",
    "MStockBroker",
    "MStockOrderError",
    "BrokerSessionManager",
    "get_session_manager",
    "reset_default_manager",
    "SESSION_STATUSES",
    "STATUS_UNAUTHENTICATED",
    "STATUS_AUTHENTICATED",
    "STATUS_EXPIRING_SOON",
    "STATUS_EXPIRED",
]
