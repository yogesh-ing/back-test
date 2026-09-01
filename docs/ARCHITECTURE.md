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
       ├── MStockSource / MStockLiveFeed (live API, needs TOTP)
       └── DbSource (PostgreSQL market_data_cache)
```

## Directory Structure

```
src/backtest/
│
├── data/                    # DATA SOURCES
│   ├── base.py              # DataSource Protocol + normalize_candles()
│   ├── synthetic.py         # SyntheticSource - random walk candles
│   ├── csv_source.py        # CsvSource - reads local CSV files
│   ├── db_source.py         # DbSource - market_data_cache + resample
│   ├── mstock_live_feed.py  # MStockLiveFeed - real-time mStock bars
│   ├── source_registry.py   # (mode, source) -> DataSource factory (P1.2)
│   └── universe.py          # Symbol universes + correlation groups
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
│   ├── backtester.py        # Backtester class - vectorized quick-screen (legacy)
│   ├── backtest_driver.py   # BacktestDriver - canonical loop (P2.1)
│   ├── backtest_runner.py   # CANONICAL run_backtest() entry (ticket #6)
│   ├── metrics.py           # compute_metrics() - Sharpe, drawdown, etc.
│   └── plotting.py          # matplotlib chart generation
│
├── runner.py                # LEGACY VECTORIZED RUNNER + shared factories
│   ├── build_source()       # Maps "synthetic" -> SyntheticSource()
│   ├── run_on_candles()     # Strategy -> Engine -> Result (quick screen)
│   ├── run_on_source()      # fetch + run_on_candles (CLI legacy path)
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
│   ├── engine.py            # ForwardTestingEngine - live loop + backtest replay
│   ├── paper_runner.py      # PaperRunner (canonical loop), CLI walk-forward/live
│   │                        #   paper trade, StrategyRunner/OrderLedger/PaperBroker
│   ├── portfolio_manager.py # PortfolioManager (multi-strategy command center)
│   ├── risk_supervisor.py   # Global daily-loss / drawdown circuit breakers
│   ├── feed.py              # SyntheticFeed - 1s random-walk bars
│   └── strategy_adapter.py  # Strategy → Signal → Order (NO fills, F-01)
│
├── simulator/               # TRADE SIMULATION PRIMITIVES
│   ├── engine_loop.py       # canonical bar-clock loop (submit → fill at next open)
│   ├── position.py          # Position tracking
│   ├── order.py             # Order lifecycle
│   ├── fill.py              # Fill execution
│   ├── execution.py         # OrderExecutor - submit()/step() bar clock + execute()
│   ├── fill_providers.py    # simulated vs broker fill seam (P3.3)
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
├── logging_config.py         # CROSS-CUTTING: handlers, levels, request ids
│                             #   configure_logging / get_logger / timed()
│                             #   (docs/LOGGING.md)
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

### 3. Python API — canonical backtest entry (`engine/backtest_runner`)
```python
from backtest.data.synthetic import SyntheticSource
from backtest.engine.backtest_runner import run_backtest

candles = SyntheticSource().get_candles("DEMO", "2024-01-01", "2024-12-31", "day")
result = run_backtest(candles, "sma_crossover", {"fast": 20, "slow": 50}, "DEMO", 100_000)
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
| Logging | stdlib `logging`, configured in `logging_config.py` |
| Auth | mStock API (login + TOTP) |
