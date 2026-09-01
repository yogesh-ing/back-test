"""Canonical source-tag mapping — single source of truth (ticket F-14).

All three source classes live in :mod:`backtest.data`; the run-classification
tags (ticket P1.1) that describe them live here too. Do NOT duplicate this map
anywhere — import it::

    from backtest.data.source_tags import SOURCE_TAGS, source_tag_for

Layering: this module imports only sibling modules under ``backtest.data``
(plus nothing from ``backtest.engine`` / ``backtest.forward``), so both
callers — :mod:`backtest.engine.backtest_driver` and
:mod:`backtest.forward.paper_runner` — can import it without a cycle.
"""

from __future__ import annotations

from typing import Any

from backtest.data.db_source import DbSource
from backtest.data.mstock_live_feed import MStockLiveFeed
from backtest.data.synthetic import SyntheticSource

__all__ = [
    "SOURCE_TAGS",
    "SOURCE_TAG_VALUES",
    "source_tag_for",
    "DEFAULT_SOURCE_TAG",
    "APP_SOURCE_TAGS",
    "app_source_tag",
]

#: Source class -> run-classification tag (P1.1 buckets: synthetic / replay
#: historical DB / mstock live feed). Matches the historical shape exactly.
SOURCE_TAGS: dict[type, str] = {
    SyntheticSource: "synthetic",
    DbSource: "replay",
    MStockLiveFeed: "mstock",
}

#: The tag VALUES of :data:`SOURCE_TAGS` — the canonical set for validating a
#: run's ``source`` classification (state files, portfolio rows, configs).
#: Derive from the map; never re-declare the strings.
SOURCE_TAG_VALUES: frozenset[str] = frozenset(SOURCE_TAGS.values())

#: Fallback used when a source class is not (yet) in the canonical map.
DEFAULT_SOURCE_TAG = "synthetic"

#: ``runner.build_source`` name -> canonical run-classification tag
#: (ticket #10). The web app's ``BACKTEST_SOURCE`` vocabulary
#: (``synthetic | csv | mstock | db``) is one level BELOW the taxonomy: CSV
#: bars are user-supplied files — they have no taxonomy tag, so they classify
#: as ``synthetic`` (data trust = generated/unknown); ``db`` historical bars
#: classify as ``replay``; ``mstock`` as ``mstock``. Single authority — UI and
#: API import this instead of re-declaring the mapping.
APP_SOURCE_TAGS: dict[str, str] = {
    "synthetic": "synthetic",
    "csv": "synthetic",
    "mstock": "mstock",
    "db": "replay",
}


def source_tag_for(source: Any, default: str = DEFAULT_SOURCE_TAG) -> str:
    """Return the canonical tag for a ``DataSource`` instance (or class).

    ``source`` may be an instance or a class object; unknown sources fall
    back to the tag above so a new source class never crashes a run.
    """
    return SOURCE_TAGS.get(type(source), default)


def app_source_tag(name: Any, default: str = DEFAULT_SOURCE_TAG) -> str:
    """Map an app-level ``build_source`` name to its canonical taxonomy tag.

    ``default`` is used for unknown names (a new app source should be added
    to :data:`APP_SOURCE_TAGS`, never silently re-tagged at the call site).
    """
    key = str(name or "").strip().lower()
    return APP_SOURCE_TAGS.get(key, default)
