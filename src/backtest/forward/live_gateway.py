"""F-12 — the live equity order seam for forward-test runners.

Before this module, a ``mode='live'`` runner on the
:class:`~backtest.forward.portfolio_manager.PortfolioManager` path still
filled through :class:`~backtest.forward.paper_runner.PaperBroker` — a live
runner could silently paper-trade. The single-run engine got its seam in
ticket #8 (:class:`~backtest.simulator.fill_providers.BrokerFillProvider`);
this is the multi-runner forward path's equivalent.

The seam discipline is the same as P3.3: **only the fill differs.**

* Order emission, tagging, fill routing, portfolio accounting, risk
  supervision — all shared with paper. The gateway duck-types
  :meth:`PaperBroker.submit_market`, so a runner never knows.
* ``submit_market`` = ledger tag → ``broker.place_order`` (idempotent: the
  ledger's ``client_order_id`` rides along; the broker order id is stamped
  back on the ledger order) → order goes WORKING. No fake instant fill.
* ``poll_pending`` = the pump (called once per manager tick): polls each
  working order, applies only the not-yet-applied DELTA of the broker's
  cumulative filled quantity (a repeated poll can never double-count),
  books the fill into the runner's own portfolio (``validate=False`` — a
  real fill is known-good history; dropping it would desync the book from
  the broker) and routes it through the SAME ``ledger.apply_fill`` paper
  uses, so the trade log, fills and UI stay identical.
* ``reconcile`` = the honesty check: the broker's order book is compared
  against the local working set; rejected/unknown-at-broker orders are
  cancelled locally with a WARNING, never silently.
* Restart-safe with P2.4: ``snapshot_state``/``restore_state`` persist the
  working set (coid → broker order id + applied qty) inside the runner's
  state file, so a restart re-arms POLLING for orders that already exist at
  the venue instead of double-placing them.

Fail-closed arming (mirrors the LiveOptionTrader invariant): constructing
the gateway needs ALL of ``confirm_live=True``, env
``ALLOW_LIVE_ORDERS ∈ {1,true,yes}`` and an authenticated broker session —
anything less raises before any order can exist.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, List, Optional

from backtest.forward.paper_runner import OrderRefused, OrderRequest

logger = logging.getLogger(__name__)

#: Env values that arm live order placement (same vocabulary as the
#: LiveOptionTrader gate — one mental model across the project).
ALLOW_LIVE_ORDERS_VALUES = {"1", "true", "yes"}


def live_orders_allowed() -> bool:
    """True when ``ALLOW_LIVE_ORDERS`` explicitly arms live placement."""
    return os.environ.get("ALLOW_LIVE_ORDERS", "").strip().lower() in ALLOW_LIVE_ORDERS_VALUES


class LiveEquityGateway:
    """Routes a runner's equity orders to the real broker, fills come back
    by polling. Duck-types :class:`PaperBroker.submit_market`."""

    def __init__(
        self,
        ledger: Any,
        broker: Any,
        exchange: str = "NSE",
        product: str = "INTRADAY",
        confirm_live: bool = False,
    ) -> None:
        # -- fail-closed arming: ALL three, or nothing trades --------------
        if not confirm_live:
            raise ValueError(
                "live order gateway refused: confirm_live=True is required "
                "(live equity orders go to the REAL broker)"
            )
        if not live_orders_allowed():
            raise ValueError(
                "live order gateway refused: set ALLOW_LIVE_ORDERS=1 to arm "
                "live equity order placement"
            )
        is_authed = getattr(broker, "is_authenticated", None)
        if not callable(is_authed) or not is_authed():
            raise ValueError(
                "live order gateway refused: broker session is not authenticated"
            )
        self.ledger = ledger
        self.broker = broker
        self.exchange = exchange
        self.product = product
        #: coid → working state (see snapshot_state for the shape).
        self._working: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    # The PaperBroker-compatible surface
    # ------------------------------------------------------------------ #

    def submit_market(
        self,
        instance_id: str,
        symbol: str,
        side: str,
        quantity: float,
        fill_price: float,
        ts: Optional[str] = None,
        tag: Optional[Dict] = None,
    ) -> str:
        """Place at the broker; returns the ledger client_order_id.

        Returns a STRING (the coid), not a :class:`FillEvent` — nothing has
        filled yet and pretending otherwise is how paper trading sneaks into
        live. Callers branch on the type.
        """
        order = self.ledger.submit(
            instance_id,
            OrderRequest(symbol=symbol, side=side, quantity=quantity, tag=tag or {}),
        )
        coid = order.client_order_id
        # The price the runner decided on — with the venue's real fill later
        # polled in, this is what makes live slippage measurable.
        order.requested_price = float(fill_price)

        from backtest.brokers.base import BrokerOrder

        broker_order = BrokerOrder(
            client_order_id=coid,
            symbol=str(symbol).strip().upper(),
            side=side,
            quantity=int(quantity),
            order_type="MARKET",
            exchange=self.exchange,
            product=self.product,
        )
        try:
            broker_order_id = str(self.broker.place_order(broker_order))
        except Exception as exc:
            # Never leave a phantom working order — the venue refused, so
            # the local order dies with it and the runner sees the error.
            # Retrying is the caller's decision and it is NOT safe by default:
            # a timeout can mean "accepted, acknowledgment lost", and sending
            # again there is how one intent becomes two live orders.
            self.ledger.cancel(coid)
            raise OrderRefused(
                f"venue refused the placement: {exc.__class__.__name__}: {exc}",
                client_order_id=coid,
                retryable=False,
            ) from exc

        order.broker_order_id = broker_order_id
        with self._lock:
            self._working[coid] = {
                "instance_id": instance_id,
                "symbol": str(symbol).strip().upper(),
                "side": side,
                "quantity": float(quantity),
                "applied_qty": 0.0,
                "broker_order_id": broker_order_id,
            }
        logger.info(
            "[live] ORDER PLACED %s %s x%g coid=%s broker_order=%s (fill pending poll)",
            side, broker_order.symbol, quantity, coid, broker_order_id,
        )
        return coid

    # ------------------------------------------------------------------ #
    # The pump — one call per manager tick
    # ------------------------------------------------------------------ #

    def poll_pending(self) -> int:
        """Poll every working order; apply new fill deltas. Returns the
        number of fill events applied. Never raises for a single-order
        broker error — it logs and keeps the order working."""
        applied = 0
        with self._lock:
            working = list(self._working.items())
        for coid, state in working:
            try:
                raw = self.broker.poll_fill(state["broker_order_id"])
            except Exception as exc:  # noqa: BLE001 — keep polling siblings
                logger.warning(
                    "[live] poll_fill failed for coid=%s broker_order=%s: %s",
                    coid, state["broker_order_id"], exc,
                )
                continue
            if raw is None:
                continue  # still working at the venue
            from backtest.simulator.fill import Fill

            # Fall back to the ORDER's own terms for any field the venue row
            # omits — done here (setdefault) because from_broker treats an
            # explicit override as a hard kwarg and collides with the row.
            row = dict(raw)
            row.setdefault("symbol", state["symbol"])
            row.setdefault("side", state["side"])
            try:
                fill = Fill.from_broker(
                    row,
                    broker_order_id=state["broker_order_id"],
                    order_id=coid,
                )
            except Exception as exc:  # noqa: BLE001 — a bad row must not kill the pump
                logger.error(
                    "[live] unparseable fill row for coid=%s: %s (%r) "
                    "— order stays working; reconcile before acting",
                    coid, exc, raw,
                )
                continue
            filled_total = float(fill.quantity)
            new_qty = filled_total - float(state["applied_qty"])
            if new_qty <= 0:
                continue  # idempotent re-poll — delta already booked
            price = float(fill.fill_price)

            self._apply_bookkeeping(coid, state, price, new_qty)
            state["applied_qty"] += new_qty
            if state["applied_qty"] >= float(state["quantity"]) - 1e-9:
                with self._lock:
                    self._working.pop(coid, None)
            applied += 1
            logger.info(
                "[live] FILL coid=%s %s %s x%g @ %.4f (cumulative %g/%g)",
                coid, state["side"], state["symbol"], new_qty, price,
                state["applied_qty"], state["quantity"],
            )
        return applied

    def _apply_bookkeeping(
        self, coid: str, state: Dict[str, Any], price: float, qty: float
    ) -> None:
        """Book the fill into the runner's portfolio + shared ledger.

        Same two-step shape as paper (portfolio order + fill application,
        then ``ledger.apply_fill`` for routing/trade-log), except the fill
        carries the broker's ACTUAL price and zero simulated fees — real
        costs are already inside the traded price.
        """
        from backtest.simulator.enums import OrderSide, OrderType, TimeInForce
        from backtest.simulator.fill import Fill, LiquidityFlag
        from backtest.simulator.order import Order as SimOrder

        order = self.ledger.get_order(coid)
        runner = self.ledger.runner_for(state["instance_id"])
        if order is None or runner is None:
            logger.error(
                "[live] fill for coid=%s has no local order/runner — REFUSING "
                "to book (book would desync from broker; reconcile manually)",
                coid,
            )
            return

        sim_order = SimOrder(
            symbol=state["symbol"],
            side=OrderSide.BUY if state["side"] == "BUY" else OrderSide.SELL,
            quantity=qty,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            portfolio_id=runner.portfolio.portfolio_id,
            strategy_name=runner.config.strategy_name,
            client_order_id=coid,
        )
        sim_order.validate()
        sim_order.submit()
        runner.portfolio.add_order(sim_order)

        fill = Fill(
            symbol=state["symbol"],
            side=state["side"],
            quantity=qty,
            fill_price=price,
            order_id=sim_order.order_id,
            reference_price=price,
            liquidity_flag=LiquidityFlag.TAKER,
            strategy_name=runner.config.strategy_name,
        )
        # A real fill is known-good history: validate=False means a local
        # limit disagreement can never desync the book from the broker.
        runner.portfolio.apply_fill(fill, validate=False)
        self.ledger.apply_fill(coid, price, qty)

    # ------------------------------------------------------------------ #
    # The honesty check
    # ------------------------------------------------------------------ #

    def reconcile(self) -> Dict[str, int]:
        """Compare the broker's order book against the local working set.

        * rejected/cancelled at the broker → cancelled locally + WARNING;
        * fully filled but never reported → WARNING (poll should have caught
          it — the venue and the book have diverged);
        * locally working but unknown at the broker → WARNING (day orders
          can expire at the venue).
        """
        summary = {
            "working": len(self._working),
            "rejected": 0,
            "unknown_at_broker": 0,
            "divergent": 0,
        }
        try:
            book = {str(o.broker_order_id): o for o in self.broker.get_order_book()}
        except Exception as exc:  # noqa: BLE001 — a broker outage must not kill the tick
            logger.warning("[live] reconcile skipped — order book unavailable: %s", exc)
            return summary
        for coid, state in list(self._working.items()):
            row = book.get(state["broker_order_id"])
            if row is None:
                summary["unknown_at_broker"] += 1
                logger.warning(
                    "[live] coid=%s broker_order=%s is UNKNOWN at the broker "
                    "(expired or purged) — cancelling locally",
                    coid, state["broker_order_id"],
                )
                self.ledger.cancel(coid)
                with self._lock:
                    self._working.pop(coid, None)
                continue
            status = str(row.status or "").upper()
            if status in ("REJECTED", "CANCELLED"):
                summary["rejected"] += 1
                logger.warning(
                    "[live] coid=%s broker_order=%s was %s at the broker — "
                    "cancelled locally (no fill existed)",
                    coid, state["broker_order_id"], status,
                )
                self.ledger.cancel(coid)
                with self._lock:
                    self._working.pop(coid, None)
            elif status in ("COMPLETE", "FILLED", "EXECUTED"):
                filled = float(getattr(row, "filled_quantity", 0) or 0)
                if filled > float(state["applied_qty"]) + 1e-9:
                    summary["divergent"] += 1
                    logger.warning(
                        "[live] coid=%s shows %g filled at the broker but only "
                        "%g booked locally — poll missed a delta, forcing re-poll",
                        coid, filled, state["applied_qty"],
                    )
        return summary

    # ------------------------------------------------------------------ #
    # P2.4 round-trip — restarts re-arm polling, never re-place
    # ------------------------------------------------------------------ #

    def snapshot_state(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "working": [dict(state, coid=coid) for coid, state in self._working.items()],
            }

    def restore_state(self, payload: Dict[str, Any]) -> None:
        for state in payload.get("working", []):
            coid = state["coid"]
            with self._lock:
                self._working[coid] = {
                    "instance_id": state["instance_id"],
                    "symbol": state["symbol"],
                    "side": state["side"],
                    "quantity": float(state["quantity"]),
                    "applied_qty": float(state.get("applied_qty", 0.0)),
                    "broker_order_id": state["broker_order_id"],
                }
            # The ledger order died with the old process — re-tag it so the
            # incoming fill has somewhere to route.
            if self.ledger.get_order(coid) is None:
                self.ledger.reattach_order(
                    state["instance_id"], coid, state, state["broker_order_id"]
                )
            logger.info(
                "[live] re-armed polling for coid=%s broker_order=%s (%g/%g filled)",
                coid, state["broker_order_id"],
                state.get("applied_qty", 0.0), state["quantity"],
            )

    def working_count(self) -> int:
        with self._lock:
            return len(self._working)

    def working_coids(self) -> List[str]:
        with self._lock:
            return list(self._working)

    def working_state(self, coid: str) -> Optional[Dict[str, Any]]:
        """The working set's entry for ``coid`` (None when not working here)."""
        with self._lock:
            state = self._working.get(coid)
            return dict(state) if state is not None else None

    # ------------------------------------------------------------------ #
    # Phase 3 — amending / cancelling a working order at the VENUE
    # ------------------------------------------------------------------ #
    #
    # Both go to the broker first and only touch local state once the venue
    # has agreed. The opposite order (mark cancelled locally, hope the venue
    # follows) is how a "cancelled" order fills anyway: the fill arrives,
    # finds no working state, and either gets silently dropped or — worse —
    # booked against a book that already assumed the position was gone.

    def _venue_handle(self, coid: str) -> Any:
        """The ``BrokerOrder`` a modify/cancel needs (venue id included)."""
        from backtest.brokers.base import BrokerOrder

        order = self.ledger.get_order(coid)
        state = self.working_state(coid)
        if order is None:
            raise KeyError(f"unknown order: {coid}")
        if state is None:
            raise ValueError(
                f"order {coid} is not working at the venue — nothing to amend "
                "or cancel (it may already have filled; reconcile before acting)"
            )
        return BrokerOrder(
            broker_order_id=state["broker_order_id"],
            client_order_id=coid,
            symbol=state["symbol"],
            side=state["side"],
            quantity=int(order.quantity),
            order_type="LIMIT" if order.limit_price else "MARKET",
            limit_price=order.limit_price,
            exchange=self.exchange,
            product=self.product,
        )

    def cancel_working(self, coid: str) -> Dict[str, Any]:
        """Cancel a working order at the venue, then locally.

        Raises (without changing local state) when the venue refuses — a
        rejected cancel means the order may still fill, and the operator must
        see that instead of a green "cancelled" that is not true.
        """
        ctx = self._venue_handle(coid)
        try:
            self.broker.cancel_order(ctx)
        except Exception as exc:
            raise OrderRefused(
                f"venue refused the cancel: {exc.__class__.__name__}: {exc}",
                client_order_id=coid,
                retryable=False,
            ) from exc
        if not self.ledger.cancel(coid):
            raise ValueError(
                f"order {coid} could not be cancelled locally after the venue "
                "accepted — reconcile the book before trading further"
            )
        with self._lock:
            self._working.pop(coid, None)
        logger.info(
            "[live] CANCELLED coid=%s broker_order=%s at the venue",
            coid, ctx.broker_order_id,
        )
        return {"client_order_id": coid, "venue_cancelled": True}

    def modify_working(
        self,
        coid: str,
        quantity: Optional[float] = None,
        limit_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Amend a working order at the venue, then mirror it locally.

        The ledger row is only rewritten AFTER the venue accepted, so the two
        can never claim different quantities for the same live order.
        """
        ctx = self._venue_handle(coid)
        if quantity is not None:
            new_qty = float(quantity)
            if new_qty <= 0:
                raise ValueError(f"amended quantity must be positive, got {new_qty!r}")
            ctx.quantity = int(new_qty)
        if limit_price is not None:
            new_price = float(limit_price)
            if new_price <= 0:
                raise ValueError(f"amended limit price must be positive, got {new_price!r}")
            ctx.limit_price = new_price
            ctx.order_type = "LIMIT"
        try:
            self.broker.modify_order(ctx)
        except Exception as exc:
            raise OrderRefused(
                f"venue refused the amendment: {exc.__class__.__name__}: {exc}",
                client_order_id=coid,
                retryable=False,
            ) from exc

        order = self.ledger.amend(coid, quantity=quantity, limit_price=limit_price)
        with self._lock:
            state = self._working.get(coid)
            if state is not None:
                state["quantity"] = float(order.quantity)
        logger.info(
            "[live] AMENDED coid=%s broker_order=%s → qty=%g limit=%s",
            coid, ctx.broker_order_id, order.quantity, order.limit_price,
        )
        return {
            "client_order_id": coid,
            "venue_amended": True,
            "quantity": order.quantity,
            "limit_price": order.limit_price,
            "amend_count": order.amend_count,
        }
