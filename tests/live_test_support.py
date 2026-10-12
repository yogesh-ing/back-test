"""Shared support for tests that exercise the LIVE bucket's accounting.

F-12 made live runners fail-closed: ``mode='live'`` runners are refused
unless the order gateway is armed (confirm flag + ``ALLOW_LIVE_ORDERS`` +
an authenticated broker). Suites that test live-bucket RISK ISOLATION,
scoping and UI — not the order path — arm the gateway with this
deterministic fake venue so the accounting under test still runs.

Phase 3 (Live Order Management) made the venue's answer to a cancel/amend
matter, so the fake now implements the order lifecycle the way a real venue
does: an unknown or settled ``broker_order_id`` is REFUSED rather than
quietly accepted.
"""

from __future__ import annotations


class FakeLiveBroker:
    """Authenticated venue fake; records what it was asked to do.

    Fills are never spontaneous — script them via :attr:`fills` (the
    ``poll_fill`` row) to test the pump.
    """

    def __init__(self) -> None:
        #: Every BrokerOrder ever placed, in order.
        self.placed: list = []
        #: broker_order_id → scripted cumulative fill row (poll_fill serves it)
        self.fills: dict = {}
        #: broker_order_ids the venue accepted a cancel for.
        self.cancelled: list = []
        #: (broker_order_id, quantity, limit_price) the venue accepted an amend for.
        self.amended: list = []
        #: broker_order_ids the venue considers done — cancel/amend refuse them.
        self.settled: set = set()

    def is_authenticated(self) -> bool:
        return True

    def _known(self) -> set:
        return {f"FAKE-{i + 1}" for i in range(len(self.placed))}

    def place_order(self, order) -> str:
        self.placed.append(order)
        return f"FAKE-{len(self.placed)}"

    def cancel_order(self, order) -> None:
        """Venue cancel: unknown or already-settled ids raise, like a venue."""
        target = str(getattr(order, "broker_order_id", "") or "")
        if target not in self._known() or target in self.settled:
            raise RuntimeError(f"venue has no open order {target!r} to cancel")
        self.cancelled.append(target)

    def modify_order(self, order) -> None:
        """Venue amend: records the new terms, refuses unknown/settled ids."""
        target = str(getattr(order, "broker_order_id", "") or "")
        if target not in self._known() or target in self.settled:
            raise RuntimeError(f"venue has no open order {target!r} to amend")
        self.amended.append((target, order.quantity, order.limit_price))

    def poll_fill(self, broker_order_id):
        return self.fills.get(str(broker_order_id))

    def get_order_book(self):
        return []


#: Splat into PortfolioManager()/reset_portfolio_manager() to arm the
#: live-order gateway with the fake venue. Pair with the env:
#:   monkeypatch.setenv("ALLOW_LIVE_ORDERS", "1")
#:
#: NOTE: this is a module-level singleton and therefore accumulates state
#: across a test session. Tests that assert on what the venue was ASKED to do
#: (place/cancel/amend) must pass their own ``FakeLiveBroker()`` instead:
#:   ARMED_KWARGS | {"live_broker": broker}
ARMED_KWARGS = {
    "live_broker": FakeLiveBroker(),
    "confirm_live_orders": True,
}
