"""(mode, source) -> DataSource factory (ticket P1.2).

The single place that decides *where a run's bars come from*:

* ``mode='backtest'``            -> :class:`~backtest.data.db_source.DbSource`
  (historical DB, fixed)
* ``mode='live'``                -> :class:`~backtest.data.mstock_live_feed.
  MStockLiveFeed` (real broker feed, fixed)
* ``mode='paper', source='mstock'``     -> live broker data, paper risk
* ``mode='paper', source='synthetic'``  -> generated bars, replayed at
  ``replay_speed`` bars/second

Unknown modes and paper runs without a valid source choice raise
:class:`~backtest.db.config.ConfigError` with a message naming the bad value.
"""

from __future__ import annotations

from typing import Any

from backtest.data.base import DataSource
from backtest.data.db_source import DbSource
from backtest.data.mstock_live_feed import MStockLiveFeed
from backtest.data.synthetic import SyntheticSource
from backtest.db.config import ConfigError

__all__ = ["SourceRegistry", "source_registry"]


class SourceRegistry:
    """Factory mapping ``(mode, source choice)`` to a DataSource instance."""

    def get_source(self, mode: str, choice: str | None = None, **kwargs: Any) -> DataSource:
        mode = str(mode or "").strip().lower()

        if mode == "backtest":
            return DbSource(**kwargs)  # fixed: historical DB

        if mode == "live":
            return MStockLiveFeed(**kwargs)  # fixed: real broker feed

        if mode == "paper":
            if choice is None:
                raise ConfigError("paper mode needs source: 'mstock' or 'synthetic', got None")
            choice = str(choice).strip().lower()
            if choice == "mstock":
                return MStockLiveFeed(**kwargs)  # live data, paper risk
            if choice == "synthetic":
                return SyntheticSource(replay_speed=kwargs.get("replay_speed", 1))
            raise ConfigError(f"paper mode needs source: 'mstock' or 'synthetic', got {choice!r}")

        raise ConfigError(f"unknown mode: {mode!r} (expected 'backtest', 'paper', or 'live')")


#: Shared default instance.
source_registry = SourceRegistry()
