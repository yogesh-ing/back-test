"""Shared support for tests that exercise the LIVE bucket's accounting.

F-12 made live runners fail-closed: ``mode='live'`` runners are refused
unless the order gateway is armed (confirm flag + ``ALLOW_LIVE_ORDERS`` +
an authenticated broker). Suites that test live-bucket RISK ISOLATION,
scoping and UI — not the order path — arm the gateway with this
deterministic fake venue so the accounting under test still runs.
"""

from __future__ import annotations


class FakeLiveBroker:
    """Authenticated venue fake; records placements, never fills them."""

    def __init__(self) -> None:
        self.placed: list = []
        #: broker_order_id → scripted cumulative fill row (poll_fill serves it)
        self.fills: dict = {}

    def is_authenticated(self) -> bool:
        return True

    def place_order(self, order) -> str:
        self.placed.append(order)
        return f"FAKE-{len(self.placed)}"

    def poll_fill(self, broker_order_id):
        return self.fills.get(str(broker_order_id))

    def get_order_book(self):
        return []


#: Splat into PortfolioManager()/reset_portfolio_manager() to arm the
#: live-order gateway with the fake venue. Pair with the env:
#:   monkeypatch.setenv("ALLOW_LIVE_ORDERS", "1")
ARMED_KWARGS = {
    "live_broker": FakeLiveBroker(),
    "confirm_live_orders": True,
}
