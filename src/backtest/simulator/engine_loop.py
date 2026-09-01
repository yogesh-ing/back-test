"""The canonical bar-clock engine loop (ticket P2.1).

One loop drives every bar-replay run — paper runs
(:class:`~backtest.forward.paper_runner.PaperRunner`) and backtests
(:class:`~backtest.engine.backtest_driver.BacktestDriver`). Both are the
SAME code: only the source, the run classification (``mode`` /
``source`` tags, set by the caller before the loop) and the entry sizing
differ. That is what makes "backtest ≈ forward" a structural guarantee
rather than an approximation.

Fill discipline (ticket P1.3): a signal computed on bar ``t`` becomes an
order while bar ``t`` is the latest known data and trades at bar
``t+1``'s **open** — never bar ``t``'s close. The executor's arm rule
enforces this: an order first seen in a ``step()`` only arms; it trades
at the next bar's open.

Layering: this module lives in ``backtest.simulator`` and must not import
from ``backtest.engine`` or ``backtest.forward`` (see the package
docstring). The source, strategy, portfolio and executor are all
duck-typed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Iterator, Optional

import pandas as pd

from backtest.simulator.enums import OrderSide, OrderType, TimeInForce
from backtest.simulator.errors import ValidationError
from backtest.simulator.order import Order

if TYPE_CHECKING:  # pragma: no cover — typing only (layering rule)
    from backtest.simulator.execution import OrderExecutor
    from backtest.simulator.portfolio import Portfolio

__all__ = ["Bar", "OrderQueue", "run_engine_loop", "to_python_scalar"]

logger = logging.getLogger("backtest.simulator.engine_loop")


def to_python_scalar(value: Any) -> Any:
    """Convert a numpy scalar to a plain Python value.

    The simulator's ``Decimal`` conversion rejects numpy types (their
    ``repr`` is ``np.float64(...)`` in numpy 2), so bars leave the
    DataFrame in native Python numerics.
    """
    if value is None:
        return None
    item = getattr(value, "item", None)
    if callable(item):
        return item()
    return value


@dataclass
class Bar:
    """The minimal bar view the executor needs (``open`` is the fill anchor)."""

    open: Any
    close: Any
    volume: Any | None = None
    timestamp: Any | None = None


class OrderQueue:
    """Idempotent order intake for a bar-replay run.

    The same ``client_order_id`` is accepted exactly once; re-submitting it
    (a retried bar, a duplicated signal) is ignored rather than
    double-executed. Falls back to the exchange-unique ``order_id`` when no
    client id is set.
    """

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._orders: list[Order] = []

    def submit(self, order: Order) -> bool:
        """Queue ``order``; returns ``False`` when it was already queued."""
        key = order.client_order_id or order.order_id
        if key in self._seen:
            return False
        self._seen.add(key)
        self._orders.append(order)
        return True

    @property
    def orders(self) -> tuple[Order, ...]:
        return tuple(self._orders)

    def __iter__(self) -> Iterator[Order]:
        return iter(self._orders)

    def __len__(self) -> int:
        return len(self._orders)


def run_engine_loop(
    *,
    source: Any,
    strategy: Any,
    portfolio: "Portfolio",
    executor: "OrderExecutor",
    order_queue: OrderQueue,
    symbols: list[str],
    start: str | None = None,
    end: str | None = None,
    interval: str = "day",
    quantity: int = 100,
    size_fn: Optional[Callable[[str, float, "Portfolio"], int]] = None,
    db: Any = None,
    coid_prefix: str = "paper",
    log_label: str = "engine loop",
) -> dict[str, Any]:
    """Replay the source bar-by-bar and return the portfolio summary.

    Parameters mirror the run's inputs: ``source`` supplies
    ``get_candles(symbol, start, end, interval)`` per symbol; ``strategy``
    supplies the vectorised ``generate_signals(candles)``; ``quantity`` /
    ``size_fn`` size entries (exits always close the actual held
    quantity); ``db`` (optional DatabaseManager) persists the portfolio
    graph when the run finishes.

    Per bar tick:

    1. signal transitions on this bar → orders (queued, NOT traded yet);
    2. ``executor.step`` fills what was queued on an EARLIER bar, at this
       bar's open;
    3. mark to market and record the equity snapshot at this bar's close.
    """
    if not symbols:
        raise ValidationError(f"{log_label} needs at least one symbol", code="no_symbols")
    if executor.portfolio is None:
        executor.portfolio = portfolio

    # -- order emission (never raises for a bad trade) ----------------------

    def submit_order(symbol: str, side: str, qty: Any, ts: Any) -> None:
        try:
            order = Order(
                symbol=symbol,
                side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                quantity=qty,
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.DAY,
                portfolio_id=portfolio.portfolio_id,
                strategy_name=getattr(strategy, "name", None),
                client_order_id=f"{coid_prefix}-{ts.isoformat()}-{symbol}-{side}",
            )
            order.validate()
            order.submit()
        except ValidationError as exc:
            logger.warning("skipping invalid %s %s order: %s", side, symbol, exc)
            return
        order_queue.submit(order)
        portfolio.add_order(order)
        executor.submit(order)

    def submit_buy(symbol: str, bar: Bar, ts: Any) -> None:
        """Size and submit an entry order for this bar's signal."""
        qty = size_fn(symbol, float(bar.open), portfolio) if size_fn is not None else quantity
        submit_order(symbol, "buy", qty, ts)

    # -- bars & signals ------------------------------------------------------

    frames: dict[str, pd.DataFrame] = {}
    signals: dict[str, pd.Series] = {}
    for symbol in symbols:
        candles = source.get_candles(symbol, start, end, interval)
        if candles is None or candles.empty:
            raise ValidationError(
                f"source returned no bars for {symbol}",
                code="no_bars",
                symbol=symbol,
            )
        frames[symbol] = candles
        signals[symbol] = strategy.generate_signals(candles)

    timestamps = sorted(set().union(*(set(f.index) for f in frames.values())))
    prev_signal: dict[str, int] = {}

    for ts in timestamps:
        bars_this_tick: dict[str, Bar] = {}
        closes: dict[str, Any] = {}
        for symbol in symbols:
            frame = frames[symbol]
            if ts not in frame.index:
                continue
            row = frame.loc[ts]
            volume = row.get("volume")
            if volume is None or pd.isna(volume):
                volume = None
            else:
                volume = to_python_scalar(volume)
            bars_this_tick[symbol] = Bar(
                open=to_python_scalar(row["open"]),
                close=to_python_scalar(row["close"]),
                volume=volume,
                timestamp=ts,
            )
            closes[symbol] = to_python_scalar(row["close"])

        # 1) signal on this bar → order, queued for the NEXT bar's open.
        for symbol, bar in bars_this_tick.items():
            sig = int(signals[symbol].get(ts, 0))
            prev = prev_signal.get(symbol, 0)
            prev_signal[symbol] = sig
            if sig in (0, 1) and sig == prev:
                continue
            position = portfolio.get_position(symbol)
            pending_buys = [
                o
                for o in portfolio.pending_orders
                if o.symbol == symbol and o.side is OrderSide.BUY
            ]
            pending_sells = [
                o
                for o in portfolio.pending_orders
                if o.symbol == symbol and o.side is OrderSide.SELL
            ]
            if sig == 1 and prev == 0:
                # Enter from a flat book, or RE-ENTER while a close of
                # this symbol is still in flight: that close executes at
                # THIS bar's open (the decision runs before step) and the
                # new buy arms one bar behind it, so it fills only after
                # the book is flat. Without the re-entry, a flat→long
                # signal on the bar after a 1-bar close is missed
                # entirely — a fill-timing leak vs lagged backtests.
                if position is None:
                    if not pending_buys:
                        submit_buy(symbol, bar, ts)
                elif pending_sells and not pending_buys:
                    submit_buy(symbol, bar, ts)
            elif sig == 0 and prev == 1:
                if position is not None and not pending_sells:
                    # Close exactly what is held (partial-fill safe).
                    submit_order(symbol, "sell", abs(position.quantity), ts)
                elif position is None and pending_buys:
                    # 1-bar spike: the entry fills at this bar's open
                    # (after the decision), so queue the matching close
                    # right behind it — the pair brackets one bar, the
                    # same exposure the lagged backtest model has.
                    submit_order(
                        symbol,
                        "sell",
                        sum(float(o.quantity) for o in pending_buys),
                        ts,
                    )

        # 2) fills at THIS bar's open, for orders armed earlier.
        try:
            executor.step(bars_this_tick)
        except ValidationError as exc:
            # Backstop for an order no longer fundable at the fill price
            # (extreme open gap): skip it, keep the run alive.
            logger.warning("skipping unexecutable order at %s: %s", ts, exc)

        # 3) mark to market + equity snapshot at this bar's close.
        if closes:
            portfolio.update_prices(closes)
            portfolio.record_equity(ts)

        portfolio.sync_orders()

    if db is not None:
        portfolio.save_to_db(db)

    logger.info(
        "%s finished: %d bars, %d orders, %d closed positions, equity=%s",
        log_label,
        len(timestamps),
        len(order_queue),
        len(portfolio.closed_positions),
        portfolio.calculate_total_equity(),
    )
    return portfolio.summary()
