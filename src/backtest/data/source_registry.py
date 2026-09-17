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

``mode='backtest'`` sources are wrapped in :class:`~backtest.data.
corporate_actions.AdjustedSource` when the corporate-action policy
(``config/data_quality.yaml → daily_bar.corporate_actions``) is enabled —
raw DB bars are back-adjusted at read time; disabled ⇒ unchanged.
"""

from __future__ import annotations

from typing import Any

from backtest.data.base import DataSource
from backtest.data.corporate_actions import AdjustedSource, calendar_from_config
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
            source = DbSource(**kwargs)  # fixed: historical DB
            # Corporate-action policy (review §3.3): raw DB bars are
            # back-adjusted at READ time when the policy is enabled and has
            # actions. Disabled/empty ⇒ the plain DbSource, byte-identical.
            calendar = calendar_from_config()
            if calendar:
                return AdjustedSource(source, calendar)
            return source

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
