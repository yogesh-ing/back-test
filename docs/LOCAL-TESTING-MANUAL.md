# Local Testing Manual — Forward Testing Simulator (Phase 4)

This manual explains how to test the forward testing simulator **locally on your machine** without pushing secrets to git. It covers mock-only testing (no credentials) and optional live testing with mStock.

---

## 1. Prerequisites

```bash
# Clone repo
git clone https://github.com/yogesh-ing/back-test.git
cd back-test

# Create venv (recommended)
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# .venv\Scripts\activate   # Windows

# Install deps
pip install -r requirements.txt

# Verify PYTHONPATH
export PYTHONPATH=src
```

**Required packages:** `pandas`, `numpy`, `requests`, `python-dotenv`, `SQLAlchemy`, `PyYAML`, `pytest`, `matplotlib`, `pyarrow`, `psycopg2-binary` (for PostgreSQL, optional for SQLite).

---

## 2. Mock-Only Testing (No Credentials Needed) ✅ Recommended Start

This is how Phase 4 (Steps 10-12) is tested in CI. No network, no API keys.

### Run all tests

```bash
PYTHONPATH=src pytest tests/ -q
# Expected: 972 passed, 4 skipped (skipped = need mStock creds)
```

### Run Phase 4 only

```bash
PYTHONPATH=src pytest tests/test_market_data_handler.py -q
PYTHONPATH=src pytest tests/test_data_validator.py -q
PYTHONPATH=src pytest tests/test_time_manager.py -q
PYTHONPATH=src pytest tests/test_forward_engine.py -q -k "not live"
```

### What mock data is used?

**No external files needed.** Mock data is generated in-memory:

- **MockBrokerFeed** (`src/backtest/live/market_data_handler.py`): In-memory feed that you inject ticks/bars into via `inject_tick()` / `inject_bar()`. No network.
- **SyntheticDataSource** (`src/backtest/data/synthetic.py`): Generates random walk OHLCV
- **CSV Source** (`src/backtest/data/csv_source.py`): Replays CSV files if you have them
- **BarBuilder**: Aggregates ticks into bars (1min, 3min, 5min, 15min, 30min, 1hr, 1day) with alignment to timeframe boundaries

Example from tests:

```python
from backtest.live.market_data_handler import MarketDataHandler

handler = MarketDataHandler(symbols=["INFY"], provider="mock")
handler.connect()
handler.subscribe_symbols(["RELIANCE"])

# Observer pattern
handler.on_tick_received(lambda tick: print(f"Tick: {tick}"))
handler.on_bar_closed(lambda bar: print(f"Bar closed: {bar}"))

# Inject tick as if from broker
handler.inject_tick({
    "symbol": "INFY",
    "bid": 1499,
    "ask": 1501,
    "last": 1500,
    "volume": 100,
    "timestamp": "2024-01-02T09:15:00+05:30"
})
# After enough ticks, a 1min bar will be built and on_bar_closed fires
```

### Test data quality validator with corrupted data

```python
from backtest.live.data_validator import DataValidator

validator = DataValidator(config={"strictness":"normal"})

# Valid bar
assert validator.validate_bar({"symbol":"INFY","open":100,"high":101,"low":99,"close":100,"volume":1000}).valid

# Invalid: high < low
result = validator.validate_bar({"symbol":"INFY","open":100,"high":98,"low":99,"close":100,"volume":1000})
assert not result.valid
print(result.code, result.reason)  # ohlc_high_low

# Spike detection
validator = DataValidator()
for price in [100,101,100,101,100,101,100,101,100,101]:
    validator._price_history["INFY"].append(price)

result = validator.check_for_spikes(200, "INFY", threshold=3.0)
assert not result.valid  # spike
```

### Test time manager (NSE calendar)

```python
from backtest.live.time_manager import TimeManager
from zoneinfo import ZoneInfo
from datetime import datetime

tm = TimeManager(market="NSE")  # 09:15-15:30 IST

# Market open check
dt = datetime(2024, 1, 2, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
assert tm.is_market_open(dt) == True

dt = datetime(2024, 1, 2, 8, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
assert tm.is_market_open(dt) == False

# Weekend
dt = datetime(2024, 1, 6, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))  # Saturday
assert tm.is_market_open(dt) == False

# Bar alignment
aligned = tm.align_to_timeframe("2024-01-02T09:17:32+05:30", "5min")
print(aligned)  # 09:15

# Mock time for testing
from datetime import timedelta
tm.set_mock_time(datetime(2024, 1, 2, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata")))
print(tm.get_current_time())  # Returns mock time
tm.advance_mock_time(timedelta(minutes=5))
```

---

## 3. Optional Live Testing with mStock (Requires .env)

If you have mStock API credentials, you can test real data fetching.

### Where and how to provide .env

**Location:** Repo root, same folder as `.env.example`

```bash
cp .env.example .env
chmod 600 .env
```

**Edit `.env`:**

```ini
# mStock TypeA API – get from https://api.mstock.trade or Mirae dashboard
MSTOCK_API_KEY=your_api_key
MSTOCK_USERNAME=your_client_id (e.g. AB1234)
MSTOCK_PASSWORD=your_password
MSTOCK_CHECKSUM=W
MSTOCK_AUTH_MODE=otp
MSTOCK_BASE_URL=https://api.mstock.trade

# Optional: if you already have a valid token, cache it to skip OTP
# The auth module caches token in .mstock_session_token file
# echo "your_token" > .mstock_session_token

# DB – for local dev, SQLite is zero-setup
FORWARD_TEST_DB_URL=sqlite:///forward_test.db
FORWARD_TEST_DB_PROFILE=development
```

**Security:**
- `.env` is in `.gitignore` – `git status` will NOT show it as to-be-committed, and `git push` will NOT upload it
- Never paste tokens in chat or commit them
- For GitHub Actions, use Repo → Settings → Secrets → Actions instead of file

**How it works:**
- `src/backtest/live/auth.py` reads `MSTOCK_USERNAME/PASSWORD`, calls `/openapi/typea/connect/login`, then asks for OTP/TOTP
- OTP mode: set `MSTOCK_OTP` env or enter when prompted
- TOTP mode: set `MSTOCK_AUTH_MODE=totp` and `MSTOCK_TOTP` or enter from authenticator app
- Token is cached in `.mstock_session_token` (also gitignored) to avoid re-auth every run

### Run live integration tests (will be skipped without .env)

```bash
# These tests are skipped if no creds
PYTHONPATH=src pytest tests/test_mstock_auth.py -s
PYTHONPATH=src pytest tests/test_mstock_data.py -s
PYTHONPATH=src pytest tests/test_mstock_live_integration.py -s
```

### Manual live data fetch

```python
from backtest.live.mstock import MStockSource

source = MStockSource()
# Fetch 1min bars for last 5 days
df = source.get_candles("INFY", "2024-01-01", "2024-01-05", interval="1min")
print(df.head())
print(df.tail())

# Now test with MarketDataHandler wired to mStock
from backtest.live.market_data_handler import MarketDataHandler

handler = MarketDataHandler(symbols=["INFY"], provider="mstock")
handler.connect()
handler.subscribe_symbols(["INFY"])

# Observer
handler.on_bar_closed(lambda bar: print(f"Live bar: {bar}"))

# Fetch historical and inject as if live
for idx, row in df.iterrows():
    bar = {
        "symbol": "INFY",
        "timestamp": idx,
        "open": row["open"],
        "high": row["high"],
        "low": row["low"],
        "close": row["close"],
        "volume": row["volume"]
    }
    handler.inject_bar(bar)
```

### Run engine with real data (backtest replay mode)

```bash
# Uses MStockSource + ForwardTestingEngine in backtest_mode
PYTHONPATH=src python -m backtest.forward.engine --config config/forward_testing.yaml --backtest --symbols INFY --dry-run
```

Or via Python:

```python
from backtest.forward.engine import ForwardTestingEngine
from backtest.live.mstock import MStockSource

engine = ForwardTestingEngine(
    config_file="config/forward_testing.yaml",
    config_dict={
        "data": {"symbols":["INFY"], "provider":"mstock", "start_date":"2024-01-01", "end_date":"2024-01-10"},
        "system": {"backtest_mode": True, "dry_run": True, "loop_interval_seconds": 0}
    },
    data_source=MStockSource()
)
engine.initialize_system()
engine.start()  # Replays historical bars
print(engine.get_status())
```

---

## 4. Testing the Full Forward Loop Locally

### Dry-run mode (no real orders)

```bash
# Config already has dry_run: false, override via CLI
PYTHONPATH=src python -m backtest.forward.engine --dry-run --symbols INFY TCS
```

### Backtest mode with mock data (no creds)

```bash
PYTHONPATH=src python - << 'PY'
from backtest.forward.engine import ForwardTestingEngine
from backtest.simulator.portfolio import Portfolio
import pandas as pd

class MockDataSource:
    def get_candles(self, symbol, start, end, interval="day"):
        import numpy as np
        dates = pd.date_range(start, end, freq='D', tz='UTC')[:20]
        close = 100 + np.cumsum(np.random.randn(len(dates)))
        return pd.DataFrame({
            "open": close, "high": close+1, "low": close-1, "close": close, "volume": 1000
        }, index=dates)

engine = ForwardTestingEngine(
    config_dict={
        "portfolio": {"name":"LocalTest","initial_capital":100000},
        "strategy": {"name":"sma_crossover","parameters":{"fast":2,"slow":3}},
        "data": {"symbols":["INFY"],"start_date":"2024-01-01","end_date":"2024-01-20"},
        "system": {"backtest_mode":True,"loop_interval_seconds":0,"state_file":"/tmp/local_state.json"}
    },
    data_source=MockDataSource()
)
engine.initialize_system()
engine.adapter.min_bars = 2
engine._running = True
engine._run_backtest_mode()
print(f"Loops: {engine._loop_count}, Equity: {engine.portfolio.calculate_total_equity()}, Signals: {len(engine.adapter.signal_history)}")
PY
```

### Check state recovery

```bash
ls -lh state/
cat state/forward_test_state.json | head -n 50
# Delete and restart – engine should restore from state if file exists
```

---

## 5. Docker Local Testing (Optional)

```bash
docker build -t forward-test .
docker run --env-file .env -v $(pwd)/state:/app/state forward-test
# For dry-run:
docker run --env-file .env -v $(pwd)/state:/app/state forward-test python -m backtest.forward.engine --dry-run
```

---

## 6. Troubleshooting

| Issue | Fix |
|---|---|
| `ModuleNotFoundError: backtest` | `export PYTHONPATH=src` |
| `ModuleNotFoundError: dotenv` | `pip install -r requirements.txt` |
| `No module named 'sqlalchemy'` | `pip install sqlalchemy PyYAML` |
| `FOREIGN KEY constraint failed` on signals | Engine auto-creates portfolio row – ensure DB file exists or use `sqlite:///:memory:` |
| `mStock auth failed` | Check `.env` values, check OTP, delete `.mstock_session_token` to force re-auth |
| `Market is closed` | TimeManager checks NSE 09:15-15:30 IST, weekends, holidays – use mock time for testing |
| `No bars returned` | mStock historical endpoint needs valid trading days – try `interval="day"` and wider date range |

---

## 7. What I Need From You for Phase 4

**For mock-only (current):** Nothing – I already built it with in-memory mock feeds, and all tests pass without creds.

**For live verification (optional, when you want):**
1. Create `.env` in repo root as shown above
2. Run `PYTHONPATH=src pytest tests/test_mstock_live_integration.py -s` – share the output (redact token)
3. If it works, I can run engine with real data in this sandbox too

**Security reminder:** `.env` is gitignored – it will NOT be pushed to GitHub. In Arena sandbox, it persists in workspace snapshot but not in git. For production, use env vars or secret manager, not file.

---

## 8. Quick Commands Cheat Sheet

```bash
# Mock-only full suite
PYTHONPATH=src pytest tests/ -q

# Phase 4 only
PYTHONPATH=src pytest tests/test_market_data_handler.py tests/test_data_validator.py tests/test_time_manager.py -v

# With live mStock (requires .env)
PYTHONPATH=src pytest tests/test_mstock_live_integration.py -s
PYTHONPATH=src python -m backtest.live.mstock  # if you add a main

# Engine
PYTHONPATH=src python -m backtest.forward.engine --dry-run
PYTHONPATH=src python -m backtest.forward.engine --backtest --symbols INFY --dry-run
PYTHONPATH=src python -m backtest.forward.engine --config config/forward_testing.yaml

# State
ls state/
cat state/forward_test_state.json | jq .portfolio.name
```
