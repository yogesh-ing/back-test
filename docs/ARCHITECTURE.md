# Architecture Overview

## What This App Does

**Back-Test** is an algorithmic trading platform that simulates trading strategies against historical market data. It tells you how much money a strategy would have made (or lost) over any date range.

## Data Flow

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐     ┌──────────────┐
│ Data Source  │────>│   Strategy   │────>│   Engine    │────>│   Adapter    │
│ (OHLCV bars)│     │ (buy/sell)   │     │ (simulate)  │     │ (JSON ready) │
└─────────────┘     └──────────────┘     └─────────────┘     └──────────────┘
       │                                                            │
       │                                                            ▼
       │                                                     ┌──────────────┐
       │                                                     │   Web UI     │
       │                                                     │ (charts +    │
       │                                                     │  tables)     │
       │                                                     └──────────────┘
       │
       ├── SyntheticSource (fake random walk)
       ├── CsvSource (local CSV files)
       ├── MStockSource (live API, needs TOTP)
       └── DbSource (PostgreSQL, **NOT YET WIRED**)
```

## Directory Structure

```
src/backtest/
│
├── data/                    # DATA SOURCES
│   ├── base.py              # DataSource Protocol + normalize_candles()
│   ├── synthetic.py         # SyntheticSource - random walk candles
│   ├── csv_source.py        # CsvSource - reads local CSV files
│   └── (mstock.py is in live/)
│
├── strategy/                # STRATEGY SYSTEM
│   ├── base.py              # Strategy ABC - inherit from this
│   ├── registry.py          # Auto-discovery + get_strategy()
│   └── adapter.py           # Forward-test strategy adapter
│
├── strategies/              # BUILT-IN STRATEGIES (auto-discovered)
│   ├── buy_and_hold.py      # Buy once, hold forever
│   ├── sma_crossover.py     # Fast/slow MA crossover
│   ├── rsi_reversion.py     # RSI mean-reversion
│   └── donchian_breakout.py # Channel breakout
│
├── engine/                  # BACKTEST ENGINE
│   ├── backtester.py        # Backtester class - simulates trades
│   ├── metrics.py           # compute_metrics() - Sharpe, drawdown, etc.
│   └── plotting.py          # matplotlib chart generation
│
├── runner.py                # ORCHESTRATOR
│   ├── build_source()       # Maps "synthetic" -> SyntheticSource()
│   ├── run_on_candles()     # Strategy -> Engine -> Result
│   └── RunSpec              # Dataclass for CLI runs
│
├── adapters/                # API RESPONSE ADAPTERS
│   └── backtest_adapter.py  # BacktestResult -> JSON for UI
│
├── api/                     # FLASK API ENDPOINTS
│   ├── backtest.py          # POST /api/backtest/run
│   ├── strategies.py        # GET /api/strategies
│   ├── forward.py           # POST /api/forward/start, GET /api/forward/status
│   └── broker_auth.py       # POST /api/broker/login, /verify-totp
│
├── web/                     # FLASK WEB APP
│   ├── app.py               # App factory, --source flag, page routes
│   ├── templates/           # Jinja2 HTML templates
│   └── static/js/           # Frontend JavaScript
│
├── db/                      # DATABASE
│   ├── models.py            # SQLAlchemy ORM (10 tables)
│   ├── manager.py           # DatabaseManager - connection pool + retries
│   └── config.py            # FORWARD_TEST_DB_URL config
│
├── forward/                 # FORWARD TESTING (paper trading)
│   ├── engine.py            # ForwardTestEngine - bar-by-bar replay
│   ├── paper.py             # CLI paper-trade commands
│   ├── portfolio.py         # StrategyAccount - tracks positions
│   └── strategy_adapter.py  # Wraps Strategy for forward test loop
│
├── simulator/               # TRADE SIMULATION PRIMITIVES
│   ├── position.py          # Position tracking
│   ├── order.py             # Order lifecycle
│   ├── fill.py              # Fill execution
│   ├── execution.py         # Order routing
│   ├── commission.py        # Fee calculation
│   ├── slippage.py          # Slippage model
│   ├── position_sizing.py   # Kelly, fixed-fraction, etc.
│   └── risk_manager.py      # Stop-loss, take-profit
│
├── live/                    # LIVE MARKET INTEGRATION
│   ├── auth.py              # mStock login + TOTP verification
│   ├── mstock.py            # MStockSource - live candle fetcher
│   ├── preflight.py         # Pre-flight checks
│   └── time_manager.py      # Market hours detection
│
├── brokers/                 # BROKER INTEGRATION
│   ├── base.py              # Broker protocol
│   ├── mstock.py            # mStock broker adapter
│   └── session_manager.py   # Session lifecycle + expiry monitor
│
├── cli.py                   # CLI ENTRY POINT
│   ├── backtest list        # List strategies
│   ├── backtest run         # Run single backtest
│   ├── backtest compare     # Compare strategies
│   └── backtest papertrade  # Paper trade (walkforward/live)
│
└── runner.py                # SHARED ORCHESTRATION
```

## Entry Points

### 1. Web UI (primary)
```bash
PYTHONPATH=src python -m backtest.web.app --host 0.0.0.0 --port 5000 --source synthetic
```
Opens browser to `http://localhost:5000` with 4 tabs: Dashboard, Backtest, Compare, Forward.

### 2. CLI
```bash
PYTHONPATH=src python -m backtest run --strategy sma_crossover --symbol DEMO --from 2024-01-01 --to 2024-12-31 --source synthetic
```

### 3. Python API
```python
from backtest.runner import build_source, run_on_candles
from backtest.engine.backtester import BacktestConfig

source = build_source("synthetic")
candles = source.get_candles("DEMO", "2024-01-01", "2024-12-31", "day")
result = run_on_candles(candles, "sma_crossover", {"fast": 20, "slow": 50}, "DEMO", BacktestConfig())
print(result.metrics["sharpe"], result.metrics["total_return"])
```

## Key Interfaces

### DataSource Protocol (`data/base.py`)
```python
class DataSource(Protocol):
    def get_candles(self, symbol: str, start: str, end: str, interval: str = "day") -> pd.DataFrame:
        # Returns: DataFrame with columns [open, high, low, close, volume]
        # Index: DatetimeIndex
        # Sorted ascending, deduplicated, NaN-free
```

### Strategy Base (`strategy/base.py`)
```python
class Strategy(ABC):
    name: str           # Required. Unique identifier.
    description: str    # Shown in UI
    params: dict        # Schema for dynamic form rendering
    
    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        # Returns: Series of +1 (long), -1 (short), 0 (flat)
        # Same DatetimeIndex as candles
```

### BacktestResult (`engine/backtester.py`)
```python
@dataclass
class BacktestResult:
    equity: pd.Series      # Equity curve (dollar values)
    returns: pd.Series     # Period returns
    position: pd.Series    # Position state (+1, 0, -1)
    candles: pd.DataFrame  # Original OHLCV data
    config: BacktestConfig # Initial capital, commission, etc.
    metrics: dict          # Sharpe, drawdown, win_rate, etc.
```

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.11+, Flask |
| Database | PostgreSQL 18 + TimescaleDB 2.29 |
| Frontend | Vanilla JS, Chart.js |
| Data | pandas, numpy |
| ORM | SQLAlchemy 2.0 |
| Auth | mStock API (login + TOTP) |
