# Backtest Engine

## Overview

The engine simulates trading based on signals from a strategy. It takes OHLCV candles + signals, applies commission/slippage, and produces an equity curve + metrics.

## Core Classes

### BacktestConfig (`engine/backtester.py`)

```python
@dataclass
class BacktestConfig:
    initial_capital: float = 100_000.0
    commission_pct: float = 0.0003      # 0.03% per trade
    slippage_pct: float = 0.0005        # 0.05% slippage
    periods_per_year: int = 252         # Trading days per year
    stop_loss: float | None = None      # e.g. 0.05 = 5%
    take_profit: float | None = None    # e.g. 0.10 = 10%
```

### BacktestResult (`engine/backtester.py`)

```python
@dataclass
class BacktestResult:
    equity: pd.Series       # Dollar value over time
    returns: pd.Series      # Period-by-period returns
    position: pd.Series     # +1 (long), -1 (short), 0 (flat)
    candles: pd.DataFrame   # Original OHLCV data
    config: BacktestConfig  # Config used
    metrics: dict           # Computed metrics (Sharpe, drawdown, etc.)
```

## Execution Flow

```
1. Strategy.generate_signals(candles) -> signals (+1, -1, 0)
2. Backtester.run(candles, signals) -> BacktestResult
   a. Lag signals by 1 day (no lookahead)
   b. Calculate position changes (turnover)
   c. Apply commission + slippage on each change
   d. Compute equity curve from returns
   e. If stop_loss/take_profit set: use intrabar logic
3. compute_metrics(result) -> dict of metrics
4. BacktestAdapter(result).to_all() -> JSON for UI
```

## Two Execution Modes

### Vectorized (Fast)
Used when no stop_loss/take_profit:
```python
held = target.shift(1).fillna(0)           # Lagged position
gross = held * close.pct_change()          # Gross returns
costs = turnover * (commission + slippage) # Transaction costs
equity = capital * (1 + net).cumprod()     # Equity curve
```

### Risk-Managed (Bar-by-Bar)
Used when stop_loss or take_profit is set:
- Tracks entry price
- Checks intrabar high/low for stop/target hits
- Blocks re-entry after stop/target exit
- Same equity calculation, but with exit logic

## Metrics (`engine/metrics.py`)

| Metric | Key | Description |
|--------|-----|-------------|
| Total Return | `total_return` | Final equity / initial - 1 |
| CAGR | `cagr` | Annualized return |
| Volatility | `volatility` | Annualized std dev of returns |
| Sharpe Ratio | `sharpe` | Return / volatility |
| Max Drawdown | `max_drawdown` | Worst peak-to-trough decline |
| Calmar Ratio | `calmar` | CAGR / abs(max_drawdown) |
| Win Rate | `win_rate` | Share of **closed** trades with P&L > 0 (0.0 when nothing has closed) |
| Num Trades | `num_trades` | Round trips, **including** a position still open at the last bar |
| Closed / Open | `closed_trades`, `open_trades` | The split behind those two numbers |
| Win / Loss count | `winning_trades`, `losing_trades` | Ties (P&L == 0) are neither |
| Trade P&L | `realised_pnl`, `avg_trade_pnl`, `best_trade_pnl`, `worst_trade_pnl` | Per-trade equity deltas, closed trades only |
| Exposure | `exposure` | % of time in market |
| Final Equity | `final_equity` | End dollar value |

## Trade Accounting (`engine/trades.py`)

`compute_metrics` and `BacktestAdapter.to_trades()` both read
:func:`backtest.engine.trades.walk_trades`, so the "Trades"/"Win Rate" cards and
the trade table are literally the same computation:

| Rule | Why |
|---|---|
| A trade = a run of bars holding the same sign | Entry and exit are one trade, not two |
| P&L = `equity[exit] − equity[entry−1]` | Costs are booked on the bars where the position changes, so they land on the trade that paid them, and Σ trade P&L == total P&L exactly |
| A position open at the last bar counts in `num_trades`, marked to the final close | The table needs to show it; it is not a result yet |
| …and is **excluded from `win_rate`** | An unrealised trade is neither a win nor a loss; the UI shows "—" instead of a fake 0.00% when nothing has closed |
| A sign flip closes at the previous bar and re-opens on the flip bar | Every bar is attributed exactly once — no double counting |

## Commission Model

```python
# Applied on every position change
turnover = abs(held.diff())  # 0 -> 1 or 1 -> 0
costs = turnover * (commission_pct + slippage_pct)
net_returns = gross_returns - costs
```

Default: 0.03% commission + 0.05% slippage = 0.08% total cost per trade.

## Running a Backtest

### Via Python API
```python
from backtest.runner import build_source, run_on_candles
from backtest.engine.backtester import BacktestConfig

source = build_source("synthetic")
candles = source.get_candles("DEMO", "2024-01-01", "2024-12-31", "day")
config = BacktestConfig(initial_capital=100_000, stop_loss=0.05)
result = run_on_candles(candles, "sma_crossover", {"fast": 20, "slow": 50}, "DEMO", config)

print(f"Sharpe: {result.metrics['sharpe']:.2f}")
print(f"Return: {result.metrics['total_return']*100:.1f}%")
print(f"Max DD: {result.metrics['max_drawdown']*100:.1f}%")
```

### Via CLI
```bash
PYTHONPATH=src python -m backtest run \
  --strategy sma_crossover \
  --source synthetic \
  --symbol DEMO \
  --from 2024-01-01 \
  --to 2024-12-31 \
  --capital 100000 \
  --stop-loss 0.05 \
  --plot
```

### Via Web API
```bash
curl -X POST http://localhost:5000/api/backtest/run \
  -H "Content-Type: application/json" \
  -d '{"strategy":"sma_crossover","symbol":"DEMO","from_date":"2024-01-01","to_date":"2024-12-31","capital":100000,"params":{"fast":20,"slow":50}}'
```

## Adapter (`adapters/backtest_adapter.py`)

Converts `BacktestResult` to JSON for the UI:

```python
adapter = BacktestAdapter(result)
payload = adapter.to_all()
# Returns: {
#   "config": {...},
#   "metrics": {"total_pnl": ..., "sharpe": ..., ...},
#   "equity": [{"date": "2024-01-01", "value": 100000}, ...],
#   "drawdown": [...],
#   "trades": [{"id": 1, "entry_date": ..., "side": "LONG", ...}, ...]
# }
```
