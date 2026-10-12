"""Order/trade seeding helpers for the cross-broker analytics tests (PRD-003).

Split out of ``conftest.py`` so the fixtures there stay fixtures and these
can be imported by name — the same rule the rest of ``tests/`` follows
(``tests/intelligence``, ``tests/brokers``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional

from backtest.forward.paper_runner import OrderRequest
from backtest.forward.portfolio_manager import get_portfolio_manager


def _iso(days_ago: float = 0.0, seconds: float = 0.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago, seconds=seconds)).isoformat()


def seed_trades(instance_id: str, pnls: List[float], days_ago: float = 1.0) -> None:
    """Write synthetic closed trades onto a runner's portfolio book.

    Injected the way the state store restores them: ``closed_trades`` is
    derived from ``portfolio.closed_positions``, so a zero-size Position with
    a ``realized_pnl`` and a ``closed_at`` is exactly what a real closed round
    trip looks like to the analytics layer.
    """
    from backtest.simulator.position import Position

    portfolio = get_portfolio_manager().get_runner(instance_id).portfolio
    for i, pnl in enumerate(pnls):
        pos = Position(
            symbol="NIFTY",
            quantity=50,
            average_entry_price=100,
            current_price=100,
        )
        pos.quantity = pos.quantity.__class__(0)  # closed: zero size
        pos.realized_pnl = pos.realized_pnl.__class__(str(pnl))
        pos.opened_at = datetime.now(timezone.utc) - timedelta(days=days_ago, seconds=i * 60 + 300)
        pos.closed_at = datetime.now(timezone.utc) - timedelta(days=days_ago, seconds=i * 60)
        portfolio.closed_positions.append(pos)


def place_order(
    instance_id: str,
    *,
    side: str = "BUY",
    quantity: float = 50.0,
    requested_price: Optional[float] = 100.0,
    fill_price: Optional[float] = None,
    status: str = "FILLED",
    days_ago: float = 0.0,
    fill_delay_s: float = 2.0,
    created_ts: Optional[str] = None,
) -> str:
    """Submit an order to the ledger and drive it to a terminal state."""
    ledger = get_portfolio_manager().ledger
    order = ledger.submit(
        instance_id,
        OrderRequest(
            symbol="NIFTY",
            side=side,
            quantity=quantity,
            order_type="MARKET",
        ),
    )
    order.requested_price = requested_price
    order.created_ts = created_ts or _iso(days_ago)

    if status == "FILLED":
        fill_price = fill_price if fill_price is not None else requested_price
        fill_ts = _iso(days_ago, -fill_delay_s)
        # apply_fill stamps its own `now`, so the order fields are set
        # directly to keep the test's timeline exact (apply_fill's own
        # behaviour is covered by the OrderLedger tests).
        order.status = "FILLED"
        order.filled_qty = quantity
        order.avg_fill_price = fill_price
        order.filled_ts = fill_ts
        order.updated_ts = fill_ts
        if requested_price:
            diff = fill_price - requested_price
            adverse = diff if side == "BUY" else -diff
            order.slippage = round(adverse, 6)
            order.slippage_pct = round(adverse / requested_price, 8)
    elif status == "REJECTED":
        ledger.reject(order.client_order_id, "insufficient margin")
    elif status == "CANCELLED":
        ledger.cancel(order.client_order_id)
    return order.client_order_id


def place_orders(
    instance_id: str,
    count: int,
    *,
    slip_bps: float,
    fill_rate_pct: float = 100.0,
    reject_rate_pct: float = 0.0,
    fill_delay_s: float = 2.0,
    days_ago: float = 0.5,
    quantity: float = 50.0,
    requested_price: float = 100.0,
) -> None:
    """Place ``count`` orders with a controlled outcome mix and slippage.

    The three outcome shares are INDEPENDENT and sum to 100%: rejections are
    taken out of the filled bucket, so asking for 95% fill / 2% reject yields
    exactly 95/2/3 — not a fill rate silently reduced by the rejections.
    Slippage is applied in one direction (a BUY fills above its request) so
    the resulting ``avg_slippage_bps`` is exactly ``slip_bps``.
    """
    n_rejected = int(round(count * reject_rate_pct / 100.0))
    n_cancelled = int(round(count * max(0.0, 100.0 - fill_rate_pct - reject_rate_pct) / 100.0))
    n_filled = max(0, count - n_rejected - n_cancelled)
    fill_price = requested_price * (1.0 + slip_bps / 10_000.0)

    for i in range(count):
        if i < n_filled:
            place_order(
                instance_id,
                quantity=quantity,
                requested_price=requested_price,
                fill_price=fill_price,
                status="FILLED",
                days_ago=days_ago,
                fill_delay_s=fill_delay_s + (i % 5) * 0.4,
            )
        elif i < n_filled + n_rejected:
            place_order(
                instance_id,
                quantity=quantity,
                requested_price=requested_price,
                status="REJECTED",
                days_ago=days_ago,
            )
        else:
            place_order(
                instance_id,
                quantity=quantity,
                requested_price=requested_price,
                status="CANCELLED",
                days_ago=days_ago,
            )
