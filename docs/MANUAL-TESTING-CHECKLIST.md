# Manual Testing Checklist — Happy Path (Windows + Real Broker API)

> **Roles:** Python QA Engineer + Windows DevOps + PostgreSQL DBA
> **Stack:** Python Backend, Windows Direct PostgreSQL, .env for DB + Broker API, Real mStock Credentials
> **Goal:** Verify app reads/writes correctly to local Windows PostgreSQL and communicates with broker API

---

## Pre-Flight (5 mins)

### 1. Verify .env

```powershell
cd C:\Users\YourName\back-test
cat .env | findstr -v "PASSWORD\|TOKEN\|SECRET"
# Should show FORWARD_TEST_DB_URL, MSTOCK_API_KEY, TELEGRAM_BOT_TOKEN etc.
# Ensure no real secrets printed in logs – check that sensitive values are "***" in safe logs

# Check .env is ignored
git check-ignore -v .env
# Expected: .gitignore:.env
```

**Expected `.env` content (redacted):**
```ini
FORWARD_TEST_DB_URL=postgresql+psycopg2://postgres:****@localhost:5432/forward_test
MSTOCK_API_KEY=****
MSTOCK_USERNAME=AB1234
TELEGRAM_BOT_TOKEN=****
TELEGRAM_CHAT_ID=123456789
```

### 2. Verify DB Connection

```powershell
$env:PGPASSWORD="your_postgres_password"
psql -U postgres -h localhost -d forward_test -c "SELECT count(*) FROM portfolios; SELECT * FROM schema_migrations;"
# Expected: count may be 0 initially, schema_migrations should have version 001
```

### 3. Verify Python Env

```powershell
.\.venv\Scripts\Activate.ps1
$env:PYTHONPATH="src"
python -c "from backtest.db.manager import DatabaseManager; db=DatabaseManager.from_env(); print(db.health_check())"
# Expected: {"healthy": True, "dialect": "postgresql", "latency_ms": <100}
```

---

## Happy Path — 10 Steps

### Step 1 – Create/Load Portfolio (Write to DB)

```powershell
python - << 'PY'
from backtest.db.config import DatabaseConfig
from backtest.db.manager import DatabaseManager
from backtest.db.models import Base
from backtest.simulator.portfolio import Portfolio

cfg = DatabaseConfig(url="postgresql+psycopg2://postgres:your_password@localhost:5432/forward_test")
db = DatabaseManager(cfg)
db.connect()
Base.metadata.create_all(db.engine)

portfolio = Portfolio(name="ManualTestWin", initial_capital=100000)
portfolio.save_to_db(db)
print(f"Portfolio saved: {portfolio.portfolio_id} {portfolio.name}")

# Load back
loaded = Portfolio.load_from_db(db, portfolio.portfolio_id)
print(f"Loaded: {loaded.name} cash={loaded.current_cash}")

db.disconnect()
PY
```

**Verify in psql:**
```powershell
psql -U postgres -h localhost -d forward_test -c "SELECT portfolio_id, name, initial_capital, current_cash, status FROM portfolios WHERE name='ManualTestWin';"
# Expected: 1 row, name ManualTestWin, 100000, active
```

**✅ Pass criteria:** Portfolio row exists, `current_cash` = 100000, no FK errors.

---

### Step 2 – Test Market Data Handler (Mock-Only, No Broker)

```powershell
python - << 'PY'
from backtest.live.market_data_handler import MarketDataHandler

handler = MarketDataHandler(symbols=["INFY"], provider="mock")
handler.connect()
handler.subscribe_symbols(["INFY"])

ticks = []
handler.on_tick_received(lambda t: ticks.append(t))

handler.inject_tick({"symbol":"INFY","bid":1499,"ask":1501,"last":1500,"volume":100,"timestamp":"2024-01-02T09:15:10+05:30"})
handler.inject_tick({"symbol":"INFY","last":1501,"volume":50,"timestamp":"2024-01-02T09:15:20+05:30"})
handler.inject_tick({"symbol":"INFY","last":1502,"volume":20,"timestamp":"2024-01-02T09:16:10+05:30"})

print(f"Ticks received: {len(ticks)}")
print(f"Current quote: {handler.get_current_quote('INFY')}")
print(f"Current bar 1min: {handler.get_current_bar('INFY','1min')}")
print(f"Stats: {handler.get_stats()}")
PY
```

**✅ Pass criteria:** Ticks received =2 (first 2 same minute), 1 bar closed on minute boundary (open 1500 close 1501 vol 150), buffers bounded.

---

### Step 3 – Test Data Validator (Corrupted Data)

```powershell
python - << 'PY'
from backtest.live.data_validator import DataValidator

validator = DataValidator(config={"strictness":"normal"})

# Valid
result = validator.validate_bar({"symbol":"INFY","open":100,"high":101,"low":99,"close":100,"volume":1000})
print(f"Valid bar: {result.valid} {result.code}")

# Invalid OHLC
result = validator.validate_bar({"symbol":"INFY","open":100,"high":98,"low":99,"close":100,"volume":1000})
print(f"Invalid high<low: {result.valid} {result.code} {result.reason}")

# Spike
for p in [100,101,100,101,100,101,100,101,100,101]:
    validator._price_history["INFY"].append(p)
result = validator.check_for_spikes(200, "INFY")
print(f"Spike 200 vs 100 avg: {result.valid} {result.code}")

print(f"Stats: {validator.get_stats()}")
PY
```

**✅ Pass criteria:** Valid passes, invalid fails with code `ohlc_high_low`, spike fails with `price_spike`, stats show failure rate.

---

### Step 4 – Test Time Manager (NSE Calendar)

```powershell
python - << 'PY'
from backtest.live.time_manager import TimeManager
from datetime import datetime
from zoneinfo import ZoneInfo

tm = TimeManager(market="NSE")

# Tuesday 10:00 IST open
dt = datetime(2024, 1, 2, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
print(f"Tue 10:00 open? {tm.is_market_open(dt)}")  # True

# Saturday closed
dt = datetime(2024, 1, 6, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
print(f"Sat 10:00 open? {tm.is_market_open(dt)}")  # False

# Bar alignment 09:17:32 -> 09:15 for 5min
aligned = tm.align_to_timeframe("2024-01-02T09:17:32+05:30", "5min")
print(f"Aligned 09:17:32 to 5min: {aligned}")

# Next open after Tue 16:00 -> Wed 09:15
next_open = tm.get_next_market_open(datetime(2024, 1, 2, 16, 0, tzinfo=ZoneInfo("Asia/Kolkata")))
print(f"Next open after Tue 16:00: {next_open}")
PY
```

**✅ Pass criteria:** Weekday 10:00 True, weekend False, alignment 09:15, next open Wed 09:15.

---

### Step 5 – Test Strategy Adapter (No Lookahead)

```powershell
python - << 'PY'
from backtest.strategy.registry import get_strategy
from backtest.simulator.portfolio import Portfolio
from backtest.forward.strategy_adapter import StrategyAdapter

Sma = get_strategy("sma_crossover")
strat = Sma(fast=2, slow=3)
portfolio = Portfolio(name="AdapterTest", initial_capital=100000)
adapter = StrategyAdapter(strategy=strat, portfolio=portfolio, symbols=["INFY"], min_bars=3)

bars = [
    {"symbol":"INFY","timestamp":"2024-01-01T09:15:00+05:30","open":100,"high":101,"low":99,"close":100,"volume":1000},
    {"symbol":"INFY","timestamp":"2024-01-02T09:15:00+05:30","open":101,"high":102,"low":100,"close":101,"volume":1000},
    {"symbol":"INFY","timestamp":"2024-01-03T09:15:00+05:30","open":102,"high":103,"low":101,"close":102,"volume":1000},
]

for bar in bars:
    sigs = adapter.on_bar_close(bar)
    print(f"Bar {bar['close']} -> signals: {len(sigs)} action={sigs[0].action if sigs else 'none'}")

# Check no lookahead: bar_ts < generated_at
for sig in adapter.signal_history:
    assert sig.bar_ts < sig.generated_at
print(f"No lookahead verified for {len(adapter.signal_history)} signals")

print(f"Orders: {len(adapter.order_history)}")
PY
```

**✅ Pass criteria:** First 2 bars no signals (min_bars), 3rd bar BUY signal, `bar_ts < generated_at` for all, order created.

---

### Step 6 – Test Position Sizing (6 Methods)

```powershell
python - << 'PY'
from backtest.simulator.position_sizing import PositionSizer, SizingConfig, SizingConstraints
from backtest.simulator.portfolio import Portfolio

portfolio = Portfolio(name="SizingTest", initial_capital=100000)

tests = [
    ("fixed_quantity", {"fixed_quantity": 100}, 100),
    ("fixed_dollar", {"fixed_dollar_amount": 10000}, 100),
    ("percentage_portfolio", {"percentage": 0.05}, 50),
    ("risk_based", {"risk_per_trade": 0.01, "stop_loss_pct": 0.02}, 500),
]

for method, params, expected in tests:
    cfg = SizingConfig(method=method, **params)
    sizer = PositionSizer(cfg)
    qty = sizer.calculate_position_size(symbol="INFY", current_price=100, portfolio=portfolio)
    print(f"{method} @100: {qty} expected {expected} {'✅' if int(qty)==expected else '❌'}")

# Kelly
cfg = SizingConfig(method="kelly", win_rate=0.55, avg_win=150, avg_loss=100, kelly_fraction=0.5)
sizer = PositionSizer(cfg)
qty = sizer.calculate_position_size(symbol="INFY", current_price=100, portfolio=portfolio)
print(f"kelly: {qty} expected 125")

# Constraints: max 10% cap
cfg = SizingConfig(method="fixed_quantity", fixed_quantity=1000, constraints=SizingConstraints(max_position_pct=0.1))
sizer = PositionSizer(cfg)
qty = sizer.calculate_position_size(symbol="INFY", current_price=100, portfolio=portfolio)
print(f"Constrained to 10%: {qty} expected 100")
PY
```

**✅ Pass criteria:** All methods return expected qty, constraints cap correctly.

---

### Step 7 – Test Risk Manager (Circuit Breakers)

```powershell
python - << 'PY'
from backtest.simulator.portfolio import Portfolio
from backtest.simulator.risk_manager import RiskManager, RiskConfig
from backtest.simulator.order import Order

portfolio = Portfolio(name="RiskTest", initial_capital=100000)
risk = RiskManager(portfolio, RiskConfig(restricted_symbols={"BAD"}, max_drawdown_pct=0.1))

# Restricted symbol should be rejected
order = Order(symbol="BAD", side="buy", quantity=10, order_type="market")
order.submit()
result = risk.validate_order(order, current_price=100)
print(f"BAD symbol allowed? {result.allowed} code={result.code}")  # False, restricted_symbol

# Valid should pass
order2 = Order(symbol="INFY", side="buy", quantity=10, order_type="market")
order2.submit()
print(f"INFY allowed? {risk.validate_order(order2, current_price=100).allowed}")  # True

# Emergency stop
risk.emergency_stop_all("Manual test emergency")
print(f"Halted? {risk.is_halted()}")  # True
print(f"New order after halt allowed? {risk.validate_order(order2, current_price=100).allowed}")  # False

# Override
risk.config.allow_override = True
risk.config.override_code = "SECRET123"
print(f"Override with wrong code: {risk.override('WRONG')}")  # False
print(f"Override with correct: {risk.override('SECRET123')}")  # True
print(f"Halted after override? {risk.is_halted()}")  # False
PY
```

**✅ Pass criteria:** Restricted rejected, valid passes, halt blocks new orders, override resumes.

---

### Step 8 – Test Stop Manager (Trailing + OCO)

```powershell
python - << 'PY'
from backtest.simulator.portfolio import Portfolio, PortfolioLimits
from backtest.simulator.stop_manager import StopManager

portfolio = Portfolio(name="StopTest", initial_capital=100000, limits=PortfolioLimits(allow_short=True))
pos = portfolio.open_position("INFY", 100, 100)
manager = StopManager(portfolio)

# Add 2% SL and 5% TP as OCO
sl = manager.add_stop_loss(pos, stop_type="percentage", params={"pct":0.02, "oco_group":"exit1"})
tp = manager.add_take_profit(pos, target_type="percentage", params={"pct":0.05, "oco_group":"exit1"})
print(f"SL @ {sl.price} TP @ {tp.price} OCO group {sl.oco_group}")

# Price drops to 97 – SL should trigger, TP cancelled via OCO
hits = manager.check_stops({"INFY": {"close":97,"low":97,"high":101}})
print(f"Hits when price 97: {len(hits)} type={hits[0].stop_type if hits else 'none'}")
print(f"Active stops after SL hit: {len(manager.get_active_stops('INFY'))}")  # 0 due to OCO

# Trailing test
portfolio2 = Portfolio(name="TrailingTest", initial_capital=100000)
pos2 = portfolio2.open_position("INFY", 100, 100)
manager2 = StopManager(portfolio2)
trailing = manager2.add_stop_loss(pos2, stop_type="trailing_fixed", params={"trailing_amount":2})
print(f"Trailing initial @ {trailing.price}")  # 98
updated = manager2.update_trailing_stops({"INFY":105})
print(f"After price 105, trailing -> {trailing.price}")  # 103
updated2 = manager2.update_trailing_stops({"INFY":102})
print(f"After price down to 102, trailing stays @ {trailing.price} (ratchet one-way)")  # still 103
PY
```

**✅ Pass criteria:** SL 98, TP 105, OCO cancels other when one triggers, trailing moves up only (98→103) and stays.

---

### Step 9 – Test Performance & Trade Analyzer (Write to DB)

```powershell
python - << 'PY'
from backtest.db.config import DatabaseConfig
from backtest.db.manager import DatabaseManager
from backtest.db.models import Base
from backtest.simulator.portfolio import Portfolio
from backtest.simulator.performance import PerformanceCalculator
from backtest.simulator.trade_analyzer import TradeAnalyzer

cfg = DatabaseConfig(url="postgresql+psycopg2://postgres:your_password@localhost:5432/forward_test")
db = DatabaseManager(cfg)
db.connect()
Base.metadata.create_all(db.engine)

portfolio = Portfolio(name="PerfTest", initial_capital=100000)
# Simulate some equity history
for eq in [100000, 101000, 100500, 102000]:
    portfolio.current_cash = eq
    portfolio.record_equity()

# Simulate closed trades
pos = portfolio.open_position("INFY", 100, 100)
portfolio.reduce_position("INFY", 100, 110)

calc = PerformanceCalculator(portfolio=portfolio, db_manager=db)
metrics = calc.calculate_all_metrics()
print(f"Total return %: {metrics['total_return_pct']*100:.2f}%")
print(f"Sharpe: {metrics['sharpe_ratio']:.2f}")
print(f"Max DD %: {metrics['max_drawdown_pct']*100:.2f}%")
print(f"Trades: {metrics['total_trades']}")

# Save to DB
metric_id = calc.save_to_db()
print(f"Metrics saved to DB id={metric_id}")

# Trade analyzer
analyzer = TradeAnalyzer(portfolio=portfolio)
report = analyzer.generate_trade_report()
print(f"Trade report: {report['total_trades']} trades, win rate {report['win_rate']*100:.1f}%")

# Export
import tempfile
from pathlib import Path
tmp = Path(tempfile.gettempdir()) / "trades_test.csv"
path = analyzer.export_trades(format="csv", file_path=tmp)
print(f"Trades exported to {path}")

db.disconnect()
PY
```

**Verify in psql:**
```powershell
psql -U postgres -h localhost -d forward_test -c "SELECT portfolio_id, calculation_date, total_trades, win_rate, sharpe_ratio FROM performance_metrics ORDER BY calculated_at DESC LIMIT 1;"
# Expected: 1 row, total_trades 1, win_rate 1.0, sharpe maybe 0
```

**✅ Pass criteria:** Metrics calculated, saved to DB, trade report shows 1 trade, CSV exported.

---

### Step 10 – Test Real Broker API (mStock) + Telegram Alerts

**Requires `.env` with real credentials – see Windows Quick-Start Guide Step 5.**

```powershell
# Test 1: mStock historical fetch (no trading, just data)
python - << 'PY'
from backtest.live.mstock import MStockSource

source = MStockSource()
try:
    df = source.get_candles("INFY", "2024-01-01", "2024-01-05", interval="day")
    print(f"Fetched {len(df)} bars for INFY")
    print(df.head())
    print("mStock connection ✅")
except Exception as e:
    print(f"mStock failed (check .env): {e}")
PY

# Expected: 5 daily bars with open/high/low/close/volume, no errors

# Test 2: Telegram alert (preferred)
python - << 'PY'
from backtest.alerts.manager import AlertManager

manager = AlertManager(config={"min_level":"info","channels":{"log":{"enabled":True},"telegram":{"enabled":True}}})
# Will try Telegram if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env, else just log
record = manager.send_alert(level="info", message="Test from Windows manual checklist", channels=["log","telegram"])
print(f"Alert sent via: {record.success_channels} failed: {record.failed_channels}")
print(f"Message: {record.message}")

# Test convenience methods
record2 = manager.alert_on_trade(type('obj', (), {"symbol":"INFY","side":"BUY","quantity":100,"fill_price":1500,"realized_pnl":500,"reason":"Manual test"})())
print(f"Trade alert: {record2.message}")

# Daily summary
record3 = manager.send_daily_summary(equity=105000, pnl=5000, trades=10, win_rate=60.0)
print(f"Daily summary: {record3.message}")
PY

# Expected: If Telegram configured, you get message on your phone instantly
# If not configured, log channel succeeds and shows "telegram not configured" in failed_channels – still passes
```

**Verify Telegram on phone:** You should receive `🔔 Trade executed: INFY BUY 100 @ 1500 | PnL: 500 | Manual test` or similar.

**✅ Pass criteria:** mStock returns DataFrame with 5 rows, Telegram message arrives on phone (or log shows success if Telegram not configured).

---

### Step 11 – Full Engine End-to-End (Dry-Run)

```powershell
# Dry-run – signals but no real orders – safest final check
$env:PYTHONPATH="src"
python -m backtest.forward.engine --config config/forward_testing.yaml --dry-run --symbols INFY --backtest

# Check state file created
dir state/
cat state/forward_test_state.json | Select-Object -First 20

# Check logs
cat logs/alerts.log | Select-Object -Last 20
```

**Verify in psql (portfolio + signals):**
```powershell
psql -U postgres -h localhost -d forward_test -c "SELECT name, status, current_cash FROM portfolios WHERE name='Forward Test 1';"
psql -U postgres -h localhost -d forward_test -c "SELECT symbol, signal_type, direction, executed, skip_reason FROM strategy_signals ORDER BY generated_at DESC LIMIT 5;"
# Expected: signals with executed=false, skip_reason dry_run or hold, bar_ts < generated_at (no lookahead)
```

**✅ Pass criteria:** Engine starts, heartbeat logs every 60s, state file saved, no crash, signals in DB with `bar_ts < generated_at`.

---

### Step 12 – Dashboard Verification

```powershell
$env:PYTHONPATH="src"
python -m backtest.dashboard.app --port 5000

# Open browser: http://localhost:5000
```

**Checklist in browser:**
- [ ] Portfolio Overview shows equity ₹100k+, cash, pos value, today P&L, total P&L with green/red
- [ ] Key Metrics shows trades today, win rate, Sharpe, max DD, exposure
- [ ] System Status shows Market Data Connected, Strategy active, Health healthy, Loops count, Last Update timestamp
- [ ] Equity Curve line chart updates
- [ ] Daily P&L bar chart green/red
- [ ] Drawdown chart
- [ ] Win/Loss pie
- [ ] Open Positions table shows INFY, TCS with qty, entry, current, unreal P&L, age, Close button
- [ ] Recent Trades table shows last 20 with green/red
- [ ] Active Orders table with Cancel button
- [ ] Manual Order Entry form works – enter INFY BUY 10 MARKET → Submit → shows in Active Orders
- [ ] Toggle Theme (dark/light) works
- [ ] Auto-refresh every 5s (check timestamp updates)

**✅ Pass criteria:** All sections load, charts render, buttons work, no JS console errors.

---

## Final Verification Queries (Copy-Paste for psql)

```powershell
$env:PGPASSWORD="your_password"
psql -U postgres -h localhost -d forward_test -f db/verify_schema.sql

psql -U postgres -h localhost -d forward_test -c "
SELECT 'portfolios' AS tbl, count(*) FROM portfolios
UNION ALL SELECT 'positions', count(*) FROM positions
UNION ALL SELECT 'orders', count(*) FROM orders
UNION ALL SELECT 'fills', count(*) FROM fills
UNION ALL SELECT 'trades', count(*) FROM trades
UNION ALL SELECT 'equity_curve', count(*) FROM equity_curve
UNION ALL SELECT 'strategy_signals', count(*) FROM strategy_signals
UNION ALL SELECT 'performance_metrics', count(*) FROM performance_metrics;
"

psql -U postgres -h localhost -d forward_test -c "
-- Lookahead bias check – should return 0 rows (PASS)
SELECT count(*) AS biased_signals_count,
       CASE WHEN count(*)=0 THEN 'PASS – No lookahead bias' ELSE 'FAIL – Bias detected!' END AS result
FROM strategy_signals
WHERE bar_ts >= generated_at;
"

psql -U postgres -h localhost -d forward_test -c "
-- Recent signals audit
SELECT symbol, signal_type, direction, strength, target_position, executed, skip_reason, generated_at, bar_ts
FROM strategy_signals
ORDER BY generated_at DESC LIMIT 10;
"
```

**All queries should show PASS and reasonable counts.**

---

## Success Criteria – You’re Done When:

- [ ] `verify_schema.sql` shows all PASS (11 tables, 2 views, 14 FKs, >=46 indexes)
- [ ] Portfolio created and saved to Postgres, loaded back correctly
- [ ] Market data handler normalizes mock ticks and aggregates bars correctly (1min boundary)
- [ ] Validator rejects corrupted OHLC and detects spikes
- [ ] Time manager correctly identifies NSE market open/closed and aligns bars
- [ ] Strategy adapter generates signals with `bar_ts < generated_at` (no lookahead) and creates orders
- [ ] Position sizing returns expected qty for all 6 methods and respects constraints
- [ ] Risk manager rejects restricted symbols and halts on drawdown, override works
- [ ] Stop manager triggers SL at 97 (2% below 100) and trailing ratchets one-way
- [ ] Performance calculator calculates total return, Sharpe, max DD, saves to DB
- [ ] Trade analyzer categorizes by symbol/exit reason/holding and calculates MAE/MFE
- [ ] mStock live fetch returns DataFrame (requires .env) and Telegram alert arrives on phone
- [ ] Engine dry-run starts, heartbeats, saves state JSON, signals in DB
- [ ] Dashboard loads at localhost:5000 with all 7 sections and charts

If all checked, your Windows + PostgreSQL + Real Broker API setup is **validated** and ready for live paper trading.

