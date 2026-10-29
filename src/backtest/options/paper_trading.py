"""Options paper trading — multi-leg order execution and position tracking.

Extends the equity ``PaperBroker`` with option-specific logic:

- Multi-leg atomic execution (all-or-nothing)
- OptionPosition tracking with structure_id grouping
- MTM (mark-to-market) calculation from live quotes
- Lot size validation
- Premium calculation

Usage::

    from backtest.options.paper_trading import OptionPaperBroker, OptionPosition

    broker = OptionPaperBroker(capital=1_000_000)
    fills = broker.execute_structure(trade_intent, quote_provider)
"""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from enum import Enum
from typing import Any, Protocol

from backtest.live.market_status import IST, MARKET_CLOSE_TIME, MARKET_OPEN_TIME
from backtest.timekeeping import from_market_clock, from_stored_utc, now_utc
from backtest.options.execution_gate import V1_ALLOWED_STRUCTURES, check_intent
from backtest.strategy.intent import (
    OptionLeg,
    TradeIntent,
)

logger = logging.getLogger("backtest.options.paper_trading")

ZERO = Decimal("0")

#: How old a held leg's last successful mark may get before the mark counts as
#: stale (PRD §4.1/§8.1 freshness SLA). Measured in market-session seconds, not
#: bar counts: a feed that stops delivering bars produces no *failed* marks at
#: all, so counting failures never notices the freeze. Sized to clear the R6
#: watchdog (60 s pass, 120 s repair age) so a healthy book never trips it;
#: calibrate per underlying against the soak's measured latency distribution.
STALE_AFTER_SECONDS = 180.0

#: Marks may age only while the exchange is trading (§4.4: the exchange clock is
#: authoritative), so the session window is part of the metric, not a filter.
_SESSION_OPEN = MARKET_OPEN_TIME
_SESSION_CLOSE = MARKET_CLOSE_TIME
#: Days of session time an age may span before the arithmetic stops counting.
#: A mark that old is past every SLA in use, and the loop stays bounded.
_MAX_AGE_DAYS = 30


def _now_utc() -> datetime:
    """The freshness clock's single 'now' — patch this to test ageing deterministically."""
    return datetime.now(timezone.utc)


def _utc(dt: datetime | None) -> datetime | None:
    """Normalise a timestamp to aware UTC for the freshness comparison.

    Marks are written aware UTC now; a naive value reaching here is a legacy
    row or a pre-fix in-memory object, and is read as UTC because that is what
    ``utcnow()``/``isoformat()`` produced before the 2026-10-27 clock fix.
    """
    return from_stored_utc(dt)


def _session_seconds_between(mark: datetime, reference: datetime) -> float:
    """Seconds of NSE session time in ``[mark, reference]`` (both aware UTC).

    Time outside 09:15–15:30 IST is not counted: nobody can refresh a mark while
    the exchange is shut, so a book carried overnight has not aged. Weekends are
    skipped by the weekday rule; a market holiday is counted as a full session
    because the holiday calendar is a DB read and this runs on every dashboard
    row — over-reporting age errs toward halting, which is the safe side.
    """
    start = mark.astimezone(IST)
    end = reference.astimezone(IST)
    if end <= start:
        return 0.0
    total = 0.0
    day = start.date()
    sessions = 0
    while day <= end.date() and sessions < _MAX_AGE_DAYS:
        if day.weekday() < 5:
            open_dt = datetime.combine(day, _SESSION_OPEN, tzinfo=IST)
            close_dt = datetime.combine(day, _SESSION_CLOSE, tzinfo=IST)
            total += max(
                0.0,
                (min(close_dt, end) - max(open_dt, start)).total_seconds(),
            )
            sessions += 1
        day += timedelta(days=1)
    return total


def session_is_open(now: datetime | None = None) -> bool:
    """Whether the NSE session is trading at ``now`` (naive timestamps are UTC).

    Pure clock arithmetic: the freshness gate asks this on every mark, so it may
    not reach for the holiday master, which is a database read. Weekends are shut
    by the weekday rule and a market holiday counts as open — over-reporting
    staleness halts entries, which is the safe direction.
    """
    reference = _utc(now) or _now_utc()
    ist_time = reference.astimezone(IST).time()
    return _SESSION_OPEN <= ist_time <= _SESSION_CLOSE


def _age_seconds(last_updated: datetime | None, now: datetime | None) -> float | None:
    """Market-session seconds since ``last_updated``, or ``None`` with no mark."""
    stamp = _utc(last_updated)
    if stamp is None:
        return None
    reference = _utc(now) or _now_utc()
    return max(0.0, _session_seconds_between(stamp, reference))


def _iso_utc(dt: datetime | None) -> str | None:
    """Format ``dt`` as an unambiguous ISO-8601 UTC string."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc).isoformat()
    return dt.astimezone(timezone.utc).isoformat()


def _ist_session_date(dt: datetime | None) -> str | None:
    """Map ``dt`` to its IST calendar date ``YYYY-MM-DD`` (naive treated as UTC)."""
    stamp = _utc(dt)
    if stamp is None:
        return None
    return stamp.astimezone(IST).date().isoformat()


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class PositionStatus(Enum):
    OPEN = "open"
    CLOSED = "closed"
    EXPIRED = "expired"


class OrderFillStatus(Enum):
    PENDING = "pending"
    FILLED = "filled"
    PARTIAL = "partial"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class OptionPosition:
    """A single option position (one leg of a structure).

    Positions are grouped by ``structure_id`` to track multi-leg structures
    as a unit for P&L and closing purposes.
    """

    position_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    structure_id: str = ""
    strategy_name: str = ""

    # Instrument details
    instrument_token: str = ""
    trading_symbol: str = ""
    underlying: str = ""
    option_type: str = ""  # CE or PE
    strike: Decimal = ZERO
    expiry: date | None = None
    lot_size: int = 25

    # Position
    side: str = ""  # BUY or SELL
    quantity: int = 0  # number of lots
    entry_price: Decimal = ZERO  # premium per unit
    current_price: Decimal = ZERO  # latest MTM price
    status: PositionStatus = PositionStatus.OPEN

    # P&L
    realized_pnl: Decimal = ZERO
    unrealized_pnl: Decimal = ZERO
    commission: Decimal = ZERO

    # Timestamps
    opened_at: datetime = field(default_factory=_now_utc)
    closed_at: datetime | None = None
    last_updated: datetime = field(default_factory=_now_utc)

    # Mark freshness observability (MTM Staleness PRD R1)
    mtm_failures: int = 0
    last_quote_error: str | None = None

    # Metadata
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def mark_ts(self) -> str | None:
        """ISO-8601 UTC timestamp of the last successful quote mark (R1)."""
        return _iso_utc(self.last_updated)

    def mark_age_seconds(self, now: datetime | None = None) -> float | None:
        """Market-session seconds since this leg's last mark (``None`` if never marked)."""
        if self.status != PositionStatus.OPEN:
            return None
        return _age_seconds(self.last_updated, now)

    def is_mark_stale(
        self,
        stale_after_seconds: float = STALE_AFTER_SECONDS,
        now: datetime | None = None,
    ) -> bool:
        """True when the last successful mark is older than the freshness SLA.

        Session time, not failed attempts: a stalled feed stops producing bars
        (and so stops producing failures), which is exactly the case the SLA
        must catch.
        """
        age = self.mark_age_seconds(now)
        return (
            self.status == PositionStatus.OPEN
            and age is not None
            and age > float(stale_after_seconds)
        )

    @property
    def total_quantity(self) -> int:
        """Total units (lots × lot_size)."""
        return self.quantity * self.lot_size

    @property
    def notional_value(self) -> Decimal:
        """Notional = entry_price × total_quantity."""
        return self.entry_price * Decimal(str(self.total_quantity))

    @property
    def is_long(self) -> bool:
        return self.side == "BUY"

    @property
    def is_short(self) -> bool:
        return self.side == "SELL"

    def calculate_unrealized_pnl(self) -> Decimal:
        """Calculate unrealized P&L based on current_price."""
        if self.status != PositionStatus.OPEN:
            return ZERO

        if self.is_long:
            # Long: profit when price goes up
            self.unrealized_pnl = (self.current_price - self.entry_price) * Decimal(
                str(self.total_quantity)
            )
        else:
            # Short: profit when price goes down
            self.unrealized_pnl = (self.entry_price - self.current_price) * Decimal(
                str(self.total_quantity)
            )
        return self.unrealized_pnl

    def close(self, exit_price: Decimal, timestamp: datetime | None = None) -> Decimal:
        """Close the position and calculate realized P&L.

        Returns the realized P&L (gross of commission).
        """
        if self.status != PositionStatus.OPEN:
            return ZERO

        self.current_price = exit_price
        if self.is_long:
            self.realized_pnl = (exit_price - self.entry_price) * Decimal(
                str(self.total_quantity)
            )
        else:
            self.realized_pnl = (self.entry_price - exit_price) * Decimal(
                str(self.total_quantity)
            )

        self.status = PositionStatus.CLOSED
        self.closed_at = from_market_clock(timestamp) or now_utc()
        self.last_updated = self.closed_at
        return self.realized_pnl

    def update_mtm(self, price: Decimal) -> Decimal:
        """Update current price and recalculate unrealized P&L."""
        self.current_price = price
        self.last_updated = now_utc()
        self.mtm_failures = 0
        self.last_quote_error = None
        return self.calculate_unrealized_pnl()

    def record_mtm_failure(self, error: str | None = None) -> Decimal:
        """Record a failed quote attempt while holding the last known mark (R1).

        Increments ``mtm_failures`` and records ``last_quote_error`` without
        advancing ``last_updated`` (so ``mark_ts`` remains the timestamp of
        the last *successful* mark).
        """
        self.mtm_failures += 1
        self.last_quote_error = str(error or "empty quote row")
        if self.current_price <= 0 and self.entry_price > 0:
            self.current_price = self.entry_price
        return self.calculate_unrealized_pnl()


@dataclass
class StructurePosition:
    """Groups related OptionPositions by structure_id for multi-leg tracking.

    A structure (e.g. BullCallSpread) has 2+ legs that are opened and
    closed together.  This tracks the aggregate P&L and status.
    """

    structure_id: str
    structure_type: str
    strategy_name: str
    underlying: str
    expiry: date
    legs: list[OptionPosition] = field(default_factory=list)
    opened_at: datetime = field(default_factory=_now_utc)
    closed_at: datetime | None = None
    #: Why the structure closed (T0.2). One of ``"manual"``,
    #: ``"auto_square_off"``, ``"expiry_settlement"`` — or ``None`` while
    #: the structure is still open.
    exit_reason: str | None = None
    #: IST calendar date (``YYYY-MM-DD``) stamped when the structure closes so
    #: ``StrategyRunner.daily_pnl()`` rolls options at ``00:00 IST`` (OPT-6).
    session_date: str | None = None
    #: IST calendar date the structure was OPENED on (BUG-18). Attribution needs
    #: both ends: a monthly leg entered on the 24th and closed on the 29th has a
    #: lifetime P&L that belongs to four different sessions.
    entry_session_date: str | None = None
    #: Session date of the last mark, and the net open P&L booked at that mark.
    #: Together they give ``prior_close_net_pnl`` — the anchor that keeps an
    #: overnight gap out of the next session's ``daily_pnl`` (BUG-18).
    last_mark_session: str | None = None
    last_mark_net_pnl: Decimal = ZERO
    prior_close_net_pnl: Decimal = ZERO
    #: Entry + exit statutory charges attributed to this structure (Recon F4).
    statutory_fees: Decimal = ZERO

    @property
    def is_open(self) -> bool:
        return any(leg.status == PositionStatus.OPEN for leg in self.legs)

    @property
    def total_entry_cost(self) -> Decimal:
        """Net cost to open the structure (debit = positive, credit = negative)."""
        cost = ZERO
        for leg in self.legs:
            leg_cost = leg.entry_price * Decimal(str(leg.total_quantity))
            if leg.is_long:
                cost += leg_cost  # BUY = pay premium
            else:
                cost -= leg_cost  # SELL = receive premium
        return cost

    @property
    def total_unrealized_pnl(self) -> Decimal:
        return sum(leg.unrealized_pnl for leg in self.legs)

    @property
    def total_realized_pnl(self) -> Decimal:
        return sum(leg.realized_pnl for leg in self.legs)

    @property
    def total_commission(self) -> Decimal:
        return sum(leg.commission for leg in self.legs)

    @property
    def gross_pnl(self) -> Decimal:
        """Gross realized P&L across closed/expired legs (before commissions)."""
        return self.total_realized_pnl

    @property
    def net_pnl(self) -> Decimal:
        """Realized P&L net of brokerage commissions and statutory fees (Recon F4)."""
        return self.total_realized_pnl - self.total_commission - self.statutory_fees

    @property
    def effective_session_date(self) -> str | None:
        """IST calendar date of closure, falling back to ``closed_at`` if unstamped."""
        if self.session_date:
            return self.session_date
        if self.closed_at is not None:
            return _ist_session_date(self.closed_at)
        return None

    @property
    def exit_session_date(self) -> str | None:
        """The session the structure left the book on — BUG-18's other half."""
        return self.effective_session_date

    @property
    def carried_overnight(self) -> bool:
        """True when this structure has held across at least one session boundary.

        A deliberate positional carry says so on the row; the trader is not left
        to infer an overnight hold from a settlement number that moved while the
        market was shut.
        """
        if not self.entry_session_date:
            return False
        current = self.last_mark_session or _ist_session_date(_now_utc())
        return self.entry_session_date != current

    @property
    def net_open_pnl(self) -> Decimal:
        """Unrealized P&L net of the charges this structure has already incurred."""
        return self.total_unrealized_pnl - self.total_commission - self.statutory_fees

    @property
    def net_open_pnl_today(self) -> Decimal:
        """Today's slice of :attr:`net_open_pnl` (BUG-18).

        The overnight gap belongs to the session that booked it as open MTM
        yesterday, so subtracting the prior-close anchor keeps the sum of the
        two sessions equal to the lifetime P&L — no double count, no gap.
        """
        return self.net_open_pnl - self.prior_close_net_pnl

    def record_session_mark(self, session: str | None) -> None:
        """Roll the prior-close anchor when the IST session date advances.

        Called once per mark pass. The FIRST mark of a new session promotes the
        last mark of the previous session into ``prior_close_net_pnl``, which is
        exactly the number today's P&L must start from. A mark for an *older*
        session (a repair reaching back to a stale leg) must not move it: the
        anchor tracks the newest session the book has seen, or today's slice
        would silently inherit yesterday's number.
        """
        if session is None:
            return
        if self.last_mark_session is not None and session > self.last_mark_session:
            self.prior_close_net_pnl = self.last_mark_net_pnl
        if self.last_mark_session is not None and session < self.last_mark_session:
            return
        self.last_mark_session = session
        self.last_mark_net_pnl = self.net_open_pnl

    @property
    def mark_ts(self) -> str | None:
        """Oldest successful mark timestamp across open legs (R1)."""
        open_legs = [leg for leg in self.legs if leg.status == PositionStatus.OPEN]
        if not open_legs:
            open_legs = list(self.legs)
        stamps = [_utc(leg.last_updated) for leg in open_legs if leg.last_updated is not None]
        if not stamps:
            return None
        return min(stamps).isoformat()

    @property
    def mtm_failures(self) -> int:
        open_legs = [leg for leg in self.legs if leg.status == PositionStatus.OPEN]
        return max((leg.mtm_failures for leg in open_legs), default=0)

    @property
    def quote_error(self) -> str | None:
        for leg in self.legs:
            if leg.status == PositionStatus.OPEN and leg.last_quote_error:
                return leg.last_quote_error
        return None

    def mark_age_seconds(self, now: datetime | None = None) -> float | None:
        """Oldest mark across open legs — the structure is as fresh as its worst leg."""
        ages = [
            age
            for leg in self.legs
            if leg.status == PositionStatus.OPEN
            for age in (leg.mark_age_seconds(now),)
            if age is not None
        ]
        return max(ages) if ages else None

    def is_mark_stale(
        self,
        stale_after_seconds: float = STALE_AFTER_SECONDS,
        now: datetime | None = None,
    ) -> bool:
        return any(
            leg.is_mark_stale(stale_after_seconds, now)
            for leg in self.legs
            if leg.status == PositionStatus.OPEN
        )


# ---------------------------------------------------------------------------
# Quote provider protocol
# ---------------------------------------------------------------------------

class QuoteProvider(Protocol):
    """Provides live option quotes for MTM calculation."""

    def get_quote(self, instrument_token: str) -> dict[str, Any]:
        """Return ``{"ltp": float, "bid": float, "ask": float, ...}``."""
        ...

    def register_contract(self, contract: Any) -> None:
        """Register an option contract so token → symbol resolution survives restarts (R3)."""
        ...


def _execution_quote(provider: Any, instrument_token: str, side: str) -> dict[str, Any]:
    """Fill-price quote for one leg (PRD §4.1 cross-the-spread seam).

    When the provider offers ``execution_quote`` (the
    :class:`~backtest.options.quote_providers.BidAskQuoteProvider` wrapper the
    runner path installs by default), fills cross the spread — buy at ask, sell
    at bid — and a one-sided or zero book raises ``NoExecutableQuoteError``
    rather than pricing the leg off LTP. Providers without the seam keep the
    older behaviour: fill at LTP.
    """
    execution_quote = getattr(provider, "execution_quote", None)
    if callable(execution_quote):
        quote = execution_quote(instrument_token, side)
        # Guard against duck-typing accidents (e.g. MagicMock providers in
        # tests "have" every attribute): only trust a real quote dict.
        if isinstance(quote, dict):
            return quote
    return provider.get_quote(instrument_token)


def _mark_quote(provider: Any, instrument_token: str, side: str) -> dict[str, Any]:
    """Mark price for a held leg — what closing it right now would fetch.

    A provider with a ``mark_quote`` seam marks conservatively (PRD §4.2): bid
    on a long leg, ask on a short one, never LTP. Providers without it mark off
    LTP as before.
    """
    mark_quote = getattr(provider, "mark_quote", None)
    if callable(mark_quote):
        quote = mark_quote(instrument_token, side)
        if isinstance(quote, dict):
            return quote
    return provider.get_quote(instrument_token)


# ---------------------------------------------------------------------------
# Exchange price grid (BUG-06) — every booked option fill must be printable
# ---------------------------------------------------------------------------

#: NSE option premiums move in ₹0.05. A price off this grid cannot be sent to
#: the venue, so it cannot be a fill either — 733/910 paper fills on 2026-10-28
#: were exactly that: numbers no counterparty ever quoted.
NSE_OPTION_TICK_SIZE = Decimal("0.05")

#: :class:`~backtest.options.quote_providers.BidAskQuoteProvider.source_name`
#: appends this marker when it wraps a provider, i.e. the real live paper path
#: reports ``"live:mstock+bidask"``. Its presence means fills were crossed
#: through the spread by the seam, so the spread has ALREADY been paid.
CROSS_SPREAD_SOURCE_MARKER = "+bidask"

#: Labels naming a price built from a model instead of a venue book. Those
#: quotes carry no real counterparty, so the percentage slippage model stays on.
SYNTHETIC_SOURCE_MARKERS = ("synthetic", "model", "mid")

#: ``OptionPosition.metadata`` key remembering the grid a leg was filled on, so
#: the exit is quantised against the instrument's own tick and not a guess.
TICK_SIZE_METADATA_KEY = "tick_size"


def _to_decimal(value: Any) -> Decimal | None:
    """Best-effort Decimal for quote/contract scalars; ``None`` when not a number."""
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def quantize_nse_tick(
    price: Any,
    side: str,
    tick_size: Decimal = NSE_OPTION_TICK_SIZE,
) -> Decimal:
    """Push ``price`` onto the exchange grid, always in the ADVERSE direction.

    NSE option contracts trade in ₹0.05 steps; a fill at ₹178.08 is not a price,
    it is arithmetic noise (BUG-06). Rounding toward the trader would book a
    fill the venue could never print, so the rounding is deliberately hostile:
    ``BUY`` rounds UP (``ROUND_CEILING``) and ``SELL`` rounds DOWN
    (``ROUND_FLOOR``). The result is always a multiple of ``tick_size``, so a
    paper book stays reproducible as a live order — and any cost the rounding
    adds is a cost a real session would have charged too.

    ``tick_size`` comes from the contract when the model carries one; the
    ₹0.05 default is the NSE derivative grid, NOT a general rule, which is why
    this helper is only reachable from :class:`OptionPaperBroker` (option
    legs) and never from the equity fill paths.
    """
    amount = _to_decimal(price)
    if amount is None:
        return ZERO
    step = _to_decimal(tick_size)
    if step is None or step <= 0:
        step = NSE_OPTION_TICK_SIZE
    rounding = ROUND_FLOOR if str(side).upper() == "SELL" else ROUND_CEILING
    ticks = (amount / step).quantize(Decimal("1"), rounding=rounding)
    return (ticks * step).quantize(step)


def _fills_against_live_depth(quote: Any, side: str, provider: Any) -> bool:
    """True when the leg priced off the venue's own two-sided book.

    Three independent proofs, all required — this is what stops the spread +
    bps double-count without ever letting the engine invent a side:

    1. the cross-the-spread seam stamped the quote (``execution_side``), so
       ``ltp`` IS the ask we bought at / the bid we sold into;
    2. the row is genuinely two-sided (both ``bid`` and ``ask`` positive) —
       a one-sided or errored row falls through to the old model;
    3. the provider's ``source_name`` carries ``+bidask`` and is not
       labelled synthetic/mid — a Black-Scholes book is a model, not depth.
    """
    if not isinstance(quote, dict) or quote.get("error"):
        return False
    if str(quote.get("execution_side", "")).upper() != str(side).upper():
        return False
    bid = _to_decimal(quote.get("bid"))
    ask = _to_decimal(quote.get("ask"))
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return False
    source = str(getattr(provider, "source_name", "") or "")
    if CROSS_SPREAD_SOURCE_MARKER not in source:
        return False
    lowered = source.lower()
    return not any(marker in lowered for marker in SYNTHETIC_SOURCE_MARKERS)


def _top_of_book_quantity(quote: Any, side: str) -> Decimal | None:
    """Visible size at the level being paid for, when the feed actually shows it.

    ``None`` means "no evidence" — never assume a bigger book than the venue
    published, and never assume a smaller one either: with no size the order is
    treated as fitting the top of book, which is the zero-cost default the
    ticket asks for, not an invented impact.
    """
    depth = quote.get("depth") if isinstance(quote, dict) else None
    if not isinstance(depth, dict):
        return None
    levels = depth.get("ask" if str(side).upper() == "BUY" else "bid")
    if not isinstance(levels, (list, tuple)) or not levels:
        return None
    level = levels[0]
    if not isinstance(level, dict):
        return None
    side_price = _to_decimal(quote.get("ask" if str(side).upper() == "BUY" else "bid"))
    level_price = _to_decimal(level.get("price"))
    if side_price is None or level_price is None or level_price != side_price:
        return None  # depth disagrees with the booked side: claim nothing
    for key in ("quantity", "qty", "size"):
        qty = _to_decimal(level.get(key))
        if qty is not None and qty > 0:
            return qty
    return None


def _resolve_tick_size(provider: Any, instrument_token: str, *carriers: Any) -> Decimal:
    """The instrument's own tick, falling back to the NSE option grid.

    Looked up in order: whatever the caller carries (leg / position metadata),
    then the provider's contract registry, then ₹0.05. A contract that says
    otherwise wins — the grid belongs to the instrument, not to this module.
    """
    for carrier in carriers:
        if carrier is None:
            continue
        raw = (
            carrier.get(TICK_SIZE_METADATA_KEY)
            if isinstance(carrier, dict)
            else getattr(carrier, TICK_SIZE_METADATA_KEY, None)
        )
        step = _to_decimal(raw)
        if step is not None and step > 0:
            return step
    try:
        contract: Any = None
        for name in ("get_contract", "contract_for", "find_contract"):
            finder = getattr(provider, name, None)
            if callable(finder):
                contract = finder(instrument_token)
                if contract is not None:
                    break
        if contract is None:
            registry = getattr(provider, "_contracts", None)
            if isinstance(registry, dict):
                contract = registry.get(str(instrument_token))
        step = _to_decimal(getattr(contract, TICK_SIZE_METADATA_KEY, None))
        if step is not None and step > 0:
            return step
    except Exception:  # noqa: BLE001 — a missing registry must never block a fill
        logger.debug("[option-paper] tick lookup failed for %s", instrument_token)
    return NSE_OPTION_TICK_SIZE


class FakeQuoteProvider:
    """Returns a fixed price for all instruments (for testing)."""

    def __init__(self, default_price: float = 100.0) -> None:
        self.default_price = default_price
        self.prices: dict[str, float] = {}
        self._contracts: dict[str, Any] = {}

    def register_contract(self, contract: Any) -> None:
        token = str(getattr(contract, "instrument_token", "") or "")
        if token:
            self._contracts[token] = contract

    def get_quote(self, instrument_token: str) -> dict[str, Any]:
        price = self.prices.get(instrument_token, self.default_price)
        return {"ltp": price, "bid": price - 0.5, "ask": price + 0.5}

    def set_price(self, instrument_token: str, price: float) -> None:
        self.prices[instrument_token] = price


# ---------------------------------------------------------------------------
# OptionPaperBroker
# ---------------------------------------------------------------------------

class OptionPaperBroker:
    """Paper broker for option trading — handles multi-leg structures.

    Unlike the equity ``PaperBroker`` which handles single orders,
    this broker executes complete ``TradeIntent`` structures atomically:
    either all legs fill or none do.

    Parameters
    ----------
    capital:
        Starting capital in ₹.
    slippage_pct:
        Slippage as a percentage (e.g. 0.001 = 0.1%). Applies to fills priced
        off a mid/model quote only — a leg that already crossed a live
        two-sided book pays the spread, not the spread *and* this (BUG-06).
    slippage_ticks:
        Residual impact in WHOLE ticks for fills against live two-sided depth,
        added on top of the spread (default ``0``: the top of book was big
        enough, the spread is the whole cost). Integer ticks keep the fill on
        the exchange grid instead of drifting onto paise.
    max_slippage_ticks:
        Ceiling on the tick count when an order visibly walks the published
        book, so a thin level cannot multiply into an absurd price.
    commission_per_lot:
        Commission per lot in ₹ (flat fee).
    fee_calculator:
        Optional :class:`~backtest.simulator.fees.CommissionCalculator`.
        When supplied, the complete statutory stack (STT, exchange txn,
        SEBI, stamp duty, GST) is computed per leg via
        ``calculate_structure()`` on every execution, alongside the legacy
        ``commission_per_lot`` charge. Costs never block execution: a fee
        calculation error is logged and the trade proceeds.
    allowed_structures:
        Execution-gate whitelist (see
        :mod:`backtest.options.execution_gate`). Defaults to the V1 set —
        one long call or one long put — so a structure the V1 contract forbids
        cannot fill here even when it arrives as a hand-built intent. A
        research profile that genuinely needs spreads passes
        ``RESEARCH_STRUCTURES`` explicitly.
    """

    def __init__(
        self,
        capital: float = 1_000_000.0,
        slippage_pct: float = 0.001,
        commission_per_lot: float = 20.0,
        fee_calculator: Any | None = None,
        on_structure_opened: Any | None = None,
        on_structure_closed: Any | None = None,
        on_leg_fill: Any | None = None,
        allowed_structures: Any | None = None,
        slippage_ticks: int = 0,
        max_slippage_ticks: int = 5,
    ) -> None:
        self.capital = Decimal(str(capital))
        self.available_cash = Decimal(str(capital))
        self.slippage_pct = Decimal(str(slippage_pct))
        self.commission_per_lot = Decimal(str(commission_per_lot))
        self.fee_calculator = fee_calculator
        #: whole-tick residual impact on top of a crossed live book (BUG-06)
        self.slippage_ticks = max(0, int(slippage_ticks))
        self.max_slippage_ticks = max(self.slippage_ticks, int(max_slippage_ticks))
        self._statutory_fees_paid = ZERO
        self.allowed_structures = frozenset(
            allowed_structures
            if allowed_structures is not None
            else V1_ALLOWED_STRUCTURES
        )

        # Gap G4.2 observers — fired after every state transition so the
        # persistence layer (or tests) can mirror the book. Exceptions in an
        # observer never block execution.
        #: ``fn(structure, entry_fees: Decimal)`` after a successful open.
        self.on_structure_opened = on_structure_opened
        #: ``fn(structure, realized_pnl: Decimal)`` after close/settlement.
        self.on_structure_closed = on_structure_closed
        #: ``fn(record: dict)`` once per leg, on entry and on exit. This is how
        #: an option fill reaches the shared :class:`OrderLedger` (the Orders
        #: tab) without going through the equity fill router.
        self.on_leg_fill = on_leg_fill

        # Position tracking
        self._positions: dict[str, OptionPosition] = {}  # position_id -> position
        self._structures: dict[str, StructurePosition] = {}  # structure_id -> structure
        self._order_history: list[dict[str, Any]] = []

        # Deterministic ID sequences (A6): counters instead of uuid4 so a
        # backtest run is reproducible. Uniqueness holds within one broker
        # instance — which is the only scope a broker's ids need.
        self._structure_seq = 0
        self._position_seq = 0

    # ------------------------------------------------------------------
    # Core execution
    # ------------------------------------------------------------------

    def execute_structure(
        self,
        intent: TradeIntent,
        quote_provider: QuoteProvider,
        timestamp: datetime | None = None,
    ) -> list[OptionPosition]:
        """Execute a complete option structure atomically.

        All legs fill together or the entire structure is rejected.

        Parameters
        ----------
        intent:
            The trade intent from the expression layer.
        quote_provider:
            Provides live quotes for fill prices.
        timestamp:
            Trade timestamp (defaults to now).

        Returns
        -------
        List of ``OptionPosition`` objects created.

        Raises
        ------
        StructureNotAllowedError
            If the intent fails the execution gate (see ``allowed_structures``).
        InsufficientMarginError
            If available cash is insufficient for the net debit.
        LotSizeValidationError
            If any leg has invalid lot size.
        """
        check_intent(intent, self.allowed_structures)

        # ``timestamp`` is the caller's clock: the live bridge hands over its
        # bar clock (aware UTC), the backtest driver and the tests hand over bar
        # times (naive IST). from_market_clock declares that second form instead
        # of refusing it, which is what made 27 tests die on 2026-10-27.
        ts = from_market_clock(timestamp) or _now_utc()

        # Validate lot sizes
        for leg in intent.legs:
            self._validate_lot_size(leg)

        # Get fill prices for all legs
        fills: list[tuple] = []
        net_cost = ZERO
        for leg in intent.legs:
            quote = _execution_quote(quote_provider, leg.instrument_token, leg.side)
            quoted_price = Decimal(str(quote.get("ltp", 0)))
            tick = _resolve_tick_size(quote_provider, leg.instrument_token, leg)
            fill_price = self._apply_slippage(
                quoted_price,
                leg.side,
                quote=quote,
                provider=quote_provider,
                quantity=int(leg.total_quantity),
                tick_size=tick,
            )
            # Unpriceable leg guard (live-session lesson 2026-09-21): a
            # transient broker failure surfaces as ltp=0. Filling at 0
            # books a phantom ₹0 entry and the exit then books the WHOLE
            # premium as profit. A live market never sells an option for
            # ₹0 — reject the structure instead of filling blind.
            if fill_price <= 0:
                raise InsufficientMarginError(
                    f"no live quote for {leg.trading_symbol or leg.instrument_token} "
                    "(ltp=0) — structure rejected rather than filled at a phantom price"
                )
            leg_cost = fill_price * Decimal(str(leg.total_quantity))
            commission = self.commission_per_lot * Decimal(str(leg.quantity))

            if leg.side == "BUY":
                net_cost += leg_cost + commission
            else:
                net_cost -= leg_cost - commission

            fills.append(
                (
                    leg,
                    fill_price,
                    commission,
                    quoted_price,
                    self._fill_basis(quote, leg.side, quote_provider),
                )
            )

        # Full statutory fee stack (Phase 8) — computed per leg when a fee
        # calculator is attached. Cash-bearing (Gap G4.1): the total is
        # debited from ``available_cash`` the same way a real broker's
        # contract note hits the ledger, and is excluded from equity via the
        # ``total_statutory_fees_paid`` term in :attr:`total_equity`. Fees
        # never block execution: a calculation error is logged and the trade
        # proceeds without statutory charges.
        statutory_fees = ZERO
        if self.fee_calculator is not None:
            try:
                fee_breakdown = self.fee_calculator.calculate_structure(
                    [
                        {
                            "side": leg.side,
                            "quantity": leg.total_quantity,
                            "price": fill_price,
                        }
                        for leg, fill_price, _, _, _ in fills
                    ],
                    when=ts,
                )
                statutory_fees = Decimal(str(fee_breakdown.total))
                self._statutory_fees_paid += statutory_fees
            except Exception:
                logger.exception(
                    "[option-paper] fee calculation failed for %s — "
                    "executing without statutory fees",
                    intent.structure_type,
                )

        # Check margin — net premium + commissions + the statutory stack.
        if net_cost + statutory_fees > self.available_cash:
            raise InsufficientMarginError(
                f"Need ₹{net_cost + statutory_fees:,.2f} "
                f"but only ₹{self.available_cash:,.2f} available"
            )

        # Atomic execution — create all positions.
        # Deterministic ids: timestamp prefix (the injected bar time) plus a
        # per-broker monotonic counter — NOT hash(intent): TradeIntent is a
        # frozen dataclass carrying a dict field, so hash() raises TypeError.
        self._structure_seq += 1
        structure_id = f"struct_{ts:%Y%m%d%H%M%S}_{self._structure_seq:04d}"
        positions = []

        # Per-leg strike lookup: metadata may carry a single "strike" (single-leg)
        # or a "strikes" dict keyed by trading_symbol (multi-leg spreads).
        strikes_map = intent.metadata.get("strikes", {})
        default_strike = intent.metadata.get("strike", "0")

        for leg, fill_price, commission, quoted_price, fill_basis in fills:
            self._position_seq += 1
            raw_opt_type = str(intent.metadata.get("option_type", "") or "").upper()
            if raw_opt_type not in ("CE", "PE"):
                sym_up = str(leg.trading_symbol or "").upper()
                if sym_up.endswith("CE"):
                    raw_opt_type = "CE"
                elif sym_up.endswith("PE"):
                    raw_opt_type = "PE"
                else:
                    raw_opt_type = str(intent.metadata.get("option_type", ""))
            position = OptionPosition(
                position_id=f"pos_{ts:%Y%m%d%H%M%S}_{self._position_seq:04d}",
                structure_id=structure_id,
                strategy_name=intent.strategy_name,
                instrument_token=leg.instrument_token,
                trading_symbol=leg.trading_symbol,
                underlying=intent.view.underlying,
                option_type=raw_opt_type,
                strike=Decimal(str(strikes_map.get(leg.trading_symbol, default_strike))),
                expiry=intent.expiry,
                lot_size=leg.lot_size,
                side=leg.side,
                quantity=leg.quantity,
                entry_price=fill_price,
                current_price=fill_price,
                commission=commission,
                opened_at=ts,
                metadata={
                    "structure_type": intent.structure_type,
                    "fill_price": str(fill_price),
                    # the grid this leg filled on, remembered so the exit is
                    # quantised against the same instrument tick (BUG-06)
                    TICK_SIZE_METADATA_KEY: str(
                        _resolve_tick_size(quote_provider, leg.instrument_token, leg)
                    ),
                    # how the fill was priced: "live_depth" crossed a real
                    # two-sided book (spread only), "model" paid the bps model
                    "fill_basis": fill_basis,
                },
            )
            self._positions[position.position_id] = position
            positions.append(position)
            self._fire_leg_fill(
                {
                    "phase": "entry",
                    "structure_id": structure_id,
                    "position_id": position.position_id,
                    "structure_type": intent.structure_type,
                    "underlying": intent.view.underlying,
                    "symbol": leg.trading_symbol or leg.instrument_token,
                    "instrument_token": leg.instrument_token,
                    "side": leg.side,
                    "lots": int(leg.quantity),
                    "lot_size": int(leg.lot_size),
                    "quantity": int(leg.total_quantity),
                    "price": fill_price,
                    "requested_price": quoted_price,
                    "commission": commission,
                    "ts": ts,
                }
            )

        # Create structure
        structure = StructurePosition(
            structure_id=structure_id,
            structure_type=intent.structure_type,
            strategy_name=intent.strategy_name,
            underlying=intent.view.underlying,
            expiry=intent.expiry,
            legs=positions,
            opened_at=ts,
            entry_session_date=_ist_session_date(ts),
            statutory_fees=statutory_fees,
        )
        self._structures[structure_id] = structure

        # Update cash — full cost including the statutory stack.
        self.available_cash -= net_cost + statutory_fees

        # Log
        self._log_fill(intent, structure_id, fills, ts)

        self._fire_structure_opened(structure, statutory_fees)

        logger.info(
            "[option-paper] Executed %s structure=%s legs=%d cost=₹%.2f cash=₹%.2f",
            intent.structure_type,
            structure_id[:8],
            len(fills),
            float(net_cost),
            float(self.available_cash),
        )

        return positions

    def close_structure(
        self,
        structure_id: str,
        quote_provider: QuoteProvider,
        timestamp: datetime | None = None,
        reason: str = "manual",
    ) -> Decimal:
        """Close all legs of a structure atomically.

        Returns the total realized P&L (gross of commission).

        ``reason`` is recorded on the structure's ``exit_reason`` so a
        trade log can say *why* a structure closed — ``"manual"`` (the
        default, e.g. a strategy signal or a UI button),
        ``"auto_square_off"`` (pre-expiry DTE exit) or
        ``"expiry_settlement"`` (cash settlement at expiry).
        """
        structure = self._structures.get(structure_id)
        if structure is None:
            raise ValueError(f"Structure {structure_id} not found")

        ts = from_market_clock(timestamp) or _now_utc()
        total_pnl = ZERO

        open_legs = [
            position for position in structure.legs
            if position.status == PositionStatus.OPEN
        ]
        exit_fees = ZERO
        closing_fills: list[dict[str, Any]] = []

        for position in open_legs:
            quote = _execution_quote(
                quote_provider,
                position.instrument_token,
                "SELL" if position.is_long else "BUY",
            )
            is_failed_quote = (
                not isinstance(quote, dict)
                or not quote
                or bool(quote.get("error"))
            )
            if is_failed_quote:
                # Fail-closed exit: if the quote provider fails during a close,
                # settle at the last known mark (or entry price) rather than
                # booking a fake ₹0.00 fill.
                quoted_price = (
                    position.current_price
                    if position.current_price > 0
                    else position.entry_price
                )
            else:
                quoted_price = Decimal(str(quote.get("ltp", 0) or 0))
            exit_side = "SELL" if position.is_long else "BUY"
            exit_tick = _resolve_tick_size(
                quote_provider, position.instrument_token, position.metadata, position
            )
            exit_price = self._apply_slippage(
                quoted_price,
                exit_side,
                quote=quote if isinstance(quote, dict) else None,
                provider=quote_provider,
                quantity=int(position.total_quantity),
                tick_size=exit_tick,
            )

            pnl = position.close(exit_price, ts)
            total_pnl += pnl

            # Credit back to cash
            if position.is_long:
                # Selling long position → receive cash
                self.available_cash += exit_price * Decimal(str(position.total_quantity))
            else:
                # Buying back short position → pay cash
                self.available_cash -= exit_price * Decimal(str(position.total_quantity))

            closing_fills.append(
                {
                    "side": exit_side,
                    "quantity": position.total_quantity,
                    "price": exit_price,
                }
            )
            self._fire_leg_fill(
                {
                    "phase": "exit",
                    "structure_id": structure.structure_id,
                    "position_id": position.position_id,
                    "structure_type": structure.structure_type,
                    "underlying": structure.underlying,
                    "symbol": position.trading_symbol or position.instrument_token,
                    "instrument_token": position.instrument_token,
                    "side": exit_side,
                    "lots": int(position.quantity),
                    "lot_size": int(position.lot_size),
                    "quantity": int(position.total_quantity),
                    "price": exit_price,
                    "requested_price": quoted_price,
                    "commission": position.commission,
                    "ts": ts,
                }
            )

        # Exit-side statutory stack (Gap G4.1): a real contract note charges
        # brokerage and sell-side STT on closing orders too. Same policy as
        # the entry side — computed when a calculator is attached, cash
        # debited, never blocking the close.
        if self.fee_calculator is not None and closing_fills:
            try:
                fee_breakdown = self.fee_calculator.calculate_structure(
                    closing_fills,
                    when=ts,
                )
                exit_fees = Decimal(str(fee_breakdown.total))
                self._statutory_fees_paid += exit_fees
                self.available_cash -= exit_fees
            except Exception:
                logger.exception(
                    "[option-paper] exit fee calculation failed for %s — "
                    "closing without statutory fees",
                    structure_id[:8],
                )

        structure.closed_at = ts
        structure.exit_reason = reason
        structure.session_date = _ist_session_date(ts)
        structure.statutory_fees += exit_fees

        self._fire_structure_closed(structure, total_pnl)

        logger.info(
            "[option-paper] Closed structure=%s pnl=₹%.2f fees=₹%.2f cash=₹%.2f",
            structure_id[:8],
            float(total_pnl),
            float(exit_fees),
            float(self.available_cash),
        )

        return total_pnl

    def update_mtm(
        self,
        quote_provider: QuoteProvider,
        timestamp: datetime | None = None,
    ) -> Decimal:
        """Update MTM for all open positions.

        Returns total unrealized P&L across all open positions.

        ``timestamp`` is accepted for interface symmetry with
        :meth:`execute_structure` / :meth:`close_structure`; MTM updates
        always stamp positions with wall-clock time.

        Atomic structure marking: for multi-leg structures, quotes are fetched
        for all open legs first. If any leg of a structure fails its quote,
        none of the legs in that structure overwrite ``current_price`` (avoiding
        a partial-leg "Frankenstein spread" mark), while failing legs increment
        ``mtm_failures`` and store ``last_quote_error``.
        """
        total_unrealized = ZERO
        open_positions = [
            p for p in self._positions.values() if p.status == PositionStatus.OPEN
        ]
        if not open_positions:
            return total_unrealized

        # Group open positions by structure_id so multi-leg structures mark atomically.
        groups: dict[str, list[OptionPosition]] = {}
        for pos in open_positions:
            key = pos.structure_id or f"_pos:{pos.position_id}"
            groups.setdefault(key, []).append(pos)

        raised_exc: Exception | None = None

        for group_positions in groups.values():
            leg_outcomes: list[tuple[OptionPosition, bool, Decimal, str | None]] = []
            for position in group_positions:
                try:
                    quote = _mark_quote(
                        quote_provider, position.instrument_token, position.side
                    )
                except Exception as exc:  # noqa: BLE001 — record failure before re-raising
                    raised_exc = exc
                    leg_outcomes.append((position, True, ZERO, str(exc)))
                    continue

                if not isinstance(quote, dict) or not quote:
                    leg_outcomes.append((position, True, ZERO, "empty quote row"))
                    continue

                price = Decimal(str(quote.get("ltp", 0) or 0))
                # Fail-closed marking (2026-09-23 + R1/AC #3): a FAILED quote
                # (empty row / error key from a dead session or unknown token)
                # must not overwrite the last known price. A GENUINE zero
                # (ltp=0 with NO "error" key) passes through and updates the
                # mark to 0 with mark_stale=False.
                if bool(quote.get("error")):
                    leg_outcomes.append(
                        (position, True, price, str(quote.get("error")))
                    )
                else:
                    leg_outcomes.append((position, False, price, None))

            any_failed = any(is_fail for _, is_fail, _, _ in leg_outcomes)
            if any_failed:
                for position, is_fail, _, err_msg in leg_outcomes:
                    if is_fail:
                        total_unrealized += position.record_mtm_failure(err_msg)
                    else:
                        # Sibling leg failed — keep this leg's last consistent
                        # price so the structure net mark does not desync,
                        # while clearing any stale error on this healthy leg.
                        position.last_quote_error = None
                        if position.current_price > 0:
                            total_unrealized += position.calculate_unrealized_pnl()
                        elif position.entry_price > 0:
                            position.current_price = position.entry_price
                            total_unrealized += position.calculate_unrealized_pnl()
            else:
                for position, _, price, _ in leg_outcomes:
                    total_unrealized += position.update_mtm(price)

        # BUG-18: roll the prior-close anchor for every open structure. The
        # first mark that lands in a new IST session promotes yesterday's last
        # mark into the anchor, so ``daily_pnl`` can book only today's move.
        mark_session = _ist_session_date(timestamp if timestamp is not None else _now_utc())
        for structure in self._structures.values():
            if structure.is_open:
                structure.record_session_mark(mark_session)

        if raised_exc is not None:
            raise raised_exc

        return total_unrealized

    # ------------------------------------------------------------------
    # Persistence support (Gap G4.2)
    # ------------------------------------------------------------------

    def _fire_structure_opened(self, structure: StructurePosition, entry_fees: Decimal) -> None:
        if self.on_structure_opened is None:
            return
        try:
            self.on_structure_opened(structure, entry_fees)
        except Exception:  # noqa: BLE001 — mirroring must never block trading
            logger.exception(
                "[option-paper] on_structure_opened observer failed for %s",
                structure.structure_id[:8],
            )

    def _fire_leg_fill(self, record: dict[str, Any]) -> None:
        if self.on_leg_fill is None:
            return
        try:
            self.on_leg_fill(record)
        except Exception:  # noqa: BLE001 — the order ledger never blocks a fill
            logger.exception(
                "[option-paper] on_leg_fill observer failed for %s leg %s",
                str(record.get("structure_id", "?"))[:8],
                record.get("position_id", "?"),
            )

    def _fire_structure_closed(self, structure: StructurePosition, realized_pnl: Decimal) -> None:
        if self.on_structure_closed is None:
            return
        try:
            self.on_structure_closed(structure, realized_pnl)
        except Exception:  # noqa: BLE001 — mirroring must never block trading
            logger.exception(
                "[option-paper] on_structure_closed observer failed for %s",
                structure.structure_id[:8],
            )

    def _notify_settlement(self, structure_id: str) -> None:
        """Post-expiry hook (called by the expiry pipeline).

        ``settle_expired`` closes positions leg-by-leg; once no leg of a
        structure remains open, fire the closed observer so persistence can
        stamp the row ``expired``.
        """
        structure = self._structures.get(structure_id)
        if structure is None or structure.is_open:
            return
        if structure.closed_at is None:
            structure.closed_at = _now_utc()
        if structure.session_date is None:
            structure.session_date = _ist_session_date(structure.closed_at)
        self._fire_structure_closed(structure, structure.total_realized_pnl)

    def restore_structure(self, structure: StructurePosition, fees_paid: Decimal = ZERO) -> None:
        """Rehydrate a persisted open structure after a restart (G4.2).

        Registers the structure and its legs, then re-applies the opening
        cash debit (net premium + commissions + the entry-side statutory
        stack) so ``available_cash`` matches the pre-restart book.
        """
        self._structures[structure.structure_id] = structure
        for leg in structure.legs:
            self._positions[leg.position_id] = leg
        # BUG-18: a restored book must start today from the number yesterday
        # ended on, not from zero. A snapshot that remembers its own last mark
        # rolls forward through the same rule a mark pass uses — which also
        # keeps a mid-session restart from re-booking the morning's P&L as the
        # overnight gap. A pre-BUG-18 snapshot has no memory, so fall back to
        # the entry session and treat everything before today as carried.
        entry_session = structure.entry_session_date or _ist_session_date(structure.opened_at)
        structure.entry_session_date = entry_session
        today = _ist_session_date(_now_utc())
        if structure.last_mark_session is not None:
            structure.record_session_mark(today)
        elif entry_session is not None and today is not None and entry_session != today:
            structure.prior_close_net_pnl = structure.net_open_pnl
            structure.last_mark_session = today
            structure.last_mark_net_pnl = structure.net_open_pnl
        debit = structure.total_entry_cost + structure.total_commission + Decimal(str(fees_paid))
        self.available_cash -= debit
        logger.info(
            "[option-paper] restored structure=%s legs=%d debit=₹%.2f",
            structure.structure_id[:8],
            len(structure.legs),
            float(debit),
        )

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_open_positions(self) -> list[OptionPosition]:
        """Return all open option positions."""
        return [p for p in self._positions.values() if p.status == PositionStatus.OPEN]

    def get_positions_by_structure(self, structure_id: str) -> list[OptionPosition]:
        """Return all positions for a given structure."""
        return [
            p for p in self._positions.values()
            if p.structure_id == structure_id
        ]

    def get_open_structures(self) -> list[StructurePosition]:
        """Return all structures that have at least one open leg."""
        return [s for s in self._structures.values() if s.is_open]

    def get_closed_structures(self) -> list[StructurePosition]:
        """Return every structure with no open legs left (T0.2/A2).

        The mirror of :meth:`get_open_structures`.  Without it there is no
        way to enumerate closed structures at all, so a trade log has
        nothing to read.

        A structure counts as closed whether it was closed explicitly
        (:meth:`close_structure`) or cash-settled at expiry — settlement
        sets its legs to ``EXPIRED``, so ``is_open`` is ``False`` either
        way.  Check :attr:`StructurePosition.exit_reason` for which.

        Ordered by insertion, i.e. the order the structures were opened.
        """
        return [s for s in self._structures.values() if not s.is_open]

    def get_structure(self, structure_id: str) -> StructurePosition | None:
        return self._structures.get(structure_id)

    @property
    def total_commission_paid(self) -> Decimal:
        """Every rupee of brokerage charged since the book opened.

        Commissions leave ``available_cash`` the moment a structure fills,
        so they must be subtracted from equity too — otherwise a paying
        broker's book would look richer than the cash it actually holds.
        """
        return sum(p.commission for p in self._positions.values())

    @property
    def total_statutory_fees_paid(self) -> Decimal:
        """Every statutory rupee (STT, exchange txn, SEBI, stamp, GST) charged.

        Cash-bearing since Gap G4.1: these debits leave ``available_cash``
        at execution, so equity must net them out too — otherwise a book
        with real contract-note costs would overstate its own equity.
        """
        return self._statutory_fees_paid

    @property
    def total_costs_paid(self) -> Decimal:
        """Brokerage + statutory stack — everything trading has cost so far."""
        return self.total_commission_paid + self._statutory_fees_paid

    @property
    def total_equity(self) -> Decimal:
        """Capital + realized + unrealized P&L − commissions − statutory fees.

        NOT ``available_cash + unrealized``: cash is debited the full
        premium at open while unrealized is measured from the entry price,
        so summing those two would make equity drop by the premium the
        moment a long option is bought. Anchoring on starting capital keeps
        equity flat across an at-market open and moving only with P&L —
        matching how the equity P&L reconciliation test (and a real
        broker's funds view) behaves.
        """
        unrealized = sum(
            p.unrealized_pnl for p in self._positions.values()
            if p.status == PositionStatus.OPEN
        )
        return (
            self.capital
            + self.total_realized_pnl
            + unrealized
            - self.total_commission_paid
            - self._statutory_fees_paid
        )

    @property
    def total_realized_pnl(self) -> Decimal:
        """Realized P&L across closed AND expired positions.

        Expiry settlement marks positions ``EXPIRED`` (not ``CLOSED``), so
        filtering on ``CLOSED`` alone would drop settled P&L from the
        summary and break the equity reconciliation.
        """
        return sum(
            p.realized_pnl for p in self._positions.values()
            if p.status in (PositionStatus.CLOSED, PositionStatus.EXPIRED)
        )

    @property
    def total_unrealized_pnl(self) -> Decimal:
        """Sum of open legs' mark-to-market P&L (as of the last ``update_mtm``).

        Mirrors :attr:`total_realized_pnl` — the two halves of the book's P&L
        that :attr:`total_equity` combines.
        """
        return sum(
            p.unrealized_pnl for p in self._positions.values()
            if p.status == PositionStatus.OPEN
        )

    @property
    def total_margin_used(self) -> Decimal:
        """Approximate margin used (sum of sell-side notional values)."""
        margin = ZERO
        for pos in self._positions.values():
            if pos.status == PositionStatus.OPEN and pos.is_short:
                margin += pos.entry_price * Decimal(str(pos.total_quantity))
        return margin

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate_lot_size(self, leg: OptionLeg) -> None:
        """Validate that lot size is positive."""
        if leg.lot_size < 1:
            raise LotSizeValidationError(
                f"Invalid lot_size {leg.lot_size} for {leg.trading_symbol}"
            )
        if leg.quantity < 1:
            raise LotSizeValidationError(
                f"Invalid quantity {leg.quantity} for {leg.trading_symbol}"
            )

    def _apply_slippage(
        self,
        price: Decimal,
        side: str,
        *,
        quote: Any | None = None,
        provider: Any | None = None,
        quantity: int = 0,
        tick_size: Decimal | None = None,
    ) -> Decimal:
        """Turn a quoted price into a fill the exchange could have printed.

        Two models, chosen by where the price came from, then ONE grid (BUG-06):

        * **live two-sided depth** — the leg already paid the full spread by
          crossing it, so a percentage of slippage on top double-charges a cost
          the book is not carrying. Residual impact is an integer number of
          ticks (``slippage_ticks``, default ``0`` while the order visibly fits
          the top of book), never a fraction of a paise.
        * **mid / synthetic / one-sided** — no real counterparty was crossed,
          so the historical ``slippage_pct`` model still applies unchanged.

        Either way the result goes through :func:`quantize_nse_tick`, rounding
        adversely, so 100 % of option fills land on the instrument's grid.
        """
        step = tick_size if tick_size is not None else NSE_OPTION_TICK_SIZE
        if _fills_against_live_depth(quote, side, provider):
            return self._grid_fill(
                price + self._residual_ticks(quote, side, int(quantity or 0)) * step,
                side,
                step,
            )
        if side == "BUY":
            raw = price * (ONE + self.slippage_pct)
        else:
            raw = price * (ONE - self.slippage_pct)
        return self._grid_fill(raw, side, step)

    @staticmethod
    def _grid_fill(price: Decimal, side: str, tick_size: Decimal) -> Decimal:
        """Adverse rounding onto the instrument grid (last step of every fill)."""
        return quantize_nse_tick(price, side, tick_size)

    @staticmethod
    def _fill_basis(quote: Any, side: str, provider: Any) -> str:
        """Which pricing model a leg used — recorded so the fill is auditable."""
        return (
            "live_depth"
            if _fills_against_live_depth(quote, side, provider)
            else "model"
        )

    def _residual_ticks(self, quote: Any, side: str, quantity: int) -> Decimal:
        """Whole ticks of own-impact on a spread-crossed fill.

        Zero while the order fits inside the published top-of-book size; one
        extra tick per whole top-of-book consumed beyond it, capped by
        ``max_slippage_ticks``. No visible size is *not* a reason to charge
        impact — the venue said nothing, so the model says nothing.
        """
        ticks = self.slippage_ticks
        visible = _top_of_book_quantity(quote, side)
        if visible is not None and quantity > int(visible):
            extra = int(math.ceil(Decimal(quantity) / visible)) - 1
            ticks += max(0, min(extra, self.max_slippage_ticks - self.slippage_ticks))
        return Decimal(ticks)

    def _log_fill(
        self,
        intent: TradeIntent,
        structure_id: str,
        fills: list[tuple],
        ts: datetime,
    ) -> None:
        """Log fill details for audit trail."""
        record = {
            "timestamp": ts.isoformat(),
            "structure_id": structure_id,
            "structure_type": intent.structure_type,
            "strategy": intent.strategy_name,
            "underlying": intent.view.underlying,
            "expiry": str(intent.expiry),
            "legs": [
                {
                    "symbol": leg.trading_symbol,
                    "side": leg.side,
                    "quantity": leg.quantity,
                    "fill_price": str(fill_price),
                    "commission": str(commission),
                }
                for leg, fill_price, commission, _, _ in fills
            ],
            "cash_after": str(self.available_cash),
            "statutory_fees": str(self._statutory_fees_paid),
        }
        self._order_history.append(record)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ONE = Decimal("1")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class InsufficientMarginError(Exception):
    """Raised when available cash is insufficient for a trade."""
    pass


class LotSizeValidationError(Exception):
    """Raised when lot size or quantity is invalid."""
    pass
