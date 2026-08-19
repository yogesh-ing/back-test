from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import pandas as pd

from backtest.data.csv_source import CsvSource
from backtest.data.synthetic import SyntheticSource
from backtest.engine.backtester import BacktestConfig, Backtester
from backtest.strategy.registry import get_strategy


@dataclass
class RunSpec:
    strategy: str
    symbol: str
    start: str
    end: str
    interval: str = "day"
    strategy_params: dict[str, Any] | None = None


def build_source(name: str, **kwargs):
    source_name = (name or "").lower()
    if source_name == "synthetic":
        return SyntheticSource()
    if source_name == "csv":
        return CsvSource(root=kwargs.get("data_root", "data"))
    if source_name == "mstock":
        from backtest.live.mstock import MStockSource
        return MStockSource()
    raise ValueError(f"unsupported source: {name}")


def _effective_config(config: BacktestConfig | None, strategy: Any) -> BacktestConfig:
    cfg = config or BacktestConfig()
    if cfg.stop_loss is None:
        cfg = replace(cfg, stop_loss=getattr(strategy, "stop_loss", None))
    if cfg.take_profit is None:
        cfg = replace(cfg, take_profit=getattr(strategy, "take_profit", None))
    return cfg


def run_on_candles(candles: pd.DataFrame, strategy_name: str, strategy_params: dict[str, Any] | None = None, symbol: str = "DEMO", config: BacktestConfig | None = None):
    strategy_cls = get_strategy(strategy_name)
    strategy_params = strategy_params or {}
    strategy = strategy_cls(**strategy_params)
    cfg = _effective_config(config, strategy)

    signals = strategy.generate_signals(candles)
    result = Backtester(cfg).run(candles, signals)
    result.metrics["strategy"] = strategy_name
    result.metrics["strategy_params"] = strategy_params
    result.metrics["symbol"] = symbol
    result.metrics["stop_loss"] = cfg.stop_loss
    result.metrics["take_profit"] = cfg.take_profit
    return result


def run_backtest(source, spec: RunSpec, config: BacktestConfig | None = None):
    candles = source.get_candles(spec.symbol, spec.start, spec.end, spec.interval)
    return run_on_candles(candles, spec.strategy, spec.strategy_params, spec.symbol, config)


def compare_strategies(source, symbol: str, start: str, end: str, strategies: list[str], interval: str = "day", config: BacktestConfig | None = None) -> dict[str, Any]:
    candles = source.get_candles(symbol, start, end, interval)
    results = {}
    for name in strategies:
        results[name] = run_on_candles(candles, name, config=config)
    return results
