from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from backtest.engine.backtester import BacktestConfig, Backtester
from backtest.forward.broker import SimulatedBroker
from backtest.forward.portfolio import Portfolio
from backtest.strategy.registry import get_strategy


def run_walkforward(source, strategies: list[str] | str, symbol: str, start: str, end: str, allocations: dict[str, float] | None = None, interval: str = "day") -> dict[str, Any]:
    if isinstance(strategies, str):
        strategies = [strategies]
    candles = source.get_candles(symbol, start, end, interval)
    if allocations is None:
        allocations = {name: 100_000.0 for name in strategies}

    portfolio = Portfolio(allocations)
    broker = SimulatedBroker(cost_rate=0.0008)
    
    # Pre-compute signals for all strategies using full window
    all_signals = {}
    for name in strategies:
        strategy_cls = get_strategy(name)
        strategy = strategy_cls()
        signals = strategy.generate_signals(candles)
        all_signals[name] = signals.shift(1).fillna(0).clip(-1, 1)
    
    per_strategy = {name: [] for name in strategies}
    
    # Initialize state trackers per strategy
    state = {}
    for name in strategies:
        state[name] = {
            "held": 0.0,
            "entry_price": None,
            "blocked": False,
            "equity": float(allocations.get(name, 100000.0)),
        }
    
    for idx, (bar_idx, row) in enumerate(candles.iterrows()):
        prev_close = float(candles["close"].iloc[idx - 1]) if idx > 0 else float(row["close"])
        
        for name in strategies:
            desired = float(all_signals[name].iloc[idx])
            
            # Run broker step (matches engine's per-bar logic)
            broker_state = broker.step(desired, row.to_dict(), state[name]["held"], prev_close, state[name]["entry_price"], state[name]["blocked"])
            
            # Extract broker outputs
            held = float(broker_state["held"])
            entry_price = broker_state["entry_price"]
            blocked = bool(broker_state["blocked"])
            net = float(broker_state["net"])
            
            # Compound equity like the engine does
            state[name]["equity"] *= (1.0 + net)
            state[name]["held"] = held
            state[name]["entry_price"] = entry_price
            state[name]["blocked"] = blocked
            
            per_strategy[name].append(state[name]["equity"])
    
    # Rebuild portfolio accounts from final state for snapshot compatibility
    for name in strategies:
        account = portfolio.allocate(name, allocations.get(name, 100000.0))
        account.position = state[name]["held"]
        account.entry_price = state[name]["entry_price"]
        account.blocked = state[name]["blocked"]
        account.equity_history = per_strategy[name]
    
    return {
        "portfolio": portfolio,
        "equity": {name: per_strategy[name] for name in strategies},
        "total_equity": sum(per_strategy[name][-1] if per_strategy[name] else 0.0 for name in strategies),
    }


def _load_live_state(path: str) -> tuple[Portfolio, dict[str, Any]]:
    file_path = Path(path)
    if not file_path.exists():
        return Portfolio(), {"resume_count": 0, "processed_bars": 0, "equity_history": {}, "positions": {}, "entry_prices": {}, "blocked": {}}

    payload = json.loads(file_path.read_text())
    if isinstance(payload, dict) and "portfolio" in payload:
        portfolio = Portfolio.load_from_snapshot(payload.get("portfolio", {}))
        state = payload.get("state", {})
        for strategy, account in portfolio.accounts.items():
            track = state.get("equity_history", {}).get(strategy, [])
            if track:
                account.equity_history = [float(x) for x in track]
            if strategy in state.get("positions", {}):
                account.position = float(state["positions"][strategy])
            if strategy in state.get("entry_prices", {}):
                account.entry_price = state["entry_prices"][strategy]
            if strategy in state.get("blocked", {}):
                account.blocked = bool(state["blocked"][strategy])
        return portfolio, state

    portfolio = Portfolio.load_from_snapshot(payload)
    return portfolio, {"resume_count": 0, "processed_bars": 0, "equity_history": {}, "positions": {}, "entry_prices": {}, "blocked": {}}


def _save_live_state(portfolio: Portfolio, path: str, state: dict[str, Any]) -> str:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"portfolio": portfolio.snapshot(), "state": state}
    if state.get("equity_history") is None:
        state["equity_history"] = {}
    for name, account in portfolio.accounts.items():
        state.setdefault("equity_history", {})[name] = list(account.equity_history)
        state.setdefault("positions", {})[name] = account.position
        state.setdefault("entry_prices", {})[name] = account.entry_price
        state.setdefault("blocked", {})[name] = account.blocked
    file_path.write_text(json.dumps(payload, indent=2))
    return str(file_path)


def run_live_papertrade(
    source,
    strategies: list[str] | str,
    symbol: str,
    allocations: dict[str, float] | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    interval: str = "day",
    state_file: str | None = None,
    poll_interval_s: int = 60,
    resume_on_start: bool = True,
) -> dict[str, Any]:
    """Run a live-style paper trade loop with resumable state.

    This uses the same broker math as walk-forward, but it can resume from a saved
    snapshot on subsequent runs. Each call recomputes the strategy on the full
    historical window and processes only bars not yet persisted.
    """
    if isinstance(strategies, str):
        strategies = [strategies]
    if not strategies:
        raise ValueError("at least one strategy required")
    if allocations is None:
        allocations = {name: 100_000.0 for name in strategies}

    if from_date is None or to_date is None:
        raise ValueError("live papertrade requires both --from and --to")

    candles = source.get_candles(symbol, from_date, to_date, interval)
    portfolio = Portfolio(allocations)
    meta = {"resume_count": 0, "processed_bars": 0}

    if state_file and resume_on_start:
        portfolio, saved_meta = _load_live_state(state_file)
        meta = {
            "resume_count": int(saved_meta.get("resume_count", 0)) + 1,
            "processed_bars": int(saved_meta.get("processed_bars", 0)),
            "equity_history": saved_meta.get("equity_history", {}),
            "positions": saved_meta.get("positions", {}),
            "entry_prices": saved_meta.get("entry_prices", {}),
            "blocked": saved_meta.get("blocked", {}),
        }
        if int(meta.get("processed_bars", 0)) >= len(candles):
            per_strategy = {name: list(portfolio.accounts.get(name, portfolio.allocate(name, float(allocations.get(name, 100_000.0)))).equity_history) for name in strategies}
            saved_state = {
                "resume_count": meta.get("resume_count", 0),
                "processed_bars": len(candles),
                "poll_interval_s": poll_interval_s,
                "equity_history": {name: list(per_strategy.get(name, [])) for name in strategies},
                "positions": {name: float(portfolio.accounts.get(name, portfolio.allocate(name, float(allocations.get(name, 100_000.0)))).position) for name in strategies},
                "entry_prices": {name: portfolio.accounts.get(name, portfolio.allocate(name, float(allocations.get(name, 100_000.0)))).entry_price for name in strategies},
                "blocked": {name: bool(portfolio.accounts.get(name, portfolio.allocate(name, float(allocations.get(name, 100_000.0)))).blocked) for name in strategies},
            }
            _save_live_state(portfolio, state_file, saved_state)
            return {
                "portfolio": portfolio,
                "equity": {name: per_strategy[name] for name in strategies},
                "total_equity": sum(per_strategy[name][-1] if per_strategy[name] else 0.0 for name in strategies),
                "state": {**saved_state},
            }

    for name in strategies:
        account = portfolio.accounts.get(name)
        if account is None:
            account = portfolio.allocate(name, float(allocations.get(name, 100_000.0)))
        if not account.equity_history:
            account.equity_history = [float(allocations.get(name, 100_000.0))]

    broker = SimulatedBroker(cost_rate=0.0008)
    per_strategy = {name: list(portfolio.accounts.get(name, portfolio.allocate(name, float(allocations.get(name, 100_000.0)))).equity_history) for name in strategies}
    state = {}
    for name in strategies:
        account = portfolio.accounts.get(name)
        state[name] = {
            "held": float(account.position if account else 0.0),
            "entry_price": account.entry_price if account else None,
            "blocked": bool(account.blocked if account else False),
            "equity": float(account.equity_history[-1] if account and account.equity_history else allocations.get(name, 100_000.0)),
        }

    start_idx = int(meta.get("processed_bars", 0))
    all_signals = {}
    for name in strategies:
        strategy_cls = get_strategy(name)
        strategy = strategy_cls()
        signals = strategy.generate_signals(candles)
        all_signals[name] = signals.shift(1).fillna(0).clip(-1, 1)

    for idx in range(start_idx, len(candles)):
        row = candles.iloc[idx]
        prev_close = float(candles["close"].iloc[idx - 1]) if idx > 0 else float(row["close"])
        for name in strategies:
            desired = float(all_signals[name].iloc[idx])
            broker_state = broker.step(desired, row.to_dict(), state[name]["held"], prev_close, state[name]["entry_price"], state[name]["blocked"])
            held = float(broker_state["held"])
            entry_price = broker_state["entry_price"]
            blocked = bool(broker_state["blocked"])
            net = float(broker_state["net"])
            state[name]["equity"] *= (1.0 + net)
            state[name]["held"] = held
            state[name]["entry_price"] = entry_price
            state[name]["blocked"] = blocked
            per_strategy[name].append(state[name]["equity"])

            account = portfolio.accounts.get(name)
            if account is None:
                account = portfolio.allocate(name, float(allocations.get(name, 100_000.0)))
            account.position = state[name]["held"]
            account.entry_price = state[name]["entry_price"]
            account.blocked = state[name]["blocked"]
            account.equity_history = per_strategy[name]

        meta["processed_bars"] = idx + 1
        if state_file:
            _save_live_state(portfolio, state_file, {
                "resume_count": meta.get("resume_count", 0),
                "processed_bars": meta["processed_bars"],
                "poll_interval_s": poll_interval_s,
                "equity_history": {name: list(per_strategy.get(name, [])) for name in strategies},
                "positions": {name: float(portfolio.accounts.get(name, portfolio.allocate(name, float(allocations.get(name, 100_000.0)))).position) for name in strategies},
                "entry_prices": {name: portfolio.accounts.get(name, portfolio.allocate(name, float(allocations.get(name, 100_000.0)))).entry_price for name in strategies},
                "blocked": {name: bool(portfolio.accounts.get(name, portfolio.allocate(name, float(allocations.get(name, 100_000.0)))).blocked) for name in strategies},
            })

    if state_file:
        _save_live_state(portfolio, state_file, {
            "resume_count": meta.get("resume_count", 0),
            "processed_bars": meta["processed_bars"],
            "poll_interval_s": poll_interval_s,
            "equity_history": {name: list(per_strategy.get(name, [])) for name in strategies},
            "positions": {name: float(portfolio.accounts.get(name, portfolio.allocate(name, float(allocations.get(name, 100_000.0)))).position) for name in strategies},
            "entry_prices": {name: portfolio.accounts.get(name, portfolio.allocate(name, float(allocations.get(name, 100_000.0)))).entry_price for name in strategies},
            "blocked": {name: bool(portfolio.accounts.get(name, portfolio.allocate(name, float(allocations.get(name, 100_000.0)))).blocked) for name in strategies},
        })

    return {
        "portfolio": portfolio,
        "equity": {name: per_strategy[name] for name in strategies},
        "total_equity": sum(per_strategy[name][-1] if per_strategy[name] else 0.0 for name in strategies),
        "state": {
            "resume_count": meta.get("resume_count", 0),
            "processed_bars": meta.get("processed_bars", 0),
            "poll_interval_s": poll_interval_s,
            "equity_history": {name: list(per_strategy.get(name, [])) for name in strategies},
            "positions": {name: float(portfolio.accounts.get(name, portfolio.allocate(name, float(allocations.get(name, 100_000.0)))).position) for name in strategies},
            "entry_prices": {name: portfolio.accounts.get(name, portfolio.allocate(name, float(allocations.get(name, 100_000.0)))).entry_price for name in strategies},
            "blocked": {name: bool(portfolio.accounts.get(name, portfolio.allocate(name, float(allocations.get(name, 100_000.0)))).blocked) for name in strategies},
        },
    }


def poll_live_papertrade(
    source,
    strategies: list[str] | str,
    symbol: str,
    allocations: dict[str, float] | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    interval: str = "day",
    state_file: str | None = None,
    poll_interval_s: int = 60,
    resume_on_start: bool = True,
    max_cycles: int | None = None,
) -> list[dict[str, Any]]:
    """Poll live market data and process a paper trade loop on each tick."""
    if poll_interval_s <= 0:
        raise ValueError("poll_interval_s must be positive")
    if to_date is None:
        to_date = datetime.utcnow().strftime("%Y-%m-%d")

    cycles = 0
    results: list[dict[str, Any]] = []
    while True:
        result = run_live_papertrade(
            source=source,
            strategies=strategies,
            symbol=symbol,
            allocations=allocations,
            from_date=from_date,
            to_date=to_date,
            interval=interval,
            state_file=state_file,
            poll_interval_s=poll_interval_s,
            resume_on_start=resume_on_start,
        )
        results.append(result)
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            break
        time.sleep(poll_interval_s)

    return results


def save_state(portfolio: Portfolio, path: str) -> str:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps(portfolio.snapshot(), indent=2))
    return str(file_path)


def load_state(path: str) -> Portfolio:
    payload = json.loads(Path(path).read_text())
    if isinstance(payload, dict) and "portfolio" in payload:
        return Portfolio.load_from_snapshot(payload["portfolio"])
    return Portfolio.load_from_snapshot(payload)
