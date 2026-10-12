# Back-Test

An algorithmic trading platform for Indian markets: backtest, compare, forward-test (paper), and trade strategies across equity and NIFTY/BANKNIFTY options — with portfolio-level risk, order management, and portfolio intelligence watching the combined book.

## Current Status (2026-10-06)

| Area | Status |
|------|--------|
| Backtest / Compare engine | ✅ Production — deterministic, request-id logging, 4,849 tests collected |
| Backtest run ledger | ✅ Live — every completed run/compare persists full payload + provenance (`api/backtest_run_store.py`, migration 018, merged 2026-10-03) |
| Data tab & instrument lists | ✅ Live — names-only picker, curated NIFTY 200 + NSE index universes, dedupe + 502-retry + holiday-aware fetch, per-symbol coverage (PRs #39/#40/#42); 1-min ingest in progress |
| Broker session-expiry alerts | ✅ Live — in-app widget + Telegram + re-login popup when an mStock/Dhan session expires (PR #41) |
| Market-data cache clock | ✅ Repaired — naive bars bound to IST and stored as aware UTC (`data/base.bar_timestamp`), 45.6M rows migrated (f2eb7e3); NSE holiday table seeded 2022–2026, fetches skip closures |
| Forward testing (paper) | ✅ Production — server-side replay clock, equity + options, persistence across restarts |
| Options (paper, live, backtest) | ✅ Paper + live complete — 9/9 PRD phases; atomic multi-leg execution, Greeks, full statutory fee stack, expiry handling. Backtest frozen (see below) |
| Playbooks (unified trading) | ✅ Live — declarative option-strategy configs spawn runners; risk envelope with "estimated" badge |
| Live Order Management | ✅ Live — position actions (SL/TP/Close) + Orders ledger tab with cancel, amend-at-venue, aging alerts, bounded auto-retry |
| Risk management | ✅ Live — 3 tiers: global breakers, independent per-bucket breakers, `/risk` monitoring page |
| Portfolio Intelligence & Alerts | ✅ Live — portfolio Greeks in ₹, concentration, correlation, VIX regime, pub/sub AlertBroker with strategy hooks, DB persistence (migration 005) |
| Strategy Analytics | ✅ Live — `/analytics` per-strategy performance suite + extended `/compare` |
| Parameter Optimization | ✅ Live — grid/random/Bayesian/genetic search with walk-forward validation, sensitivity analysis, robustness scoring |
| **Consolidated P&L & Tax Reporting** | **✅ Live — single statement across all brokers/books, correct Indian tax classification, estimated tax, exports (PDF/xlsx/csv), contract-note reconciliation, monthly email reports** |
| Multi-broker sessions | ✅ mStock + Dhan — registry-based, per-broker login via auth modal, **concurrent per-broker sessions** (session manager v2); one broker is *UI-active* at a time |
| Feed-quality monitoring | ✅ Live — staleness/gaps/repeats/error-rate per (broker, symbol), durable JSONL log, restart-proof |
| Data sources | ✅ Synthetic, CSV, mStock, Dhan; PostgreSQL/TimescaleDB cache — curated NIFTY 200 + NSE index universes at 1-min (coarser timeframes resampled at read time) |
| Multi-broker *concurrent* sessions + segmented trading | ✅ Shipped — Phases A–D (`docs/MULTI-BROKER-PRD.md`): concurrent sessions, segments & routing, per-broker execution, cross-broker risk; real-credential smoke test remains |
| Live broker fill polling (F-12) | ✅ Shipped — `forward/live_gateway.py` + `poll_fill` wired: working live orders are polled, only the not-yet-booked fill delta is applied (live orders still report `placed`) |
| Live Dhan order contract (`BrokerOrderBase`) | ✅ Shipped — `brokers/dhan.py` implements place/modify/cancel/order-book/poll_fill (Phase C) |

## What It Does

**Test trading strategies before risking real money — then drive them live with human overrides.** Feed it historical OHLCV candle data, pick a strategy, and the engine simulates trades; graduate to forward paper-testing on a live clock; arm live runners with portfolio-wide risk breakers, manual position controls, and an intelligence layer watching the combined book.

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
| **Options Backtest** | *(Python API)* Model-driven options backtesting: the same expression layer (view → selector → structure → intent) run bar-by-bar over a candle frame with synthetic Black-Scholes pricing — **frozen** (data-source routing decision 2026-10: backtests are equity-only, see below) |
| **Order Management** | Manual steering wheel on the Command Center — per-position actions (Modify SL / Target / Close 50% / Close All) + an Orders ledger tab with cancel, amend-at-venue, aging alerts, slippage and bounded auto-retry (see below) |
| **Multi-Broker** | Concurrent per-broker sessions (mStock and Dhan behind one auth contract) with segments & routing, per-broker execution and cross-broker risk (Phases A–D shipped) — see [docs/MULTI-BROKER-PRD.md](docs/MULTI-BROKER-PRD.md); feed-quality monitoring on every live bar (see Data Sources) |
| **Portfolio Intelligence** | Risk layer above individual strategies: portfolio Greeks in ₹ with scenario revaluation, concentration (underlying / group / strike clusters), strategy P&L correlation, volatility-regime fit, and a pub/sub alert system strategies can subscribe to — see [docs/PORTFOLIO-INTELLIGENCE.md](docs/PORTFOLIO-INTELLIGENCE.md), [docs/ALERTS-GUIDE.md](docs/ALERTS-GUIDE.md) |
| **Risk** | 3-tiered risk management & monitoring — global circuit breakers (daily loss, drawdown, leverage), per-bucket breakers with independent halts, and a live `/risk` page + dashboard risk strip |
| **Analytics** | `/analytics` — per-strategy performance suite (P&L attribution, trade stats, equity behaviour, risk ratios, edge degradation detection) plus an extended `/compare` — see [docs/STRATEGY-PERFORMANCE-ANALYTICS.md](docs/STRATEGY-PERFORMANCE-ANALYTICS.md) |
| **Optimization** | Systematic parameter search (grid/random/Bayesian/genetic) with walk-forward validation, sensitivity analysis, robustness scoring, and one-click apply to runners — see [docs/STRATEGY-PERFORMANCE-ANALYTICS.md](docs/STRATEGY-PERFORMANCE-ANALYTICS.md), [docs/OPTIMIZATION-ENGINE.md](docs/OPTIMIZATION-ENGINE.md) |
| **Reporting** | Consolidated P&L across all brokers/books, correct Indian tax classification, estimated tax liability, exports (PDF/xlsx/csv), contract-note reconciliation, monthly email reports — see [docs/WEB-UI.md](docs/WEB-UI.md) §4 |

## Built-In Strategies

| Strategy | Logic |
|----------|-------|
| **Buy & Hold** | Buy once, hold forever — baseline benchmark |
| **SMA Crossover** | Buy when fast MA crosses above slow MA |
| **RSI Reversion** | Buy oversold, sell overbought (mean-reversion) |
| **Donchian Breakout** | Buy on new highs, sell on new lows (momentum) |
| **Price Move** | Buy/sell based on price movement threshold (e.g. ₹5) |
| **Directional Options** | EMA momentum → bullish/bearish `MarketView` — feeds the options expression layer (long call/put, spreads) |
| **Plugins** | Drop-in strategies in `plugins/strategies/` (e.g. `ema_reversion_pob`, `atm_instant_buy`) — loaded automatically, each entry publishes its `eligible_instruments` for the UI dropdown. The 2026-10 swing battery (`swing_*` plugins) failed its gates — nothing from it was deployed |

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

- **Paper:** Portfolio command center — Positions tab (structures as one net row
  with legs, Δ/Θ, manual SL/target), Playbooks tab (configs + Deploy), Greeks grid
  and expiry alerts under Risk & intelligence. The old standalone `/options` page
  was removed (owner decision, GAP-3); the manual trade UI now lives here
- **Forward (automated):** the forward engine's options bridge trades
  structures on an isolated paper book, one open structure at a time;
  open structures persist across server restarts and are rehydrated on
  startup
- **Live:** `LiveOptionTrader(dry_run=True)` first — logs payloads, places nothing
- **Docs:** [docs/OPTIONS-PAPER-LIVE.md](docs/OPTIONS-PAPER-LIVE.md),
  [docs/OPTIONS-BACKTEST-PRD.md](docs/OPTIONS-BACKTEST-PRD.md)

**Status:** the options PRD is complete (9/9 phases) — instrument model,
expression layer, paper trading, live trading, Greeks & margin, the full
statutory fee stack, expiry handling, the portfolio options views, persistence,
forward-test wiring, and an end-to-end integration suite.

### Playbooks (Unified Trading — Plug-and-Play Option Configs)

**Playbook = declarative, reusable option strategy config** — structure, strikes policy, exits, sizing, risk envelope — no code. Portfolio spawns Runners *from* Playbooks. One concept, embedded, no new page. Strategy owns WHAT (signal), Engine owns HOW (live/paper check, margin, lot_size from instrument master).

- **Entity:** `Playbook` dataclass in `src/backtest/options/playbook.py` (final spec: `playbook_id` uuid4 at creation, `underlying` option-only V1, `structure_type` default_factory, `strike_selection` atm|delta|otm|itm, `exit_config` default_factory with `reenter=False` churn guard, `max_loss_per_trade` per-SIGNAL ₹ envelope, `tags` default_factory, `version` int auto-bump, `created_at`/`updated_at`)
- **Registry:** in-memory V1 singleton `_REGISTRY`, 3 seeded defaults (NIFTY ATM Bull Spread Conservative, NIFTY ATM Long Call/Put Directional, BANKNIFTY Delta 35 Spread), optional JSON via `PLAYBOOKS_PATH` env, delete blocks `pb_default_*`
- **Methods:** `to_expression()` → runner `instrument.expression`, `to_runner_config(strategy_name, allocated_capital, ...)` → spawn payload, `risk_envelope(spot, lot_size)` → `{"estimated": True, "max_loss_per_signal": ₹, ...}` — V1 2%/1%/4% moneyness model capped by `max_loss_per_trade`, lot_size resolved from instrument master never stored (NSE revises lot sizes)
- **API (6 routes):** `GET /api/playbooks?tag=&underlying=`, `GET /api/playbooks/<id>`, `POST /api/playbooks`, `PUT /api/playbooks/<id>` (bumps version), `DELETE /api/playbooks/<id>` (blocks seeds), `POST /api/playbooks/<id>/spawn` (returns config, no side effects — one creation path `POST /api/portfolio/runner/create`)
- **Execution Engine:** `src/backtest/forward/execution_engine.py` — C2 data-ownership rule (strategies NEVER call broker/quote APIs, all bars+chain snapshots flow engine→strategy, enforced via `_assert_data_ownership()`), C3 two-tier exits `EXIT_PRECEDENCE` (0 emergency Engine unconditional >1 stop >2 target >3 DTE >4 flip, re-entry next bar only default false, evidence -₹41,844 same-bar churn), C4 risk envelope `estimated:true` flag rendered in UI as "estimated" badge, C5 `create_app` exists `web/app.py:244` and `emergency_stop` endpoint `api/portfolio.py:310`
- **Portfolio Integration:** `get_portfolio_summary()` totals = runners + manual book (dashboard_book merged, honest), `emergency_flatten_all(mode)` closes BOTH books — legacy manual-book trades still count in the totals and are flattened with everything else
- **UI:** Portfolio tabs `Positions | Equity | Orders | Risk & intelligence | Trade history | 📚 Playbooks | Activity log`, Playbooks tab card grid with name, underlying·structure·strike·qty, exit bits, risk cap + "estimated" badge + version badge, Deploy/Edit/Delete/New (structures and their legs close from the Positions tab), banners unified (Strategy WHAT / Engine HOW) + dashboard-book count
- **Conditions C1-C5 cleared (U0.1):** C1 mutable defaults fixed via `field(default_factory=...)` + regression tests, C2 doc+assert, C3 precedence list, C4 estimated flag, C5 verified — see `tests/test_playbooks_conditions.py` (6 tests PASS)
- **Docs:** [docs/ARCHITECTURE-UNIFIED-TRADING.md](docs/ARCHITECTURE-UNIFIED-TRADING.md) — signed off with conditions, Q6 hard-delete criteria: zero new manual structures in 14d AND ≥10 playbook runners. (Full implementation log: `docs/archive/UNIFIED-TRADING-TASKS.md` — P0–P6 all done.)

### Live Order Management (the two trading tabs)

The Portfolio Command Center is a trading console, not just a monitor — automation keeps trading, but a human can intervene instantly:

- **Open Positions tab** (flat rows across runners, option structures as one net row with legs) carries an **Actions** column: 🛑 Modify SL · 🎯 Modify Target · ◐ Close 50% (equity only) · ✕ Close All. Manual levels are **prices** (share price / net premium per unit), validated server-side against the live mark and checked on **every bar, every mark move, and every stress markdown** — a manual stop fires even if the strategy never trades again. Whichever exit fires first (manual or strategy) wins.
- **Orders tab** is the engine's own `OrderLedger`: PENDING (age + the only cancel button in the app), FILLED (requested vs fill → adverse slippage per unit), REJECTED. Phase-3 additions: **amend a working order at its venue** (venue asked first — a refusal leaves local state untouched), **order-aging alerts** (warn 60 s / alert 5 min, one audit entry per band), and **bounded opt-in auto-retry** of safe refusals (`retry_policy` — live placement errors are *never* auto-resent; a lost acknowledgment can mean the order already exists).
- On a live runner a close/cancel returns `status: "placed"` — the UI says so instead of claiming the position is closed.

Endpoints, semantics, the plain-English "why" and the two tabs: [docs/PORTFOLIO-CENTER.md](docs/PORTFOLIO-CENTER.md)

### Risk Management & Monitoring (3 tiers)

| Tier | Scope | Behaviour |
|------|-------|-----------|
| **Global breakers** | Whole account | Daily loss limit, max drawdown %, leverage telemetry (`GlobalRiskConfig` — validated, typo-detecting partial updates via the risk-config API) |
| **Per-bucket breakers** | Each bucket (paper/live) is independent | A breach in `paper` does not halt `live`; halts are **session-scoped** (latches are written but deliberately not re-armed on boot) and peak/day anchors re-baseline to the restored book |
| **Monitoring surface** | Operator visibility | Dedicated `/risk` page + `risk_strip`/`risk_board` dashboard components; runner-level risk config endpoints; stress markdowns also checked against manual levels |

### Strategy Performance Analytics

`/analytics` — a per-strategy performance suite over the forward/portfolio books: overview rollup + per-runner detail (`GET /api/analytics`, `GET /api/analytics/<instance_id>`), alongside the extended `/compare`. Built as an `analytics_service` over the engine's trade walk, with a UI page and its own test suite.

### Options Backtesting (model-driven)

**Status (2026-10): frozen.** The data-source routing decision made backtests
equity-only — `CHAIN_SOURCES = ()` in `src/backtest/data/sources_policy.py`
means `option_backtest_allowed()` is always false, and option expiries come
only from the broker chain. The Phase-A driver below was built and tested but
is not reachable from the UI or the API.

The options expression layer has a backtest driver — the
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

See [docs/OPTIONS-BACKTEST-PRD.md](docs/OPTIONS-BACKTEST-PRD.md) for the design;
the Phase-A build log lives at `docs/archive/OPTIONS-BACKTEST-TASKS.md`.

## Parameter Optimization

`🎯 Optimize` (`/optimize`) searches a strategy's parameters. It supports
grid, random, Bayesian and genetic search, with up to 8 parameters and
parallel backtests. Every combination is scored with the same engine as a
normal backtest, and checked against risk constraints (max drawdown, minimum
trades, win rate, …). The best set is then stress-tested with sensitivity
sweeps and optional walk-forward validation.

Results include:
- heatmaps of any two parameters;
- stability plateaus;
- a 0–10 robustness score and plain-English overfitting warnings;
- a baseline-vs-optimized comparison and CSV export.

One click applies the winner to a runner through a **3-step wizard**: review
the validation checks (walk-forward, robustness, deflated Sharpe, Monte Carlo,
data source, trade count — each ✅/⚠️/❌), choose the target (new paper runner,
replace existing, A/B, or live with an inline explanation of the ≥30-days
paper-history gate), then confirm by typing CONFIRM. Every apply is recorded
in an audit trail with rollback, and names the originating backtest when the
run came from one. Applying to **live** is gated: it needs explicit
confirmation plus walk-forward validation, and is refused for overfitted runs.
Storage is PostgreSQL (migrations 009–015) or the dev SQLite profile. See
[docs/OPTIMIZATION-ENGINE.md](docs/OPTIMIZATION-ENGINE.md) and
[docs/backTest-enhance.md](docs/backTest-enhance.md).

### Data sources

The app runs on **real data by default**: if the configured source is disabled
(e.g. synthetic), it falls back to the best enabled source — DB bars from
`market_data_cache` first, then broker feeds, then CSV. Synthetic candles are
never used as a silent fallback. A symbol must have data before it appears in
the Backtest/Optimize/Compare dropdowns; fetch it on the Data tab first.

## Data Sources

| Source | Description |
|--------|-------------|
| **Synthetic** | Random-walk generated candles — no API needed |
| **CSV** | Read from local `data/*.csv` files |
| **mStock** | Real market data from mStock API (requires auth + TOTP) |
| **Dhan** | Real market data from Dhan HQ — `POST /marketfeed/ohlc` latest bars (minute-floor IST) + `/charts/intraday` candles; requires an active Dhan session (`DHAN_API_KEY`, `DHAN_CLIENT_ID`) |
| **PostgreSQL** | DB-first cache of real market data — 1-min bars (coarser timeframes resampled at read time), deduped, 502-retried, holiday-aware fetches |

### Multi-Broker Sessions

The broker layer is **registry-based and switchable** — mStock and Dhan implement the same `BrokerAuthBase` contract (login → verify TOTP → session status → logout). Sessions are **per-broker and concurrent** (session manager v2, Phases A–D of the multi-broker PRD); one broker is UI-active at a time:

- `POST /api/broker/list` → available brokers; `POST /api/broker/select` → switch (drops the prior session)
- The auth modal renders per-broker field labels (mStock: User ID/Password/PIN/TOTP · Dhan: Client ID/PIN/TOTP)
- Dhan auth is a single call: `Client ID + PIN + TOTP → generateAccessToken` (guarded by `DHAN_API_KEY`)

### Feed-Quality Monitor

Every bar delivered by a live feed (mStock **or** Dhan) is observed by a per-`(broker, symbol)` `FeedQualityMonitor`: staleness (observed-at − bar timestamp), gap detection (missing minutes), stale repeats (same timestamp re-delivered), and hourly error rate. Observations append to a durable JSONL log (`data/feed_quality.log`) and `aggregate_report()` recomputes from the log, so the picture survives restarts. Inspect via `GET /api/broker/feed-quality` (add `?live=1` for in-memory state).

## Database (PostgreSQL + TimescaleDB)

Real market data for the **curated NIFTY 200 universe + NSE indices** (`stock-list/nse_ind_nifty200list.csv` + the built-in index list) is stored in a TimescaleDB hypertable as **1-minute bars**; coarser timeframes are resampled at read time. The earlier daily cache was purged 2026-10-03 in favour of the 1-min base (see `docs/BACKTEST-RUN-PERSISTENCE-PRD.md`).

### Key Tables

| Table | Purpose |
|-------|---------|
| `market_data_cache` | OHLCV candle data (hypertable, partitioned by time) |
| `instruments` | 154K instruments from mStock (NSE, BSE, NFO, CDS) |
| `portfolios` | Forward-test portfolio snapshots |
| `trades` | Matched round-trip trades |
| `equity_curve` | Mark-to-market equity snapshots (on-demand snapshot API + UI components) |
| `strategy_signals` | Audit log of every signal generated |
| `trade_structures` | Durable options book — open/closed multi-leg structures with leg snapshots (survives restarts) |
| `corporate_actions` | Action vocabulary for read-time back-adjustment with a ±40% split-suspect gate |

## Project Structure

```
src/backtest/
├── data/           # Data sources (synthetic, csv, mstock, dhan, db)
│                   #   + corporate_actions policy + universe
├── strategy/       # Strategy base class + registry
├── strategies/     # Built-in strategies (SMA, RSI, Donchian, Buy&Hold, PriceMove, DirectionalOptions,
│                   #   nifty_scalper, banknifty_straddle)
├── engine/         # Backtest engine (trade simulation, metrics, options backtest driver)
├── forward/        # Forward testing (paper trading): portfolio manager, paper runner,
│                   #   feed registry (MStockBarFeed + DhanBarFeed on a shared base),
│                   #   feed_quality monitor, options bridge, risk supervisor
├── simulator/      # Costs, slippage, fills, risk — incl. option fee stack
├── options/        # Options trading: selectors, structures, paper/live
│                   #   execution, Greeks, margin, fees, expiry, persistence, playbooks
├── instruments/    # Instrument model (equity, option, expiry calendar)
├── db/             # SQLAlchemy models + DB manager
├── optimization/   # Parameter optimization: grid/random/Bayesian/genetic
│                   #   search, walk-forward, sensitivity, apply-to-runner
├── intelligence/   # Portfolio Intelligence: Greeks ₹, concentration, correlation,
│                   #   regime, collector, async persistence (migration 005 tables)
├── alerts/         # Pub/sub AlertBroker: dedup, escalation, auto-resolve,
│                   #   ack, catalog; strategies subscribe via hooks
├── brokers/        # Broker session layer: BrokerAuthBase + BrokerOrderBase,
│                   #   mstock (auth+orders) + dhan (auth+data),
│                   #   registry/switch_broker session manager, remember_session
├── db/             # SQLAlchemy models + DB manager + migrations (alembic, v005)
├── api/            # REST endpoints: backtest, strategies, forward, portfolio,
│                   #   broker_auth (list/select/login/feed-quality), playbooks,
│                   #   analytics, monitor (intelligence), data_manager, symbols
├── web/            # Flask web app (UI + API)
├── live/           # mStock live auth + data adapter
├── cli.py          # Command-line interface
└── runner.py       # Orchestrates data → strategy → engine → results
```

Outside `src/`: `plugins/strategies/` (drop-in strategy plugins) and `reference-code/` (broker SDK references, e.g. DhanHQ-py).

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# One-time setup: copy .env.example to .env and set FORWARD_TEST_DB_URL
# (PostgreSQL) + broker credentials — loaded automatically at import.
cp .env.example .env

# Run with synthetic data (no API needed)
PYTHONPATH=src python -m backtest.web.app --host 0.0.0.0 --port 5000 --source synthetic

# Run with real data from PostgreSQL
PYTHONPATH=src python -m backtest.web.app --host 0.0.0.0 --port 5000 --source db

# Production (Linux): gunicorn — one worker + threads, in-process trading state
PYTHONPATH=src gunicorn -c gunicorn.conf.py backtest.web.wsgi:app

# Useful switches (all also available as env vars — see .env.example)
#   --log-level DEBUG      full trace of every request, with request ids
#   --log-file logs/app.log
#   --currency USD          money display for every page (default: INR / ₹)
#   --replay-speed 5        forward-test clock: bars revealed per second
```

Open `http://localhost:5000` → Backtest tab → Pick a strategy → Hit **Run Backtest**.

### Docs Map

| Doc | Covers |
|-----|--------|
| [docs/USER-GUIDE.md](docs/USER-GUIDE.md) | **How to use it** — index, workflows and troubleshooting (start here) |
| [docs/how-to/](docs/how-to/README.md) | **Per-functionality how-to** — one file per page/tab, step by step |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System architecture |
| [docs/WEB-UI.md](docs/WEB-UI.md) | Every page and tab |
| [docs/PORTFOLIO-CENTER.md](docs/PORTFOLIO-CENTER.md) | Command Center: runners, buckets, breakers, order management endpoints & semantics |
| [docs/OPTIONS-PAPER-LIVE.md](docs/OPTIONS-PAPER-LIVE.md) / [docs/OPTIONS-BACKTEST-PRD.md](docs/OPTIONS-BACKTEST-PRD.md) | Options paper/live and options backtest |
| [docs/ARCHITECTURE-UNIFIED-TRADING.md](docs/ARCHITECTURE-UNIFIED-TRADING.md) | Playbooks (unified trading) |
| [docs/FORWARD-TESTING.md](docs/FORWARD-TESTING.md) / [docs/OPTIONS-FORWARD-TESTING.md](docs/OPTIONS-FORWARD-TESTING.md) | Forward test engine |
| [docs/DATA-SOURCES.md](docs/DATA-SOURCES.md) / [docs/DATABASE.md](docs/DATABASE.md) | Data sources (incl. how to add one), PostgreSQL/TimescaleDB (incl. how to add a table) |
| [docs/STRATEGY-AUTHORING.md](docs/STRATEGY-AUTHORING.md) | Writing strategies/plugins — flow, hooks, hard rules, conformance battery, catalog, lifecycle |
| [docs/STRATEGY-PERFORMANCE-ANALYTICS.md](docs/STRATEGY-PERFORMANCE-ANALYTICS.md) | **Live/paper performance analytics + parameter optimization** (new) |
| [docs/OPTIMIZATION-ENGINE.md](docs/OPTIMIZATION-ENGINE.md) | Optimization engine technical details |
| [docs/WEB-UI.md](docs/WEB-UI.md) §4 | **Consolidated P&L & tax reporting page** (PRD-002) |
| [docs/STRATEGY-TEMPLATE-GUIDE.md](docs/STRATEGY-TEMPLATE-GUIDE.md) | Strategy template + Pine Script converter (v5 — `strategy.entry`/`close` and stateful `strategy.order(... when=)` scripts convert via `generate_signals`) |
| [docs/STRATEGY-GUIDELINES.md](docs/STRATEGY-GUIDELINES.md) | Rules & review checklist for new strategies (+ `templates/strategy_test_template.py`) |
| [docs/LOGGING.md](docs/LOGGING.md) | Logging levels, request ids, debugging table |
| [docs/PORTFOLIO-INTELLIGENCE.md](docs/PORTFOLIO-INTELLIGENCE.md) / [docs/ALERTS-GUIDE.md](docs/ALERTS-GUIDE.md) / [docs/STRATEGY-ALERTS.md](docs/STRATEGY-ALERTS.md) | Portfolio Intelligence: Greeks, concentration, regime; alert system + strategy subscription hooks |
| [docs/MULTI-BROKER-PRD.md](docs/MULTI-BROKER-PRD.md) | Concurrent multi-broker sessions, segmented capital allocation, cross-broker risk |
| [docs/OPEN-ITEMS-TRACKER.md](docs/OPEN-ITEMS-TRACKER.md) | Current status: what is done, what is open |
| [docs/project-overview.md](docs/project-overview.md) | Plain-language platform tour, written from the code |
| [docs/archive/](docs/archive/) | Shipped epics' task logs, past status snapshots, superseded design docs — provenance only |

## Parameter Optimization

`POST /api/optimize/*` (grid / random / Bayesian / genetic search + walk-forward
validation) runs parameter sweeps through the same backtest driver, with an
O(1)-memory SQLite result store, pruned candidate cache and a UI page. Migrations
009–013 back the runs table. See the optimizer section in
[docs/BACKTEST-ENGINE.md](docs/BACKTEST-ENGINE.md).

## Consolidated P&L & Tax Reporting (PRD-002)

A single consolidated statement across every broker and both books (live + paper), with correct Indian tax classification, estimated tax liability, and exportable annexures:

**Tax heads (FY 2026-27):**
- F&O → non-speculative business income (slab rate, default 30%)
- Intraday equity → speculative business income (slab rate)
- Delivery ≤12m → STCG 20% (s.111A)
- Delivery >12m → LTCG 12.5% above ₹1,25,000 exemption (s.112A)
- Paper trading → not taxable (never enters a return)
- Unknown instruments → fail-closed `UNCLASSIFIED`

**Key features:**
- Gross P&L → itemised fees (brokerage, STT, exchange, SEBI, stamp, GST) → net → estimated tax → net-after-tax
- Groups by broker, mode (live/paper), tax category, strategy
- Contract-note reconciliation per broker (PASS/WARNING/FAIL tolerance bands)
- Exports: PDF statement, ITR annexures workbook (xlsx/csv/json), uncapped trade ledger CSV
- Monthly email reports on 1st of month (dry-run to `var/reporting/outbox` unless SMTP configured)
- Rates/tolerances/mailer in `config/reporting.yaml` (override with `REPORTING_CONFIG_PATH`)

**Access:** `/reporting` UI page, or `GET /api/reporting/pnl/consolidated`, `POST /api/reporting/pnl/export/{pdf,itr,trades}`, `POST /api/reporting/pnl/reconcile`, `POST /api/reporting/email`. See [docs/WEB-UI.md](docs/WEB-UI.md) §4 for full page elements and endpoints.

## Tests

```bash
cd src && python -m pytest ../tests/ -q          # full suite (4,800+ collected)
cd src && python -m pytest ../tests/ -q -k options   # options slice only
cd src && python -m pytest ../tests/test_position_management.py -q   # order management (67 tests)
cd src && python -m pytest ../tests/intelligence ../tests/alerts ../tests/db ../tests/reporting_*.py -q   # intelligence + alerts + migrations + reporting (500+ tests)
```

Plus JS behaviour assertions across Node harnesses (`tests/js/*.mjs` — positions actions, Orders tab incl. amend + aging, alert widget, monitor page, P&L view models, broker-card grouping), run by `tests/test_web_components.py`, `tests/test_reporting_ui.py`, and `tests/test_settings_panel.py`; skipped when node is absent.

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
