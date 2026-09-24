"""Backtest evaluation for the optimizer: one param set → standardized metrics.

Isolation / standardization (PRD "Backtest Runner" requirements)
----------------------------------------------------------------
* **Same data for every combination** — candles are loaded ONCE per run and
  handed to the worker pool through the pool *initializer*, so each worker
  process receives the frame a single time (no re-download, no re-pickle per
  task). Walk-forward windows are date slices of that same frame.
* **Same fills for every combination** — every evaluation goes through the
  repo's canonical entry points: :func:`backtest.engine.backtest_runner.run_backtest`
  (``engine='driver'``, next-bar-open fills — the default),
  :func:`~backtest.engine.backtest_runner.run_quick_screen` (vectorized quick
  filter) or :class:`~backtest.engine.option_backtest_driver.OptionBacktestDriver`
  (``engine='options'``, multi-leg structures, synthetic Black-Scholes chain).
* **No state leakage** — every evaluation builds a fresh strategy instance,
  portfolio and executor; workers are separate processes.

Metrics come from ``engine/metrics.compute_metrics`` + ``engine/trades`` (the
same code the Backtest page uses), then are mapped onto the
``optimization_results`` column set by :func:`standardize_metrics`.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
import traceback
from collections import OrderedDict
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

import numpy as np
import pandas as pd

from backtest.engine.backtester import BacktestConfig, BacktestResult
from backtest.engine.metrics import compute_metrics
from backtest.engine.trades import walk_trades

log = logging.getLogger("backtest.optimization.evaluator")

#: Cap used when a profitable run has no losing trade (PF would be ∞).
PROFIT_FACTOR_CAP = 100.0

#: Minutes per bar, for ``avg_holding_time_minutes`` (NSE session = 375 min).
_TF_MINUTES = {
    "1min": 1, "5min": 5, "15min": 15, "1hour": 60, "4hour": 240,
    "1day": 375, "1week": 375 * 5,
}


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvalWindow:
    """Evaluate on ``[start, end]``; bars from ``warmup_from`` feed indicators.

    Bars in ``[warmup_from, start)`` precede the measured range, so using
    them for indicator warm-up is not lookahead. The first measured bar is
    forced flat (no position carried in from the warm-up).
    """

    start: str
    end: str
    warmup_from: str | None = None

    def key(self) -> tuple:
        return (self.warmup_from, self.start, self.end)


def slice_frame(candles: pd.DataFrame, start: str | None, end: str | None) -> pd.DataFrame:
    idx = candles.index.strftime("%Y-%m-%d")
    mask = np.ones(len(candles), dtype=bool)
    if start:
        mask &= idx >= start
    if end:
        mask &= idx <= end
    return candles.loc[mask]


def _trim(result: BacktestResult, start: str, end: str, capital: float) -> BacktestResult:
    """Keep ``[start, end]`` of a result, rebased to ``capital``, flat first bar."""
    eq = result.equity
    dates = eq.index.strftime("%Y-%m-%d")
    mask = (dates >= start) & (dates <= end)
    equity = eq.loc[mask]
    if equity.empty:
        return result
    position = result.position.reindex(equity.index).fillna(0.0).copy()
    returns = equity.pct_change().fillna(0.0)
    if len(position):
        position.iloc[0] = 0.0
        returns.iloc[0] = 0.0
    rebased = pd.Series(capital * (1.0 + returns).cumprod().values, index=equity.index)
    candles = result.candles.reindex(equity.index) if result.candles is not None else None
    trimmed = BacktestResult(
        equity=rebased,
        returns=returns,
        position=position,
        candles=candles,
        config=result.config,
        metrics={},
    )
    trimmed.metrics = compute_metrics(trimmed)
    return trimmed


# ---------------------------------------------------------------------------
# Standardization
# ---------------------------------------------------------------------------


def _f(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _drawdown_duration_days(equity: pd.Series) -> int:
    """Longest peak-to-recovery (or peak-to-end) stretch, in calendar days."""
    if equity is None or len(equity) < 2:
        return 0
    peak = equity.cummax()
    under = equity < peak - 1e-9
    longest = 0
    start_ts = None
    for ts, is_under in under.items():
        if is_under and start_ts is None:
            start_ts = ts
        elif not is_under and start_ts is not None:
            longest = max(longest, (pd.Timestamp(ts) - pd.Timestamp(start_ts)).days)
            start_ts = None
    if start_ts is not None:
        longest = max(longest, (pd.Timestamp(equity.index[-1]) - pd.Timestamp(start_ts)).days)
    return int(longest)


def standardize_metrics(
    base: dict[str, Any],
    pnls: Sequence[float],
    equity: pd.Series,
    returns: pd.Series,
    *,
    timeframe: str = "1day",
    periods_per_year: int = 252,
) -> dict[str, Any]:
    """Map engine metrics onto the ``optimization_results`` column set.

    ``pnls`` are CLOSED-trade P&Ls (currency). ``base`` is a
    ``compute_metrics`` dict (or the options driver's equivalent).
    """
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = float(sum(wins))
    gross_loss = float(-sum(losses))
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    else:
        profit_factor = PROFIT_FACTOR_CAP if gross_profit > 0 else 0.0
    profit_factor = min(profit_factor, PROFIT_FACTOR_CAP)

    r = returns.fillna(0.0) if returns is not None else pd.Series(dtype=float)
    downside = r[r < 0]
    downside_dev = (
        float(downside.std(ddof=0) * math.sqrt(periods_per_year)) if len(downside) else 0.0
    )
    total_trades = int(base.get("num_trades", base.get("total_trades", len(pnls))) or 0)
    closed = len(pnls)
    win_rate = (len(wins) / closed * 100.0) if closed else 0.0
    holding_bars = _f(base.get("avg_holding_bars")) or 0.0
    return {
        "sharpe": _f(base.get("sharpe")) or 0.0,
        "sortino": _f(base.get("sortino")) or 0.0,
        "calmar": _f(base.get("calmar")) or 0.0,
        "total_return": _f(base.get("total_return")) or 0.0,
        "cagr": _f(base.get("cagr")) or 0.0,
        "max_drawdown": _f(base.get("max_drawdown")) or 0.0,
        "drawdown_duration_days": _drawdown_duration_days(equity),
        "profit_factor": profit_factor,
        "win_rate": round(win_rate, 2),
        "total_trades": total_trades,
        "closed_trades": closed,
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "expectancy": (sum(pnls) / closed) if closed else 0.0,
        "avg_win": (gross_profit / len(wins)) if wins else 0.0,
        "avg_loss": (-gross_loss / len(losses)) if losses else 0.0,
        "largest_win": max(wins) if wins else 0.0,
        "largest_loss": min(losses) if losses else 0.0,
        "volatility": _f(base.get("volatility")) or 0.0,
        "downside_deviation": downside_dev,
        "avg_holding_time_minutes": int(round(holding_bars * _TF_MINUTES.get(timeframe, 375))),
        "avg_slippage": None,
        "exposure": _f(base.get("exposure")) or 0.0,
        "final_equity": _f(equity.iloc[-1]) if equity is not None and len(equity) else None,
        "bars": int(len(equity)) if equity is not None else 0,
    }


def downsample_curve(equity: pd.Series, max_points: int = 400) -> list[list[Any]]:
    """``[[YYYY-MM-DD, equity], ...]`` with at most ``max_points`` points."""
    if equity is None or equity.empty:
        return []
    n = len(equity)
    step = max(1, math.ceil(n / max_points))
    picked = equity.iloc[::step]
    if picked.index[-1] != equity.index[-1]:
        picked = pd.concat([picked, equity.iloc[[-1]]])
    return [[pd.Timestamp(ts).strftime("%Y-%m-%d"), round(float(v), 2)]
            for ts, v in picked.items()]


# ---------------------------------------------------------------------------
# One evaluation (pure, picklable)
# ---------------------------------------------------------------------------


def split_params(params: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(strategy_params, engine_params)`` — engine knobs are ``engine.*``."""
    strategy, engine = {}, {}
    for k, v in params.items():
        if k.startswith("engine."):
            engine[k[len("engine."):]] = v
        else:
            strategy[k] = v
    return strategy, engine


def _run_equity_engine(
    candles: pd.DataFrame, settings: dict, strategy: str, sparams: dict,
    window: EvalWindow | None,
) -> BacktestResult:
    from backtest.engine.backtest_runner import run_backtest, run_quick_screen

    capital = float(settings["capital"])
    symbol = str(settings.get("symbol", "DEMO"))
    if window is not None:
        frame = slice_frame(candles, window.warmup_from or window.start, window.end)
    else:
        frame = candles
    if frame.empty:
        raise ValueError("no bars in the evaluation window")
    if settings.get("engine") == "quick_screen":
        start = window.start if window else frame.index[0].strftime("%Y-%m-%d")
        end = window.end if window else frame.index[-1].strftime("%Y-%m-%d")
        return run_quick_screen(frame, strategy, sparams, symbol, capital, start, end)
    result = run_backtest(frame, strategy, sparams, symbol, capital)
    if window is not None and window.warmup_from and window.warmup_from < window.start:
        result = _trim(result, window.start, window.end, capital)
    return result


def _threshold_decider(threshold: float) -> Callable[[Any], str | None]:
    """The driver's default structure decider with a tunable conviction cut.

    Mirrors ``default_structure_decider`` exactly (same enum comparisons,
    same structures) — only the 0.7 outright-vs-spread threshold moves.
    """
    from backtest.strategy.intent import Direction

    def decide(view: Any) -> str | None:
        if view.direction is Direction.BULLISH:
            return "long_call" if view.confidence >= threshold else "bull_call_spread"
        if view.direction is Direction.BEARISH:
            return "long_put" if view.confidence >= threshold else "bear_put_spread"
        return None

    return decide


def _run_options_engine(
    candles: pd.DataFrame, settings: dict, strategy: str, sparams: dict,
    eparams: dict, window: EvalWindow | None,
) -> tuple[pd.Series, list[float], dict[str, Any]]:
    from backtest.engine.option_backtest_driver import (
        BacktestConfig as OptConfig,
        OptionBacktestDriver,
    )
    from backtest.strategy.registry import get_strategy

    capital = float(settings["capital"])
    frame = candles
    if window is not None:
        frame = slice_frame(candles, window.warmup_from or window.start, window.end)
    if frame.empty:
        raise ValueError("no bars in the evaluation window")
    selector_type = str(settings.get("selector_type") or "atm")
    selector_kwargs: dict[str, Any] = {}
    if "delta_target" in eparams:
        selector_type = "delta"
        selector_kwargs["delta_target"] = float(eparams["delta_target"])
    decider = None
    if "spread_threshold" in eparams:
        decider = _threshold_decider(float(eparams["spread_threshold"]))
    max_open = eparams.get("max_open_structures")
    cfg = OptConfig(
        capital=capital,
        selector_type=selector_type,
        selector_kwargs=selector_kwargs,
        decider=decider,
        strategy_params=sparams,
        max_open_structures=int(max_open) if max_open is not None else None,
    )
    instance = get_strategy(strategy)(**sparams)
    underlying = str(settings.get("symbol") or "NIFTY")
    result = OptionBacktestDriver(frame, config=cfg, underlying=underlying,
                                  strategy=instance).run()
    points = [(pd.Timestamp(p.timestamp).normalize(), float(p.equity))
              for p in result.equity_curve]
    equity = pd.Series([v for _, v in points], index=[t for t, _ in points], dtype="float64")
    start = window.start if window else None
    pnls = []
    for rec in result.trade_log:
        if rec.is_open:
            continue
        opened = pd.Timestamp(rec.opened_at).strftime("%Y-%m-%d") if rec.opened_at else None
        if start and opened and opened < start:
            continue
        pnls.append(float(rec.realized_pnl) - float(rec.commission or 0))
    if start:
        dates = equity.index.strftime("%Y-%m-%d")
        equity = equity.loc[dates >= start]
        if not equity.empty:
            equity = equity / equity.iloc[0] * capital
    open_count = sum(1 for rec in result.trade_log if rec.is_open)
    extra = {"open_trades": open_count, "num_trades": len(pnls) + open_count}
    return equity, pnls, extra


def evaluate(
    candles: pd.DataFrame,
    settings: dict[str, Any],
    strategy: str,
    params: dict[str, Any],
    window: EvalWindow | None = None,
    keep_curve: bool = False,
) -> dict[str, Any]:
    """Run ONE backtest; never raises (errors come back in the payload)."""
    t0 = time.perf_counter()
    sparams, eparams = split_params(params)
    timeframe = str(settings.get("timeframe", "1day"))
    try:
        if settings.get("engine") == "options":
            equity, pnls, extra = _run_options_engine(
                candles, settings, strategy, sparams, eparams, window
            )
            if equity.empty:
                raise ValueError("options backtest produced no equity points")
            returns = equity.pct_change().fillna(0.0)
            dummy = BacktestResult(
                equity=equity, returns=returns,
                position=pd.Series(0.0, index=equity.index),
                candles=None, config=BacktestConfig(initial_capital=float(settings["capital"])),
                metrics={},
            )
            base = compute_metrics(dummy)
            base.update(extra)
        else:
            result = _run_equity_engine(candles, settings, strategy, sparams, window)
            equity, returns = result.equity, result.returns
            base = result.metrics or compute_metrics(result)
            trades = walk_trades(equity, result.position.fillna(0)) if len(equity) else []
            pnls = [float(t.pnl) for t in trades if not t.is_open]
        metrics = standardize_metrics(base, pnls, equity, returns, timeframe=timeframe)
        payload: dict[str, Any] = {"params": params, "metrics": metrics, "error": None}
        if keep_curve:
            payload["curve"] = downsample_curve(equity)
    except Exception as exc:  # noqa: BLE001 - one bad combination must not kill the run
        payload = {
            "params": params,
            "metrics": {},
            "error": f"{exc.__class__.__name__}: {exc}",
            "traceback": traceback.format_exc(limit=6),
        }
    payload["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    return payload


# ---------------------------------------------------------------------------
# Worker pool plumbing (module-level so it pickles)
# ---------------------------------------------------------------------------

_WORKER: dict[str, Any] = {}


def _init_worker(candles: pd.DataFrame, settings: dict, strategy: str) -> None:
    """Pool initializer: receive the shared frame once per worker process."""
    try:
        from backtest.plugins import discover_plugins

        discover_plugins()  # plugin strategies live in the parent's registry only
    except Exception:  # noqa: BLE001
        pass
    logging.getLogger("backtest").setLevel(logging.ERROR)  # quiet per-run warnings
    _WORKER.update(candles=candles, settings=settings, strategy=strategy)


def _worker_eval(job: tuple[dict, EvalWindow | None, bool]) -> dict[str, Any]:
    params, window, keep_curve = job
    return evaluate(
        _WORKER["candles"], _WORKER["settings"], _WORKER["strategy"], params, window, keep_curve
    )


class Cancelled(Exception):
    """Raised inside a search when the run is cancelled."""


class Evaluator:
    """Cached, optionally parallel batch evaluator with progress callbacks.

    ``workers <= 1`` evaluates inline (deterministic, used by tests). The
    cache is keyed by ``(params, window)`` so methods that revisit a point
    (Bayesian snapping, GA elitism, sensitivity sweeps through the optimum)
    never pay for the same backtest twice.
    """

    def __init__(
        self,
        candles: pd.DataFrame,
        settings: dict[str, Any],
        strategy: str,
        *,
        workers: int = 1,
        cancel_event: threading.Event | None = None,
        pause_event: threading.Event | None = None,
        cache_size: int = 50_000,
    ) -> None:
        self.candles = candles
        self.settings = dict(settings)
        self.strategy = strategy
        self.workers = max(1, int(workers))
        self.cancel_event = cancel_event or threading.Event()
        self.pause_event = pause_event or threading.Event()
        self._cache: OrderedDict[tuple, dict] = OrderedDict()
        self._cache_size = cache_size
        self._pool: ProcessPoolExecutor | None = None
        self.evaluations = 0  # backtests actually executed
        self.eval_seconds = 0.0

    # -- lifecycle ------------------------------------------------------------

    def __enter__(self) -> "Evaluator":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _ensure_pool(self) -> ProcessPoolExecutor | None:
        if self.workers <= 1:
            return None
        if self._pool is None:
            self._pool = ProcessPoolExecutor(
                max_workers=self.workers,
                initializer=_init_worker,
                initargs=(self.candles, self.settings, self.strategy),
            )
        return self._pool

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def cache_key(params: dict[str, Any], window: EvalWindow | None) -> tuple:
        return (tuple(sorted(params.items())), window.key() if window else None)

    def cached(self, params: dict[str, Any], window: EvalWindow | None = None) -> dict | None:
        return self._cache.get(self.cache_key(params, window))

    def _remember(self, key: tuple, payload: dict) -> None:
        self._cache[key] = payload
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    def _wait_if_paused(self) -> None:
        while self.pause_event.is_set() and not self.cancel_event.is_set():
            time.sleep(0.1)
        if self.cancel_event.is_set():
            raise Cancelled()

    # -- main API ---------------------------------------------------------------

    def evaluate_batch(
        self,
        param_sets: Iterable[dict[str, Any]],
        window: EvalWindow | None = None,
        *,
        keep_curve: bool = False,
        on_result: Callable[[dict[str, Any], bool], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Evaluate ``param_sets`` (order preserved); ``on_result(payload, fresh)``.

        ``fresh`` is False for cache hits. Raises :class:`Cancelled` when the
        cancel event fires (pending futures are dropped).
        """
        param_sets = list(param_sets)
        out: list[dict | None] = [None] * len(param_sets)
        todo: list[tuple[int, tuple, dict]] = []
        for i, params in enumerate(param_sets):
            key = self.cache_key(params, window)
            hit = self._cache.get(key)
            if hit is not None and (not keep_curve or "curve" in hit):
                out[i] = hit
                if on_result:
                    on_result(hit, False)
            else:
                todo.append((i, key, params))

        pool = self._ensure_pool()
        if pool is None:
            for i, key, params in todo:
                self._wait_if_paused()
                payload = evaluate(self.candles, self.settings, self.strategy, params,
                                   window, keep_curve)
                self._record(i, key, payload, out, on_result)
        else:
            pending: dict[Future, tuple[int, tuple]] = {}
            queue = list(todo)
            limit = self.workers * 4  # bounded in-flight → cancel/pause stay responsive
            while queue or pending:
                self._wait_if_paused()
                while queue and len(pending) < limit:
                    i, key, params = queue.pop(0)
                    fut = pool.submit(_worker_eval, (params, window, keep_curve))
                    pending[fut] = (i, key)
                done, _ = wait(list(pending), timeout=0.5, return_when=FIRST_COMPLETED)
                for fut in done:
                    i, key = pending.pop(fut)
                    try:
                        payload = fut.result()
                    except Exception as exc:  # noqa: BLE001 - broken worker
                        payload = {"params": param_sets[i], "metrics": {},
                                   "error": f"worker failed: {exc}", "elapsed_ms": 0.0}
                    self._record(i, key, payload, out, on_result)
                if self.cancel_event.is_set():
                    for fut in pending:
                        fut.cancel()
                    raise Cancelled()
        return [p for p in out if p is not None]

    def _record(self, i: int, key: tuple, payload: dict, out: list,
                on_result: Callable | None) -> None:
        self.evaluations += 1
        self.eval_seconds += float(payload.get("elapsed_ms", 0.0)) / 1000.0
        self._remember(key, payload)
        out[i] = payload
        if on_result:
            on_result(payload, True)


def default_workers() -> int:
    """``OPTIMIZER_WORKERS`` env, else min(8, CPU count)."""
    raw = os.getenv("OPTIMIZER_WORKERS")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            log.warning("OPTIMIZER_WORKERS=%r is not an integer — using the CPU count", raw)
    return max(1, min(8, os.cpu_count() or 1))
