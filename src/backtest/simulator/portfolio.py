"""Portfolio model for the forward testing simulator.

:class:`Portfolio` is the root aggregate: it owns cash, open positions, order
references and the equity history, and it enforces the risk limits that decide
whether a new position may be opened.

Cash convention
---------------
Cash moves on every position change, and the direction depends on the side::

    open  long   cash -= qty * price + commission
    open  short  cash += qty * price - commission      (proceeds credited)
    close long   cash += qty * price - commission
    close short  cash -= qty * price + commission

Combined with :attr:`Position.market_value` being *signed*, this makes one
formula correct for both directions::

    total_equity = cash + position_value

Worked example — short 10 @ 100: cash rises by 1,000 while position value is
−1,000, so equity is unchanged at entry (correct). If the price falls to 90,
position value becomes −900 and equity rises by 100 (correct).

Relationship to the database
----------------------------
This class is pure in-memory domain logic with no I/O of its own. Persistence
goes through :meth:`save_to_db` / :meth:`load_from_db`, which take a
:class:`~backtest.db.manager.DatabaseManager`. That keeps every calculation
unit-testable without a database.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Iterable, Iterator, Mapping

from backtest.simulator.errors import (
    DuplicatePositionError,
    InsufficientFundsError,
    LimitExceededError,
    PortfolioStateError,
    PositionNotFoundError,
    ShortSellingNotAllowedError,
    ValidationError,
)
from backtest.simulator.money import (
    ZERO,
    is_zero,
    money,
    price as to_price,
    quantize_money,
    to_decimal,
)
from backtest.simulator.position import Position

if TYPE_CHECKING:  # pragma: no cover
    from backtest.db.manager import DatabaseManager

__all__ = [
    "Portfolio",
    "PortfolioLimits",
    "PortfolioStatus",
    "EquityPoint",
    "PositionCheck",
]

logger = logging.getLogger("backtest.simulator.portfolio")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PortfolioStatus:
    """Lifecycle states, matching ``portfolios.status`` in the schema."""

    ACTIVE = "active"
    """Trading normally."""
    PAUSED = "paused"
    """Loop still running, but no new positions may be opened."""
    STOPPED = "stopped"
    """Terminal."""

    ALL = (ACTIVE, PAUSED, STOPPED)


@dataclass(frozen=True)
class EquityPoint:
    """One mark-to-market snapshot, mirroring a row of ``equity_curve``."""

    ts: datetime
    total_equity: Decimal
    cash: Decimal
    position_value: Decimal
    unrealized_pnl: Decimal = ZERO
    realized_pnl: Decimal = ZERO

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts.isoformat(),
            "total_equity": str(self.total_equity),
            "cash": str(self.cash),
            "position_value": str(self.position_value),
            "unrealized_pnl": str(self.unrealized_pnl),
            "realized_pnl": str(self.realized_pnl),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EquityPoint":
        return cls(
            ts=datetime.fromisoformat(payload["ts"]),
            total_equity=money(payload["total_equity"]),
            cash=money(payload["cash"]),
            position_value=money(payload["position_value"]),
            unrealized_pnl=money(payload.get("unrealized_pnl", ZERO)),
            realized_pnl=money(payload.get("realized_pnl", ZERO)),
        )


@dataclass(frozen=True)
class PositionCheck:
    """Result of :meth:`Portfolio.can_open_position`.

    Truthy when the trade is permitted, so it reads naturally::

        if portfolio.can_open_position("INFY", 10, 1500):
            ...

    while still carrying ``code`` and ``reason`` for logging and for the
    ``strategy_signals.skip_reason`` column.
    """

    allowed: bool
    code: str = "ok"
    reason: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.allowed

    def raise_if_denied(self) -> None:
        """Raise the matching exception when the check failed."""
        if self.allowed:
            return
        mapping = {
            "portfolio_not_active": PortfolioStateError,
            "insufficient_funds": InsufficientFundsError,
            "short_selling_not_allowed": ShortSellingNotAllowedError,
            "duplicate_position": DuplicatePositionError,
        }
        exc_type = mapping.get(self.code, LimitExceededError)
        if exc_type is DuplicatePositionError:
            raise DuplicatePositionError(self.reason)
        raise exc_type(self.reason, code=self.code, **dict(self.details))

    def __str__(self) -> str:  # pragma: no cover - debug helper
        return "allowed" if self.allowed else f"denied[{self.code}]: {self.reason}"


@dataclass
class PortfolioLimits:
    """Risk limits enforced by :meth:`Portfolio.can_open_position`.

    Every limit is optional (``None`` disables it) so a portfolio can start
    permissive and be tightened later. The Step 15 risk manager layers
    portfolio-wide checks on top of these per-trade ones.

    Parameters
    ----------
    allow_short:
        When ``False``, any negative-quantity request is refused.
    max_open_positions:
        Cap on concurrently open positions.
    max_position_value:
        Absolute cap on a single position's notional, in account currency.
    max_position_pct:
        Cap on a single position as a fraction of total equity (``0.2`` = 20%).
    max_gross_exposure_pct:
        Cap on the sum of all absolute exposures, as a fraction of equity.
        Values above ``1.0`` permit leverage.
    max_leverage:
        Multiplier applied to equity when computing buying power. ``1.0`` is
        a cash account.
    min_trade_value:
        Reject dust trades whose notional is below this.
    """

    allow_short: bool = False
    max_open_positions: int | None = None
    max_position_value: Decimal | None = None
    max_position_pct: Decimal | None = None
    max_gross_exposure_pct: Decimal | None = None
    max_leverage: Decimal = Decimal("1")
    min_trade_value: Decimal | None = None

    def __post_init__(self) -> None:
        for name in (
            "max_position_value",
            "max_position_pct",
            "max_gross_exposure_pct",
            "min_trade_value",
        ):
            value = getattr(self, name)
            if value is not None:
                coerced = to_decimal(value, name)
                if coerced <= ZERO:
                    raise ValidationError(
                        f"{name} must be positive when set",
                        code="invalid_limit",
                        limit=name,
                    )
                setattr(self, name, coerced)

        self.max_leverage = to_decimal(self.max_leverage, "max_leverage")
        if self.max_leverage < Decimal("1"):
            raise ValidationError(
                "max_leverage must be at least 1.0",
                code="invalid_limit",
                limit="max_leverage",
            )
        if self.max_open_positions is not None and self.max_open_positions < 1:
            raise ValidationError(
                "max_open_positions must be at least 1 when set",
                code="invalid_limit",
                limit="max_open_positions",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "allow_short": self.allow_short,
            "max_open_positions": self.max_open_positions,
            "max_position_value": str(self.max_position_value) if self.max_position_value else None,
            "max_position_pct": str(self.max_position_pct) if self.max_position_pct else None,
            "max_gross_exposure_pct": (
                str(self.max_gross_exposure_pct) if self.max_gross_exposure_pct else None
            ),
            "max_leverage": str(self.max_leverage),
            "min_trade_value": str(self.min_trade_value) if self.min_trade_value else None,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PortfolioLimits":
        return cls(
            allow_short=bool(payload.get("allow_short", False)),
            max_open_positions=payload.get("max_open_positions"),
            max_position_value=payload.get("max_position_value"),
            max_position_pct=payload.get("max_position_pct"),
            max_gross_exposure_pct=payload.get("max_gross_exposure_pct"),
            max_leverage=payload.get("max_leverage", Decimal("1")),
            min_trade_value=payload.get("min_trade_value"),
        )


class Portfolio:
    """Cash, positions and risk limits for one forward-testing run.

    Parameters
    ----------
    name:
        Human-readable identifier. Unique in the database.
    initial_capital:
        Starting cash. Must be positive.
    current_cash:
        Cash on hand. Defaults to ``initial_capital`` for a fresh portfolio;
        supply it explicitly when restoring saved state.
    limits:
        Risk limits. Defaults to a cash account with shorting disabled.

    Raises
    ------
    ValidationError
        If capital is non-positive or ``status`` is not a known state.

    Examples
    --------
    >>> p = Portfolio(name="run-1", initial_capital=100000)
    >>> p.open_position("INFY", 10, 1500, commission=5)     # doctest: +ELLIPSIS
    <Position INFY long ...>
    >>> p.calculate_total_equity()
    Decimal('99995.0000')
    """

    def __init__(
        self,
        name: str,
        initial_capital: Any,
        current_cash: Any | None = None,
        portfolio_id: str | None = None,
        status: str = PortfolioStatus.ACTIVE,
        base_currency: str = "INR",
        limits: PortfolioLimits | None = None,
        created_at: datetime | None = None,
    ) -> None:
        self.name = str(name).strip()
        if not self.name:
            raise ValidationError("name must not be empty", code="invalid_name")

        self.initial_capital = money(initial_capital, "initial_capital")
        if self.initial_capital <= ZERO:
            raise ValidationError(
                "initial_capital must be positive",
                code="invalid_capital",
                value=str(self.initial_capital),
            )

        self.current_cash = (
            money(current_cash, "current_cash")
            if current_cash is not None
            else self.initial_capital
        )

        if status not in PortfolioStatus.ALL:
            raise ValidationError(
                f"unknown status {status!r}; expected one of {PortfolioStatus.ALL}",
                code="invalid_status",
            )

        self.portfolio_id = portfolio_id or str(uuid.uuid4())
        self.status = status
        self.base_currency = base_currency
        self.limits = limits or PortfolioLimits()
        self.created_at = created_at or _utcnow()

        self.positions: dict[str, Position] = {}
        """Open positions, keyed by symbol."""

        self.closed_positions: list[Position] = []
        """Positions retained as history after being fully closed."""

        self.pending_orders: list[Any] = []
        """Working orders. Populated once Step 5 lands the Order model."""

        self.filled_orders: list[Any] = []
        """Completed orders, most recent last."""

        self.equity_history: list[EquityPoint] = []
        """Mark-to-market snapshots appended by :meth:`record_equity`."""

        self.realized_pnl: Decimal = ZERO
        """Cumulative realised P&L, gross of commission."""

        self.total_commission: Decimal = ZERO

        logger.info(
            "portfolio created: %s (%s %s)",
            self.name, self.base_currency, self.initial_capital,
        )

    # -- valuation ---------------------------------------------------------

    def calculate_position_value(self) -> Decimal:
        """Net signed market value of all open positions.

        Shorts contribute negatively, so this can be negative overall.
        """
        return quantize_money(sum((p.market_value for p in self.positions.values()), ZERO))

    def calculate_total_equity(self) -> Decimal:
        """Cash plus net position value — the portfolio's net worth."""
        return quantize_money(self.current_cash + self.calculate_position_value())

    def calculate_gross_exposure(self) -> Decimal:
        """Sum of absolute exposures. Longs and shorts both add."""
        return quantize_money(sum((p.notional for p in self.positions.values()), ZERO))

    def calculate_net_exposure(self) -> Decimal:
        """Signed exposure — longs minus shorts. Same as position value."""
        return self.calculate_position_value()

    def calculate_margin_used(self) -> Decimal:
        """Capital tied up in open positions.

        With ``max_leverage = 1`` (a cash account) this equals gross exposure.
        Under leverage it is gross exposure divided by the leverage multiplier.
        """
        gross = self.calculate_gross_exposure()
        if self.limits.max_leverage <= ZERO:  # pragma: no cover - guarded in limits
            return gross
        return quantize_money(gross / self.limits.max_leverage)

    def calculate_buying_power(self) -> Decimal:
        """How much notional can still be opened, never negative.

        For a cash account this is simply available cash. Under leverage it is
        ``equity × leverage − gross exposure``.
        """
        if self.limits.max_leverage == Decimal("1"):
            return max(ZERO, self.current_cash)
        capacity = self.calculate_total_equity() * self.limits.max_leverage
        return max(ZERO, quantize_money(capacity - self.calculate_gross_exposure()))

    @property
    def unrealized_pnl(self) -> Decimal:
        """Open P&L across every position, gross of commission."""
        return quantize_money(sum((p.unrealized_pnl for p in self.positions.values()), ZERO))

    @property
    def total_pnl(self) -> Decimal:
        """Realised plus unrealised, gross of commission."""
        return quantize_money(self.realized_pnl + self.unrealized_pnl)

    @property
    def total_return(self) -> Decimal:
        """Equity change since inception, in currency."""
        return quantize_money(self.calculate_total_equity() - self.initial_capital)

    @property
    def total_return_pct(self) -> Decimal:
        """Equity change as a fraction of initial capital (``0.05`` = +5%)."""
        if self.initial_capital == ZERO:  # pragma: no cover - guarded in __init__
            return ZERO
        return (self.total_return / self.initial_capital).quantize(Decimal("0.000001"))

    def get_current_exposure(self) -> dict[str, Any]:
        """Exposure breakdown for the risk manager and the dashboard."""
        equity = self.calculate_total_equity()
        long_value = quantize_money(
            sum((p.market_value for p in self.positions.values() if p.is_long), ZERO)
        )
        short_value = quantize_money(
            sum((p.notional for p in self.positions.values() if p.is_short), ZERO)
        )
        gross = self.calculate_gross_exposure()
        return {
            "gross_exposure": gross,
            "net_exposure": self.calculate_net_exposure(),
            "long_exposure": long_value,
            "short_exposure": short_value,
            "gross_exposure_pct": (
                (gross / equity).quantize(Decimal("0.000001")) if equity > ZERO else ZERO
            ),
            "open_positions": len(self.positions),
            "cash": self.current_cash,
            "equity": equity,
            "margin_used": self.calculate_margin_used(),
            "buying_power": self.calculate_buying_power(),
        }

    # -- position access ---------------------------------------------------

    def get_position(self, symbol: str) -> Position | None:
        """Return the open position for ``symbol``, or ``None``."""
        return self.positions.get(str(symbol).strip().upper())

    def has_position(self, symbol: str) -> bool:
        return self.get_position(symbol) is not None

    def require_position(self, symbol: str) -> Position:
        """Like :meth:`get_position` but raises when absent.

        Raises
        ------
        PositionNotFoundError
        """
        position = self.get_position(symbol)
        if position is None:
            raise PositionNotFoundError(f"no open position for {symbol}")
        return position

    def open_symbols(self) -> list[str]:
        return sorted(self.positions)

    def __contains__(self, symbol: object) -> bool:
        return isinstance(symbol, str) and self.has_position(symbol)

    def __len__(self) -> int:
        return len(self.positions)

    def __iter__(self) -> Iterator[Position]:
        return iter(self.positions.values())

    # -- validation --------------------------------------------------------

    def can_open_position(
        self, symbol: str, quantity: Any, at_price: Any
    ) -> PositionCheck:
        """Check every limit for a proposed new position.

        Returns a :class:`PositionCheck` rather than a bare bool so the caller
        gets a machine-readable ``code`` explaining any refusal — the Step 13
        adapter writes it to ``strategy_signals.skip_reason``.

        Checks, in order: portfolio active · valid inputs · short permitted ·
        no duplicate open position · minimum trade value · max open positions ·
        per-position value cap · per-position percentage cap · gross exposure
        cap · sufficient buying power.
        """
        symbol = str(symbol).strip().upper()

        try:
            qty = to_price(quantity, "quantity")
            px = to_price(at_price, "price")
        except ValueError as exc:
            return PositionCheck(False, "invalid_input", str(exc))

        if self.status != PortfolioStatus.ACTIVE:
            return PositionCheck(
                False,
                "portfolio_not_active",
                f"portfolio is {self.status}, not active",
                {"status": self.status},
            )
        if is_zero(qty):
            return PositionCheck(False, "zero_quantity", "quantity must be non-zero")
        if px <= ZERO:
            return PositionCheck(False, "invalid_price", "price must be positive")

        if qty < ZERO and not self.limits.allow_short:
            return PositionCheck(
                False,
                "short_selling_not_allowed",
                f"short selling is disabled; cannot sell {abs(qty)} {symbol}",
                {"symbol": symbol},
            )

        if self.has_position(symbol):
            return PositionCheck(
                False,
                "duplicate_position",
                f"an open position already exists for {symbol}",
                {"symbol": symbol},
            )

        notional = quantize_money(abs(qty) * px)
        limits = self.limits

        if limits.min_trade_value is not None and notional < limits.min_trade_value:
            return PositionCheck(
                False,
                "below_min_trade_value",
                f"trade value {notional} is below the minimum {limits.min_trade_value}",
                {"notional": str(notional), "minimum": str(limits.min_trade_value)},
            )

        if (
            limits.max_open_positions is not None
            and len(self.positions) >= limits.max_open_positions
        ):
            return PositionCheck(
                False,
                "max_open_positions",
                f"already holding {len(self.positions)} positions "
                f"(limit {limits.max_open_positions})",
                {"open": len(self.positions), "limit": limits.max_open_positions},
            )

        if limits.max_position_value is not None and notional > limits.max_position_value:
            return PositionCheck(
                False,
                "max_position_value",
                f"position value {notional} exceeds the limit {limits.max_position_value}",
                {"notional": str(notional), "limit": str(limits.max_position_value)},
            )

        equity = self.calculate_total_equity()
        if limits.max_position_pct is not None and equity > ZERO:
            pct = notional / equity
            if pct > limits.max_position_pct:
                return PositionCheck(
                    False,
                    "max_position_pct",
                    f"position would be {pct:.2%} of equity "
                    f"(limit {limits.max_position_pct:.2%})",
                    {"pct": str(pct), "limit": str(limits.max_position_pct)},
                )

        if limits.max_gross_exposure_pct is not None and equity > ZERO:
            projected = (self.calculate_gross_exposure() + notional) / equity
            if projected > limits.max_gross_exposure_pct:
                return PositionCheck(
                    False,
                    "max_gross_exposure",
                    f"gross exposure would be {projected:.2%} of equity "
                    f"(limit {limits.max_gross_exposure_pct:.2%})",
                    {"projected": str(projected), "limit": str(limits.max_gross_exposure_pct)},
                )

        # A short credits cash rather than consuming it, but still needs
        # margin capacity — which the gross-exposure check above covers.
        if qty > ZERO:
            buying_power = self.calculate_buying_power()
            if notional > buying_power:
                return PositionCheck(
                    False,
                    "insufficient_funds",
                    f"need {notional} but only {buying_power} of buying power is available",
                    {"required": str(notional), "available": str(buying_power)},
                )

        return PositionCheck(True)

    # -- position mutation -------------------------------------------------

    def add_position(self, position: Position) -> Position:
        """Attach an existing :class:`Position` **without** moving cash.

        Use this when restoring saved state or wiring up a position built
        elsewhere. To open a position and settle the cash in one step, use
        :meth:`open_position`.

        Raises
        ------
        DuplicatePositionError
            If the symbol already has an open position — mirroring the
            ``uq_positions_one_open_per_symbol`` index in the database.
        ValidationError
            If the position is already closed.
        """
        if not position.is_open:
            raise ValidationError(
                "cannot add a closed position",
                code="position_closed",
                symbol=position.symbol,
            )
        if position.symbol in self.positions:
            raise DuplicatePositionError(
                f"an open position already exists for {position.symbol}"
            )
        position.portfolio_id = self.portfolio_id
        self.positions[position.symbol] = position
        logger.info("position added: %s qty=%s", position.symbol, position.quantity)
        return position

    def open_position(
        self,
        symbol: str,
        quantity: Any,
        at_price: Any,
        commission: Any = ZERO,
        strategy_name: str | None = None,
        validate: bool = True,
    ) -> Position:
        """Open a position and settle the cash.

        Parameters
        ----------
        quantity:
            Signed. Positive opens a long, negative opens a short.
        validate:
            When ``True`` (default) every limit in :meth:`can_open_position`
            is enforced. Pass ``False`` only to restore known-good state.

        Raises
        ------
        InsufficientFundsError, LimitExceededError, ShortSellingNotAllowedError,
        DuplicatePositionError, PortfolioStateError
            Whichever check failed. See :meth:`can_open_position`.
        """
        symbol = str(symbol).strip().upper()
        if validate:
            self.can_open_position(symbol, quantity, at_price).raise_if_denied()

        qty = to_price(quantity, "quantity")
        px = to_price(at_price, "price")
        fee = money(commission, "commission")
        if fee < ZERO:
            raise ValidationError("commission must not be negative", code="invalid_commission")

        position = Position(
            symbol=symbol,
            quantity=qty,
            average_entry_price=px,
            current_price=px,
            portfolio_id=self.portfolio_id,
            commission_total=fee,
            strategy_name=strategy_name,
        )
        self.positions[symbol] = position

        # Long pays out; short receives proceeds. Commission is always a cost.
        notional = abs(qty) * px
        self.current_cash = quantize_money(
            self.current_cash + (-notional if qty > ZERO else notional) - fee
        )
        self.total_commission = quantize_money(self.total_commission + fee)

        logger.info(
            "opened %s %s %s @ %s (commission %s), cash now %s",
            position.position_type, abs(qty), symbol, px, fee, self.current_cash,
        )
        return position

    def update_position(self, symbol: str, at_price: Any) -> Position:
        """Mark one position to ``at_price``.

        Raises
        ------
        PositionNotFoundError
        """
        position = self.require_position(symbol)
        position.update_price(at_price)
        return position

    def update_prices(self, prices: Mapping[str, Any]) -> None:
        """Mark many positions at once.

        Symbols with no open position are ignored, so a feed covering the
        whole watchlist can be passed straight in.
        """
        for symbol, value in prices.items():
            position = self.get_position(symbol)
            if position is not None and value is not None:
                position.update_price(value)

    def reduce_position(
        self, symbol: str, quantity: Any, at_price: Any, commission: Any = ZERO
    ) -> Position:
        """Partially close a position, realising P&L and settling cash.

        If the reduction closes the position entirely it is moved to
        :attr:`closed_positions`.
        """
        position = self.require_position(symbol)
        result = position.reduce_shares(quantity, at_price, commission)

        self.current_cash = quantize_money(self.current_cash + result.cash_delta)
        self.realized_pnl = quantize_money(self.realized_pnl + result.realized_pnl)
        self.total_commission = quantize_money(self.total_commission + result.commission)

        if result.fully_closed:
            self._retire(position)

        logger.info(
            "reduced %s by %s @ %s, realised %s",
            position.symbol, result.quantity_closed, at_price, result.realized_pnl,
        )
        return position

    def close_position(
        self, symbol: str, at_price: Any | None = None, commission: Any = ZERO
    ) -> Position:
        """Close a position entirely.

        Parameters
        ----------
        at_price:
            Exit price. Defaults to the position's latest mark, which is what
            an end-of-run flatten wants.

        Raises
        ------
        PositionNotFoundError
        """
        position = self.require_position(symbol)
        exit_price = at_price if at_price is not None else position.effective_price
        return self.reduce_position(symbol, abs(position.quantity), exit_price, commission)

    def close_all_positions(self, prices: Mapping[str, Any] | None = None) -> list[Position]:
        """Flatten everything. Used by the Step 20 shutdown path."""
        closed: list[Position] = []
        for symbol in list(self.positions):
            exit_price = (prices or {}).get(symbol)
            closed.append(self.close_position(symbol, exit_price))
        return closed

    def _retire(self, position: Position) -> None:
        """Move a fully-closed position out of the open book."""
        self.positions.pop(position.symbol, None)
        self.closed_positions.append(position)

    # -- lifecycle ---------------------------------------------------------

    def pause(self) -> None:
        """Stop opening new positions; existing ones stay open."""
        self.status = PortfolioStatus.PAUSED
        logger.warning("portfolio %s paused", self.name)

    def resume(self) -> None:
        """Return to active trading.

        Raises
        ------
        PortfolioStateError
            If the portfolio was stopped. ``stopped`` is terminal by design —
            silently reviving it would mask a bug in the caller.
        """
        if self.status == PortfolioStatus.STOPPED:
            raise PortfolioStateError(
                "a stopped portfolio cannot be resumed", code="portfolio_stopped"
            )
        self.status = PortfolioStatus.ACTIVE
        logger.info("portfolio %s resumed", self.name)

    def stop(self) -> None:
        """Terminal state. Cannot be undone."""
        self.status = PortfolioStatus.STOPPED
        logger.warning("portfolio %s stopped", self.name)

    # -- equity history ----------------------------------------------------

    def record_equity(self, ts: datetime | None = None) -> EquityPoint:
        """Append a mark-to-market snapshot and return it."""
        point = EquityPoint(
            ts=ts or _utcnow(),
            total_equity=self.calculate_total_equity(),
            cash=self.current_cash,
            position_value=self.calculate_position_value(),
            unrealized_pnl=self.unrealized_pnl,
            realized_pnl=self.realized_pnl,
        )
        self.equity_history.append(point)
        return point

    def peak_equity(self) -> Decimal:
        """Highest recorded equity, falling back to current then initial."""
        if not self.equity_history:
            return max(self.calculate_total_equity(), self.initial_capital)
        return max(p.total_equity for p in self.equity_history)

    def current_drawdown(self) -> Decimal:
        """Fractional drop from peak equity (``0.10`` = 10% below peak)."""
        peak = self.peak_equity()
        if peak <= ZERO:
            return ZERO
        drop = peak - self.calculate_total_equity()
        if drop <= ZERO:
            return ZERO
        return (drop / peak).quantize(Decimal("0.000001"))

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe snapshot of the whole portfolio.

        Decimals become strings so a round trip through JSON preserves exact
        precision. This is the payload the Step 20 state manager persists.
        """
        return {
            "portfolio_id": self.portfolio_id,
            "name": self.name,
            "initial_capital": str(self.initial_capital),
            "current_cash": str(self.current_cash),
            "base_currency": self.base_currency,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "realized_pnl": str(self.realized_pnl),
            "total_commission": str(self.total_commission),
            "limits": self.limits.to_dict(),
            "positions": [p.to_dict() for p in self.positions.values()],
            "closed_positions": [p.to_dict() for p in self.closed_positions],
            "equity_history": [p.to_dict() for p in self.equity_history],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Portfolio":
        """Rebuild a portfolio from :meth:`to_dict` output."""
        portfolio = cls(
            name=payload["name"],
            initial_capital=payload["initial_capital"],
            current_cash=payload.get("current_cash"),
            portfolio_id=payload.get("portfolio_id"),
            status=payload.get("status", PortfolioStatus.ACTIVE),
            base_currency=payload.get("base_currency", "INR"),
            limits=PortfolioLimits.from_dict(payload.get("limits") or {}),
            created_at=(
                datetime.fromisoformat(payload["created_at"])
                if payload.get("created_at")
                else None
            ),
        )
        portfolio.realized_pnl = money(payload.get("realized_pnl", ZERO))
        portfolio.total_commission = money(payload.get("total_commission", ZERO))

        for raw in payload.get("positions", []):
            position = Position.from_dict(raw)
            position.portfolio_id = portfolio.portfolio_id
            portfolio.positions[position.symbol] = position
        for raw in payload.get("closed_positions", []):
            portfolio.closed_positions.append(Position.from_dict(raw))
        for raw in payload.get("equity_history", []):
            portfolio.equity_history.append(EquityPoint.from_dict(raw))
        return portfolio

    # -- persistence -------------------------------------------------------

    def save_to_db(self, db: "DatabaseManager") -> str:
        """Upsert the portfolio and its positions, returning the portfolio id.

        Runs in a single transaction: either the portfolio row and every
        position land together, or nothing does. A partially-written portfolio
        would misreport equity on the next restart.

        Position rows are reconciled rather than blindly inserted — a position
        closed in memory is updated in place, preserving its ``position_id``
        and satisfying the one-open-position-per-symbol index.
        """
        from sqlalchemy import select

        from backtest.db.models import Portfolio as PortfolioRow
        from backtest.db.models import Position as PositionRow

        with db.session() as session:
            row = session.get(PortfolioRow, self.portfolio_id)
            if row is None:
                row = PortfolioRow(portfolio_id=self.portfolio_id)
                session.add(row)
            row.name = self.name
            row.initial_capital = self.initial_capital
            row.current_cash = self.current_cash
            row.base_currency = self.base_currency
            row.status = self.status
            row.created_at = self.created_at
            session.flush()

            # ORDER MATTERS. Closed positions are written first and flushed
            # before the open ones.
            #
            # Consider a symbol that was saved while open, then closed and
            # reopened in memory. Writing the new open row first would leave
            # two rows with status='open' for that symbol at flush time,
            # violating uq_positions_one_open_per_symbol. Flipping the old row
            # to 'closed' first keeps the invariant true at every point.
            #
            # no_autoflush stops session.get() from flushing a half-built unit
            # of work mid-loop, which would reintroduce the same ordering bug.
            with session.no_autoflush:
                for position in self.closed_positions:
                    self._upsert_position(session, PositionRow, position)
            session.flush()

            with session.no_autoflush:
                for position in self.positions.values():
                    self._upsert_position(session, PositionRow, position)
            session.flush()

        logger.info("portfolio %s saved (%s)", self.name, self.portfolio_id)
        return self.portfolio_id

    def _upsert_position(self, session: Any, PositionRow: Any, position: Position) -> None:
        """Insert or update one position row."""
        row = session.get(PositionRow, position.position_id)
        if row is None:
            row = PositionRow(position_id=position.position_id)
            session.add(row)
        row.portfolio_id = self.portfolio_id
        row.symbol = position.symbol
        row.exchange = position.exchange
        row.position_type = position.position_type
        row.quantity = position.quantity
        row.average_entry_price = position.average_entry_price
        row.current_price = position.current_price
        row.unrealized_pnl = position.unrealized_pnl
        row.realized_pnl = position.realized_pnl
        row.commission_total = position.commission_total
        row.opened_at = position.opened_at
        row.closed_at = position.closed_at
        row.last_updated = position.last_updated
        row.status = position.status

    @classmethod
    def load_from_db(cls, db: "DatabaseManager", portfolio_id: str) -> "Portfolio":
        """Reconstruct a portfolio and its open positions from the database.

        Only ``status = 'open'`` positions are loaded into
        :attr:`positions`; closed rows stay in the database as history and are
        not needed to resume trading.

        Raises
        ------
        PositionNotFoundError
            If no portfolio has that id. (Reusing the lookup error type keeps
            the "not found" family together.)
        """
        from sqlalchemy import select

        from backtest.db.models import Portfolio as PortfolioRow
        from backtest.db.models import Position as PositionRow

        with db.session() as session:
            row = session.get(PortfolioRow, portfolio_id)
            if row is None:
                raise PositionNotFoundError(f"no portfolio with id {portfolio_id}")

            portfolio = cls(
                name=row.name,
                initial_capital=row.initial_capital,
                current_cash=row.current_cash,
                portfolio_id=row.portfolio_id,
                status=row.status,
                base_currency=row.base_currency,
                created_at=row.created_at,
            )

            open_rows = session.scalars(
                select(PositionRow).where(
                    PositionRow.portfolio_id == portfolio_id,
                    PositionRow.status == "open",
                )
            ).all()

            for prow in open_rows:
                position = Position(
                    symbol=prow.symbol,
                    quantity=prow.quantity,
                    average_entry_price=prow.average_entry_price,
                    position_id=prow.position_id,
                    portfolio_id=portfolio_id,
                    exchange=prow.exchange,
                    current_price=prow.current_price,
                    realized_pnl=prow.realized_pnl,
                    commission_total=prow.commission_total,
                    opened_at=prow.opened_at,
                    last_updated=prow.last_updated,
                )
                portfolio.positions[position.symbol] = position

        logger.info(
            "portfolio %s loaded with %d open positions", portfolio.name, len(portfolio.positions)
        )
        return portfolio

    # -- misc --------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        """One-line-per-field summary for logging and the dashboard."""
        return {
            "name": self.name,
            "status": self.status,
            "cash": self.current_cash,
            "position_value": self.calculate_position_value(),
            "equity": self.calculate_total_equity(),
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.unrealized_pnl,
            "total_return": self.total_return,
            "total_return_pct": self.total_return_pct,
            "open_positions": len(self.positions),
            "closed_positions": len(self.closed_positions),
            "total_commission": self.total_commission,
            "drawdown": self.current_drawdown(),
        }

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<Portfolio {self.name!r} status={self.status} "
            f"equity={self.calculate_total_equity()} positions={len(self.positions)}>"
        )
