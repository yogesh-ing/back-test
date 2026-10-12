"""Shared test helpers: synthetic loader, fake runner manager, a small SMA config."""

from __future__ import annotations

import copy
import dataclasses
from functools import lru_cache
from typing import Any


@lru_cache(maxsize=8)
def candles(symbol: str, start: str, end: str, timeframe: str):
    from backtest.runner import build_source

    return build_source("synthetic").get_candles(symbol, start, end, timeframe)


def synthetic_loader(cfg):
    bt = cfg.backtest
    return candles(bt.symbol, bt.start_date, bt.end_date, bt.timeframe)


class FakeRunner:
    def __init__(self, config):
        self.config = config


class FakeManager:
    """Just enough of PortfolioManager for apply/rollback."""

    def __init__(self):
        self.runners: dict[str, FakeRunner] = {}
        self.calls: list[tuple] = []
        self.audit: list[str] = []
        self._n = 0

    def add_runner(self, config, start: bool = True) -> str:
        self._n += 1
        iid = f"inst-{self._n}"
        self.runners[iid] = FakeRunner(dataclasses.replace(config, instance_id=iid))
        self.calls.append(("add", iid, dict(config.strategy_params or {})))
        return iid

    def remove_runner(self, instance_id: str) -> None:
        self.calls.append(("remove", instance_id))
        self.runners.pop(instance_id, None)

    def get_runner(self, instance_id: str):
        return self.runners.get(instance_id)

    def control_runner(self, instance_id: str, action: str) -> Any:
        self.calls.append((action, instance_id))
        return {"ok": True}

    def list_instances(self, _mode=None) -> list[dict]:
        return [{"instance_id": iid, "name": r.config.name,
                 "strategy_name": r.config.strategy_name, "mode": r.config.mode,
                 "status": "running"} for iid, r in self.runners.items()]

    def _audit_log(self, action: str, **kw: Any) -> None:
        self.audit.append(action)


SMA_DOC = {
    "strategyId": "sma_crossover",
    "objectiveFunction": "sharpe",
    "method": "grid",
    "parameters": [
        {"name": "fast", "min": 5, "max": 20, "step": 5},
        {"name": "slow", "min": 40, "max": 100, "step": 30},
    ],
    "constraints": [{"metric": "min_trades", "operator": ">=", "value": 3}],
    "backtestConfig": {"startDate": "2021-01-01", "endDate": "2023-12-31", "symbol": "DEMO"},
    # PRD Part 2 §2 gates synthetic data behind an explicit acknowledgement and
    # this fixture source IS synthetic, so the canonical document carries the
    # tick. Tests about the GATE use `unacknowledged_sma_doc()` instead — the
    # absence is the thing under test and should never be an accident.
    "dataAttestation": {"acknowledged": True},
}


def sma_doc(**over) -> dict:
    """A submission whose operator has already ticked the synthetic box.

    See ``SMA_DOC``: the tick lives on the canonical document so no test has to
    remember to add it, and the gate tests ask for its absence by name.
    """
    doc = copy.deepcopy(SMA_DOC)
    doc.update(over)
    return doc


def unacknowledged_sma_doc(**over) -> dict:
    """The same submission with the synthetic tick NOT given.

    Use this in tests that pin the gate itself; use :func:`sma_doc` for
    everything else.
    """
    doc = sma_doc(**over)
    doc.pop("dataAttestation", None)
    return doc
