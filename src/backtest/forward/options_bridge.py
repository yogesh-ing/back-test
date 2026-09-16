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

Position lifecycle (task B1): one structure at a time. While it is open new
views do not pyramid — they are **exit signals**. Every bar the bridge asks
:class:`~backtest.options.exit_policy.ExitPolicy` whether to leave (stop,
target, days-to-expiry, time stop, signal flip, signal neutral) and closes
through the broker, recording ``exit_reason``. ``expression["exit"]``
configures the rules; ``reenter`` optionally reverses into the opposite
structure on a flip. Expiry settlement is B2: :meth:`OptionsBridge.on_bar`
drives the canonical :class:`~backtest.options.expiry.ExpiryManager` against
the bar clock, so a structure held into expiry is cash-settled at the
settlement spot on the expiry bar, and the next view re-enters on the next
monthly expiry (a roll).

Reporting (task C2): :meth:`OptionsBridge.open_structures_snapshot` turns the
book's open legs into flat, JSON-safe rows (structure, strikes, net premium,
mark, unrealized P&L, next expiry) so the portfolio matrix and the deep-dive
drawer can render an option runner with option-shaped columns instead of the
equity ones. ``summary()`` embeds it as ``open_structures_detail``.

Per-bar pricing (task A1): the bridge is also the forward loop's **market
clock**. :meth:`OptionsBridge.on_bar` moves the synthetic spot to each bar
close, pins the quote provider's pricing reference to the bar timestamp (so
theta follows the replay clock instead of the wall clock) and marks the book
to market. Without it a runner's legs keep their entry premium forever and
``unrealized_pnl`` stays pinned at zero.
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Deque, Optional

from backtest.options.exit_policy import (
    EXIT_DTE,
    EXIT_SIGNAL_FLIP,
    RISK_REASONS,
    ExitConfig,
    ExitPolicy,
    _label,
)
from backtest.options.expiry import EXIT_REASON_SETTLEMENT, ExpiryManager
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
        # -- exit policy (task B1) ------------------------------------------
        self.exit_policy = ExitPolicy(ExitConfig.from_expression(self.expression.get("exit")))
        #: Minutes before the close at which the canonical expiry pipeline
        #: squares a *same-day* expiry off at market (B2). 0 = never (ride to
        #: cash settlement). Daily forward bars land at 09:15, so this only
        #: fires for intraday bar cycles.
        self.squareoff_minutes_before: int = int(
            self.expression.get("squareoff_minutes_before", 30) or 0
        )

        self.open_structure_id: Optional[str] = None
        self.executed_count: int = 0
        self.closed_count: int = 0
        #: Underlying of the most recent structure — the symbol whose spot
        #: drives pricing (set at entry, defaulted for the first bar).
        self.underlying: str = "NIFTY"
        #: Last mark-to-market book value, for runner heartbeats/UI.
        self.last_unrealized_pnl: Decimal = Decimal("0")
        self.last_spot: Optional[float] = None
        self.last_mtm_ts: Optional[str] = None
        #: Most recent close, for the runner's signal log / deep dive.
        self.last_exit: Optional[dict[str, Any]] = None
        #: Exits that happened outside :meth:`on_market_view` (the per-bar
        #: risk rules) — the runner drains these to log them.
        self._exit_events: Deque[dict[str, Any]] = deque(maxlen=32)

        # -- bar clock / structure age bookkeeping ---------------------------
        self._bar_index: int = 0
        self._entry_bar_index: Optional[int] = None
        self._exit_bar_index: Optional[int] = None
        self._bars_without_view: int = 0
        self._bar_dt: Optional[datetime] = None
        self._structure_direction: Optional[Any] = None
        self._structure_expiry: Optional[Any] = None
        self._entry_premium: Decimal = Decimal("0")
        self._settlement_count: int = 0
        self._expiry_manager: Optional[ExpiryManager] = None
        # U2.2: re-entry tracking — max_reentries_per_day knob, default 2, next-bar only
        self._reentries_today: int = 0
        self._reentry_day: Optional[Any] = None

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def on_market_view(self, view: MarketView | None, strategy_name: str) -> dict[str, Any] | None:
        """Consume one bar's view: manage an open structure, else open one.

        Called once per bar for single-symbol option runners, **including
        bars with no view** — a viewless bar is how the neutral/time rules and
        a flip out of a position are detected, so ``None`` is meaningful
        rather than a no-op.

        Returns:

        * ``{"exited": True, ...}`` when a structure was closed,
        * the entry summary dict when a structure was opened,
        * ``{"rejected": True, ...}`` on a soft rejection (e.g. margin),
        * ``None`` when nothing happened.
        """
        if self._has_open_structure():
            closed = self._manage_open_structure(view, strategy_name)
            if closed is None:
                # Still holding: a view against the position is an exit signal,
                # never an entry one (V1 does not pyramid).
                return None
            # U2.2: same-bar re-entry impossible — even flip+reenter must wait to next bar
            # should_reenter() now returns False on same-bar, so we always return closed here
            # Re-entry will happen on next bar via normal entry path, counted via _reentries_today
            if not self._should_reenter(view):
                return closed
            # If we reach here, it's next-bar re-entry (should_reenter true and not same-bar)
            # For safety, still check blocked_reentry
            if self._blocked_reentry(view):
                return closed
            # Flip + reenter → fall through and open the reverse structure (next-bar only).
            # Counting happens in the entry path below via is_reentry check.
        elif view is None:
            # Nothing open and no conviction — nothing to do.
            return None

        structure_type = self._structure_type_for(view)
        if structure_type not in STRUCTURES:
            logger.warning("[options-bridge] unknown structure type %r", structure_type)
            return None

        if self._blocked_reentry(view):
            logger.debug("[options-bridge] entry suppressed on the exit bar")
            return None

        # U2.2: detect re-entry on next bar (last exit flip + reenter true) and enforce max_reentries_per_day
        is_reentry = False
        try:
            is_reentry = self._should_reenter(view)
        except Exception:
            is_reentry = False
        if is_reentry:
            # Already checked max in _should_reenter, but double-check and count
            try:
                self._reentries_today += 1
            except Exception:
                self._reentries_today = 1

        dte_block = self._entry_dte_block()
        if dte_block is not None:
            return dte_block

        try:
            return self._execute(view, structure_type, strategy_name)
        except Exception as exc:  # noqa: BLE001 — a bad bar must not kill the runner
            logger.warning(
                "[options-bridge] %s execution failed: %s", structure_type, exc
            )
            return {"rejected": True, "reason": str(exc)}

    def pop_exit_event(self) -> Optional[dict[str, Any]]:
        """Drain one exit produced by the per-bar risk rules.

        The runner calls this after :meth:`on_bar`, since a stop/target exit
        happens inside the pricing hook and still needs a signal-log entry.
        """
        return self._exit_events.popleft() if self._exit_events else None

    @property
    def bars_in_trade(self) -> int:
        """Bars since the open structure was entered (0 on the entry bar)."""
        if self._entry_bar_index is None:
            return 0
        return self._bar_index - self._entry_bar_index

    def summary(self) -> dict[str, Any]:
        """Compact book state for runner state payloads and tests."""
        broker = self.option_broker
        closed = broker.get_closed_structures()
        wins = sum(1 for s in closed if s.total_realized_pnl >= 0)
        total_pnl = sum((s.total_realized_pnl for s in closed), Decimal("0"))
        return {
            "open_positions": len(broker.get_open_positions()),
            "open_structures": len(broker.get_open_structures()),
            "executed_count": self.executed_count,
            "closed_count": self.closed_count,
            "capital": float(broker.capital),
            "equity": float(broker.total_equity),
            "realized_pnl": float(broker.total_realized_pnl),
            "unrealized_pnl": float(self.last_unrealized_pnl),
            "last_spot": self.last_spot,
            "last_mtm_ts": self.last_mtm_ts,
            "quote_source": str(getattr(self.quote_provider, "source_name", "unknown")),
            "last_exit": dict(self.last_exit) if self.last_exit else None,
            "settled_count": self._settlement_count,
            # -- closed-structure metrics (task B3) -------------------------
            "closed_structures": len(closed),
            "wins": wins,
            "losses": len(closed) - wins,
            "win_rate": (wins / len(closed)) if closed else 0.0,
            "total_pnl": float(total_pnl),
            "avg_pnl": (float(total_pnl) / len(closed)) if closed else 0.0,
            "costs_paid": float(broker.total_costs_paid),
            "exit_policy": self.exit_policy.config.to_dict(),
            # -- open-structure detail (task C2) ----------------------------
            "open_structures_detail": self.open_structures_snapshot(),
            "bars_in_trade": self.bars_in_trade if self.open_structure_id else 0,
        }

    # ------------------------------------------------------------------ #
    # Book metrics (task A2) — the runner folds these into its own numbers
    # ------------------------------------------------------------------ #

    @property
    def net_pnl(self) -> Decimal:
        """Book P&L since inception: realized + unrealized − all costs.

        ``total_equity − capital`` — the exact amount a runner's equity must
        move by when this bridge's book is folded in.
        """
        broker = self.option_broker
        return broker.total_equity - broker.capital

    @property
    def unrealized_pnl(self) -> Decimal:
        """Open legs' mark-to-market P&L (as of the last bar)."""
        return self.option_broker.total_unrealized_pnl

    @property
    def premium_at_risk(self) -> Decimal:
        """Net premium paid for the structures still open.

        For every V1 structure (long call/put, bull call spread, bear put
        spread) the net debit **is** the maximum loss, so this is the capital
        genuinely committed — the option analogue of an equity runner's
        ``deployed_capital``. Credit structures (net premium received) report
        zero here rather than a negative commitment; their risk sits with the
        broker's margin model.
        """
        total = Decimal("0")
        for structure in self.option_broker.get_open_structures():
            debit = structure.total_entry_cost
            if debit > 0:
                total += debit
        return total

    # ------------------------------------------------------------------ #
    # Reporting (task C2) — the book as flat rows for the matrix/deep-dive
    # ------------------------------------------------------------------ #

    def open_structures_snapshot(self) -> list[dict[str, Any]]:
        """Every open structure as a flat, JSON-safe row.

        The portfolio matrix and the deep-dive drawer are equity-shaped: they
        read ``symbol``/``qty``/``entry_price``/``current_price``/
        ``unrealized_pnl``. Rather than teach three views about option legs,
        the legs are collapsed here into one row per structure, with the same
        keys **plus** the option-specific ones (strikes, per-leg detail, next
        expiry, bars held). ``entry_price``/``current_price`` are the signed
        net premium per unit — debit positive, credit negative — so
        ``(current − entry) × units`` reads as the structure's P&L, exactly
        like an equity position.

        The next expiry is the one the **rules** will act on: the structure's
        own expiry (B2 settles it there) or, if that is unknown, the policy's
        nearest expiry as of the bar clock. Never raises: a calendar hiccup
        leaves ``next_expiry`` as ``None`` rather than taking out the caller
        (this runs inside the 1 Hz SSE snapshot).
        """
        rows: list[dict[str, Any]] = []
        for structure in self.option_broker.get_open_structures():
            legs = list(structure.legs)
            units = max((leg.total_quantity for leg in legs), default=0)
            lots = max((leg.quantity for leg in legs), default=0)

            def _net_premium(price_attr: str) -> float:
                """Signed premium per unit: longs pay, shorts receive."""
                if not units:
                    return 0.0
                total = Decimal("0")
                for leg in legs:
                    price = getattr(leg, price_attr, Decimal("0")) or Decimal("0")
                    total += price * Decimal(str(leg.total_quantity)) * (
                        1 if leg.is_long else -1
                    )
                return float(total / Decimal(str(units)))

            leg_rows = [
                {
                    "option_type": leg.option_type,
                    "side": "LONG" if leg.is_long else "SHORT",
                    "strike": float(leg.strike),
                    "trading_symbol": leg.trading_symbol,
                    "qty": leg.total_quantity,
                    "entry_price": float(leg.entry_price),
                    "current_price": float(leg.current_price),
                    "pnl": float(leg.unrealized_pnl),
                }
                for leg in legs
            ]
            entry = _net_premium("entry_price")
            current = _net_premium("current_price")
            unrealized = float(structure.total_unrealized_pnl)
            rows.append(
                {
                    # Equity-compatible shape (so existing renderers work) …
                    "symbol": f"{structure.underlying} {structure.structure_type}",
                    "label": f"{structure.underlying} {structure.structure_type} "
                    + " ".join(str(leg.strike) for leg in legs),
                    "side": "LONG" if legs and legs[0].is_long else "SHORT",
                    "qty": lots,
                    "units": units,
                    "lot_size": legs[0].lot_size if legs else 0,
                    "entry_price": round(entry, 2),
                    "current_price": round(current, 2),
                    "unrealized_pnl": round(unrealized, 2),
                    "entry_cost": round(entry * units, 2),
                    "open_pnl_pct": round(unrealized / (entry * units), 4)
                    if entry * units
                    else 0.0,
                    "entry_ts": structure.opened_at.isoformat()
                    if structure.opened_at
                    else None,
                    # … plus the option-specific columns (C2).
                    "kind": "option",
                    "structure_id": structure.structure_id,
                    "structure_type": structure.structure_type,
                    "underlying": structure.underlying,
                    "strikes": [float(leg.strike) for leg in legs],
                    "legs": len(legs),
                    "legs_detail": leg_rows,
                    "expiry": self._structure_expiry_iso(structure),
                    "next_expiry": self._structure_expiry_iso(structure),
                    "bars_held": (
                        self._bar_index - self._entry_bar_index
                        if self._entry_bar_index is not None
                        else 0
                    ),
                }
            )
        return rows

    def _structure_expiry_iso(self, structure: Any) -> Optional[str]:
        """The structure's expiry as an ISO date; nearest policy expiry as a
        fallback, and ``None`` if the calendar cannot be read."""
        expiry = getattr(structure, "expiry", None) or self._structure_expiry
        if expiry is None:
            try:
                expiry = self._select_expiry(self._generator(), self.underlying)
            except Exception:  # noqa: BLE001 — reporting must never break the feed
                return None
        if expiry is None:
            return None
        return expiry.isoformat() if hasattr(expiry, "isoformat") else str(expiry)

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
        3. every open leg is re-priced and its unrealized P&L recomputed;
        4. the **mechanical** exit rules (stop, target, days-to-expiry, time
           stop) are evaluated against the fresh mark. Signal rules — flip and
           neutral — need the strategy's view and are evaluated by
           :meth:`on_market_view` instead, so a viewless bar cannot close a
           position on a view it never saw. A close here is queued for
           :meth:`pop_exit_event`.

        Returns the book's total unrealized P&L (post-action), or ``None`` when
        there is nothing open (or the book is empty). Pricing failures are
        logged and swallowed: a bad bar must never kill the runner.
        """
        self._bar_index += 1
        bar_dt = self._bar_datetime(ts)
        if bar_dt is not None or self._bar_dt is None:
            self._bar_dt = bar_dt
        # U2.2: reset re-entries per day when day changes
        try:
            current_day = self._bar_dt.date() if self._bar_dt else None
            if current_day and self._reentry_day != current_day:
                self._reentries_today = 0
                self._reentry_day = current_day
        except Exception:
            pass
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
        except Exception as exc:  # noqa: BLE001 — pricing must never kill the runner
            logger.warning(
                "[options-bridge] MTM failed for %s @ %s: %s", symbol, price, exc
            )
            return None

        self._maybe_risk_exit(strategy_name="")
        self._maybe_expiry_settlement()
        return self.last_unrealized_pnl

    # ------------------------------------------------------------------ #
    # Expiry settlement (task B2)
    # ------------------------------------------------------------------ #

    def _expiry(self) -> ExpiryManager:
        """The canonical expiry pipeline, bound to this bridge's own book.

        Reused rather than reimplemented so the forward loop settles exactly
        like the backtest driver and the dashboard: cash settlement at the
        spot settlement price, ``expiry_settlement`` stamped on the structure,
        and the broker's close observers fired (persistence).
        """
        if self._expiry_manager is None:
            self._expiry_manager = ExpiryManager(
                self.option_broker,
                squareoff_minutes_before=self.squareoff_minutes_before,
            )
        return self._expiry_manager

    def _maybe_expiry_settlement(self) -> Optional[dict[str, Any]]:
        """Cash-settle a structure whose expiry the bar clock has reached.

        Runs on every bar after the mark-to-market, and settles **on** the
        expiry bar (``include_today=True``): index options settle on the
        expiry-day close, which is exactly the price the bar carries. Only
        relevant when the exit policy rides into settlement
        (``min_days_to_expiry: null``) — otherwise the DTE rule has already
        squared the position off the day before.
        """
        if self._bar_dt is None or not self._has_open_structure():
            return None
        structure = self.option_broker.get_structure(self.open_structure_id or "")
        if structure is None or structure.expiry is None:
            return None

        try:
            report = self._expiry().process_expiries(
                self.quote_provider,
                _GeneratorSettlementProvider(self._generator(), self.underlying),
                as_of=self._bar_dt,
                include_today=True,
            )
        except Exception as exc:  # noqa: BLE001 — a settlement failure must not kill the runner
            logger.warning("[options-bridge] expiry settlement failed: %s", exc)
            return None

        if not report.get("settled_count") and not report.get("squared_off_count"):
            return None
        return self._record_settlement(structure, report)

    def _record_settlement(
        self, structure: Any, report: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        """Turn a settlement report into the same event shape as a close."""
        settled = bool(report.get("settled_count"))
        realized = structure.total_realized_pnl
        event = {
            "exited": True,
            "structure_id": structure.structure_id,
            "structure_type": structure.structure_type,
            "underlying": structure.underlying,
            "direction": _label(self._structure_direction),
            "reason": EXIT_REASON_SETTLEMENT if settled else EXIT_DTE,
            "detail": (
                f"cash settled at expiry {structure.expiry}"
                if settled
                else "squared off into expiry"
            ),
            "pnl": float(realized),
            "bars_held": self.bars_in_trade,
            "strategy_name": "",
            "settled": settled,
        }

        self.open_structure_id = None
        self.closed_count += 1
        self._settlement_count += 1
        self.last_exit = event
        self._exit_bar_index = self._bar_index
        self._entry_bar_index = None
        self._bars_without_view = 0
        self._structure_direction = None
        self._structure_expiry = None
        self._entry_premium = Decimal("0")
        self.last_unrealized_pnl = self.option_broker.total_unrealized_pnl
        self._exit_events.append(event)

        logger.info(
            "[options-bridge] %s %s — pnl ₹%.2f (%d settled, %d squared off)",
            "settled" if settled else "squared off",
            structure.structure_id[:8],
            float(realized),
            report.get("settled_count", 0),
            report.get("squared_off_count", 0),
        )
        return event

    def _maybe_risk_exit(self, strategy_name: str) -> Optional[dict[str, Any]]:
        """Apply the view-independent exit rules to the freshly marked book."""
        decision = self._evaluate_exit(view=None)
        if decision is None or decision.reason not in RISK_REASONS:
            return None
        return self._exit_structure(
            decision, self._bar_ts(self._bar_dt), strategy_name, queue=True
        )

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
    def _bar_ts(bar_dt: Optional[datetime]) -> Optional[str]:
        """ISO timestamp for the bar clock (``None`` → provider wall clock)."""
        return bar_dt.isoformat() if bar_dt is not None else None

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

    # ------------------------------------------------------------------ #
    # Exits (task B1)
    # ------------------------------------------------------------------ #

    def _manage_open_structure(
        self, view: MarketView | None, strategy_name: str
    ) -> dict[str, Any] | None:
        """Evaluate the exit policy for the open structure and act on it.

        Returns the exit event when a structure was closed, else ``None``.
        Also maintains the neutral-bar counter (bars without a directional
        view), which the neutral rule needs.
        """
        if view is None or self._is_neutral_view(view):
            self._bars_without_view += 1
        else:
            self._bars_without_view = 0

        decision = self._evaluate_exit(view)
        if decision is None:
            return None
        # queue=False: this caller hands the event straight back to the runner,
        # so it must not also sit in the per-bar drain queue.
        return self._exit_structure(
            decision, self._bar_ts(self._bar_dt), strategy_name, queue=False
        )

    def _evaluate_exit(self, view: MarketView | None) -> Any:
        """Ask the policy; `None` means keep holding."""
        structure = self.option_broker.get_structure(self.open_structure_id or "")
        if structure is None:
            return None
        try:
            return self.exit_policy.evaluate(
                view=view,
                structure_direction=self._structure_direction,
                unrealized_pnl=structure.total_unrealized_pnl,
                basis=self._entry_premium,
                bars_held=self.bars_in_trade,
                bars_without_view=self._bars_without_view,
                bar_date=self._bar_dt.date() if self._bar_dt else None,
                expiry=structure.expiry,
            )
        except Exception as exc:  # noqa: BLE001 — a bad rule must not block trading
            logger.warning("[options-bridge] exit evaluation failed: %s", exc)
            return None

    def _exit_structure(
        self,
        decision: Any,
        ts: Optional[str],
        strategy_name: str,
        queue: bool = True,
    ) -> Optional[dict[str, Any]]:
        """Close the open structure at the current mark; record the event.

        ``queue=True`` (the per-bar risk path) leaves the event in
        :attr:`_exit_events` for :meth:`pop_exit_event`; ``queue=False`` (a
        caller that returns the event itself) does not.
        """
        structure_id = self.open_structure_id
        if structure_id is None:
            return None
        structure = self.option_broker.get_structure(structure_id)
        if structure is None:
            self.open_structure_id = None
            return None

        bars_held = self.bars_in_trade
        try:
            realized = self.option_broker.close_structure(
                structure_id,
                self.quote_provider,
                timestamp=self._bar_datetime(ts),
                reason=decision.reason,
            )
        except Exception as exc:  # noqa: BLE001 — a failed close must not kill the runner
            logger.warning("[options-bridge] close failed for %s: %s", structure_id[:8], exc)
            return None

        event = {
            "exited": True,
            "structure_id": structure_id,
            "structure_type": structure.structure_type,
            "underlying": structure.underlying,
            "direction": _label(self._structure_direction),
            "reason": decision.reason,
            "detail": decision.detail,
            "pnl": float(realized),
            "bars_held": bars_held,
            "strategy_name": strategy_name,
        }
        self.open_structure_id = None
        self.closed_count += 1
        self.last_exit = event
        self._exit_bar_index = self._bar_index
        self._entry_bar_index = None
        self._bars_without_view = 0
        self._structure_direction = None
        self._structure_expiry = None
        self._entry_premium = Decimal("0")
        if queue:
            self._exit_events.append(event)
        # The legs are closed, so the open-leg mark is gone. Keep the summary
        # honest for the rest of this bar.
        self.last_unrealized_pnl = self.option_broker.total_unrealized_pnl

        logger.info(
            "[options-bridge] closed %s (%s) after %d bars — pnl ₹%.2f — %s",
            structure.structure_type,
            structure_id[:8],
            bars_held,
            float(realized),
            decision.detail or decision.reason,
        )
        return event

    def _select_expiry(self, generator: Any, underlying: str) -> Optional[Any]:
        """Nearest expiry **as of the bar clock** (falls back to today).

        Replay-anchored selection is what makes days-to-expiry rules and the
        expiry calendar mean anything in a forward test: on a historical bar
        the relevant expiry is the one then-current, and once the replay clock
        rolls past it the next month's expiry takes over automatically.
        """
        reference = self._bar_dt.date() if self._bar_dt is not None else None
        try:
            return NearestExpiryPolicy().select_expiry(
                generator.available_expiries(underlying, reference=reference),
                reference_date=reference,
            )
        except TypeError:
            # Custom generators may not accept ``reference`` — keep working.
            return NearestExpiryPolicy().select_expiry(generator.available_expiries(underlying))

    def _entry_dte_block(self) -> Optional[dict[str, Any]]:
        """Refuse to open a structure the expiry rule would immediately close.

        If the policy squares off within ``min_days_to_expiry`` days of expiry
        (or the expiry has already passed on the replay clock), opening a fresh
        structure there would be a one-bar trade — so say so instead.
        """
        min_dte = self.exit_policy.config.min_days_to_expiry
        if min_dte is None or self._bar_dt is None:
            return None
        generator = self._generator()
        try:
            expiry = self._select_expiry(generator, self.underlying)
        except Exception:  # noqa: BLE001 — never block on a calendar hiccup
            return None
        if expiry is None:
            return None
        days = (expiry - self._bar_dt.date()).days
        if days > min_dte:
            return None
        return {
            "rejected": True,
            "reason": f"{days}d to expiry {expiry} ≤ {min_dte}d — new entries paused",
        }

    def _should_reenter(self, view: MarketView | None) -> bool:
        """Flip-close + ``reenter`` → open reverse NEXT bar only (never same-bar) — U2.2."""
        if view is None or self.last_exit is None:
            return False
        if self.last_exit.get("reason") != EXIT_SIGNAL_FLIP:
            return False
        # Next-bar only: same-bar re-entry impossible even if reenter=true
        if self._exit_bar_index is not None and self._exit_bar_index == self._bar_index:
            return False
        # V1.1 knob: max_reentries_per_day default 2
        max_per_day = self.expression.get("exit", {}).get("max_reentries_per_day", 2)
        if hasattr(self, "_reentries_today") and self._reentries_today >= max_per_day:
            return False
        return self.exit_policy.should_reenter(self.last_exit.get("direction"), view)

    def _blocked_reentry(self, view: MarketView | None = None) -> bool:
        """True on the bar a rule closed a structure — no same-bar re-entry.

        Without this, a stop-out would immediately re-open the same structure
        while the strategy is still bullish, which reads as "the stop did
        nothing". ``reenter`` only bypasses it for a genuine flip.
        """
        if self._exit_bar_index is None:
            return False
        # U2.2: same-bar re-entry impossible, ever — even flip+reenter must wait to next bar
        return self._exit_bar_index == self._bar_index

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

        expiry = self._select_expiry(generator, underlying)
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
        # Entry bookkeeping for the exit rules (B1): age, direction, and the
        # net premium that percentage stops/targets are measured against.
        self._entry_bar_index = self._bar_index
        self._exit_bar_index = None
        self._bars_without_view = 0
        self._structure_direction = view.direction
        self._structure_expiry = expiry
        opened = self.option_broker.get_structure(self.open_structure_id or "")
        self._entry_premium = opened.total_entry_cost if opened is not None else Decimal("0")
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
    def _is_neutral_view(view: MarketView) -> bool:
        """True when a view carries no direction (NEUTRAL counts as silence)."""
        direction = getattr(view, "direction", None)
        if direction is None:
            return True
        return _label(direction).lower() == "neutral"

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


class _GeneratorSettlementProvider:
    """Settlement price = the quote generator's current spot (B2).

    Mirrors ``engine/option_backtest_driver.py``'s provider of the same name:
    both replay loops settle against the spot the synthetic market is standing
    at on the expiry bar.
    """

    def __init__(self, generator: Any, underlying: str = "NIFTY") -> None:
        self.generator = generator
        self.underlying = underlying

    def get_settlement_price(self, underlying: str) -> float:
        return float(self.generator.get_spot(underlying or self.underlying))
