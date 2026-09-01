"""Legacy vectorized runner (quick-screen engine) and shared source factory.

LAYERING (ticket #6): this module is NOT the canonical backtest entry. The
single canonical entry is ``backtest.engine.backtest_runner.run_backtest``
(the fill-exact :class:`~backtest.engine.backtest_driver.BacktestDriver`);
what remains here is the legacy vectorized
:class:`~backtest.engine.backtester.Backtester` path (the optional
``mode='quick_screen'`` filter and the CLI's historical ``run``/``compare``
commands, whose numbers are the documented pre-P2.2 shape) plus the shared
``build_source`` factory and ``RunSpec``.

* :func:`run_on_source` — fetch candles + ``run_on_candles`` (legacy path).
* :func:`run_on_candles` — the vectorized engine wrapper itself (also used
  by :func:`backtest.engine.backtest_runner.run_quick_screen`).
* :func:`compare_strategies` — CLI comparison over one candle fetch.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import pandas as pd

from backtest.data.csv_source import CsvSource
from backtest.data.synthetic import SyntheticSource
from backtest.engine.backtester import BacktestConfig, Backtester
from backtest.logging_config import get_logger
from backtest.strategy.registry import get_strategy

log = get_logger(__name__)


@dataclass
class RunSpec:
    strategy: str
    symbol: str
    start: str
    end: str
    interval: str = "1day"
    strategy_params: dict[str, Any] | None = None


def build_source(name: str, **kwargs):
    source_name = (name or "").lower()
    if source_name == "synthetic":
        source: Any = SyntheticSource()
    elif source_name == "csv":
        source = CsvSource(root=kwargs.get("data_root", "data"))
    elif source_name == "mstock":
        from backtest.live.mstock import MStockSource

        source = MStockSource()
    elif source_name == "db":
        from backtest.data.db_source import DbSource

        source = DbSource()
    else:
        log.error("[source] unsupported source %r (want synthetic|csv|mstock|db)", name)
        raise ValueError(f"unsupported source: {name}")
    log.debug("[source] %s → %s", source_name, type(source).__name__)
    return source


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
    active = int((signals.fillna(0) != 0).sum())
    log.debug(
        "[run] %s on %s: %d bars, params=%s, %d/%d bars with a signal, stop=%s tp=%s",
        strategy_name, symbol, len(candles), strategy_params, active, len(candles),
        cfg.stop_loss, cfg.take_profit,
    )
    if active == 0:
        log.warning(
            "[run] %s produced NO signals on %s (%d bars, params=%s) — the run will be "
            "flat: the indicator warmup likely exceeds the window, or the thresholds "
            "never trigger on this data",
            strategy_name, symbol, len(candles), strategy_params,
        )
    result = Backtester(cfg).run(candles, signals)
    result.metrics["strategy"] = strategy_name
    result.metrics["strategy_params"] = strategy_params
    result.metrics["symbol"] = symbol
    result.metrics["stop_loss"] = cfg.stop_loss
    result.metrics["take_profit"] = cfg.take_profit
    log.debug(
        "[run] %s → return=%.4f trades=%s win_rate=%.2f exposure=%.2f",
        strategy_name, result.metrics.get("total_return", 0.0),
        result.metrics.get("num_trades", 0), result.metrics.get("win_rate", 0.0),
        result.metrics.get("exposure", 0.0),
    )
    return result


def run_on_source(source, spec: RunSpec, config: BacktestConfig | None = None):
    """Legacy vectorized run over a source (CLI path; NOT the canonical engine)."""
    candles = source.get_candles(spec.symbol, spec.start, spec.end, spec.interval)
    return run_on_candles(candles, spec.strategy, spec.strategy_params, spec.symbol, config)


def compare_strategies(source, symbol: str, start: str, end: str, strategies: list[str], interval: str = "1day", config: BacktestConfig | None = None) -> dict[str, Any]:
    candles = source.get_candles(symbol, start, end, interval)
    results = {}
    for name in strategies:
        results[name] = run_on_candles(candles, name, config=config)
    return results
