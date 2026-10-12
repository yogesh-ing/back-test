# PRD — Backtest & Compare / Optimize Enhancements

> **Implementation status** (updated 2026-10-06). Implemented one section at a
> time, "bugs first" as §1 asks.
>
> | Section | Status |
> |---|---|
> | Part 1 §1.1 Engine consistency | ✅ Done — driver stays the default, `quick_screen` is an explicit **Fast Preview** opt-in, permanent Engine badge, yellow approximate warning, `mixed` stamp when Compare slots disagree |
> | Part 1 §1.2 Data source visibility | ✅ Done — permanent Data badge, red non-real-data banner, `provenance` record on every backtest / compare-slot / optimize-run payload. **Updated 2026-09-30**: a disabled configured source no longer blocks the tabs — `app.resolve_source` falls back to the best enabled source (db → mstock → dhan → csv), the gate badge stays green with one informational fallback line, and no ⛔ banner is shown |
> | Part 1 §1.3 Symbol picker + coverage | ✅ Done — `GET /api/data/coverage`, shared picker on all three pages, All/Equity/Index/F&O tabs. **Updated 2026-09-30**: the run pages' dropdowns list only symbols WITH data (`available=1`) with readable `SYMBOL (name) — bars` labels, and the search box was removed (a symbol without data is no longer selectable at all — fetch it on the Data tab first). **Updated 2026-10-01 (issues.txt B1)**: the endpoint now returns `hidden_total` (scoped to the active query/tab), and the picker's summary appends `N symbols hidden — load data first (Data tab →)` plus `M more with data — search to find them` when `PAGE_SIZE` truncates — so a short list reads as "load data first", never as "these symbols/indices do not exist". **Updated 2026-10-06 — the 2026-09-30 decision is REVERSED for the run pages**: the dropdown is a STATIC NAME LIST again (`names_only=1`: symbols and names from the shipped universe ∪ the symbols in `market_data_cache`, no bar counts / from-to / timeframes), because asking the server to measure the catalogue before it could draw one row cost 3.4s against a 140k-row scriptmaster catalogue and got worse with every instrument. Every row is selectable — the list no longer greys out "no data" rows, since whether a symbol can serve a request depends on the timeframe AND the dates, which an `<option>` cannot express. Availability is answered by the RUN, which fails with what IS stored: `Symbol 'X' has no 1day data between 2026-10-01 and 2026-10-31. Available — 1min 02 Sep 2026 to 30 Sep 2026 (4,817 bars).` The Data tab keeps real per-symbol coverage (`include_catalogue=0`) — that is its job — but no longer reads the 140k catalogue to render it. **Updated again 2026-10-06**: that tab now makes ONE request per page load (`include_catalogue=0` = fetchable universe ∪ everything fetched) and renders BOTH its list and its inventory table from it, instead of asking `/api/data/inventory` for a second full `GROUP BY` over the same rows — page load 1054ms → 271ms on a 1M-bar cache, and the two views can no longer disagree. Rows gained the window (`1min · 4,817 bars · 02 Sep → 30 Sep`); the table is one row per symbol with a Stored column; a universe symbol with no bars reads "not fetched yet" rather than "no data", because on that page it is a tickable instruction. Full detail + the hand-off list for the ingest work: `docs/HANDOFF-2026-10-06-instrument-list-and-ingest.md` |
> | Part 1 §1.4 Timeframe reality + `periods_per_year` | ✅ Done — `data.base.periods_per_year()` (252 × bars/day, weekly 52) reaches the engine, the optimizer and the UI; dropdowns offer only real granularities. **Updated 2026-10-06**: on the run pages the dropdown now offers the full canonical set again (the names-only list cannot know what a symbol has), so "only real granularities" is enforced where it can name the dates instead — `DbSource.get_candles` refuses a timeframe that cannot be BUILT from the stored bars (1day can never answer 1min: fine→coarse only) and reports what the symbol can serve; the Data tab's picker still narrows to real granularities |
> | Part 1 §2 Richer metrics | ✅ Done — `engine/metrics_risk.py` (Omega, skew, excess kurtosis, CVaR, Ulcer, drawdown episodes, streaks, Sharpe SE), `Trade.bars_held` for real durations, `trade_count_flag` ok/warn/insufficient, four collapsible sections + the insufficient-sample banner on the Backtest result page, all keys in `BacktestAdapter.to_all()` |
> | Part 1 §3 Benchmark / cost shock / Monte Carlo | ✅ Done — `engine/benchmark.py` (buy-and-hold metrics, alpha, beta), `engine/cost_shock.py` (1x/2x/3x slippage, green/yellow/red), `engine/monte_carlo.py` (reorder + bootstrap, trade concentration), `POST /api/backtest/monte-carlo`, three collapsible panels. **Two PRD rules replaced** — see the notes below |
> | Part 1 §4 Compare enhancements | ⬜ Not started (Compare already applies one shared engine — see §1.1) |
> | Part 1 §5 Certification readiness | ✅ Done — `engine/readiness.py` `build_readiness()`, 8 checks with ✅/⚠/❌, `tune_this_available` flag |
> | Part 1 §6 "Tune This" | ✅ Done — `components/tune_this.js`, two-state button (never disabled), session-handle result id, walk-forward defaults. **Updated 2026-09-30**: the prefill now also carries `baselineMetrics` (Sharpe/Return/Trades for the §2 banner) and the `baselineFillExact` + `baselineRealData` flags |
> | Part 2 §1 Receiving "Tune This" | ✅ Done — prefill banner on the Optimize setup form. **Updated 2026-09-30**: the banner quotes the baseline performance (Sharpe / Return / Trades); when the origin ran fill-exact on real data the banner says the baseline is imported instead of re-run, the run stores `baselineImported` + `baselineMetrics` so the search skips the redundant baseline evaluation, and the audit row names the originating backtest (`originated_from_backtest_id`) |
> | Part 2 §2 Data source lock + attestation | ✅ Done — migration 015, measured attestation on the run, data confirmation box on the setup form |
> | Part 2 §3 Deflated Sharpe | ✅ Done — migration 014, `optimization/deflation.py`, best-result panel + sort column |
> | Part 2 §4 Monte Carlo on the winner | ✅ Done — `POST /api/optimize/runs/<id>/monte-carlo`, apply-gate acknowledgement (a flag to read, not a block) |
> | Part 2 §5 Cleaner Apply-to-Paper Gate | ✅ Done (2026-09-30) — the apply modal is now the PRD's 3-step wizard: **Step 1 Review** with the checks table (walk-forward run/pass, robustness, deflated Sharpe, Monte Carlo P(profit), data source, trade count — each ✅/⚠️/❌; a missing check renders ⚠ "not run", never a pass); **Step 2 Choose Target** with New-Paper-Runner recommended by default, replace-existing inline picker, A/B, and the LIVE option kept clickable with an inline explanation of the ≥30-days paper-history gate instead of a dead control; **Step 3 Confirm & Audit** with the chain, the parameter diff, and a type-"CONFIRM" handshake replacing the old checkbox. Back/Continue navigation throughout |
> | Part 2 §6 Regime breakdown + warning panel | ✅ Done — regime table on the best result, sticky warning-signs panel |
>
> Implementation notes live in `PROJECT-CONTEXT.md` under "Backtest & Compare —
> Engine & Data Provenance" and "Single-Run Checks (§3)". Deviations from the
> letter of the PRD are recorded there and in the git history.
>
> ### §3 — two rules in the PRD cannot fire, and are replaced rather than shipped dead
>
> 1. **"Re-runs at 2× and 3× the configured slippage."** The canonical backtest is
>    frictionless by default (`free_executor`: zero slippage, zero fees), so
>    2 × zero is zero. Implemented literally, every strategy ever run would
>    report three identical green rows. §3.2 now stresses from an explicit,
>    labelled 5 bps NSE default when the run is frictionless, and the payload
>    carries `base_bps_source` so the UI says the run itself was not costed.
> 2. **"Median / 5th / 95th percentile final equity" and "above the 90th
>    percentile" from shuffling trades.** A shuffle cannot change a sum, so
>    under a pure reorder the final equity is identical in all 1,000
>    simulations and P(profit) is exactly 100% or 0%. Separately, a same-size
>    bootstrap resamples the very sample that defines its own distribution, so
>    the actual result's percentile has a mathematical ceiling of ~74% for any
>    n — a 90th-percentile rule can never fire. The final-equity spread now
>    comes from a **bootstrap** (resample with replacement) and the reorder
>    block is reported as path-only; the "was this lucky" question is asked
>    directly via **trade concentration** (what share of gross profit rests on
>    the single best trade), which can fail.

---

Let me check the "Optimize from Backtest" flow and data source question first, then I'll draft the PRD.

---

## Pre-PRD Checks

### Check 1: "Tune This" Button — Does it make sense?

**Yes, and here's exactly how it should work:**

Looking at the Optimize API, it already accepts `strategyId`, `symbol`, `startDate`, `endDate`, `initialCapital` — all of which a completed Backtest result already knows. So a "Tune This" button on a Backtest result page is just **pre-filling the Optimize setup form with the current backtest's context** and navigating there. No new backend work needed — pure UI convenience. The user doesn't lose their backtest result; they just jump to Optimize with everything pre-filled.

### Check 2: Is Optimize Actually Using Real Broker Data?

Reading the Optimize doc carefully:

> *"loading: candles are loaded once and shared with every worker process"*

> *"backtestConfig: symbol, timeframe, startDate, endDate, initialCapital"*

It pulls candles the same way Backtest does — from whichever source the app was started with (`--source synthetic|csv|db|mstock`). So:

- If the app is running with `--source db` → it uses the real PostgreSQL cached data ✅ (today that cache is the curated NIFTY 200 + NSE index universes at 1-minute bars — coarser timeframes resampled at read time; the old daily cache was purged 2026-10-03)
- If running with `--source synthetic` → it uses random-walk generated candles ❌ (and critically, **the user probably doesn't know this**)
- If running with `--source mstock` → it pulls live from broker ✅

**The problem:** There is no label anywhere on the Optimize UI (or Backtest UI) telling you which data source powered this run. You could optimize a strategy on synthetic data, get a robustness score of 8/10, and think it's certified — when it was never tested on real prices. This needs to be fixed as part of this PRD.

---

Now the PRD. Two parts as requested.

---

# PRD — Part 1: Backtest & Compare Enhancements

---

## Overview

Backtest and Compare are the first filter a strategy passes through before it can be considered for optimization or paper trading. Right now they give you basic results but don't give you enough information to make a confident "yes, take this further" or "no, kill this idea" decision. This PRD fixes that.

**One guiding rule for everything in Part 1:** Nothing here touches the Optimize tab's logic. Everything here either fixes existing bugs, adds richer metrics to single-run results, or adds lightweight checks (Monte Carlo, cost shock, benchmark) that only need one fixed parameter set to run.

---

## Section 1 — Bugs to Fix First (No New Features Until These Are Done)

### 1.1 Engine Consistency

**Problem:** Backtest/Compare currently defaults to the `quick_screen` (vectorized, approximate) engine. Optimize uses the `driver` (fill-exact, canonical) engine. Same strategy, same parameters, two different numbers — and the user doesn't know why.

**Fix:**
- Change Backtest and Compare to use the `driver` engine by default
- Display a permanent, non-dismissable badge on every result page showing which engine produced the numbers: `Engine: Fill-Exact (Canonical)` or `Engine: Quick-Screen (Approximate)`
- If Quick-Screen is used (e.g., for a very fast preview mode), show a yellow warning: *"These numbers are approximate. Switch to Full Engine for certification-grade results."*
- The `quick_screen` option can remain available as an explicit opt-in "Fast Preview" toggle — not the default

### 1.2 Data Source Visibility

**Problem:** No result page tells you whether the backtest ran on synthetic random data, CSV files, or real broker data. A result on synthetic data is meaningless for certification.

**Fix:**
- Every result page (Backtest, Compare, Optimize) must show a permanent data source badge: `Data: Real (PostgreSQL)`, `Data: Live (mStock)`, `Data: Synthetic`, `Data: CSV`
- If source is Synthetic or CSV, show a red banner: *"Results based on [Synthetic/CSV] data. Real-data certification required before paper testing."*
- Attach `data_source`, `data_fetch_date`, `symbol`, `timeframe`, `date_range`, `bars_count`, and `engine_used` to every stored result record — these become part of the audit trail

### 1.3 Symbol Picker — Missing Symbols and No Indices

**Problem:** Symbols silently disappear if no data is cached. Indices (NIFTY 50, BANKNIFTY spot) are not shown. No explanation given.

**Fix:**
- Build a `GET /api/data/coverage` endpoint that returns every known instrument (from the `instruments` table — all 154K), each with:
  - `symbol`, `name`, `instrument_type` (EQUITY / INDEX / FUTURES / OPTIONS)
  - `data_available: bool`
  - `bars_count`, `from_date`, `to_date`, `timeframes_available[]` (what granularities are actually stored)
- The symbol picker in Backtest, Compare, and Optimize all use this single endpoint
- Symbols with no data are shown but greyed out with a tooltip: *"No data loaded. Go to Data tab → fetch data for this symbol."*
- The picker has tabs or filter buttons: All / Equity / Index / F&O
- Indices (NIFTY 50, BANKNIFTY, SENSEX, NIFTY BANK, NIFTY MIDCAP 150, INDIA VIX) are explicitly added to the known instrument list from the NSE index segment of the `instruments` table

### 1.4 Timeframe Selector — Must Reflect Reality

**Problem:** The timeframe dropdown offers options but silently produces daily bars regardless of what you pick (documented gap G6). For intraday timeframes, `periods_per_year = 252` is also wrong, making Sharpe and CAGR silently incorrect.

**Fix:**

**Data layer (prerequisite):**
- Confirm whether 1-minute bars from live feeds are being persisted to `market_data_cache` with a `timeframe` column. If not, add persistence
- Store data at the finest available granularity (1-minute). Derive coarser timeframes at query time via resampling: `open=first, high=max, low=min, close=last, volume=sum`
- The `GET /api/data/coverage` endpoint (above) returns `timeframes_available[]` per symbol so the UI knows what to offer

**UI:**
- The timeframe dropdown reads `timeframes_available` from the coverage API for the selected symbol
- Only offer timeframes that actually have data — no phantom options
- If only daily data exists for a symbol, only show `1D`

**Engine:**
- `periods_per_year` must be calculated dynamically from the actual timeframe being backtested:

| Timeframe | periods_per_year |
|---|---|
| 1 minute | 252 × 375 (NSE trading minutes per day) |
| 5 minute | 252 × 75 |
| 10 minute | 252 × 37 |
| 15 minute | 252 × 25 |
| 30 minute | 252 × 12 |
| 1 hour | 252 × 6 |
| 1 day | 252 |

- This must ship in the same release as timeframe support. Timeframe support without this fix produces confidently wrong Sharpe and CAGR numbers

---

## Section 2 — Richer Metrics (Shared Engine Enhancement)

These are added to `engine/metrics.py` once. Every tab that uses the engine — Backtest, Compare, Optimize — gets these automatically.

### 2.1 New Metrics to Add

**Risk-Adjusted:**

| Metric | Key | Plain description |
|---|---|---|
| Sortino Ratio | `sortino` | Like Sharpe but only penalizes downside volatility, not upside. More honest for strategies with occasional big wins. |
| Omega Ratio | `omega` | Ratio of all winning return area to all losing return area. Captures the full return shape. |
| Return Skewness | `skewness` | Is the strategy's return distribution tilted toward occasional big wins (positive = good) or occasional big losses (negative = danger signal, especially for short-premium strategies)? |
| Excess Kurtosis | `kurtosis` | Are the tails fatter than normal? A high positive number means rare extreme events are more likely than they look. |
| VaR 95% | `var_95` | On a typical bad day (top 5% worst), how much would you lose? In ₹ and as % of equity. |
| CVaR / Expected Shortfall 95% | `cvar_95` | On the days that ARE in that worst 5%, what's the average loss? Always worse than VaR — this is the number that matters for tail risk. |
| Ulcer Index | `ulcer_index` | Combines drawdown depth AND duration into one number. Better than max drawdown alone for "how painful is this to hold?" |

**Drawdown Detail (currently only depth is reported):**

| Metric | Key | Description |
|---|---|---|
| Max Drawdown Duration | `max_drawdown_duration_days` | How many calendar days did the worst drawdown last start to end? |
| Max Drawdown Recovery Days | `max_drawdown_recovery_days` | How many days to fully recover from the worst drawdown? |
| Time in Drawdown % | `time_in_drawdown_pct` | What percentage of the total backtest period was the strategy below its previous peak? |
| Number of Drawdowns > 10% | `drawdowns_over_10pct` | How many times did it drop more than 10%? |

**Trade Quality:**

| Metric | Key | Description |
|---|---|---|
| Profit Factor | `profit_factor` | Gross profit ÷ gross loss. > 1.5 is decent. < 1.0 means losing strategy. More meaningful than win rate alone. |
| Expectancy per Trade (₹) | `expectancy_inr` | Average ₹ made or lost per trade, accounting for win rate and payoff ratio together. The single most useful trade-quality number. |
| Payoff Ratio | `payoff_ratio` | Average winning trade ÷ average losing trade. |
| Max Consecutive Wins | `max_consecutive_wins` | |
| Max Consecutive Losses | `max_consecutive_losses` | Critical for capital tolerance planning. |
| Avg Trade Duration (bars) | `avg_trade_duration_bars` | |
| Median Trade Duration (bars) | `median_trade_duration_bars` | |

**Statistical Confidence:**

| Metric | Key | Description |
|---|---|---|
| Trade Count Warning | `trade_count_flag` | Flag: `ok` if ≥ 30 closed trades, `warn` if 20–29, `insufficient` if < 20. Displayed prominently — Sharpe is unreliable on small samples. |
| Sharpe Standard Error | `sharpe_std_error` | ≈ 1/√N. If Sharpe is 1.5 and std error is 1.2, the "edge" is noise. |

### 2.2 Display Rules

- The existing metrics panel keeps its current layout
- New metrics appear in clearly labelled expandable sections below: "Risk & Tail Metrics", "Drawdown Detail", "Trade Quality", "Statistical Confidence"
- If `trade_count_flag` is `insufficient`, overlay a yellow warning banner on the entire result: *"Fewer than 20 closed trades — all metrics have very high statistical uncertainty. Run on a longer date range or different symbol before drawing conclusions."*
- All new metrics are included in the JSON payload from `BacktestAdapter.to_all()` — same payload, just more keys

---

## Section 3 — New Single-Run Checks (Backtest & Compare Only)

These three features only need a single fixed result to work — they are NOT search/optimization features.

### 3.1 Benchmark Comparison

**What it does:** Automatically runs buy-and-hold on the same symbol, same date range, same capital, and shows it alongside every result. Makes it immediately obvious whether the strategy actually adds value over "do nothing."

**Implementation:**
- `buy_and_hold` is already a registered strategy. After every Backtest/Compare run, silently run buy-and-hold on the same inputs (it's near-instant)
- Show a persistent "vs Benchmark" row in the metrics panel: `Strategy: Sharpe 1.2 / Return 34% | Buy & Hold: Sharpe 0.8 / Return 28%`
- Show Alpha: strategy return minus benchmark return, same period
- Show Beta: correlation of strategy daily returns to benchmark daily returns (simple OLS, one-liner)
- In the equity curve chart, always overlay the benchmark curve as a grey dashed line
- In Compare tab, the benchmark is one common reference line across all 4 strategies (not a 5th card — it's a reference)

**No new API needed** — this is handled server-side before the result is returned. Add `benchmark` key to the adapter payload.

### 3.2 Cost-Shock Stress Test

**What it does:** Re-runs the exact same backtest at 2× and 3× the configured slippage, and shows whether the edge survives. This is the fastest and most practically useful robustness check for a single-run result.

**Why it matters:** Real slippage is unpredictable. Your own engineering notes say slippage typically dwarfs commission. A strategy that requires perfect fills to be profitable is not safe for live trading.

**Implementation:**
- After every Backtest/Compare run, automatically run the same configuration at `slippage_pct × 2` and `slippage_pct × 3`
- Show a mini-table below the main metrics:

```
Cost Scenario    | Total Return | Sharpe | Profitable?
─────────────────|──────────────|────────|────────────
Base (0.05%)     |    +34%      |  1.42  |    ✅
2× Slippage      |    +21%      |  0.98  |    ✅
3× Slippage      |     -4%      |  -0.21 |    ❌
```

- If the strategy turns unprofitable at 2× slippage: red warning banner — *"Edge disappears at 2× slippage. Strategy is not robust to execution uncertainty."*
- If profitable at 2× but not 3×: yellow warning
- If profitable at 3×: green indicator

**No new API endpoint needed** — three quick vectorized runs, bundled into the existing result payload under `cost_shock` key.

### 3.3 Monte Carlo — Trade Sequence Resampling

**What it does:** Takes the list of closed trades from the backtest, randomly shuffles their order many times (e.g., 1,000 times), rebuilds the equity curve each time, and shows the distribution of outcomes. Answers: "Was this result lucky, or would this strategy have done well in most orderings of these same trades?"

**Why it belongs in Backtest/Compare and NOT Optimize:** Monte Carlo here checks the quality of ONE fixed result. Optimize already does walk-forward on rolling windows which serves the "is it overfit" question for the search process. Monte Carlo is complementary — it checks if the trade sequence itself was lucky, which is independent of parameter tuning.

**Implementation:**
- Takes `closed_trades` P&L list from the result (already computed by `walk_trades`)
- Runs 1,000 random shuffles, rebuilds cumulative equity each time
- Reports:
  - Median final equity across 1,000 runs
  - 5th percentile final equity (the pessimistic case)
  - 95th percentile final equity (the optimistic case)
  - Median max drawdown, 95th percentile max drawdown
  - Probability of profit: % of simulations that ended above starting capital
- Display as a fan chart overlaid on the equity curve: dark band = 25th–75th percentile, light band = 5th–95th percentile, solid line = actual result
- If the actual result is above the 90th percentile of simulations: yellow flag — *"Actual result was unusually lucky in trade ordering. Median expectation is significantly lower."*
- If probability of profit < 60%: red flag

**API:** Add `POST /api/backtest/monte-carlo` endpoint that accepts a result_id (or inline trade list) and returns the distribution stats. Run asynchronously (1,000 shuffles on even 100 trades takes under 1 second — can be synchronous in practice).

---

## Section 4 — Compare Tab Enhancements

### 4.1 Mandatory Identical Conditions

**Problem:** Nothing stops comparing Strategy A on quick-screen engine with Strategy B on fill-exact engine, or different date ranges. The comparison is then meaningless.

**Fix:**
- Compare tab has one shared configuration panel at the top: symbol, date range, capital, timeframe, engine, cost settings
- These apply to ALL strategies being compared — no per-strategy overrides of these fields
- Strategy-specific fields (parameters) remain per-strategy
- If any strategy fails to run, show its card with the error — do not silently exclude it

### 4.2 Same-Strategy, Different-Symbols Mode

**New mode in the Compare tab.** Currently Compare = different strategies, same data. Add a toggle: "Compare Strategies" (current behavior) vs "Test Generalization" mode.

In "Test Generalization" mode:
- User picks ONE strategy with ONE set of parameters
- User picks up to 4 different symbols (can include NIFTY, BANKNIFTY, individual stocks)
- The system runs the strategy on all 4 symbols over the same date range
- Results shown side by side: does the edge generalize across instruments?
- If a strategy only works on one stock out of four similar ones, that is a curve-fitting red flag — shown explicitly

### 4.3 Inter-Strategy Correlation Panel

**What it does:** Shows how correlated the 4 strategies' daily returns are with each other. If two strategies are 0.95 correlated, running both adds no diversification — you're just taking the same risk twice.

**Implementation:**
- After all runs complete, compute pairwise Pearson correlation of daily returns
- Display as a 4×4 heatmap (color coded: green = low correlation = good diversification, red = high correlation = redundant)
- Flag any pair with correlation > 0.8 with a warning: *"Strategies A and B are highly correlated. Running both simultaneously offers little diversification benefit."*

### 4.4 Statistical Significance Between Candidates

**What it does:** Tells you whether Strategy A's higher Sharpe than Strategy B is real or just noise.

**Implementation:**
- After all runs complete, run a simple bootstrap test: resample each strategy's daily return series 1,000 times, compare Sharpe distributions
- Show "A is likely better than B (85% confidence)" or "No significant difference between A and B"
- This is not a hard gate — it's informational text below each comparison
- Prevents the common mistake of promoting the "best" strategy out of 4 when all 4 have statistically identical performance and the apparent winner is just the luckiest

### 4.5 Rebased Equity Curves

Small but impactful: in Compare, all equity curves must start at the same point (index to 100 at start date) so they're visually comparable regardless of strategy return magnitude. Currently if two strategies have different initial capital behavior, the chart is misleading.

---

## Section 5 — Basic Certification Readiness Indicator

This is a simple traffic light panel — it does NOT make the decision, it just surfaces information you need to make the decision yourself.

**Shown at the bottom of every Backtest result, and per-strategy in Compare.**

| Check | Green | Yellow | Red |
|---|---|---|---|
| Engine | Fill-Exact | — | Quick-Screen |
| Data Source | Real (DB/Broker) | CSV | Synthetic |
| Trade Count | ≥ 30 | 20–29 | < 20 |
| Beats Benchmark | Yes | — | No |
| Cost-Shock (2×) | Profitable | — | Loss-making |
| Profit Factor | ≥ 1.5 | 1.0–1.5 | < 1.0 |
| Max Drawdown | < 15% | 15–25% | > 25% |
| Monte Carlo P(profit) | ≥ 75% | 50–75% | < 50% |

**Below the traffic light:**
- If all green: *"Basic checks passed. Consider running Optimize to validate parameter robustness before paper trading."* + **"Tune This →"** button (see Section 6)
- If any red: *"One or more checks failed. Review flagged items before proceeding to Optimize."*
- If all green but Optimize not yet run: the "Proceed to Paper" button is shown but disabled with tooltip: *"Complete Optimize step first."*

**Important:** This is advisory only — no hard blocks at this stage. Hard blocks live in Optimize (they already exist) and in the Paper→Live gate (also already exists).

---

## Section 6 — "Tune This" Button (Backtest → Optimize Flow)

**What it is:** A button on a completed Backtest result that opens the Optimize tab with all common fields pre-filled from the current backtest context.

**Pre-filled fields:**
- `strategyId` from current result
- `symbol`, `startDate`, `endDate`, `timeframe`, `initialCapital` from current result
- `engine` from current result (whichever was used)
- `data_source` locked to same source
- Parameters pre-filled with the current values (as the `current` baseline — Optimize already shows delta vs. current)
- Default objective: `sharpe`
- Default method: `bayesian` (best balance of speed and thoroughness for a first-time optimization)
- Walk-forward: enabled by default, with `trainPeriodDays` set to 2/3 of the total backtest period, `testPeriodDays` the remaining 1/3

**What it is NOT:** It does not auto-run the Optimize. It just navigates to the Optimize setup page with fields pre-filled. The user reviews and hits "Start Optimization" themselves.

**Implementation:** Pure frontend — read from the current result JSON, build the Optimize config object, navigate to `/optimize` with it as URL state or session storage. No new API endpoint.

**Reverse flow:** When an Optimize run completes and the user clicks "Apply to Paper," the confirmation screen shows: *"Applied from Backtest result [ID] → Optimize run [ID] → Paper runner [ID]"* — a three-step audit chain visible in the audit log.

---

## Wireframes — Part 1

### Wireframe 1: Backtest Result Page (Enhanced)

```
┌─────────────────────────────────────────────────────────────────────┐
│  BACKTEST: SMA Crossover — RELIANCE — 2020-01-01 to 2024-12-31     │
│  ┌─────────────────────┐  ┌──────────────────┐                     │
│  │ Engine: Fill-Exact  │  │ Data: Real (DB)  │  1D | Bars: 1,247   │
│  └─────────────────────┘  └──────────────────┘                     │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  EQUITY CURVE                                              [1Y][ALL]│
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │  ___                                                        │   │
│  │ /   \___/‾‾‾\___/‾‾‾‾‾‾‾‾‾‾‾‾‾‾\____/‾‾‾‾‾  ← Strategy   │   │
│  │ - - - - - - - - - - - - - - - - - - - - - -  ← Buy & Hold  │   │
│  │ ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  ← MC bands   │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
├─────────────────────────────────────────────────────────────────────┤
│  CORE METRICS                                                       │
│  ┌──────────┬──────────┬──────────┬──────────┬──────────┐         │
│  │ Return   │ Sharpe   │ Max DD   │ Trades   │ Win Rate │         │
│  │  +34%    │  1.42    │  -18%    │   47     │  58%     │         │
│  │ BnH:+28% │ BnH:0.81 │          │          │          │         │
│  └──────────┴──────────┴──────────┴──────────┴──────────┘         │
│                                                                     │
│  Alpha: +6%   Beta: 0.73                                           │
│                                                                     │
├─────────────────────────────────────────────────────────────────────┤
│  ▼ RISK & TAIL METRICS                                              │
│  ┌────────────┬────────────┬───────────┬───────────┬──────────┐   │
│  │  Sortino   │  Omega     │  Skew     │  CVaR 95% │  Ulcer   │   │
│  │   1.89     │  1.34      │  +0.21    │  -2.1%/d  │   4.2    │   │
│  └────────────┴────────────┴───────────┴───────────┴──────────┘   │
│                                                                     │
├─────────────────────────────────────────────────────────────────────┤
│  ▼ DRAWDOWN DETAIL                                                  │
│  Max DD Duration: 47 days  │  Recovery: 61 days  │  Time in DD: 23%│
│  Drawdowns > 10%: 3                                                 │
│                                                                     │
├─────────────────────────────────────────────────────────────────────┤
│  ▼ TRADE QUALITY                                                    │
│  ┌──────────────┬──────────────┬───────────┬──────────────┐        │
│  │ Profit Factor│  Expectancy  │Payoff Ratio│ Max Consec. │        │
│  │     1.68     │   ₹ 4,230    │   1.84:1  │  W:5 / L:4  │        │
│  └──────────────┴──────────────┴───────────┴──────────────┘        │
│                                                                     │
├─────────────────────────────────────────────────────────────────────┤
│  COST SHOCK                                                         │
│  ┌────────────────┬──────────────┬──────────┬─────────┐            │
│  │ Scenario       │ Total Return │  Sharpe  │ Status  │            │
│  │ Base (0.05%)   │    +34%      │   1.42   │   ✅    │            │
│  │ 2× Slippage    │    +21%      │   0.98   │   ✅    │            │
│  │ 3× Slippage    │    -4%       │  -0.21   │   ❌    │            │
│  └────────────────┴──────────────┴──────────┴─────────┘            │
│  ⚠ Edge disappears at 3× slippage — monitor execution quality live │
│                                                                     │
├─────────────────────────────────────────────────────────────────────┤
│  MONTE CARLO (1,000 simulations)                                    │
│  Median Final Equity: ₹1,28,400  │  P(Profit): 79%                 │
│  5th pct: ₹94,200  │  95th pct: ₹1,67,800                         │
│  ★ Your actual result (₹1,34,000) sits at the 68th percentile      │
│    — reasonably typical, not an outlier                             │
│                                                                     │
├─────────────────────────────────────────────────────────────────────┤
│  CERTIFICATION READINESS                                            │
│  ┌──────────────────┬───────┐  ┌──────────────────┬───────┐        │
│  │ Engine           │  ✅   │  │ Trade Count (47) │  ✅   │        │
│  │ Data Source      │  ✅   │  │ Beats Benchmark  │  ✅   │        │
│  │ Cost-Shock (2×)  │  ✅   │  │ Profit Factor    │  ✅   │        │
│  │ Cost-Shock (3×)  │  ⚠️   │  │ Max Drawdown     │  ✅   │        │
│  │ Monte Carlo P(p) │  ✅   │  │                  │       │        │
│  └──────────────────┴───────┘  └──────────────────┴───────┘        │
│                                                                     │
│  Basic checks passed. Validate parameter robustness before paper.  │
│                                                                     │
│  ┌─────────────────────┐   ┌──────────────────────────────────┐   │
│  │  ⚙ Tune This →      │   │  Proceed to Paper  (disabled 🔒) │   │
│  │  (Open in Optimize) │   │  Complete Optimize step first    │   │
│  └─────────────────────┘   └──────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

---

### Wireframe 2: Symbol Picker (Shared Component)

```
┌─────────────────────────────────────────────────────┐
│  Select Symbol                                      │
│  ┌─────────────────────────────────────────────┐   │
│  │ 🔍 Search: NIFTY...                         │   │
│  └─────────────────────────────────────────────┘   │
│                                                     │
│  [All] [Equity] [Index] [F&O]                       │
│                                                     │
│  Indices                                            │
│  ┌──────────────────────────────────────────────┐  │
│  │ ✅ NIFTY 50        Daily: 2020–2024 (1,247b) │  │
│  │ ✅ BANKNIFTY       Daily: 2020–2024 (1,247b) │  │
│  │ ✅ NIFTY MIDCAP150 Daily: 2021–2024  (987b)  │  │
│  │ ⬜ INDIA VIX       No data — Fetch in Data ↗ │  │
│  └──────────────────────────────────────────────┘  │
│                                                     │
│  Equity (NIFTY 200)                                 │
│  ┌──────────────────────────────────────────────┐  │
│  │ ✅ RELIANCE        Daily: 2020–2024 (1,247b) │  │
│  │ ✅ TCS             Daily: 2020–2024 (1,247b) │  │
│  │ ⬜ ADANIPORTS      No data — Fetch in Data ↗ │  │
│  │ ⬜ ZOMATO          No data — Fetch in Data ↗ │  │
│  └──────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────┘
```

---

### Wireframe 3: Compare Tab — Test Generalization Mode

```
┌─────────────────────────────────────────────────────────────────────┐
│  COMPARE                                                            │
│  Mode: [◉ Compare Strategies] [○ Test Generalization]              │
├─────────────────────────────────────────────────────────────────────┤
│  — Test Generalization Mode —                                       │
│  Strategy: [SMA Crossover ▾]   Params: fast=20, slow=50            │
│  Date Range: 2020-01-01 → 2024-12-31   Timeframe: [1D ▾]          │
│                                                                     │
│  Symbols to test on:                                                │
│  [RELIANCE ✕] [TCS ✕] [NIFTY 50 ✕] [HDFC BANK ✕]  [+ Add]       │
│                                                                     │
│  [Run Test Generalization]                                          │
├─────────────────────────────────────────────────────────────────────┤
│  RESULTS                              Benchmark (Buy & Hold) ----   │
│                                                                     │
│  ┌──────────┬──────────┬──────────┬──────────┐                     │
│  │ RELIANCE │   TCS    │ NIFTY 50 │ HDFC BNK │                     │
│  │  +34%    │  +28%    │  +19%    │  +31%    │  ← Return           │
│  │  Sh:1.42 │  Sh:1.21 │  Sh:0.87 │  Sh:1.38 │  ← Sharpe          │
│  │  PF:1.68 │  PF:1.45 │  PF:1.12 │  PF:1.61 │  ← Profit Factor   │
│  │    ✅    │    ✅    │    ⚠️    │    ✅    │  ← Certification   │
│  └──────────┴──────────┴──────────┴──────────┘                     │
│                                                                     │
│  GENERALIZATION SCORE: 3 out of 4 symbols profitable               │
│  ℹ️ Edge appears consistent. NIFTY 50 underperforms — likely low   │
│  volatility reduces signal frequency.                               │
│                                                                     │
│  CORRELATION MATRIX                                                 │
│  ┌──────────┬──────────┬──────────┬──────────┐                     │
│  │          │ RELIANCE │   TCS    │ NIFTY 50 │                     │
│  │ RELIANCE │    —     │   0.72   │   0.81   │                     │
│  │ TCS      │   0.72   │    —     │   0.68   │                     │
│  │ NIFTY 50 │   0.81   │   0.68   │    —     │                     │
│  └──────────┴──────────┴──────────┴──────────┘                     │
│  ⚠ RELIANCE and NIFTY 50 returns are highly correlated (0.81)      │
└─────────────────────────────────────────────────────────────────────┘
```

---

# PRD — Part 2: Optimize Tab Enhancements

---

## Overview

The Optimize tab is already well-built. It has walk-forward, sensitivity analysis, robustness scoring, audit trail, apply-to-paper/live with rollback. The gaps are:

1. It doesn't tell you clearly which data it ran on
2. The "Tune This" flow from Backtest needs to land cleanly here
3. Monte Carlo should be available on the winning result inside Optimize (not on every candidate — just the winner)
4. The Apply-to-Paper gate needs to be visually clearer and harder to misread
5. Deflated Sharpe (correcting for the fact that you tried many combinations) should be added

---

## Section 1 — Receiving "Tune This" from Backtest

When a user arrives at Optimize via the "Tune This" button from a Backtest result:

**UI behavior:**
- The setup form opens pre-filled (as described in Part 1 Section 6)
- A blue banner shows at the top: *"Pre-filled from Backtest result [ID] — SMA Crossover on RELIANCE, 2020–2024. Baseline performance: Sharpe 1.42, Return +34%."*
- The data source field is locked to the same source as the originating backtest (if it was Real/DB, it stays Real/DB — you cannot accidentally optimize on synthetic data if the backtest ran on real data)
- Baseline run (step 2 of the pipeline) is skipped if the originating backtest was already the fill-exact engine on real data — the result is imported directly as the baseline, saving time

**Audit trail:** The resulting Optimize run stores `originated_from_backtest_id` in the runs table. When you later do "Apply to Paper," the audit row shows the full chain: Backtest [ID] → Optimize [ID] → Paper Runner [ID].

---

## Section 2 — Data Source Lock and Attestation

**Problem:** Currently Optimize pulls candles from whatever source the app started with, and there's no visible confirmation of this anywhere.

**Fix:**
- Add `data_source`, `data_fetch_date`, `bars_count`, `symbol`, `timeframe`, `date_range` to the `optimization_runs` table (new columns, additive migration)
- On the setup page, show a data attestation box before the user can start:

```
┌──────────────────────────────────────────────────────┐
│  DATA CONFIRMATION                                   │
│  Source:    Real Data (PostgreSQL)         ✅        │
│  Symbol:    RELIANCE                                 │
│  Bars:      1,247 daily bars                        │
│  Range:     2020-01-01 → 2024-12-31                 │
│  Fetched:   2026-09-27 (today)                      │
│                                                      │
│  ⚠ If data is stale (> 30 days old), consider       │
│    re-fetching before optimizing.                    │
└──────────────────────────────────────────────────────┘
```

- If source is Synthetic: red box, cannot proceed without explicitly checking *"I understand this is synthetic data and results are not certification-grade"*
- Data attestation is stored on the run record and shown on the results page and the audit trail

---

## Section 3 — Deflated Sharpe Ratio

**What it is (plain language):** If you flip a coin 500 times trying to find a "heads-biased" coin and eventually find one that gave 60% heads, that doesn't mean the coin is actually biased — you just had 500 tries and got lucky once. The same thing happens when you try 500 parameter combinations: the "best" one will look good even if no combination actually has an edge. Deflated Sharpe corrects for this.

**What it does:**
- Takes the number of combinations tried (from the run), the best observed Sharpe, and the distribution of all tested Sharpes
- Applies the Bailey-López de Prado deflation formula
- Reports `deflated_sharpe` alongside regular Sharpe in the results

**Display:**
- In the best-result panel: `Sharpe: 1.42 | Deflated Sharpe: 0.89 (after correcting for 56 combinations tested)`
- If deflated Sharpe < 0.5 when the raw Sharpe was > 1.5: orange warning — *"Large gap between observed and deflated Sharpe. The result may be a statistical artifact of testing many combinations."*
- Stored in the `optimization_runs` table alongside `robustness_score`

---

## Section 4 — Monte Carlo on the Winner

After an Optimize run completes, offer Monte Carlo on the single best result (not on all 50,000 candidates — just the winner that would be applied to paper).

**Button location:** On the results page, next to the "Apply to Paper" button: *"Run Monte Carlo on Best Result"*

**Behavior:** Exactly the same as Part 1 Section 3.3 Monte Carlo — takes the closed trades of the winning backtest, runs 1,000 shuffles, shows fan chart and distribution stats.

**Why here too:** Walk-forward validates that the parameters generalize across time. Monte Carlo validates that the trade sequence of the best result isn't a lucky ordering. They answer different questions. Both together are the strongest pre-paper validation available without real trading.

**Gate behavior:** If Monte Carlo P(profit) < 60%, add a warning to the Apply gate — not a hard block, but a visible flag that must be acknowledged before proceeding.

---

## Section 5 — Cleaner Apply-to-Paper Gate

The existing gate is technically correct but visually cluttered. Simplify and harden it.

**Current problems:**
- `confirm_live: true` and `allow_unvalidated: true` flags are API-level concepts that leak into the UI
- The failure reasons (overfitted, no walk-forward) are shown as text but easy to miss

**New Apply flow — 3-step modal:**

```
Step 1: REVIEW RESULTS
┌──────────────────────────────────────────────────────┐
│  APPLYING: SMA Crossover — Best Parameters           │
│  fast=15, slow=75                                    │
│                                                      │
│  ┌────────────────────────┬──────────┬───────────┐  │
│  │ Check                  │ Result   │ Status    │  │
│  ├────────────────────────┼──────────┼───────────┤  │
│  │ Walk-forward run       │ Yes      │    ✅     │  │
│  │ Walk-forward pass      │ 0.78 eff │    ✅     │  │
│  │ Robustness score       │  7/10    │    ✅     │  │
│  │ Deflated Sharpe        │  0.89    │    ✅     │  │
│  │ Monte Carlo P(profit)  │   76%    │    ✅     │  │
│  │ Data source            │ Real DB  │    ✅     │  │
│  │ Trade count            │  52      │    ✅     │  │
│  └────────────────────────┴──────────┴───────────┘  │
│                                                      │
│  [Cancel]              [Continue to Step 2 →]       │
└──────────────────────────────────────────────────────┘

Step 2: CHOOSE TARGET
┌──────────────────────────────────────────────────────┐
│  Where should these parameters be applied?           │
│                                                      │
│  ◉ New Paper Runner (recommended first step)         │
│  ○ Replace existing paper runner: [select runner ▾] │
│  ○ A/B Test (run alongside existing runner)          │
│                                                      │
│  ○ Apply to LIVE runner  ⚠ Real money                │
│    (only available after paper test — currently      │
│     grayed out unless a paper runner exists for      │
│     this strategy with ≥ 30 days of history)         │
│                                                      │
│  [← Back]              [Continue to Step 3 →]       │
└──────────────────────────────────────────────────────┘

Step 3: CONFIRM & AUDIT
┌──────────────────────────────────────────────────────┐
│  CONFIRM APPLICATION                                 │
│                                                      │
│  Chain: Backtest [BT-4821] → Optimize [OPT-0193]    │
│         → New Paper Runner                           │
│                                                      │
│  Parameters changing:                               │
│  fast: 20 → 15                                      │
│  slow: 50 → 75                                      │
│                                                      │
│  This action will be logged in the audit trail.     │
│  You can rollback once via Audit → Rollback.        │
│                                                      │
│  Type "CONFIRM" to proceed:  [____________]          │
│                                                      │
│  [← Back]              [Apply to Paper Runner]      │
└──────────────────────────────────────────────────────┘
```

---

## Section 6 — Optimize Results Page — Small Improvements

### 6.1 Regime Breakdown on Best Result

After walk-forward completes, automatically show how the best parameters performed in different market regimes. Use pre-defined date bands:

| Period | Label |
|---|---|
| Jan 2020 – Mar 2020 | COVID crash |
| Apr 2020 – Dec 2021 | Recovery bull |
| Jan 2022 – Jun 2022 | Rate-hike correction |
| Jul 2022 – Dec 2023 | Volatile recovery |
| 2024 | Low-volatility grind |

For each period: Return, Sharpe, Max DD, Trades — shown in a table. A strategy that only works in bull markets should not be certified for all-weather paper trading.

### 6.2 Warning Signs Panel — Make It Impossible to Miss

Existing warning signs (Sharpe > 3, < 30 trades, best value on range edge, etc.) are generated but easy to miss in the current layout.

**Fix:** Move warning signs to a sticky panel at the top of the results page. Not dismissable. Color coded:

```
┌──────────────────────────────────────────────────────────────────┐
│  ⚠ 2 WARNING SIGNS                                              │
│  • Best parameter value (fast=5) is at the edge of your         │
│    search range. Consider extending the range downward.          │
│  • Only 23 trades in walk-forward test windows — results        │
│    have high statistical uncertainty.                            │
└──────────────────────────────────────────────────────────────────┘
```

---

## Wireframes — Part 2

### Wireframe 4: Optimize Setup Page — Pre-filled via "Tune This"

```
┌─────────────────────────────────────────────────────────────────────┐
│  OPTIMIZE                                                           │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │  📎 Pre-filled from Backtest BT-4821                        │   │
│  │  SMA Crossover · RELIANCE · 2020–2024                       │   │
│  │  Baseline: Sharpe 1.42 | Return +34% | Trades 47            │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  Strategy: SMA Crossover [locked from backtest]                     │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │  DATA CONFIRMATION                              ✅ Real DB  │   │
│  │  Symbol: RELIANCE  │  1,247 daily bars                      │   │
│  │  Range: 2020-01-01 → 2024-12-31                             │   │
│  │  Fetched: 2026-09-27 (today)                                │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  PARAMETERS TO OPTIMIZE                                             │
│  ┌─────────┬─────────┬─────────┬─────────┬────────────────────┐   │
│  │ Param   │ Current │   Min   │   Max   │  Step  │ Optimize? │   │
│  │ fast    │   20    │    5    │   40    │    5   │    ✅      │   │
│  │ slow    │   50    │   50    │  200    │   25   │    ✅      │   │
│  │stop_loss│  0.05   │  0.02   │  0.10   │  0.01  │    ❌      │   │
│  └─────────┴─────────┴─────────┴─────────┴────────────────────┘   │
│  Estimated combinations: 56  │  Est. time: ~12s                    │
│                                                                     │
│  OBJECTIVE: [Sharpe ▾]   METHOD: [Bayesian ▾]                      │
│                                                                     │
│  WALK-FORWARD: ✅ Enabled                                           │
│  Train: 730d  │  Test: 365d  │  Step: 365d                         │
│                                                                     │
│  CONSTRAINTS:  Max Drawdown < [25]%    Min Trades ≥ [20]           │
│                                                                     │
│  [Estimate Run Time]          [▶ Start Optimization]               │
└─────────────────────────────────────────────────────────────────────┘
```

---

### Wireframe 5: Optimize Results — Warning Signs + Deflated Sharpe + Apply Gate

```
┌─────────────────────────────────────────────────────────────────────┐
│  OPTIMIZE RESULTS — SMA Crossover — Run OPT-0193                   │
│  Data: Real (PostgreSQL) ✅ | Engine: Fill-Exact ✅                 │
├─────────────────────────────────────────────────────────────────────┤
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │  ⚠ 1 WARNING SIGN                                          │   │
│  │  • Best parameter (fast=5) is at the edge of search range. │   │
│  │    Consider widening the range to [1, 40] and re-running.  │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  BEST RESULT                                                        │
│  fast=15, slow=75                                                   │
│  ┌──────────┬──────────┬───────────────────────────┬──────────┐   │
│  │  Sharpe  │ Deflated │  Robustness   │  WF Eff.  │ Trades  │   │
│  │   1.61   │   0.89   │   7/10 ✅    │   0.78    │   52    │   │
│  └──────────┴──────────┴───────────────────────────┴──────────┘   │
│  Baseline (fast=20, slow=50): Sharpe 1.42 | Return +34%            │
│  Improvement: Sharpe +0.19 | Return +4.2%                          │
│                                                                     │
├─────────────────────────────────────────────────────────────────────┤
│  REGIME BREAKDOWN                                                   │
│  ┌──────────────────┬────────┬────────┬────────┬──────────┐        │
│  │ Period           │ Return │ Sharpe │ Max DD │ Trades   │        │
│  │ COVID Crash      │  -8%   │ -0.41  │  -24%  │    4     │        │
│  │ Recovery Bull    │  +47%  │  2.1   │  -9%   │   18     │        │
│  │ Rate-hike Corr.  │  +12%  │  0.88  │  -15%  │    9     │        │
│  │ Volatile Recov.  │  +21%  │  1.4   │  -11%  │   14     │        │
│  │ 2024 Low-vol     │  +7%   │  0.71  │  -8%   │    7     │        │
│  └──────────────────┴────────┴────────┴────────┴──────────┘        │
│  ⚠ Underperforms in low-volatility (2024) and crash (2020)         │
│                                                                     │
├─────────────────────────────────────────────────────────────────────┤
│  WALK-FORWARD          SENSITIVITY                 HEATMAP          │
│  [View Splits ▾]       [View Curves ▾]            [View ▾]         │
│                                                                     │
├─────────────────────────────────────────────────────────────────────┤
│  ┌────────────────────────┐   ┌──────────────────────────────────┐ │
│  │  Run Monte Carlo on   │   │                                  │ │
│  │  Best Result          │   │   Apply Best Parameters →        │ │
│  └────────────────────────┘   └──────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────┘
```

---

## What is NOT in this PRD

To be explicit about boundaries:

- **No changes to Forward/Paper tab behavior** — that's a separate scope
- **No changes to Live trading gate** — it already exists and works; Part 2 Section 5 only improves the UI presentation of the existing gate, not its logic
- **No new optimization search methods** — the four existing ones (grid, random, Bayesian, genetic) are sufficient
- **No portfolio-level correlation checks in Optimize** — that belongs in the Portfolio tab's intelligence layer
- **No scheduled re-optimization** — already documented as out of scope, re-run via CLI cron as documented