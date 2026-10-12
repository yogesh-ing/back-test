# Backtest Run Persistence — Product Requirements Document

- **Status:** BUILT / MERGED — shipped to main 2026-10-03 (commit `c0ce76b`). Implemented as specified: migration 018 (`db/alembic/versions/20261003_1200_018_backtest_run_ledger.py`), store `src/backtest/api/backtest_run_store.py` (`save_run` / `save_compare` / `save_optimizer_baseline` / `list_runs` / `get_run` / `stats`, R7 retention `SERIES_CAP_PER_GROUP = 500`), endpoints `GET /api/backtest/runs[/stats|/<id>]` + `persisted` flag in `src/backtest/api/backtest.py`. PRD text below is the approved (v3) spec.
- **Date:** 2026-10-03 (v1 2026-10-03; v2 2026-10-03; v3 2026-10-03)
- **Severity:** Medium (no data-loss risk; audit-trail gap — completed backtests leave no server-side trace)
- **Related:** `docs/MTM-STALENESS-OBSERVABILITY-PRD.md` (same "attest at the moment of truth" philosophy), `docs/OPTIMIZATION-ENGINE.md`, PRD §1.1/§1.2 (provenance), PRD §5 (readiness), `db/alembic/versions` (this work is **migration 018**, single revision, see §9 — numbered 018 because the alembic head was 017 `market_holidays` after the strategy-builder merge)

## Revision log

- **v1→v2:** All five blocking items from initial architect review accepted: 1a compare-table definition (R1b), 1b `payload_version` (R1 column), 1c composite indexes (R1), 2a canonical hash (R1 note), 2b `persisted` flag + UI badge (R3), 2c code fingerprint (R1 provenance), 3a flat metric columns (R1), 3b optimizer baseline in ledger (R6), 3c split transactions + `series_stored` (R2/R3), 3d retention decided at N=500 (R7), observability adapted to existing stack (R8), cost table (R7), minors (accepted).
- **v2→v3 (Architect Polish):** 2a `series_stored` bool → `series_status` enum for disambiguation (R1, R2, R3, R4), 2b explicit boundary for `config_hash` economic determinants (R1), 2c two-step retention sweep transaction (R7), 2d `comparison_version` mismatch UX mapped to 422 (R3).
- **v3 (Wireframes):** Added §11 detailing UI states for History List, Run Detail, and Compare History based on `series_status` and `persisted` flags.

## 1. Problem statement

A completed backtest is a fact about a config + a dataset + an engine, produced at a moment in time. Today that fact is thrown away: `POST /api/backtest/run` and `POST /api/backtest/run-many` compute the full payload (metrics, equity, drawdown, trades, signals, `cost_shock`, `provenance`, `readiness`) and `jsonify` it. The only survival mechanism is the browser's localStorage cache (`backtest.js` L10-34, `backtest_recent_runs`, max 12 entries) — per-profile, silently droppable, and gone on a machine switch or a storage flush.

Why this bites now, concretely: `market_data_cache` was purged on 2026-10-03 (all 1-day bars + BSE 1-min deleted; `bak_*` tables kept). Any stored-with-provenance result could still be *interpreted* later ("this ran on DB-sourced candles fetched on date X"); an unstored one cannot be reproduced, defended, or compared. The review → deployment path for the swing work (e.g. the `donchian_1d` battery) currently depends on markdown files and browser memory because there is no run ledger.

Meanwhile the optimizer already does this right: `optimization_runs` / `optimization_results` / `parameter_presets` / `optimization_audit` persist every combo, the walk-forward verdict, the deflated Sharpe, and a data attestation written at run time — and result pages *read back stored rows rather than re-running* (`PROJECT-CONTEXT.md`). The single-run and compare flows are the missing ledger at the same standard.

## 2. Current state (verified in code, 2026-10-03)

| Flow | Persists? | Where |
|---|---|---|
| `/api/backtest/run` | **No** | `api/backtest.py::run_backtest_endpoint` (L312-427) returns JSON only |
| `/api/backtest/run-many` | **No** | `run_many` (L596-743): process-pool slots, `_comparison_block` computed live; `compare.js` re-runs everything on every visit |
| `/api/optimize/*` | **Yes** | `optimization/store.py` → 4 tables (migrations 013/015); presets carry `backtest_metrics` + `optimization_run_id` lineage |
| `equity_curve` / `performance_metrics` / `trades` tables | — | **Not usable here**: keyed on `portfolio_id`, they are the forward/paper portfolio ledger, not a backtest store |

Frontend留存 today: `localStorage["backtest_recent_runs"]`, deduped by `[strategy, symbol, timeframe, from_date, to_date, params]`, full trades/equity kept, oldest dropped beyond 12. **Fate: removed as a source of truth** (R4); two histories that can disagree is worse than one.

## 3. Requirements

Decision this PRD takes for granted: **persist at the single backtest run**, because compare and optimize are analyses *over runs*. Storing only post-optimization would bake survivorship bias into the history. The optimizer's existing storage is left untouched apart from R6.

### R1 — `backtest_runs` table (migration 018)

One immutable row per *completed, successful* run. Append-only: the same config re-run on re-fetched data is a new fact, not an update.

| Column | Type | Notes |
|---|---|---|
| `run_id` | UUIDStr PK | same convention as `OptimizationRun` |
| `config_hash` | String(64), not null | deterministic hash — construction rule below |
| `kind` | String(20), not null | `single` \| `compare_slot` \| `optimizer_baseline` |
| `parent_compare_id` | UUIDStr FK → `backtest_compare_runs`, nullable | set for compare slots only |
| `optimization_run_id` | UUIDStr FK → `optimization_runs`, `ON DELETE SET NULL`, nullable | set for `optimizer_baseline` rows only (R6); deliberately a *separate* FK, not an overload of `parent_compare_id` |
| strategy_id, symbol, timeframe, date_from, date_to, capital, engine | flat columns | list/filter without JSON unpacking |
| `payload_version` | Integer, not null, default 1 | **schema of the stored payload**, bumped in `BacktestAdapter` (single canonical source); read endpoint refuses mismatched majors (§5.3) |
| `params` / `readiness` / `cost_shock` | JSONVariant | `readiness` and `cost_shock` persisted *as computed at run time* — the traffic light must describe the run, never be recomputed against today's config |
| `config` | JSONVariant, not null | the adapter's `config` dict **verbatim** (`params` is a sub-projection); required for byte-identical read-back — the endpoint enriches config with stop-loss/take-profit/bars etc. beyond the hashed determinants |
| **Flat metric columns**: `sharpe`, `sortino`, `calmar`, `cagr`, `total_return`, `max_drawdown` (NUMERIC(10,4), nullable); `win_rate` NUMERIC(5,2); `profit_factor` Score; `total_trades` Integer | | Written from the same metrics dict through the *same* sanitizer as `RESULT_METRIC_COLUMNS`; ranges (`sharpe > 1.5`) become index-backed queries. `Float` is not used; money/Decimal-exact accounting is house style. |
| `metrics` | JSONVariant | full metrics blob kept for completeness/re-render; flat columns are a *projection*, never a replacement |
| `series_status` | String(12), not null, default `'write_failed'` | **Lifecycle of the R2 heavy payload**: `present` \| `evicted` \| `write_failed`. Set to `present` upon R2 commit. R7 retention sets to `evicted`. A non-`present` row still lists and shows flat metrics, but detail endpoint returns degraded payload. |
| `provenance` JSONVariant + flat `data_source`, `bars_count`, `fetched_first_ts`, `fetched_last_ts`, `data_fetch_date`, `code_fingerprint` | | written together by one helper so flat columns and JSON cannot drift. `code_fingerprint`: JSON with `app_git_sha` (git short hash captured at app start) and `strategy_sha256` (hash of the strategy module source at import). Non-null asserted at write. |
| `created_at`, `created_by` | | `created_by` nullable today; comment in migration: becomes NOT NULL when web auth ships |

**`config_hash` construction.** Exact, canonical, no float-format ambiguity. All economic determinants (commissions, slippage, data adjustment method) **must** be nested inside `params` prior to hashing; if they are top-level config fields passed to the adapter but omitted from the hash, hash collisions on economically distinct runs will occur.

```python
canonical = json.dumps(
    {
        "strategy": strategy_id, "symbol": symbol, "timeframe": timeframe,
        "from_date": from_date, "to_date": to_date, "params": params,
        "engine": engine, "capital": capital,
    },
    sort_keys=True, allow_nan=False, separators=(",", ":"),
)
config_hash = hashlib.sha256(canonical.encode()).hexdigest()
```

Params must be JSON-clean: `allow_nan=False` makes a NaN in *params* a hard error at hash time (the NaN→NULL sanitizer applies to *metrics* only). Test: the same params dict built two ways (insertion order swapped) hashes identically.

**Indexes.**

```sql
CREATE INDEX ix_backtest_runs_list   ON backtest_runs (strategy_id, symbol, timeframe, created_at DESC);
CREATE INDEX ix_backtest_runs_config ON backtest_runs (config_hash, created_at DESC);
CREATE INDEX ix_backtest_runs_kind   ON backtest_runs (kind, created_at DESC);
CREATE INDEX ix_backtest_runs_opt    ON backtest_runs (optimization_run_id) WHERE optimization_run_id IS NOT NULL;
```

### R1b — `backtest_compare_runs` table (migration 018)

| Column | Type | Notes |
|---|---|---|
| `compare_id` | UUIDStr PK | |
| `comparison_mode` | String(20), not null | **`strategies` \| `generalization`** — the literal values `run_many` already stamps (L732-734) |
| `comparison_version` | Integer, not null, default 1 | version of the `_comparison_block` schema — logic changes invalidate stored blocks |
| `config_snapshot` | JSONVariant, not null | the full `run-many` request body |
| `provenance` JSONVariant + flat `data_source`, `date_from`, `date_to`, `symbols_used`, `engines_used` | | one shared attestation for the comparison |
| `comparison_block` | JSONVariant | correlation matrix, Sharpe significance, per-pair rows — stored verbatim |
| `slot_count`, `slot_errors` | Integer, JSONVariant | `slot_errors` = `{slot_id: {"error": msg}}` for failed slots |
| `created_at`, `created_by` | | as R1 |

Index: `(created_at DESC)`.

### R2 — heavy series in a second table

`backtest_run_series(run_id PK/FK CASCADE, trades JSON, equity JSON, drawdown JSON, signals JSON, extras JSON, bytes_written Integer, stored_at)`. `extras` carries the remaining payload top-levels (`benchmark`, `monte_carlo`) so read-back is byte-identical to the live response — a detail page re-renders both without re-running. `bytes_written` feeds R8 sizing and R7 retention. Series is written *after* and *separately from* R1 (R3 transaction split). Upon successful commit of the R2 row, the parent R1 row's `series_status` is updated to `present`.

### R3 — write path fails *soft but loud*; read path never re-runs

- `run_backtest_endpoint`: after payload assembly, call `backtest_run_store.save_run(...)`. Persistence failure logs ERROR with the `request_id` and the run still returns 200.
- **The ledger can't lie.** Every `/run` and `/run-many` response carries `persisted: true|false` (+ `persist_error` string when false). The UI renders a persistent ⚠ `not stored` badge on the result and the run entry is *not* added to server history.
- New endpoints:
  - `GET /api/backtest/runs?strategy=&symbol=&timeframe=&min_sharpe=&kind=&limit=&offset=` — R1 rows newest-first, flat metrics only, metric-range filters served by the flat columns.
  - `GET /api/backtest/runs/<id>` — full payload reconstructed from R1+R2, shape identical to what `/run` returned. Refuses on `payload_version` major mismatch with 422 + the stored version.
- `run_many`: transaction 1 writes the R1b parent + all successful slot children + `slot_errors`; transaction 2+ writes each slot's R2 series independently, updating R1 `series_status` to `present` upon success. Read-back of a compare run refuses on `comparison_version` major mismatch with 422 (symmetry with `payload_version`).
- Lineage join: `parameter_presets` gains nullable `backtest_run_id` UUIDStr FK (`SET NULL`), written when a preset is saved from a plain backtest. **In migration 018**.

### R4 — UI: history, not redesign

- Backtest page: "Recent runs" served from `GET /api/backtest/runs`; localStorage cache **removed**. Opening a stored run re-renders from the stored payload, labelled "stored run <date>, source <data_source>". Runs with `persisted=false` badge loudly.
- Detail page handles `series_status`: `present` renders full chart; `evicted` renders muted *"Chart data expired"* badge with flat metrics; `write_failed` renders hard *"Storage error, re-run recommended"* badge.
- Compare page: stored comparisons listed by `compare_id`; read-back, no process pool.
- Out of scope: dashboards, leaderboard, promotion state machines.

### R5 — lineage joins the deployment story

Review chain: **preset → (`backtest_run_id` | `optimization_run_id`) → stored metrics + readiness + cost_shock + provenance + code fingerprint**.

### R6 — optimizer baseline writes one ledger row

Each optimize job's baseline evaluation writes one `backtest_runs` row, `kind='optimizer_baseline'`, `optimization_run_id` set. Grid candidates stay in `optimization_results` only.

### R7 — retention (decided, ships with Slice 1)

- All R1/R1b rows kept forever (~2 KB each; 50 runs/day ≈ 37 MB/year).
- `backtest_run_series` capped at **N=500 rows per `(strategy_id, symbol, timeframe)` group**, oldest evicted first.
- Enforcement: post-write sweep on the housekeeping worker. Idempotent two-step transaction so a missed sweep only delays, never corrupts, and the UI never reads `present` for a missing R2 row:

```sql
BEGIN;
  UPDATE backtest_runs 
    SET series_status = 'evicted' 
    WHERE run_id IN (SELECT run_id FROM ... beyond N);
  DELETE FROM backtest_run_series 
    WHERE run_id IN (SELECT run_id FROM backtest_runs WHERE series_status = 'evicted');
COMMIT;
```

| Component | Size/row | Runs/day | 1-year |
|---|---|---|---|
| R1 row | ~2 KB | 50 | ~37 MB |
| R2 series (avg) | ~1.5 MB | 50 | ~27 GB uncapped |
| R2 with N=500/group cap | bounded | — | ~1-2 GB |

### R8 — ledger health observability

No metrics stack exists in this repo; implemented with existing machinery:

| Signal | Implementation |
|---|---|
| persist success/failure | named INFO/ERROR log events `[run-ledger] persist_ok run=… hash=…` / `[run-ledger] persist_failed run=… err=…` |
| failure-rate alert | `persist_failed` routes through existing alerts notifier (rate-limited: first per 10 min) |
| series write size | `bytes_written` column (R2) — queried for retention sizing |
| ledger health | `GET /api/backtest/runs/stats` → `{total_runs, last_24h, series_rows, series_bytes_total, series_evicted, last_persist_error}` |

## 4. Explicitly NOT doing (non-goals)

- No changes to the optimizer tables/flow beyond the R6 baseline row and the nullable preset FK.
- No forward-test ledger overlap.
- No data-cache retention changes.
- No metrics stack (Prometheus etc.).
- No backfill.

## 5. Gotchas / risk register

1. **Unbounded growth.** Bounded by R7 from day one.
2. **Same run written twice.** Accepted — append-only; UI groups by `config_hash`.
3. **Payload shape drift.** Two version axes: `payload_version` on R1 and `comparison_version` on R1b. Major mismatch → 422 refusal.
4. **`quick_screen` vs `driver` rows are not comparable.** Flat `engine` + provenance badge make this visible.
5. **Data determinism caveat.** Provenance is stored and read-back is labelled as a stored run.
6. **Code determinism.** `code_fingerprint` distinguishes same strategy name across commits. Read-back does *not* refuse on fingerprint mismatch — only `payload_version` gates reads; a fingerprint difference is context, not corruption.

## 6. Open questions — resolution status

All questions are closed:

| Q | Answer |
|---|---|
| Q1 | Main SQLAlchemy engine (`db/manager.py`), same as optimizer. |
| Q2 | All ledger rows forever; series capped N=500 per (strategy, symbol, timeframe). |
| Q3 | JSON blobs; normalize only when a query needs it. |
| Q4 | localStorage removed as source of truth. |
| Q5 | Baseline row yes (R6), grid candidates no. |

## 7. Implementation slices

1. **Slice 1 — ledger:** migration 018 = R1 + R1b + R2 + `parameter_presets.backtest_run_id`, `backtest/api/backtest_run_store.py` (reusing the optimizer sanitizer), fail-soft-but-loud write incl. `persisted` flag, `GET /runs` + `GET /runs/<id>` + `GET /runs/stats`, R7 retention sweep (two-step transaction), R8 log/alert wiring.
   Tests: round-trip payload equality; canonical-hash stability (two insertion orders, `allow_nan=False` rejection); sanitizer NaN/inf→NULL; write-failure → 200 + `persisted=false` + alert line; `payload_version` 422; fingerprint non-null assertion; `series_status` transitions (`write_failed` → `present` on R2 commit; `present` → `evicted` on R7 sweep); retention eviction deletes R2 but keeps R1.
2. **Slice 2 — compare:** `run_many` parent/child transactions, `comparison_version`, stored comparison read-back (422 on major mismatch), compare page history list, R6 optimizer baseline row.
3. **Slice 3 — UI + lineage:** server-backed history lists, `persisted=false` badge, `series_status` UI handling (chart render vs. expired/error badges), preset → run link, localStorage removal.

## 8. Validation

- Determinism: run config C twice against the same range; assert two rows, identical `config_hash`, equal metrics.
- Post-purge honesty: read back a run whose `data_source` no longer has coverage; stored provenance + label render, no re-fetch attempted.
- Outage drill: stop Postgres, run `/api/backtest/run` → expect 200, payload intact, `persisted=false`, ⚠ badge, alert line; restart, run again → `persisted=true`.
- Series lifecycle: run a heavy backtest → R1 `series_status='present'`, R2 exists → trigger retention sweep → R1 `series_status='evicted'`, R2 gone → `GET /runs/<id>` returns flat metrics + degraded chart state.
- Load: `run-many` 4 heavy slots; list endpoint payload excludes series; WAL/txn sizes logged; `bytes_written` distribution recorded.
- Suite: touches `api/backtest.py` — slice-scoped backtest-API tests first.

## 9. Migration scope

**018** creates `backtest_runs`, `backtest_compare_runs`, `backtest_run_series`, adds `parameter_presets.backtest_run_id` (nullable, FK SET NULL), and creates the indexes in R1/R1b. One revision. Reversible downgrade drops the three tables and the one column.

## 10. Points not adopted verbatim (author's counter-arguments)

1. **§4 observability → log events + alerts, not counters.** No metrics stack exists; R8 implements the same four signals via named log lines and the existing `backtest/alerts/` notifier.
2. **§1a `comparison_mode` enum.** Adopted `strategies | generalization` — the literal values `run_many` stamps today — not `multi_strategy/multi_symbol/multi_period`.
3. **§3b baseline lineage.** Implemented as a dedicated nullable `optimization_run_id` FK on `backtest_runs`, not `parent_id` pointing at two different tables (preserves FK integrity).
4. **§3a metric column types.** Uses this repo's `Score`/`NUMERIC(10,4)` rather than `Float`, matching `RESULT_METRIC_COLUMNS` exactly so optimizer/ledger numbers stay comparable without float drift.

---

## 11. UI Wireframes

### 11.1 Backtest History Panel (Replaces `localStorage` Dropdown)

Served by `GET /api/backtest/runs`. Displays R1 flat metrics. Grouped by `config_hash` (latest on top, older accessible via expand).

```text
┌─ Recent Runs ──────────────────────────────────────────────────────────────┐
│ Filter: [Strategy ▾] [Symbol ▾] [TF ▾]            [Min Sharpe: ____]     │
├───────────────────────────────────────────────────────────────────────────┤
│                                                                           │
│  ▼ donchian_1d | SBIN | 1d | 2024-01-01 → 2024-10-03                    │
│    ┌─ Run ID: a1b2... ────── 2026-10-03 14:32 ─── ⚠ NOT STORED ──────┐  │
│    │ Sharpe: 1.82 | CAGR: 14.2% | Max DD: -18.4% | Trades: 142      │  │
│    └──────────────────────────────────────────────────────────────────┘  │
│                                                                           │
│    ┌─ Run ID: c3d4... ────── 2026-10-02 09:15 ─── 🟢 READY ──────────┐  │
│    │ Sharpe: 1.85 | CAGR: 14.5% | Max DD: -17.9% | Trades: 140      │  │
│    │ [📉 Chart Data Expired]                                          │  │
│    └──────────────────────────────────────────────────────────────────┘  │
│                                                                           │
│  ▼ mean_revert | RELIANCE | 1d | 2024-01-01 → 2024-10-03                │
│    ┌─ Run ID: e5f6... ────── 2026-10-03 15:05 ─── 🟡 CAUTION ────────┐  │
│    │ Sharpe: 0.94 | CAGR: 6.1%  | Max DD: -12.3% | Trades: 89       │  │
│    └──────────────────────────────────────────────────────────────────┘  │
│                                                                           │
└───────────────────────────────────────────────────────────────────────────┘
```

**UI Rules:**
1. If `persisted=false` (API response flag): Show `⚠ NOT STORED`. Click re-runs instead of fetching.
2. If `series_status == 'evicted'`: Show `[📉 Chart Data Expired]`. Click loads flat metrics only.
3. If `series_status == 'write_failed'`: Show `[⛔ Storage Error - Re-run]`. Click re-runs.
4. If `readiness == 'RED'`: Show `🔴 FAIL` (provenance/readiness traffic light).
5. Clicking a valid row hits `GET /api/backtest/runs/<id>` and renders in the main view.

### 11.2 Backtest Result Detail View (Live vs. Stored)

The main right-side canvas. Must clearly differentiate a live computation from a historical read-back so users never confuse stale data with current reality.

```text
┌─ Backtest Result ────────────────────────────────────────────────────────┐
│                                                                          │
│  [ LIVE RUN ]           2026-10-03 15:45         Engine: driver         │
│  ──────────────────────────────────────────────────────────────────────  │
│  Strategy: donchian_1d  |  Symbol: SBIN  |  TF: 1d                     │
│  Period: 2024-01-01 → 2024-10-03  |  Capital: ₹1,000,000               │
│                                                                          │
│  ┌─ Provenance ──────────────────────────────────────────────────────┐  │
│  │ Source: db_candles  |  Fetched: 2026-10-03  |  Bars: 190          │  │
│  │ Git SHA: a4b7c1d   |  Strat Hash: 8f9e...                      │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│  ┌─ Metrics ─────────────────────────────────────────────────────────┐  │
│  │ Sharpe: 1.85 | Sortino: 2.41 | CAGR: 14.5% | Max DD: -17.9%    │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│  ┌─ Equity Curve ────────────────────────────────────────────────────┐  │
│  │                          /|                                       │  │
│  │                        /  |                                       │  │
│  │                  ____/    |                                       │  │
│  │                /          |                                       │  │
│  │              /            |                                       │  │
│  │            /              |                                       │  │
│  │          /                |                                       │  │
│  │─────────┘                 |                                       │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

**State Variant: Stored Run**
Replace `[ LIVE RUN ]` with:
`[ STORED RUN — 2026-10-02 09:15 | Source: db_candles ]`
Background tint slightly grey/blue to visually distinguish from live white/green.

**State Variant: Degraded Run (`series_status = 'evicted'`)**
Replace `Equity Curve` block with:
```text
│  ┌─ Equity Curve ────────────────────────────────────────────────────┐  │
│  │                                                                    │  │
│  │            [ 📉 Chart Data Expired ]                               │  │
│  │            Series evicted by retention policy.                     │  │
│  │            Flat metrics above are from the stored ledger.          │  │
│  │                                                                    │  │
│  └───────────────────────────────────────────────────────────────────┘  │
```

**State Variant: Storage Error (`series_status = 'write_failed'`)**
Replace `Equity Curve` block with:
```text
│  ┌─ Equity Curve ────────────────────────────────────────────────────┐  │
│  │                                                                    │  │
│  │            [ ⛔ Storage Error ]                                    │  │
│  │            Heavy series failed to persist. Re-run recommended.     │  │
│  │                                                                    │  │
│  └───────────────────────────────────────────────────────────────────┘  │
```

### 11.3 Compare History List

Served from the `backtest_compare_runs` table. Clicking avoids the process pool and reads stored `comparison_block`.

```text
┌─ Comparison History ────────────────────────────────────────────────────┐
│                                                                          │
│  ┌─ Compare ID: f7g8... ───── 2026-10-03 16:20 ─── Mode: strategies ─┐ │
│  │ Slots: 3 (Success) | Symbols: SBIN, RELIANCE, TCS                  │ │
│  │ Sharpe Range: 0.94 → 1.85 | Correlation Matrix: Stored            │ │
│  │ [ Load Comparison ]                                                │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  ┌─ Compare ID: h9i0... ───── 2026-10-02 11:05 ─── Mode: generaliz. ┐ │
│  │ Slots: 4 (3 Success, 1 Error) | Symbol: ITC | TFs: 1d, 5d, 15d..│ │
│  │ [ ⚠ Partial Result ]  [ Load Comparison ]                         │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

**UI Rules:**
1. If `slot_errors` is not empty: Show `[ ⚠ Partial Result ]` and list failed slots in a tooltip.
2. If `comparison_version` mismatch (422): Disable `[ Load Comparison ]` and show `[ ⛠ Outdated Calc Logic - Re-run ]`.