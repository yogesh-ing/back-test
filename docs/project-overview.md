# Project Overview — Back-Test

*A plain-language tour of what this repository is, what it can do, and how the
pieces fit together. Written from the code, not from the specs, so where the
docs and the source disagree this file follows the source.*

Last verified against the 2026-10-06 tree — includes the backtest run ledger
(migration 018), the Data tab rework, broker session-expiry alerts, and the
`market_data_cache` UTC→IST clock repair.

---

## 1. What this project is

**An algorithmic trading platform for testing stock and index-option trading
strategies without risking real money.**

You give it historical price data (OHLCV candles — Open, High, Low, Close,
Volume), pick a strategy such as "buy when the 20-day average crosses above the
50-day average", and the engine replays history bar by bar, simulating every
trade the strategy would have made. At the end you get an equity curve, a trade
list, and performance metrics (Sharpe ratio, max drawdown, win rate, P&L).

The focus is the **Indian market** — NSE symbols, INR currency, IST timezone,
Indian broker cost models for both equity (STT, stamp duty, SEBI turnover fees,
GST) and index options (flat per-order brokerage, STT on the sell side only),
and mStock as the broker integration.

The core question the platform answers: *would this strategy have made money,
after realistic costs?* The "after realistic costs" part is where most of the
code lives — a naive backtest that ignores commission and slippage will happily
tell you a losing strategy is profitable.

### Paper first — with one narrow, gated path to real orders

The default and overwhelmingly exercised path is simulation: every "trade" is a
row in a database or an in-process ledger. Real order placement exists in
exactly one place — the mStock order client (`brokers/mstock.py`) plus the
options multi-leg trader (`options/live_trading.py`) built on it — and it is
gated twice: an authenticated broker session is required, and
`LiveOptionTrader` defaults to `dry_run=True`, which logs the exact payload
that *would* be sent and places nothing. See §8 for exactly where the line
sits — this is the single most important thing to understand before using it.

---

## 2. The five things you can do with it

| Mode | What it does | Where it lives |
|---|---|---|
| **Backtest** | Run one strategy over a historical date range, get results — every completed run now persists to the run ledger (`backtest_runs`, migration 018) | `POST /api/backtest/run` |
| **Compare** | Run several strategies over the same data, side by side — slots persist to the ledger too | `POST /api/backtest/run-many` |
| **Forward test** | Paper-trade a replay — bars are revealed gradually, as if live | `POST /api/forward/start` |
| **Portfolio** | Run many strategies at once under shared risk limits | `POST /api/portfolio/runner/create` |
| **Portfolio (Live)** | Live-scoped command center — real money only | `GET /portfolio/live` |
| **Portfolio (Paper)** | Paper sandbox — simulated fills only | `GET /portfolio/paper` |
| **Portfolio (Overview)** | Combined view — Live prominent, Paper secondary | `GET /portfolio` |
| **Options** | Multi-leg option structures (long call/put, spreads) — paper or live, inside the Command Center (the old standalone `/options` page was removed) | `GET /portfolio` Positions/Playbooks tabs |
| **Data** | Fetch and inspect the market-data cache — curated NIFTY 200 + NSE index universes, holiday-aware, deduped fetches | `GET /data` |
| **Reporting** | Consolidated P&L across all brokers/books, tax estimation, exports, reconciliation | `GET /reporting` |

The distinction between **backtest** and **forward test** is the interesting
one. A backtest computes the whole result instantly. A forward test replays the
same data *slowly* — one bar per second by default — so the strategy sees the
market unfold the way it would in real trading, and you can watch it work.
Crucially the replay clock runs **on the server** in a background thread, so it
keeps advancing whether or not a browser tab is open.

**The `/forward` page is a replay of historical data.** The Command Center's
portfolio runners are the live path: they consume real broker feeds
(`MStockBarFeed` / `DhanBarFeed` via `forward/feed_registry.py`) bar by bar,
still paper-filled unless armed live.

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
5. **The run is persisted** — every completed single/compare run lands in the
   run ledger (`backtest_runs` / `backtest_compare_runs` / `backtest_run_series`,
   migration 018, `api/backtest_run_store.py`): full payload, provenance,
   config hash — so a result can be re-opened, not just re-run.

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

Thirteen built-ins, all in `src/backtest/strategies/`, all self-registering
into a registry via a `@register` decorator so the UI and CLI discover them
automatically. The core five:

| Name | Logic | Type |
|---|---|---|
| `buy_and_hold` | Buy at the start, never sell | Baseline benchmark |
| `sma_crossover` | Buy when fast moving average crosses above slow | Trend-following |
| `rsi_reversion` | Buy oversold, sell overbought | Mean-reversion |
| `donchian_breakout` | Buy on new N-day highs, sell on new lows | Momentum |
| `price_move` | Buy/sell based on price movement threshold (e.g. ₹5) | Threshold-based |

Plus `nifty_scalper`, `banknifty_straddle`, `bollinger_reversion`,
`ema_pullback`, `macd_trend`, `momentum_roc`, `atm_instant_buy`, and the
`option_directional` market-view strategy — and any drop-in plugin in
`plugins/strategies/`, loaded by discovery at boot (that includes the
2026-10 `swing_*` battery, which failed its gates — nothing from it was
deployed).

`buy_and_hold` exists to be beaten. If a clever strategy can't outperform
"buy it and do nothing", the cleverness isn't paying for itself.

Adding a strategy means subclassing the base in `strategy/base.py`, setting a
`name`, implementing `generate_signals(df) -> Series`, and applying `@register`.
See `docs/STRATEGY-AUTHORING.md` (and `docs/archive/STRATEGIES.md` for the
original catalog).

A strategy can also express a *directional view* instead of a raw signal:
`Strategy.generate_market_view()` returns a `MarketView` (bullish/bearish,
conviction, spot) that the options layer converts into a `TradeIntent` —
strike selection (ATM / delta / fixed distance), an expiry policy, and one of
four structures (long call, long put, bull call spread, bear put spread).

---

## 5. Where the data comes from

| Source | Description | Needs credentials? |
|---|---|---|
| **Synthetic** | Random-walk generated candles | No — the zero-setup default |
| **CSV** | Local `data/*.csv` files | No |
| **PostgreSQL** | Cached real market data | DB only |
| **mStock** | Live API (Indian broker) | Yes — API key + password + OTP/TOTP |
| **Dhan** | Live API (DhanHQ v2) | Yes — Client ID + PIN + TOTP |

Selected at launch with `--source synthetic|csv|db|mstock`.

The database holds real NSE data fetched through the **Data tab**: the curated
**NIFTY 200** list (`stock-list/nse_ind_nifty200list.csv`) plus a built-in NSE
**index universe** (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX, INDIAVIX
and the sectoral indices), stored as **1-minute bars** — coarser timeframes are
resampled at read time. Fetching is deduplicated, retries mStock's intermittent
502s, and skips exchange holidays from the `market_holidays` table (seeded
2022–2026). The earlier daily cache was purged 2026-10-03; the 1-min base is
still being backfilled (see `docs/OPEN-ITEMS-TRACKER.md`).

**Known gap:** timeframe selection is cosmetic on synthetic and CSV sources —
they produce daily bars regardless of what you ask for (gap G6).

---

## 6. The realistic-cost simulator

`src/backtest/simulator/` is the largest and most carefully built subsystem —
18 modules dedicated to *not lying to you about costs*.

| Module | Responsibility |
|---|---|
| `money.py` | Decimal arithmetic — the foundation |
| `fees.py` | Broker cost profiles (Zerodha, IBKR, …) + options fee stack |
| `commission.py` | Brokerage calculation (incl. flat ₹20/order options model) |
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

### Option orders cost differently — and that is modelled

Options price separately from equity: `OptionsCommission` charges a flat
**₹20 per order** (not per lot), STT applies to the sell side only, and
`FeeCalculator.calculate_structure()` produces per-leg breakdowns that merge
into one total — a two-leg spread pays brokerage twice, exactly like a real
contract note. `validate_against_contract_note()` audits the model against a
transcribed broker contract note. Reference anchor, tested: one NIFTY lot at
₹120.50 premium costs **₹27.63** all-in on the mStock flat plan (no STT on the
buy side).

---

## 7. Multi-strategy portfolio management

`forward/portfolio_manager.py` is a control tower for running **50+ strategies
simultaneously**. Design properties:

- **Isolated capital buckets** — each `StrategyRunner` gets its own capital;
  the manager only aggregates, it never moves money between runners.
- **One shared order ledger** — all runners submit through a single tagging
  ledger, so fills can never be attributed to the wrong strategy.
- **Tick-driven risk** — `risk_supervisor.py` runs on *every* feed tick.

### Bucket separation (Live vs Paper)

The portfolio is split into **two independent buckets** — Live and Paper —
with complete isolation between them:

- **Per-bucket state** — equity, peak equity, drawdown, and daily P&L are all
  *derived* from each bucket's runners (not duplicated). A dict key like
  `_bucket_peak["live"]` stores only the high-water mark; actual equity is
  computed by summing `runner.equity()` for that bucket.
- **Independent circuit breakers** — a paper breach does NOT halt live trading,
  and vice versa. Each bucket has its own halt flag, day-anchor, and daily
  loss tracking.
- **Scoped bulk control** — `pause_all(mode="paper")` pauses only paper runners.
  Without `mode=`, it acts as a master control on all buckets.
- **Master kill** — `emergency_flatten_all()` without a mode flattens both
  buckets. With `mode="live"`, it flattens only live positions.
- **Capability flag** — `get_portfolio_summary()` includes a `capability`
  dict indicating whether a broker is connected. The frontend uses this to
  show "REAL MONEY" or "Simulated fills" on the Live page.

### Three views

| View | Route | Shows |
|---|---|---|
| **Overview** | `/portfolio` | Live command center (prominent) + Paper sandbox (compact) |
| **Live** | `/portfolio/live` | Live runners only, scoped metrics, Emergency Flatten visible |
| **Paper** | `/portfolio/paper` | Paper runners only, sandbox framing, no Emergency Flatten |

All three views consume a **single SSE stream** (`/api/portfolio/stream`).
The stream payload embeds per-bucket aggregates in the `buckets` field and
the `capability` dict. Frontend filters client-side based on `PAGE_MODE`.

### Risk supervisor

The `RiskSupervisor` enforces portfolio-wide circuit breakers that **override
individual strategy decisions**:

| Breaker | Trigger |
|---|---|
| Per-bucket daily loss limit | Daily P&L within one bucket breaches the limit |
| Per-bucket max drawdown | Peak-to-trough equity drop in one bucket exceeds a fraction |
| Concentration warning | 3+ runners hold LONG in the same correlation group |

Two halt modes: `PAUSE_AND_HOLD` (stop new entries, let existing stops ride) and
`EMERGENCY_FLATTEN` (cancel and exit everything). Target: a breach halts every
runner **within the same tick, under 500 ms**.

Pre-registered universes: `NIFTY_50`, `TOP_10_CRYPTO`, `TOP_20_CRYPTO`, plus
custom registration. Correlation groups drive the concentration warning.

---

## 7b. Strategy Performance Analytics & Parameter Optimization

Two complementary systems for evaluating and improving strategies:

### Performance Analytics (Live/Paper)

The analytics engine (`src/backtest/api/analytics_service.py`) computes quantitative performance metrics on-demand from running strategies:

| Category | Metrics |
|----------|---------|
| **Risk-adjusted returns** | Sharpe, Sortino, Calmar ratios |
| **Drawdown analysis** | Max drawdown, duration, underwater curve |
| **Trade statistics** | Win rate, expectancy, streaks, holding time |
| **Equity curve** | Total return, CAGR, volatility, downside deviation |
| **Edge degradation** | Detects when live performance diverges from backtest |

Analytics support multiple periods (`7d`, `30d`, `90d`, `1y`, `all_time`) and handle both equity and option strategies with appropriate adjustments. The system merges in-memory trades with persisted database history for long-running portfolios (>200 trades).

**Access:** `/analytics` UI tab, or `GET /api/analytics/strategies/<name>` API.

### Parameter Optimization

The optimization engine (`src/backtest/optimization/`) systematically searches strategy parameter spaces to find robust combinations:

**Search Methods:**
- **Grid:** Exhaustive search (best for ≤3 params)
- **Random:** Uniform coverage (quick exploration)
- **Bayesian:** Gaussian process + expected improvement (efficient for expensive backtests)
- **Genetic:** Population-based evolution (escapes local optima)

**Validation Pipeline:**
```
Config → Baseline → Search → Sensitivity → Walk-Forward → Robustness Score → Results
```

Key features:
- **Walk-forward validation:** Out-of-sample testing with rolling train/test splits
- **Sensitivity analysis:** 1-D sweeps around best point, re-centering up to 3 rounds
- **Robustness scoring:** 0-10 score based on WF degradation, plateau width, trade stability
- **Constraint filtering:** Min/max Sharpe, drawdown, trades, profit factor
- **One-click apply:** Apply winning params to paper/live runners with full audit trail

**Safety guards:**
- Live application requires explicit confirmation AND walk-forward pass AND robustness ≥6
- Fails closed if flagged as overfitted
- One-click rollback to previous parameters via audit log

**Access:** `/optimize` UI tab, or `POST /api/optimize/runs` API.

### How They Work Together

Typical workflow:
1. Run strategy with current params → review analytics (identify weaknesses)
2. Optimize parameters targeting those weaknesses (e.g., high drawdown → optimize for Calmar)
3. Validate with walk-forward (ensure not overfit)
4. Check robustness score (<6 = caution)
5. Apply to paper runner → monitor 30 days
6. If successful, apply to live (with confirmation)

See [docs/STRATEGY-PERFORMANCE-ANALYTICS.md](STRATEGY-PERFORMANCE-ANALYTICS.md) for complete documentation.

### Consolidated P&L & Tax Reporting (PRD-002)

The reporting system (`src/backtest/reporting/`) produces a single consolidated statement across every broker and both books (live + paper), with correct Indian tax classification, estimated tax liability, and exportable annexures:

**What it does:**
- Classifies every closed trade into the correct head of income under the Income-tax Act, 2025
- Computes gross P&L → itemised fees (brokerage, STT, exchange, SEBI, stamp, GST) → net → estimated tax → net-after-tax
- Groups results by broker, mode (live/paper), tax category, and strategy
- Reconciles platform P&L against broker contract notes with tolerance bands
- Exports PDF statements, ITR annexures (xlsx/csv/json), and uncapped trade ledgers
- Sends monthly email reports on the 1st of each month (dry-run to outbox unless SMTP configured)

**Tax Classification (correct as of FY 2026-27):**

| Activity | Head of Income | Rate | Loss Rules |
|---|---|---|---|
| F&O (futures & options) | Non-speculative business income | Slab rate (default 30%) | Set off against any income except salary; carry forward 8 years |
| Intraday equity | Speculative business income | Slab rate (default 30%) | Only against speculative gains; carry forward 4 years |
| Delivery equity ≤12 months | Short-term capital gain (s.111A) | 20% | Only against capital gains; carry forward 8 years |
| Delivery equity >12 months | Long-term capital gain (s.112A) | 12.5% above ₹1,25,000 exemption | Only against capital gains; carry forward 8 years |
| Paper trading | Not taxable | N/A | Never enters a tax return |
| Unknown instrument | Fail-closed `UNCLASSIFIED` | Error | Report flagged for manual review |

**Key design decisions:**
- **STT is deductible against business income** (F&O + intraday, per s.36(1)(xv)) but **NOT against capital gains** (proviso to s.48) — a subtlety that quietly matters on delivery trades where STT is 0.1% on both legs
- **Losses produce carry-forward**, never a negative tax bill
- **Paper trading is excluded from tax** entirely (zero costs, zero revenue)
- **Unknown instruments fail-closed** rather than guessing a category (silently misclassifying inputs is how wrong numbers get filed)
- **One trade found twice is counted once** — deduplication prevents double-counting when multiple sources report the same fill

**Configuration:**
Rates, reconciliation tolerances, and mailer settings live in `config/reporting.yaml`:
```yaml
tax:
  stcg_rate: 0.20                    # Short-term capital gains rate
  ltcg_rate: 0.125                   # Long-term capital gains rate
  ltcg_exemption: 125000             # Annual LTCG exemption threshold
  business_slab_rate: 0.30           # Marginal slab rate for business income
  cess_rate: 0.04                    # Health & education cess on tax

reconciliation:
  pass_tolerance: 10                 # ≤₹10 apart = rounding, not signal
  warn_tolerance: 100                # <₹100 apart = warning
  warn_pct: 1.0                      # ... or under 1% of note's P&L

monthly_email:
  enabled: false                     # Nothing sent until true AND smtp.host set
  send_on_day: 1                     # Day of month
  send_at_hour: 9                    # 24h clock, IST
```

Override the config file path with `REPORTING_CONFIG_PATH`. Every export carries the "estimate only — verify with a chartered accountant" disclaimer.

**API endpoints:**
- `GET /api/reporting/pnl/consolidated?from_date=&to_date=&include_paper=&brokers=&demo=` — The consolidated report
- `GET /api/reporting/config` — Tax rules + thresholds (no secrets)
- `POST /api/reporting/pnl/export/pdf` — PDF statement
- `POST /api/reporting/pnl/export/itr?format=xlsx|csv|json` — ITR annexures workbook
- `POST /api/reporting/pnl/export/trades` — Trade ledger CSV
- `POST /api/reporting/pnl/reconcile` — Contract-note reconciliation `{broker, contract_note_pnl, contract_note_fees?}`
- `POST /api/reporting/email` — Monthly email (dry-run writes `.eml` to `var/reporting/outbox`)

**Fail-soft guarantees:**
- Missing database → empty report with warning (never 500)
- Malformed dates/unparseable numbers → 400 (silently guessing inputs is how wrong numbers get filed)
- Dead source → only its rows lost, rest of report intact
- No secrets stored in config files (SMTP password via env var `REPORTING_SMTP_PASSWORD`)

**Filing guidance included:**
- F&O + intraday + capital gains → **ITR-3** (Schedule CG A3/B3 + PGBP)
- s.44AD presumptive route cannot carry capital gains
- Delivery-only book → ITR-2

Access: `/reporting` UI page, or API endpoints above. See [docs/WEB-UI.md](WEB-UI.md) §4 for full page elements.

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
| Order place / modify / cancel | `brokers/mstock.py` (ticket P3.2) |
| Order book + fill polling | `get_order_book()`, `poll_fill()` |
| Multi-leg option orders with rollback | `options/live_trading.py` → `LiveOptionTrader` |

### What still does not exist

- **No equity order path on the replay engine.** The `/forward` replay and the
  single-run loop trade against `OrderLedger`/`PaperBroker` simulated fills.
  (The Command Center *does* have a live seam now: `forward/live_gateway.py`
  routes `mode='live'` runner orders to the broker and polls their fills.)
- **No live-credentials exercise.** The order client and `LiveOptionTrader`
  are tested against mocks (20 live-trading tests); the documented live
  dry-run against a real mStock session (PRD task T9.5) is still pending.

### Options book persistence

The earlier claim "no persistence for the options book" is obsolete: option
structures persist in the `trade_structures` DB table and in the portfolio
state file (`PORTFOLIO_STATE_PATH` — `forward/state_store.py` snapshots
structures and the forward bridge's book), and are rehydrated on startup.

### How the live path is gated

`brokers/base.py` composes two contracts: `BrokerAuthBase` (login / TOTP /
session / logout) and `BrokerOrderBase` (`place_order` / `modify_order` /
`cancel_order` / `get_order_book` / `calculate_order_margin`). A live broker
must implement both. Two gates sit in front of any real order:

1. **Session guard** — `place_order()` calls `_require_session()` and fails
   cleanly unless an authenticated mStock session is active.
2. **Dry-run default** — `LiveOptionTrader(broker, dry_run=True)` logs the
   exact multi-leg payload that *would* be sent and places nothing. Going
   live means `dry_run=False` with real credentials.

So: **the HTTP order layer is real and tested, the decision engines remain
paper-first, and one narrow, dry-run-default path connects them.** The
paper-side groundwork (`OrderLedger`, production-shaped `orders` table) is
unchanged and still backs every simulated run.

### One more trap

The old `forward/live_engine.py` (a standalone `LiveForwardEngine` with zero
importers) is **gone** — the live seam today is `forward/live_gateway.py`
(`LiveEquityGateway`), which *is* wired: `portfolio_manager.py` builds a
gateway per broker, submits live orders through it and runs `poll_pending()`
on every tick. Paper runners never see it.

---

## 9. Architecture map

```
src/backtest/
├── data/            Data sources: synthetic, CSV, DB, mStock + universes
├── strategy/        Base class, registry, adapter
├── strategies/      13 built-in strategies + plugin discovery
├── engine/          Backtester, metrics, trade walk, plotting
├── simulator/       Costs, fills, sizing, risk, portfolio accounting (18 files)
├── options/         Options: selectors, structures, paper/live execution,
│                    Greeks, margin, fees, expiry (11 files)
├── instruments/     Instrument model: equity, option, expiry calendar (6 files)
├── forward/         Forward testing: engine, runners, portfolio manager,
│                    risk supervisor, order ledger, feeds, live gateway
├── live/            mStock auth, API client, preflight, time & data validation
├── brokers/         Broker auth + order contract + mStock + Dhan + session manager
├── marketdata/      Tick→bar aggregation, quality checks, time sync
├── db/              SQLAlchemy models, connection manager, config
├── api/             Flask blueprints (the REST surface) + run ledger
├── web/             Flask app: pages + static + templates
├── adapters/        BacktestAdapter — one result shape for every UI
├── analysis/        Strategy comparison
├── alerts/          Alert manager + outbound notifier
├── pine/            Pine Script v5 converter (parser → codegen, stateful path)
├── config_manager/  Layered YAML + env + profile config loader
├── cli.py           Command-line interface
└── runner.py        Orchestrates data → strategy → engine → results
```

**Scale:** 215 Python modules, ~95,000 lines in `src/backtest/`; 204 test
files, 4,849 tests collected.

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

The Flask app's core config is env + CLI flags only (auto-loaded from `.env`
by `backtest/__init__.py`). Its *subsystems* read their own YAML
(`config/database.yaml`, `config/reporting.yaml`, `config/alerts.yaml`,
`config/pine_converter.yaml`), each layered over the env vars:

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
| `market_holidays` | NSE exchange closures 2022–2026 (fetch skip + market-status) |
| `backtest_runs` / `backtest_compare_runs` / `backtest_run_series` | Run ledger — every completed backtest/compare, migration 018 |
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

# One-time: copy .env.example to .env (Postgres FORWARD_TEST_DB_URL + broker
# credentials); the package auto-loads it. pyproject.toml pins
# pytest pythonpath=src.

# Web UI with synthetic data — no credentials, no DB needed
PYTHONPATH=src python -m backtest.web.app --host 0.0.0.0 --port 5000 --source synthetic

# With real data from PostgreSQL
PYTHONPATH=src python -m backtest.web.app --host 0.0.0.0 --port 5000 --source db
```

Then open `http://localhost:5000` → Backtest → pick a strategy → **Run Backtest**.

`PYTHONPATH=src` is required for a plain checkout — the package is not
pip-installed (pyproject.toml exists mainly for pytest's `pythonpath`). The
broker (mStock) session is **per-process**: log in from the UI after every
restart or the live feeds/runners pause.

### CLI

```bash
backtest list                                       # list strategies
backtest run --strategy sma_crossover --from 2024-01-01 --to 2024-12-31
backtest compare --strategies sma_crossover,rsi_reversion --from D1 --to D2
backtest preflight                                  # DNS / HTTPS / auth checks
backtest papertrade --mode walkforward --strategies X --from D1 --to D2
```

### Web pages

`/` · `/backtest` · `/compare` · `/forward` · `/portfolio` · `/portfolio/live` ·
`/portfolio/paper` · `/data` · `/health`

### REST API

- **Backtest** — `POST /api/backtest/run`, `/run-many`
- **Strategies** — `GET /api/strategies`, `/api/strategies/<name>/params`
- **Forward** — `POST /api/forward/start`, `/stop`; `GET /status`, `/sessions`,
  `/trades`, `/equity`
- **Portfolio** — `GET /summary`, `/universes`, `/runner/<id>`, `/stream` (SSE),
  `/buckets`; `POST /runner/create`, `/runner/<id>/control`, `/control/<action>`,
  `/emergency_stop` — all control endpoints accept `?mode=live|paper` for
  scoped bulk actions
- **Options** — `GET /api/options/summary`, `/positions`, `/greeks`;
  `POST /api/options/structures/<id>/close`, `/api/options/expiry/process`
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

**Web app** — production runs **gunicorn** (in `requirements.txt`,
`gunicorn -c gunicorn.conf.py backtest.web.wsgi:app` — one worker, 8 threads,
`preload_app` deliberately off because per-process daemon threads start at
import). The Flask dev server (`python -m backtest.web.app`) remains the
Windows/dev path.

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

State survives a restart better than it used to: command-center config/books
persist via `PORTFOLIO_STATE_PATH` (runners restore PAUSED) and options
structures via `trade_structures`; broker sessions, forward replay sessions
and the feed registry are still in-process and lost on restart.

---

## 14. Testing and quality

- **~4,800 tests collected** (204 test files), 4 skipped needing real mStock
  credentials. Portfolio-specific: 116 tests covering bucket state, breaker
  independence, flow semantics, scoped API endpoints, and UI views.
  Options-specific: 226 tests across 8 modules (expression, paper, live,
  Greeks, fees, expiry, web, integration) plus 52 instrument-model tests.
- 91 JavaScript behaviour assertions across 8 Node harnesses (`tests/js/*.mjs`).
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

1. **Decision engines are paper-first.** The `/forward` replay and single-run
   loop never place real orders. Live seams exist (multi-leg options via
   `LiveOptionTrader`, equity via `forward/live_gateway.py`) but have not been
   exercised against the live mStock API (pending task T9.5).
2. **`/forward` is a replay** of historical data, not a live feed — the
   Command Center's portfolio runners are the live-feed path.
3. **In-memory state** — forward replay sessions, broker sessions and the feed
   registry are lost on restart (portfolio state and option structures do
   persist — see §13).
4. **Single-worker only** — see §13.
5. **Flask dev server** for Windows/dev; gunicorn (one worker) for production.
6. **Timeframe is cosmetic** on synthetic and CSV sources (daily bars only).
7. **DB connection hoarding** — the Flask `:5000` process accumulates ~99/100
   PostgreSQL connections over ~9h; known, untraced (tracker).
8. **Daily-loss breaker UTC window** — the day anchor is keyed on the UTC
   date, so 00:00–05:30 IST can be blind to the IST trading day's losses.
9. **Two dead config files** — `market_data.yaml`, `time_sync.yaml`.
10. **Money is inexact on SQLite** — `NUMERIC` degrades to float. Use PostgreSQL
    for reported numbers.
11. **Broker cost rates drift** — point-in-time figures, re-verified
    2026-09-28 (equity FY 2024-25; F&O STT per Budget 2026, effective
    2026-04-01). India changes rates regularly — `config/brokers.yaml` warns to
    check a recent contract note before trusting cost-sensitive results.

---

## 17. Where to read next

| Question | File |
|---|---|
| **How do I actually use the UI — every page and tab?** | `docs/USER-GUIDE.md` |
| How does the whole thing fit together? | `docs/ARCHITECTURE.md` |
| How does the backtest engine work? | `docs/BACKTEST-ENGINE.md` |
| How do I add a strategy or data source? | `docs/STRATEGY-AUTHORING.md` (strategies), `docs/DATA-SOURCES.md` (sources) |
| What are the strategies doing? | `docs/STRATEGY-AUTHORING.md` §6 (catalog + engine guarantees) |
| How does forward testing work? | `docs/FORWARD-TESTING.md` |
| Multi-strategy portfolios? | `docs/PORTFOLIO-CENTER.md` |
| Options paper & live trading? | `docs/OPTIONS-PAPER-LIVE.md` |
| Live/Paper separation design | `docs/archive/REFACTOR-PORTFOLIO-LIVE-PAPER-SEPARATION.md` (shipped) |
| Schema and migrations? | `docs/DATABASE.md`, `db/archive/DB-IMPLEMENTATION-GUIDE.md` |
| Something is broken | `instructions/ENGINEERING-NOTES.md`, `docs/LOGGING.md` |
| What is done, what is planned? | `docs/OPEN-ITEMS-TRACKER.md` (planned work noted in `docs/MULTI-BROKER-PRD.md`) |
| Invariants I must not break | `PROJECT-CONTEXT.md` |
| mStock endpoints | `docs/archive/mstock-typea-api-reference.md` |
| Consolidated P&L & tax reporting? | `docs/WEB-UI.md` §4, `config/reporting.yaml` |