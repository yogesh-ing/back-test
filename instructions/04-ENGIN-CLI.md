# Card 04 — Engine, metrics, plotting, runner, CLI

**Prerequisite:** Cards 00–03. **Builds:** a working `run` + `compare` on
synthetic data. Honor Card 00 invariants exactly.

## `engine/backtester.py`
- `@dataclass BacktestConfig(initial_capital=100_000.0, commission_pct=0.0003,
  slippage_pct=0.0005, periods_per_year=252, stop_loss=None, take_profit=None)`.
- `@dataclass BacktestResult(equity, returns, position, candles, config, metrics)`.
- `Backtester(config=None).run(candles, signals)`: align + clip target to
  [−1,1]; if `stop_loss` or `take_profit` set ⇒ `_run_with_risk` else
  `_run_vectorized`; `equity = cumprod(1+net)×capital`;
  `metrics = compute_metrics(result)`.
  - `_run_vectorized(close, target)`: `held=target.shift(1).fillna(0)`;
    `gross=held×close.pct_change()`; `turnover=held.diff().abs()`;
    `costs=turnover×(commission+slippage)`; `net=gross−costs`.
  - `_run_with_risk(candles, target)`: per-bar loop implementing Card 00 rule 5
    exactly (entry-price tracking, intrabar stop/target vs `low`/`high`,
    stop-first, re-entry block, turnover cost on entry and forced exit).

## `engine/metrics.py`
- `compute_metrics(result) -> dict` keys: `total_return, cagr, volatility,
  sharpe, max_drawdown, calmar, num_trades, win_rate, exposure, final_equity,
  bars`.
  - `total_return = equity[-1]/capital − 1`;
    `cagr = (equity[-1]/equity[0])**(1/years) − 1`, `years=n/ppy`.
  - `volatility = std(ddof=0)×√ppy`; `sharpe = mean×ppy/vol` (0 if vol 0).
  - `max_drawdown = min(equity/equity.cummax()−1)`; `calmar = cagr/|dd|` (0 if
    dd≥0).
  - `num_trades, win_rate` via `_trade_stats`: open 0→nonzero, close on
    return-to-0 or sign flip; PnL = dir×(exit−entry) on close; open-at-end counts
    marked-to-last.
  - `exposure = mean(|position|>0)`.

## `engine/plotting.py`
- `plot_result(result, path=None, show=False)`: 3 panels — equity, drawdown %
  underwater, close shaded where in-market; `Agg` backend when not showing.
- `plot_comparison(results: dict, path=None, show=False)`: overlay equity curves
  (labelled with Sharpe) + shared drawdown panel.

## `runner.py`
- `@dataclass RunSpec(strategy, symbol, start, end, interval="day",
  strategy_params=None)`.
- `build_source(name, **kwargs)`: `synthetic|csv|mstock` factory.
- `run_backtest(source, spec, config=None)`: fetch candles once → `run_on_candles`.
- `run_on_candles(candles, strategy_name, strategy_params, symbol, config=None)`:
  instantiate; `cfg=_effective_config(config, strategy)`; signals; run engine;
  attach `strategy, strategy_params, symbol, stop_loss, take_profit` to metrics.
- `compare_strategies(source, symbol, start, end, strategies, interval="day",
  config=None) -> dict`: fetch candles **once**; run each; preserve order.
- `_effective_config(config, strategy)`: **copy** via `dataclasses.replace` (no
  stop leak); fill `stop_loss/take_profit` from the strategy when config leaves
  them `None`.

## `cli.py` (+ `__main__.py` → `cli.main()`)
Subcommands: `list`, `preflight`, `run`, `compare`.
- **`list`**: print each strategy name, params, first docstring line.
- **`preflight`**: non-destructive (no login) — dependency imports, `.env` keys,
  `MSTOCK_AUTH_MODE` valid, DNS + HTTPS reachability for `api.mstock.trade`;
  OK/FAIL rows; non-zero exit if any FAIL.
- **`run`** flags: `--strategy`(req) `--source{synthetic,csv,mstock}` `--symbol`
  `--from` `--to` `--interval` `--capital` `--commission` `--slippage`
  `--stop-loss` `--take-profit` `--param k=v`(repeatable, type-coerced)
  `--data-root` `--totp` `--json` `--save-equity` `--plot [path]`
  `--chart-dir`(default `charts`) `--no-chart` `--show`.
  - Chart **on by default** → `charts/<strategy>_<symbol>_<interval>_<epoch>.png`.
  - Print metrics table; add a `Risk` line when stop/target set.
- **`compare`** flags: `--strategies a,b,c`(req, ≥2) + same source/date/cost/risk
  flags + `--sort-by{sharpe,cagr,total_return,max_drawdown,calmar,win_rate,volatility,num_trades}`
  (default `sharpe`; only `volatility` ascending) `--csv` `--json` chart flags.
  - Print ranked table; save overlay to
    `charts/compare_<symbol>_<interval>_<epoch>.png`.

**Verify:**
```
$env:PYTHONPATH="src"
python -m backtest run --strategy donchian_breakout --source synthetic --symbol DEMO --from 2021-01-01 --to 2025-01-01
python -m backtest compare --strategies sma_crossover,rsi_reversion,buy_and_hold --source synthetic --symbol DEMO --from 2021-01-01 --to 2025-01-01
```