# Portfolio Intelligence & Monitoring (PRD P0 §1.1–1.3)

> Status 2026-09-26: **§1.1 Greeks dashboard, §1.2 concentration & correlation,
> §1.3 market regime** are implemented. §1.4 liquidity monitor, §1.5 cross-strategy
> conflict detector, the DB schema and the alert rules engine will come later.

Each runner has its own limits (sizing, max drawdown, the daily-loss breaker).
None of them sees the **combined** book, so this layer watches four things:

| Question | Section | Where |
|---|---|---|
| If NIFTY moves 2%, or IV jumps 5 points, what does the whole book lose? | Greeks & scenarios | `monitoring/greeks.py` |
| Are three "different" strategies really one NIFTY bet? | Concentration | `monitoring/concentration.py` |
| Do the strategies lose on the same days? | Correlation | `monitoring/correlation.py` |
| Is the market in the regime my strategies were built for? | Regime | `monitoring/regime.py` |

## Using it

* **UI**: `/monitor` (nav: 🧠 Monitor). `/portfolio/greeks` is the PRD alias.
  - Tabs: *Greeks & scenarios*, *Concentration*, *Correlation*, *Market regime*.
    `#greeks`, `#regime`, etc. deep-link to a tab.
  - The alert list sits on top on every tab and polls every 2 s (choose 1 s,
    5 s or Paused). Polling stops while the tab is hidden.
  - The scope picker offers All / Paper / Live.
* **Server-side sweep**: alerts are raised even when no browser is open.
  - The portfolio manager runs a full evaluation every `sweep_every_ticks`
    feed ticks (default 5) and writes each alert **transition** to the audit
    log with `scope="monitor"`. Filter the Risk page audit timeline by
    *Monitor* to see them.
  - The sweep never fetches external data.
  - After 5 consecutive failures the sweep switches itself off (logged). The
    feed and the breakers are unaffected either way.
* **API** (all GET unless noted; `?mode=paper|live`, omitted = all buckets):

| Endpoint | Returns |
|---|---|
| `/api/monitor/snapshot` | every section + active alerts (what the page polls) |
| `/api/monitor/greeks` · `/concentration` · `/correlation` · `/regime` | one section (+ its alerts) |
| `/api/monitor/regime?symbol=BANKNIFTY` | regime for a specific underlying |
| `/api/monitor/alerts` | active alerts, counts, last sweep; `?history=1&limit=N` adds resolved history |
| `POST /api/monitor/alerts/<id>/ack` | acknowledge (404 if unknown) |
| `/api/monitor/config` | the effective limits |

An invalid `mode` returns 400. Snapshots are cached for `cache_ttl_s`
(1 s), so several open tabs don't multiply the work.

## What's in the book

`collector.py` is the only module that reads the command center:

* **Runner equity positions** are marked at the runner's last price.
* **Runner option legs** come from each runner's `OptionsBridge` book.
* **The manual options book** (dashboard) is included for the *All* and
  *Paper* scopes.

**Implied vol per leg**, first match wins. The source is reported for every
leg as `iv_source`:
1. `contract`: the chain contract's own vol.
2. `implied`: inverted from the leg's current premium (accepted range 2%–300%).
3. `default`: `default_iv` (15%). This raises a "Greeks on assumed
   volatility" alert, because those numbers are approximate.

**Spot per leg**, first match wins:
1. The bridge's last priced spot.
2. The runner's last price.
3. The price any other runner sees for that underlying.
4. As a last resort, one shared estimate per underlying (the mean strike).

All legs of an underlying share one spot. (An earlier "spot = the leg's own
strike" fallback made a bull call spread read as delta-short.)

**DTE** runs to 15:30 IST on the expiry date, on the book's own bar clock.

## §1.1 Greeks: money units, not raw Greeks

Raw Greeks can't be added across underlyings: 1 NIFTY delta ≠ 1 RELIANCE
share. And "gamma −1,000" means very different things on a ₹5L book and a
₹5Cr book. So every total is **₹ P&L for a defined move**:

| Card | Definition |
|---|---|
| Delta · per 1% | Σ Δ·units·S·1% |
| Gamma · ±2% | convexity P&L on a 2% move, on top of delta: Σ ½·Γ·units·(S·2%)² |
| Vega · per vol pt | Σ vega·units (₹ per 1 IV point) |
| Theta · per day | Σ θ·units (₹ per calendar day) |
| Net premium | Σ premium received − paid (credit > 0) |
| Margin used | option-broker margin + equity notional, vs allocated capital |

**Scenarios** use full Black-Scholes revaluation, not a Taylor estimate:
- spot ±0.5/1/2/3%;
- IV ±2/5 points;
- one day of decay;
- combined stresses (e.g. *Gap down: −3% & IV +8*).

The Δ-Γ estimate is shown next to the full revaluation so you can see where
convexity dominates. Every underlying gets the same % move (β = 1, stated in
the payload).

**Limits** are fractions of equity (`config/monitoring.yaml → greek_limits`):

| Limit | Warning | Critical | Fires on |
|---|---|---|---|
| `delta_1pct` | 1% | 2% | \|delta P&L per 1%\| |
| `gamma_2pct` | 0.5% | 1% | convexity **loss** on ±2% (short gamma only) |
| `vega_1pt` | 0.75% | 1.5% | \|vega\| |
| `theta_day` | 0.3% | 0.6% | decay **paid** per day (long premium only) |
| `worst_loss` | 3% | 5% | worst scenario loss. Also **critical** whenever it exceeds the headroom left before the daily-loss breaker |

> **Mapping to the PRD's absolute limits.** The PRD example (delta ±500/1000,
> gamma −800/−1200, vega 3000/5000, theta −15k/−25k) assumes one book size
> and one underlying. The ratio form sets the same kind of limit in a
> scale-free way. Worked example (also in the YAML): a ₹5L book short 150
> units of an ATM NIFTY straddle, 7 DTE, IV 15%, loses ≈ ₹28.7k from
> convexity on a 2% move. That's 5.7% of equity, so gamma is **critical**
> (hand-checked Black-Scholes: Γ = 0.000767, and the test suite pins it).
> For fixed-₹ limits, set the ratio to `limit / equity`.

**Recommendations are specific**:
- Delta alerts name the hedge, e.g. "Sell ~3.2 NIFTY futures lots". Sub-lot
  residue isn't offered as a hedge.
- Gamma alerts name the structure that contributes the most short gamma, and
  the share of it you'd remove by closing it.

## §1.2 Concentration

* **Exposure by underlying**: share of gross notional. An option's notional is
  the underlying value (units × spot), not the premium. Warning at >50%,
  critical at >70%.
* **Correlated groups**: `INDIA_INDEX` = NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY,
  SENSEX (configurable). Warning 70%, critical 85%.
* **Gating**: percentage alerts need ≥ 2 distinct exposures held by ≥ 2
  strategies. A single bull call spread is 100% NIFTY by design, and that isn't
  *hidden* concentration. A book of only Indian indices *will* trip the group
  alert, which is correct: they move together.
* **Strategy stacking**: ≥ 3 strategies on one underlying raises a warning
  that names who leans which way.
* **Strike clustering**: ≥ 3 legs at one underlying/strike/expiry raises a
  liquidity warning, because exiting together competes for the same book.
* Also reported: Herfindahl index, effective number of underlyings, gross
  leverage.

## §1.2 Correlation

* Pearson correlation on **₹ P&L changes** (not equity levels, which trend).
  Lookback is 300 observations; pairs with fewer than 20 overlapping
  observations are marked insufficient.
* **Primary input**: a same-instant equity sample of every runner, taken on
  every feed tick (`record_tick`). These samples are aligned by
  construction. Runner equity curves stamp wall-clock time, which doesn't
  line up in fast replay, so they're only a fallback (snapped to a common
  grid, tz-aware). The source is reported as `series_source`.
* Alerts:
  - correlation > 0.70 → warning, > 0.85 → critical;
  - correlation ≤ −0.70 → **info** ("offsetting pair"), because a hedge isn't
    a danger. That's why the test is signed, not `abs()`;
  - with ≥ 3 strategies, the effective number of independent bets (DR²,
    where DR = diversification ratio) is checked too.
* Flat strategies (no position → zero variance) show "·" and are left out of
  the diversification maths.

## §1.3 Market regime

* **Vol index**, first available wins. The source is always reported:
  1. a `vix` series (e.g. INDIAVIX from the DB);
  2. the mean IV of the book's legs on that underlying;
  3. realized vol as a labelled proxy.
* **Regime** thresholds (vol points):
  - low: index < 15 **and** realized < 12;
  - high: index > 25 **or** realized > 20;
  - otherwise moderate.
  Either measure can put the book in high vol, and the cards show both.
* **Realized vol** is annualised from the actual bar spacing: 252 for daily
  bars, 252 × 375 for 1-minute bars.

> The synthetic feed moves up to ±0.6% per 1-minute bar. That annualises to
> 100%+ realized vol, so synthetic runs usually read **High volatility**.
> That's correct maths on unrealistic data.

* **Transition**: a >20% change over 5 bars, always measured **within one
  series**. Option IV has no history, so its transition is judged on realized
  vol. (Comparing today's IV with yesterday's realized vol reads a structural
  IV/RV gap as a "−90% collapse".)
* **Range expansion**: the latest bar's range is > 1.5× the recent average.
* **Strategy regime fit** (optimal / acceptable / mismatch / unprofiled):
  - A profile in `strategy_profiles` wins. It matches the runner name,
    strategy name or structure type, case-insensitive.
  - Otherwise the profile is **inferred from the open legs**: all short means
    premium seller, all long means premium buyer.
  - If legs don't settle it, the structure name decides. The repo's bare
    `strangle` is the credit strangle (`ShortStrangle`); debit spreads are
    directional.
  - Last, strategy-name keywords decide (breakout/momentum means trend,
    rsi/reversion means mean-reversion).
  - A premium seller that's mismatched in a high-vol regime is **critical**;
    other mismatches are warnings.

## Alert lifecycle (`monitoring/alerts.py`)

* The analytics re-derive their findings from scratch on every evaluation. The
  alert book turns that stream into **transitions**:
  - *raised*;
  - *escalated*: only above the alert's peak severity for its current
    lifetime, so a metric hovering on a threshold doesn't page repeatedly;
  - *resolved*.
* **Hysteresis**: an alert resolves only after 3 consecutive evaluations
  without it. Until then it shows as *clearing*.
* **Acknowledgement** sticks until the alert escalates past its peak.
* Books are per scope: a Paper pass never resolves a Live alert.
* Info findings stay in the UI only. They aren't audited and can't be
  acknowledged.

## Performance

Measured with 20 runners:
- a full evaluation takes 8–15 ms (correlation is vectorised);
- a tick takes ~34 ms on its own;
- `record_tick` adds < 1 ms per tick;
- the sweep costs ~2–4 ms per tick, averaged at the default cadence.

## Configuration

Everything lives in `config/monitoring.yaml`. Override the path with
`MONITORING_CONFIG_PATH`. Every key is optional. The app's `--currency`
flag also sets the currency symbol in alert text.

## Tests

* `tests/monitoring/`: Greeks (hand Black-Scholes values), scenarios, limits,
  concentration gating, signed correlation incl. tz and µs alignment, regime
  classification and transition regressions, alert lifecycle, a real
  `PortfolioManager` integration (sweep cadence, failure budget, tick sampling,
  shared-spot regression, audit), and the API.
* `tests/js/test_monitor_page.mjs`: the page's pure render functions
  (ordering, ack control, signs, heatmap, escaping of runner names).
