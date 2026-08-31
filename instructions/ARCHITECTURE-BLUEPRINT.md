# Architecture Blueprint — `back-test`

> Read-only audit blueprint. Updated to reflect current repo state.
> **Version: 2.0** · Date: 2026-08-31 · Commit: `b68e328`
> Previous: 1.0 (`aa2b583`, 2026-08-30)
> Dependency edges in §2 were generated automatically with Python's `ast` module —
> the raw output is saved next to this file as **`graph.txt`** (regenerate:

---

## 0. Actual Stack (verified from the repo — not the template you pasted)

| Layer | Technology | Evidence |
|---|---|---|
| Backend | **Python 3.11** (Docker: `python:3.11-slim`; code targets 3.10+), **Flask ≥ 2.0** | `Dockerfile`, `requirements.txt`, `src/backtest/web/app.py` |
| Data / compute | **pandas ≥ 2.0, numpy ≥ 1.24**, pyarrow, matplotlib | `requirements.txt` |
| ORM / migrations | **SQLAlchemy ≥ 2.0**, **alembic ≥ 1.13**, psycopg2-binary ≥ 2.9 | `requirements.txt`, `src/backtest/db/models.py`, `db/alembic/` |
| Database | **PostgreSQL 13+ (README says PG 18 + TimescaleDB 2.29) + SQLite fallback** for dev | `db/migrations/001_initial_schema.sql`, `src/backtest/db/models.py` |
| Frontend | **Server-rendered Jinja2 + vanilla JS** (no framework, no TS), **Chart.js 4.4.1 via CDN**, **SSE** (not WebSocket) for the portfolio page, 2 s `setInterval` polling for forward | `src/backtest/web/templates/base.html`, `src/backtest/web/static/js/*` |
| External broker | **mStock** (Mirae) REST API — auth (TOTP) + market data only | `src/backtest/brokers/mstock.py`, `src/backtest/live/*` |
| Tests | pytest ≥ 8 (≈100 test files), 4 Node `.mjs` behaviour harnesses for JS | `tests/` |

⚠️ The prompt template you pasted (FastAPI + React/TS + WebSocket) does **not** match this
repository. Everything below is what the code actually is.

---

## 1. Repo Map

```
back-test/
├── src/backtest/                  # 100% of the application code (~35.4k lines Python)
│   ├── __main__.py                # `python -m backtest` → cli.main()
│   ├── cli.py                     # CLI: list | run | compare | preflight | papertrade
│   ├── runner.py                  # SHARED orchestrator: build_source(), run_on_candles()
│   ├── logging_config.py          # configure_logging(), get_logger(), request-id context
│   │
│   ├── web/                       # ★ PRIMARY ENTRY POINT — unified Flask app
│   │   ├── app.py                 # create_app()/run_app(); page routes; request logging;
│   │   │                          #   registers all 7 API blueprints; broker expiry monitor
│   │   ├── templates/             # base, backtest, compare, dashboard, forward,
│   │   │                          #   portfolio, data_manager, placeholder (Jinja2)
│   │   └── static/js/             # backtest.js, compare.js, forward.js, portfolio.js,
│   │                              #   dashboard.js, data_manager.js, deep_dive.js,
│   │                              #   broker_auth_modal.js, broker_status.js,
│   │                              #   charts/* (equity/drawdown/signals + compare/*),
│   │                              #   components/* (metrics_cards, trade_table,
│   │                              #   params_form, currency, loader, toast)
│   │
│   ├── api/                       # Flask blueprints (route handlers)
│   │   ├── backtest.py            # POST /api/backtest/run, /run-many (≤4 slots, threads)
│   │   ├── strategies.py          # GET /api/strategies, /api/strategies/<n>/params
│   │   ├── symbols.py             # GET /api/symbols (db mode only, cached)
│   │   ├── forward.py             # ★ ForwardSession (server-side replay clock) +
│   │   │                          #   /api/forward/start|stop|status|sessions|trades|equity
│   │   ├── portfolio.py           # /api/portfolio/* REST + SSE /api/portfolio/stream
│   │   ├── broker_auth.py         # /api/broker/login|verify-totp|status|logout
│   │   └── data_manager.py        # /api/data/fetch|stop|status|inventory (mStock → DB job)
│   │
│   ├── data/                      # Data sources (DataSource protocol)
│   │   ├── base.py                # DataSource Protocol + normalize_candles()
│   │   ├── synthetic.py           # SyntheticSource — seeded random walk
│   │   ├── csv_source.py          # CsvSource — data/*.csv
│   │   ├── db_source.py           # ★ DbSource — reads market_data_cache (raw SQL) +
│   │   │                          #   pandas resample 1min→any
│   │   └── universe.py            # Symbol universes (NIFTY50/100/200…) + correlation groups
│   │
│   ├── strategy/                  # Strategy abstraction
│   │   ├── base.py                # Strategy ABC: params schema, validate(),
│   │   │                          #   generate_signals()/entries()/exits()
│   │   ├── registry.py            # @register + pkgutil auto-discovery, get_strategy()
│   │   └── adapter.py             # Re-exports forward.strategy_adapter (no logic)
│   │
│   ├── strategies/                # Built-in strategies (auto-discovered)
│   │   ├── buy_and_hold.py  sma_crossover.py  rsi_reversion.py  donchian_breakout.py
│   │
│   ├── engine/                    # ★ BACKTEST ENGINE (vectorized, no orders)
│   │   ├── backtester.py          # BacktestConfig, BacktestResult, Backtester.run()
│   │   │                          #   → _run_vectorized | _run_with_risk (stops/TP)
│   │   ├── metrics.py             # compute_metrics() — sharpe, drawdown, …
│   │   ├── trades.py              # walk_trades()/trade_stats() — single source of truth
│   │   └── plotting.py            # matplotlib charts (CLI)
│   │
│   ├── adapters/
│   │   └── backtest_adapter.py    # BacktestAdapter — BacktestResult → JSON payloads
│   │                              #   (to_metrics/to_equity/to_drawdown/to_trades/
│   │   │    to_signals/to_compare/to_all)
│   │
│   ├── forward/                   # ★ FORWARD TESTING — four different engines live here
│   │   ├── __init__.py            # re-exports (incl. ForwardTestingEngine, paper fns)
│   │   ├── engine.py              # (A) ForwardTestingEngine — CLI "Step 20" loop engine,
│   │   │                          #     YAML config, state JSON, Mock* placeholders
│   │   ├── strategy_adapter.py    # (A) StrategyAdapter — bar-by-bar bridge: Strategy →
│   │   │                          #     Signal → Order → OrderExecutor; DB signal log
│   │   ├── runner.py              # (C) StrategyRunner — isolated capital bucket,
│   │   │                          #     single-symbol or pool/universe, rolling buffers
│   │   ├── portfolio_manager.py   # (C) PortfolioManager — control tower, SSE summary,
│   │   │                          #     global circuit breakers (singleton)
│   │   ├── feed.py                # (C) SyntheticFeed — seeded random-walk bars @1 s
│   │   ├── order_ledger.py        # (C) OrderLedger (PRT- coid tagging, fill routing) +
│   │   │                          #     PaperBroker (instant fill at bar close)
│   │   ├── risk_supervisor.py     # (C) RiskSupervisor — global daily-loss/drawdown halt
│   │   ├── paper.py               # (B) run_walkforward / run_live_papertrade —
│   │   │                          #     bar-by-bar over FULL precomputed signals
│   │   ├── broker.py              # (B) SimulatedBroker.step() — per-bar cost/stop math
│   │   ├── portfolio.py           # (B) Portfolio/StrategyAccount (exposure units)
│   │   └── live_engine.py         # (D) LiveForwardEngine — 60 s poll loop (mStock or
│   │                              #     synthetic), raw-SQL state in forward_test_*
│   │
│   ├── simulator/                 # Heavyweight trade-simulation primitives (Decimal)
│   │   ├── money.py enums.py errors.py lots.py
│   │   ├── order.py               # Order (lifecycle, validation, DB-aware)
│   │   ├── fill.py                # Fill (immutable, fees/slippage attribution)
│   │   ├── commission.py fees.py  # fee models (zerodha…)
│   │   ├── slippage.py            # slippage models (fixed/bps/hybrid/vol)
│   │   ├── execution.py           # ★ OrderExecutor — market/limit/stop, realism levels,
│   │   │                          #   latency, liquidity checks, events
│   │   ├── position.py            # Position (signed qty, avg entry, lots)
│   │   ├── portfolio.py           # ★ simulator.Portfolio — cash, positions, orders,
│   │   │                          #   can_open_position, apply_fill, DB save/load
│   │   ├── position_sizing.py     # sizers: fixed, %, risk-based, ATR, volatility, Kelly
│   │   ├── risk_manager.py        # per-portfolio risk limits
│   │   ├── stop_manager.py        # stop-loss/take-profit manager
│   │   ├── performance.py         # PerformanceCalculator → performance_metrics table
│   │   └── trade_analyzer.py      # round-trip analysis
│   │
│   ├── db/                        # Connection layer + ORM
│   │   ├── models.py              # ★ 10 SQLAlchemy tables (mirror of 001 schema)
│   │   ├── manager.py             # DatabaseManager — pool, retries, session(), health
│   │   └── config.py              # config/database.yaml + FORWARD_TEST_DB_* env layering
│   │
│   ├── live/                      # mStock integration
│   │   ├── auth.py                # TOTP (HMAC-SHA1) + login, session cache
│   │   ├── mstock.py              # MStockSource — candle fetch/normalize (DataSource)
│   │   ├── preflight.py           # DNS/HTTPS/auth checks
│   │   ├── time_manager.py        # NSE market-hours detection
│   │   ├── data_validator.py      # DataValidator — OHLC sanity, staleness, gaps
│   │   └── market_data_handler.py # MarketDataHandler + BrokerFeed (Mock/MStock) +
│   │                              #   BarBuilder (tick→bar) — used by forward/engine.py
│   │
│   ├── marketdata/                # ⚠ THIRD (isolated) tick/bar pipeline — not wired to
│   │                              #    any engine (orphan; tests only)
│   │   ├── ticks.py               # Tick, normalize_tick
│   │   ├── bars.py                # Bar, BarAggregator (multi-timeframe, gap fill)
│   │   ├── feed.py                # DataFeed ABC, MockFeed, MStockFeed
│   │   ├── handler.py             # MarketDataHandler (poll/ingest/persist)
│   │   ├── quality.py             # quality checks (gaps, staleness, spikes)
│   │   ├── timesync.py            # clock drift handling
│   │   └── errors.py
│   │
│   ├── brokers/                   # mStock auth-only broker layer
│   │   ├── base.py                # BrokerAuthBase (ABC)
│   │   ├── mstock.py              # MStockBroker — login/TOTP/status (NO order methods)
│   │   └── session_manager.py     # singleton session lifecycle + expiry monitor thread
│   │
│   ├── dashboard/                 # ⚠ SECOND, separate Flask app (Step 19) — NOT registered
│   │                              #    in web/app.py; own main()/port
│   │   ├── app.py                 # dashboard routes + --engine flag → ForwardTestingEngine
│   │   └── data_provider.py       # engine → JSON serializer
│   │
│   ├── alerts/manager.py          # ⚠ orphan (tests only) — alert rules/notifications
│   ├── analysis/comparison.py     # ⚠ orphan (tests only) — DB-backed run comparisons
│   └── config_manager/manager.py  # ⚠ orphan (tests only) — YAML config manager
│
├── db/
│   ├── migrations/001_initial_schema.sql          # ★ DDL source of truth (10 tables)
│   │            001_initial_schema.sqlite.sql     # SQLite variant
│   │            001_initial_schema_rollback.sql
│   ├── alembic/versions/20260819_1657_001_initial_forward_testing_schema.py
│   ├── verify_schema.sql
│   └── DB-IMPLEMENTATION-GUIDE.md, CONNECTION-MANAGER.md
│
├── config/                        # app.yaml, database.yaml, forward_testing.yaml,
│                                  #   brokers.yaml, risk.yaml, slippage.yaml, stops.yaml,
│                                  #   execution.yaml, position_sizing.yaml, marketdata.yaml,
│                                  #   market_data.yaml, calendar.yaml, alerts.yaml,
│                                  #   data_quality.yaml, performance.yaml, quality.yaml,
│                                  #   time_sync.yaml
├── scripts/fetch_nifty500_historical.py     # bulk 1min ingestion → market_data_cache
│   (+ fetch_all_1min.ps1/.bat)
├── benchmarks/                    # load + performance benchmarks
├── tests/                         # ~100 pytest files + tests/js/*.mjs + tests/manual
├── instructions/                  # PRDs / task trackers (multi-strategy PRD, etc.)
├── docs/                          # ARCHITECTURE, DATABASE, FORWARD-TESTING, WEB-UI, …
├── stock-list/nse_ind_nifty200list.csv
├── requirements.txt  Dockerfile  alembic.ini  tox.ini  .env.example
├── forward_testing.service        # systemd unit for the forward engine
├── graphify-out/                  # pre-existing AST graph cache (4,394 nodes)
├── ARCHITECTURE-BLUEPRINT.md      # ← this file
└── graph.txt                      # ← ast-generated dependency map (§2.10)
```

---

## 2. File-by-File Responsibility

Format: **purpose** · *imports* → *imported by*. (All internal `backtest.*` edges verified
by the ast script; external deps listed only where notable.)

### 2.1 Web shell — `web/`
- **`web/app.py`** (414 ln) — App factory `create_app(source, log_level, currency, replay_speed)`.
  Installs logging + request-id middleware, registers 7 blueprints, starts the broker
  session expiry monitor, defines page routes (`/`, `/backtest`, `/dashboard`, `/compare`,
  `/forward`, `/portfolio`, `/data`) + `/api/config`, `/health`.
  Imports: `backtest.api.*` (all blueprints), `brokers.session_manager`, `logging_config`,
  `data.db_source` (lazy, only when `--source db`). Imported by: nobody (entry point,
  `python -m backtest.web.app`).
- **`web/templates/*.html`** — Jinja2 pages; each loads a matching JS controller.
- **`web/static/js/backtest.js`** — Backtest page: `fetch /api/strategies` → dropdown,
  `/api/strategies/<n>/params` → dynamic form, `POST /api/backtest/run` → renders metric
  cards, equity/drawdown/signal charts, trade table.
- **`web/static/js/compare.js`** — Compare page: `POST /api/backtest/run-many` with 2–4
  slots; per-slot "Open in Backtest".
- **`web/static/js/forward.js`** — Forward page: `POST /api/forward/start`, then
  `setInterval(poll, 2000)` → `GET /api/forward/status?state_id=…`; remembers
  `state_id` in sessionStorage; broker-auth gate (403 → modal).
- **`web/static/js/portfolio.js`** — Portfolio page: `new EventSource("/api/portfolio/stream")`
  (1 Hz SSE) + REST control calls (`runner/create`, `runner/<id>/control`,
  `control/<action>`, `emergency_stop`, `test/breach`, `universes`, `summary`).
- **`web/static/js/dashboard.js`** — Dashboard page: strategy list +
  `GET /api/forward/status` (cross-page bot status) refreshed every 3 s.
- **`web/static/js/data_manager.js`** — Data page: start/stop mStock fetch job, inventory.
- **`web/static/js/broker_auth_modal.js` / `broker_status.js`** — login/TOTP modal;
  polls `/api/broker/status`.
- **`web/static/js/components/*`, `charts/*`** — presentational: metrics_cards,
  trade_table, params_form, equity/drawdown/signals charts (+ compare variants).

### 2.2 API blueprints — `api/`
- **`api/backtest.py`** (371) — `POST /api/backtest/run` (single) and
  `POST /api/backtest/run-many` (≤4 slots via `ThreadPoolExecutor`).
  Flow: `_resolve_strategy` → `_candles` (source per `BACKTEST_SOURCE`) →
  `run_on_candles` → `_trim_to_range` → `BacktestAdapter.to_all()`.
  Imports: `runner`, `engine.backtester`, `engine.metrics`, `strategy.registry`,
  `adapters.backtest_adapter`, `logging_config`. Imported by: `api/__init__`,
  `web/app.py` (SUPPORTED_TIMEFRAMES re-import), `forward/live_engine.py` (⚠ imports
  `_interval` from here — layering violation).
- **`api/strategies.py`** (41) — `GET /api/strategies` (catalogue) +
  `GET /api/strategies/<name>/params` (schema for the dynamic form).
  Imports: `strategy.registry` (`get_all`, `get_params`).
- **`api/symbols.py`** (44) — `GET /api/symbols`: db-mode only, `DbSource().list_symbols()`,
  module-level cache. Imports: `data.db_source`.
- **`api/forward.py`** (829) — ★ Web forward test. `ForwardSession` (line 92): holds a
  **precomputed** `BacktestResult`, a daemon clock thread (`_loop`, line 194) reveals bars
  at `bars_per_second`; `snapshot()` (line 352) slices the revealed prefix
  (`_prefix_result`, line 257), recomputes metrics and builds the UI payload via
  `BacktestAdapter`. Endpoints: `start` (line 634; broker-auth guard for `mode=live`),
  `stop`, `status`, `sessions`, `trades`, `equity`. Process-wide
  `OrderedDict` `_SESSIONS` (max 20) + `_ACTIVE_ID`.
  Imports: `runner`, `engine.*`, `adapters.backtest_adapter`, `brokers.session_manager`.
- **`api/portfolio.py`** (306) — REST + SSE surface over the `PortfolioManager` singleton:
  `summary`, `universes`, `runner/create`, `runner/<id>` (deep dive),
  `runner/<id>/control` (pause/resume/stop/flatten/start), `control/<action>`
  (pause_all/resume_all/stop_all/emergency_flatten/reset_breaker), `emergency_stop`,
  `test/breach` (crash injection), **`GET /api/portfolio/stream`** (SSE, JSON every 1 s).
  Imports: `forward.portfolio_manager`, `forward.risk_supervisor`, `forward.runner`,
  `data.universe`.
- **`api/broker_auth.py`** (151) — `/api/broker/login`, `/verify-totp`, `/status`,
  `/logout` via `BrokerSessionManager`.
- **`api/data_manager.py`** (415) — Background-thread mStock fetch job
  (`_run_fetch_job` → `_fetch_bars_chunked` → `_persist_bars` into `market_data_cache`),
  inventory + status endpoints. Imports: `data.db_source`, raw SQLAlchemy `text()`.

### 2.3 Data layer — `data/`
- **`base.py`** (44) — `DataSource` Protocol (`get_candles(symbol, start, end, interval)`)
  + `normalize_candles()` (lowercase cols, DatetimeIndex, dedupe, sort, dropna).
  Imported by: synthetic, csv_source, db_source, live/mstock, forward/live_engine.
- **`synthetic.py`** (66) — seeded random-walk candles (per symbol+seed).
- **`csv_source.py`** (47) — reads `data/*.csv`.
- **`db_source.py`** (205) — ★ `DbSource.get_candles`: raw SQL against
  `market_data_cache`; `_find_best_source_tf` picks the finest stored timeframe
  (`day` shortcut, else 1min→…→1hour probe), then **pandas `resample` up** to the
  requested interval (`_resample`, line 160). `list_symbols(timeframe)`.
  URL resolution: `DATABASE_URL` → `FORWARD_TEST_DB_URL` →
  `postgresql+psycopg2://postgres:postgres@localhost:5432/forward_test` (⚠ independent
  of `db/config.py`).
- **`universe.py`** (171) — `Universe` registry (NIFTY50/100/200…),
  `CORRELATION_GROUPS` used by the risk supervisor.

### 2.4 Strategy layer
- **`strategy/base.py`** (292) — `Strategy` ABC: class attrs `name/description/version/
  author/params`, param schema validation (`param_schema()`, `validate()`),
  `generate_signals(candles) -> pd.Series` (also entries/exits variant).
- **`strategy/registry.py`** (109) — `@register`, `_discover()` (pkgutil scan of
  `backtest.strategies`, skips broken modules), `list_strategies/get_strategy/get_all/
  get_params`. Imported by: runner, cli, api/backtest, api/strategies,
  forward/engine, forward/live_engine, forward/paper.
- **`strategy/adapter.py`** (67) — pure re-export of `forward.strategy_adapter`.
- **`strategies/{buy_and_hold,sma_crossover,rsi_reversion,donchian_breakout}.py`** —
  16–51 lines each; subclass `Strategy`; auto-discovered (statically "orphan" —
  imported dynamically by the registry).

### 2.5 Backtest engine — `engine/` + `runner.py` + `adapters/`
- **`runner.py`** (102) — `build_source(name)` (synthetic|csv|mstock|db),
  `run_on_candles(candles, strategy_name, params, symbol, config)` — instantiates the
  strategy, resolves per-strategy stop/TP into `BacktestConfig`, calls
  `strategy.generate_signals()` then `Backtester(cfg).run()`, stamps metadata.
  Also `run_backtest(source, RunSpec)`, `compare_strategies()`.
  Imported by: `api/backtest.py`, `api/forward.py`, `cli.py`, `forward/live_engine.py`.
- **`engine/backtester.py`** (193) — `BacktestConfig` (capital, commission 0.03%,
  slippage 0.05%, stop/TP), `BacktestResult`, `Backtester.run()` (line 39): reindex +
  clip signals to [-1,1] → **`_run_vectorized`** (line 89: `held = target.shift(1)`,
  gross = held·pct_change, turnover costs, cumprod) **or `_run_with_risk`** (line 100:
  Python per-bar loop with intrabar stop/TP exits, re-entry block flag).
  Position semantics = **exposure units** (1.0 = fully invested), **no orders, no lots**.
- **`engine/metrics.py`** (69) — `compute_metrics(result)`: return, CAGR, vol, sharpe,
  max drawdown, calmar, exposure, final equity; trade stats via `engine.trades`.
- **`engine/trades.py`** (160) — `walk_trades(equity, position, close)`: a trade = run of
  consecutive same-sign bars; P&L from the **equity curve** (costs included); open
  position = one trade marked to final close, excluded from win rate. `trade_stats()`.
  Single source of truth for cards **and** table (gaps G1/G2).
- **`engine/plotting.py`** (54) — matplotlib output for CLI.
- **`adapters/backtest_adapter.py`** (239) — `BacktestAdapter(result)` → JSON:
  `to_metrics()`, `to_equity()`, `to_drawdown()`, `to_trades()` (via `walk_trades`),
  `to_signals()` (buys/sells/candles), `to_compare()`, `to_all()`. Imported by
  `api/backtest.py`, `api/forward.py`.

### 2.6 Forward testing — `forward/` (four engines)
- **(A) `engine.py`** (1232) — `ForwardTestingEngine` ("Step 20", CLI + Dockerfile CMD):
  YAML config (`config/forward_testing.yaml`), `initialize_system()` wires
  simulator.Portfolio + Strategy + PositionSizer + OrderExecutor + StrategyAdapter +
  MarketDataHandler (real, falling back to Mock*) + RiskManager + StopManager +
  PerformanceCalculator + TradeAnalyzer + StateManager (JSON snapshot). `start()` →
  `run_loop()` (line 1005, poll loop every `loop_interval_seconds`) or
  `_run_backtest_mode()` (line 1098, replay candles from a DataSource). State saved to
  `state/forward_test_state.json`.
  Imports: `db.manager`, `db.models`, `forward.strategy_adapter`, `live.*`,
  `simulator.{errors,money,execution,fees,slippage,performance,portfolio,position_sizing,
  risk_manager,stop_manager,trade_analyzer}`, `strategy.registry`.
  Imported by: `forward/__init__`, `dashboard/app.py` (optional `--engine`), tests.
- **(A) `strategy_adapter.py`** (1445) — `StrategyAdapter`: per-symbol OHLCV DataFrames
  (capped 5000 bars), `on_market_data()`/`on_bar_close()` (line 601) →
  `generate_signals(symbol)` (line 732: runs the strategy over the accumulated buffer,
  takes `series.iloc[-1]`, clips to [-1,1], compares with current position, emits
  typed `Signal`) → `execute_signals()` (line 985: size via sizer, validate via
  `portfolio.can_open_position`, create `simulator.Order`, optionally
  `executor.execute(order, snapshot)`, persist to `strategy_signals`).
  Imports: `simulator.{enums,errors,money,order,position_sizing}`, `strategy.base`,
  `db.models` (lazy). Imported by: `forward/engine.py`, `strategy/adapter.py` (re-export).
- **(B) `paper.py`** (330) — `run_walkforward(source, strategies[...], symbol, …)`:
  precomputes **shifted** signals for all strategies over the full window, then a
  single bar loop calls `SimulatedBroker.step()` per strategy (line 17);
  `run_live_papertrade()` (line 125) = same math + resumable JSON state file;
  `poll_live_papertrade()` (line 276) = loop it. CLI `papertrade` command uses these.
  Imports: `engine.backtester`, `forward.broker`, `forward.portfolio`, `strategy.registry`.
- **(B) `broker.py`** (103) — `SimulatedBroker.step(desired, bar, held, prev_close,
  entry_price, blocked)`: exposure-unit fill at prev_close, turnover cost (rate 0.0008),
  intrabar stop/TP → forced exit + `blocked` flag. Mirrors `_run_with_risk`.
- **(B) `portfolio.py`** (88) — `Portfolio`/`StrategyAccount` (cash, exposure position,
  equity history, snapshot/load).
- **(C) `runner.py`** (717) — `StrategyRunner`: isolated cash bucket + per-symbol
  deques (max 500 bars); `process_candle_event()` (line 229) for SINGLE targets,
  `on_tick_end()` (line 305) → `_process_pool()` for universe targets (rank by
  `_entry_score`, top-K entries); `_signal_for()` (line 393) rebuilds a DataFrame each
  bar and runs `strategy.generate_signals(df)`; `_act_on_signal()` (line 419) →
  `_emit_entry/_emit_close` → `PaperBroker.submit_market`; `on_fill()` routes fills
  back; instance circuit breakers (`_check_instance_risk`); `get_state()/get_detail()`.
  Imports: `forward.order_ledger`, `data.universe`, `strategy.registry` (lazy).
- **(C) `portfolio_manager.py`** (451) — `PortfolioManager` (process singleton via
  `get_portfolio_manager()`): owns `OrderLedger` + `PaperBroker` + `RiskSupervisor` +
  `SyntheticFeed`; `add_runner/remove_runner/control_runner`, bulk ops,
  `emergency_flatten_all`, `reset_circuit_breaker`; `_on_bar()` (line 227) fans each bar
  to matching runners under **one RLock**, `_on_tick_end()` (line 242) → pool scans +
  `_evaluate_risk()`; `get_portfolio_summary()` (line 299) = the SSE payload.
  Imports: `forward.feed`, `forward.order_ledger`, `forward.risk_supervisor`,
  `forward.runner`. Imported by: `api/portfolio.py`.
- **(C) `feed.py`** (176) — `SyntheticFeed`: deterministic (symbol-seeded) random-walk
  bars every `tick_seconds` (default 1 s) in a daemon thread; warmup burst;
  `on_bar`/`on_tick_end` callbacks.
- **(C) `order_ledger.py`** (292) — `OrderLedger`: `PRT-{id8}-{ms}-{seq}` client order
  ids, routing map, `apply_fill()` → handler dispatch (outside lock); `PaperBroker.
  submit_market()` fills **instantly at the supplied price (bar close)**, slippage_pct
  default 0.
- **(C) `risk_supervisor.py`** (195) — `RiskSupervisor.evaluate()`: global daily-loss
  (absolute) + max-drawdown (fraction) breaches → `HALT_PAUSE` / `HALT_FLATTEN`;
  correlation/concentration warnings; latch stays until explicit reset.
  Imports: `data.universe`.
- **(D) `live_engine.py`** (697) — `LiveForwardEngine`: singleton dict per `state_id`;
  `start()` loads state from DB → 60 s loop: `_tick_live()` (market-hours gate →
  `_fetch_latest_bar` from mStock minute endpoint) or `_tick_synthetic()`;
  `_process_bar()` (line 450): rolling 200-bar window → strategy signal → inline
  long/short trade book → `_save_trade/_close_trade/_save_equity` via **raw SQL**
  against `forward_test_state / forward_test_trades / forward_test_equity`
  (⚠ **tables that exist in no DDL in this repo**). Imports: `live.auth`,
  `runner.build_source`, `strategy.registry`, `api.backtest._interval` (⚠).
  Imported by: nobody (statically orphan — dead path).

### 2.7 Simulator — `simulator/` (Decimal-typed execution core)
- **`order.py`** (1037) — `Order` dataclass: lifecycle (pending→partial→filled/…),
  validation, `submit()`, DB row mapping. Imports: `db.models`, `enums`, `money`.
- **`fill.py`** (685) — `Fill`: immutable execution record, fee + slippage attribution,
  DB persistence. Imports: `db.models`, `commission`, `position`.
- **`execution.py`** (938) — ★ `OrderExecutor.execute(order, snapshot)` (line 614):
  realism presets, market-hours + halt checks, latency simulation, liquidity checks,
  market/limit/stop processing, price improvement, rejection codes, event callbacks;
  applies fills to the portfolio. Imports: `fees`, `fill`, `slippage`, `order`,
  `portfolio`, `enums`, `money`.
- **`portfolio.py`** (1277) — ★ `simulator.Portfolio`: cash (Decimal), `Position`
  objects, order book, `can_open_position()` (limits: shorting, max positions, leverage,
  margin), `apply_fill()`, `close_all_positions()`, DB save/load.
- **`position.py`** (721) — `Position`: signed qty, avg entry, mark-to-market, lots.
- **`position_sizing.py`** (1288) — `PositionSizer` + fixed/percentage/risk-based/
  ATR/volatility/Kelly sizers, `SizingConfig`.
- **`slippage.py`** (1102) — slippage calculators (fixed, bps, hybrid, volatility).
- **`fees.py`** (1029) / **`commission.py`** (408) — broker fee models (zerodha, etc.).
- **`risk_manager.py`** (865) — per-portfolio risk validation (max position value/pct,
  drawdown, daily loss, leverage).
- **`stop_manager.py`** (1038) — stop-loss/take-profit/trailing/time-stop manager.
- **`performance.py`** (687) — `PerformanceCalculator`: equity curve + daily rollup →
  `performance_metrics` table.
- **`trade_analyzer.py`** (766) — round-trip stats from a portfolio.
- **`money.py`** (113) — Decimal helpers; **`enums.py`** (167); **`errors.py`** (107);
  **`lots.py`** (378).
- Package `__init__.py` (317) re-exports the whole public surface.
- Imported by: `forward/engine.py`, `forward/strategy_adapter.py`, `dashboard/*`,
  `marketdata/quality.py`, tests. **Not** imported by the web backtest/forward API paths.

### 2.8 DB layer — `db/`
- **`models.py`** (839) — 10 ORM tables (see §4). Cross-dialect types
  (`UUIDStr`, `JSONVariant`, `BigIntPK`), `StrEnum`s, CHECK constraints mirroring SQL.
- **`manager.py`** (750) — `DatabaseManager`: engine factory (pooling, SQLite pragmas,
  slow-query logging), `connect()/connection()/session()/begin_transaction()`,
  `execute_query/many`, `fetch_*`, `health_check`, `pool_status`. `from_env()` uses
  `db/config.py`.
- **`config.py`** (356) — layered config: `config/database.yaml` profile →
  `FORWARD_TEST_DB_*` env → defaults; validation.
- Imported by: `forward/engine.py`, `simulator/{order,fill,position,portfolio,
  performance}`, `marketdata/handler.py`, `live/market_data_handler.py`, tests.
  (The web app itself only touches the DB via `DbSource` raw SQL + `api/data_manager`.)

### 2.9 Live / broker / isolated subsystems
- **`live/auth.py`** (175) — mStock login + TOTP (HMAC-SHA1) + session cache.
- **`live/mstock.py`** (157) — `MStockSource` (DataSource): fetch + normalize candles
  from the mStock API.
- **`live/preflight.py`** (89) — DNS/HTTPS/auth preflight.
- **`live/time_manager.py`** (613) — NSE session/holiday logic.
- **`live/data_validator.py`** (698) — OHLC/staleness/gap validation.
- **`live/market_data_handler.py`** (964) — `MarketDataHandler` + `BrokerFeed`
  (Mock/MStock) + `BarBuilder` (tick→bar aggregation): the data front-end of
  `ForwardTestingEngine`. Imports: `live.mstock`, `live.data_validator`,
  `live.time_manager`, `db.models`.
- **`brokers/base.py`** (125) / **`brokers/mstock.py`** (331) / **`brokers/
  session_manager.py`** (291) — auth-only broker abstraction + singleton
  `BrokerSessionManager` with a 5-min expiry-monitor daemon thread.
  Imported by: `api/broker_auth.py`, `api/forward.py` (guard), `web/app.py` (monitor).
- **`marketdata/`** (6 files, ~2,850 ln) — a **complete, isolated tick pipeline**
  (`Tick`, `BarAggregator`, `DataFeed`/`MockFeed`/`MStockFeed`, `MarketDataHandler`,
  `quality`, `timesync`). **Orphan**: nothing in the app runtime imports it (tests only).
- **`dashboard/app.py`** (786) / **`dashboard/data_provider.py`** (552) — a **second
  Flask app** (Step 19) with its own `main()`; can launch `ForwardTestingEngine` via
  `--engine`. **Not** registered by `web/app.py`.
- **`alerts/manager.py`** (1002), **`analysis/comparison.py`** (826),
  **`config_manager/manager.py`** (558) — orphan subsystems (tests only).
- **`cli.py`** (241) — argparse CLI: `list/run/compare/preflight/papertrade`.
- **`logging_config.py`** — process-wide logging + request-id contextvars.

### 2.10 Automated dependency map (`graph.txt`)

`graph.txt` (repo root) lists, for every `src/backtest` module, its internal imports and
its importers — produced by a 60-line `ast` script (handles `import X`,
`from X import Y`, relative imports, and resolves each name to a real module).
Highlights it confirms:

- **Orphan modules** (no in-package importer): `forward/live_engine`, `marketdata/*`
  (whole package), `alerts/*`, `analysis/*`, `config_manager/*`, `dashboard/*`
  (separate entry point), `cli` (entry point), and the 4 built-in strategies
  (imported *dynamically* by `strategy/registry._discover()`).
- `forward/live_engine.py` → imports `api.backtest` (engine importing from the API layer).
- `web/app.py` → all 7 blueprints + `brokers.session_manager` + `data.db_source` (lazy).
- Nothing in `web/` or `api/` (except `forward/engine`'s own deps) imports
  `simulator/` — the heavy execution core is **not** on the web request path.

---

## 3. Data Flow Diagrams

### 3.1 Strategy "upload" → dropdown

There is **no upload mechanism** — strategies are Python modules. Flow:

```mermaid
flowchart LR
  A["src/backtest/strategies/*.py\n(Strategy subclasses with @register\nvia Strategy.__init_subclass__)"] -->|imported dynamically| B["strategy/registry.py\n_discover() via pkgutil\n(registry._REGISTRY dict)"]
  B --> C["GET /api/strategies\napi/strategies.py\nget_all()"]
  B --> C2["GET /api/strategies/<name>/params\nget_params()"]
  C -->|JSON list| D["backtest.js / forward.js /\nportfolio.js → <select> dropdown"]
  C2 -->|param schema| E["params_form.js renders\nnumber inputs per param"]
```

Adding a strategy = drop a new `.py` into `src/backtest/strategies/`; discovery happens
lazily on the first registry call and survives broken modules (skipped + logged).

### 3.2 Backtest run → metrics/equity/drawdown to frontend

```mermaid
sequenceDiagram
  participant UI as backtest.js
  participant API as api/backtest.py<br/>POST /api/backtest/run
  participant SRC as data source<br/>(build_source)
  participant STR as strategy
  participant ENG as engine/Backtester
  participant ADP as BacktestAdapter

  UI->>API: {strategy, symbol, from_date, to_date, capital, params, timeframe}
  API->>API: _resolve_strategy → get_strategy(name)
  API->>SRC: get_candles(symbol, warmup_start, to_date, interval)
  Note over SRC: synthetic|csv|db|<br/>db: SQL market_data_cache + pandas resample
  SRC-->>API: OHLCV DataFrame (tz-naive, sorted)
  API->>STR: generate_signals(candles)
  STR-->>API: Series ∈ [-1,1]
  API->>ENG: run_on_candles → Backtester(cfg).run(candles, signals)
  Note over ENG: vectorized: held=signals.shift(1)<br/>+ risk path w/ intrabar stop/TP
  ENG-->>API: BacktestResult(equity, returns, position, metrics)
  API->>API: _trim_to_range(result, from, to)<br/>(force flat at first bar, re-base, recompute)
  API->>ADP: to_all() = to_metrics + to_equity + to_drawdown + to_trades + to_signals
  ADP-->>UI: JSON {metrics, equity, drawdown, trades, signals, config}
  UI->>UI: Chart.js renders cards + charts + trade table
```

Compare page: same, but `POST /api/backtest/run-many` → `ThreadPoolExecutor(max_workers
=min(4, slots))`, one `run_slot` per strategy/timeframe slot.

### 3.3 Forward test (single strategy) → signal → trade

**The web Forward page does NOT simulate — it replays a precomputed backtest.**

```mermaid
sequenceDiagram
  participant UI as forward.js
  participant API as api/forward.py
  participant ENG as engine (full backtest)
  participant FS as ForwardSession
  participant CLK as daemon thread _loop()
  participant ADP as BacktestAdapter

  UI->>API: POST /api/forward/start {strategy, symbol, mode, range, capital, speed}
  Note over API: mode=live ⇒ 403 unless BrokerSessionManager.is_authenticated()
  API->>ENG: _load_candles → run_on_candles(...) → _trim_to_range
  ENG-->>API: BacktestResult for the WHOLE window
  API->>FS: ForwardSession(result, bars_per_second)
  FS->>CLK: start daemon thread (TICK_SECONDS = 0.25 s)
  API-->>UI: {state_id, total, revealed=1, bars_per_second}
  loop every 2 s
    UI->>API: GET /api/forward/status?state_id=…
    API->>FS: snapshot()   (pure read, lock)
    FS->>FS: _prefix_result(n): slice candles/equity/position[:n],<br/>re-base, compute_metrics(prefix)
    FS->>ADP: to_metrics/to_equity/to_drawdown/to_trades
    FS-->>UI: {status, progress{revealed,total,pct}, metrics, equity,<br/>drawdown, trades, signals(≤ last revealed ts), positions, …}
  end
  Note over CLK: tick(0.25 s) → advance(⌊bars·speed·dt⌋) — reveals bars<br/>on the SERVER wall clock, even with no polling
  Note over FS: revealed == total ⇒ status=stopped (auto)
```

So the "signal → trade" on the web forward page is **not** an execution: `trades` come
from `walk_trades()` over the revealed prefix of the precomputed equity curve;
`positions` is the open leg of the prefix; the strategy is never re-run per bar.

**The four real (simulating) forward paths**, for contrast:

| # | Engine | Entry | Bar source | Trade execution |
|---|--------|-------|-----------|-----------------|
| A | `ForwardTestingEngine` (CLI/Docker) | `python -m backtest.forward.engine --config …` | `MarketDataHandler.get_latest_data()` (mock or mStock feed) or `_run_backtest_mode` replay | `StrategyAdapter.execute_signals` → `simulator.Order` → `OrderExecutor.execute` → `Fill` → `portfolio.apply_fill` (Decimal, fees, slippage, realism) |
| B | `paper.py` walk-forward (CLI `papertrade`) | `cli.papertrade_command` | historical `source.get_candles` once | `SimulatedBroker.step()` — exposure units, 0.08% cost, intrabar stops |
| C | `PortfolioManager` + `StrategyRunner` (web /portfolio) | `POST /api/portfolio/runner/create` | `SyntheticFeed` random walk @1 s | `PaperBroker.submit_market` → instant fill at bar close via `OrderLedger` |
| D | `LiveForwardEngine` (unwired) | `start_engine(state_id)` — called by nobody | 60 s poll of mStock minute API (or synthetic) | inline dict trade book, raw SQL (⚠ missing tables) |

### 3.4 Portfolio page → multi-strategy aggregation

```mermaid
flowchart TB
  subgraph FEED["SyntheticFeed (daemon thread, 1 s ticks, seeded random walk)"]
    B[emit_one → on_bar(symbol, bar)<br/>+ on_tick_end(tick_ts)]
  end
  subgraph MGR["PortfolioManager (process singleton, one RLock)"]
    OB["_on_bar: fan out to every runner<br/>whose config.symbols ∋ symbol"]
    TE["_on_tick_end: runner.on_tick_end (pool scan)<br/>+ _evaluate_risk (supervisor)"]
    SUM["get_portfolio_summary(): sum over<br/>runner.get_state() rows"]
  end
  subgraph RUNNERS["StrategyRunner × N (isolated cash buckets)"]
    R1["runner 1: process_candle_event →<br/>_signal_for (strategy on rolling 500-bar buffer)<br/>→ _act_on_signal → broker.submit_market"]
    RN["runner N …"]
  end
  subgraph EXEC["OrderLedger + PaperBroker"]
    L["PRT-{id}-{ms}-{seq} tag → apply_fill →<br/>route FillEvent to owning runner.on_fill"]
  end
  B --> OB --> R1
  B --> OB --> RN
  B --> TE
  R1 -->|submit_market| L -->|on_fill| R1
  RN --> L
  SUM -.->|SSE /api/portfolio/stream, 1 Hz<br/>+ REST control endpoints| UI["portfolio.js<br/>EventSource → grid of runner cards"]
  TE -->|breach| HALT["halt latch: pause all / emergency flatten all"]
```

Aggregation is a **plain sum of per-runner equity** (`_aggregate_equity`,
`get_portfolio_summary` line 299): `total_equity = Σ r.equity()`, daily PnL =
`Σ r.daily_pnl()`, drawdown vs portfolio peak, halt state from the supervisor.
Runners never share capital; the manager only aggregates and halts.

---

## 4. Data Model

Source of truth: `db/migrations/001_initial_schema.sql` (manual path,
`schema_migrations` table) ≡ `db/alembic/versions/…001…py` ≡ `src/backtest/db/models.py`
(ORM mirror; `create_all` used by `ForwardTestingEngine.initialize_system`).

### 4.1 ER diagram

```mermaid
erDiagram
  portfolios ||--o{ positions : "fk_positions_portfolio (CASCADE)"
  portfolios ||--o{ orders : "fk_orders_portfolio (CASCADE)"
  portfolios ||--o{ trades : "fk_trades_portfolio (CASCADE)"
  portfolios ||--o{ equity_curve : "fk_equity_portfolio (CASCADE)"
  portfolios ||--o{ performance_metrics : "fk_perf_portfolio (CASCADE)"
  portfolios ||--o{ strategy_signals : "fk_signals_portfolio (CASCADE)"
  portfolios |o--o{ system_logs : "fk_logs_portfolio (SET NULL)"
  positions ||--o{ orders : "fk_orders_position (SET NULL)"
  positions ||--o{ fills : "fk_fills_position (SET NULL)"
  positions |o--o{ trades : "fk_trades_position (SET NULL)"
  orders ||--o{ fills : "fk_fills_order (CASCADE)"
  orders |o--o{ trades : "fk_trades_entry/exit_order (SET NULL)"
  orders |o--o| strategy_signals : "fk_signals_order (SET NULL)"
  market_data_cache }o..|| null : "standalone (hypertable in prod)"
```

### 4.2 Tables & columns

**`portfolios`** (root aggregate; one row per forward-testing run)
`portfolio_id UUID PK` · `name VARCHAR(128) UNIQUE` · `initial_capital NUMERIC(20,4) >0` ·
`current_cash NUMERIC(20,4)` · `base_currency CHAR(3) 'INR'` · `status VARCHAR(16)
CHECK(active|paused|stopped)` · `created_at/updated_at TIMESTAMPTZ` (trigger on update).

**`positions`** (net open exposure; closed rows kept as history)
`position_id UUID PK` · `portfolio_id FK` · `symbol` · `exchange 'NSE'` ·
`position_type long|short` · `quantity NUMERIC(20,8)` (signed) ·
`average_entry_price` · `current_price` · `unrealized_pnl` · `realized_pnl` ·
`commission_total` · `opened_at` · `closed_at` · `last_updated` · `status open|closed` ·
CHECK: qty sign vs type; `closed ⇒ closed_at NOT NULL`;
**partial unique index** `uq_positions_one_open_per_symbol (portfolio_id, symbol) WHERE
status='open'`.

**`orders`** (lifecycle record)
`order_id UUID PK` · `portfolio_id FK` · `position_id FK NULL` · `symbol` · `side
buy|sell` · `order_type market|limit|stop|stop_limit|trailing_stop` · `quantity >0` ·
`filled_quantity ≤ quantity` · `limit_price` · `stop_price` · `trailing_amount` ·
`average_fill_price` · `time_in_force day|gtc|ioc|fok` · `status
pending|partial|filled|cancelled|rejected` · `rejection_reason` · `client_order_id
(partial-unique per portfolio)` · `broker_order_id` · `submitted_at/filled_at/
cancelled_at/updated_at`.
CHECKs: limit orders need limit_price, stops need stop_price, filled ⇒
`filled_at NOT NULL AND filled_quantity = quantity`, rejected ⇒ reason.

**`fills`** (append-only, immutable)
`fill_id UUID PK` · `order_id FK` · `position_id FK NULL` · `symbol` · `side` ·
`quantity >0` · `fill_price >0` · `commission ≥0` · `slippage_bps` (signed) ·
`slippage_amount` · `exchange_fees ≥0` · `regulatory_fees ≥0` · `liquidity_flag
maker|taker` · `reference_price` · `filled_at/created_at`.

**`trades`** (matched round trip, written on position close)
`trade_id UUID PK` · `portfolio_id FK` · `position_id FK NULL` · `symbol` ·
`strategy_name` · `direction long|short` · `entry_order_id FK` · `exit_order_id FK` ·
`quantity >0` · `entry_price/exit_price` · `entry_time/exit_time` (`exit ≥ entry`) ·
`gross_pnl` · `net_pnl` (= gross − commission − slippage) · `commission_total` ·
`slippage_total` · `holding_period_minutes` · `return_percentage` ·
`exit_reason (signal|stop_loss|take_profit|trailing_stop|time_stop|risk_limit|manual|
eod_flat)`.

**`equity_curve`** (mark-to-market snapshots; idempotent writer)
`equity_id BIGSERIAL PK` · `portfolio_id FK` · `ts TIMESTAMPTZ` · `total_equity` ·
`cash` · `position_value` · `daily_pnl` · `cumulative_pnl` · `drawdown` ·
`drawdown_pct` · **UNIQUE (portfolio_id, ts)**.

**`market_data_cache`** (OHLCV; **hypertable in prod** — `create_hypertable` is applied
manually per `docs/DATABASE.md`, not in the migration file)
`data_id BIGSERIAL PK` · `symbol` · `exchange` · `timeframe CHECK(1min|3min|5min|15min|
30min|60min|1hour|day|week|month)` · `ts TIMESTAMPTZ` (bar OPEN time) ·
`open/high/low/close NUMERIC(20,8) >0` · `volume ≥0` · `bid/ask` (bid ≤ ask) ·
`source 'mstock'` · `ingested_at` · **UNIQUE (symbol, exchange, timeframe, ts)** ·
OHLC-sanity CHECKs.

**`performance_metrics`** (daily rollup, safe to rebuild)
`metric_id BIGSERIAL PK` · `portfolio_id FK` · `calculation_date DATE` ·
`total_trades/winning_trades/losing_trades` · `win_rate` · `avg_win/avg_loss` ·
`largest_win/largest_loss` · `profit_factor` · `expectancy` · `sharpe_ratio` ·
`sortino_ratio` · `max_drawdown(_percentage)` · `total_return(_percentage)` ·
`total_commission` · `total_slippage` · `calculated_at` · UNIQUE (portfolio_id, date).

**`strategy_signals`** (audit log; feeds look-ahead-bias detection)
`signal_id BIGSERIAL PK` · `portfolio_id FK` · `symbol` · `strategy_name` ·
`signal_type entry|exit` · `direction long|short|flat` · `strength ∈[0,1]` ·
`target_position ∈[-1,1]` · `bar_ts` (completed bar open time; must precede
`generated_at`) · `generated_at` · `indicators_snapshot JSONB (GIN index)` ·
`executed BOOL` · `order_id FK NULL` · `skip_reason`.

**`system_logs`**
`log_id BIGSERIAL PK` · `portfolio_id FK SET NULL` · `ts` · `log_level` ·
`component` · `message` · `stack_trace` · `context JSONB`.

### 4.3 Tables referenced but NOT defined in the repo

| Table | Referenced by | Status |
|---|---|---|
| `forward_test_state` | `forward/live_engine.py` (raw SQL, lines 253/323) | **no DDL anywhere** → path broken at runtime |
| `forward_test_trades` | `forward/live_engine.py` (272/359/383/401) | **no DDL anywhere** |
| `forward_test_equity` | `forward/live_engine.py` (439) | **no DDL anywhere** |
| `instruments` | README/DATABASE.md (154K mStock instruments); `api/data_manager.py:_load_instruments` reads it | not in any migration — created outside the repo |

Note: there are **no** `strategies`, `backtest_results`, or `forward_runs` tables —
backtest results are ephemeral (JSON responses / in-memory sessions); strategies are
Python code; the only strategy-adjacent table is `strategy_signals` (and
`trades.strategy_name`). "Forward runs" exist only as in-memory `ForwardSession`s
(web), `PortfolioManager` runners (web portfolio), or `portfolios` rows (engine A/CLI).

---

## 5. Execution / Engine Flow (function-by-function)

### 5.1 When a backtest is triggered (`POST /api/backtest/run`)

```
api/backtest.py::run_backtest()                                   # line 202
├─ _resolve_strategy(name) → strategy/registry.get_strategy(name)
│   └─ _discover(): pkgutil scan of backtest.strategies (idempotent)
├─ _interval(timeframe)            # "1D"→"day", unknown→"day"+warning
├─ _candles(symbol, warmup_start, to_date, interval)
│   └─ runner.build_source(name)   # from app.config["BACKTEST_SOURCE"]
│       └─ SyntheticSource | CsvSource | MStockSource | DbSource
│           └─ (DbSource) _find_best_source_tf → raw SQL SELECT … market_data_cache
│              → _resample (pandas) → normalize_candles
├─ _check_params(strategy_cls, params)     # log-only schema check
├─ runner.run_on_candles(candles_full, strategy, params, symbol, BacktestConfig(capital))
│   ├─ strategy = strategy_cls(**params)          # Strategy.__init__ validates
│   ├─ _effective_config: per-strategy stop_loss/take_profit → cfg
│   ├─ signals = strategy.generate_signals(candles)   # pd.Series, full window
│   └─ engine/backtester.Backtester(cfg).run(candles, signals)      # line 39
│       ├─ target = reindex+clip(-1,1)
│       ├─ if stop_loss/take_profit: _run_with_risk(candles, target) # line 100
│       │     for each bar: desired = lagged_target (shift(1))
│       │       entry at prev_close; intrabar stop/TP check on high/low;
│       │       forced exit → held=0, blocked=True, extra cost
│       │       net = held·(end/prev_close − 1) − turnover·(comm+slippage)
│       │       equity *= (1 + net)                       # per-bar loop
│       └─ else: _run_vectorized(candles, target)         # line 89
│             held = target.shift(1).fillna(0)
│             gross = held·close.pct_change(); turnover = |held.diff()|
│             equity = capital·cumprod(1 + gross − turnover·cost)
│       └─ compute_metrics(result)   # engine/metrics.py:61 → sharpe/dd/trades…
│             └─ engine/trades.walk_trades(equity, position) + trade_stats
├─ _trim_to_range(result, from_date, to_date)              # api/backtest.py:126
│     mask by date string; position.iloc[0]=0; re-base equity to capital;
│     recompute compute_metrics; re-stamp strategy metadata
└─ adapters/backtest_adapter.BacktestAdapter(result).to_all()
      ├─ to_metrics / to_equity / to_drawdown (equity/cummax) /
      │  to_trades (walk_trades over the trimmed run) / to_signals
      └─ jsonify(payload) → 200
```

### 5.2 When a forward-test bar/tick arrives

**(i) Web Forward page (ForwardSession)** — bars are not "arriving" events; the daemon
thread `_loop()` (api/forward.py:194) calls `tick(0.25 s)` → `advance(⌊dt·speed⌋)` →
increments `_revealed`. Nothing executes; the next `GET /status` → `snapshot()` (line
352) → `_prefix_result(n)` (line 257) slices the precomputed series and recomputes
metrics. The strategy **never sees a bar at runtime**.

**(ii) `ForwardTestingEngine.run_loop()`** (forward/engine.py:1005) per loop iteration:

```
data_handler.get_latest_data()            # live/market_data_handler.py
  └─ per symbol: feed.get_latest_tick → normalize_tick → BarBuilder.add_tick
     → closed bar? (market closed bars flushed at EOD / force_close)
validator.validate(market_data)           # live/data_validator.py (OHLC sanity, gaps)
portfolio.update_prices({sym: close})     # simulator/Portfolio — mark-to-market
stop_manager.check_stops(market_data)     # simulator/stop_manager
for sym, bar in market_data: adapter.on_bar_close(bar)
  └─ strategy_adapter.py:601
     ├─ _append_bar(sym, normalized)      # per-symbol DataFrame (≤5000 bars)
     ├─ generate_signals(symbol)          # :732
     │    ├─ strat.generate_signals(candles)        # FULL history, completed bars only
     │    ├─ target = series.iloc[-1], clip [-1,1] (long-only ⇒ max(0,·))
     │    ├─ _decide_action(current, target) → BUY/SELL/HOLD + entry/exit
     │    └─ _save_signal_to_db(Signal, executed=…)  # strategy_signals row
     └─ execute_signals(signals, market_data=bar)    # :985
          ├─ sizer.calculate_position_size(signal, portfolio, current_price)
          ├─ portfolio.can_open_position(sym, qty, price)  (reject → skip_reason)
          ├─ Order(**kwargs).validate().submit(); portfolio.add_order(order)
          └─ executor.execute(order, snapshot=bar)        # simulator/execution.py:614
               ├─ realism checks (market open, halted, latency, liquidity)
               ├─ process_market/limit/stop_order → fill_price (slippage applied)
               ├─ fees = CommissionCalculator (model per realism profile)
               ├─ Fill → portfolio.apply_fill(fill) → position/cash update
               └─ event callbacks
performance.update_metrics(portfolio)     # simulator/performance.py → equity_curve
state_manager.save_state(engine)          # every N minutes (atomic JSON)
```

`_run_backtest_mode()` (engine.py:1098): same per-bar body, but bars come from
`data_source.get_candles(symbol, start, end, timeframe)` iterated in order, injected
via `data_handler.inject_bar(bar)`.

**(iii) `PortfolioManager` tick** (portfolio_manager.py):

```
SyntheticFeed._loop (1 s) → emit_one()
├─ per symbol: _make_bar (seeded random walk) → on_bar(symbol, bar)
│   └─ PortfolioManager._on_bar (line 227)  [under manager RLock]
│        for runner in runners where symbol ∈ runner.config.symbols:
│            runner.process_candle_event(symbol, bar)      # runner.py:229
│              ├─ de-dup by ts; append to deque(500)
│              ├─ _roll_trading_day(ts); _mark_to_market()
│              └─ SINGLE target: _process_single → _signal_for(sym)  # :393
│                   ├─ _bars_to_frame(deque)  (RangeIndex DataFrame)
│                   ├─ strategy.generate_signals(df).iloc[-1]  → {-1,0,1}
│                   └─ _act_on_signal(sym, sig, bar.close, bar.ts)  # :419
│                        └─ _emit_entry/_emit_close → broker.submit_market(...)
│                             └─ OrderLedger.submit → coid PRT-… → apply_fill(price)
│                                  └─ runner.on_fill(FillEvent)
│                                       ├─ BUY: cash −= qty·price; positions[sym]=…
│                                       └─ SELL: cash += …; realized_pnl; closed_trades
│                                       └─ _mark_to_market(record=True)
└─ on_tick_end(tick_ts)
    └─ PortfolioManager._on_tick_end (line 242)
         ├─ runner.on_tick_end (POOL runners): _process_pool — scan all basket
         │    symbols, exits first, rank entries by _entry_score, top-K ≤
         │    max_pool_positions, then _check_instance_risk
         └─ _evaluate_risk → RiskSupervisor.evaluate(runners, equity, peak, daily_pnl)
              └─ breach ⇒ halt latch; pause all (or flatten all if drawdown breach)
```

**(iv) `LiveForwardEngine._run_loop()`** (live_engine.py:513, 60 s):
`_tick_live`: `_is_market_open()` (IST 09:15–15:30 weekdays) → `_fetch_latest_bar`
(mStock minute endpoint, last of yesterday..today) → `_process_bar(bar)` (line 450):
ts de-dup → rolling 200-bar list → DataFrame → `strategy.generate_signals(df).iloc[-1]`
→ inline long/short book: entry `qty = int(capital·0.95/price)`, `_save_trade` (raw
SQL), exit `_close_trade` (raw SQL PnL update), `_save_equity` (raw SQL insert).

### 5.3 When a signal fires and a trade is placed

| Path | Signal object | Order object | Fill | Fees/slippage | Position state |
|---|---|---|---|---|---|
| Backtest (vectorized/risk) | raw Series value | **none** | implicit at next bar's prev_close | `commission_pct + slippage_pct` on turnover only | exposure float ∈ [-1,1] |
| Forward web (replay) | precomputed (never re-fired) | **none** | precomputed | precomputed | precomputed |
| paper.py walk-forward | precomputed shifted Series | **none** | `SimulatedBroker.step` at prev_close | flat 0.08% of turnover | exposure float |
| ForwardTestingEngine | typed `Signal` (DB row) | `simulator.Order` (validated, submitted, DB-mapped) | `OrderExecutor` → `Fill` (DB row) | fee model + slippage model, realism-dependent | `simulator.Portfolio`/`Position` (Decimal, signed, lots) |
| PortfolioManager runner | int in {−1,0,1} (no object) | ledger `Order` (in-memory, coid-tagged) | `PaperBroker` instant at bar close | slippage_pct = 0 by default | runner dict (floats) |

**Key asymmetry**: only path (ForwardTestingEngine) goes through the real
order/fill/fee machinery and writes `orders`/`fills`/`trades`/`strategy_signals` rows.
The other four paths produce trade *lists in JSON* or equity-curve numbers, and none of
them can write to the `orders`/`fills` tables (the web forward path has no DB writes at
all — `ForwardSession` is pure in-memory).

---

## 6. State & Time Management

### 6.1 How the engine advances time

| Mode | Time source | Granularity |
|---|---|---|
| Backtest | **Candle index only** — vectorized: no time at all (whole frame at once); risk path: sequential `iterrows()` over the same frame. "Now" = current row. | bar |
| Forward web replay | **Server wall clock**: `threading.Event` + `TICK_SECONDS=0.25 s` daemon; `bars_per_second` (default 1, max 5000, 0 = frozen/manual). Progress = `_revealed` cursor. | wall-clock |
| ForwardTestingEngine | `time.sleep(loop_interval_seconds)` (config, default 1 s) in `run_loop`; in `backtest_mode`, wall clock only paces the replay (`sleep(interval·0.1)`) while bars iterate in order. | wall-clock |
| LiveForwardEngine | `stop_event.wait(60)` — 60 s poll; market-hours gate in IST; bar ts comes from the mStock payload. | 60 s |
| Portfolio feed | `stop.wait(tick_seconds)` — 1 s synthetic tick; bar ts = wall clock. | 1 s |

No mode uses a simulated-trading clock that decouples bar time from wall time (except
the replay cursor, which is purely a reveal counter). There is no shared
`TradingCalendar`/time-advance abstraction between engines.

### 6.2 How OHLCV is fetched from TimescaleDB

1. **Ingestion** (write side): `scripts/fetch_nifty500_historical.py` (or the web Data
   page → `api/data_manager.py::_run_fetch_job` → `_fetch_bars_chunked` →
   `_persist_bars`) pulls mStock candles and upserts into `market_data_cache`
   (`UNIQUE (symbol, exchange, timeframe, ts)`); `create_hypertable('market_data_cache',
   'ts')` is applied **manually** in prod (docs/DATABASE.md) — the migration files do not
   contain hypertable DDL.
2. **Read side** (backtest/compare/forward-web, `--source db`): `DbSource.get_candles`
   → raw SQL `SELECT ts, open, … WHERE symbol AND timeframe AND ts BETWEEN …` →
   `_find_best_source_tf` (prefer `day` if requested day; else probe 1min→1hour for the
   finest stored) → **pandas `resample` up** (open=first, high=max, low=min, close=last,
   volume=sum) → `normalize_candles` (lowercase cols, tz-naive DatetimeIndex, sorted,
   deduped). The TimescaleDB chunking/hypertable is only used opaquely by the `BETWEEN`
   scan; no continuous aggregates, no `time_bucket`.
3. **Other readers bypass `DbSource` entirely**: `LiveForwardEngine._load_historical_bars`
   does its own raw SQL (`timeframe IN ('1min','minute') ORDER BY ts DESC LIMIT 200` —
   note: `'minute'` is not a legal timeframe value in the schema), and
   `api/symbols.py`/`api/data_manager.py` do their own queries.

### 6.3 Orders: simulated vs real

- **Simulated (all current paths)**:
  - Backtest: no orders — pure exposure arithmetic.
  - `SimulatedBroker` (paper.py): fills at prev_close, 0.08% cost, intrabar stops.
  - `OrderExecutor` (simulator/): full order lifecycle with realism presets
    (instant/paper/realistic), latency, liquidity, limit/stop handling, fee + slippage
    models — but always *simulated fills*; `broker_order_id` stays NULL.
  - `PaperBroker` (portfolio): instant fill at bar close, 0 slippage by default.
- **"Real" (auth only)**: `brokers/mstock.py::MStockBroker` implements login/TOTP/
  status/logout **only** — no `place_order`/`cancel` methods exist anywhere in the
  repo. `MStockBrokerFeed`/`MStockSource` fetch data. So real order flow = **not
  implemented**; the auth layer exists to gate live-mode starts and to fetch data.
- `ForwardTestingEngine` has a `db_manager` hook and `simulator` order/fill DB mapping,
  but the CLI path only writes `strategy_signals`/`equity_curve` in practice (fills are
  applied in-memory unless `save_to_db` is called).

### 6.4 State persistence matrix

| State | Where | Survives refresh | Survives restart |
|---|---|---|---|
| Web forward sessions (`ForwardSession`) | in-process `OrderedDict` (max 20) | ✅ | ❌ |
| Portfolio runners/manager | in-process singleton | ✅ | ❌ (documented "V1 in-memory, persistence V2") |
| ForwardTestingEngine | `state/forward_test_state.json` (atomic write, every N min + on stop) | ✅ | ✅ |
| LiveForwardEngine | PostgreSQL `forward_test_*` tables | ✅ | ✅ **but tables missing** |
| paper.py live mode | `.live_papertrade_state.json` (per `--state`) | ✅ | ✅ |
| Backtest results | nowhere (JSON response only) | — | — |
| Broker session | `BrokerSessionManager` in-memory + TTL | ✅ | ❌ |

---

## 7. Identified Weak Points (no fixes applied)

### 7.1 Backtest vs forward-test logic divergence / duplication

1. **Five parallel execution semantics.** Backtest vectorized path
   (`engine/backtester.py:89`), risk path (`:100`), `SimulatedBroker.step`
   (`forward/broker.py:26`), `OrderExecutor` (`simulator/execution.py:614`), and
   `PaperBroker` (`forward/order_ledger.py:277`) each implement their own fill/cost/stop
   rules with different cost models: 0.03% comm + 0.05% slip (vectorized), 0.08% flat
   (walk-forward), fee+slippage *models* with realism presets (simulator), 0 (portfolio).
   A strategy's backtest P&L and its forward P&L are not produced by shared code.
2. **The web "Forward Test" is not a forward test.** `api/forward.py` runs the *whole*
   backtest up front (`start()`, line 634 → `run_on_candles`) and then merely reveals
   the precomputed equity/position series. There is no per-bar execution, no order
   lifecycle, no DB write, and the strategy is never re-run. It answers "how would this
   backtest have looked bar-by-bar", not "how would the strategy have traded bar-by-bar".
3. **Fill-timing rules disagree (lookahead surface).**
   - Backtest: signal@t held from t+1 (`shift(1)`), fill at bar t+1's prev_close.
   - `StrategyRunner` (`forward/runner.py:393` → `:419`): signal computed from a buffer
     *including the just-closed bar*, executed at that same bar's close (same-bar fill).
   - `StrategyAdapter` (`forward/strategy_adapter.py:601`): docstring claims "orders are
     executed on the next tick", but `execute_signals` fills against the just-closed
     bar's snapshot when `market_data` is supplied.
   - `LiveForwardEngine._process_bar` (`live_engine.py:450`): signal from a window
     including the current bar, traded at the current bar's close.
   So "no lookahead" is enforced by three different, not mutually consistent mechanisms
   (`shift(1)`; "completed bars only" + same-bar fill; ts de-dup).
4. **Copy-paste across API files.** `_TIMEFRAME_TO_INTERVAL` (api/backtest.py:37 ≡
   api/forward.py:51), `WARMUP_BARS = 0` (both), and `_trim_to_range`
   (api/backtest.py:126 ≡ api/forward.py:579) are duplicated verbatim; the two copies
   can drift.
5. **Position/unit model fragmentation.** Exposure units ∈ [-1,1] (backtest,
   SimulatedBroker, StrategyAccount), float share quantities (StrategyRunner,
   LiveForwardEngine), signed `Decimal` lots (`simulator.Portfolio/Position`). No common
   "position" abstraction; conversions are implicit (`qty = capital·0.95/price` vs
   `budget = cash·pct`).
6. **Two "Portfolio" types + a third aggregate.** `forward/portfolio.py::Portfolio`
   (StrategyAccount), `simulator/portfolio.py::Portfolio` (Decimal, DB), and
   `PortfolioManager`'s dict-based rollup — plus `db.models.Portfolio`. Names collide
   (`from backtest.forward.portfolio import Portfolio` vs
   `from backtest.simulator.portfolio import Portfolio`).
7. **`forward_test_*` phantom tables.** `live_engine.py` depends on
   `forward_test_state/trades/equity` which no migration, alembic revision, or
   `create_all` creates — that engine path raises on first `_load_state`.

### 7.2 Where multi-strategy parallelism is blocked today

1. **Backtest:** `run-many` hard-caps at **4 slots** (api/backtest.py, `len(slots) > 4`
   → 400) with `ThreadPoolExecutor(max_workers=min(4, slots))` — GIL-bound pandas work,
   no process pool, no async.
2. **Web forward:** one `ForwardSession` = one strategy + one symbol
   (`api/forward.py:92`); there is no endpoint to start a multi-strategy forward run.
   `_ACTIVE_ID` makes "the" session singular; sessions cap at 20, in-memory only.
3. **CLI forward:** `ForwardTestingEngine` config carries a single
   `strategy.name` (one strategy per engine); multi-strategy CLI is only the
   precomputed walk-forward in `paper.py` (no per-strategy order lifecycle).
4. **Portfolio engine (the multi-strategy path)** — parallelism exists structurally
   (N runners, isolated buckets) but is serialized by:
   - one manager-wide `RLock` held for **every** bar dispatch, pool scan and risk pass
     (`portfolio_manager.py:227/242`); all N strategy evaluations run sequentially
     inside one feed thread;
   - one `SyntheticFeed` thread ticking every 1 s (single global clock);
   - in-memory only: no process/worker scaling, no persistence.
5. **Shared singletons**: `get_portfolio_manager()`, `_SESSIONS`, `get_session_manager()`
   — everything lives in one Flask process (dev server, `use_reloader=False`).

### 7.3 Where live & historical feeds are handled inconsistently

1. **Three market-data stacks** (plus a fourth ad-hoc):
   - `data/` sources (`SyntheticSource/CsvSource/MStockSource/DbSource`) — backtest &
     web forward replay;
   - `live/market_data_handler.py` (`BrokerFeed`/`BarBuilder`) — ForwardTestingEngine;
     default provider is **mock**;
   - `marketdata/` (Tick/BarAggregator/MockFeed/MStockFeed/quality/timesync) — **orphan
     package**, not imported by any engine (tests only);
   - `live_engine.py` — raw `requests` + raw SQL, no validator, no aggregator.
2. **The portfolio engine trades a random walk.** `PortfolioManager` is hard-wired to
   `SyntheticFeed` (`portfolio_manager.py:67`); there is no wiring to
   `DbSource`, the mStock feed, or `marketdata/` — so "live" multi-strategy testing
   consumes seeded random numbers.
3. **`mode=live` on the web Forward page is an auth gate, not a data mode.**
   `api/forward.py` `start()`: `mode` only decides whether the broker-auth guard fires;
   the replay always runs on historical candles from `--source`. No live bar is ever
   consumed by the web forward page.
4. **Timeframe naming drift.** Web maps `1H→"hour"`, `4H→"4hour"` (api/backtest.py:37),
   but `DbSource` stores/probes `"1hour"`, `"60min"` and its resample table has no
   `"hour"`/`"4hour"` key — so `1H` on db source silently returns the finest stored
   bars unsampled (warning only). Synthetic/CSV sources ignore timeframe entirely
   (documented gap G6). `live_engine` queries `timeframe IN ('1min','minute')` —
   `'minute'` violates the schema CHECK.
5. **TZ handling split.** `market_data_cache.ts` is `TIMESTAMPTZ`; `normalize_candles`
   passes tz through untouched while the project data contract (PROJECT-CONTEXT.md) is
   "tz-naive DatetimeIndex"; `ForwardSession` compares bar dates as strings
   (`c["date"] <= cutoff_str`); `live_engine` compares raw ts strings; the feed uses
   wall-clock UTC. No single time-zone authority.
6. **Three DB-URL resolution paths.** `db/config.py` (yaml + `FORWARD_TEST_DB_*`
   env → `DatabaseManager`), `DbSource` (`DATABASE_URL` → `FORWARD_TEST_DB_URL` →
   hardcoded `postgres:postgres@localhost:5432/forward_test`), and
   `live_engine` (`FORWARD_TEST_DB_URL` → empty default). Two apps in the same process
   can point at different databases.
7. **Hypertable assumption is doc-only.** Migrations create plain tables; README/
   DATABASE.md describe `market_data_cache` as a hypertable. A fresh DB from migrations
   has no chunking, yet all queries are written as if the time range scan is the fast
   path.
8. **`instruments` table is out-of-band** (README says 154K rows; `data_manager` reads
   it; no DDL in repo) — schema state cannot be reconstructed from the repo alone.

### 7.4 Other structural issues worth flagging for the refactor

- **Orphaned subsystems** (imported only by tests, not by the app): `marketdata/`
  (~2,850 ln), `alerts/` (1,002), `analysis/` (826), `config_manager/` (558),
  `forward/live_engine.py` (697, broken tables), `dashboard/` (1,338, second Flask app
  shadowing the unified one), the 4 strategies only via dynamic discovery.
- **Layering violations:** `forward/live_engine.py` imports `api.backtest._interval`
  (engine → API); `api/data_manager.py` owns ingestion job threads + raw SQL
  (API → DB internals); `web/app.py` lazily imports `data.db_source` for a boot log.
- **Two Flask apps** with overlapping routes and ports (both default 5000); the
  Dockerfile CMD runs `backtest.forward.engine` (CLI), not the web app — the artifact
  and the default runtime disagree.
- **Config sprawl:** 16 YAML files in `config/` of which the runtime reads
  `forward_testing.yaml`, `database.yaml` (+ a few referenced by
  `config_manager`/`simulator` loaders); the rest are aspirational (alerts, calendar,
  quality, time_sync…).
- **In-process concurrency on the Flask dev server:** forward clock threads, feed
  thread, SSE stream, broker expiry monitor, and data-fetch job threads all share one
  process; no queues, no worker model — a 50-runner portfolio + SSE clients run in
  whatever thread the dev server gives them.
- **Metrics recomputation per poll:** `ForwardSession.snapshot()` recomputes
  `compute_metrics` + `walk_trades` over the revealed prefix on every `/status` (2 s
  poll per tab); the portfolio SSE serializes every runner state every second. Both
  scale linearly with run length / runner count per request.
- **`graphify-out/`** (pre-existing AST graph cache, 4,394 nodes) is checked in; the
  fresh `graph.txt` in this blueprint is the minimal replacement for dependency
  mapping.

---

## Appendix A — Entry points

| Entry point | Command | What it runs |
|---|---|---|
| Web UI | `PYTHONPATH=src python -m backtest.web.app --source db` | unified Flask app (all 6 pages) |
| CLI | `PYTHONPATH=src python -m backtest run|compare|list|preflight|papertrade` | cli.py (backtest + walk-forward) |
| Forward engine | `PYTHONPATH=src python -m backtest.forward.engine --config config/forward_testing.yaml` | ForwardTestingEngine (Dockerfile CMD) |
| Dashboard (legacy) | `PYTHONPATH=src python -m backtest.dashboard.app [--engine]` | second Flask app |
| systemd | `forward_testing.service` | forward engine as a service |

## Appendix B — Key environment variables (`.env.example`)

`MSTOCK_API_KEY`, `MSTOCK_USERNAME`, `MSTOCK_PASSWORD`, `MSTOCK_CHECKSUM`,
`MSTOCK_AUTH_MODE`, `MSTOCK_BASE_URL`, `MSTOCK_SESSION_TTL_MINUTES`,
`FORWARD_TEST_DB_URL`, `FORWARD_TEST_DB_PROFILE`, `FORWARD_TEST_DB_ECHO`,
plus `BACKTEST_SOURCE`, `BACKTEST_CURRENCY`, `BACKTEST_LOG_LEVEL(_FILE)`,
`FORWARD_REPLAY_SPEED` (read by `web/app.py`).

## Appendix C — Test surface (where behaviour is pinned)

- Engine/math: `test_backtest.py`, `test_engine_trades.py`, `test_exits.py`,
  `test_compare.py`, `test_comparison.py`, `test_plotting.py`.
- Forward: `test_forward.py` (paper), `test_forward_engine.py` (engine A),
  `test_forward.py` + `test_api_forward.py` (web replay), `test_portfolio_engine.py`
  (manager), `test_circuit_breakers.py`, `test_strategy_adapter.py`.
- Simulator: `test_simulator_*` (11 files).
- API/web: `test_api_backtest/strategies/forward/portfolio/broker_auth.py`,
  `test_web_components.py`, `test_dashboard.py`, plus `tests/js/*.mjs` (36 assertions).
- DB: `test_db_manager.py`, `test_db_schema.py`, `test_e2e_instruments_to_db.py`.