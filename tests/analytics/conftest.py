"""Shared fixtures for the cross-broker analytics tests (PRD-003).

Builds a real :class:`~backtest.forward.portfolio_manager.PortfolioManager`
with two real runners, then overrides the ONE venue-resolution seam
(``PortfolioManager._runner_broker``) so the paper runners report two
different venues. Everything else — the order ledger, closed trades, equity
curves, ``get_state`` — is the production object, so these tests exercise the
same code the dashboard runs.

Why override the seam instead of spawning ``mode='live'`` runners: a live
runner fails closed unless a real broker session is authenticated (see
``PortfolioManager._live_gateway_for``), which no unit test should do.
"""

from __future__ import annotations

import textwrap
from typing import Dict, List, Tuple

import pytest

from backtest.brokers.segments import reset_segments_config
from backtest.forward.paper_runner import RunnerConfig
from backtest.forward.portfolio_manager import get_portfolio_manager, reset_portfolio_manager

BROKERS = ("mstock", "dhan")

#: Segment names used by these tests. Runners are fail-closed against an
#: unknown segment (``StrategyRunner.__init__`` → ``resolve_execution_broker``),
#: so the test owns its config rather than inheriting ``config/segments.yaml``.
SEGMENTS_YAML = textwrap.dedent(
    """
    segments:
      options_index:
        display_name: "Index Options"
        broker: mstock
        mode: paper
        allocated_capital: 1200000
      equity_intraday:
        display_name: "Equity Intraday"
        broker: dhan
        mode: paper
        allocated_capital: 800000
      ghost:
        display_name: "Ghost Segment"
        broker: dhan
        mode: paper
        allocated_capital: 100000
    data:
      primary: mstock
    """
)


class VenueMap:
    """Patchable ``_runner_broker`` that answers from an instance → venue map."""

    def __init__(self) -> None:
        self.by_instance: Dict[str, str] = {}
        self.default = "paper"

    def __call__(self, runner) -> str:
        return self.by_instance.get(runner.instance_id, self.default)

    def assign(self, instance_id: str, broker: str) -> str:
        self.by_instance[instance_id] = broker
        return instance_id


@pytest.fixture()
def venues(monkeypatch, tmp_path):
    """A two-venue deployment: runners tagged to ``mstock`` / ``dhan``."""
    path = tmp_path / "segments.yaml"
    path.write_text(SEGMENTS_YAML, encoding="utf-8")
    monkeypatch.setenv("SEGMENTS_CONFIG_PATH", str(path))
    reset_segments_config()

    reset_portfolio_manager()
    mgr = get_portfolio_manager()
    venue_map = VenueMap()
    monkeypatch.setattr(type(mgr), "_runner_broker", venue_map)

    made: List[Tuple[str, str]] = []

    def add(
        name: str,
        strategy: str,
        broker: str,
        capital: float,
        segment: str = "equity_intraday",
    ):
        cfg = RunnerConfig(
            name=name,
            strategy_name=strategy,
            symbols=["NIFTY"],
            allocated_capital=capital,
            mode="paper",
            source="synthetic",
            segment=segment,
        )
        instance_id = mgr.add_runner(cfg, start=False)
        venue_map.assign(instance_id, broker)
        made.append((instance_id, broker))
        return instance_id

    yield {"mgr": mgr, "add": add, "venues": venue_map, "made": made}
    reset_portfolio_manager()
    reset_segments_config()
