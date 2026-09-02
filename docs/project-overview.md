# Project Overview — Back-Test

*A plain-language tour of what this repository is, what it can do, and how the
pieces fit together. Written from the code, not from the specs, so where the
docs and the source disagree this file follows the source.*

Last verified against commit `aa2b583` (2026-09-02).

---

## 1. What this project is

**An algorithmic trading platform for testing stock-trading strategies without
risking real money.**

You give it historical price data (OHLCV candles — Open, High, Low, Close,
Volume), pick a strategy such as "buy when the 20-day average crosses above the
50-day average", and the engine replays history bar by bar, simulating every
trade the strategy would have made. At the end you get an equity curve, a trade
list, and performance metrics (Sharpe ratio, max drawdown, win rate, P&L).

The focus is the **Indian equity market** — NSE symbols, INR currency, IST
timezone, Indian broker cost models (STT, stamp duty, SEBI turnover fees, GST),
and mStock as the broker integration.

The core question the platform answers: *would this strategy have made money,
after realistic costs?* The "after realistic costs" part is where most of the
code lives — a naive backtest that ignores commission and slippage will happily
tell you a losing strategy is profitable.

### It is a simulator, not a trading system

Nothing in this repository can place a real order. There is no `place_order`
anywhere in `src/`. The broker integration handles **login and market data
only**. Every "trade" is a row in a database. See §8 for exactly where the line
sits — this is the single most important thing to understand before using it.

---

## 2. The four things you can do with it

| Mode | What it does | Where it lives |
|---|---|---|
| **Backtest** | Run one strategy over a historical date range, get results | `POST /api/backtest/run` |
| **Compare** | Run several strategies over the same data, side by side | `POST /api/backtest/run-many` |
| **Forward test** | Paper-trade a replay — bars are revealed gradually, as if live | `POST /api/forward/start` |
| **Portfolio** | Run many strategies at once under shared risk limits | `POST /api/portfolio/runner/create` |

The distinction between **backtest** and **forward test** is the interesting
one. A backtest computes the whole result instantly. A forward test replays the
same data *slowly* — one bar per second by default — so the strategy sees the
market unfold the way it would in real trading, and you can watch it work.
Crucially the replay clock runs **on the server** in a background thread, so it
keeps advancing whether or not a browser tab is open.

**Forward testing here is still a replay of historical data, not a live market
feed.** The name is aspirational.

---

## 3. How a backtest actually flows

```
Data source            Strategy              Engine                 Results
(synthetic/CSV/    →   generate_signals  →   Backtester.run     →   metrics
 DB/mStock)            returns +1/0/-1       simulates trades       equity curve
                                             applies costs          trade table
```

1. **Data source** produces a canonical DataFrame: lowercase columns
   `open, high, low, close, volume`, ascending tz-naive `DatetimeIndex`. Every
   source normalises to this one shape, which is why sources are swappable.
2. **Strategy** receives that frame and returns a signal series: `+1` long,
   `0` flat, `-1` short.
3. **Engine** (`engine/backtester.py`) turns signals into positions, applies
   commission and slippage, and produces an equity curve.
4. **Metrics** (`engine/metrics.py`) reduce the equity curve to numbers.

### The no-lookahead rule

The most important correctness property in the whole codebase:

> **Position at bar `t` is determined by the signal at bar `t-1`.**

Implemented as `target.shift(1)`. Without this the backtest "sees" a bar's
closing price before deciding to trade on that bar, which produces gorgeous,
completely fictional returns. This is the classic way backtests lie, and the
codebase guards it explicitly (`PROJECT-CONTEXT.md` invariant #1).

### Two engine paths

`Backtester.run()` dispatches to one of two implementations that must agree:

- `_run_vectorized` — fast pandas math, no intrabar events.
- `_run_with_risk` — bar-by-bar loop, needed when stop-loss/take-profit can
  fire *inside* a bar.

Invariant #3 requires these to produce the same numbers when no stops are set.
Walk-forward equity must reconcile with the vectorized result to `1e-5`.

### Defaults

| Setting | Default |
|---|---|
| Initial capital | 100,000 |
| Commission | 0.03% (`0.0003`) |
| Slippage | 0.05% (`0.0005`) |
| Periods per year | 252 (trading days) |

### Metrics produced

`total_return`, `cagr`, `volatility`, `sharpe`, `max_drawdown`, `calmar`,
`num_trades`, `closed_trades`, `open_trades`, `winning_trades`,
`losing_trades`, `win_rate`, `realised_pnl`, `avg_trade_pnl`,
`best_trade_pnl`, `worst_trade_pnl`, `exposure`, `final_equity`, `bars`.

Two subtleties worth knowing, both deliberate:
- `num_trades` counts **round trips** and includes one still-open trade (marked
  to the final close).
- `win_rate` is over **closed trades only** — an open position isn't a result
  yet.

---

## 4. Built-in strategies

Four, all in `src/backtest/strategies/`, all self-registering into a registry
via a `@register` decorator so the UI and CLI discover them automatically.

| Name | Logic | Type |
|---|---|---|
| `buy_and_hold` | Buy at the start, never sell | Baseline benchmark |
| `sma_crossover` | Buy when fast moving average crosses above slow | Trend-following |
| `rsi_reversion` | Buy oversold, sell overbought | Mean-reversion |
| `donchian_breakout` | Buy on new N-day highs, sell on new lows | Momentum |

`buy_and_hold` exists to be beaten. If a clever strategy can't outperform
"buy it and do nothing", the cleverness isn't paying for itself.

Adding a strategy means subclassing the base in `strategy/base.py`, setting a
`name`, implementing `generate_signals(df) -> Series`, and applying `@register`.
See `docs/ADDING-NEW.md`.

---

## 5. Where the data comes from

| Source | Description | Needs credentials? |
|---|---|---|
| **Synthetic** | Random-walk generated candles | No — the zero-setup default |
| **CSV** | Local `data/*.csv` files | No |
| **PostgreSQL** | Cached real market data | DB only |
| **mStock** | Live API (Indian broker) | Yes — API key + password + OTP/TOTP |

Selected at launch with `--source synthetic|csv|db|mstock`.

The database holds real NIFTY 200 data: **201 stocks, 467K+ daily bars,
Jan 2020 – Aug 2026**, plus **154K instruments** from mStock across NSE, BSE,
NFO and CDS.

**Known gap:** timeframe selection is cosmetic on synthetic and CSV sources —
they produce daily bars regardless of what you ask for (gap G6).

---

## 6. The realistic-cost simulator

`src/backtest/simulator/` is the largest and most carefully built subsystem —
18 modules dedicated to *not lying to you about costs*.

| Module | Responsibility |
|---|---|
| `money.py` | Decimal arithmetic — the foundation |
| `fees.py` | Broker cost profiles (Zerodha, IBKR, …) |
| `commission.py` | Brokerage calculation |
| `slippage.py` | The gap between expected and actual fill price |
| `execution.py` | Order matching, partial fills, liquidity limits |
| `fill.py` / `order.py` / `lots.py` | Order lifecycle and lot tracking |
| `position.py` / `portfolio.py` | Position and cash accounting |
| `position_sizing.py` | How large a trade to place |
| `risk_manager.py` / `stop_manager.py` | Risk limits, stops |
| `performance.py` / `trade_analyzer.py` | Analytics |

### Everything monetary is a `Decimal`

Not a float. From `money.py`:

> *Binary floats cannot represent `0.1` exactly, and once a few thousand fills
> have accumulated the equity curve stops reconciling with the sum of trade
> P&L — a bug that is miserable to track down.*

Two quantisation levels mirror the DB schema: **4 dp** for money
(`NUMERIC(20,4)`), **8 dp** for prices and quantities (`NUMERIC(20,8)`).
`to_decimal()` actively **rejects floats** via `repr()` inspection, so a stray
float can't silently leak in.

*Caveat: SQLite stores `NUMERIC` as float, so money values are slightly wrong
on SQLite. Use PostgreSQL for anything you report on.*

### Indian costs are modelled properly

A "zero brokerage" delivery trade is not free. Per `ENGINEERING-NOTES.md`, a
₹1 lakh Indian delivery round trip costs **~₹238** in statutory charges even
with zero brokerage — STT both sides at 0.1%, plus exchange fees, SEBI turnover
fee, stamp duty, DP charges and GST. Delivery and intraday have materially
different tax treatment (intraday STT is sell-side only at 0.025%), so picking
the wrong `TradeSegment` can be off by ~8x.

### Slippage usually matters more than commission

The engineering notes are blunt: *"Strategy profitable in backtest, loses live →
compare `backtest` vs `realistic` slippage profiles. Slippage typically dwarfs
commission."* Execution is seeded (default `42`) so runs are reproducible, and
orders are capped at 10% of bar volume by default (`max_participation`) because
you can't buy more than the market traded.

---

## 7. Multi-strategy portfolio management

`forward/portfolio_manager.py` is a control tower for running **50+ strategies
simultaneously**. Design properties:

- **Isolated capital buckets** — each `StrategyRunner` gets its own capital;
  the manager only aggregates, it never moves money between runners.
- **One shared order ledger** — all runners submit through a single tagging
  ledger, so fills can never be attributed to the wrong strategy.
- **Tick-driven risk** — `risk_supervisor.py` runs on *every* feed tick.

The `RiskSupervisor` enforces portfolio-wide circuit breakers that **override
individual strategy decisions**:

| Breaker | Trigger |
|---|---|
| Global daily loss limit | Summed daily P&L across all runners breaches the limit |
| Global max drawdown | Aggregate peak-to-trough equity drop exceeds a fraction |
| Concentration warning | 3+ runners hold LONG in the same correlation group |

Two halt modes: `PAUSE_AND_HOLD` (stop new entries, let existing stops ride) and
`EMERGENCY_FLATTEN` (cancel and exit everything). Target: a breach halts every
runner **within the same tick, under 500 ms**.

Pre-registered universes: `NIFTY_50`, `TOP_10_CRYPTO`, `TOP_20_CRYPTO`, plus
custom registration. Correlation groups drive the concentration warning.

---

## 8. What is simulated vs. what is real — read this before trusting anything

This is the sharpest edge in the project, and the docs blur it. The precise
state of the mStock broker integration:

### What exists and works

| Capability | Where |
|---|---|
| Login (username/password) | `brokers/mstock.py` → `login()` |
| OTP / TOTP second factor | `brokers/mstock.py` → `verify_totp()` |
| TOTP code generation (HMAC-SHA1) | `live/auth.py` → `generate_totp_code()` |
| Session status / token / logout | `get_session_status()`, `logout()` |
| Session expiry monitoring | `brokers/session_manager.py`, background thread |
| Fetching historical bars | `live/mstock.py` → `get_bars()` |
| Fetching the latest quote | `get_latest()` |
| Connectivity preflight (DNS/HTTPS/auth) | `live/preflight.py` |

### What does not exist

There is **no order placement, modification or cancellation against any real
broker.** Confirmed by grep: `place_order` appears zero times in `src/`.

`brokers/base.py` — the abstract contract every broker must satisfy — defines
exactly four methods: `login`, `verify_totp`, `get_session_status`, `logout`.
**There is no order surface in the base class**, so this is greenfield at the
interface level, not merely unimplemented for mStock.

### What is ready for it

The groundwork is genuinely in place, which makes the gap easy to misread:

- `forward/order_ledger.py` — a real `OrderLedger` with `submit`/`cancel`/
  `apply_fill`, `client_order_id` idempotency keys and thread-safe locking, plus
  a `PaperBroker` that fills against simulated prices.
- `db/models.py` — the `orders` table is production-shaped, with check
  constraints for order types, limit/stop price consistency, fill consistency
  and mandatory rejection reasons.
- `docs/archive/mstock-typea-api-reference.md` documents every endpoint that
  *would* be needed: `POST /openapi/typea/orders/regular`, `PUT .../{order_id}`,
  `DELETE .../{order_id}`, `cancelall`, order book, order details, margin
  calculator.

That reference file states the position plainly: *"No order API is required for
the first paper-trading stage."*

So: **schema and ledger ready, HTTP layer entirely absent.**

### One more trap

`src/backtest/forward/live_engine.py` — 697 lines implementing
`LiveForwardEngine` with a real mStock polling loop, state persistence and a
module-level engine registry — is **orphaned**. `grep live_engine` across `src/`
returns zero importers. It is the only code resembling a live feed, and nothing
calls it. Don't assume it runs.

---

## 9. Architecture map

```
src/backtest/
├── data/            Data sources: synthetic, CSV, DB, mStock + universes
├── strategy/        Base class, registry, adapter
├── strategies/      The four built-in strategies
├── engine/          Backtester, metrics, trade walk, plotting
├── simulator/       Costs, fills, sizing, risk, portfolio accounting (18 files)
├── forward/         Forward testing: engine, runners, portfolio manager,
│                    risk supervisor, order ledger, feeds
├── live/            mStock auth, API client, preflight, time & data validation
├── brokers/         Broker auth contract + mStock + session manager
├── marketdata/      Tick→bar aggregation, quality checks, time sync
├── db/              SQLAlchemy models, connection manager, config
├── api/             Flask blueprints (the REST surface)
├── web/             Flask app: pages + static + templates
├── dashboard/       Legacy standalone dashboard (slated for retirement)
├── adapters/        BacktestAdapter — one result shape for every UI
├── analysis/        Strategy comparison
├── alerts/          Alert manager
├── config_manager/  Layered YAML + env + profile config loader
├── cli.py           Command-line interface
└── runner.py        Orchestrates data → strategy → engine → results
```

**Scale:** 98 Python modules, ~36,000 lines in `src/`; 61 test modules.

### One-result-shape rule

`adapters/backtest_adapter.py` produces the same payload shape for Backtest,
Compare and Forward. That's why the frontend can reuse identical chart and
table components across all three pages. Related invariant #6: trade accounting
has **one source of truth** (`engine/trades.py`) feeding both `compute_metrics`
and the adapter — numbers are never re-derived from the position sign.

---

## 10. Configuration — two systems, split by entry point

Worth stating clearly because it surprises people.

### YAML — for the engine

17 files in `config/`. The pattern is **per-module constants**, not central
loading; each module resolves `parents[3]/"config"/<name>.yaml`:

| File | Read by |
|---|---|
| `forward_testing.yaml` | `forward/engine.py` — the main engine config |
| `risk.yaml` | `simulator/risk_manager.py` |
| `execution.yaml` | `simulator/execution.py` |
| `slippage.yaml` | `simulator/slippage.py` |
| `stops.yaml` | `simulator/stop_manager.py` |
| `brokers.yaml` | `simulator/fees.py` |
| `position_sizing.yaml` | `simulator/position_sizing.py` |
| `performance.yaml` | `simulator/performance.py` |
| `marketdata.yaml` | `marketdata/handler.py` |
| `calendar.yaml` | `marketdata/timesync.py` |
| `quality.yaml` | `marketdata/quality.py` |
| `data_quality.yaml` | `live/data_validator.py` |
| `alerts.yaml` | `alerts/manager.py` |
| `app.yaml` | `config_manager/manager.py` |
| `database.yaml` | `db/config.py` and Alembic |

`config/forward_testing.yaml` is the main engine config — sections for
`portfolio`, `strategy`, `risk`, `execution`, `sizing`, `data` and `system`.
Loaded by `load_forward_config()`, parsed into typed dataclasses that raise
`ValidationError` on bad values. All sections optional; defaults fill the gaps.

Two gotchas:
- **`market_data.yaml` and `time_sync.yaml` are dead files** — zero Python
  references. They sit next to the live `marketdata.yaml` and `calendar.yaml`
  and are easy to edit by mistake.
- **The forward engine reads no environment variables.** Its docstring claims
  "plus env overrides", but there is no `os.environ` access in that module. The
  layered YAML→env→profile machinery lives in `config_manager/manager.py`, which
  reads `app.yaml` and is *not* wired to the forward engine.

### Environment variables — for the Flask app

The web app reads no YAML at all. It is env + CLI flags only:

| Variable | Purpose |
|---|---|
| `FORWARD_TEST_DB_URL` | Database connection (SQLite or PostgreSQL) |
| `FORWARD_TEST_DB_PROFILE` | `development` / `testing` / `production` |
| `MSTOCK_API_KEY`, `MSTOCK_USERNAME`, `MSTOCK_PASSWORD` | Broker credentials |
| `MSTOCK_AUTH_MODE` | `otp` or `totp` |
| `BACKTEST_LOG_LEVEL`, `BACKTEST_LOG_FILE` | Logging |
| `BACKTEST_CURRENCY` | Display currency (default INR) |
| `FORWARD_REPLAY_SPEED` | Bars revealed per second; `0` = manual stepping |

Full annotated list in `.env.example`. Forward-session parameters arrive in the
`POST /api/forward/start` request body, not from config at all.

---

## 11. Database

**PostgreSQL + TimescaleDB** in production, SQLite for local development.
SQLAlchemy 2.0 models with Alembic migrations.

| Table | Purpose |
|---|---|
| `portfolios` | Portfolio state and cash |
| `positions` | Open and closed positions |
| `orders` | Order lifecycle |
| `fills` | Individual executions |
| `trades` | Matched round trips |
| `equity_curve` | Mark-to-market snapshots |
| `market_data_cache` | OHLCV candles — TimescaleDB hypertable |
| `performance_metrics` | Computed metrics |
| `strategy_signals` | Audit log of every signal generated |
| `system_logs` | Application logs |

The schema does real work rather than just storing rows. Constraints that have
already caught bugs: one open position per symbol
(`uq_positions_one_open_per_symbol`), `status='filled'` requires both
`filled_at` and full quantity, rejected orders require a non-empty reason,
limit orders require a limit price, `client_order_id` is unique so duplicate
submissions are rejected as an idempotency key.

Write ordering matters: `Portfolio.save_to_db()` writes closed positions before
open ones (to dodge the uniqueness constraint) and follows
portfolios→positions→orders→fills atomically to satisfy foreign keys.

---

## 12. Running it

```bash
pip install -r requirements.txt

# Web UI with synthetic data — no credentials, no DB needed
PYTHONPATH=src python -m backtest.web.app --host 0.0.0.0 --port 5000 --source synthetic

# With real data from PostgreSQL
PYTHONPATH=src python -m backtest.web.app --host 0.0.0.0 --port 5000 --source db
```

Then open `http://localhost:5000` → Backtest → pick a strategy → **Run Backtest**.

`PYTHONPATH=src` is required — the package is not pip-installed.

### CLI

```bash
backtest list                                       # list strategies
backtest run --strategy sma_crossover --from 2024-01-01 --to 2024-12-31
backtest compare --strategies sma_crossover,rsi_reversion --from D1 --to D2
backtest preflight                                  # DNS / HTTPS / auth checks
backtest papertrade --mode walkforward --strategies X --from D1 --to D2
```

### Web pages

`/` · `/backtest` · `/compare` · `/forward` · `/portfolio` · `/dashboard` ·
`/data` · `/health`

### REST API

- **Backtest** — `POST /api/backtest/run`, `/run-many`
- **Strategies** — `GET /api/strategies`, `/api/strategies/<name>/params`
- **Forward** — `POST /api/forward/start`, `/stop`; `GET /status`, `/sessions`,
  `/trades`, `/equity`
- **Portfolio** — `GET /summary`, `/universes`, `/runner/<id>`, `/stream` (SSE);
  `POST /runner/create`, `/runner/<id>/control`, `/control/<action>`,
  `/emergency_stop`
- **Broker** — `POST /api/broker/login`, `/verify-totp`, `/logout`;
  `GET /api/broker/status`
- **Data** — `GET /api/data/status`, `/inventory`; `POST /api/data/fetch`, `/stop`

---

## 13. Deployment and the concurrency trap

**Python 3.11** in deployment (`python:3.11-slim`, pre-commit pinned to
`python3.11`), though `tox.ini` still tests `py39, py310, py311` — so 3.9 is the
supported floor.

Two separate deployables:

**Engine** — `Dockerfile` runs `python -m backtest.forward.engine`, no ports
exposed, with a healthcheck that just verifies the module imports.
`forward_testing.service` runs the same module under systemd with
`MemoryMax=2G`, `CPUQuota=200%`, `Restart=on-failure` and security hardening.

**Web app** — currently the **Flask development server** (`app.run(...)`).
Gunicorn appears only in archived docs and as open item #10 in the task tracker.
It is not in `requirements.txt`.

### The trap

**All forward-test state is in-process and thread-based.** `api/forward.py` keeps
sessions in a module-level `OrderedDict` behind an `RLock`, with a daemon thread
advancing the replay clock. Same pattern in `live_engine.py`,
`portfolio_manager.py`, `brokers/session_manager.py`, `forward/feed.py`.

Therefore **`gunicorn --workers 2` would silently break the app**: worker 1
starts a forward session, worker 2 serves `/api/forward/status` and 404s;
broker auth lands in one worker only, so the other rejects authenticated
requests. If you move to a production WSGI server, either use
`--workers 1` (threads only), or externalise session state to the DB first.

State also does not survive a restart — it survives a page refresh, nothing
more. Persistence is tracked as V2 item #3.

---

## 14. Testing and quality

- **1,875 tests passing**, 4 skipped (need real mStock credentials).
- 36 JavaScript behaviour assertions across 4 Node harnesses (`tests/js/*.mjs`).
- Coverage gate: **80% minimum**, enforced in `tox.ini`.
- `tests/` splits into `unit/`, `integration/`, `e2e/`, `js/`, `manual/`,
  `fixtures/`.
- `benchmarks/` holds load and performance tests (`pytest-benchmark`).
- Lint stack: black, isort, flake8, pylint, mypy — wired through pre-commit
  and tox.

```bash
PYTHONPATH=src pytest tests/ -q          # all tests
PYTHONPATH=src pytest tests/ -q -k "not live"   # skip credential-dependent
tox                                       # full matrix + lint + mypy
```

Note the sandbox convention recorded in `PROJECT-CONTEXT.md`: rebuild the venv
each session with
`python3 -m venv /home/user/.venv && /home/user/.venv/bin/pip install -q -r requirements.txt`.

---

## 15. Observability

Every entry point installs `logging_config.configure_logging()`. Invariant #7
states that `--log-level DEBUG` **must** explain any empty or flat result — if a
backtest returns nothing and the logs don't say why, that is itself a bug.

Every request carries an id, and every `/api` error quotes it in the response,
so the browser toast, the log line and the traceback can all be matched up.

`docs/LOGGING.md` carries a symptom→cause table for the usual suspects (empty
results, zero trades, card/table mismatches, 403 on Forward Start).

For deeper debugging, `instructions/ENGINEERING-NOTES.md` is the single most
valuable file in the repo — a symptom→likely-cause→where-to-look table covering
money and P&L, database, and market data, written from bugs that actually
happened.

---

## 16. Current limitations

Straight from the code and trackers, not aspirational:

1. **No live trading.** No order placement against any broker. Paper only.
2. **Forward testing is a replay** of historical data, not a live market feed.
3. **In-memory state** — forward sessions and broker auth are lost on restart.
4. **Single-worker only** — see §13.
5. **Flask dev server** in production; Gunicorn still an open task.
6. **Timeframe is cosmetic** on synthetic and CSV sources (daily bars only).
7. **`live_engine.py` is orphaned** — 697 lines, zero importers.
8. **Two dead config files** — `market_data.yaml`, `time_sync.yaml`.
9. **Money is inexact on SQLite** — `NUMERIC` degrades to float. Use PostgreSQL
   for reported numbers.
10. **Legacy `dashboard/app.py`** duplicates `/forward` and is slated for
    retirement (tracker item #11).
11. **Broker cost rates are FY 2024-25** and India changes them regularly —
    `config/brokers.yaml` warns to check a recent contract note before trusting
    cost-sensitive results.

---

## 17. Where to read next

| Question | File |
|---|---|
| How does the whole thing fit together? | `docs/ARCHITECTURE.md` |
| How does the backtest engine work? | `docs/BACKTEST-ENGINE.md` |
| How do I add a strategy or data source? | `docs/ADDING-NEW.md` |
| What are the strategies doing? | `docs/STRATEGIES.md` |
| How does forward testing work? | `docs/FORWARD-TESTING.md` |
| Multi-strategy portfolios? | `docs/PORTFOLIO-CENTER.md` |
| Schema and migrations? | `docs/DATABASE.md`, `db/DB-IMPLEMENTATION-GUIDE.md` |
| Something is broken | `instructions/ENGINEERING-NOTES.md`, `docs/LOGGING.md` |
| What is done, what is planned? | `instructions/TASK-TRACKER.md` |
| Invariants I must not break | `PROJECT-CONTEXT.md` |
| mStock endpoints | `docs/archive/mstock-typea-api-reference.md` |