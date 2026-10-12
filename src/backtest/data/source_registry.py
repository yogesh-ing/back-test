"""(mode, source) -> DataSource factory (ticket P1.2).

The single place that decides *where a run's bars come from*:

* ``mode='backtest'``            -> :class:`~backtest.data.db_source.DbSource`
  (historical DB, fixed)
* ``mode='live'``                -> :class:`~backtest.data.mstock_live_feed.
  MStockLiveFeed` (real broker feed, fixed)
* ``mode='paper', source='mstock'``     -> live broker data, paper risk
* ``mode='paper', source='synthetic'``  -> generated bars, replayed at
  ``replay_speed`` bars/second — **only when the data-source policy allows
  synthetic**. With synthetic disabled in ``config/data_sources.yaml`` this
  raises ``ConfigError`` carrying the policy's refusal; there is no bypass
  here, and the test suite opts back in through the policy's own ``testing``
  profile (``tests/conftest.py``).

Unknown modes and paper runs without a valid source choice raise
:class:`~backtest.db.config.ConfigError` with a message naming the bad value.
Sources disabled by policy raise the same ``ConfigError``, so callers have one
failure type to handle.

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
from backtest.data.sources_policy import source_policy
from backtest.data.synthetic import SyntheticSource
from backtest.db.config import ConfigError

__all__ = ["SourceRegistry", "source_registry"]


def _require(name: str, where: str) -> None:
    """Policy check that speaks this module's error type.

    :func:`backtest.data.sources_policy.require_enabled` raises a
    ``ValueError`` (the API boundary turns those into 409s). Callers here
    already handle ``ConfigError``, so the same refusal sentence is re-raised
    in the local currency rather than adding a second exception type to the
    contract of ``get_source``.
    """
    refusal = source_policy().refusal_for(name)
    if refusal is not None:
        raise ConfigError(f"{refusal} [{where}]")


class SourceRegistry:
    """Factory mapping ``(mode, source choice)`` to a DataSource instance."""

    def get_source(self, mode: str, choice: str | None = None, **kwargs: Any) -> DataSource:
        """Build the feed for a run.

        Only the **synthetic** branch is policy-gated here. ``db`` and
        ``mstock`` are real sources whose *availability* is a different
        question from their *legitimacy* — a ``DbSource`` for an unreachable
        database raises when it is read, and the run endpoints already refuse a
        disabled requested source at the request boundary
        (:mod:`backtest.api.data_guard`). What this factory must never do is
        hand out generated candles to a deployment that switched them off.
        """
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
                # Config-gated, with no bypass: when ``data_sources.yaml`` says
                # synthetic is off, a paper run on generated bars is refused
                # here rather than silently served. Tests flip it on through
                # the policy's own ``testing`` profile (tests/conftest.py),
                # not by weakening this check.
                _require("synthetic", "paper mode")
                return SyntheticSource(replay_speed=kwargs.get("replay_speed", 1))
            raise ConfigError(f"paper mode needs source: 'mstock' or 'synthetic', got {choice!r}")

        raise ConfigError(f"unknown mode: {mode!r} (expected 'backtest', 'paper', or 'live')")


#: Shared default instance.
source_registry = SourceRegistry()
