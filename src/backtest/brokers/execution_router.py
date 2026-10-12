"""ExecutionRouter — routes each runner's orders to ITS broker (PRD §4.3).

Why: with concurrent multi-broker sessions, every live runner must place
orders through the broker of its segment (or its explicit
``execution_broker`` override) — never another broker's session.

Guarantees (LOM rules extended):

* ``mode=paper`` → the paper broker (broker assignment ignored);
* ``mode=live`` + expired/missing session for the runner's broker →
  :class:`BrokerSessionExpired` — the runner PAUSES entries
  (RETRY_REFUSED semantics). NO silent rerouting to another broker EVER;
* ``poll_fill(order_id, broker)`` polls the CORRECT broker's order book.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

__all__ = ["ExecutionRouter", "BrokerSessionExpired", "get_execution_router"]

logger = logging.getLogger("backtest.brokers.execution_router")


class BrokerSessionExpired(RuntimeError):
    """The runner's broker session is expired/absent — entries must pause.

    Carries ``broker_name`` so alerts can say WHICH broker needs
    re-authentication (Risk-mitigation 10.1).
    """

    def __init__(self, broker_name: str, message: str | None = None) -> None:
        self.broker_name = broker_name
        super().__init__(
            message
            or f"{broker_name} session expired or not authenticated — "
            f"runners routed to {broker_name} pause entries; re-authenticate to resume"
        )


class ExecutionRouter:
    """Resolves runner → broker instance for order placement and fills."""

    def __init__(
        self,
        session_manager: Any = None,
        segments: Any = None,
        paper_broker: Any = None,
    ) -> None:
        self._session_manager = session_manager
        self._segments = segments
        self._paper_broker = paper_broker

    # -- lazy singletons (kept injectable for tests) --------------------

    @property
    def sessions(self) -> Any:
        if self._session_manager is None:
            from backtest.brokers.session_manager import get_session_manager

            self._session_manager = get_session_manager()
        return self._session_manager

    @property
    def segments(self) -> Any:
        if self._segments is None:
            from backtest.brokers.segments import get_segments_config

            self._segments = get_segments_config()
        return self._segments

    # -- routing ---------------------------------------------------------

    def broker_name_for(self, runner_config: Any) -> Optional[str]:
        """The broker name a runner routes to (``None`` → paper broker).

        Reads ``mode`` / ``segment`` / ``execution_broker`` from any object
        or mapping with those fields (RunnerConfig, dict, playbook row).
        """
        get = (
            runner_config.get
            if isinstance(runner_config, dict)
            else lambda k, d=None: getattr(runner_config, k, d)
        )
        from backtest.brokers.segments import resolve_execution_broker

        return resolve_execution_broker(
            mode=get("mode", "paper"),
            segment=get("segment", None),
            execution_broker=get("execution_broker", None),
            config=self._segments,  # None → module default
        )

    def order_for(self, runner_config: Any) -> Any:
        """The broker instance this runner's orders go to.

        * paper mode → the injected paper broker (or ``None`` when the
          caller owns its own paper book — the PortfolioManager path);
        * live mode → the broker instance, ONLY while its session is valid;
        * expired/absent session → :class:`BrokerSessionExpired` (the caller
          pauses entries; no silent rerouting).
        """
        name = self.broker_name_for(runner_config)
        if name is None:
            return self._paper_broker
        broker = self.sessions.get_authenticated_broker(name)
        if broker is None:
            raise BrokerSessionExpired(name)
        return broker

    # -- fills -------------------------------------------------------------

    def poll_fill(self, order_id: str, broker_name: str) -> Optional[dict]:
        """Poll fill status from the CORRECT broker's order book.

        Returns the broker's fill/status payload (or ``None`` when the
        order is not found yet). Raises :class:`BrokerSessionExpired` when
        that broker's session is gone — a fill poll must never silently
        query a different broker.
        """
        broker = self.sessions.get_authenticated_broker(broker_name)
        if broker is None:
            raise BrokerSessionExpired(broker_name)

        poll = getattr(broker, "poll_fill", None)
        if callable(poll):
            return poll(order_id)

        # Generic fallback: scan the broker's order book for the id.
        get_book = getattr(broker, "get_order_book", None)
        if not callable(get_book):
            return None
        for order in get_book():
            boid = getattr(order, "broker_order_id", None)
            if boid is not None and str(boid) == str(order_id):
                return {
                    "status": getattr(order, "status", None),
                    "filled_quantity": getattr(order, "filled_quantity", 0),
                    "average_fill_price": getattr(order, "average_fill_price", None),
                }
        return None


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------

_router: ExecutionRouter | None = None


def get_execution_router() -> ExecutionRouter:
    global _router
    if _router is None:
        _router = ExecutionRouter()
    return _router
