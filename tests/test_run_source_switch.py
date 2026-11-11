"""The ``OPTION_DEPTH_SOURCE`` spawn switch (§7 Q3, impl plan Phase 3 item 5).

One question, answered identically at every spawn (paper_runner, the portfolio
runner, the live CLI): which feed does this runner bar from when the config
names none. ``config.source`` always wins; the switch only fills the gap; the
policy's synthetic-by-omission contract underneath is untouched.
"""
from __future__ import annotations

import pytest

from backtest.data.sources_policy import (
    DEPTH_SOURCE_ENV,
    reset_source_policy,
    resolve_run_source,
)


@pytest.fixture(autouse=True)
def _isolate_policy():
    reset_source_policy()
    yield
    reset_source_policy()


def _default_profile(monkeypatch) -> None:
    """Synthetic off, real feeds on — the deployment the switch targets."""
    monkeypatch.setenv("BACKTEST_DATA_PROFILE", "default")
    monkeypatch.delenv(DEPTH_SOURCE_ENV, raising=False)
    reset_source_policy()


def test_an_unset_switch_resolves_to_the_policy_broker_feed(monkeypatch):
    _default_profile(monkeypatch)
    assert resolve_run_source(None) == "mstock"
    assert resolve_run_source("") == "mstock"


def test_the_switch_selects_upstox_when_the_config_names_nothing(monkeypatch):
    _default_profile(monkeypatch)
    monkeypatch.setenv(DEPTH_SOURCE_ENV, "upstox")
    assert resolve_run_source(None) == "upstox"
    assert resolve_run_source("") == "upstox"


def test_an_explicit_config_source_beats_the_switch(monkeypatch):
    _default_profile(monkeypatch)
    monkeypatch.setenv(DEPTH_SOURCE_ENV, "upstox")
    assert resolve_run_source("mstock") == "mstock"
    assert resolve_run_source("dhan") == "dhan"
    assert resolve_run_source("generated") == "synthetic"  # policy-keyed


def test_an_unusable_switch_value_is_ignored_silently(monkeypatch):
    _default_profile(monkeypatch)
    monkeypatch.setenv(DEPTH_SOURCE_ENV, "co-location box B")
    assert resolve_run_source(None) == "mstock"


def test_the_answer_lands_on_config_source_and_env_changes_cannot_move_it(monkeypatch):
    _default_profile(monkeypatch)
    monkeypatch.setenv(DEPTH_SOURCE_ENV, "upstox")
    config_source = resolve_run_source(None)  # the spawn reads it once
    assert config_source == "upstox"
    monkeypatch.setenv(DEPTH_SOURCE_ENV, "mstock")  # mid-bar env change
    assert resolve_run_source(config_source) == "upstox"  # still pinned


def test_synthetic_still_comes_by_omission_where_the_policy_enables_it(monkeypatch):
    """The portfolio's synthetic-by-omission contract (api/portfolio.py:72-82)
    must survive the switch: no answer, no switch, policy synthetic -> synthetic.
    """
    monkeypatch.setenv("BACKTEST_DATA_PROFILE", "testing")
    monkeypatch.delenv(DEPTH_SOURCE_ENV, raising=False)
    reset_source_policy()
    assert resolve_run_source(None) == "synthetic"
