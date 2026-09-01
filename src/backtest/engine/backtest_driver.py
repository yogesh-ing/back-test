"""BacktestDriver — backtest on the simulator step loop (ticket P2.1).

The SAME engine loop as :class:`~backtest.forward.paper_runner.PaperRunner`
(:func:`backtest.simulator.engine_loop.run_engine_loop`) — only the
source, the run classification (``source`` tag; ``mode='paper'`` because
migration 002 defines 'paper' as *simulated fills*) and the entry sizing
differ. This is what makes "backtest ≈ forward" a structural guarantee
rather than an approximation: one loop, two entry points. The vectorized
:class:`~backtest.engine.backtester.Backtester` stays as-is (the quick
screen path); the driver is the fill-exact path.

Layering note (ticket F-14): the ``source`` tag map lives in
``backtest.data.source_tags`` (canonical, shared with ``PaperRunner``) —
this module must not import from ``backtest.forward``.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from backtest.data.source_tags import source_tag_for
from backtest.simulator.engine_loop import OrderQueue, run_engine_loop

logger = logging.getLogger("backtest.engine.backtest_driver")

__all__ = ["BacktestDriver"]


class BacktestDriver:
    """Backtest driver over historical bars — same loop as PaperRunner.

    Parameters mirror :class:`PaperRunner`; the portfolio is tagged
    ``mode='paper'`` (simulated fills — the only mode the P1.1 schema
    allows besides 'live') and the ``source`` tag is derived from the
    source class (``replay`` for :class:`DbSource`) unless
    ``source_tag`` is given.
    """

    def __init__(
        self,
        source: Any,
        strategy: Any,
        portfolio: Any,
        executor: Any,
        order_queue: OrderQueue | None = None,
        symbols: list[str] | None = None,
        start: str | None = None,
        end: str | None = None,
        interval: str = "day",
        quantity: int = 100,
        size_fn: Optional[Callable[[str, float, Any], int]] = None,
        db: Any = None,
        source_tag: str | None = None,
    ) -> None:
        self.source = source
        self.strategy = strategy
        self.portfolio = portfolio
        self.executor = executor
        self.order_queue = order_queue or OrderQueue()
        self.symbols = [str(s).strip().upper() for s in (symbols or [])]
        self.start = start
        self.end = end
        self.interval = interval
        self.quantity = int(quantity)
        self.size_fn = size_fn
        self.db = db

        # Run classification: simulated fills ⇒ mode 'paper' (P1.1 schema:
        # paper = simulated fills, live = real broker orders); the bars'
        # origin (replay vs synthetic vs mstock) comes from the source class.
        self.portfolio.mode = "paper"
        self.portfolio.source = source_tag or source_tag_for(source)

    def run(self) -> dict[str, Any]:
        """Run the shared engine loop and return the portfolio summary."""
        return run_engine_loop(
            source=self.source,
            strategy=self.strategy,
            portfolio=self.portfolio,
            executor=self.executor,
            order_queue=self.order_queue,
            symbols=self.symbols,
            start=self.start,
            end=self.end,
            interval=self.interval,
            quantity=self.quantity,
            size_fn=self.size_fn,
            db=self.db,
            coid_prefix="backtest",
            log_label="backtest run",
        )

    def performance_summary(self) -> dict[str, Any]:
        """The portfolio summary (equity, positions, P&L) after a run."""
        return self.portfolio.summary()

    def __repr__(self) -> str:
        return (f"<BacktestDriver {getattr(self.strategy, 'name', '?')} "
                f"on {self.symbols or '…'}>")
