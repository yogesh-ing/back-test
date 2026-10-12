# Backtester Project Context

## Quick State
Build offline backtesting engine + live mStock connectivity. 19/22 acceptance tests pass. Cards 0-6 complete. Card 07 Phase 1 done (CLI wired). Live polling + state persistence deferred.

**Latest work (2026-10-06):** instrument lists re-architected — the run pages
load a static name-only list (~96ms, was 3.4–5.6s), availability is answered by
the backtest request with the dates that DO exist, a timeframe that cannot be
built from the stored bars is now refused instead of silently returning daily
bars as minutes, and the Data tab loads one payload for both its list and its
table (1054ms → 271ms). **Start with
`docs/HANDOFF-2026-10-06-instrument-list-and-ingest.md`** — §4 lists what the
1-min-only ingest work must still handle, including a silent timezone trap a
daily check cannot catch.

**Also shipped (Sep–Oct 2026):** backtest run-ledger persistence (migration 018,
merged 2026-10-03 — every completed run/compare persists payload + provenance,
`api/backtest_run_store.py`); the Data tab rework (curated
`stock-list/nse_ind_nifty200list.csv` + NSE index universe, dedupe + 502-retry
+ holiday-aware fetch — PRs #39/#40/#42); broker session-expiry alerts (in-app
widget + Telegram + re-login popup, PR #41, `brokers/session_manager.py`);
`market_data_cache` UTC→IST clock repair (`data/base.bar_timestamp` + a
45.6M-row migration, commit f2eb7e3); the NSE holiday table seeded 2022–2026
(`tools/seed_market_holidays.py`, migration 017) with holiday-aware fetch skip.
The 2026-10 swing battery (`plugins/strategies/swing_*`) failed all its gates —
nothing was deployed.

## Architecture
```
src/backtest/
├── data/       # Data sources (synthetic, CSV, mStock API)
├── strategy/   # Strategy base + registry (13 built-ins + plugin discovery)
├── engine/     # Backtester (vectorized + risk-aware paths), metrics, plotting
├── forward/    # Walk-forward + paper trading runner
├── live/       # mStock auth (TOTP/OTP), API client, preflight checks
└── cli.py      # Commands: list, run, compare, preflight, papertrade
```

## Invariants (Must Hold)
1. **No-lookahead**: Position @ bar t = signal @ bar t-1. Use `target.shift(1)`
2. **Signal clipping**: target ∈ [-1, 1] (long/flat/short)
3. **Per-bar consistency**: risk-aware path mirrors vectorized math
4. **Stop/target exits**: exit forced intrabar, position zeroed, cost added, re-entry blocked
5. **Walkforward reconciliation**: equity matches vectorized backtest (tol 1e-5)
6. **Trade accounting**: one source of truth — `engine/trades.py` feeds both `compute_metrics`
   and `BacktestAdapter`. Trade P&L is equity-based (costs included), `num_trades` counts
   round trips (an open position counts, marked to final close) and `win_rate` is over
   **closed** trades only. Never re-derive either number from the position sign.
7. **Everything is observable**: `backtest.logging_config.configure_logging()` is installed by
   every entry point; `--log-level DEBUG` must explain any empty/flat result (`docs/LOGGING.md`).

## Data Contract
Canonical OHLCV frame: lowercase cols (open, high, low, close, volume), tz-naive DatetimeIndex ascending.

## CLI Commands
```
backtest list                           # List all registered strategies (built-ins + plugins)
backtest run --strategy X --from D1 --to D2   # Single backtest
backtest compare --strategies X,Y,Z --from D1 --to D2  # Multi-strategy
backtest preflight                      # DNS/HTTPS/auth checks
backtest papertrade --mode walkforward --strategies X --from D1 --to D2  # Paper trade
```

## Test Status
- ✅ 3418 passed, 28 skipped, 4 xfailed, 3 xpassed — `PYTHONPATH=src pytest tests/ -q`
  (as of 2026-09-28, post Consolidated P&L / PRD-002 + the broker-catalogue grouping;
  the count drifts per ticket). Skips are environment-gated (mStock credentials,
  optional readers, node absent). The xfail/xpass pair in
  `tests/strategies/test_registry_template.py` is the per-bar-budget probe: it flips
  with machine load, not with code (verified: 4/3 in isolation, 5/2 under a full run).
- ✅ 91 JS behaviour assertions across 8 Node harnesses (`tests/js/*.mjs`) — positions
  actions, Orders tab (amend + aging), the P&L page's view models, and the Cost & Risk
  broker-card grouping (extracted from the template's inline script) in a stub DOM
  (`tests/test_web_components.py`, `tests/test_reporting_ui.py`,
  `tests/test_settings_panel.py`; skipped without node)
- ✅ Reporting suite: `tests/test_reporting_{core,exports,email,api,ui}.py` +
  `tests/test_reporting_sources.py` — tax classification/estimate, ledger counting,
  the DB read path (broker attribution never guessed, paper zero-costs, estimated
  stacks flagged), exports read back (PDF/OOXML), the mailer's never-send-by-accident
  rule, and every endpoint incl. its 400s. Package coverage 93% (every module ≥88%).
- ⚠ Sandbox note: rebuild the venv each session —
  `python3 -m venv /home/user/.venv && /home/user/.venv/bin/pip install -q -r requirements.txt pytest-cov flake8`

## Key Files & Current State

| File | Purpose | Status |
|------|---------|--------|
| engine/backtester.py | Vectorized quick-screen engine | ✅ Stable (lagged signals) — canonical path is `backtest_driver` |
| engine/backtest_driver.py | Backtest on the shared engine loop | ✅ New (P2.1) — same loop as `PaperRunner` |
| engine/trades.py | Trade walk + stats (cards and table share it) | ✅ New (G1/G2) — equity-based, open trade excluded from win_rate |
| simulator/engine_loop.py | Canonical bar-clock loop (submit → fill at next bar's open) | ✅ New (P2.1) |
| simulator/execution.py | `OrderExecutor` — `submit()`/`step()` bar clock + `execute()` | ✅ New (P1.3) |
| forward/paper_runner.py | Walk-forward / live paper CLI + `PaperRunner` (canonical loop) + command-center `StrategyRunner`/`OrderLedger`/`PaperBroker` | ✅ Re-architected (P1.4) |
| forward/strategy_adapter.py | Strategy → Signal → Order, **no fills** | ✅ Signal-only (F-01) |
| logging_config.py | Handlers, levels, request ids | ✅ New (U1) — see docs/LOGGING.md |
| auth.py | TOTP (HMAC-SHA1) + OTP flows, session cache | ✅ Complete |
| mstock.py | API client + data normalization | ✅ Complete |
| preflight.py | DNS/HTTPS/auth checks | ✅ Complete |
| cli.py | All 5 commands wired | ✅ Complete |
| optimization/ | Parameter optimization engine: config → search (grid/random/Bayesian/GA) → sensitivity → walk-forward → analysis → store; apply-to-runner + rollback | ✅ New — docs/OPTIMIZATION-ENGINE.md, migrations 009–013 |
| api/optimize.py | `/api/optimize/*` REST (503 without a DB) | ✅ New |
| forward/portfolio_manager.py | Command center: runners/buckets/breakers + positions/orders read + manual position actions | ✅ LOM (2026-09-23) |
| data/provenance.py | Engine + data-source labels, warnings and the `provenance` record stamped on every result | ✅ New (PRD §1.1/§1.2) |
| web/static/js/components/provenance.js | Renders the Engine/Data badges + non-dismissable banners on a result page | ✅ New (PRD §1.1/§1.2) |
| data/coverage.py | The union of known instruments + what has bars, behind `GET /api/data/coverage` | ✅ New (PRD §1.3) |
| data/base.periods_per_year() | Timeframe → annualisation factor (252 × bars/day; weekly 52) | ✅ New (PRD §1.4) |
| web/static/js/components/symbol_picker.js | The one instrument picker (search + All/Equity/Index/F&O) used by 3 pages | ✅ New (PRD §1.3) |
| web/static/js/components/timeframes.js | UI timeframe vocabulary + "only what this symbol has" dropdown | ✅ New (PRD §1.4) |
| web/static/js/components/position_actions.js | Positions-table action buttons + their modals | ✅ LOM (2026-09-23) |
| web/static/js/components/orders_tab.js | Orders tab: ledger rows, slippage, cancel, badge, amend + aging (Phase 3) | ✅ LOM (2026-09-23) |
| api/backtest_run_store.py | Backtest run ledger — `backtest_runs`/`backtest_compare_runs`/`backtest_run_series` (migration 018) | ✅ New (2026-10-03) |
| data/base.bar_timestamp() | Naive bars = IST exchange wall clock, bound as aware UTC (45.6M-row repair, f2eb7e3) | ✅ Fixed (2026-10-06) |
| brokers/session_manager.py | Concurrent per-broker sessions (v2) + expiry monitor → alerts + re-login (PR #41) | ✅ New (2026-09/10) |

## Known Limitations
- Timeframe is cosmetic on synthetic/CSV sources (daily bars only) — see gap G6 / U2
- Command-center state persists (`PORTFOLIO_STATE_PATH`): configs/book/anchors/manual levels
  round-trip and runners restore PAUSED. Two deliberate exceptions: breaker latches are not
  re-armed on boot (session-scoped), and ledger orders are not persisted (the Orders tab
  starts empty after a restart)
- **Live broker fills still open** (findings F-12): bucket UI + mode/source tags done
  (P4.1), but `BrokerFillProvider` + `MStockLiveFeed` wiring, `poll_fill` in the broker
  ABC, and bucket-level risk anchors remain
- Auth tests require mStock credentials (skipped)
- Flask `:5000` hoards ~99/100 PostgreSQL connections over ~9h — known, untraced
  (see `docs/OPEN-ITEMS-TRACKER.md`)
- Daily-loss breaker day-anchor is keyed on the UTC date, so the 00:00–05:30 IST
  window can be blind to the IST trading day's losses (tracker)
- Equity 1-min backfill interrupted at 77/200 symbols (2026-10-04 broker 502
  storm); index 1-min backfill: NIFTY done, BANKNIFTY/FINNIFTY/MIDCPNIFTY paused
  pending go-ahead, SENSEX/INDIAVIX deferred (tracker)

## Next Steps (If Continuing)
1. Wire `mode='live'` command-center runners through `BrokerFillProvider` + `MStockLiveFeed` (F-12)
2. Add portfolio/runner state persistence across restarts (V2)
3. Test mStock auth with real credentials
4. ~~Finish the docs pass for `instructions/ARCHITECTURE-BLUEPRINT.md`~~ — blueprint archived to `docs/archive/` (superseded by `docs/ARCHITECTURE.md` + `graph.txt`)

## Live Order Management (2026-09-23)
- Enhanced the positions tab (flat rows across runners, equity + option structures) with
  an **Actions** column: Modify SL / Modify Target / Close 50% / Close All → modals →
  `POST /api/portfolio/position/action`.
- Added a sibling **Orders** tab: `GET /api/portfolio/orders`, `POST .../<coid>/cancel`,
  status filters, slippage (adverse-positive per unit), order aging on PENDING rows.
- **Phase 3 (advanced order management):** amend a working order at its venue
  (`POST .../<coid>/modify` — quantity/limit, venue asked first, original terms and coid
  preserved), order-aging bands (warn 60s / alert 5min, one audit entry per band) and
  bounded opt-in **auto-retry** of refusals (`RunnerConfig.retry_policy`; live placement
  errors are never auto-resent — a lost acknowledgment can mean the order already exists).
  Closing a live order goes through the venue too: a locally-cancelled order that still
  rests at the broker is how a position opens after the operator was told it was dead.
- Manual levels are prices (share price / net premium per unit), validated server-side
  against the live mark, checked on every bar and on stress markdowns; option structures
  close atomically. A live close returns `placed`, never a fake fill.
- Endpoints, semantics and the two tabs: `docs/PORTFOLIO-CENTER.md`.

## Consolidated P&L & Tax Report (PRD-002)
- `src/backtest/reporting/` — one statement across every broker and both books:
  `sources.py` (DB ⋈ portfolios, in-memory runners, labelled demo book),
  `records.py` (`TradeRecord`, `Period`, fee re-estimation), `tax.py`
  (classification + estimate + turnover/audit), `consolidator.py`
  (`ConsolidatedPnL.generate_report` → `PnLReport`), `reconciliation.py`
  (contract-note PASS/WARNING/FAIL), `exports/` (stdlib PDF + OOXML writers,
  Schedule CG / PGBP / STT / turnover / guidance annexures), `email_report.py`
  (monthly email; dry-run to `var/reporting/outbox` unless SMTP is configured).
- Tax heads: F&O = non-speculative business income and intraday equity =
  speculative business income (both slab; `business_slab_rate` default 30% as a
  safe upper bound), delivery ≤12m = STCG 20%, >12m = LTCG 12.5% above ₹1,25,000,
  paper = never taxable, unknown = fail-closed `UNCLASSIFIED`. STT is deductible
  against business income (s.36(1)(xv)) but **not** against a capital gain
  (proviso to s.48); losses produce a carry-forward, never a negative tax.
- Rates/tolerances/mailer live in `config/reporting.yaml` (`tax:`,
  `reconciliation:`, `monthly_email:`, `smtp:`, `exports:`), overridable with
  `REPORTING_CONFIG_PATH`. Every export carries the "estimate — verify with a
  chartered accountant" disclaimer.
- API: `/api/reporting/pnl/consolidated`, `/config`, `/pnl/export/{pdf,itr,trades}`,
  `/pnl/reconcile`, `/email`; page: `/reporting` (`docs/WEB-UI.md`).
- Filing guidance is explicit: F&O + intraday + capital gains → **ITR-3**
  (Schedule CG A3/B3 + PGBP); the s.44AD/ITR-4 presumptive route cannot carry
  capital gains, and a delivery-only book → ITR-2 (see the `guidance` sheet).

## Cost & Risk Settings — broker catalogue (`/settings`)
- The panel lists every broker the fee engine can price, grouped by adoption:
  **In use** (active broker + segment/data-routing brokers, expanded) and
  **everything else collapsed** (config/brokers.yaml entries + built-in presets).
  A yaml-only broker such as `dhan` used to be invisible here, so it could not
  be edited or contract-note validated; it now appears with its file rates.
- Provenance chips: `config/brokers.yaml`, built-in preset, or *"differs from …
  this row wins"* when a panel edit shadows the file. Internal marker rows
  (`__active__`, `__live_kill_switch__`) are filtered out — they are state, not
  brokers.
- Resolution order for a cost model is **DB row → `config/brokers.yaml` → built-in
  preset**; the panel's save is an audited override, and a contract-note PASS
  stamps the profile (which is what the live-arming gate checks).

## Build Dependencies
Python 3.10+, pandas, numpy, requests, python-dotenv, matplotlib, pytest

## Key Constants
- PYTHONPATH=src (module imports)
- Default capital: 100k
- Default commission: 0.03%
- Default slippage: 0.05%
- Walk-forward equity tolerance: 1e-5 (reconciliation)

## Backtest & Compare — Engine & Data Provenance (PRD §1.1 + §1.2, 2026-09-29)
Implements **Part 1 §1.1/§1.2** of `docs/backTest-enhance.md` — the "bugs first"
slice. Later PRD sections are still open.

- **Single authority:** `data/provenance.py` owns the engine and data-source
  vocabulary, the badge labels, the advisory warnings and the `provenance`
  record. Nothing in the API, the templates or the JS re-declares a label.
  `data/source_tags.py` (the 3-way run taxonomy) is unchanged and still owns
  state-file tags.
- **Stamped on every result:** `/api/backtest/run`, each `/api/backtest/run-many`
  slot, the run-many shared block, and every `/api/optimize/runs*` payload
  (`run.provenance`). Fields: `data_source`, `data_source_label`,
  `data_source_real`, `data_fetch_date`, `symbol`, `timeframe`, `date_range`,
  `data_from`/`data_to` (what the candles **actually** covered), `bars_count`,
  `engine_used`, `engine_label`, `engine_canonical`, `engine_tier`, `warnings`.
- **Engine naming:** the record speaks one vocabulary — `backtest_driver` (fill-exact
  canonical), `quick_screen` (approximate), `options` (multi-leg), `mixed`
  (Compare slots that disagree). `backtestConfig.engine: "driver"` is the
  *same* engine as `backtest_driver` and is aliased, not renamed.
- **Quick-Screen is opt-in:** the default path was already the driver; §1.1 adds
  the explicit **Fast Preview** toggle on Backtest and Compare (Compare applies
  it to *every* slot — engine is a shared condition, never per-slot) and the
  approximate-results warning. Slots that disagree are stamped `mixed`.
- **Banners:** non-real data (synthetic/CSV, and anything unrecognised — fail
  closed) → red "Real-data certification required before paper testing";
  quick-screen → yellow approximate warning. Advisory only: no hard block.
- **Optimize needs no migration:** the run's stamp is derived from the columns
  that already exist (`backtest_config` + `analysis.stats`), so it describes the
  run rather than the app's current config. Part 2 §2's first-class attestation
  columns are still to come.
- **Fetch date:** `DbSource.last_ingested_at()` reads `MAX(ingested_at)`; a
  cached-feed result with no answer falls back to its newest bar. Synthetic
  data reports no fetch date rather than inventing one.
- **Tests:** `tests/test_provenance.py` (31), `tests/test_api_backtest_provenance.py`
  (13), `tests/js/test_provenance.mjs` (13, via `tests/test_web_components.py`),
  plus the optimize-API cases in `tests/optimization/test_api.py`.

## Symbol Coverage & Timeframes (PRD §1.3 + §1.4, 2026-09-29)
The second "bugs first" slice. Part 1 §1 is now complete.
**Re-architected 2026-10-06 — read this section first if you touch instrument
lists.** The list no longer carries coverage; availability is answered when a
backtest is requested. Full rationale, contracts and the hand-off list for the
ingest work: `docs/HANDOFF-2026-10-06-instrument-list-and-ingest.md`.

### `GET /api/data/coverage` — one endpoint, three shapes
`data/coverage.py` merges sources and returns the **union**: a symbol is listed
if ANY source knows it. A missing `<option>` reads as "this does not exist",
which was the original reported bug.

| Shape | Request | Sources | Cost |
|---|---|---|---|
| **names** (run pages) | `names_only=1` | shipped universe ∪ `SELECT DISTINCT symbol FROM market_data_cache` | ~10ms local, ~100ms on a 1M-bar cache |
| **universe** (Data tab) | `include_catalogue=0` | the above **+ the bar aggregate** (real coverage on each row) | ~280ms |
| **full** (compat default) | *no flag* | the above + the `instruments` catalogue | 3.4–5.6s — no UI caller left |

- The catalogue (mStock scriptmaster, ~140k rows) contributes ~140k contract
  rows that no tab can fetch by symbol; an F&O contract contributes its
  *underlying* (and creates it — the exchange listing a TCS future is itself the
  claim that TCS trades). Reading it costs ~1.4s and merging it ~1.8s
  (`_derivative_underlying` runs twice per row, 280k regex calls). **Absent
  means "as before"**: only an explicit `0` skips it, so no existing caller
  changed behaviour.
- Other params: `q`, `types` (incl. `fno`), `available`, `curated`, `limit`,
  `offset`, `refresh`. `names_only` wins over `available` (contradictory).
- `instrument_type` stays exactly `equity | index | futures | options`; the F&O
  tab is a filter, not a fifth type.
- The response publishes `instrument_types` and `timeframes` (the canonical
  vocabulary, finest first) so clients stop declaring their own copies.

### `coverage_known` — "not asked" is not "no data"
Names-only rows carry `data_available: null`, `bars_count: null`, empty
timeframes and **no `hint`**, plus `coverage_known: false`. Null means *nobody
looked*; `false` would mean *has no bars* and would grey out the entire list.
**`data_available` is null in that mode and null is falsy** — a consumer that
tests `!row.data_available` before `coverage_known === false` disables every
option. `available_total` is likewise `null` (not 0) and `db_available` is
`null` when the database was never consulted (the picker renders `false` as "no
data source connected", which would be a claim we had not checked).

### Availability is answered by the RUN, with dates
`POST /api/backtest/run` returns **HTTP 400** naming the requested timeframe,
the window that failed, and the dates the symbol DOES hold:

```
Symbol 'X' has no 1day data between 2026-10-01 and 2026-10-31.
Available — 1min 02 Sep 2026 to 30 Sep 2026 (4,817 bars).
```

It used to print the query's own bounds back at the user and name the internal
source timeframe ("no 1min data" when 1day was asked for). The reporter
(`DbSource._describe_stored`) never raises — a reporting failure degrades to a
generic sentence rather than replacing a useful error with a useless one. The
message is shown twice in the UI: a toast, and a **persistent banner**
(`#runError`, cleared at the start of the next run) because a 3s toast cannot
carry dates the user has to type.

### The one-way rule is enforced at read time
Resampling aggregates fine→coarse only. `DbSource.get_candles` refuses a
timeframe that cannot be BUILT from the stored bars (`_can_serve`, rank
comparison) instead of returning daily bars labelled as minutes — which is what
it did before, silently, and `list_symbols()` already refused the same request,
so the two disagreed. The servable list it reports is computed with the same
bars-cap the picker uses, so the message cannot promise a timeframe the dropdown
would not offer. Unknown spellings answer "can serve": the guard stops a
known-impossible request, and rejecting an unrankable one would break a working
run.

### The Data tab: ONE payload for the list and the table
`data_manager.js` requests `include_catalogue=0` once and both views render from
it. `/api/data/inventory` used to run its own `GROUP BY symbol, timeframe` — the
same query `load_bar_coverage` runs — so one page load scanned the cache twice
(1054ms → 271ms measured on a 1M-bar cache). It now reshapes the cached report
and asks for the **same cache key** the list asks for (asking for the curated
shape instead silently re-scans; a test pins this). Its response shape is
unchanged — published contract.
- Row labels gain the window (`1min · 4,817 bars · 02 Sep → 30 Sep`), formatted
  by string-slicing, never `new Date()` (which parses `2026-09-02` as UTC
  midnight and renders 01 Sep in a negative-offset browser).
- The table is one row per symbol with a Stored column (per-timeframe dates ride
  in the tooltip); per-timeframe dates come from `BarCoverage.dates_by_timeframe`,
  which the aggregate was already reading and discarding.
- A universe symbol with no bars is "not fetched yet" and stays listed — on this
  page that row is a tickable instruction, not a defect.

### Caches
The coverage cache is **keyed by shape** (`names` / `curated` / `universe` /
`full`, 60s TTL). One slot for several shapes meant a names-only request could
serve a cached full report — and a curated-only report could answer an
unfiltered request while silently dropping every scriptmaster symbol. A finished
fetch calls `invalidate_coverage_cache()`, which also clears
`symbols._CACHED_SYMBOLS`: that listing had **no expiry at all**, so the Forward
page kept showing the symbol set from boot and a symbol fetched minutes ago
stayed invisible until the process restarted.

### Shared picker
- `components/symbol_picker.js` replaces the three hand-maintained `<option>`
  lists. Backtest, Compare (one picker; slot timeframes follow the shared
  symbol), Optimize and the Portfolio spawn modal all mount it; all of them now
  get the names-only list, so **every row is selectable** — the run is what
  decides whether the data exists. `timeframesFor()` returns `[]` in that mode,
  which makes the timeframe dropdown fall back to the full canonical set.
- The Forward page still uses `/api/symbols` (out of scope) — now correctly
  invalidated on fetch.
- **2026-10-01 (issues.txt P1/S2, spawn form)**: the Portfolio Center's spawn
  modal mounts the shared picker for free equity strategies (`defaultTab:
  "equity"`) instead of a free-text ticker; an unanswered picker blocks submit.
  Strategies carry a `default_segment` that preselects the spawn segment.

### Tests
`tests/test_data_coverage.py` (62), `tests/test_api_data_coverage.py` (41),
`tests/data/test_db_source_missing_window.py` (12),
`tests/data/test_timeframe_derivation.py` (15),
`tests/data/test_window_bounds.py` (19), `tests/test_timeframe_periods.py` (31),
`tests/js/test_symbol_picker.mjs` (31) and `tests/js/test_data_manager_labels.mjs`
(16) — the two JS harnesses run via `tests/test_web_components.py`, which asserts
their exact counts, so **bump those numbers when you add cases**.

---

## Richer Metrics (PRD §2, 2026-09-29)

Implements **Part 1 §2** of `docs/backTest-enhance.md` — the richer metric
families. Part 1 §1 was the "bugs first" slice; §3–§6 and all of Part 2 remain open.

### Where the maths lives
`engine/metrics_risk.py` holds every §2 estimator as a pure function over an
equity curve, a return series or a list of trade P&Ls. `engine/metrics.py` stays
the orchestrator, so Backtest, Compare and Optimize all pick the metrics up from
one place, as §2 asks.

### Decisions worth knowing

- **CVaR 95% is an alias of ES 95%, not a second computation.** They are the
  same number under two names. The local `_var_es()` that used to live in
  `metrics.py` was deleted in favour of `metrics_risk.var_es`, so there is one
  definition of the quantile rather than two that can drift.
- **Sharpe standard error is Lo's (2002) `sqrt((1 + S²/2)/N)`**, not `1/sqrt(N)`.
  The simpler form understates the error exactly when the Sharpe looks good.
  `metrics_sections` also ships a 95% interval around the reported Sharpe.
- **Max drawdown duration runs peak → recovery, not trough → recovery**, and an
  unrecovered episode is measured to the end of the run *and flagged*
  (`max_drawdown_recovered: false`) rather than given a duration that implies a
  recovery that never happened.
- **Omega is capped at 100**, not `inf`. A curve with no losing bar has an
  undefined Omega, and `Infinity` does not survive JSON.
- **`kurtosis` is EXCESS (Fisher) kurtosis** — a normal distribution reads 0.00,
  not 3.0. The PRD's key name is kept; the convention is in the docstring.
- **`trade_count_flag` counts CLOSED trades** and ships as a **string**
  (`ok` / `warn` / `insufficient`) so the UI can distinguish the `warn` middle
  state. An open trade is not a result yet.
- **Drawdown/expectancy/duration families skip censored data**: `cvar_95`,
  `payoff_ratio`, the streaks and `avg/median_trade_duration_bars` are computed
  over closed trades only.

### `Trade.bars_held` — why it had to be added
§2 wants a real average/median trade duration. The walk in `engine/trades.py` is
the only place that knows the entry and exit bar positions, so it records the
count there and everything downstream reads it.

Two things that were wrong in the obvious implementation and are pinned by test:
1. A trade that exits on a **flat** bar books its exit cost on that bar, but it
   was **not held** on it. `exit_i - entry_i + 1` credits it with a bar it was
   flat for, which inflates every holding period on the page. Both closed-exit
   paths pass `bars` explicitly; only the still-open trade adds the inclusive 1.
2. An **open** trade is right-censored — held *at least* N bars — so it is
   excluded from the duration averages rather than averaged in.

Note that the pre-existing `avg_holding_bars` (`exposure x bars / num_trades`) is
**algebraically the same as the measured mean** whenever the trade spans tile the
curve, which they do by construction. It was never obviously wrong. The actual
gain from `bars_held` is the **median**, which that estimate cannot produce at
all — and the fact that the new number is a measurement rather than an inference
that happens to agree. `avg_holding_bars` is kept for the surfaces already
reading it.

### UI
`web/static/js/components/metric_sections.js` renders four `<details>` sections
below the existing metrics grid on the Backtest result page (Wireframe 1):
Risk & Tail, Drawdown Detail, Trade Quality, Statistical Confidence. Per §2.2 the
existing card grid is untouched — this component never writes to `#metricsCards`.

The `insufficient` banner is the one piece of state that changes how a reader
should read everything else, so it renders first, `role="alert"`, and has no
close control. `ok` and `warn` render a small flag chip and no banner.

Every row formatter returns `null` for a missing value so the row is **omitted**
rather than printed as a confident `0.00`; a section with nothing in it does not
render. Non-numeric values are coerced when possible and otherwise escaped
rather than dropped, because a row that silently disappears hides a number the
operator asked to see.

### Tests
`tests/test_metrics_risk.py` (59, one estimator at a time),
`tests/test_metrics_sections.py` (37, end-to-end through `compute_metrics` and
`BacktestAdapter.to_all()`), `tests/js/test_metric_sections.mjs` (19, via
`tests/test_web_components.py`), plus `bars_held` cases in
`tests/test_engine_trades.py`.

---

## Single-Run Checks (PRD §3, 2026-09-29)

Implements **Part 1 §3** — benchmark comparison, cost-shock stress and Monte
Carlo. These ride on the *same* result payload as the metric cards
(`benchmark`, `cost_shock`, `monte_carlo`), not behind separate fetches, so a
check can never qualify a different result than the one on screen.
`POST /api/backtest/monte-carlo` is the PRD's standalone endpoint and calls
the same function — a test asserts the two agree.

### Two PRD rules that cannot fire, and what replaced them

**1. "2x and 3x the configured slippage."** The canonical path is frictionless
by default (`free_executor` = zero slippage, zero fees), so 2 x 0 = 0. Taken
literally the table shows three identical green rows for every strategy — a
vacuously reassuring result, which is worse than none. `cost_shock.py` now
stresses from `DEFAULT_COST_SHOCK_BASE_BPS = 5.0` (the same NSE large-cap
default the `FixedBpsSlippage` docstring already states) when the run is
frictionless, and the payload carries `base_bps_source` so the panel says the
cards above were produced at 0 bps. When a slippage level *is* configured, the
1x column reuses the actual result instead of re-running it.

**2. "Shuffle the trades, then report median / 5th / 95th percentile final
equity, and flag anything above the 90th percentile."** Two independent
problems:

- A shuffle changes the path, never the sum. Final equity is **identical** in
  all 1,000 reorderings, and P(profit) is exactly 100% or 0%. Reporting those
  as a distribution puts three identical numbers in a table shaped like a
  spread.
- A same-size bootstrap resamples the very sample that defines its own
  distribution, so the actual result's percentile has a **ceiling of ~0.74 for
  any n** (`test_the_actual_percentile_has_a_mathematical_ceiling`). A 90th-
  percentile rule can never fire, no matter what the trades look like.

So `monte_carlo.py` runs two experiments and keeps them apart. `reorder` is
the PRD's shuffle and is reported **path-only** (its final equity is invariant
by construction, which the payload asserts in
`reorder.final_equity_is_invariant`). The final-equity spread comes from
`bootstrap` — resample *with* replacement, so the multiset changes too. And
the "was this one lucky trade?" question is asked directly via
`trade_concentration`: a run whose gross profit is 90% one trade has not been
shown to repeat, and no amount of resampling can show it, because the outlier
is *inside* the sample being resampled.

### Other decisions
- **Slippage is passed to the executor at construction**, never assigned
  afterwards. `OrderExecutor.__init__` hands the calculator to its
  `SimulatedFillProvider`, so `executor.slippage = ...` is a silent no-op and
  every fill still comes out frictionless — which looks exactly like "slippage
  does not affect this strategy". Both `free_executor` and `costed_executor`
  now take a `slippage=` argument and the docstrings say why.
- **Dropped trades are reported, not folded into the return.** Past a certain
  slippage an all-in order needs more buying power than the account has, so
  trades stop happening. Each scenario carries `trades_dropped` and the panel
  says the fall is a sizing limit, not only a thinner edge.
- **Alpha is simple excess return** (strategy minus benchmark, same period) as
  the PRD words it — NOT the regression intercept. Beta is the OLS slope, with
  a flat-benchmark guard: zero variance in the denominator is reported as 0.0,
  not as an enormous number that would read as enormous sensitivity.
- **A verdict drawn from a handful of trades is suppressed**
  (`status: insufficient_trades`), on the same reasoning as §2's flag.
- **Every failure path returns `available: False` with a reason.** A stress
  test that throws must not take the result page down with it. Quick-screen
  has no slippage argument, so it reports unavailable rather than being
  silently skipped.
- **Monte Carlo drawdowns are at trade boundaries**, not per bar — the
  resampled path has one point per trade. Stated in `drawdown_note` so the
  panel never presents 0.1% as the run's max drawdown.
- Seeded (`DEFAULT_SEED = 42`) so refreshing the page does not move the numbers.

### UI
`web/static/js/components/run_checks.js` renders three collapsed
`<details>` panels below the §2 sections. They qualify the headline numbers,
so they are opt-in — but an unavailable check always renders its own row with
the reason, because a silently absent panel reads as a panel that passed.

### Cost
Three extra engine runs, ~60 ms each on 800 bars. A full `/api/backtest/run`
with all three checks completes in ~0.5 s.

### Tests
`tests/test_run_checks.py` (80), `tests/test_api_run_checks.py` (22),
`tests/js/test_run_checks.mjs` (28, via `tests/test_web_components.py`), plus
updated shape assertions in `tests/test_api_backtest.py` and
`tests/test_backtest_adapter.py`.

---

## Compare Tab Enhancements (PRD §4, 2026-09-29)

Implements **Part 1 §4** of `docs/backTest-enhance.md`. Part 1 §1–§4 are now
complete; §5 and all of Part 2 remain open.

The Compare page previously let each slot pick its own symbol *and* its own
timeframe, hid the slots that failed, plotted raw equity levels, and answered
neither "are these four rows four bets?" nor "did the top row actually win?".

### What counts as a shared condition (§4.1)

Dates, capital, engine **and timeframe** are now read once and sent in the
`shared` block; `run-many` applies them to every slot. Only strategy and
parameters remain per-slot.

- The per-slot timeframe `<select>` is gone from the slot card and the
  timeframe moved into the shared config panel. The per-slot `symbol` send was
  removed too — in Compare Strategies mode the symbol is a shared condition
  too, and only Test Generalization may vary it.
- The shared timeframe is filled from the shared symbol's real coverage (§1.4),
  so it never offers a granularity with no bars behind it.
- **A failed slot keeps its column.** `renderCompareTable` takes *all* slots,
  not just the successful ones: the metric cells render `—`, a `Status` row
  carries the error, and the Backtest/Forward buttons are omitted because there
  is no result to open. A three-column table after one slot blew up reads as
  "that is what the comparison was".
- Slot errors are HTML-escaped before reaching the table. They are
  server-supplied strings that land in an attribute; an unescaped quote or tag
  there would break the table or inject markup into the results page.

### Test Generalization (§4.2)

A mode toggle above the shared config. `generalization` keeps **one** strategy
and **one** parameter set in a shared editor and turns each slot into a symbol
row, capped at four.

- The server distinguishes the two modes by the *data*, not by a client-sent
  flag: `provenance.comparison_mode` is `generalization` when the slots
  disagree on symbol, `strategies` otherwise. Note the key is
  `comparison_mode`, **not** `mode` — `mode` is the engine tier
  (fill-exact / quick-screen) read by the provenance badge.
- `provenance.symbol` lists every symbol when they differ, so a multi-symbol run
  is never badged with the one symbol that was not used.
- A slot with no `symbol` of its own falls back to the shared one, so
  Compare-Strategies requests are byte-identical to before.

### The correlation matrix is aligned, and flat curves are undefined (§4.3)

`engine/comparison.py::correlation_matrix` outer-joins every result's
per-bar returns and **drops any bar not shared by all** before correlating.
Pairwise intersections would let one cell be measured over 500 bars and the
next over 300, and the panel says how many bars it used.

- A zero standard deviation (a strategy that never moved) yields `None`, not
  `0.0`. A 0.0 would read as "perfectly diversifying" — the opposite of the
  truth — so those cells render `n/a`, carry a `undefined_correlation`
  warning naming the flat curves, and are excluded from the >0.8 flags.
- Pairs above `HIGH_CORRELATION = 0.8` are listed in `high_correlation_pairs`
  and each gets its own warning. The panel states the count in words, because
  a red square on its own does not tell a reader whether to do anything.
- Labels are built as `strategy · SYMBOL (params)` and de-duplicated by slot
  id, because two slots on the same strategy is how a parameter sweep works and
  a name-keyed matrix would silently collapse them into one row.

### The significance bootstrap is paired, not independent (§4.4)

`sharpe_significance` draws **one** set of day-indices per simulation and
applies it to every strategy.

This is the load-bearing decision in the slice. Two strategies in Compare
Strategies mode traded the same market days, so their daily returns are
correlated. Resampling each series *independently* would let the shared market
shocks separate between the two series, manufacturing disagreement the market
never produced — and the panel would report a confident winner between two
strategies that in truth took the same bets. Paired resampling keeps
collinear strategies correctly tied.

- Verdicts are two-sided over `SIGNIFICANCE_LEVELS` (≥95% A better, ≤5% B
  better, otherwise `no_significant_difference`).
- If **no** pair differs, the block carries a `no_significant_winner` warning:
  *"Promoting the top row out of these results is promoting the luckiest, not
  the best."*
- Seeded (`seed=42`) so a refresh does not reorder the panel.
- Sharpes are annualised by the **shared** timeframe, read from the slot
  payloads, falling back to daily. Never hard-coded to 252.
- The panel is informational and says so. It gates nothing.

### Equity curves are indexed to 100 (§4.5)

`rebase_to_100` (server) and `indexTo100` (chart) both divide a curve by its
base at the **first shared date**, not each slot's own first bar — a slot that
starts later therefore begins above or below 100 instead of being falsely shown
as a winner that started flat.

- Two identical starting-capital slots that made 40% and 4% produce two lines
  100 apart and 100.4 apart, so the chart's whole vertical range is spent
  re-stating the starting capital instead of showing which strategy won.
- A non-positive or non-finite base is returned **unchanged** rather than
  divided into, which would fill the curve with infinities and blank the chart.
  Both implementations carry the same guard.

### Tests
`tests/test_engine_comparison.py` (25) pins the maths, including the two
"would mislead a user" cases: two copies of one strategy are never significant,
and a flat curve is undefined rather than zero. `tests/test_api_backtest_comparison.py`
(18) pins the wiring — per-slot symbols, provenance, failed-slot exclusion and
the page markup. `tests/js/test_compare_panels.mjs` (24) pins what the panels
render, and `tests/js/test_compare_controller.mjs` (13) pins the request the
page actually sends; both are driven from `tests/test_web_components.py`.

---

## Certification Readiness (PRD §5, 2026-09-29)

Implements **Part 1 §5** of `docs/backTest-enhance.md`. §1–§5 are now
complete; §6 and all of Part 2 remain open.

`engine/readiness.py` grades eight checks and returns
`{verdict, counts, checks[], summary, red_flags[], unproven[],
tune_this_available}`. It is computed server-side so the Backtest page and the
Compare table cannot disagree about the same run.

### The fourth state is the whole point of this module

§5 draws a green/yellow/red table. Real runs need a fourth state, and this is
the decision worth knowing about: **a check that could not be evaluated is
`unknown`, never green.**

The tempting shortcut is to let a missing input fall through to green — a
Quick-Screen run has no cost shock, so "no cost shock" becomes "no cost
problem", and the reader is handed a tick nobody earned on the most
promotional panel on the page. Forcing it red is the opposite mistake: it
charges the same underlying fact twice, once as Engine and again as Cost-Shock,
and makes one limitation read as a strategy that failed four checks.

So `unknown` renders neutrally, carries the server's reason verbatim, is listed
in `unproven`, and is excluded from `all_green`. A run is "all green" only when
all eight were genuinely evaluated and genuinely passed.

The same reasoning drives two other guards:

- **A run with zero closed trades gets `unknown` for max drawdown, not green.**
  Its drawdown is 0.0%, which is inside the "under 15%" green band. A green
  tick there would be the strongest possible endorsement of a result containing
  no trades at all.
- **The drawdown bands read absolute depth.** §5 writes the thresholds as
  positive ("< 15%") while the metric is negative, so a naive comparison passes
  every drawdown on earth. `-30 < 15` is true.

### Thresholds come from §2, not from §5

`TRADE_COUNT_OK`/`TRADE_COUNT_WARN` (30/20) are imported from
`metrics_risk` rather than re-declared. §2 already bands the metric block on
the same split, and two different trade-count bands on one page would be a
contradiction the user has to notice.

### The summary line the PRD does not supply

§5 gives a line for "any red" and one for "all green", and nothing for the
middle. The middle gets its own line that concedes exactly what is missing
("Nothing failed, but 1 weak, 1 unproven…"). Saying "basic checks passed" in
that gap would be the one statement a reader of this panel is entitled to rely
on.

### Cost-shock wording

A green cost-shock tick on a frictionless run must not imply the strategy
survived doubled costs it never paid, so the value reads
`profitable at 10 bps (2x the 5 bps default — this run charged no slippage)`.
The check grades on the **2x** row as §5 specifies, even when 3x has already
failed; the 3x column is on the cost-shock panel above it.

### The existing "Proceed to Paper" button is untouched

§5's wireframe shows a disabled Proceed-to-Paper button. **Not implemented.**
The standing constraint on this work is no Forward/Paper behaviour changes, and
disabling that button is exactly such a change. §5 is advisory and says so in
the payload (`advisory`, `gates_nothing`) and in the panel footer. The §6
"Tune This" button is likewise not built; `tune_this_available` is exposed so
§6 can consume it without editing this module.

### Tests
`tests/test_readiness.py` (56) pins the bands and, more importantly, the four
cases that would mislead a reader: unknown is never green, an unknown never
makes a run pass, a Quick-Screen run is not double-penalised, and a zero-trade
run is not given a green drawdown. `tests/test_api_readiness.py` (10) pins the
wiring — the block in a real response, one per slot in Compare, and none at all
for a failed slot. `tests/js/test_certification.mjs` (14) pins the rendering,
including escaping, via `tests/test_web_components.py`.

---

## "Tune This" — Backtest → Optimize (PRD §6, 2026-09-29)

Implements **Part 1 §6**. Part 1 is now complete; all of Part 2 remains open.

The button carries a finished backtest into the Optimize setup form with every
common field already filled. It does **not** start a search — a run that begins
the moment you look at it is a run you never chose, so the user reviews the
form and presses Start.

`components/tune_this.js` builds the prefill; `optimize_setup.js` applies it
*after* `loadStrategy()`, because that is what fetches the parameter space and
renders the rows the prefill has to write into. `SessionState.optimizePrefill`
is read-and-clear, so a stale hand-off cannot re-fill a form the user has since
edited.

### The engine now travels — the §1.1 bug, one hop downstream

§6 asks for "engine from current result (whichever was used)". It was not
merely missing from the button: **`optimize_setup.js`'s `buildConfig()` never
emitted `engine` at all**, so the parser's default (`driver`) applied to every
run. Dropping it means a result screened on Quick-Screen gets tuned on the
canonical driver — two engines under one apparent lineage, which is exactly
what §1.1 was written to kill.

`buildConfig()` now emits `engine`, and the Optimize page shows it in a
"carried over from" notice so the value is visible rather than applied in
silence. The canonical engine's backtest spelling is `""`, which maps to
Optimize's `driver`.

### The result ID is a session handle, and is labelled one

Backtests are stateless — nothing about a completed run is persisted — so there
is no server-side id to quote for the §6 reverse flow. `mintResultId()` creates
one per rendered result (`bt_<base36 time><random>`) and the UI says **"Result
ID (this session)"**.

This is enough for what the audit chain actually answers — *which result was
this tuned from?* — and not enough for what a stored id implies. A convincing
identifier in an audit log that resolves to nothing is worse than one that
admits it was a handle.

It travels as `backtestConfig.sourceBacktestId`, is stored on the run, and is
validated at parse time against `_ID_RE = ^[A-Za-z0-9_.:-]{1,64}$`. Anything
else is dropped with a warning: the value is later rendered into the audit
trail, and a free-form string in an audit field is a stored-XSS surface.

### The chain

`service.apply()` assembles all three links once the runner id is known:

```
backtest (session handle)  →  optimize (run_id)  →  runner (instance_id)
```

It is stored in `action_details["chain"]` and shown in the apply modal *before*
applying, updating live as the target and runner selection change. A run not
started from a backtest still produces a chain, with the first link explicitly
absent — silently omitting the link would make an un-traced apply look
identical to a traced one.

### The button is never hidden

§5 lists the button under "if all green", which reads like a gate. **It is not
used as one.** The ordinary reason to open Optimize is that the backtest above
is mediocre — a weak result is precisely what you tune — so hiding the button
unless the result is already certifiable would block the workflow the feature
exists for.

Instead the button is always present. When readiness is not all-green the hint
says so plainly, and names what Optimize will *not* fix: *"Optimize looks for
better parameters for this strategy — it does not fix Data source, Trade
count, Cost shock (2x)."* `tune_this_available` is still exposed on the §5
payload so a future gate can consume it.

### Walk-forward defaults

Train on two thirds of the backtest window, test on the rest, **step by the
test period** (the standard convention — a longer step skips bars, a shorter
one re-tests the same ones). A window too short to clear the form's own
minimums (5-day train, 2-day test) gets walk-forward switched **off with a
reason** rather than a split the server would reject at submit time, after the
user has already filled the form in.

### Not in scope
`optimize.html` still has no visible engine *control*; the value travels with
the request and is displayed. Adding a selector would let the Optimize page
diverge from the backtest that started it, which is the mismatch §6 exists to
prevent.

### Tests
`tests/js/test_tune_this.mjs` (25) pins every field §6 lists, the walk-forward
arithmetic, and the three judgement calls above — via
`tests/test_web_components.py`. `tests/test_tune_this_flow.py` (16) pins the
config parser, including that a hostile handle is refused.
`tests/optimization/test_tune_this_chain.py` (5) pins the chain against the
real service and audit store.

---

## Synthetic data disabled app-wide (2026-09-29)

`config/data_sources.yaml` is new: the list of market-data sources this
deployment may run on, each with an `enabled` flag. **Synthetic is off.**
`db`, `mstock` and `csv` are on.

This closes the gap named in the PRD's own Pre-PRD Check 2 — the app could
optimize on a random walk, return 8/10, and read as certified.

### Disabling is not deleting

`SyntheticSource` is untouched, the `--source synthetic` flag still parses, and
89 test files still generate synthetic candles on purpose. What changed is the
**app's answer** to "may I run a backtest on this source?" — and it is now a
config line instead of a code change.

That separation is the whole design. Removing the generator would have meant
rewriting a third of the test suite and would have made turning it back on a
code review; a policy file makes it one boolean.

### Where the refusal lands

`api/data_guard.py` guards the four routes that consume candles —
`/api/backtest/run`, `/api/backtest/run-many`, `/api/optimize/runs`,
`/api/optimize/estimate` — returning **409** with the reason and the list of
sources that *are* enabled. `estimate` is guarded because letting it through
would let you plan a run that then refuses to start.

Deliberately **not** guarded: pages, run history, the strategy catalogue. You
must still be able to read what you already did. A control that takes the app
down with it is an outage, not a safeguard.

### Failing open in the browser, closed on the server

`data_source_gate.js` renders a one-line strip when allowed and a red panel
plus a disabled Run button when not. It is presentation only — and when it
cannot parse a status it **does not block**. The 409 is the real control;
blocking on a missing data attribute would put a dead button under a tooltip
reading `undefined` on any page that forgot to pass the status.

It also does not un-block a button that was already disabled: `optStart` starts
life disabled until a strategy is picked, and handing that back enabled would
be a bug the gate introduced.

### `certifiable` is a second, separate switch

`csv` is enabled but **not** certifiable. It is real in shape and unverified
in fact — bars of unknown provenance whose trustworthiness depends on wherever
the files came from. Enabling it is honest; calling it certification-grade
would put back the gap this closes, one column over. `db` and `mstock` are
both enabled and certifiable.

### Turning it back on

Either edit `enabled: true` for `synthetic` in `config/data_sources.yaml`, or
start with `BACKTEST_DATA_PROFILE=testing` (the profile that opts back in
without editing the file). The test suite uses the profile via
`tests/conftest.py` and `tox.ini`.

A missing or malformed config file falls back to the **conservative** answer —
synthetic off — on the grounds that a typo in a YAML path should not be the
thing that quietly restores generated data.

### Known consequence

`--source mock_broker` runs the data pipeline as synthetic, so it is now
blocked too. That follows from the policy rather than contradicting it: a
zero-credential dry run is still a run on generated candles. If a demo or
onboarding path needs it, the `testing` profile covers that.

---

## "Tune This" button: two states, not a gate (2026-09-29)

Resolves the §6 call flagged at the time of writing. §5's `tune_this_available`
is now **used**, as a display switch rather than a permission:

| | certifiable | not certifiable |
|---|---|---|
| button | `btn-primary` | `btn-secondary` + demoted panel + "not certifiable" badge |
| clickable | yes | **yes** |
| hint | "All eight readiness checks passed" | "not certifiable — Optimize does not fix …" |

A hard gate was rejected: the ordinary reason to open Optimize is that the
result above is weak, so a disabled button would block the workflow the feature
exists for. But identical styling in both states could not warn about
anything. Two states is what makes "not a gate" legible.

A **missing** readiness payload is treated as *not* certifiable, not as green.
An older cached payload would otherwise light up the primary button and claim
eight green checks nobody ran.

---

## Deflated Sharpe Ratio (PRD Part 2 §3, 2026-09-29)

`optimization/deflation.py`. Bailey & López de Prado (2014), JPM 40(5) — the
statistic that asks what the best of N trials was worth *before* you knew
which one would win.

### Two numbers, two scales — the mistake to avoid

The wireframe asks for "Deflated Sharpe: 0.89" next to "Sharpe: 1.42" and
compares it to 0.5. The actual statistic is a probability on 0–1. So the
function returns both, and the card keeps them apart in the wording:

* **`deflated_sharpe`** = SR₀, the Sharpe the best-of-N had to beat. Same scale
  as the reported Sharpe, so "1.42 against a bar of 0.89" is a real
  comparison. This is what the wireframe means.
* **`probability`** = the canonical DSR, 0–1. "Likely genuinely positive".

Reporting only the first would be a "corrected Sharpe" the paper does not
define. Reporting only the second would leave the wireframe comparing a
probability against a Sharpe.

### Two things that fail silently

**The annualisation trap.** Every Sharpe in the system is annualised; the
paper is about per-observation Sharpes, and both the trial variance and the
SR² term change under the conversion. The first implementation passed an
already-computed √periods_per_year into a function that took the *period
count*, so the scale was applied twice — every result came out 3.98× too
large, and still looked like a Sharpe. `_annual_scale()` is now the only
place the factor exists.

**Excess vs raw kurtosis.** γ₄ is 3 for a normal distribution, so
(γ₄−1)/4 = 1/2 and the term collapses to Lo's √(1 + SR²/2). Passing excess
kurtosis (0 for a normal) shrinks the denominator and inflates every
probability — a known bug elsewhere (vectorbt #10). Defaulted to raw.

### Trials counted honestly

N is the combinations *tried*, not the rows written. Combinations that failed
constraints or errored were still chances taken; dropping them would flatter
every run. A 2-combination search has no selection to correct for and says so
rather than reporting a confident-looking number.

### Storage

`optimization_runs.deflated_sharpe` holds SR₀ alone — the sortable,
comparable number. The full block (probability, trials, observations,
dispersion) lives in `analysis["deflated_sharpe"]`, and the block always has
the same keys, degraded or not, so callers render an absent statistic without
special-casing.

`NUMERIC(6, 3)`, not `NUMERIC(4, 2)` like `robustness_score`: the bar rises
with the size of the search, so a wide search over a narrow distribution
overflows 99.99 — and a clamped value still looks plausible.

Migration **014** in all three forms: Alembic, PostgreSQL SQL, and the SQLite
mirror. The two reporting views project their columns explicitly and
`CREATE OR REPLACE VIEW` cannot change that list, so they are replaced whole.

### Tests
35 on the statistic (quantiles against published values, E[max Z] exact at
N=2, annualisation invariance, raw-vs-excess kurtosis, every degraded path),
8 on the wiring through a real service run, 11 on the card.

---

## Monte Carlo on the winner (PRD Part 2 §4, 2026-09-29)

A "Run Monte Carlo on best result" button beside Apply to Paper. On the
**winner only** — not all 50,000 candidates. The winner is the one that would
be applied, and the question is whether *its* trade sequence is a lucky
ordering.

Walk-forward and this answer different questions, which is why both are worth
having: walk-forward asks whether the **parameters** generalise across time;
Monte Carlo asks whether the **order of the trades** that produced them was
luck.

### On demand, not precomputed

`service.monte_carlo_best()` re-runs the best parameters once over the run's
own candles and its own config, then calls the same
`monte_carlo_trade_order()` the Backtest page uses — so the two paths cannot
drift. One extra backtest per click buys not storing a trade list for every
candidate across every run, which would be tens of thousands of rows to answer
a question asked once.

`evaluate()` grew a `keep_pnls` flag. It is off for the whole search; only
this one call turns it on.

### The fan chart

The engine now returns quantile paths (5/25/50/75/95) as `fan`. Only the
percentiles ship, not the individual paths: a fan chart shows the envelope of
the distribution, and sending 1,000 simulations to draw five lines is 200× the
data for the same picture.

Drawn from the **bootstrap** band, not the reorder. Under a pure reorder every
path ends at the same equity, so its "fan" would be a flat wedge — and a flat
wedge drawn to look like certainty is worse than no chart.

Long runs are downsampled to ~400 points per band, always ending on the real
final equity rather than a step short of it.

### The gate is a flag, not a block

P(profit) < 60% adds a checkbox to the Apply modal that must be ticked. **Not**
a hard block, deliberately: Monte Carlo is opt-in, so a hard gate would make
Apply depend on a check that may never have been run. A browser-only
requirement would be a rule that exists only in one place, so the server
records both the acknowledgement *and* the number — the audit trail can then
show that the check was run and what it said, not merely that someone claimed
to have read it.

An unrun check is recorded as `acknowledged: false`, which is distinguishable
from never having looked.

### Not blocked by the data-source policy

The candle policy (§ above) guards the four routes that consume candles. This
is not one of them: the candles were already read to produce this run, and the
resampling is arithmetic on trades that exist. Refusing it would remove a
check on a result the user can already see.

### Tests
22 on the service, endpoint and gate; 7 new on the fan's shape (band ordering
at every point, opening balance, downsample bounds, ending on the real
equity). Suite 4206 passed.

## PRD Part 2 §6.1 — Regime breakdown, and §6.2 warning panel (shipped)

### §6.1 What it is
`src/backtest/optimization/regimes.py` splits the **winner's** equity curve
across the PRD's five fixed calendar bands — COVID crash (2020-01-01 →
2020-03-31), Recovery bull (2020-04-01 → 2021-12-31), Rate-hike correction
(2022-01-01 → 2022-06-30), Volatile recovery (2022-07-01 → 2023-12-31),
Low-volatility grind (2024-01-01 → present) — and reports return, Sharpe, max
drawdown and trade count for each.

Deliberately **not** `src/backtest/intelligence/regime.py`. That file is a live
VIX volatility detector reading current conditions; this one is a fixed
calendar breakdown of a completed backtest. Same word, different question.

### The decision that mattered: full resolution, at the cost of one backtest
`best_curve` is downsampled to ≤400 points for drawing. Computing a per-period
Sharpe from that curve would have been wrong in a way that is invisible in the
output: sampling every third bar moves the standard deviation enough to reorder
two candidates, and a 400-point curve with a 0.0004 daily drift returns a
Sharpe in the tens of thousands. A regime table built on it would be a table of
sampling artefacts, wearing plausible numbers.

So the winner is evaluated **once more** at full resolution
(`evaluator.evaluate(..., keep_regimes=True)`), which costs one backtest
against a search that already ran thousands. That is the cheap way to be
correct. The result lands in `analysis.regimes`.

### Other calls
- **Trade counts from closed trades, by exit date** — `_trades_by_date` in
  `evaluator.py`. Inferring a trade from a kink in the equity curve is a guess
  with a number attached. `trades=None` means "not counted" and is
  deliberately distinct from `0`.
- **Bars outside every band are kept.** A 2015–2019 run lands in an
  "Other / uncovered" row with `named_coverage_pct = 0.0`. Dropping those bars
  would make the table look complete while describing nothing.
- **Return base is the period's first bar**, not the run's opening balance —
  and the first bar counts as a drawdown peak, so a period that only falls has
  a drawdown.
- **Below `MIN_BARS` (20) the Sharpe is `None` and the UI prints `—`.** The
  number would be present and large, which reads as "this period was better"
  when the truth is "this period is too short to say".
- **A failure to split returns `None`; it never fails the run.** The breakdown
  is computed in the last step, after thousands of backtests. Losing a real
  result over a cosmetic table is a bad trade.

### §6.2 Warning panel
`renderWarningPanel` in `optimize_run.js`: `position: sticky`, top of the
results, **no close button**. The warnings already existed and were already
correct — the PRD's complaint was that they were easy to miss, and a dismissable
banner would be the layout's answer to that complaint rather than a fix for it.
Any `danger` escalates the panel to red and counts itself out in the title.

### Verification
26 new tests (17 engine, 9 wiring, 13 JS). Real run: winner `{fast: 5, slow: 40}`,
15 trades, per-period counts 1 + 3 + 11 = 15 — reconciled against the run's own
`closed_trades`. Full suite 4242 passed; the `test_catalogue_entries_carry_params_and_kind`
order-dependent failure and the `benchmarks/` collection errors are both
pre-existing and reproduce on a clean tree.

## PRD Part 2 §2 — Data Source Lock and Attestation (shipped)

### The problem, restated
*"Optimize pulls candles from whatever source the app started with, and
there's no visible confirmation of this anywhere."* The gap is not the data —
it is that nobody had to **say which data**. A run that optimized 240
combinations against RELIANCE daily bars and one that optimized them against a
random walk produce numbers that look identical on the page. The second is not
wrong; it just is not a claim about RELIANCE, and nothing said so.

### The record
`src/backtest/optimization/attestation.py`. Reuses
`backtest.data.provenance` rather than restating it — that module already owns
the source labels, and two authorities for one label is how a page ends up
saying "Synthetic" in one place and "demo data" in another.

Three moments, deliberately different:

| when | what it is | bar count |
|---|---|---|
| setup page | `attestation_preview` | `None` — not knowable yet |
| run record | `attestation_record` | measured on the candles |
| results / audit | read back | never recomputed |

**The preview does not guess.** A box that invents a bar count is a box that
lies; the run record fills it in, and the setup page says "shown after the run".

### The one hard gate, and the many warnings
Synthetic requires an explicit tick. Everything else — staleness, an
undateable feed, zero bars — warns and allows. A box that refuses work for
reasons the operator cannot act on teaches them to ignore it, and then it is
not doing its job either.

The gate is enforced in `OptimizationService.submit()`, not just in the browser:
a gate only the browser enforces is a suggestion. The refusal carries
`code="synthetic_data_not_acknowledged"` so the UI can point at the tick
without matching on wording.

### Migration 015 — additive, unbackfilled
Eight nullable columns, no defaults, **no backfill**. Every run that predates
015 has no attestation, and that is the honest state of it — a backfilled value
would be a claim nobody checked. `_run_provenance()` therefore has two paths:
the stored attestation when present, and a **derived** block marked
`derived: true` for older runs. A rebuilt record presented with the same
authority as a measured one is the exact failure §2 exists to prevent.

The stored attestation is the DATA half only. `_run_provenance()` **merges** it
with the engine half from `build_provenance` rather than replacing — a run that
remembers its data but has quietly lost its engine label is not more complete,
just differently incomplete.

### Two bugs this feature introduced, caught before shipping
1. **`rerun()` silently lost the acknowledgement.** It rebuilds the config
   document from `backtest_config`, which does not carry it — so "Rerun" on
   any synthetic run became impossible. The single most confusing way for a
   gate to behave, and invisible until someone pressed the button. Fixed in
   `config_doc_from_run()`; pinned by a test.
2. **The measured rewrite dropped the acknowledgement.** `_execute()` replaces
   the preview with the measured record, and the ack is the one field not
   derived from data — nothing in the candles can re-earn what a person
   ticked. Now carried across explicitly from the stored record.

### Test-harness note
`SMA_DOC` now carries the tick, because the fixture source IS synthetic. Tests
about the gate use `unacknowledged_sma_doc()` / the `unacknowledged` fixture, so
the absence is asked for by name and is never an accident.

### Verification
60 new tests (31 record + gate, 15 API/migration/audit, 17 JS, plus migration
coverage). Full suite **4292 passed**. The 3 remaining failures — two
`benchmarks/test_load_testing.py` timing assertions and the order-dependent
`test_catalogue_entries_carry_params_and_kind` — all reproduce on a clean tree.
