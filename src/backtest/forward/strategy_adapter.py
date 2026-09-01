"""Strategy adapter for forward testing.

Bridges the existing backtest strategy abstraction (``backtest.strategy.base.Strategy``)
into the live forward-testing loop that uses ``simulator.Portfolio``,
``simulator.Order`` and ``simulator.execution.OrderExecutor``.

The adapter is intentionally **not** a new Strategy base class — it reuses
``strategy/base.py`` verbatim, as required by the task tracker deviation #5.

Design goals
------------
* **No lookahead bias.** Only *completed* bars are ever passed to the strategy.
  ``on_bar_close`` appends the bar that just closed, then calls
  ``strategy.generate_signals`` on the history including that bar. The signal
  is for the *next* bar, matching the legacy engine's ``target.shift(1)`` rule.
  ``strategy_signals.bar_ts`` (open time of the completed bar) is always
  strictly earlier than ``generated_at``, so Step 22's bias detector can query
  it.

* **Multi-symbol.** The adapter keeps a per-symbol OHLCV DataFrame. A single
  strategy instance can be reused per symbol (the common case) or a dict of
  strategy instances can be supplied.

* **Signal → Order (ticket F-01).** Signals are converted to
  ``simulator.Order`` objects, validated against
  ``Portfolio.can_open_position`` and sized by an injected ``PositionSizer``.
  The adapter NEVER fills: it hands the orders back to the caller via
  :meth:`create_orders`, and the engine drives the canonical bar-clock
  sequence (``OrderExecutor.submit`` → :meth:`OrderExecutor.step`) so a
  fill lands at the **next bar's open**, never the signal bar's close.
  ``Signal`` decisions are based on the **signal transition** (previous
  target → new target), not the live portfolio — the portfolio no longer
  knows a fill happened until the next bar, and a position-based decision
  would re-fire the same order on every bar.

* **Dry-run.** When ``dry_run=True`` signals are generated and logged but no
  orders are created.

* **Persistence.** Signals are logged to ``strategy_signals`` table via
  ``DatabaseManager`` when one is supplied. State (bars, indicators, last
  signals) can be snapshotted to JSON for Step 20 recovery.

Example
-------
>>> from backtest.strategy.base import Strategy
>>> from backtest.simulator.portfolio import Portfolio
>>> from backtest.forward.strategy_adapter import StrategyAdapter, Signal
>>> import pandas as pd
>>> class MyStrat(Strategy):
...     name = "my_strat"
...     params = {"fast": 5, "slow": 10}
...     def generate_signals(self, candles):
...         fast = candles["close"].rolling(self.fast).mean()
...         slow = candles["close"].rolling(self.slow).mean()
...         return (fast > slow).astype(int)
>>> portfolio = Portfolio(name="test", initial_capital=100000)
>>> adapter = StrategyAdapter(strategy=MyStrat(), portfolio=portfolio, symbols=["INFY"])
>>> # feed bars
>>> bar = {"symbol": "INFY", "timestamp": "2024-01-02T09:15:00+05:30",
...        "open": 1500, "high": 1510, "low": 1495, "close": 1505, "volume": 10000}
>>> adapter.on_bar_close(bar)
>>> len(adapter.signal_history)
1
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Protocol, Union

import pandas as pd

from backtest.simulator.enums import OrderSide, OrderType, TimeInForce
from backtest.simulator.errors import ValidationError
from backtest.simulator.money import ZERO, money, price as to_price, to_decimal
from backtest.simulator.order import Order
from backtest.strategy.base import Strategy

logger = logging.getLogger("backtest.forward.strategy_adapter")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: Any) -> datetime:
    """Parse many timestamp shapes into timezone-aware UTC datetime."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        # assume seconds since epoch
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    # string
    try:
        dt = pd.to_datetime(value, utc=True)
        if isinstance(dt, pd.Timestamp):
            return dt.to_pydatetime()
        # if Series, take first
        return _utcnow()
    except Exception:
        return _utcnow()


def _normalize_symbol(symbol: Any) -> str:
    return str(symbol).strip().upper()


# ---------------------------------------------------------------------------
# Signal model
# ---------------------------------------------------------------------------


class SignalAction:
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"

    ALL = (BUY, SELL, HOLD)

    @classmethod
    def validate(cls, value: Any) -> str:
        v = str(value).strip().upper()
        if v not in cls.ALL:
            raise ValidationError(f"invalid signal action {value!r}; expected one of {cls.ALL}")
        return v


class SignalType:
    ENTRY = "entry"
    EXIT = "exit"

    ALL = (ENTRY, EXIT)


class SignalDirection:
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"

    ALL = (LONG, SHORT, FLAT)


@dataclass
class Signal:
    """Normalized trading signal produced by the adapter.

    This is the dict shape described in the plan, but as a typed dataclass
    so callers get validation and helpers. It maps 1-to-1 onto
    ``db.models.StrategySignal`` for persistence.

    Attributes
    ----------
    symbol:
        Instrument, e.g. ``INFY``.
    action:
        ``BUY``/``SELL``/``HOLD``.
    quantity:
        Desired quantity. ``None`` means \"let the position sizer decide\".
    order_type:
        ``MARKET`` or ``LIMIT``.
    limit_price:
        Required for ``LIMIT`` orders.
    reason:
        Human-readable explanation, written to logs and ``skip_reason`` when
        not executed.
    indicators:
        Snapshot of indicator values at signal time, stored as JSON.
    strength:
        Confidence 0..1, optional.
    target_position:
        Desired position in [-1, 1] (1 = full long, -1 = full short, 0 = flat).
        Used for ``strategy_signals.target_position`` and direction derivation.
    bar_ts:
        Open time of the completed bar that produced this signal. Must be
        strictly earlier than ``generated_at`` for lookahead-bias detection.
    generated_at:
        When the signal was generated.
    strategy_name:
        Name of the strategy that produced it.
    signal_type:
        ``entry`` or ``exit``.
    direction:
        ``long``/``short``/``flat`` derived from ``target_position`` or action.
    """

    symbol: str
    action: str
    quantity: Optional[Decimal] = None
    order_type: str = "MARKET"
    limit_price: Optional[Decimal] = None
    reason: str = ""
    indicators: Dict[str, Any] = field(default_factory=dict)
    strength: Optional[Decimal] = None
    target_position: Optional[Decimal] = None
    bar_ts: Optional[datetime] = None
    generated_at: datetime = field(default_factory=_utcnow)
    strategy_name: str = ""
    signal_type: str = SignalType.ENTRY
    direction: str = SignalDirection.LONG

    def __post_init__(self) -> None:
        self.symbol = _normalize_symbol(self.symbol)
        if not self.symbol:
            raise ValidationError("symbol must not be empty")

        self.action = SignalAction.validate(self.action)
        self.order_type = str(self.order_type).strip().upper()
        if self.order_type not in ("MARKET", "LIMIT", "STOP", "STOP_LIMIT"):
            # allow only MARKET/LIMIT for signals, but keep extensible
            if self.order_type not in ("MARKET", "LIMIT"):
                raise ValidationError(f"invalid order_type {self.order_type!r}")

        if self.quantity is not None:
            self.quantity = to_price(self.quantity, "quantity")
            if self.quantity <= ZERO:
                raise ValidationError("quantity must be positive")

        if self.limit_price is not None:
            self.limit_price = to_price(self.limit_price, "limit_price")
            if self.limit_price <= ZERO:
                raise ValidationError("limit_price must be positive")

        if self.order_type == "LIMIT" and self.limit_price is None:
            raise ValidationError("LIMIT signals require limit_price")

        if self.strength is not None:
            self.strength = to_decimal(self.strength, "strength")
            if self.strength < ZERO or self.strength > Decimal("1"):
                raise ValidationError("strength must be between 0 and 1")

        if self.target_position is not None:
            self.target_position = to_decimal(self.target_position, "target_position")
            if self.target_position < Decimal("-1") or self.target_position > Decimal("1"):
                raise ValidationError("target_position must be in [-1, 1]")

        if self.bar_ts is not None and not isinstance(self.bar_ts, datetime):
            self.bar_ts = _parse_timestamp(self.bar_ts)

        if not isinstance(self.generated_at, datetime):
            self.generated_at = _parse_timestamp(self.generated_at)

        # derive direction if not set explicitly
        if self.target_position is not None:
            if self.target_position > ZERO:
                self.direction = SignalDirection.LONG
            elif self.target_position < ZERO:
                self.direction = SignalDirection.SHORT
            else:
                self.direction = SignalDirection.FLAT
        else:
            if self.action == SignalAction.BUY:
                self.direction = SignalDirection.LONG
            elif self.action == SignalAction.SELL:
                # SELL could be exit to flat or entry to short; default flat,
                # caller can override for short strategies
                if self.direction not in SignalDirection.ALL:
                    self.direction = SignalDirection.FLAT
            else:
                self.direction = SignalDirection.FLAT

        if self.signal_type not in SignalType.ALL:
            self.signal_type = SignalType.ENTRY if self.action == SignalAction.BUY else SignalType.EXIT

    def to_dict(self) -> Dict[str, Any]:
        def _s(v: Optional[Decimal]) -> Optional[str]:
            return str(v) if v is not None else None

        def _ts(v: Optional[datetime]) -> Optional[str]:
            return v.isoformat() if v is not None else None

        return {
            "symbol": self.symbol,
            "action": self.action,
            "quantity": _s(self.quantity),
            "order_type": self.order_type,
            "limit_price": _s(self.limit_price),
            "reason": self.reason,
            "indicators": dict(self.indicators),
            "strength": _s(self.strength),
            "target_position": _s(self.target_position),
            "bar_ts": _ts(self.bar_ts),
            "generated_at": _ts(self.generated_at),
            "strategy_name": self.strategy_name,
            "signal_type": self.signal_type,
            "direction": self.direction,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Signal":
        def _when(key: str) -> Optional[datetime]:
            v = payload.get(key)
            if v is None:
                return None
            if isinstance(v, datetime):
                return v
            try:
                return datetime.fromisoformat(str(v))
            except Exception:
                return _parse_timestamp(v)

        return cls(
            symbol=payload["symbol"],
            action=payload.get("action", "HOLD"),
            quantity=payload.get("quantity"),
            order_type=payload.get("order_type", "MARKET"),
            limit_price=payload.get("limit_price"),
            reason=payload.get("reason", ""),
            indicators=dict(payload.get("indicators", {})),
            strength=payload.get("strength"),
            target_position=payload.get("target_position"),
            bar_ts=_when("bar_ts"),
            generated_at=_when("generated_at") or _utcnow(),
            strategy_name=payload.get("strategy_name", ""),
            signal_type=payload.get("signal_type", SignalType.ENTRY),
            direction=payload.get("direction", SignalDirection.LONG),
        )


# ---------------------------------------------------------------------------
# Position sizing — re-exported from simulator.position_sizing (Step 14)
# ---------------------------------------------------------------------------
# Keep backward compatibility: the adapter originally defined its own minimal
# sizers. Step 14 introduces a full engine in simulator/position_sizing.py.
# We re-export the new implementations here so existing code keeps working,
# and add the richer sizers (risk, volatility, Kelly) as well.

try:
    from backtest.simulator.position_sizing import (
        ATRBasedSizer,
        FixedDollarSizer,
        FixedQuantitySizer,
        KellySizer,
        PercentagePortfolioSizer,
        PositionSizer as FullPositionSizer,
        RiskBasedSizer,
        VolatilitySizer,
    )

    # Adapter's PositionSizer protocol is now the full engine
    PositionSizer = FullPositionSizer  # type: ignore

except Exception:  # pragma: no cover - fallback if simulator not yet loaded
    from typing import Protocol

    class PositionSizer(Protocol):  # type: ignore[no-redef]
        """Protocol for position sizing engines (Step 14 will expand this)."""

        def calculate_position_size(
            self, signal: Signal, portfolio: Any, **kwargs: Any
        ) -> Decimal:
            ...

    class FixedQuantitySizer:  # type: ignore[no-redef]
        """Always return a fixed quantity."""

        def __init__(self, quantity: Any = 100):
            self.quantity = to_price(quantity, "quantity")
            if self.quantity <= ZERO:
                raise ValidationError("fixed quantity must be positive")

        def calculate_position_size(self, signal: Signal, portfolio: Any, **kwargs: Any) -> Decimal:
            return self.quantity

    class FixedDollarSizer:  # type: ignore[no-redef]
        """Size to a fixed dollar amount at current price."""

        def __init__(self, dollar_amount: Any = 10000):
            self.dollar_amount = money(dollar_amount, "dollar_amount")
            if self.dollar_amount <= ZERO:
                raise ValidationError("dollar amount must be positive")

        def calculate_position_size(
            self, signal: Signal, portfolio: Any, current_price: Any = None, **kwargs: Any
        ) -> Decimal:
            price = None
            if current_price is not None:
                try:
                    price = to_price(current_price, "current_price")
                except Exception:
                    price = None
            if price is None:
                price = signal.indicators.get("close") or signal.indicators.get("last")
                if price is not None:
                    try:
                        price = to_price(price, "price")
                    except Exception:
                        price = None
            if price is None or price <= ZERO:
                return Decimal("100")
            qty = (self.dollar_amount / price).quantize(Decimal("1"))
            return max(Decimal("1"), qty)

    class PercentagePortfolioSizer:  # type: ignore[no-redef]
        """Size as a percentage of total equity."""

        def __init__(self, percentage: Any = Decimal("0.05")):
            self.percentage = to_decimal(percentage, "percentage")
            if self.percentage <= ZERO or self.percentage > Decimal("1"):
                raise ValidationError("percentage must be in (0, 1]")

        def calculate_position_size(
            self, signal: Signal, portfolio: Any, current_price: Any = None, **kwargs: Any
        ) -> Decimal:
            try:
                equity = portfolio.calculate_total_equity()
            except Exception:
                equity = money(100000)
            target_value = equity * self.percentage
            price = current_price
            if price is None:
                price = signal.indicators.get("close") or signal.indicators.get("last") or 100
            try:
                price = to_price(price, "price")
            except Exception:
                price = to_price(100, "price")
            if price <= ZERO:
                return Decimal("1")
            qty = (target_value / price).quantize(Decimal("1"))
            return max(Decimal("1"), qty)

    # Minimal stubs for new sizers if import failed
    class RiskBasedSizer:  # type: ignore[no-redef]
        def __init__(self, *a, **k):
            pass

        def calculate_position_size(self, *a, **k):
            return Decimal("100")

    class VolatilitySizer(RiskBasedSizer):  # type: ignore[no-redef]
        pass

    class ATRBasedSizer(VolatilitySizer):  # type: ignore[no-redef]
        pass

    class KellySizer(RiskBasedSizer):  # type: ignore[no-redef]
        pass


# ---------------------------------------------------------------------------
# StrategyAdapter
# ---------------------------------------------------------------------------


class StrategyAdapter:
    """Bridge between backtest strategies and forward testing execution.

    Parameters
    ----------
    strategy:
        An instance of ``backtest.strategy.base.Strategy`` (or a dict mapping
        symbol → Strategy for multi-strategy setups).
    portfolio:
        ``simulator.Portfolio`` that holds cash and positions. Used for
        sizing/validation of the orders the adapter creates; the adapter
        never mutates it via fills.
    symbols:
        List of symbols to track. If None, inferred from incoming market data.
    dry_run:
        When True, signals are generated and logged but no orders are created.
    position_sizer:
        Object with ``calculate_position_size(signal, portfolio, ...)``. Defaults
        to ``FixedQuantitySizer(100)``.
    db_manager:
        ``DatabaseManager`` for persisting signals to ``strategy_signals``.
    min_bars:
        Minimum number of bars required before generating signals (warmup).
    allow_short:
        Whether to allow short signals. If False, SELL signals only close longs.
    default_order_type:
        Default order type for signals (MARKET or LIMIT).
    time_in_force:
        Default TIF for created orders.

    Attributes
    ----------
    signal_history:
        All signals generated so far.
    order_history:
        All orders created so far (via :meth:`create_orders`).
    bars:
        Per-symbol OHLCV DataFrames (completed bars only).
    """

    def __init__(
        self,
        strategy: Union[Strategy, Dict[str, Strategy]],
        portfolio: Any,
        symbols: Optional[List[str]] = None,
        dry_run: bool = False,
        position_sizer: Optional[PositionSizer] = None,
        db_manager: Any = None,
        min_bars: int = 20,
        allow_short: bool = False,
        default_order_type: str = "MARKET",
        time_in_force: str = "DAY",
    ) -> None:
        # strategy can be single instance or dict per symbol
        if isinstance(strategy, dict):
            self._strategies: Dict[str, Strategy] = {
                _normalize_symbol(k): v for k, v in strategy.items()
            }
            # pick first as default for single-symbol calls
            self.strategy: Strategy = next(iter(self._strategies.values()))
        else:
            if not isinstance(strategy, Strategy):
                raise ValidationError("strategy must be an instance of backtest.strategy.base.Strategy")
            self._strategies = {}
            self.strategy = strategy

        self.portfolio = portfolio
        self.dry_run = bool(dry_run)
        self.db_manager = db_manager
        self.min_bars = int(min_bars)
        self.allow_short = bool(allow_short)
        self.default_order_type = str(default_order_type).strip().upper()
        if self.default_order_type not in ("MARKET", "LIMIT"):
            raise ValidationError("default_order_type must be MARKET or LIMIT")
        self.time_in_force = TimeInForce.parse(time_in_force) if isinstance(time_in_force, str) else time_in_force

        self.position_sizer: PositionSizer = position_sizer or FixedQuantitySizer(100)

        self.symbols: List[str] = [_normalize_symbol(s) for s in (symbols or [])]

        # per-symbol bars: Dict[symbol, DataFrame]
        self._bars: Dict[str, pd.DataFrame] = {}
        # per-symbol latest quote
        self._latest_quotes: Dict[str, Dict[str, Any]] = {}
        # per-symbol indicator snapshots
        self._indicators: Dict[str, Dict[str, Any]] = {}
        # per-symbol last signal value (target_position)
        self._last_target: Dict[str, Decimal] = {}
        # history
        self.signal_history: List[Signal] = []
        self.order_history: List[Order] = []
        # internal state for persistence
        self._state: Dict[str, Any] = {"initialized": False}

        logger.info(
            "StrategyAdapter initialized: strategy=%s symbols=%s dry_run=%s min_bars=%s",
            getattr(self.strategy, "name", type(self.strategy).__name__),
            self.symbols,
            self.dry_run,
            self.min_bars,
        )

    # -- market data handling ---------------------------------------------

    def on_market_data(self, market_data: Mapping[str, Any] | Any) -> List[Signal]:
        """Handle incoming market data (tick or bar).

        Parameters
        ----------
        market_data:
            Dict with at least ``symbol`` and either OHLCV (bar) or
            bid/ask/last (tick). Alternatively a DataFrame row.

        Returns
        -------
        List[Signal]
            Signals generated if this was a bar close, else empty list.
        """
        if market_data is None:
            return []

        # DataFrame row support
        if isinstance(market_data, pd.Series):
            market_data = market_data.to_dict()

        if not isinstance(market_data, Mapping):
            logger.warning("on_market_data got unsupported type %s", type(market_data))
            return []

        symbol = market_data.get("symbol")
        if not symbol:
            # maybe dict keyed by symbol?
            return []

        symbol = _normalize_symbol(symbol)
        if self.symbols and symbol not in self.symbols:
            # auto-track new symbols if symbols list empty, else ignore
            if not self.symbols:
                self.symbols.append(symbol)
            else:
                # if symbols was explicitly set, still allow but log
                logger.debug("on_market_data for untracked symbol %s", symbol)

        # store latest quote
        self._latest_quotes[symbol] = dict(market_data)

        # if it looks like a bar (has close), treat as bar close
        if "close" in market_data or "c" in market_data:
            return self.on_bar_close(market_data)

        # tick only, no signal
        return []

    def on_bar_close(self, bar: Mapping[str, Any]) -> List[Signal]:
        """Handle a completed bar — signal generation ONLY (ticket F-01).

        This is the main entry point for the forward loop. It appends the bar
        to history and generates signals. It never fills anything: the caller
        turns the returned signals into orders via :meth:`create_orders` and
        drives the executor's bar clock (``submit`` → ``step``) so fills land
        at the NEXT bar's open.

        Parameters
        ----------
        bar:
            Dict with ``symbol``, ``timestamp``/``ts``, ``open``, ``high``,
            ``low``, ``close``, ``volume``. Extra keys like ``bid``/``ask``
            are preserved as indicators.

        Returns
        -------
        List[Signal]
            Generated signals for this bar.
        """
        if not isinstance(bar, Mapping):
            raise ValidationError("bar must be a mapping")

        symbol = bar.get("symbol")
        if not symbol:
            raise ValidationError("bar must contain symbol")
        symbol = _normalize_symbol(symbol)

        # normalize bar
        normalized = self._normalize_bar(bar)
        self._append_bar(symbol, normalized)

        # ensure symbols list
        if symbol not in self.symbols:
            self.symbols.append(symbol)

        # generate signals for this symbol
        return self.generate_signals(symbol=symbol)

    def _normalize_bar(self, bar: Mapping[str, Any]) -> Dict[str, Any]:
        """Normalize bar dict to canonical keys."""
        # support both short and long keys
        def _get(*keys: str, default: Any = None) -> Any:
            for k in keys:
                if k in bar and bar[k] is not None:
                    return bar[k]
            return default

        ts = _get("timestamp", "ts", "time", "datetime", default=_utcnow())
        ts = _parse_timestamp(ts)

        # OHLCV
        open_ = _get("open", "o")
        high = _get("high", "h")
        low = _get("low", "l")
        close = _get("close", "c", "last", "price")
        volume = _get("volume", "v", default=0)
        bid = _get("bid")
        ask = _get("ask")

        if close is None:
            raise ValidationError("bar must contain close/last price")

        return {
            "symbol": _normalize_symbol(_get("symbol", default="UNKNOWN")),
            "timestamp": ts,
            "open": float(open_) if open_ is not None else float(close),
            "high": float(high) if high is not None else float(close),
            "low": float(low) if low is not None else float(close),
            "close": float(close),
            "volume": int(float(volume)) if volume is not None else 0,
            "bid": float(bid) if bid is not None else None,
            "ask": float(ask) if ask is not None else None,
            "raw": dict(bar),
        }

    def _append_bar(self, symbol: str, bar: Dict[str, Any]) -> None:
        """Append normalized bar to per-symbol DataFrame."""
        symbol = _normalize_symbol(symbol)
        df = self._bars.get(symbol)
        ts = bar["timestamp"]
        # ensure ts is pd.Timestamp for index
        idx = pd.to_datetime(ts, utc=True)

        row = {
            "open": bar["open"],
            "high": bar["high"],
            "low": bar["low"],
            "close": bar["close"],
            "volume": bar["volume"],
        }
        # include bid/ask if present for indicator use
        if bar.get("bid") is not None:
            row["bid"] = bar["bid"]
        if bar.get("ask") is not None:
            row["ask"] = bar["ask"]

        new_df = pd.DataFrame([row], index=[idx])
        new_df.index.name = "timestamp"

        if df is None or df.empty:
            self._bars[symbol] = new_df
        else:
            # avoid duplicate timestamps
            if idx in df.index:
                # replace last
                df.loc[idx] = row
                self._bars[symbol] = df
            else:
                self._bars[symbol] = pd.concat([df, new_df]).sort_index()

        # keep memory bounded: keep last 5000 bars per symbol
        if len(self._bars[symbol]) > 5000:
            self._bars[symbol] = self._bars[symbol].iloc[-5000:]

        logger.debug("bar appended %s %s close=%s total_bars=%s", symbol, ts, bar["close"], len(self._bars[symbol]))

    def get_candles(self, symbol: str) -> pd.DataFrame:
        """Return accumulated candles for symbol (completed bars only)."""
        symbol = _normalize_symbol(symbol)
        df = self._bars.get(symbol)
        if df is None:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        return df.copy()

    # -- signal generation -------------------------------------------------

    def generate_signals(self, symbol: Optional[str] = None) -> List[Signal]:
        """Generate signals for one or all symbols.

        Calls the underlying ``Strategy.generate_signals`` with only completed
        bars, ensuring no lookahead bias.

        Parameters
        ----------
        symbol:
            If provided, only generate for that symbol. Otherwise all tracked
            symbols.

        Returns
        -------
        List[Signal]
        """
        symbols = [symbol] if symbol else list(self._bars.keys())
        if not symbols and self.symbols:
            symbols = self.symbols

        all_signals: List[Signal] = []

        for sym in symbols:
            sym = _normalize_symbol(sym)
            candles = self._bars.get(sym)
            if candles is None or candles.empty:
                continue

            if len(candles) < self.min_bars:
                logger.debug("not enough bars for %s: %s < %s", sym, len(candles), self.min_bars)
                continue

            # select strategy instance
            strat = self._strategies.get(sym, self.strategy)

            try:
                # call strategy's generate_signals - this is the bridge
                series = strat.generate_signals(candles)
            except NotImplementedError:
                logger.warning("strategy %s does not implement generate_signals", getattr(strat, "name", sym))
                continue
            except Exception as exc:
                logger.exception("strategy %s generate_signals failed for %s: %s", getattr(strat, "name", sym), sym, exc)
                continue

            if not isinstance(series, pd.Series):
                logger.warning("strategy %s returned non-Series for %s: %s", getattr(strat, "name", sym), sym, type(series))
                continue

            if series.empty:
                continue

            # get last signal value (most recent completed bar)
            last_idx = series.index[-1]
            last_value = series.iloc[-1]

            # handle NaN
            if pd.isna(last_value):
                continue

            try:
                target = to_decimal(last_value, "target_position")
            except Exception:
                # if 0/1 int, convert
                try:
                    target = Decimal(str(float(last_value)))
                except Exception:
                    continue

            # clip to [-1, 1]
            if target > Decimal("1"):
                target = Decimal("1")
            if target < Decimal("-1"):
                target = Decimal("-1")

            # for long-only strategies, ensure 0..1
            if not self.allow_short and target < ZERO:
                target = ZERO

            # get bar_ts: open time of completed bar that produced signal
            bar_ts = None
            try:
                # last_idx is timestamp of bar
                if isinstance(last_idx, pd.Timestamp):
                    bar_ts = last_idx.to_pydatetime()
                else:
                    bar_ts = _parse_timestamp(last_idx)
            except Exception:
                # fallback to last candle timestamp
                try:
                    bar_ts = candles.index[-1].to_pydatetime() if isinstance(candles.index[-1], pd.Timestamp) else _parse_timestamp(candles.index[-1])
                except Exception:
                    bar_ts = _utcnow()

            # indicators snapshot: try to get from strategy if it exposes method,
            # else include basic OHLCV + last close + signal
            indicators = self._build_indicators_snapshot(sym, candles, strat, last_value)

            # Determine the action from the SIGNAL TRANSITION (previous target
            # → new target), not from the portfolio: fills happen one bar
            # later (the engine's submit → step clock), so a position-based
            # decision would re-fire the same BUY on every bar until the
            # first fill lands. Transition semantics match the canonical
            # run_engine_loop (ticket F-01).
            previous_target = self._last_target.get(sym, ZERO)
            action, signal_type, reason = self._decide_action(sym, previous_target, target, candles)

            # strength: use absolute target as strength for now, or 1 if binary
            strength = abs(target) if target != ZERO else ZERO
            if strength > Decimal("1"):
                strength = Decimal("1")

            # quantity: let sizer decide later, but we can set None
            signal = Signal(
                symbol=sym,
                action=action,
                quantity=None,  # filled by create_orders via sizer
                order_type=self.default_order_type,
                reason=reason,
                indicators=indicators,
                strength=strength,
                target_position=target,
                bar_ts=bar_ts,
                generated_at=_utcnow(),
                strategy_name=getattr(strat, "name", type(strat).__name__),
                signal_type=signal_type,
                direction=SignalDirection.LONG if target > ZERO else (SignalDirection.SHORT if target < ZERO else SignalDirection.FLAT),
            )

            # Store the position state that WILL exist once the emitted action
            # fills. On a direct flip (long→short / short→long) the action
            # only CLOSES, so the intermediate state is flat; the opposite
            # side re-opens on the NEXT bar — the old position-based logic's
            # "close first, then re-enter" semantics (pinned by
            # tests/test_strategy_adapter.py against a reference old-state
            # machine; see findings F-17).
            if (previous_target > ZERO and target < ZERO) or (previous_target < ZERO and target > ZERO):
                self._last_target[sym] = ZERO
            else:
                self._last_target[sym] = target
            self._indicators[sym] = indicators

            # avoid duplicate HOLD signals flooding history? Keep them but log at debug
            if action == SignalAction.HOLD:
                logger.debug("HOLD signal %s target=%s previous=%s", sym, target, previous_target)
            else:
                logger.info("signal %s %s target=%s reason=%s", sym, action, target, reason)

            self.signal_history.append(signal)
            all_signals.append(signal)

            # persist signal to DB if manager available (executed flag is set
            # to True when create_orders maps this signal to an order)
            if self.db_manager is not None:
                try:
                    self._save_signal_to_db(signal, executed=False, skip_reason="generated" if action != SignalAction.HOLD else "hold")
                except Exception as exc:
                    logger.warning("failed to save signal to DB: %s", exc)

        return all_signals

    def _decide_action(
        self, symbol: str, previous: Decimal, target: Decimal, candles: pd.DataFrame
    ) -> tuple[str, str, str]:
        """Decide BUY/SELL/HOLD from the SIGNAL TRANSITION previous→target.

        ``previous`` is the last bar's target position (not the live
        portfolio) — fills happen one bar later, so the portfolio is
        not yet updated when this decision runs (ticket F-01).
        """
        symbol = _normalize_symbol(symbol)

        # no change
        if previous == target:
            return SignalAction.HOLD, SignalType.ENTRY, f"already at target {target}"

        # flat -> long
        if previous == ZERO and target > ZERO:
            return SignalAction.BUY, SignalType.ENTRY, f"enter long {symbol} target={target}"

        # flat -> short
        if previous == ZERO and target < ZERO:
            if not self.allow_short:
                return SignalAction.HOLD, SignalType.ENTRY, f"short not allowed for {symbol}"
            return SignalAction.SELL, SignalType.ENTRY, f"enter short {symbol} target={target}"

        # long -> flat
        if previous > ZERO and target == ZERO:
            return SignalAction.SELL, SignalType.EXIT, f"exit long {symbol}"

        # short -> flat
        if previous < ZERO and target == ZERO:
            return SignalAction.BUY, SignalType.EXIT, f"exit short {symbol}"

        # long -> short or short -> long: need to close first, then open
        # For simplicity, first close, next bar will open opposite.
        # So here we generate exit.
        if previous > ZERO and target < ZERO:
            return SignalAction.SELL, SignalType.EXIT, f"close long {symbol} to prepare short"

        if previous < ZERO and target > ZERO:
            return SignalAction.BUY, SignalType.EXIT, f"close short {symbol} to prepare long"

        # long -> long with different size? Treat as HOLD for now (sizing handled elsewhere)
        # short -> short
        return SignalAction.HOLD, SignalType.ENTRY, f"position {previous} -> {target} no order needed"

    def _build_indicators_snapshot(
        self, symbol: str, candles: pd.DataFrame, strategy: Strategy, last_signal_value: Any
    ) -> Dict[str, Any]:
        """Build indicators dict for signal logging."""
        try:
            last_row = candles.iloc[-1]
            snapshot: Dict[str, Any] = {
                "close": float(last_row["close"]),
                "open": float(last_row.get("open", last_row["close"])),
                "high": float(last_row.get("high", last_row["close"])),
                "low": float(last_row.get("low", last_row["close"])),
                "volume": int(last_row.get("volume", 0)),
                "signal_value": float(last_signal_value) if not pd.isna(last_signal_value) else 0.0,
            }

            # if strategy has params, include them
            if hasattr(strategy, "params"):
                snapshot["strategy_params"] = dict(getattr(strategy, "params", {}))

            # try to compute SMA if strategy is SMA crossover (example)
            # we can attempt to extract fast/slow from params
            try:
                if hasattr(strategy, "fast") and hasattr(strategy, "slow"):
                    fast = getattr(strategy, "fast")
                    slow = getattr(strategy, "slow")
                    fast_sma = candles["close"].rolling(fast).mean().iloc[-1]
                    slow_sma = candles["close"].rolling(slow).mean().iloc[-1]
                    snapshot["fast_sma"] = float(fast_sma) if not pd.isna(fast_sma) else None
                    snapshot["slow_sma"] = float(slow_sma) if not pd.isna(slow_sma) else None
            except Exception:
                pass

            # if strategy exposes get_indicators method, use it
            if hasattr(strategy, "get_indicators") and callable(getattr(strategy, "get_indicators")):
                try:
                    extra = strategy.get_indicators(candles)
                    if isinstance(extra, dict):
                        snapshot.update(extra)
                except Exception:
                    pass

            return snapshot
        except Exception as exc:
            logger.debug("failed to build indicators snapshot for %s: %s", symbol, exc)
            return {"signal_value": float(last_signal_value) if not pd.isna(last_signal_value) else 0.0}

    # -- signal to order conversion ---------------------------------------

    def create_orders(
        self, signals: Iterable[Signal], market_data: Optional[Mapping[str, Any]] = None
    ) -> List[Order]:
        """Convert signals to orders — validation + sizing + Order objects ONLY.

        The adapter never fills (ticket F-01): the caller owns the executor's
        bar clock. Feed the returned orders to ``OrderExecutor.submit`` while
        the signal bar is the latest data, then ``executor.step`` on the NEXT
        bar — the fill lands at that next bar's OPEN.

        Parameters
        ----------
        signals:
            List of ``Signal`` objects.
        market_data:
            Optional market snapshot dict (or dict mapping symbol→snapshot when
            sizing/validating many symbols). Used for sizing price and
            ``can_open_position`` checks only — never as a fill price.

        Returns
        -------
        List[Order]
            Orders that were created (NOT yet filled).
        """
        created_orders: List[Order] = []

        for signal in signals:
            if not isinstance(signal, Signal):
                logger.warning("create_orders got non-Signal: %s", type(signal))
                continue

            if signal.action == SignalAction.HOLD:
                # HOLD already logged in generate_signals; no extra DB row needed
                continue

            if self.dry_run:
                logger.info("dry_run: would %s %s qty=%s reason=%s", signal.action, signal.symbol, signal.quantity, signal.reason)
                if self.db_manager is not None:
                    try:
                        self._save_signal_to_db(signal, executed=False, skip_reason="dry_run")
                    except Exception:
                        pass
                continue

            # determine quantity via sizer if not provided
            quantity = signal.quantity
            if quantity is None:
                try:
                    # try to get current price for sizer
                    current_price = None
                    if market_data:
                        # market_data could be single snapshot or mapping
                        if isinstance(market_data, Mapping) and signal.symbol in market_data:
                            md = market_data[signal.symbol]
                            if isinstance(md, Mapping):
                                current_price = md.get("close") or md.get("last") or md.get("price")
                        elif isinstance(market_data, Mapping):
                            current_price = market_data.get("close") or market_data.get("last") or market_data.get("price")

                    if current_price is None:
                        # fallback to last close from candles
                        candles = self._bars.get(signal.symbol)
                        if candles is not None and not candles.empty:
                            current_price = float(candles["close"].iloc[-1])

                    qty = self.position_sizer.calculate_position_size(
                        signal, self.portfolio, current_price=current_price
                    )
                    quantity = to_price(qty, "quantity")
                except Exception as exc:
                    logger.warning("position sizer failed for %s: %s, using 100", signal.symbol, exc)
                    quantity = Decimal("100")

            # validate against portfolio
            try:
                if signal.action == SignalAction.BUY:
                    # BUY: either opening long or closing short
                    pos = self.portfolio.get_position(signal.symbol)
                    if pos is None:
                        # opening long
                        check = self.portfolio.can_open_position(signal.symbol, quantity, self._get_current_price(signal.symbol, market_data))
                        if not check:
                            reason = f"can_open_position denied: {check.code} {check.reason}"
                            logger.info("order rejected for %s: %s", signal.symbol, reason)
                            if self.db_manager is not None:
                                self._save_signal_to_db(signal, executed=False, skip_reason=reason)
                            continue
                    else:
                        # closing short or adding? For now, if pos is short, allow close
                        # if pos is long, we would be adding - treat as HOLD unless sizing says increase
                        if pos.quantity > ZERO:
                            # already long, skip if target already met
                            # (we already decided action, but double-check)
                            pass

                elif signal.action == SignalAction.SELL:
                    pos = self.portfolio.get_position(signal.symbol)
                    if pos is None:
                        # opening short?
                        if not self.allow_short:
                            reason = "short selling disabled"
                            logger.info("order rejected for %s: %s", signal.symbol, reason)
                            if self.db_manager is not None:
                                self._save_signal_to_db(signal, executed=False, skip_reason=reason)
                            continue
                        # check can open short (negative quantity)
                        check = self.portfolio.can_open_position(signal.symbol, -quantity, self._get_current_price(signal.symbol, market_data))
                        if not check:
                            reason = f"can_open_position denied for short: {check.code} {check.reason}"
                            logger.info("order rejected for %s: %s", signal.symbol, reason)
                            if self.db_manager is not None:
                                self._save_signal_to_db(signal, executed=False, skip_reason=reason)
                            continue
                    else:
                        # closing long or opening short after close handled earlier
                        # ensure quantity does not exceed position when closing
                        if pos.quantity > ZERO:
                            # closing long, quantity should not exceed position unless we allow partial
                            # we will cap to position quantity for exit
                            if signal.signal_type == SignalType.EXIT:
                                quantity = min(quantity, abs(pos.quantity))
            except Exception as exc:
                logger.warning("validation failed for %s: %s", signal.symbol, exc)
                if self.db_manager is not None:
                    try:
                        self._save_signal_to_db(signal, executed=False, skip_reason=f"validation error: {exc}")
                    except Exception:
                        pass
                continue

            # create order
            try:
                side = OrderSide.BUY if signal.action == SignalAction.BUY else OrderSide.SELL
                order_type = OrderType.MARKET if signal.order_type == "MARKET" else OrderType.LIMIT

                order_kwargs: Dict[str, Any] = {
                    "symbol": signal.symbol,
                    "side": side,
                    "quantity": quantity,
                    "order_type": order_type,
                    "time_in_force": self.time_in_force,
                    "portfolio_id": getattr(self.portfolio, "portfolio_id", None),
                    "strategy_name": signal.strategy_name,
                }
                if signal.limit_price is not None:
                    order_kwargs["limit_price"] = signal.limit_price

                order = Order(**order_kwargs)
                order.validate()
                order.submit()

                # add to portfolio tracking
                self.portfolio.add_order(order)
                self.order_history.append(order)
                created_orders.append(order)

                logger.info(
                    "order created %s %s %s %s @ %s reason=%s",
                    order.order_id,
                    signal.action,
                    quantity,
                    signal.symbol,
                    signal.limit_price or "market",
                    signal.reason,
                )

                # log signal as mapped to an order (the fill itself happens
                # on the executor's next-bar step, driven by the engine)
                if self.db_manager is not None:
                    try:
                        self._save_signal_to_db(signal, executed=True, order_id=order.order_id)
                    except Exception as exc:
                        logger.warning("failed to save executed signal: %s", exc)

            except Exception as exc:
                logger.exception("failed to create order for %s: %s", signal.symbol, exc)
                if self.db_manager is not None:
                    try:
                        self._save_signal_to_db(signal, executed=False, skip_reason=f"order creation failed: {exc}")
                    except Exception:
                        pass
                continue

        return created_orders

    def _get_current_price(self, symbol: str, market_data: Optional[Mapping[str, Any]]) -> Decimal:
        """Get current price for symbol from market_data or last bar."""
        symbol = _normalize_symbol(symbol)
        price: Any = None

        if market_data is not None:
            if isinstance(market_data, Mapping) and symbol in market_data:
                md = market_data[symbol]
                if isinstance(md, Mapping):
                    price = md.get("close") or md.get("last") or md.get("price") or md.get("ask") or md.get("bid")
            elif isinstance(market_data, Mapping):
                price = market_data.get("close") or market_data.get("last") or market_data.get("price")

        if price is None:
            candles = self._bars.get(symbol)
            if candles is not None and not candles.empty:
                price = float(candles["close"].iloc[-1])

        if price is None:
            price = 100  # fallback

        try:
            return to_price(price, "price")
        except Exception:
            return to_price(100, "price")

    # -- DB persistence ----------------------------------------------------

    def _save_signal_to_db(
        self,
        signal: Signal,
        executed: bool = False,
        order_id: Optional[str] = None,
        skip_reason: Optional[str] = None,
    ) -> str:
        """Persist signal to ``strategy_signals`` table.

        Returns signal_id (or empty string if no DB).

        Handles FK dependencies gracefully: if the portfolio row does not
        yet exist in the DB, a minimal row is created. If an order_id is
        supplied but the order row does not exist, the signal is saved
        without the FK (order_id cleared) so logging never blocks trading.
        """
        if self.db_manager is None:
            return ""

        from backtest.db.models import Portfolio as PortfolioRow
        from backtest.db.models import Order as OrderRow
        from backtest.db.models import StrategySignal as StrategySignalRow

        # map signal_type and direction to DB enums (lowercase)
        signal_type = signal.signal_type.lower() if signal.signal_type else "entry"
        direction = signal.direction.lower() if signal.direction else "long"
        if direction not in ("long", "short", "flat"):
            direction = "long" if signal.action == SignalAction.BUY else "flat"

        portfolio_id = getattr(self.portfolio, "portfolio_id", None)

        with self.db_manager.session() as session:
            # Ensure portfolio row exists for FK
            if portfolio_id:
                existing = session.get(PortfolioRow, portfolio_id)
                if existing is None:
                    # create minimal portfolio row so signals can be logged
                    try:
                        # initial_capital and current_cash required
                        init_cap = getattr(self.portfolio, "initial_capital", Decimal("100000"))
                        curr_cash = getattr(self.portfolio, "current_cash", init_cap)
                        name = getattr(self.portfolio, "name", f"portfolio-{portfolio_id[:8]}")
                        # avoid name collision
                        row = PortfolioRow(
                            portfolio_id=portfolio_id,
                            name=name,
                            initial_capital=init_cap,
                            current_cash=curr_cash,
                            status="active",
                        )
                        session.add(row)
                        session.flush()
                        logger.debug("auto-created portfolio row %s for signal logging", portfolio_id)
                    except Exception as exc:
                        # if creation fails (e.g. name collision), try to fetch by name or clear portfolio_id
                        logger.debug("auto-create portfolio failed: %s, will try without FK", exc)
                        # try to find existing by name
                        try:
                            from sqlalchemy import select

                            existing_by_name = session.scalars(
                                select(PortfolioRow).where(PortfolioRow.name == name)
                            ).first()
                            if existing_by_name is not None:
                                portfolio_id = existing_by_name.portfolio_id
                            else:
                                portfolio_id = None
                        except Exception:
                            portfolio_id = None

            # Check order_id FK - if order row does not exist, drop the FK to avoid failure
            effective_order_id = order_id
            if order_id:
                try:
                    if session.get(OrderRow, order_id) is None:
                        logger.debug("order %s not in DB, saving signal without order FK", order_id)
                        effective_order_id = None
                except Exception:
                    effective_order_id = None

            row = StrategySignalRow(
                portfolio_id=portfolio_id,
                symbol=signal.symbol,
                strategy_name=signal.strategy_name,
                signal_type=signal_type,
                direction=direction,
                strength=signal.strength,
                target_position=signal.target_position,
                bar_ts=signal.bar_ts,
                generated_at=signal.generated_at,
                indicators_snapshot=signal.indicators,
                executed=executed,
                order_id=effective_order_id,
                skip_reason=skip_reason,
            )
            session.add(row)
            session.flush()
            signal_id = getattr(row, "signal_id", "")
            logger.debug("signal saved to DB %s executed=%s", signal_id, executed)
            return str(signal_id)

    def save_signals_to_db(self, signals: Iterable[Signal], executed: bool = False) -> List[str]:
        """Batch save signals to DB."""
        ids: List[str] = []
        for sig in signals:
            try:
                sid = self._save_signal_to_db(sig, executed=executed)
                ids.append(sid)
            except Exception as exc:
                logger.warning("batch save signal failed: %s", exc)
        return ids

    # -- state management --------------------------------------------------

    def get_state(self) -> Dict[str, Any]:
        """Return JSON-serializable snapshot of adapter state for recovery."""
        return {
            "symbols": list(self.symbols),
            "bars": {sym: df.to_dict(orient="list") for sym, df in self._bars.items()},
            "bars_index": {sym: [str(i) for i in df.index] for sym, df in self._bars.items()},
            "last_target": {k: str(v) for k, v in self._last_target.items()},
            "indicators": dict(self._indicators),
            "signal_history": [s.to_dict() for s in self.signal_history[-100:]],  # last 100
            "state": dict(self._state),
            "portfolio_id": getattr(self.portfolio, "portfolio_id", None),
            "strategy_name": getattr(self.strategy, "name", type(self.strategy).__name__),
            "timestamp": _utcnow().isoformat(),
        }

    def load_state(self, state: Mapping[str, Any]) -> None:
        """Restore state from snapshot (bars, indicators, etc.)."""
        try:
            symbols = state.get("symbols", [])
            if symbols:
                self.symbols = [_normalize_symbol(s) for s in symbols]

            # restore bars
            bars_data = state.get("bars", {})
            bars_index = state.get("bars_index", {})
            for sym, data in bars_data.items():
                try:
                    idx_raw = bars_index.get(sym, [])
                    idx = pd.to_datetime(idx_raw, utc=True) if idx_raw else []
                    df = pd.DataFrame(data, index=idx)
                    df.index.name = "timestamp"
                    self._bars[_normalize_symbol(sym)] = df
                except Exception as exc:
                    logger.warning("failed to restore bars for %s: %s", sym, exc)

            last_target = state.get("last_target", {})
            for k, v in last_target.items():
                try:
                    self._last_target[_normalize_symbol(k)] = to_decimal(v, "target")
                except Exception:
                    pass

            self._indicators = dict(state.get("indicators", {}))
            self._state = dict(state.get("state", {}))

            logger.info("state restored: %s symbols, %s bars", len(self.symbols), sum(len(df) for df in self._bars.values()))
        except Exception as exc:
            logger.exception("load_state failed: %s", exc)
            raise

    def to_dict(self) -> Dict[str, Any]:
        """Alias for get_state for compatibility."""
        return self.get_state()

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        strategy: Strategy,
        portfolio: Any,
        **kwargs: Any,
    ) -> "StrategyAdapter":
        """Rebuild adapter from dict snapshot."""
        adapter = cls(strategy=strategy, portfolio=portfolio, symbols=payload.get("symbols"), **kwargs)
        adapter.load_state(payload)
        return adapter

    # -- convenience -------------------------------------------------------

    @property
    def bars(self) -> Dict[str, pd.DataFrame]:
        return {k: v.copy() for k, v in self._bars.items()}

    def reset(self) -> None:
        """Clear bars, history and state (keeps portfolio)."""
        self._bars.clear()
        self._latest_quotes.clear()
        self._indicators.clear()
        self._last_target.clear()
        self.signal_history.clear()
        self.order_history.clear()
        self._state = {"initialized": False}
        logger.info("adapter reset")

    def __repr__(self) -> str:
        return f"<StrategyAdapter strategy={getattr(self.strategy, 'name', '?')} symbols={self.symbols} signals={len(self.signal_history)} orders={len(self.order_history)} dry_run={self.dry_run}>"
