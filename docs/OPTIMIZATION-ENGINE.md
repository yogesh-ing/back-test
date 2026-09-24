# Parameter Optimization Engine

Search a strategy's parameter space, score each combination with the same
backtest engine the rest of the platform uses, check it against risk
constraints, validate the winner out of sample, and apply it to a paper (or,
deliberately, live) runner with a full audit trail and one-click rollback.

- **UI:** `🎯 Optimize` in the nav → `/optimize` (setup) → `/optimize/runs/<id>` (progress + results)
- **API:** `/api/optimize/*` (`src/backtest/api/optimize.py`)
- **Engine:** `src/backtest/optimization/`
- **Schema:** migrations 005–009 (PostgreSQL + SQLite mirror). See [`db/DB-IMPLEMENTATION-GUIDE.md`](../db/DB-IMPLEMENTATION-GUIDE.md) §11.

---

## 1. Quick start

```bash
# PostgreSQL (production). Apply the schema once:
export FORWARD_TEST_DB_URL=postgresql+psycopg2://user:pass@host:5432/forward_test
alembic upgrade head                      # 001 … 009
# or: psql -f db/migrations/005_optimization_core.sql … 009_optimization_seed_presets.sql

python -m backtest.web.app --port 5000    # then open /optimize
```

Without `FORWARD_TEST_DB_URL`, the app uses the dev SQLite profile from
`config/database.yaml`, and the store creates its four tables on first use.
With no database at all, or `OPTIMIZATION_DB=off`, the `/api/optimize/*` data
endpoints answer **503** with an explanatory message and the rest of the app
is unaffected.

| Env var | Default | Meaning |
|---|---|---|
| `FORWARD_TEST_DB_URL` | dev SQLite | Where runs/results/presets/audit live |
| `OPTIMIZER_WORKERS` | `min(8, CPUs)` | Backtest worker processes per run |
| `OPTIMIZATION_DB` | `auto` | `off` disables the feature (503) |

Typical throughput on the synthetic daily data (6 years ≈ 1 500 bars) with 2
workers: ~25 backtests/s. A 56-point grid, with sensitivity sweeps and a
3-split walk-forward, takes ~11 s. The setup page shows an estimate before you
start. It is calibrated from measured timings once a run has completed.

---

## 2. Pipeline

```
loading → baseline → search → sensitivity → walk_forward → analysis → saving
```

1. **loading**: candles are loaded once and shared with every worker process.
2. **baseline**: the strategy's current parameters are backtested, so every result is shown as a delta against what you run today.
3. **search**: grid, random, Bayesian (Gaussian process + expected improvement, pure numpy) or genetic. Results are cached by `(params, window)`, so no combination is paid for twice.
4. **sensitivity**: a 1-D sweep of each optimized parameter through the best point. If a sweep finds a better point, the engine re-centres on it and sweeps again, up to 3 rounds. The reported optimum is therefore a local optimum on every axis, not just the best point the sampler happened to hit.
5. **walk_forward** (optional): rolling train/test splits. The search is re-run on each *train* window, and the winner is scored on the *next, unseen* window. Indicators warm up on bars *before* the scored window. That is not lookahead: the first scored bar is forced flat.
6. **analysis**: heatmaps, plateau detection, top-result clustering, a 0–10 robustness score and plain-English warning signs.
7. **saving**: results are bulk-inserted, and the run row gets best/baseline/WF/analysis.

Runs execute on a background thread, and only one run executes at a time; later
runs stay `pending`. Each run fans out over a process pool. You can
**pause/resume/cancel** a run. A cancelled run keeps and saves everything
computed so far. Every 30 s the run row gets a heartbeat. At startup, runs whose
heartbeat is older than 15 min are marked `failed` ("interrupted"), because a
restart kills their thread.

### Engines

| `backtestConfig.engine` | Used for | Notes |
|---|---|---|
| `driver` (default) | equity strategies | the event-driven engine used by the regular Run Backtest API |
| `quick_screen` | wide first passes | vectorized, and much faster, but approximate |
| `options` | option strategies (auto-selected) | the option backtest engine. Adds tunable knobs `engine.delta_target`, `engine.spread_threshold` and `engine.max_open_structures` |

Parity is tested: an optimizer evaluation and `POST /api/backtest/run` produce
the same final equity for the same inputs (`tests/optimization/test_api.py`).

> **Options trade stats are net of commission.** The optimizer scores each
> closed structure as `realized_pnl − commission`. The Options Backtest
> page's per-structure `realized_pnl` is shown *before* commission, so the
> optimizer's win rate and expectancy for an option strategy can be slightly
> lower than what that page shows. Equity curves, and therefore Sharpe,
> return and drawdown, match exactly. For equity strategies, trade P&L is
> equity-based (costs included) on both paths.

---

## 3. Configuration

The API accepts the PRD's camelCase document, and snake_case works too.
Example:

```json
{
  "strategyId": "sma_crossover",
  "objectiveFunction": "sharpe",
  "method": "grid",
  "parameters": [
    {"name": "fast", "min": 5,  "max": 40,  "step": 5},
    {"name": "slow", "min": 50, "max": 200, "step": 25},
    {"name": "stop", "min": 1,  "max": 3,   "step": 1, "optimize": false, "current": 2}
  ],
  "constraints": [
    {"metric": "max_drawdown", "operator": "<",  "value": 30},
    {"metric": "min_trades",   "operator": ">=", "value": 5}
  ],
  "backtestConfig": {"symbol": "DEMO", "timeframe": "1day",
                     "startDate": "2019-01-01", "endDate": "2024-12-31",
                     "initialCapital": 100000},
  "methodSettings": {"nCalls": 60, "seed": 42},
  "walkForward": {"enabled": true, "trainPeriodDays": 730, "testPeriodDays": 365,
                  "stepDays": 365, "overfitThreshold": 0.3}
}
```

**Validation** reports *every* problem at once (`400 {"errors": {"field": "message"}}`):
- Parameter bounds come from the strategy's own `param_schema`, and `int` parameters need integer steps.
- The limits are **8** optimized parameters, **50 000** grid combinations and **5 000** evaluations for the sampling methods. A grid above the limit gets a suggestion to use random, Bayesian or genetic search.
- Periods shorter than 6 months, or runs without walk-forward, produce *warnings*, not errors.

**Objectives:** `sharpe`, `sortino`, `calmar`, `total_return`, `profit_factor`
(capped at 100), `expectancy`. A failed backtest scores −1 000 000 and never wins.

**Constraints** use **percent magnitudes** for drawdown and win rate:
- `max_drawdown < 15` means "drawdown better than 15 %".
- The PRD's signed-decimal form `> -0.15` is accepted and normalised to the same constraint.
- `min_trades` is an alias of `total_trades`.
- A failed backtest violates every constraint.

**Method defaults:**

| Method | Defaults |
|---|---|
| random | `nSamples` = 20 % of the grid (at least 10) |
| bayesian | `nCalls` 60, `nInitial` = `nCalls/4`, clamped to 5–12 |
| genetic | `population` 20, `generations` 10 |

All methods use `seed` 42, so the same config gives the same run.

---

## 4. Reading the results

| Signal | Meaning |
|---|---|
| **Heatmap** (any two params) | `max` / `mean` / `slice` through the best point. Broad warm regions are good; one hot cell is not |
| **Sensitivity** | per-param curve + *plateau*: the contiguous range scoring within 10 % of the best. `stable` / `moderate` / `sensitive` |
| **Walk-forward efficiency** | mean test score / mean train score. For ratio objectives, **overfitted** means average train→test degradation > threshold (0.30). For others, it means efficiency < 0.5 |
| **Robustness 0–10** | weighted: plateau width 4, WF efficiency 3, top-result clustering 2, sample size 1. Missing evidence is re-weighted, not zero-filled. ≥7 Robust, ≥4 Moderate, else Fragile |
| **Warning signs** | Sharpe > 3, < 30 trades, fewer than 10 trades per optimized param, best value on a range edge, sharp peaks, no walk-forward, a suspicious jump over baseline |

---

## 5. Applying parameters

`POST /api/optimize/runs/<id>/apply`
`{"target": "paper"|"live"|"ab_test"|"none", "params"?: {...}, "instance_id"?: "...", ...}`

| Target | Effect |
|---|---|
| `none` | record only: a preset snapshot plus an audit row |
| `paper` without `instance_id` | spawns a new paper runner `opt-<strategy>-<id6>` |
| `paper`/`live` with `instance_id` | flattens → removes → re-adds the runner with the new params (same capital/symbols/mode) |
| `ab_test` | spawns a paper runner `…-B` next to your existing one (the control) |

**Live is fail-closed.** It needs `confirm_live: true` and an existing live
runner (`instance_id`). It is refused if walk-forward flagged the run as
overfitted, and refused for runs without walk-forward unless
`allow_unvalidated: true`.

Every apply:
- writes an `optimization_audit` row with who, when, IP, user agent, old/new params, the diff and the expected impact;
- snapshots the params as a preset;
- logs `OPTIMIZE_APPLY` to the portfolio manager's audit log.

`POST /api/optimize/audit/<audit_id>/rollback` undoes it: it restores the old
params, or removes the spawned runner. You can roll back only once.

---

## 6. API reference

| Method & path | Purpose |
|---|---|
| `GET  /api/optimize/meta` | objectives, methods, constraint metrics, limits (no DB needed) |
| `GET  /api/optimize/strategies/<name>/space` | suggested parameter ranges (default ±50 %, snapped to a nice step) |
| `GET  /api/optimize/runners?strategy=` | runners the apply dialog can target, with their current params |
| `POST /api/optimize/estimate` | validate + evaluation count + time estimate |
| `POST /api/optimize/runs` | create (and start, unless `"start": false`) → `201 {run_id}` |
| `GET  /api/optimize/runs?strategy=&status=&limit=&offset=` | list |
| `GET  /api/optimize/runs/<id>` | status + live progress (best-so-far, ETA, recent results) |
| `DELETE /api/optimize/runs/<id>` | delete a finished run (results cascade) |
| `POST /api/optimize/runs/<id>/{start,pause,resume,cancel,rerun}` | lifecycle. `rerun` accepts `{"overrides": {...}}` |
| `POST /api/optimize/runs/<id>/apply` | see §5 |
| `POST /api/optimize/runs/<id>/presets` | save best (or given) params as a named preset |
| `GET  /api/optimize/runs/<id>/results?sort=&order=&limit=&offset=&compliant=1` | paged results. Served live from memory while running |
| `GET  /api/optimize/runs/<id>/heatmap?x=&y=&metric=&agg=&compliant=` | 2-D surface |
| `GET  /api/optimize/runs/<id>/sensitivity` | per-parameter curves + plateaus |
| `GET  /api/optimize/runs/<id>/walk-forward` | splits + summary |
| `GET  /api/optimize/runs/<id>/export.csv` | all results, one column per param + metric |
| `GET/POST /api/optimize/presets`, `PATCH/DELETE /api/optimize/presets/<id>` | presets |
| `GET  /api/optimize/audit?strategy=&run_id=` | audit trail |
| `POST /api/optimize/audit/<id>/rollback` | undo an apply |

Errors are `{"success": false, "error": "..."}`:

| Status | When |
|---|---|
| 400 | validation (with `errors`) or bad query args |
| 404 | unknown id |
| 409 | wrong state (e.g. applying a running run, rolling back twice) |
| 503 | no database |

The acting user comes from the `X-User` / `X-Forwarded-User` header.

---

## 7. Schema (migrations 005–009)

| Rev | Content |
|---|---|
| 005 | `optimization_runs` and `optimization_results` (FK **CASCADE**), plus an `updated_at` trigger via the shared `set_updated_at()` |
| 006 | `parameter_presets` and `optimization_audit` (FKs to runs are **SET NULL**, so history outlives a deleted run) |
| 007 | indexes, including partial ones (top-ranked results, active presets) |
| 008 | PostgreSQL views: `v_latest_optimization`, `v_top_results`, `v_optimization_summary`, `v_parameter_history`, `v_active_presets`. SQLite skips these |
| 009 | seeds three default presets (Conservative / Moderate / Aggressive, `strategy_id='default'`) with fixed UUIDs, idempotently |

Beyond the PRD draft, the runs table also stores `baseline_params`,
`baseline_metrics`, `baseline_score`, `analysis`, `robustness_score` and
`task_id`. Deviations from the PRD's migration code, all deliberate:

| PRD draft | Implementation | Reason |
|---|---|---|
| `down_revision=None` | chains onto `004` | would create a second Alembic root |
| `TIMESTAMPTZ` import | `sa.DateTime(timezone=True)` | the import doesn't exist |
| seed via `json.dumps` | JSONB receives dicts | `json.dumps` double-encodes the JSON |
| `onupdate=NOW()` | trigger | `onupdate=` has no database effect |

Rollback: `alembic downgrade 004`, or `psql -f db/migrations/005_009_optimization_rollback.sql`.

---

## 8. Tests

```bash
PYTHONPATH=src pytest tests/optimization tests/db/test_migrations_005_009.py -q
# + real PostgreSQL round trip (creates and drops its own scratch database):
OPTIMIZATION_TEST_PG_URL=postgresql+psycopg2://postgres@localhost/postgres \
  PYTHONPATH=src pytest tests/db/test_migrations_005_009.py -q
```

The suite has ~105 tests and runs in about 25 s, with coverage of 83–96 % per
module. It covers:
- config validation, constraint normalisation, the four search methods and the analysis maths;
- real backtests (inline vs. process pool parity, the options engine);
- the service lifecycle (run, cancel, failed load, rerun), apply/rollback against a fake runner manager, and every endpoint;
- the pages and nav link;
- the migrations (SQLite file vs. ORM, CHECK/FK behaviour, Alembic chain, PG files);
- the JS helper harness (`tests/js/test_optimize_common.mjs`), which checks that the setup page's combination count matches the backend's for ~200 generated ranges.

## 9. Not implemented (PRD items out of scope)

- **Knex migrations**: this is a Python/Alembic codebase.
- **docker-compose**: out of scope.
- **PDF report export**: CSV export is available.
- **Scheduled monthly re-optimization**: *Re-run* reproduces any run with one click, and `POST …/rerun` can be driven by cron.
- **Distributed workers across machines**: one process pool per app instance.
