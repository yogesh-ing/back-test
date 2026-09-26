# `/analytics` Tab — Quant Engineering Review & Gap Analysis

> **Scope:** Full review of the `/analytics` tab (portfolio overview + per-strategy detail) as shipped on branch `arena/01a0c511-back-test`.
> **Files reviewed:** `src/backtest/api/analytics_service.py`, `src/backtest/api/analytics.py`, `src/backtest/web/static/js/analytics.js`, `src/backtest/web/templates/analytics.html`, and the data sources they read (`src/backtest/forward/paper_runner.py`, `src/backtest/forward/state_store.py`).
> **Reviewer stance:** institutional quant desk — correctness first, then data integrity, then methodology, then UX.
> **Legend:** 🔴 Critical (numbers are wrong / unsafe) · 🟠 High (numbers silently incomplete) · 🟡 Medium (methodology debatable) · ⚪ Low (UX / polish)

---

## Executive Summary

The analytics tab is a **good v1**: it renders live portfolio and per-strategy metrics, has period filters, rolling metrics, edge-degradation detection, and a health rating. However, it currently computes every number from **in-memory, truncated runner state** (last 200 trades, decimated equity curve, 500-point persisted tail). Consequences:

1. **All long-horizon claims (90d / 1y filters) are illusory** after an app restart — history is silently truncated, so "1y Sharpe" is really "since-restart Sharpe on ≤200 trades."
2. **Several headline metrics are mathematically mislabeled or biased**: the monthly "Sharpe" is not a Sharpe; per-trade Sharpe annualization inflates with trade count; Calmar is not annualized; drawdown ignores intra-trade MTM.
3. **A user-controlled string reaches `innerHTML` unescaped** (stored-XSS vector from runner names).
4. **No persistence layer is consulted** even though DB tables with full history already exist.

Verdict: **safe to look at, not safe to make capital decisions from yet.** Fixes are ranked in §8.

---

## 1. 🔴 Critical — Correctness / Safety

### 1.1 Data truncation invalidates the period filters (data integrity)
- `StrategyRunner.closed_trades` (`paper_runner.py:785`) returns only the **last 200** trades (`MAX_TRADE_LOG = 200`, `paper_runner.py:466`).
- The equity curve is **decimated by 2** once it hits `MAX_EQUITY_POINTS = 500` (`paper_runner.py:467`, `_record_equity_point` at ~:1480) — every decimation pass throws away every other observation, including (by construction of the `::2` slice) the *latest* point's neighbours, so peak-to-trough resolution degrades over time.
- `state_store.py` persists only a **500-point tail** (`_HISTORY_TAIL = 500`, `state_store.py:56`) of `closed_trades_cache` and `equity_curve`.

**Why it matters:** every metric on the tab — Sharpe, Sortino, PF, DD, streaks, rolling windows, edge degradation — is computed from this tail. The 7d/30d/90d/1y period filters *silently* cap at 200 trades / 500 points. A strategy traded for 6 months looks like a 3-week strategy. Nothing in the UI warns the user.

**Fix direction:** move analytics reads onto the persisted DB tables (`strategy_signals`, `forward_test_trades`, `forward_test_equity` already exist) or an append-only parquet/SQLite trade log; treat runner memory as a cache, not the source of truth.

### 1.2 Stored XSS via runner name in `analytics.js`
`card.name` (and alert `a.message`) are interpolated **raw into template literals assigned to `innerHTML`**. Runner names are user-controlled at spawn time. A runner named `<img src=x onerror=...>` executes in the analytics pane of anyone who opens the tab.

**Fix direction:** escape all interpolated strings (or set `textContent`), or render via DOM builders. Same audit should cover `portfolio.js` and the compare tab.

### 1.3 Timezone handling skews every period boundary
- `_parse_ts` treats **naive timestamps as UTC**, but runner bar/exit timestamps are **IST market-time**.
- Period cutoffs use `datetime.now(timezone.utc)`; equity points are stamped `datetime.now(timezone.utc)`.
- Net effect: the UTC↔IST **5h30m skew** shifts "today" boundaries — an evening IST session is attributed to the wrong day, and `1d`/`7d` windows either over- or under-include the most recent session depending on time of day.

**Fix direction:** stamp everything in one canonical zone (IST or UTC) at write time with explicit `tzinfo`, and do all filtering in that zone. This is a one-afternoon fix that removes a whole class of "why is today's PnL missing" reports.

---

## 2. 🟠 High — Silently incomplete numbers

### 2.2 No persistence consulted → analytics memory-wipes on restart
`AnalyticsService` reads **live runner objects only**. After any app restart (like the one this session), pre-restart history survives only as the 500-point tail. The `forward_test_trades` / `forward_test_equity` DB tables are ignored. Until §1.1's fix lands, the tab should **label restart boundaries on charts** and cap the period selector honestly (e.g. "since 2026-09-25 restart").

### 2.3 Edge-degradation detection is structurally neutered
`_detect_edge_degradation` needs **≥4 rolling windows of 10 trades (≥40 trades)** to fire — but with the 200-trade cap and per-request recomputation, the "earlier" windows drift as the tail slides. Also, the alert only fires when `earlier_avg > 0`: **a strategy that was always bad never alerts** (nothing to degrade *from*). Consider: absolute rolling-Sharpe floor alert in addition to the relative one, and pin the baseline window to persisted history.

### 2.4 Portfolio equity curve joins are fragile
`_build_portfolio_equity_curve` sums runner equity **per date string** via a points-map. If a runner starts mid-period, its missing earlier dates produce a **level jump** in the portfolio curve; the PnL baseline is the sum of **allocated capital**, not actual start equity, so early portfolio DD% can be wrong. Also per-request it calls `runner.closed_trades` for every runner — O(runners × 200) per refresh, no caching, `AnalyticsService` re-instantiated per request.

---

## 3. 🟡 Medium — Methodology issues

### 3.1 Sharpe / Sortino math
- **Per-trade fallback Sharpe** annualizes with `sqrt(min(252, max(12, len(pnls))))` — the exponent grows with trade count, so the same return distribution yields different Sharpe at 12 vs 200 trades. Arbitrary and **inflates with activity**.
- **Daily-grouped Sharpe** uses only days *with trades*. Zero-PnL days are excluded → volatility understated for low-frequency books (1–2 trades/day), Sharpe overstated. Standard practice: mark-to-market the full calendar (or at least all trading days).
- **Risk-free rate hardcoded at 6%** — fine as a placeholder, but it should be config; today a config change means a code edit.
- **Sortino falls back to Sharpe** when there is no downside deviation. Defensible for tiny samples, but the UI should say "n/a (no losing trades)" rather than silently printing the Sharpe number under a Sortino label.
- **Monthly "Sharpe" in `_build_monthly_breakdown` is `mean/std*sqrt(n_trades)`** — not annualized, not comparable across months with different trade counts, and not a Sharpe under any convention. Mislabeled output on a rendered table.

### 3.2 Drawdown understated
Max drawdown is computed from the **trade-step** equity curve (one point per closed trade) unless `equity_history` exists — intra-trade MTM swings (the ones that trigger stop-outs and margin calls) are invisible. Given the equity history itself is decimated (§1.1), real portfolio DD can be materially larger than reported. For an options book, MTM DD is the number risk management actually cares about.

### 3.3 Calmar is period-length-dependent
`Calmar = total_return_pct / max_dd_pct` with **unannualized** return. A 30d window and a 1y window are not comparable; the same strategy shows wildly different "Calmar" depending on the selected period. Annualize the numerator (or state "return/MaxDD over window").

### 3.4 Health rating thresholds punish legitimate styles
`Sharpe ≥ 1.5 & DD ≤ 10% & WR ≥ 50%` — the **50% win-rate floor penalizes valid low-winrate/high-RR structures** (naked strangles, credit spreads: 60–80% loss-days are normal). There is also **no sample-size guard**: 3 trades can rate "healthy". Fix: replace WR with profit factor / payoff-ratio criteria, and gate the rating behind `n ≥ 30` ("insufficient sample").

### 3.5 Trade-distribution mixes units
`closed_trades` merges **equity trades and option structures**. Option PnL is per-structure with `qty` = lots and net-premium pricing; equity rows have different qty/price semantics. Histograms and `recent_trades` bins therefore mix units, and the qty column shows a meaningless "1 lot" for equity rows. Fix: tag each trade with an `instrument_class` and bucket distributions separately.

### 3.6 Streak logic inconsistency with breakeven
`current_is_win` uses `pnl >= 0` (breakeven = win), while the max-streak counters treat `pnl == 0` as neutral (resets neither). One breakeven trade can therefore make current-win-streak ≠ max-win-streak in confusing ways. Pick one convention (recommend: 0 = neutral everywhere) and document it.

### 3.7 Profit-factor sentinel
PF = **99.99** when there are no losing trades; the frontend maps `≥ 99 → ∞`. Works, but it's a magic number crossing an HTTP boundary. Prefer `null` + explicit frontend handling.

---

## 4. ⚪ Low — API & frontend polish

| Gap | Detail |
|---|---|
| Error handling | Frontend `fetch` failures only `console.error` — the user sees a blank pane with no toast/banner. |
| Period "1y" missing in UI | Backend supports `1y`; the dropdown only offers 7d/30d/90d/all_time. |
| Sparkline axis | Mini charts label points by index, not time — decimation makes the x-axis actively misleading. |
| No auto-refresh | No polling/SSE; numbers go stale silently on a long-open tab. |
| No pagination | Trade tables render the full 200-row cap with no paging or virtualization. |
| Broad exception → 500 | `analytics.py` wraps handlers in `except Exception → 500`; error detail is lost. Return structured 4xx/5xx with a code. |
| No caching | Service + all runner scans re-executed per request; trivially cacheable for 5–10s. |

---

## 5. What's already good (keep)

- Metric set is the right *shape*: Sharpe/Sortino/Calmar/PF/DD/streaks/rolling windows/degradation detection is a professional v1 checklist.
- Chart.js instances are properly destroyed on re-render (no leak).
- Period + mode filtering exists on both overview and detail endpoints.
- Health rating is a genuinely useful at-a-glance device (once thresholds are fixed).

---

## 6. Test coverage gap

`tests/test_api_analytics.py` (11 tests) exercises **endpoints only**. There are **no unit tests for the math**: Sharpe annualization, Sortino fallback, DD computation, streak conventions, monthly breakdown, degradation detector. Given §3, every metric function should get golden-number tests with synthetic trade streams (e.g. constant +₹100/day → known Sharpe; known sequence → known max DD/streaks). This is cheap and prevents silent regressions when §8's fixes land.

---

## 7. Recommended fix order

| # | Fix | Severity | Effort |
|---|---|---|---|
| 1 | Escape `innerHTML` interpolations (XSS) | 🔴 | XS |
| 2 | Canonical timezone stamping (IST/UTC) | 🔴 | S |
| 3 | Trade/equity append-only log (DB) + analytics reads from it | 🔴→🟠 | M |
| 4 | Unit tests for all metric math (golden numbers) | 🟡 | S |
| 5 | Fix monthly "Sharpe" label/math; Sortino "n/a" state; annualize Calmar | 🟡 | S |
| 6 | Health rating: payoff-ratio instead of WR + `n≥30` gate | 🟡 | XS |
| 7 | Portfolio curve: carry-forward missing dates; baseline = actual start equity | 🟠 | S |
| 8 | Risk-free rate → config; PF → `null` sentinel; streak convention | 🟡 | XS |
| 9 | Frontend: error toasts, 1y option, time-axis sparklines, auto-refresh | ⚪ | S |
| 10 | Instrument-class tagging in trade log; split distributions | 🟡 | M |

---

*Review generated 2026-09-25. Line numbers refer to branch `arena/01a0c511-back-test` @ `23b757c`.*
