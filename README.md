# Back-Test

An algorithmic trading platform for backtesting, comparing, and paper-trading investment strategies against historical market data.

## What It Does

**Test trading strategies before risking real money.** Feed it historical OHLCV (Open/High/Low/Close/Volume) candle data, pick a strategy, and the engine simulates trades — showing you exactly how much you would have made or lost.

## How It Works

```
Market Data (OHLCV candles)
        │
        ▼
┌─────────────────┐
│   Strategy       │  Generates buy/sell signals
│   (pluggable)    │  based on technical indicators
└────────┬────────┘
         │  signals: +1 (buy), -1 (sell), 0 (hold)
         ▼
┌─────────────────┐
│   Backtest       │  Simulates trades with position
│   Engine         │  sizing, stop-loss, take-profit
└────────┬────────┘
         │  trades, equity curve, metrics
         ▼
┌─────────────────┐
│   Results        │  Charts, trade tables, metrics
│   (Web UI)       │  Sharpe, drawdown, win rate, P&L
└─────────────────┘
```

## Five Modes

| Mode | What it does |
|------|-------------|
| **Backtest** | Run a strategy on historical data, see results |
| **Compare** | Run multiple strategies side-by-side on the same data |
| **Forward Test** | Paper-trade a strategy in simulated real-time — the replay clock runs on the server, so it keeps advancing with the tab closed |
| **Portfolio** | Run multiple strategies simultaneously under shared risk limits |
| **Portfolio (Live)** | Live-scoped command center — only real-money positions |
| **Portfolio (Paper)** | Paper sandbox — simulated fills, no risk |
| **Options** | Trade multi-leg NIFTY option structures (long call/put, bull call spread, bear put spread) with Greeks, fees, and expiry handling — paper or live |
| **Dashboard** | Overview of all strategies and their status |

## Built-In Strategies

| Strategy | Logic |
|----------|-------|
| **Buy & Hold** | Buy once, hold forever — baseline benchmark |
| **SMA Crossover** | Buy when fast MA crosses above slow MA |
| **RSI Reversion** | Buy oversold, sell overbought (mean-reversion) |
| **Donchian Breakout** | Buy on new highs, sell on new lows (momentum) |
| **Price Move** | Buy/sell based on price movement threshold (e.g. ₹5) |

## Options Trading (Paper & Live)

Trade NIFTY/BANKNIFTY **index options** through the same pipeline: a
strategy's directional view is converted into a multi-leg option structure
(long call/put, bull call spread, bear put spread), executed **atomically**
(all legs fill or none), tracked with portfolio Greeks, priced with the full
Indian statutory fee stack (STT, exchange, SEBI, stamp, GST — ₹20/order
brokerage), and auto-squared-off before expiry with cash settlement.

```
MarketView (bullish/bearish) → strike + expiry selection → TradeIntent
    → OptionPaperBroker (paper, atomic)  or  LiveOptionTrader (mStock, rollback)
```

- **Paper:** `/options` dashboard — positions, structures, Greeks grid, expiry alerts
- **Live:** `LiveOptionTrader(dry_run=True)` first — logs payloads, places nothing
- **Docs:** [docs/OPTIONS-PAPER-LIVE.md](docs/OPTIONS-PAPER-LIVE.md)

**Status:** the options PRD is complete (9/9 phases) — instrument model,
expression layer, paper trading, live trading, Greeks & margin, the full
statutory fee stack, expiry handling, the `/options` dashboard, and an
end-to-end integration suite. 226 options tests across 8 modules plus 52
instrument-model tests.

## Data Sources

| Source | Description |
|--------|-------------|
| **Synthetic** | Random-walk generated candles — no API needed |
| **CSV** | Read from local `data/*.csv` files |
| **mStock** | Real market data from mStock API (requires auth + TOTP) |
| **PostgreSQL** | *(in progress)* DB-first cache of real market data |

## Database (PostgreSQL + TimescaleDB)

Real market data for **201 NIFTY 200 stocks** (467K+ daily bars, Jan 2020 – Aug 2026) stored in a TimescaleDB hypertable for fast time-range queries.

### Key Tables

| Table | Purpose |
|-------|---------|
| `market_data_cache` | OHLCV candle data (hypertable, partitioned by time) |
| `instruments` | 154K instruments from mStock (NSE, BSE, NFO, CDS) |
| `portfolios` | Forward-test portfolio snapshots |
| `trades` | Matched round-trip trades |
| `equity_curve` | Mark-to-market equity snapshots |
| `strategy_signals` | Audit log of every signal generated |

## Project Structure

```
src/backtest/
├── data/           # Data sources (synthetic, csv, mstock)
├── strategy/       # Strategy base class + registry
├── strategies/     # Built-in strategies (SMA, RSI, Donchian, Buy&Hold)
├── engine/         # Backtest engine (trade simulation, metrics)
├── forward/        # Forward testing (paper trading)
├── simulator/      # Costs, slippage, fills, risk — incl. option fee stack
├── options/        # Options trading: selectors, structures, paper/live
│                   #   execution, Greeks, margin, fees, expiry
├── instruments/    # Instrument model (equity, option, expiry calendar)
├── db/             # SQLAlchemy models + DB manager
├── web/            # Flask web app (UI + API)
├── live/           # mStock live auth + data adapter
├── cli.py          # Command-line interface
└── runner.py       # Orchestrates data → strategy → engine → results
```

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run with synthetic data (no API needed)
PYTHONPATH=src python -m backtest.web.app --host 0.0.0.0 --port 5000 --source synthetic

# Run with real data from PostgreSQL
PYTHONPATH=src python -m backtest.web.app --host 0.0.0.0 --port 5000 --source db

# Useful switches (all also available as env vars — see .env.example)
#   --log-level DEBUG      full trace of every request, with request ids
#   --log-file logs/app.log
#   --currency USD          money display for every page (default: INR / ₹)
#   --replay-speed 5        forward-test clock: bars revealed per second
```

Open `http://localhost:5000` → Backtest tab → Pick a strategy → Hit **Run Backtest**.

## Debugging

Nothing is silent any more: every request gets an id, and every `/api` error
quotes it in the response so the toast, the log line and the traceback all match.

```bash
PYTHONPATH=src python -m backtest.web.app --source synthetic --log-level DEBUG   # web app
PYTHONPATH=src python -m backtest run --strategy sma_crossover \
    --symbol DEMO --from 2024-01-01 --to 2024-12-31 --log-level DEBUG            # CLI
```

Levels (`BACKTEST_LOG_LEVEL`) and file output (`--log-file logs/app.log`) are
documented in **[docs/LOGGING.md](docs/LOGGING.md)**, along with a
symptom→what-the-log-says table for the usual suspects (empty results,
0 trades, card/table mismatches, 403 on Forward Start).
