"""Generic broker authentication layer (mStock Authentication UI epic).

Broker-agnostic auth layer: ``BrokerAuthBase`` defines the contract
(Task 1.1), ``MStockBroker`` implements it for mStock (Task 1.2), and
``BrokerSessionManager`` holds the single active broker instance
(Task 1.3). Sessions are consumed exclusively by the Forward Testing
engine; raw session tokens never reach the browser.
"""

from __future__ import annotations

from backtest.brokers.base import (
    SESSION_STATUSES,
    STATUS_AUTHENTICATED,
    STATUS_EXPIRED,
    STATUS_EXPIRING_SOON,
    STATUS_UNAUTHENTICATED,
    BrokerAuthBase,
)

__all__ = [
    "BrokerAuthBase",
    "SESSION_STATUSES",
    "STATUS_UNAUTHENTICATED",
    "STATUS_AUTHENTICATED",
    "STATUS_EXPIRING_SOON",
    "STATUS_EXPIRED",
]
