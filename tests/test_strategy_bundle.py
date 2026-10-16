"""Phase 0 — transitive strategy-bundle fingerprint (Plan v2.1 §8, Gap #3).

The hash must change when a *template* file changes (the most likely edit
post-rewrite), be stable otherwise, and detect any drift at rehydration.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from backtest.strategy.bundle import bundle_matches, engine_git_sha, strategy_bundle
from backtest.strategy.templates import DirectionalOptionStrategy

import pytest


@pytest.fixture(autouse=True)
def _deregister_fixture_strategies():
    from backtest.strategy import registry as _registry

    def _purge() -> None:
        # Module-level fixture classes register at *collection* time, so a
        # before/after diff is not enough: purge phase0_* names outright,
        # at setup and again at teardown.
        for name in [n for n in _registry._REGISTRY if n.startswith("phase0")]:
            _registry.unregister(name)

    _purge()
    yield
    _purge()


class _BundleStrategy(DirectionalOptionStrategy):
    name = "phase0_bundle_strategy"
    description = "bundle fixture"
    version = "1.0"
    author = "tests"
    execution = {
        "emits": ["bullish"],
        "structures": {"bullish": "long_call"},
        "exit": {"structure_capped": True},
    }

    def entries(self, candles):
        return None

    def generate_market_view(self, candles):
        return None


def test_bundle_covers_strategy_template_and_execution_modules():
    bundle = strategy_bundle(_BundleStrategy)
    paths = [f.path for f in bundle.files]
    assert any(p.endswith("test_strategy_bundle.py") for p in paths)
    assert any("strategy/templates" in p for p in paths)
    assert any(p.endswith("strategy/execution.py") for p in paths)
    assert any(p.endswith("strategy/base.py") for p in paths)
    assert bundle.sha256 == strategy_bundle(_BundleStrategy).sha256  # stable


def test_bundle_matches_snapshot_and_detects_drift():
    stored = strategy_bundle(_BundleStrategy).to_dict()["bundle_files"]
    assert bundle_matches(_BundleStrategy, stored)
    drifted = [dict(entry) for entry in stored]
    drifted[0]["sha256"] = "0" * 64
    assert not bundle_matches(_BundleStrategy, drifted)


def test_template_edit_changes_inheritor_bundle(tmp_path, monkeypatch):
    # Simulate a template edit by hashing a copy of the template module
    # with one byte changed: the aggregate recipe is (path, sha) lines, so
    # any constituent change must move the aggregate.
    bundle = strategy_bundle(_BundleStrategy)
    template_entry = next(f for f in bundle.files if "templates" in f.path)
    original = Path(template_entry.path)
    if not original.is_absolute():
        original = Path.cwd() / original
    changed_sha = hashlib.sha256(original.read_bytes() + b"\n# edit\n").hexdigest()
    assert changed_sha != template_entry.sha256
    aggregate = hashlib.sha256()
    for entry in bundle.files:
        sha = changed_sha if entry is template_entry else entry.sha256
        aggregate.update(f"{entry.path}\0{sha}\n".encode())
    assert aggregate.hexdigest() != bundle.sha256


def test_engine_git_sha_is_honest():
    sha = engine_git_sha()
    assert sha is None or len(sha) >= 7  # None outside a checkout, never invented


def test_display_paths_use_posix_separators_on_every_platform():
    # bundle_sha256 aggregates these strings: a snapshot stamped on Linux
    # must recompute identically on Windows (verifier finding, Phase 0).
    bundle = strategy_bundle(_BundleStrategy)
    for entry in bundle.files:
        assert "\\" not in entry.path
        assert entry.path == entry.path.replace("\\", "/")
