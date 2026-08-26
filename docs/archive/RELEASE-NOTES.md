# Release Notes — Forward Testing Simulator v1.0 (All 24 Steps Complete)

> **Date:** 2026-08-23 · **Branch:** `arena/01a02caa-back-test` · **Tests:** 1175 passing, 4 skipped (need broker creds)

---

## Summary

Full forward testing trading simulator built in 8 phases (24 steps) from database to live dashboard, with bonus alerting, comparison, config management, and CI/CD. All components are **mock-testable** (no credentials needed) with optional live verification via real mStock API and Telegram.

**Tech Stack:** Python 3.9+ Backend, PostgreSQL 13+ (SQLite fallback for dev), SQLAlchemy ORM, Alembic, Pandas/NumPy, Flask + Chart.js Dashboard, Telegram Bot API preferred for alerts.

---

## Deliverables by Phase

### Phase 1 — Database Design & Setup (Steps 1–2) ✅
- `db/migrations/001_initial_schema.sql` – 10 tables (portfolios, positions, orders, fills, trades, equity_curve, market_data_cache, performance_metrics, strategy_signals, system_logs) + 2 views (v_open_positions, v_portfolio_summary), 14 FKs, 46+ indexes including 5 critical partial indexes, 2 triggers for updated_at, `gen_random_uuid()` handling for PG 13+ with pgcrypto fallback, idempotent `IF NOT EXISTS`
- `db/migrations/001_initial_schema.sqlite.sql` – SQLite variant with INTEGER PK
- `db/migrations/001_initial_schema_rollback.sql` – reverse dependency order
- `db/verify_schema.sql` – 11 checks printing PASS/FAIL
- `src/backtest/db/models.py` – SQLAlchemy ORM mirror, 10 tables, StrEnum for CHECK constraints
- `src/backtest/db/manager.py` – DatabaseManager with QueuePool 5/20 for PG, NullPool/StaticPool for SQLite, auto-reconnection, retry only transient faults (message-based), transaction context managers, health_check, pool_status
- `config/database.yaml` – 3 profiles development/testing/production

**Tests:** 44 + 107 =151

### Phase 2 — Core Data Models (Steps 3–6) ✅
- `simulator/money.py` – Decimal helpers, `to_decimal` via `repr()` for float safety, rejects bool, 4dp money, 8dp price
- `simulator/errors.py` – Domain exceptions
- `simulator/lots.py` – LotBook with FIFO/LIFO/AVERAGE, splits, dividends, `to_dict`/`from_dict` lossless
- `simulator/position.py` – Position with signed quantity, market_value signed, unrealized/realized PnL gross of commission, `reduce_shares`, `close_position`, cost basis methods
- `simulator/portfolio.py` – Portfolio root aggregate, cash convention long pays / short receives, `calculate_total_equity = cash + position_value`, `calculate_buying_power`, `get_current_exposure`, `can_open_position` → `PositionCheck` with code/reason, `open_position`, `reduce_position`, `close_all_positions`, `add_order`, `sync_orders`, `apply_fill` (single entry point, refuses reversal through zero), `save_to_db` writes **closed first then open** to satisfy partial unique index `uq_positions_one_open_per_symbol`, transaction atomic
- `simulator/enums.py` – OrderSide, OrderType (5 types), OrderStatus, TimeInForce, VALID_TRANSITIONS FSM, TERMINAL/WORKING sets
- `simulator/order.py` – Order full lifecycle, 5 order types market/limit/stop/stop_limit/trailing_stop, trigger sticky, trailing ratchet one-way, state machine, `is_fillable` checks trigger then limit, `calculate_fill_price` with price improvement, callbacks isolated, `to_dict`/`from_dict`
- `simulator/fill.py` – Fill immutable frozen dataclass, `object.__setattr__` for normalization, 5 commission models, `calculate_cash_delta`, `impact_on_position`
- `simulator/commission.py` – Zero, flat, per-share, percentage, tiered

**Tests:** 130 + 77 + 115 + 106 =428

### Phase 3 — Order Execution Simulation (Steps 7–9) ✅
- `simulator/slippage.py` – 5 models: Zero, FixedBps, Spread (with fallback_bps for daily bars), VolumeImpact (sqrt law), Volatility (ATR fraction), Hybrid (weighted), 4 profiles backtest/optimistic/realistic/pessimistic, tiers large/mid/small/illiquid, time-of-day multipliers, limit price cap with `capped` flag, signed adverse-positive, max_bps 1000 safety ceiling, statistics
- `config/slippage.yaml` – NSE large cap defaults, symbol tiers
- `simulator/fees.py` – IndiaEquityFees default (STT 0.1% both sides delivery, 0.025% sell-only intraday, stamp duty, DP charges flat sell-only, GST 18% on brokerage+exchange+SEBI), USEquityFees (SEC + FINRA TAF sell-only), FeeBreakdown → 3 fills columns, 10 broker presets (Zerodha, Angel, etc.), monthly volume tiers, FX converter
- `config/brokers.yaml` – 10 presets, FY 2024-25 rates
- `simulator/execution.py` – OrderExecutor where Steps 5-8 meet, liquidity caps via `max_participation` 10% of bar volume (forces partial fills), queue position via `touch_fill_probability` 0.5 realistic, availability market closed/halted, `ExecutionStatus` FILLED/PARTIAL/NO_FILL/REJECTED/CANCELLED, FOK cancelled not rejected but carries code, IOC remainder cancelled, latency reported not slept, single seeded RNG for determinism, `enforce_market_hours` off by default, `execute_all` by_symbol, statistics fill_rate, callbacks isolated
- `config/execution.yaml` – NSE 09:15-15:30 IST, 3 profiles

**Tests:** 101 + 109 + 99 =309

### Phase 4 — Live Data Integration (Steps 10–12) ✅ Mock-Only + Manual
- `live/time_manager.py` – TimeManager for NSE 09:15-15:30 IST (NYSE reference), weekend/holiday handling with built-in 2024 lists, pre-market/after-hours, `is_market_open`, `get_next_market_open/close` with weekend skip, `get_trading_days_between`, `align_to_timeframe` floors to 1min/3min/5min/15min/30min/1hr/day/week/month, `is_bar_closed`, IST/UTC/ET via ZoneInfo DST aware, mock time `set_mock_time`/`advance_mock_time`, NTP placeholder, latency stats
- `config/time_sync.yaml` – nse/nyse/mock profiles
- `live/data_validator.py` – DataValidator with `validate_tick`, `validate_bar`, `validate_ohlc_relationship` high>=all low<=all, price range 0.01-1M, bid<=ask, volume non-negative, chronological, future timestamp, spike detection Z-score rolling window 20 min 10 history threshold 3.0 (strict 2.0 lenient 4.0), gap detection intraday 300s daily 259200s (3 days), volume anomaly 5x avg, configurable strictness strict/normal/lenient, `on_failure` reject/warn/interpolate, stats, alert on 10 consecutive failures
- `config/data_quality.yaml` – 3 strictness profiles
- `live/market_data_handler.py` – MarketDataHandler normalization to standard `{symbol, timestamp UTC aware, bid, ask, last, volume, open, high, low, close, timeframe}`, `BarBuilder` per symbol/timeframe aggregates ticks into OHLCV with alignment, multi-symbol, reconnection with backoff 2^attempt max 30s, bounded deques `buffer_size` 1000, observer pattern `on_tick_received`/`on_bar_closed` with isolation, DB cache to `MARKET_DATA_CACHE` handling duplicate unique, abstract `BrokerFeed` + `MockBrokerFeed` (inject for tests) + `MStockBrokerFeed` wrapping existing `MStockSource` (wired to `live/mstock.py` per tracker)
- `config/market_data.yaml` – 4 profiles mock/mstock/csv/synthetic
- `docs/LOCAL-TESTING-MANUAL.md` – Where/how to provide `.env` (repo root, gitignored, chmod 600, `.mstock_session_token` cache), mock data sources (MockBrokerFeed in-memory, SyntheticDataSource, CSV, BarBuilder), examples for handler/validator/time_manager, optional live testing with real mStock creds, full engine dry-run/backtest, Docker, troubleshooting, cheat sheet

**Tests:** 18+18+18=54, all mock-only, no creds

### Phase 5 — Strategy Integration (Steps 13–14) ✅
- `forward/strategy_adapter.py` – StrategyAdapter bridges existing `strategy/base.py` (no duplication, deviation #5), `Signal` dataclass with BUY/SELL/HOLD, quantity, MARKET/LIMIT, limit_price, reason, indicators, strength, target_position -1..1, bar_ts, generated_at, strategy_name, signal_type entry/exit, direction long/short/flat, validation, `to_dict`/`from_dict`, no lookahead guarantee `bar_ts < generated_at` + `shift(1)` rule, per-symbol DataFrames `_bars`, multi-symbol via dict of strategies, dry-run mode, DB logging to `strategy_signals` with FK handling (auto-create portfolio row, drop order_id FK if not persisted), state persistence `get_state`/`load_state` for Step 20 recovery, 3 minimal sizers originally then re-exported from full engine
- `strategy/adapter.py` – re-export for convenience
- `simulator/position_sizing.py` – Full PositionSizer with 6 methods: fixed_quantity, fixed_dollar ($/price), percentage_portfolio (equity*% / price), risk_based `qty=(equity*risk%)/(price*stop%)`, volatility/ATR `qty=risk_amount/(ATR*multiplier)` with ATR priority explicit>signal>instance, Kelly `f*=p-q/b` `b=avg_win/avg_loss` `qty=equity*f**kelly_fraction/price` negative→0, `SizingMethod` enum with aliases, `RiskParams`, `SizingConstraints` (max value/pct, gross exposure, min trade dust filter→0, round lots floor, lot_size, max positions, leverage), `SizingResult` with audit, `apply_*` methods as per spec, config loader from YAML with 8 profiles (fixed, fixed_dollar, percentage, conservative 1% risk 2% stop, aggressive 2% risk 1% stop, volatility ATR 2x $1k, kelly 55% win 150/100 half, nse_fo lot 50)
- `config/position_sizing.yaml` – 8 profiles

**Tests:** 20 + 25 =45

### Phase 6 — Risk Management (Steps 15–16) ✅
- `simulator/risk_manager.py` – RiskManager with hierarchy order→position→portfolio, order-level restricted/allowed symbols, min/max order value, % daily vol, position-level max value/pct, max open positions, sector concentration via symbol_to_sector + sector_exposure_limits, portfolio-level max_drawdown_pct via `current_drawdown()`, daily/weekly/monthly loss limits via `_daily_pnl`, max_leverage gross/equity, max_gross_exposure_pct, max_total_exposure absolute, methods `validate_order`, `check_position_limits`, `check_buying_power`, `check_drawdown_limits`, `check_daily_loss_limit`, `check_leverage`, `emergency_stop_all` (halts, cancels via `cancel_all_orders`, pauses), `check_circuit_breakers` (drawdown, daily loss, consecutive losses), `record_trade_result` (loss streak + daily PnL), `record_error` (technical errors → auto-pause after 5), `override(code, duration)` with expiry, `validate_orders/signals` batch, alerts via `add_alert_callback`
- `config/risk.yaml` – 5 profiles default/conservative/aggressive/intraday/nse_fo/permissive
- `simulator/stop_manager.py` – StopManager with 6 stop types fixed_price/percentage/atr_based/trailing_fixed/trailing_percentage/time_based and 5 TP types fixed/percentage/risk_reward/resistance/trailing, `_calculate_stop_price` handles long/short logic (long SL below entry, TP above, short inverse), trailing ratchet one-way (sell tracks high-water mark only rises, buy tracks low-water only falls), breakeven move after trigger pct, scale-out partial profit taking, OCO groups, `add_stop_loss`, `add_take_profit`, `update_trailing_stops(current_prices)`, `check_stops(market_data)` uses low/high for trigger (long SL low<=stop, long TP high>=target), `remove_stops`, `create_orders_for_hits`, backtest_mode logging, stats
- `config/stops.yaml` – 7 profiles

**Tests:** 24 + 21 =45

### Phase 7 — Performance Tracking (Steps 17–19) ✅
- `simulator/performance.py` – PerformanceCalculator with return metrics total $/%, CAGR, annualized `mean*252`, daily/cumulative/MoM via resample ME/W, best/worst day/week/month, risk metrics vol `std`, annualized `vol*sqrt(252)`, max DD $ `cummax-equity` and % `equity/cummax-1`, current DD, DD duration, VaR 95%/99% percentile, ratios Sharpe `(excess_mean/vol*sqrt)` with risk-free daily, Sortino downside vol, Calmar `CAGR/abs(maxDD)`, Information vs benchmark, Treynor placeholder, trade stats from `closed_positions` total/win/loss, win_rate, avg win/loss, largest win/loss, profit_factor, holding period from opened_at/closed_at, expectancy, consecutive wins/losses, commission total, avg trade size, `update_equity_curve()` via `record_equity()`, cached DataFrames, `save_to_db()` → `PERFORMANCE_METRICS`, respects layering (no engine import)
- `config/performance.yaml` – 4 profiles
- `simulator/trade_analyzer.py` – TradeAnalyzer with `AnalyzedTrade` enriched model holding_category scalp/day/swing/position, pnl_bucket, day_of_week/hour, `analyze_trade` with MAE/MFE (long MAE lowest low-entry ≤0, MFE highest high-entry ≥0, short inverse, with price history filtering), execution quality bps vs mid, commission % PnL, `categorize_trades` by symbol/strategy/time_of_day/day_of_week/holding/pnl_bucket/exit_reason, `find_patterns` streaks, performance by hour/day, best/worst symbols, optimal holding, entry analysis, `generate_trade_report(date_range)` with daily summary, `export_trades` CSV/JSON/Excel, `calculate_execution_quality`, `calculate_slippage_analysis`
- `dashboard/data_provider.py` – DashboardDataProvider backend no Flask dep: overview, positions, trades, equity/daily/drawdown/win-loss charts, orders, metrics, system status, `get_all_dashboard_data()`
- `dashboard/app.py` – Flask + Chart.js, binds `0.0.0.0:5000` for Arena preview, single HTML responsive grid, dark/light mode CSS variables + localStorage, auto-refresh 5s, 7 sections as spec: portfolio big equity, positions with close btn, trades green/red, equity line, daily P&L bar green/red, drawdown line, win/loss pie, orders cancel btn, metrics, system status badges, controls start/stop/pause/resume, manual order form, logs, API `GET /api/portfolio/positions/trades/orders/metrics/equity_curve/daily_pnl/drawdown/win_loss/status/all`, `POST /api/start/stop/pause/resume/close_position/cancel_order/manual_order`, `/health`, CLI `run_dashboard`

**Tests:** 14 + 15 + 15 =44

### Phase 8 — System Orchestration (Step 20) ✅
- `forward/engine.py` – ForwardTestingEngine with `ForwardTestingConfig` 7 sections, `load_forward_config()` YAML validation, placeholders replaced with real implementations (MarketDataHandler, DataValidator, TimeManager, RiskManager, StopManager, PerformanceCalculator, TradeAnalyzer) with fallback to mocks, `StateManager` atomic JSON save via temp file replace, `initialize_system()` DB connect + create tables + portfolio create/restore + strategy via registry + sizer + executor + adapter + data handler + validators/managers, `start()` blocks with heartbeat 60s + slow-loop >1s warning, `stop()` graceful save + DB persist, `pause`/`resume`, `run_loop()` live polling, `_run_backtest_mode()` replays historical candles from DataSource, lifecycle hooks `on_start/on_stop/on_error/on_market_open/close` with isolation, signal handlers SIGINT/SIGTERM, dry-run & backtest modes, monitoring, `get_status()`, CLI
- `config/forward_testing.yaml` – NSE defaults
- `Dockerfile` – python:3.11-slim, layer caching, healthcheck
- `forward_testing.service` – systemd with hardening, restart on failure

**Tests:** 14

### Bonus (Steps 21–24) ✅ 100% Complete

**Step 21 – Alert & Notification System** – `alerts/manager.py` + `config/alerts.yaml`
- 7 channels: **Telegram Bot API preferred** (fast, free, reliable, `POST https://api.telegram.org/bot{token}/sendMessage` with Markdown, 30/h rate limit) – user preferred over email delayed and SMS costly, plus Email SMTP TLS, SMS Twilio mock, Slack webhook, Discord webhook, Desktop plyer, Log file
- `AlertLevel` with ORDER for threshold, `AlertChannel` validation, `AlertType` 9 types, `ChannelConfig` with creds from `.env` (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, SLACK_WEBHOOK_URL, etc.), `AlertConfig` with routing by level/type (type>level>default: critical→telegram+slack+sms+log, error→telegram+slack+log, warning/info→telegram+log), quiet hours IST 22:00-07:00 with critical override, rate limiting per hour per channel, templates with placeholders, history deque 1000, convenience methods `alert_on_trade/error/limit_breach/stop_loss/take_profit/daily_summary`, integration via `RiskManager.add_alert_callback()`
- 33 tests with mocked SMTP and `requests.post` for Slack/Discord/Telegram verifying URL and payload

**Step 22 – Backtesting Comparison Tool** – `analysis/comparison.py`
- `ComparisonAnalyzer` loads backtest from JSON/CSV/DataFrame/dict and forward from DB (portfolio_id via EquityCurve/Trade/Fill queries), portfolio object, or state JSON, `compare_metrics` (return diff, Sharpe diff, win rate diff, trade count diff, slippage/commission impact), `compare_trades` (missing/extra symbols, PnL diff), `calculate_attribution` (friction explains %), `detect_lookahead_bias` query `bar_ts >= generated_at` should be 0 rows – critical, `statistical_significance_tests` t-test via scipy, `generate_comparison_report` JSON or PDF with side-by-side equity curves + metric table + attribution pie, recommendations
- 13 tests

**Step 23 – Configuration Manager** – `config_manager/manager.py` + `config/app.yaml`
- Unified manager for all YAMLs with layered precedence defaults<YAML<env<overrides, env parsing bool/int/float/JSON, YAML/JSON support, dot-path get/set `risk.max_drawdown_pct`, schema validation placeholder, hot-reload `reload_config()` always + `check_and_reload()` with mtime, safe logging redacts sensitive keys (password, token, api_key), safe save skips secrets to `.env`, `.env` support via dotenv, global funcs `load_config/get_config/set_config/save_config/reload_config`, 4 profiles development/testing/production
- 13 tests

**Step 24 – Testing & CI/CD Setup** – `.github/workflows/ci.yml`, `.pre-commit-config.yaml`, `tox.ini`, `tests/unit/`, `integration/`, `e2e/`, `fixtures/`, `benchmarks/`
- `ci.yml`: lint (black --check, isort --check, flake8, pylint, mypy) → test matrix py39/310/311 with pytest-cov 80% fail-under, unit/integration/all mock-only, Codecov → build Docker + import test + docs → deploy-staging on main → deploy-production manual approval (workflow file requires `workflows` permission – present locally at `.github/workflows/ci.yml` but couldn’t be pushed via GitHub App in sandbox, needs manual addition via GitHub UI)
- `.pre-commit-config.yaml`: black/isort/flake8/mypy/pylint/trailing-whitespace/check-yaml/detect-private-key
- `tox.ini`: py39/310/311/lint/mypy/benchmark
- `tests/fixtures/market_data_samples.py`: random ticks/bars, corrupted fixtures, spike data, MockBrokerAPI/MockMarketDataFeed/MockTimeManager/MockDatabase
- `benchmarks/`: performance benchmarks (order creation/execution/slippage/fees/equity) + load tests (100 symbols*10 bars, 1000 orders, 1000 1min bars)

---

## Windows Manual Validation Guides (New)

Three highly scannable markdown guides for your direct PostgreSQL installation:

1. **`docs/DATABASE-MIGRATION-SEQUENCE-GUIDE.md`** – Dependency graph, file inventory, strict numbered execution list (portfolios→positions→orders→fills→trades→equity_curve→market_data_cache→performance_metrics→strategy_signals→system_logs→views), verification queries expecting 11 tables, 2 views, 14 FKs, 46+ indexes, rollback reverse order, common errors & fixes, idempotency guarantee

2. **`docs/WINDOWS-POSTGRES-QUICKSTART-GUIDE.md`** – Exact PowerShell/cmd commands for `psql --version`, service check, `psql -U postgres -h localhost -d postgres`, `CREATE DATABASE forward_test`, `CREATE USER ft_app`, `createdb`, `psql -f db/migrations/001_initial_schema.sql`, `psql -f db/verify_schema.sql`, `.env` exact syntax with `postgresql+psycopg2://` and URL-encoding, where to place `MSTOCK_API_KEY`, `TELEGRAM_BOT_TOKEN`, `SLACK_WEBHOOK_URL`, etc., venv creation `python -m venv .venv` + `.\.venv\Scripts\Activate.ps1` + `pip install -r requirements.txt`, app start via `python -m backtest.forward.engine --dry-run` and dashboard `python -m backtest.dashboard.app --port 5000`, full copy-paste block

3. **`docs/MANUAL-TESTING-CHECKLIST.md`** – 12-step happy path: pre-flight .env + DB + Python health check, create/load portfolio (write to DB + psql verify), market data handler mock tick flow + bar aggregation 1min boundary, validator corrupted OHLC + spike detection, time manager NSE open/weekend/holiday + bar alignment, strategy adapter no lookahead `bar_ts < generated_at`, position sizing 6 methods + constraints, risk manager restricted + emergency stop + override, stop manager 2% SL 5% TP OCO + trailing ratchet one-way, performance calculator total return/Sharpe/max DD + save to DB + trade analyzer categorization + MAE/MFE + CSV export, real broker API mStock fetch 5 daily bars + Telegram alert on phone, full engine dry-run + state JSON + signals audit, dashboard browser checklist (7 sections, charts, buttons, theme, auto-refresh), final verification queries for schema and bias detection `bar_ts >= generated_at` should be 0 rows

---

## How to Test Locally on Windows (from LOCAL-TESTING-MANUAL.md)

**Mock-only (no creds, CI passes):**
```powershell
$env:PYTHONPATH="src"
pytest tests/ -q -k "not live"  # 1175 passed, 4 skipped
pytest tests/test_market_data_handler.py tests/test_data_validator.py tests/test_time_manager.py -v
```

**Live with real mStock + Telegram (requires .env):**
```powershell
# .env in repo root (gitignored)
# MSTOCK_API_KEY, MSTOCK_USERNAME, MSTOCK_PASSWORD, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
pytest tests/test_mstock_live_integration.py -s
python -m backtest.forward.engine --backtest --symbols INFY --dry-run
python -m backtest.dashboard.app --port 5000
# Open http://localhost:5000
```

**Where to provide .env:** Repo root same folder as `.env.example`, `chmod 600`, never `git add` – `git check-ignore -v .env` shows `.gitignore:.env`. For this Arena sandbox, create via `cat > .env << 'EOF'...` – persists in branch snapshot but not in git push. For production, use GitHub Secrets or secret manager.

---

## Known Limitations (from TASK-TRACKER)

1. Tax lots not persisted – no lots table – FIFO position reloaded collapses to one lot at average – workaround `to_dict()`/`from_dict()` JSON snapshot lossless – Step 20 state manager should use it
2. Order `status_history`, `triggered`, `extreme_price` not persisted – no columns – survives in JSON snapshot

---

## Deviations from Plan (Deliberate)

1. SEC/FINRA TAF fees (US) → Both regimes implemented (IndiaEquityFees default, USEquityFees available) – repo trades NSE via mStock but plan names US fees
2. Column `timestamp` → `ts` – timestamp is SQL type name, forces quoting
3. NYSE calendar → NSE calendar 09:15-15:30 IST – same reason as 1
4. Broker Alpaca/IBKR → mStock (already implemented in `live/`)
5. New Strategy base class (Step 13) → Adapt existing `strategy/base.py` – avoid duplication
6. Native SQL ENUM → VARCHAR + CHECK – portability to SQLite
7. New `simulator/` package for Steps 3-6 – avoids collision with `forward.portfolio.Portfolio` and ORM `db.models.Portfolio`

---

## Next Steps (Optional)

All 24 steps done. You can now:
- Run manual testing checklist on Windows with real broker creds
- Add `.github/workflows/ci.yml` manually via GitHub UI (requires workflows permission – file is in workspace)
- Extend holiday calendars with `pandas_market_calendars` for full NSE 2024-2025
- Add more broker presets in `config/brokers.yaml`
- Implement real WebSocket for market data handler (currently mock + historical REST)

**Release is ready for manual validation on Windows.**

