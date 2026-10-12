# Strategy Performance Analytics & Parameter Optimization

Comprehensive performance evaluation, risk analysis, and systematic parameter optimization for trading strategies. This document covers the analytics engine that powers live/paper trading dashboards and the optimization framework that finds robust strategy parameters.

---

## Table of Contents

1. [Overview](#overview)
2. [Strategy Performance Analytics](#strategy-performance-analytics)
3. [Parameter Optimization Engine](#parameter-optimization-engine)
4. [Integration & Workflow](#integration--workflow)
5. [API Reference](#api-reference)
6. [Database Schema](#database-schema)
7. [Best Practices](#best-practices)

---

## Overview

The platform provides two complementary analysis systems:

### **Performance Analytics** (Live/Paper Trading)
Real-time quantitative evaluation of running strategies:
- Risk-adjusted returns (Sharpe, Sortino, Calmar)
- Drawdown analysis and recovery tracking
- Trade distribution and streak analysis
- Monthly/weekly performance breakdowns
- Edge degradation detection
- Portfolio-level aggregation

### **Parameter Optimization** (Backtesting)
Systematic search for robust strategy parameters:
- Multi-method search (Grid, Random, Bayesian, Genetic)
- Walk-forward validation (out-of-sample testing)
- Sensitivity analysis and robustness scoring
- Constraint-based filtering
- One-click apply to live runners with audit trail

**Key Principle:** Analytics tell you *how well* a strategy is performing; optimization tells you *which parameters* make it perform best — but only if validated properly.

---

## Strategy Performance Analytics

Analytics are computed on-demand from live/paper portfolio managers and historical trade data. The system handles both equity and option strategies with appropriate adjustments for each.

### Core Metrics

#### Risk-Adjusted Returns

| Metric | Formula | Interpretation |
|--------|---------|----------------|
| **Sharpe Ratio** | `(Return - Rf) / σ` | Reward per unit of total volatility. >1.0 good, >2.0 excellent. |
| **Sortino Ratio** | `(Return - Rf) / σ_downside` | Like Sharpe, but penalizes only downside volatility. Better for asymmetric strategies. |
| **Calmar Ratio** | `CAGR / Max Drawdown` | Return relative to worst drawdown. >1.0 acceptable. |
| **Profit Factor** | `Gross Profit / Gross Loss` | Total wins vs losses. >1.5 solid, >2.0 strong. |

**Configuration:** Risk-free rate defaults to 6% annually, configurable via `ANALYTICS_RISK_FREE_RATE` env var or `config/performance.yaml`.

#### Drawdown Analysis

- **Max Drawdown:** Largest peak-to-trough decline (absolute ₹ and %)
- **Drawdown Duration:** Days from peak to trough + recovery
- **Current Drawdown:** Active underwater period
- **Underwater Curve:** Time series showing all drawdown periods

**Alert Threshold:** The overview alerts flag a strategy with drawdown
(`max_drawdown_pct`) above 15% as critical, and one with Sharpe < 1.0 (across
at least 5 trades) as a warning; a losing streak of 4+ trades raises an info
alert. These appear in the Risk Board.

#### Trade Statistics

| Statistic | Description |
|-----------|-------------|
| Win Rate | % of trades with positive P&L (breakeven = neutral, not counted) |
| Avg Win / Avg Loss | Mean profit of winners vs mean loss of losers |
| Largest Win / Loss | Single biggest winner and loser |
| Expectancy | Average profit per trade: `(Win% × Avg Win) - (Loss% × Avg Loss)` |
| Streaks | Max consecutive wins/losses (breakeven breaks neither streak) |
| Holding Time | Average time in trade (minutes/hours/days) |

#### Equity Curve Analysis

The system computes:
- **Total Return:** Final equity / initial capital - 1
- **CAGR:** Compound annual growth rate
- **Volatility:** Annualized standard deviation of returns
- **Downside Deviation:** Volatility of negative returns only
- **Beta:** Correlation with benchmark (if available)

### Period Filtering

Analytics support multiple time windows:
- `7d`, `30d`, `90d`, `1y`, `all_time`

**Important:** All timestamps use IST market clock (UTC+5:30). Naive timestamps are assumed IST; aware timestamps are trusted as-is. This prevents UTC↔IST skew from shifting "today" boundaries.

### Trade History Provenance

For long-running portfolios (>200 trades), the system merges:
1. **In-memory trades** from the portfolio manager (recent tail)
2. **Persisted trades** from the database (`trades` table)

Memory rows win on natural-key collisions (same symbol, entry time, exit time). The response includes a `history` block indicating data sources.

### Edge Degradation Detection

The analytics engine flags potential edge decay when (rolling metrics,
compared window-over-window):

- Rolling Sharpe drops ≥ 20% from its earlier window, or falls below 1.0
- Rolling Sharpe is **negative** — the absolute floor: a strategy that was
  always bad has nothing to "degrade" from, so this fires on its own

These warnings appear in the **Risk Board** under "Strategy Health."
(Overview alerts additionally fire on low Sharpe < 1.0 with ≥ 5 trades,
drawdown > 15%, and losing streaks of 4+.)

### Option Strategy Adjustments

Option strategies have special handling:
- **P&L is net of commission** (realized_pnl - commission)
- **Structures close atomically** (all legs together)
- **Greeks snapshots** track delta/gamma/vega/theta exposure
- **IV regime fit** shows whether strategy matches current volatility

### API Endpoints

Analytics are served by two endpoints (equity curve, drawdown, trade
distribution and monthly breakdown are computed inside the service and
returned in these responses, not as separate routes):

```http
GET /api/analytics/overview?period=30d&mode=paper
GET /api/analytics/strategy/<instance_id>?period=90d
GET /api/analytics/cross-broker/summary|execution|compare|migration-impact|recommend
```

The strategy detail endpoint is keyed by **runner instance id**, not strategy
name; its period default is `90d` (the overview's is `30d`).

**Response Example:**
```json
{
  "success": true,
  "strategy_name": "ema_crossover",
  "period": "30d",
  "metrics": {
    "sharpe_ratio": 1.85,
    "sortino_ratio": 2.12,
    "calmar_ratio": 1.43,
    "profit_factor": 1.92,
    "win_rate": 0.58,
    "expectancy": 245.50,
    "max_drawdown": -0.082,
    "total_return": 0.067,
    "cagr": 0.24,
    "total_trades": 42,
    "avg_holding_time_minutes": 185
  },
  "health_warnings": [],
  "history": {
    "source": "merged",
    "db_trades": 158,
    "memory_trades": 42
  }
}
```

---

## Parameter Optimization Engine

The optimization engine systematically searches a strategy's parameter space to find combinations that maximize an objective function while satisfying risk constraints. It uses the **same backtest engine** as the rest of the platform, ensuring consistency.

### Architecture

```
Config Validation → Baseline Backtest → Search Method → Sensitivity Analysis
       ↓                ↓                    ↓               ↓
   Constraints      Current Params     Grid/Random/    1-D Sweeps +
   Check            Scored             Bayesian/        Re-centering
                                        Genetic
                                              ↓
                                      Walk-Forward Validation
                                              ↓
                                       Robustness Scoring
                                              ↓
                                    Results + Audit Trail
```

### Configuration

An optimization run requires:

```json
{
  "strategyId": "ema_crossover",
  "objectiveFunction": "sharpe",
  "method": "bayesian",
  "parameters": [
    {"name": "fast_period", "min": 5, "max": 50, "step": 5},
    {"name": "slow_period", "min": 50, "max": 200, "step": 25},
    {"name": "stop_loss_pct", "min": 1, "max": 5, "step": 0.5, "optimize": false, "current": 2}
  ],
  "constraints": {
    "min_sharpe": 1.0,
    "max_drawdown_pct": 15,
    "min_trades": 30,
    "min_profit_factor": 1.3
  },
  "backtest": {
    "symbol": "NIFTY",
    "start_date": "2020-01-01",
    "end_date": "2024-12-31",
    "timeframe": "1day",
    "initial_capital": 100000
  },
  "walkForward": {
    "enabled": true,
    "trainPeriodDays": 60,
    "testPeriodDays": 30,
    "stepDays": 30,
    "maxEvalsPerSplit": 150,
    "overfitThreshold": 0.3
  }
}
```

**Limits** (enforced in `backtest/optimization/config.py`):
- Max optimized parameters: **8** (`MAX_OPTIMIZED_PARAMS`) — beyond this, curse of dimensionality makes results unreliable
- Max grid combinations: **50,000** (`MAX_GRID_COMBINATIONS`)
- Random search default: **20% of the grid size** (`default_random_samples`)
- Bayesian search default: **60 evaluations** (`n_calls`)

### Search Methods

| Method | Best For | Pros | Cons |
|--------|----------|------|------|
| **Grid** | Exhaustive search, few params (≤3) | Guarantees finding global optimum in space | Exponential blowup: 10×10×10 = 1000 combos |
| **Random** | Quick exploration, many params | Uniform coverage, parallelizable | May miss peaks |
| **Bayesian** | Expensive backtests, smooth landscapes | Learns structure, converges faster | Assumes smoothness, can get stuck |
| **Genetic** | Complex interactions, rugged landscapes | Escapes local optima, good for non-linear | More evaluations needed, stochastic |

#### Bayesian Optimization Details

Uses Gaussian Process regression with Expected Improvement acquisition:
- Builds surrogate model of objective function
- Balances exploration (uncertain regions) vs exploitation (known good regions)
- Typically finds near-optimal in 50-100 evaluations vs 1000+ for grid

#### Genetic Algorithm Details

- Population size: 20 (`population`)
- Generations: 10 (`generations`) — budget is population × generations, capped by the space size
- Selection: tournament
- Crossover: uniform
- Mutation: `mutation_rate` 0.2 per param, ±1 step
- Elitism: best solutions survive unchanged

### Walk-Forward Validation

Walk-forward (WF) tests whether the optimizer found a **robust** strategy or just overfit the training data:

1. Split data into **calendar-based rolling windows** — by default 60-day
   train, 30-day test, stepping 30 days (`trainPeriodDays`/`testPeriodDays`/
   `stepDays`; any number of splits that fit inside the date range)
2. For each split:
   - Run optimization on **train** window
   - Test winner on **next, unseen test** window
   - Indicators warm up on bars *before* scored window (no lookahead)
3. Compare average train score vs average test score

**Overfitting Criteria** (computed from the scored splits in
`backtest/optimization/walk_forward.py`):
- Average train→test score **degradation** > `overfit_threshold`
  (default **0.3**) → **overfitted**
- **Efficiency** (`avg_test_score / avg_train_score`) < **0.5** → **overfitted**

The UI shows the efficiency ratio (`avg_test / avg_train`). Values near 1.0
indicate robustness; <0.5 signals overfitting.

### Sensitivity Analysis

The engine sweeps each optimized parameter one-dimensionally around the
chosen value (`sensitivity_curve` in `backtest/optimization/analysis.py`),
evenly thinning the swept grid to at most 25 points:

- **Plateau Detection:** a value is "on the plateau" when its score is within
  10% (`PLATEAU_TOLERANCE`) of the best — broad flat regions are preferred
  over peaks. Stability = plateau width / swept range.
- **Sharp Peak Warning:** a curve is labelled *sensitive* when stability
  < 0.12 or the best neighbour's score drops > 40% (`neighbor_drop > 0.4`) —
  fragile, likely overfit.

The result is a per-parameter **local** classification (plateau / sensitive),
not just the best point the sampler happened to hit.

### Robustness Scoring

The engine computes a 0-10 robustness score from the evidence available
(`robustness_score` in `backtest/optimization/analysis.py`); missing evidence
is dropped and the remaining weights are re-normalized:

| Factor | Weight | Calculation |
|--------|--------|-------------|
| **Parameter plateaus** | 4 | mean stability from the 1-D sensitivity sweeps |
| **Walk-forward efficiency** | 3 | `avg_test/avg_train` capped at 0.8 |
| **Top-result clustering** | 2 | `1 − dispersion` (spread of results within 5% of best) |
| **Trade-count sufficiency** | 1 | `min(1, total_trades / 30)` |

**Interpretation:**
- **8-10:** Very robust, safe to deploy
- **6-8:** Reasonably robust, monitor closely
- **4-6:** Fragile, consider wider parameter ranges
- **<4:** Likely overfit, do not deploy

### Results & Analysis

#### Heatmaps

2-D surface plots of any metric over two parameters:
- `agg=max`: Best value over other params (optimizer's view)
- `agg=mean`: Average over other params (robustness view)
- `agg=slice`: Other params pinned to anchor (default: best row)

Missing cells are `null` (random/Bayesian runs are sparse).

#### Top Result Clustering

The top-10 constraint-compliant results (`top_cluster`) are analysed for how
tightly they cluster in parameter space — dispersion per parameter is the
std of the top-10 values divided by the swept range. This helps identify:
- **Multiple optima:** Different param sets achieving similar performance
- **Stable regions:** A tight cluster means the good region is a *region*,
  not a lucky isolated point (preferred over sharp peaks)

#### Warning Signs

Plain-English warnings generated by the analysis (`warning_signs` in
`backtest/optimization/analysis.py`) carry a `level` (info / warning /
danger) and a code. Examples of what the engine actually emits:

- `high_sharpe` — "Sharpe 3.4 > 3 — unusually high; verify it is not fitted noise."
- `few_trades` — "Only 8 trades — results are statistically weak (aim for 30+)."
- `trades_per_param` — fewer than 10 trades per optimized parameter
- `edge_<param>` — "Best slow=200 is at the maximum of the searched range — consider extending the range."
- `sharp_peak_<name>` — a parameter's 1-D curve is a peak, not a plateau
- `wf_overfit` (danger) — walk-forward flagged significant out-of-sample degradation
- `no_wf` — "Walk-forward validation was off — out-of-sample behaviour is unknown."

### Apply to Runner

Once a result is selected, users can apply it to a running strategy:

1. **Paper Mode (Default):** Applies immediately, no confirmation needed
2. **Live Mode:** Requires explicit `confirm_live: true` flag AND:
   - Walk-forward validation enabled (`walk_forward_enabled`, unless
     `allow_unvalidated=true`)
   - Run not flagged as overfitted

The system records:
- **Audit log entry** in `optimization_audit` table
- **Parameter preset** saved for rollback
- **Portfolio manager restart** (if runner is active)
- **One-click rollback** to previous parameters

**Fail-Closed Policy:** Live application is refused unless all safety checks pass. This prevents deploying overfitted strategies to real money.

### API Endpoints

```http
# Get default parameter space for a strategy
GET /api/optimize/strategies/<name>/space

# Validate config + estimate runtime
POST /api/optimize/estimate

# Create and start optimization run
POST /api/optimize/runs

# List runs (with filters)
GET /api/optimize/runs?strategy=ema_crossover&status=completed

# Get run status + live progress
GET /api/optimize/runs/<id>

# Cancel/pause/resume a run
POST /api/optimize/runs/<id>/cancel
POST /api/optimize/runs/<id>/pause
POST /api/optimize/runs/<id>/resume

# Get paginated results (sortable)
GET /api/optimize/runs/<id>/results?page=1&sort=score&order=desc

# Get heatmap data
GET /api/optimize/runs/<id>/heatmap?x=fast_period&y=slow_period&metric=sharpe&agg=max

# Get sensitivity curves
GET /api/optimize/runs/<id>/sensitivity

# Get walk-forward report
GET /api/optimize/runs/<id>/walk-forward

# Export all results as CSV
GET /api/optimize/runs/<id>/export.csv

# Apply best/selected params to runner
POST /api/optimize/runs/<id>/apply
{
  "result_id": "...",
  "target": "paper",
  "confirm_live": false
}

# Save as preset
POST /api/optimize/runs/<id>/presets
{
  "name": "Conservative EMA Cross",
  "result_id": "..."
}

# List presets
GET /api/optimize/presets?strategy=ema_crossover

# Rollback to previous preset
POST /api/optimize/audit/<id>/rollback
```

---

## Integration & Workflow

### Typical Workflow

1. **Baseline Assessment**
   - Run strategy with current parameters
   - Review analytics dashboard (Sharpe, drawdown, win rate)
   - Identify weaknesses (e.g., high drawdown, low win rate)

2. **Optimization Setup**
   - Select parameters to optimize (2-4 recommended)
   - Set reasonable ranges (avoid extremes)
   - Choose objective function matching weakness:
     - High drawdown → `calmar` or add `max_drawdown_pct` constraint
     - Low win rate → `profit_factor` or `expectancy`
     - General improvement → `sharpe` (default)

3. **Run Optimization**
   - Start with `quick_screen` engine for wide search
   - Use Bayesian method (efficient for expensive backtests)
   - Enable walk-forward (calendar splits; defaults 60d train / 30d test,
     30d step)
   - Monitor progress (live ETA, best-so-far)

4. **Analyze Results**
   - Check robustness score (<6 → be cautious)
   - Review heatmaps (look for plateaus, not peaks)
   - Read warning signs (address any red flags)
   - Verify walk-forward: degradation under the overfit threshold and
     efficiency ≥ 0.5

5. **Validate Out-of-Sample**
   - If WF failed, try different time periods or narrower ranges
   - Consider reducing number of optimized parameters
   - Look for parameter clusters (multiple good solutions)

6. **Apply & Monitor**
   - Apply to paper runner first
   - Monitor for 30 days minimum
   - Compare live performance to backtest expectations
   - If live degrades >20% vs backtest, revert and investigate

7. **Go Live (Optional)**
   - After 30-day paper success, apply to live runner
   - Confirm live application (explicit consent required)
   - Set alerts for drawdown breaches
   - Review weekly: is live matching backtest?

### When NOT to Optimize

- **<100 trades in backtest:** Insufficient sample, results will be noisy
- **Already robust:** If current Sharpe >1.5 and drawdown <15%, optimization may not add value
- **Market changed:** If underlying dynamics shifted (regime change), old backtest data is irrelevant
- **Curve fitting temptation:** Don't keep optimizing until you get the number you want

### Performance Tips

- **Use `quick_screen` engine** for initial wide searches (10-50× faster)
- **Cache is automatic:** Same params never backtested twice in one run
- **Parallel workers:** Default `min(8, CPU_count)`, tune via `OPTIMIZER_WORKERS` env
- **Typical throughput:** ~25 backtests/sec on synthetic daily data (6 years ≈ 1500 bars)

---

## API Reference

### Analytics Endpoints

#### GET `/api/analytics/strategy/<instance_id>`

Get performance metrics for a specific strategy **runner**, keyed by its
instance id (the id shown on the portfolio page), not by strategy name.

**Query Parameters:**
- `period` (optional): `7d`, `30d`, `90d`, `1y`, `all_time` (default: `90d`)

**Response:**
```json
{
  "success": true,
  "instance_id": "<instance_id>",
  "period": "90d",
  "metrics": {...},
  "equity_curve": [...],
  "drawdown_series": [...],
  "health_warnings": [...]
}
```

#### GET `/api/analytics/overview`

Aggregate metrics across all active strategy runners.

**Query Parameters:**
- `period` (optional): Same as above (default: `30d`)
- `mode` (optional): `paper`, `live`, `ab_test` — filter by runner mode

**Response:**
```json
{
  "success": true,
  "portfolio_metrics": {
    "total_equity": 1250000,
    "total_return": 0.18,
    "sharpe_ratio": 1.65,
    "max_drawdown": -0.095,
    "active_strategies": 5,
    "total_trades": 234
  },
  "strategies": [...],
  "alerts": [...]
}
```

#### Cross-broker endpoints

`GET /api/analytics/cross-broker/summary|execution|compare|migration-impact|recommend`
— broker execution-quality analytics (also `period`/`mode` filtered).

### Optimization Endpoints

#### POST `/api/optimize/runs`

Create and optionally start an optimization run.

**Request Body:**
```json
{
  "strategyId": "ema_crossover",
  "objectiveFunction": "sharpe",
  "method": "bayesian",
  "parameters": [...],
  "constraints": {...},
  "backtest": {...},
  "walk_forward": {...},
  "auto_start": true
}
```

**Response:**
```json
{
  "success": true,
  "run_id": "uuid-here",
  "status": "running",
  "estimated_combinations": 150,
  "estimated_duration_seconds": 45
}
```

#### GET `/api/optimize/runs/<id>/results`

Get paginated, sortable results.

**Query Parameters:**
- `page` (optional): Page number (default: 1)
- `per_page` (optional): Results per page (default: 50, max: 500)
- `sort` (optional): Column to sort by (default: `score`)
- `order` (optional): `asc` or `desc` (default: `desc`)
- `compliant_only` (optional): Only return constraint-compliant results (default: `false`)

**Sortable Columns:**
`score`, `sharpe`, `sortino`, `calmar`, `total_return`, `max_drawdown`, `profit_factor`, `win_rate`, `total_trades`, `expectancy`

**Response:**
```json
{
  "success": true,
  "total_results": 150,
  "page": 1,
  "per_page": 50,
  "results": [
    {
      "result_id": "uuid",
      "rank": 1,
      "params": {"fast_period": 15, "slow_period": 75},
      "metrics": {...},
      "score": 2.15,
      "constraints_met": true,
      "constraint_violations": []
    },
    ...
  ]
}
```

#### POST `/api/optimize/runs/<id>/apply`

Apply selected parameters to a running strategy.

**Request Body:**
```json
{
  "result_id": "uuid-of-selected-result",
  "target": "paper",
  "confirm_live": false,
  "user": "trader_name"
}
```

**Validation:**
- `target` must be `paper`, `live`, `ab_test`, or `none`
- `confirm_live: true` required for `target: "live"`
- Run must not be flagged as overfitted
- Walk-forward must be enabled (unless overridden with `allow_unvalidated`)

**Response:**
```json
{
  "success": true,
  "applied_to": "paper",
  "preset_id": "uuid-of-saved-preset",
  "audit_id": "uuid-of-audit-entry",
  "runner_restarted": true
}
```

---

## Database Schema

### Optimization Tables

#### `optimization_runs`

Main table for optimization job metadata.

| Column | Type | Description |
|--------|------|-------------|
| `run_id` | UUID | Primary key |
| `strategy_id` | VARCHAR(100) | Strategy name |
| `objective_function` | VARCHAR(50) | `sharpe`, `sortino`, `calmar`, etc. |
| `method` | VARCHAR(50) | `grid`, `random`, `bayesian`, `genetic` |
| `param_space` | JSONB | Parameter ranges and steps |
| `constraints` | JSONB | Risk constraints |
| `backtest_config` | JSONB | Symbol, dates, timeframe, capital |
| `status` | VARCHAR(20) | `pending`, `running`, `completed`, `failed`, `cancelled` |
| `total_combinations` | INT | Planned evaluations |
| `tested_combinations` | INT | Completed evaluations |
| `valid_combinations` | INT | Passed constraints |
| `best_params` | JSONB | Winning parameter set |
| `best_score` | NUMERIC(10,4) | Objective score of winner |
| `best_metrics` | JSONB | Full metrics of winner |
| `baseline_params` | JSONB | Original strategy params |
| `baseline_score` | NUMERIC(10,4) | Baseline objective score |
| `walk_forward_enabled` | BOOLEAN | Whether WF was run |
| `walk_forward_results` | JSONB | WF split results |
| `overfitted` | BOOLEAN | WF verdict |
| `robustness_score` | NUMERIC(4,2) | 0-10 robustness rating |
| `analysis` | JSONB | Heatmaps, sensitivity, warnings |
| `created_at` | TIMESTAMP | Job creation time |
| `completed_at` | TIMESTAMP | Job completion time |

#### `optimization_results`

Individual parameter combinations and their metrics.

| Column | Type | Description |
|--------|------|-------------|
| `result_id` | UUID | Primary key |
| `run_id` | UUID | FK to optimization_runs |
| `params` | JSONB | Parameter values tested |
| `sharpe` | NUMERIC(10,4) | Sharpe ratio |
| `sortino` | NUMERIC(10,4) | Sortino ratio |
| `calmar` | NUMERIC(10,4) | Calmar ratio |
| `total_return` | NUMERIC(10,4) | Total return (decimal) |
| `max_drawdown` | NUMERIC(10,4) | Max drawdown (decimal, negative) |
| `profit_factor` | NUMERIC(10,4) | Profit factor |
| `win_rate` | NUMERIC(5,2) | Win rate (percentage) |
| `total_trades` | INT | Number of trades |
| `expectancy` | NUMERIC(10,2) | Expected profit per trade |
| `constraints_met` | BOOLEAN | Passed all constraints |
| `constraint_violations` | JSONB | Which constraints failed |
| `objective_score` | NUMERIC(10,4) | Computed objective score |
| `rank` | INT | Rank within run (1 = best) |

**Indexes:**
- GIN index on `params` for containment queries
- Composite index on `(run_id, rank)` for top-N queries
- Index on `(run_id, objective_score)` for sorting

#### `parameter_presets`

Saved parameter sets for quick switching.

| Column | Type | Description |
|--------|------|-------------|
| `preset_id` | UUID | Primary key |
| `strategy_id` | VARCHAR(100) | Strategy name |
| `name` | VARCHAR(100) | Preset name |
| `description` | TEXT | Optional description |
| `params` | JSONB | Parameter values |
| `source` | VARCHAR(50) | `optimization`, `manual`, `default`, `snapshot` |
| `optimization_run_id` | UUID | FK if from optimization |
| `backtest_metrics` | JSONB | Metrics when tested |
| `is_active` | BOOLEAN | Currently in use |
| `applied_count` | INT | Times applied |
| `last_applied_at` | TIMESTAMP | Last application time |

#### `optimization_audit`

Append-only audit trail for parameter changes.

| Column | Type | Description |
|--------|------|-------------|
| `audit_id` | UUID | Primary key |
| `run_id` | UUID | FK to optimization_runs (nullable) |
| `strategy_id` | VARCHAR(100) | Strategy name |
| `action` | VARCHAR(50) | `applied`, `rolled_back`, `preset_created` |
| `old_params` | JSONB | Previous parameters |
| `new_params` | JSONB | New parameters |
| `params_diff` | JSONB | Structured diff |
| `applied_to_bucket` | VARCHAR(100) | Which runner segment |
| `applied_to_mode` | VARCHAR(20) | `paper` or `live` |
| `runner_restarted` | BOOLEAN | Whether runner restarted |
| `expected_impact` | JSONB | Predicted metrics change |
| `actual_impact` | JSONB | Observed metrics change (filled later) |
| `requires_approval` | BOOLEAN | Flagged for review |
| `approved_by` | VARCHAR(100) | Approver name |
| `user_id` | VARCHAR(100) | User who made change |
| `timestamp` | TIMESTAMP | Change time |

### Analytics Tables

Analytics primarily read from existing tables:
- `trades` for trade statistics
- `equity_curve` for equity/drawdown analysis
- `performance_metrics` for precomputed daily rollups
- `portfolio_greeks_history` for option strategy Greeks

No dedicated analytics tables — everything is computed on-demand from transactional data.

---

## Best Practices

### Performance Analytics

1. **Use Appropriate Periods**
   - Short-term (7d-30d): Detect recent changes, not for judging quality
   - Medium-term (90d-1y): Balance recency and statistical significance
   - Long-term (all_time): Overall quality, but may hide recent degradation

2. **Watch for Red Flags**
   - Sharpe < 1.0: Strategy may not have edge
   - Win rate < 40%: Needs high reward:risk to compensate
   - Max drawdown > 20%: Psychologically difficult to hold
   - Fewer than 30 trades: Sample too small for confidence

3. **Compare Against Benchmark**
   - Always compare to buy-and-hold for same instrument
   - If strategy can't beat passive holding, question its value
   - Look at risk-adjusted comparison (Sharpe), not just returns

4. **Monitor Regularly**
   - Weekly: Check for health warnings
   - Monthly: Review full analytics, compare to backtest expectations
   - Quarterly: Re-evaluate strategy thesis, consider optimization

### Parameter Optimization

1. **Start Narrow**
   - Optimize 2-3 parameters max initially
   - Use tight ranges around current values
   - Expand only if no good results found

2. **Choose Objective Wisely**
   - `sharpe`: General purpose, balances return and risk
   - `calmar`: When drawdown is primary concern
   - `profit_factor`: When win rate matters more than magnitude
   - `expectancy`: When trade frequency varies widely

3. **Set Realistic Constraints**
   - `min_trades: 30`: Statistical significance
   - `max_drawdown_pct: 15`: Psychological comfort
   - `min_profit_factor: 1.3`: Positive expectancy
   - Tighter constraints → fewer valid results, but higher quality

4. **Always Use Walk-Forward**
   - Never trust in-sample results alone
   - Calendar splits with 60-day train / 30-day test (30-day step) are the
     default
   - Average train→test degradation under `overfitThreshold` (default 0.3)
     is the bar; efficiency below 0.5 is flagged
   - If WF fails repeatedly, strategy may be unoptimizable

5. **Prefer Plateaus Over Peaks**
   - A broad region of good performance is more robust than a sharp peak
   - Check heatmaps: look for large contiguous good areas
   - If best point is isolated, consider neighboring points with slightly lower score but more stability

6. **Robustness Score Matters**
   - Score ≥ 8: Deploy with confidence
   - Score 6-8: Deploy but monitor closely
   - Score 4-6: Questionable, consider re-optimizing with different settings
   - Score < 4: Do not deploy, likely overfit

7. **Paper Trade Before Live**
   - Minimum 30 days paper trading
   - Compare live results to backtest expectations
   - If live degrades > 20% vs backtest, investigate before going live

8. **Document Everything**
   - Save presets with descriptive names ("Conservative EMA Cross v2")
   - Note why you chose specific parameters
   - Track actual performance vs expected in audit log

### Common Pitfalls

❌ **Over-optimization:** Optimizing too many parameters on too little data → curve fitting
✅ **Solution:** Limit to 2-4 params, ensure >100 trades, use walk-forward

❌ **Chasing perfection:** Trying to maximize returns without regard to risk
✅ **Solution:** Use risk-adjusted objectives (Sharpe, Calmar), set drawdown constraints

❌ **Ignoring regime changes:** Optimizing on old data that no longer reflects current market
✅ **Solution:** Use recent data (last 2-3 years), check if VIX regime matches strategy design

❌ **Deploying without paper testing:** Going live immediately after optimization
✅ **Solution:** Always paper trade for 30+ days, verify live matches backtest

❌ **Forgetting transaction costs:** Optimizer finds high-frequency strategy that dies from commissions
✅ **Solution:** Include realistic commission model in backtest config, set `min_trades` constraint

---

## Troubleshooting

### Optimization Runs Slow

**Symptoms:** ETA keeps increasing, run takes hours

**Causes:**
- Too many combinations (grid with fine steps)
- Complex strategy with expensive indicators
- Too many workers causing contention

**Solutions:**
- Switch to `bayesian` or `random` method
- Use `quick_screen` engine for initial search
- Reduce parameter ranges or increase step size
- Limit `OPTIMIZER_WORKERS` to physical CPU count

### Walk-Forward Fails Consistently

**Symptoms:** Every WF split shows degradation > 50%

**Causes:**
- Strategy overfits to specific market conditions
- Parameter ranges too wide, allowing degenerate solutions
- Insufficient data per split

**Solutions:**
- Narrow parameter ranges
- Increase `min_trades` constraint
- Use fewer splits (3 instead of 4)
- Try different time periods (maybe market regime changed)

### No Valid Results

**Symptoms:** All results fail constraints

**Causes:**
- Constraints too tight
- Strategy genuinely poor for chosen instrument/timeframe
- Bug in strategy code

**Solutions:**
- Relax constraints temporarily to see what's achievable
- Try different objective function
- Test strategy manually with current params first
- Check logs for backtest errors

### Live Performance Degrades

**Symptoms:** Live Sharpe 50% lower than backtest

**Causes:**
- Market regime changed
- Slippage/commission higher than modeled
- Data snooping bias in optimization

**Solutions:**
- Revert to previous parameters (use audit rollback)
- Re-optimize with more recent data
- Increase slippage/commission assumptions
- Reduce strategy complexity (fewer params)

---

## Further Reading

- [Strategy Guidelines](STRATEGY-GUIDELINES.md) — Writing robust strategies
- [Optimization Engine Deep Dive](OPTIMIZATION-ENGINE.md) — Technical implementation details
- [Forward Testing Guide](FORWARD-TESTING.md) — Paper/live trading setup
- [Portfolio Intelligence](PORTFOLIO-INTELLIGENCE.md) — Alerts and risk management
- [Database Schema](DATABASE.md) — Full schema reference
