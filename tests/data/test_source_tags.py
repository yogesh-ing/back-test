"""Source taxonomy — the canonical SOURCE_TAGS map (single source of truth).

2026-10-01: DhanLiveFeed joined as a first-class tag, so ``dhan`` enters
SOURCE_TAG_VALUES — which is exactly what the spawn modal's Data source
dropdown renders and RunnerConfig validates against. A dhan-fed runner was
previously impossible to spawn (source refused) even though DhanBarFeed
stamped its bars ``_source="dhan"`` and the fan-out matched on it.
"""

from __future__ import annotations

from backtest.data.dhan_live_feed import DhanLiveFeed
from backtest.data.mstock_live_feed import MStockLiveFeed
from backtest.data.source_tags import (
    APP_SOURCE_TAGS,
    SOURCE_TAG_VALUES,
    app_source_tag,
    source_tag_for,
)


def test_dhan_is_a_first_class_source_tag():
    assert "dhan" in SOURCE_TAG_VALUES
    assert source_tag_for(DhanLiveFeed()) == "dhan"


def test_each_broker_feed_tags_as_itself():
    assert source_tag_for(MStockLiveFeed()) == "mstock"
    assert source_tag_for(DhanLiveFeed()) == "dhan"


def test_app_level_dhan_maps_to_dhan():
    """The app-level name keeps a truthful venue label — dhan runs are never
    relabelled as mstock (or as synthetic, the old unknown-class default)."""
    assert app_source_tag("dhan") == "dhan"
    assert APP_SOURCE_TAGS["dhan"] == "dhan"
