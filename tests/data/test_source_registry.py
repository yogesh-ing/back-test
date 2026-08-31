"""SourceRegistry — the (mode, source) factory (ticket P1.2).

Acceptance:

* every ``(mode, choice)`` combination returns the correct source class;
* invalid combinations raise :class:`ConfigError` with a clear message;
* ``config/forward_testing.yaml`` loads with the new ``mode``/``source``/
  ``replay_speed`` keys and the registry honours the config values.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backtest.data.base import DataSource
from backtest.data.db_source import DbSource
from backtest.data.mstock_live_feed import MStockLiveFeed
from backtest.data.source_registry import SourceRegistry
from backtest.data.synthetic import SyntheticSource
from backtest.db.config import ConfigError
from backtest.forward.engine import load_forward_config

REPO_ROOT = Path(__file__).resolve().parents[2]
FORWARD_YAML = REPO_ROOT / "config" / "forward_testing.yaml"


@pytest.fixture()
def reg() -> SourceRegistry:
    return SourceRegistry()


# ---------------------------------------------------------------------------
# The ticket's six tests
# ---------------------------------------------------------------------------


def test_backtest_returns_dbsource(reg):
    assert isinstance(reg.get_source("backtest"), DbSource)


def test_live_returns_mstock(reg):
    assert isinstance(reg.get_source("live"), MStockLiveFeed)


def test_paper_mstock(reg):
    assert isinstance(reg.get_source("paper", "mstock"), MStockLiveFeed)


def test_paper_synthetic(reg):
    assert isinstance(reg.get_source("paper", "synthetic"), SyntheticSource)


def test_paper_no_choice_raises(reg):
    with pytest.raises(ConfigError):
        reg.get_source("paper", None)


def test_bad_mode_raises(reg):
    with pytest.raises(ConfigError):
        reg.get_source("banana")


# ---------------------------------------------------------------------------
# Acceptance: invalid combos raise ConfigError with a clear message
# ---------------------------------------------------------------------------


def test_paper_unknown_source_raises_with_message(reg):
    with pytest.raises(ConfigError, match="banana"):
        reg.get_source("paper", "banana")


def test_bad_mode_message_names_the_value(reg):
    with pytest.raises(ConfigError, match="banana"):
        reg.get_source("banana")


def test_paper_none_choice_message_names_it(reg):
    with pytest.raises(ConfigError, match="None"):
        reg.get_source("paper", None)


@pytest.mark.parametrize("mode", ["PAPER", " paper ", "Live"])
def test_mode_is_case_and_whitespace_insensitive(reg, mode):
    if "paper" in mode.lower():
        assert isinstance(reg.get_source(mode, "synthetic"), SyntheticSource)
    else:
        assert isinstance(reg.get_source(mode), MStockLiveFeed)


# ---------------------------------------------------------------------------
# Contract details
# ---------------------------------------------------------------------------


def test_all_sources_satisfy_datasource_protocol(reg):
    for src in (
        reg.get_source("backtest"),
        reg.get_source("live"),
        reg.get_source("paper", "mstock"),
        reg.get_source("paper", "synthetic"),
    ):
        assert isinstance(src, DataSource)


def test_replay_speed_passthrough(reg):
    src = reg.get_source("paper", "synthetic", replay_speed=5)
    assert src.replay_speed == 5.0


def test_replay_speed_defaults_to_one(reg):
    assert reg.get_source("paper", "synthetic").replay_speed == 1.0


def test_db_source_receives_db_url(reg):
    src = reg.get_source("backtest", db_url="sqlite:///:memory:")
    assert isinstance(src, DbSource)
    assert src.db_url == "sqlite:///:memory:"
