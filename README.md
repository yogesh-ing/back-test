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

## Modes

| Mode | What it does |
|------|-------------|
| **Backtest** | Run a strategy on historical data, see results |
| **Compare** | Run multiple strategies side-by-side on the same data |
| **Forward Test** | Paper-trade a strategy in simulated real-time — the replay clock runs on the server, so it keeps advancing with the tab closed. Equity *and* options: the forward engine's options bridge converts a strategy's directional view into multi-leg option structures on an isolated paper book |
| **Portfolio** | Run multiple strategies simultaneously under shared risk limits |
| **Portfolio (Live)** | Live-scoped command center — only real-money positions |
| **Portfolio (Paper)** | Paper sandbox — simulated fills, no risk |
| **Options** | Trade multi-leg NIFTY option structures (long call/put, bull call spread, bear put spread) with Greeks, fees, and expiry handling — paper or live |
| **Options Backtest** | *(Python API)* Model-driven options backtesting: the same expression layer (view → selector → structure → intent) run bar-by-bar over a candle frame with synthetic Black-Scholes pricing — no UI tab yet |
| **Dashboard** | Overview of all strategies and their status |

## Built-In Strategies

| Strategy | Logic |
|----------|-------|
| **Buy & Hold** | Buy once, hold forever — baseline benchmark |
| **SMA Crossover** | Buy when fast MA crosses above slow MA |
| **RSI Reversion** | Buy oversold, sell overbought (mean-reversion) |
| **Donchian Breakout** | Buy on new highs, sell on new lows (momentum) |
| **Price Move** | Buy/sell based on price movement threshold (e.g. ₹5) |
| **Directional Options** | EMA momentum → bullish/bearish `MarketView` — feeds the options expression layer (long call/put, spreads) |

## Options Trading (Paper, Live & Backtest)

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
- **Forward (automated):** the forward engine's options bridge trades
  structures on an isolated paper book, one open structure at a time;
  open structures persist across server restarts and are rehydrated on
  startup
- **Live:** `LiveOptionTrader(dry_run=True)` first — logs payloads, places nothing
- **Docs:** [docs/OPTIONS-PAPER-LIVE.md](docs/OPTIONS-PAPER-LIVE.md),
  [docs/OPTIONS-BACKTEST-PRD.md](docs/OPTIONS-BACKTEST-PRD.md)

**Status:** the options PRD is complete (9/9 phases) — instrument model,
expression layer, paper trading, live trading, Greeks & margin, the full
statutory fee stack, expiry handling, the `/options` dashboard, persistence,
forward-test wiring, and an end-to-end integration suite.

### Options Backtesting (model-driven)

The options expression layer now has a production backtest driver — the
same view → selector → structure path the paper/live books use, run
bar-by-bar over historical candles with deterministic synthetic pricing:

- **Deterministic by construction**: quotes are pinned to bar time
  (`set_reference`), structure/position IDs are monotonic counters, and
  identical runs produce byte-identical trade logs and equity curves
- **4 Phase-A structures**: long call, long put, bull call spread,
  bear put spread (the other four need a chain-shape refactor — Phase B)
- **Exits are labelled**: `auto_square_off`, `expiry_settlement`,
  `strategy_signal` — so trade logs explain *why* every position closed
- **All results are model results**: outputs carry the disclaimer that
  they price synthetic Black-Scholes, not historical market premiums

See [docs/OPTIONS-BACKTEST-PRD.md](docs/OPTIONS-BACKTEST-PRD.md) and the
task tracker [docs/OPTIONS-BACKTEST-TASKS.md](docs/OPTIONS-BACKTEST-TASKS.md).

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
| `trade_structures` | Durable options book — open/closed multi-leg structures with leg snapshots (survives restarts) |

## Project Structure

```
src/backtest/
├── data/           # Data sources (synthetic, csv, mstock)
├── strategy/       # Strategy base class + registry
├── strategies/     # Built-in strategies (SMA, RSI, Donchian, Buy&Hold, PriceMove, DirectionalOptions)
├── engine/         # Backtest engine (trade simulation, metrics, options backtest driver)
├── forward/        # Forward testing (paper trading)
├── simulator/      # Costs, slippage, fills, risk — incl. option fee stack
├── options/        # Options trading: selectors, structures, paper/live
│                   #   execution, Greeks, margin, fees, expiry, persistence
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

## Tests

```bash
cd src && python -m pytest ../tests/ -q          # full suite (2,100+ tests)
cd src && python -m pytest ../tests/ -q -k options   # options slice only
```

The options layer is Decimal-exact throughout, and the options backtest
path is deterministic by construction — the suite asserts byte-identical
results across identical runs.

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
