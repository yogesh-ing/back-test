"""Options backtest driver — Phase A (tasks A3 + A4: expression seam + loop).

This module is the **first production caller** of the options expression
layer.  Until now ``create_selector()`` → ``create_structure().build()``
was exercised only by tests and the ``/options`` web API; nothing in a
production code path ever turned a :class:`MarketView` into a
:class:`TradeIntent`.

Task A3 is the seam::

    MarketView
        → create_selector(type).pick_strikes(...)
        → create_structure(type).build(...)
        → TradeIntent

The seam is deliberately a standalone function so it can be tested — and
reused — without a loop; :class:`OptionBacktestDriver` (below) is the
bar-by-bar loop built on it (task A4).

Scope
-----
Phase A supports four structures: ``long_call``, ``long_put``,
``bull_call_spread``, ``bear_put_spread``.

The other four names accepted by ``TradeIntent._VALID_STRUCTURES``
(``straddle``, ``strangle``, ``iron_condor``, ``calendar_spread``) are
rejected here with a specific error rather than a generic one.  They are
not merely unimplemented: a chain is ``dict[Decimal, OptionContract]`` —
keyed by strike and **single-sided** (``generate_chain`` materialises CE
*or* PE), and ``OptionStructure.build()`` takes exactly **one** expiry.
So a two-sided or multi-expiry structure cannot be represented at all;
widening the scope needs the chain-shape change described in the PRD §3.

Determinism
-----------
Nothing here reads a clock or a random source.  The caller supplies
``expiry`` and the view supplies ``bar_timestamp``; there is no
``date.today()`` fallback anywhere in this path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Callable, Mapping

import pandas as pd

from backtest.instruments.option import OptionContract
from backtest.options.expiry import ExpiryManager
from backtest.options.paper_trading import (
    InsufficientMarginError,
    OptionPaperBroker,
    PositionStatus,
    StructurePosition,
)
from backtest.options.quote_providers import (
    SyntheticChainGenerator,
    SyntheticQuoteProvider,
)
from backtest.options.selector import create_selector
from backtest.options.structures import create_structure
from backtest.strategy.intent import Direction, MarketView, TradeIntent
from backtest.strategies.option_directional import DirectionalOptions

#: Structures buildable in Phase A.  Everything else needs a chain shape
#: that can hold both option types and more than one expiry.
PHASE_A_STRUCTURES: frozenset[str] = frozenset(
    {
        "long_call",
        "long_put",
        "bull_call_spread",
        "bear_put_spread",
    }
)

#: Above this conviction the default decider buys an outright option
#: (uncapped upside) instead of a defined-risk spread.
DEFAULT_SPREAD_THRESHOLD = 0.7


class UnsupportedStructureError(ValueError):
    """The requested structure is outside Phase A scope.

    Raised instead of silently skipping, because the structure choice is
    configuration, not a market condition — a mapping that asks for a
    straddle is a bug in the caller, not a trade the market declined.
    """


#: Callable mapping a view to a structure name, or ``None`` for no trade.
StructureDecider = Callable[[MarketView], "str | None"]


def default_structure_decider(view: MarketView) -> str | None:
    """Choose a Phase A structure from direction and confidence.

    **This is policy, not mechanism.**  It exists so the seam is usable out
    of the box; a backtest with different ideas injects its own
    ``decider`` into :func:`build_intent_from_view`.

    The rule:

    =========================  ==============================
    View                       Structure
    =========================  ==============================
    ``NEUTRAL``                ``None`` — no trade
    bullish, conviction ≥ 0.7  ``long_call`` (uncapped upside)
    bullish, below 0.7         ``bull_call_spread`` (cheap, capped)
    bearish, conviction ≥ 0.7  ``long_put``
    bearish, below 0.7         ``bear_put_spread``
    =========================  ==============================

    Note the comparisons are against the :class:`Direction` **enum**.
    Comparing ``view.direction`` to a string is always ``False`` and would
    silently produce a zero-trade backtest — do not "simplify" this.
    """
    if view.direction is Direction.BULLISH:
        return (
            "long_call"
            if view.confidence >= DEFAULT_SPREAD_THRESHOLD
            else "bull_call_spread"
        )
    if view.direction is Direction.BEARISH:
        return (
            "long_put"
            if view.confidence >= DEFAULT_SPREAD_THRESHOLD
            else "bear_put_spread"
        )
    return None


def build_intent_from_view(
    view: MarketView | None,
    chain: dict[Decimal, OptionContract],
    expiry: date,
    *,
    strategy_name: str = "",
    selector_type: str = "atm",
    selector_kwargs: Mapping[str, Any] | None = None,
    decider: StructureDecider | None = None,
) -> TradeIntent | None:
    """Turn a market view plus an option chain into an executable intent.

    Parameters
    ----------
    view:
        The strategy's directional view.  ``None`` or ``NEUTRAL`` means
        "no option trade this bar".
    chain:
        Contracts for one expiry, keyed by strike.  One side only — pass a
        ``"CE"`` chain for bullish structures and a ``"PE"`` chain for
        bearish ones, because the shape cannot hold both.
    expiry:
        Expiry of every contract in ``chain``.
    strategy_name:
        Recorded on the intent for audit.
    selector_type:
        ``"atm"`` (default), ``"delta"``, ``"fixed_distance"`` or
        ``"target_price"`` — see :func:`create_selector`.
    selector_kwargs:
        Forwarded to the selector constructor.
    decider:
        Structure policy.  Defaults to :func:`default_structure_decider`.

    Returns
    -------
    A :class:`TradeIntent`, or ``None`` when there is no trade to make: a
    neutral or missing view, a decider that declines, a missing spot, or a
    chain too thin for the chosen structure.

    Raises
    ------
    UnsupportedStructureError
        The decider chose a structure outside :data:`PHASE_A_STRUCTURES`.
    """
    if view is None or view.direction is Direction.NEUTRAL:
        return None
    if not chain:
        return None
    if float(view.spot_price or 0) <= 0:
        # Without a spot, strike selection degrades to "lowest strike"
        # instead of "nearest strike" — a silently wrong trade.
        return None

    structure_type = (decider or default_structure_decider)(view)
    if structure_type is None:
        return None
    if structure_type not in PHASE_A_STRUCTURES:
        raise UnsupportedStructureError(
            f"'{structure_type}' is not a Phase A structure. "
            f"Supported: {sorted(PHASE_A_STRUCTURES)}. "
            f"straddle, strangle, iron_condor and calendar_spread need a "
            f"two-sided / multi-expiry chain, which the current "
            f"dict[strike] -> OptionContract shape cannot represent "
            f"(PRD section 3)."
        )

    structure = create_structure(structure_type)
    selector = create_selector(selector_type, **(dict(selector_kwargs or {})))

    strikes = selector.pick_strikes(
        view.spot_price,
        sorted(chain),
        view.direction,
        view=view,
        count=structure.max_legs,
    )
    if len(strikes) < structure.min_legs:
        # Chain too thin around the selected strikes to build the structure.
        return None

    return structure.build(
        view=view,
        strikes=strikes,
        chain=chain,
        expiry=expiry,
        strategy_name=strategy_name,
    )


# ---------------------------------------------------------------------------
# Trade log and equity curve (task A2)
# ---------------------------------------------------------------------------
# The equity backtester reconstructs "a trade" as a run of consecutive bars
# holding the same position sign (``backtest.engine.trades.walk_trades``).
# That model cannot describe an options book: several structures are open at
# once, each with its own legs, expiry and exit reason, and a run of bars says
# nothing about which structure was which.
#
# So the options trade log is built from the structures themselves rather than
# inferred from an equity curve.  Portfolio-level metrics — drawdown, Sharpe,
# total return — can still come off the equity series (that part of
# ``compute_metrics`` needs only ``equity``); the per-trade part cannot be
# reused.  Recorded in the task tracker as the A5 design constraint.
# ---------------------------------------------------------------------------


def _json_safe(value: Any) -> Any:
    """Coerce Decimal / date / datetime to JSON-serialisable forms.

    Money stays a **string**: the options layer is Decimal-exact throughout,
    and round-tripping ₹ through a float is how rounding bugs get introduced.
    Numeric consumers should read the dataclass fields, not this dict.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


@dataclass(frozen=True)
class StructureTradeRecord:
    """One option structure, flattened for a trade log.

    Covers closed *and* still-open structures; ``is_open`` says which, and
    mirrors the equity engine's convention of listing the open trade while
    excluding it from win/loss statistics.
    """

    structure_id: str
    structure_type: str
    strategy_name: str
    underlying: str
    expiry: date
    opened_at: datetime
    closed_at: datetime | None
    exit_reason: str | None
    is_open: bool
    legs: tuple[dict[str, Any], ...]
    net_entry_cost: Decimal
    realized_pnl: Decimal
    commission: Decimal

    @property
    def leg_count(self) -> int:
        return len(self.legs)

    @property
    def is_win(self) -> bool:
        """Only a *closed* structure with positive P&L is a win — an open
        position has no result yet."""
        return not self.is_open and self.realized_pnl > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "structure_id": self.structure_id,
            "structure_type": self.structure_type,
            "strategy_name": self.strategy_name,
            "underlying": self.underlying,
            "expiry": _json_safe(self.expiry),
            "opened_at": _json_safe(self.opened_at),
            "closed_at": _json_safe(self.closed_at) if self.closed_at else None,
            "exit_reason": self.exit_reason,
            "is_open": self.is_open,
            "leg_count": self.leg_count,
            "legs": [dict(leg) for leg in self.legs],
            "net_entry_cost": _json_safe(self.net_entry_cost),
            "realized_pnl": _json_safe(self.realized_pnl),
            "commission": _json_safe(self.commission),
        }


def _leg_record(position) -> dict[str, Any]:
    """Flatten one leg. ``exit_price`` is ``None`` while the leg is open —
    ``current_price`` is then the MTM price, not an exit."""
    closed = position.status is not PositionStatus.OPEN
    return {
        "trading_symbol": position.trading_symbol,
        "side": position.side,
        "quantity": position.quantity,
        "lot_size": position.lot_size,
        "strike": _json_safe(position.strike),
        "option_type": position.option_type,
        "status": position.status.value,
        "entry_price": _json_safe(position.entry_price),
        "exit_price": _json_safe(position.current_price) if closed else None,
        "realized_pnl": _json_safe(position.realized_pnl),
    }


def structure_to_record(structure: StructurePosition) -> StructureTradeRecord:
    """Flatten one structure into a trade-log record."""
    return StructureTradeRecord(
        structure_id=structure.structure_id,
        structure_type=structure.structure_type,
        strategy_name=structure.strategy_name,
        underlying=structure.underlying,
        expiry=structure.expiry,
        opened_at=structure.opened_at,
        closed_at=structure.closed_at,
        exit_reason=structure.exit_reason,
        is_open=structure.is_open,
        legs=tuple(_leg_record(leg) for leg in structure.legs),
        net_entry_cost=structure.total_entry_cost,
        realized_pnl=structure.total_realized_pnl,
        commission=structure.total_commission,
    )


def build_trade_log(
    broker: OptionPaperBroker,
    *,
    include_open: bool = True,
) -> list[StructureTradeRecord]:
    """Every structure the book knows about, oldest first.

    Ordering depends on two facts, neither of which involves an identifier
    (so it stays deterministic even before task A6 lands): ``list.sort`` is
    stable, and the broker tracks structures in a dict, which preserves
    insertion order.  Structures opened at the same timestamp therefore
    come out in execution order.
    """
    structures = broker.get_closed_structures()
    if include_open:
        structures = broker.get_open_structures() + structures
    records = [structure_to_record(structure) for structure in structures]
    records.sort(key=lambda record: record.opened_at)
    return records


@dataclass(frozen=True)
class EquityPoint:
    """One dated snapshot of the book — a point on the equity curve."""

    timestamp: datetime
    equity: Decimal
    cash: Decimal
    costs_paid: Decimal
    margin_used: Decimal

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": _json_safe(self.timestamp),
            "equity": _json_safe(self.equity),
            "cash": _json_safe(self.cash),
            "costs_paid": _json_safe(self.costs_paid),
            "margin_used": _json_safe(self.margin_used),
        }


def capture_equity(broker: OptionPaperBroker, timestamp: datetime) -> EquityPoint:
    """Snapshot the book.  Call once per bar, **after** MTM and expiry."""
    return EquityPoint(
        timestamp=timestamp,
        equity=broker.total_equity,
        cash=broker.available_cash,
        costs_paid=broker.total_costs_paid,
        margin_used=broker.total_margin_used,
    )


# ---------------------------------------------------------------------------
# The driver loop (task A4)
# ---------------------------------------------------------------------------


def _last_thursday(year: int, month: int) -> date:
    """Last Thursday of the given month — the NSE monthly expiry convention."""
    if month == 12:
        first_next = date(year + 1, 1, 1)
    else:
        first_next = date(year, month + 1, 1)
    last_day = first_next - timedelta(days=1)
    offset = (last_day.weekday() - 3) % 7  # Thursday == 3
    return last_day - timedelta(days=offset)


def next_monthly_expiry(reference: date) -> date:
    """Last Thursday of the reference month (or next month if it has passed).

    A pure function of ``reference`` — deliberately **no**
    ``date.today()`` fallback, unlike
    :meth:`SyntheticChainGenerator.next_monthly_expiry`, so the driver
    stays deterministic over historical bars (PRD §9).
    """
    this_month = _last_thursday(reference.year, reference.month)
    if this_month >= reference:
        return this_month
    if reference.month == 12:
        return _last_thursday(reference.year + 1, 1)
    return _last_thursday(reference.year, reference.month + 1)


@dataclass(frozen=True)
class BacktestConfig:
    """Knobs for one options backtest run.

    ``capital`` and ``slippage_pct`` map straight onto the broker;
    ``selector_type`` / ``selector_kwargs`` / ``decider`` are the seam's
    injection points; ``squareoff_minutes_before`` is passed to the
    ``ExpiryManager``.
    """

    capital: float = 1_000_000.0
    slippage_pct: float = 0.001
    commission_per_lot: float = 20.0
    selector_type: str = "atm"
    selector_kwargs: Mapping[str, Any] = field(default_factory=dict)
    decider: StructureDecider | None = None
    strategy_params: Mapping[str, Any] = field(default_factory=dict)
    squareoff_minutes_before: int = 0
    max_open_structures: int | None = None  # PRD risk.max_positions; None = unlimited


@dataclass(frozen=True)
class BacktestResult:
    """Everything one run produced — trade log, equity curve, alerts, and
    a metrics summary.  Money fields stay :class:`Decimal`; call ``to_dict``
    for the JSON-safe shape.
    """

    trade_log: list[StructureTradeRecord]
    equity_curve: list[EquityPoint]
    alerts: list[dict[str, Any]]
    metrics: dict[str, Any]
    config: BacktestConfig

    def to_dict(self) -> dict[str, Any]:
        return {
            "metrics": self.metrics,
            "alerts": self.alerts,
            "trades": [record.to_dict() for record in self.trade_log],
            "equity_curve": [point.to_dict() for point in self.equity_curve],
        }


class OptionBacktestDriver:
    """Bar-by-bar options backtest over an underlying candle frame.

    Per bar, in order::

        set_spot → generate_chain → register_chain → set_reference
        → strategy.generate_market_view(candles_window)
        → build_intent_from_view (the A3 seam)
        → broker.execute_structure → broker.update_mtm
        → expiry_manager.process_expiries → capture_equity

    Two correctness-critical details baked in:

    1. **The quote provider is pinned to bar time** via
       ``set_reference`` each bar.  ``price_contract`` otherwise falls
       back to the wall clock, which over historical bars yields negative
       time-to-expiry and collapses every premium to intrinsic value.
    2. **Bars are stamped at 15:30 IST** (market close) so the expiry
       manager's minute-based square-off window means something with
       daily bars.

    Chains are single-sided (V1 shape): bullish views price against a CE
    chain, bearish views against a PE chain.  The chain is generated
    **after** the view's direction is known.
    """

    def __init__(
        self,
        candles: pd.DataFrame,
        *,
        config: BacktestConfig | None = None,
        underlying: str = "NIFTY",
        generator: SyntheticChainGenerator | None = None,
        broker: OptionPaperBroker | None = None,
        strategy: Any | None = None,
        expiry_manager: ExpiryManager | None = None,
    ) -> None:
        """All collaborators are injectable for testing; each ``None``
        default constructs the production default."""
        self.candles = candles
        self.config = config or BacktestConfig()
        self.underlying = underlying

        self.generator = generator or SyntheticChainGenerator()
        self.quote_provider = SyntheticQuoteProvider(self.generator)
        self.broker = broker or OptionPaperBroker(
            capital=self.config.capital,
            slippage_pct=self.config.slippage_pct,
            commission_per_lot=self.config.commission_per_lot,
        )
        self.strategy = strategy or DirectionalOptions(**dict(self.config.strategy_params))
        self.expiry_manager = expiry_manager or ExpiryManager(
            self.broker,
            squareoff_minutes_before=self.config.squareoff_minutes_before,
        )

        # Settlement off the generator's spot — the same synthetic world
        # the options priced against (still injectable for tests).
        self.settlement_provider = _GeneratorSettlementProvider(self.generator)

        self.trade_log: list[StructureTradeRecord] = []
        self.equity_curve: list[EquityPoint] = []
        self._skipped_entries: list[dict[str, Any]] = []

    # -- main loop -------------------------------------------------------

    def run(self) -> BacktestResult:
        """Execute the loop over every bar and compile the result."""
        for bar_time, bar in self.candles.iterrows():
            timestamp = self._bar_timestamp(bar_time)
            self._process_bar(timestamp, bar)
        return self._compile()

    def _process_bar(self, timestamp: datetime, bar: pd.Series) -> None:
        # 1. Spot + pricing reference
        close = float(bar["close"])
        self.generator.set_spot(self.underlying, close)
        self.quote_provider.set_reference(timestamp)

        # 2. Strategy view on the full history up to and including this bar
        window = self.candles.loc[: bar.name]
        view = self.strategy.generate_market_view(window)

        # 3. Chain after the view: bullish → CE chain, bearish → PE chain
        expiry = next_monthly_expiry(timestamp.date())
        chain = self._chain_for(view, expiry)
        self.quote_provider.register_chain(chain)

        # 4. Entry attempt (the A3 seam)
        if view is not None and chain:
            self._try_entry(view, chain, expiry, timestamp)

        # 5. MTM, expiry pipeline, snapshot
        self.broker.update_mtm(self.quote_provider, timestamp)
        self.expiry_manager.process_expiries(
            self.quote_provider,
            self.settlement_provider,
            as_of=timestamp,
        )
        self.equity_curve.append(capture_equity(self.broker, timestamp))

    def _try_entry(
        self,
        view: MarketView,
        chain: dict[Decimal, OptionContract],
        expiry: date,
        timestamp: datetime,
    ) -> None:
        cap = self.config.max_open_structures
        if cap is not None and len(self.broker.get_open_structures()) >= cap:
            self._skipped_entries.append(
                {"timestamp": timestamp, "reason": "max_open_structures", "error": f"cap={cap}"}
            )
            return
        try:
            intent = build_intent_from_view(
                view,
                chain,
                expiry,
                strategy_name=self.strategy.name,
                selector_type=self.config.selector_type,
                selector_kwargs=self.config.selector_kwargs,
                decider=self.config.decider,
            )
            if intent is None:
                return
            positions = self.broker.execute_structure(
                intent, self.quote_provider, timestamp
            )
            if positions:
                self._on_position_opened(view, positions, timestamp)
        except UnsupportedStructureError:
            raise  # configuration bug — never hide it
        except InsufficientMarginError as exc:
            self._skipped_entries.append(
                {"timestamp": timestamp, "reason": "insufficient_margin", "error": str(exc)}
            )

    def _on_position_opened(
        self,
        view: MarketView,
        positions: list,
        timestamp: datetime,
    ) -> None:
        """Hook for bookkeeping when a structure fills (e.g. entry logging)."""
        return None

    def _chain_for(
        self,
        view: MarketView | None,
        expiry: date,
    ) -> dict[Decimal, OptionContract]:
        """Single-sided chain matching the view's direction (or CE by
        default when there is no view — MTM/expiry still need a chain)."""
        option_type = "CE"
        if view is not None and view.direction is Direction.BEARISH:
            option_type = "PE"
        return self.generator.generate_chain(
            self.underlying,
            expiry=expiry,
            option_type=option_type,
        )

    def _bar_timestamp(self, bar_time: Any) -> datetime:
        """Combine the bar's date with the 15:30 IST market close."""
        if isinstance(bar_time, pd.Timestamp):
            d = bar_time.date()
        else:
            d = pd.Timestamp(bar_time).date()
        return datetime.combine(d, time(15, 30))

    def _compile(self) -> BacktestResult:
        self.trade_log = build_trade_log(self.broker, include_open=True)
        equity = [float(point.equity) for point in self.equity_curve]
        closed = [r for r in self.trade_log if not r.is_open]
        wins = [r for r in closed if r.is_win]
        metrics = {
            "total_trades": len(self.trade_log),
            "closed_trades": len(closed),
            "win_rate": (len(wins) / len(closed)) if closed else None,
            "realized_pnl": str(sum((r.realized_pnl for r in closed), Decimal("0"))),
            "total_commission": str(sum((r.commission for r in closed), Decimal("0"))),
            "final_equity": str(self.equity_curve[-1].equity) if self.equity_curve else None,
            "max_drawdown": _max_drawdown(equity),
            "skipped_entries": len(self._skipped_entries),
        }
        return BacktestResult(
            trade_log=self.trade_log,
            equity_curve=self.equity_curve,
            alerts=[alert.to_dict() for alert in self.expiry_manager.alerts],
            metrics=metrics,
            config=self.config,
        )


def _max_drawdown(equity: list[float]) -> float | None:
    """Max peak-to-trough decline as a negative fraction (None if flat)."""
    if len(equity) < 2:
        return None
    peak = equity[0]
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, (value - peak) / peak)
    return worst


class _GeneratorSettlementProvider:
    """Settlement price = the generator's current spot for the underlying."""

    def __init__(self, generator: SyntheticChainGenerator) -> None:
        self._generator = generator

    def get_settlement_price(self, underlying: str) -> float:
        return self._generator.get_spot(underlying)
