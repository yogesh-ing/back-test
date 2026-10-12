# Portfolio Intelligence

Portfolio-wide risk analytics and alerts across **every** open position (paper
runners, live runners, the manual options book): aggregated Greeks, scenario
P&L, concentration, strategy correlation, market regime and option-chain
activity.

> **Information only.** The platform calculates, informs and broadcasts.
> Strategies decide. Nothing in this layer closes, resizes or blocks a
> position. A strategy that subscribes to an alert may pause *its own* entries
> or *request* its own exit, which its runner executes through the normal
> engine path (see [STRATEGY-ALERTS.md](STRATEGY-ALERTS.md)).

Related: [ALERTS-GUIDE.md](ALERTS-GUIDE.md) (what each alert means and how
people usually respond), [PORTFOLIO-CENTER.md](PORTFOLIO-CENTER.md) (where it
appears in the UI).

---

## Where to find it

* **Risk Board tab** of the Portfolio Command Center (`/portfolio`,
  `/portfolio/paper`, `/portfolio/live`). Five collapsible sections are added
  above the existing per-runner risk table; open/closed state is remembered
  per section in `localStorage` (`pi.section.<name>`):
  1. **Portfolio Greeks** (`#pi-greeks`) — net Δ / Γ / Vega / Θ cards with
     long/short bias, scenario P&L (NIFTY ±2%, IV ±5 pts, crash) and a
     per-strategy breakdown table. Refreshes every **1 s** while the tab is
     visible.
  2. **Concentration** (`#pi-concentration`) — exposure share per underlying
     (bar per underlying, flagged above the threshold) and strike clusters.
  3. **Correlation** (`#pi-correlation`) — heatmap of pairwise P&L correlation
     between runners (or between strategies), recomputed every 5 minutes.
  4. **Market Regime** (`#pi-regime`) — current VIX band, 24 h change,
     transition badge, regime history chart and a strategy-fit table.
  5. **Market Activity** (`#pi-activity`, collapsed by default) — OI anomalies
     and liquidity dry-ups from option-chain snapshots.
* **Alert widget** — bottom-right on every page. See
  [ALERTS-GUIDE.md](ALERTS-GUIDE.md#the-alert-widget).

The *All buckets* page aggregates paper + live; the paper/live pages scope the
Greeks, concentration and correlation to that bucket (`?mode=paper|live`).
Alerts are always portfolio-wide.

---

## Architecture

```
 feed bars ─► PortfolioManager._on_bar ─► runners (unchanged)
                  │                         │
                  └─► IntelligenceService.on_bar   (regime inputs, feed receipt time)
 tick end  ─► IntelligenceService.on_tick_end      (correlation sample, snapshot invalidation)

 evaluator thread (every --alert-refresh-interval s)
   collect_legs()  ── runner books + option bridges + manual options book
        │               (manager lock briefly, then each runner lock in turn)
        ▼
   PortfolioGreeksAggregator · ConcentrationMonitor · CorrelationCalculator
   MarketRegimeDetector · MarketActivityMonitor
        │
        ▼ rules (thresholds from config/portfolio_intelligence.yaml)
   AlertBroker ──► subscribers: Strategy.on_alert / on_alert_resolved
        │     ├──► listeners: IntelligencePersister (async queue → DB)
        │     └──► version counter → alert widget / SSE
        ▼
   REST: /api/portfolio/*, /api/market/*, /api/alerts/*
```

| Module | Role |
|---|---|
| `backtest/alerts/types.py` | `AlertType`, `Severity`, `Alert` dataclass, event-vs-condition classification |
| `backtest/alerts/broker.py` | `AlertBroker` pub/sub + lifecycle, `alert_broker()` singleton |
| `backtest/alerts/alert_broker.py` | PRD import path (re-exports the broker) |
| `backtest/alerts/catalog.py` | Per-type copy: title, "what this means", typical responses, UI section |
| `backtest/intelligence/collectors.py` | Turns runner books / bridges / manual book into `ExposureLeg`s |
| `backtest/intelligence/greeks.py` | Black-Scholes aggregation + full-revaluation scenarios |
| `backtest/intelligence/concentration.py` | Exposure by underlying, strike clustering |
| `backtest/intelligence/correlation.py` | Rolling Pearson correlation of runner equity changes |
| `backtest/intelligence/regime.py` | VIX bands with hysteresis, realized-vol proxy, regime fit |
| `backtest/intelligence/market_activity.py` | OI-change spikes, bid-ask spread dry-ups |
| `backtest/intelligence/service.py` | `IntelligenceService` — owns the above, runs rules, feeds the broker |
| `backtest/intelligence/persistence.py` | `IntelligencePersister` — async writes to the three tables |
| `backtest/api/intelligence.py` | Flask blueprint for every endpoint below |
| `web/static/js/components/alert_widget.js` | Global widget + detail modal |
| `web/static/js/components/portfolio_intelligence.js` | Risk Board sections |

**Concurrency.** Evaluation and publishing run outside the manager lock;
subscriber callbacks run outside the broker lock (each under its own runner's
lock); a failing callback is logged and counted, never propagated, and never
stops other subscribers. Database writes are queued to a background thread, so
a slow database never slows the feed.

**Fail-soft.** Any exception inside the intelligence layer is logged and
swallowed — it can degrade analytics, never trading.

---

## Calculations and units

All Greeks use the same `BlackScholes` model the option books price with,
with each leg's own implied volatility when known (default 15%, flagged in
`warnings` as "IV unknown").

| Figure | Unit | Meaning |
|---|---|---|
| `net_delta` | share-equivalents | Σ sign · Δ · units. ₹ P&L per 1-point move. One NIFTY futures lot = 75. |
| `net_gamma` | Δ change per **1% move** | Σ sign · Γ · units · spot · 1%. Normalised so NIFTY, BANKNIFTY and stocks are comparable. |
| `net_vega` | ₹ per +1 IV point | |
| `net_theta` | ₹ per calendar day | positive = earning time decay |

* **Scenarios** are full revaluations (every leg repriced at the shocked spot
  and vol), not Taylor approximations, so short-gamma convexity shows up.
  Every underlying is shocked by the same percentage.
* **Expiry day.** A leg expiring today is priced with half a trading day left
  — at T = 0 Black-Scholes gamma collapses to zero and would hide exactly the
  expiry-day risk a short straddle carries.
* **Unpriceable legs** (no spot, no expiry) are excluded from totals and listed
  under `missing_greeks`; the UI shows a warning rather than a silent zero.
* **Per-strategy rows** are per runner instance (two runners of the same
  strategy are two rows); `gamma_share` / `delta_share` are each row's share of
  the absolute total and drive "contributing strategies" in alerts.
* **Option side** is derived from the leg's symbol suffix (`…CE` / `…PE`)
  when the book records a combined type, so strangles price correctly.

**Concentration** uses gross notional (|units| × spot) per underlying.
`concentration_high` needs at least `concentration_min_positions` positions —
a one-position book is trivially "100% concentrated" and is not flagged.
**Strike clustering** counts distinct positions (structures) at one
underlying/strike/type.

**Correlation** samples every runner's equity once per feed tick, correlates
the tick-to-tick *changes* (Pearson) over the last `correlation_window`
aligned samples, and needs `correlation_min_samples` before scoring a pair
(cells show "–" until then). The matrix is cached for 5 minutes;
`?refresh=1` forces a recompute.

**Regime** input priority, always reported honestly in `source`:
1. an explicit VIX print — `POST /api/market/vix` or bars for a symbol in
   `vix_symbols` (`manual`, `feed:INDIAVIX`, …), valid for `vix_stale_s`;
2. annualised realized volatility of the `regime_benchmark` bars
   (`realized_vol_proxy:NIFTY`, `is_proxy: true` — the UI labels it
   "realized-vol proxy", not VIX);
3. otherwise `unknown`.

Bands: `< regime_low_max` → `low_vol`, `≥ regime_high_min` → `high_vol`,
otherwise `moderate_vol`. A change must clear a band edge by
`regime_hysteresis` points to count, so VIX hovering at 22.0 does not flap.
The first reading after `unknown` sets the regime without raising a
"change". Strategy fit compares the current value against
`Strategy.regime_vix_range` (e.g. `(10, 15)` → "Optimized for VIX 10–15").

**Market activity** reads option-chain rows (`strike`, `option_type`, `oi`,
`bid`, `ask`) from the chain snapshot recorder, or from
`POST /api/market/chain-activity`. An OI anomaly is a |ΔOI| larger than
`oi_spike_multiplier` × that strike's rolling mean |ΔOI| (after
`oi_min_history` snapshots); a liquidity dry-up is a spread wider than
`spread_multiplier` × its rolling average. Synthetic chains carry no OI, and
the section says so ("no OI data") instead of showing zeros.

---

## Configuration

`config/portfolio_intelligence.yaml` (path overridable with
`PORTFOLIO_INTELLIGENCE_CONFIG`). Precedence: defaults ← YAML ← `PI_<KEY>`
environment variables ← explicit overrides. The file documents every key;
the important ones:

| Key | Default | PRD |
|---|---|---|
| `gamma_critical` | `-150` | "net γ < −1000" — see note |
| `delta_warning_abs` | `800` | \|Δ\| > 800 |
| `concentration_max_pct` | `0.60` | > 60% in one underlying |
| `strike_cluster_min` | `3` | 3+ positions at one strike |
| `correlation_warning` | `0.80` | > 0.8 |
| `oi_spike_multiplier` | `3.0` | > 3× normal |
| `spread_multiplier` | `2.0` | spread > 2× average |
| `feed_stale_s` | `120` | last bar > 2 min |
| `ignore_ttl_s` | `3600` | auto-dismiss after 1 h |
| `greeks_cache_s` / `regime_sample_s` / `correlation_cache_s` | 1 / 300 / 300 | 1 s / 5 min / 5 min |

> **Why `gamma_critical` is −150, not −1000.** The PRD's −1000 was
> illustrative. In this platform's units (Δ change per 1% move) one short ATM
> NIFTY strangle lot is about −16 and a straddle lot near expiry about −40, so
> −1000 would need ~60 short straddle lots before ever firing. −150 ≈ a few
> lots of expiry-week short gamma. Tune it to your book.

### Command-line flags

```bash
python -m backtest.web.app                                   # intelligence ON (default)
python -m backtest.web.app --alert-refresh-interval 2        # evaluate rules every 2 s
python -m backtest.web.app --disable-portfolio-intelligence  # off: no evaluator, no widget, API → 503
```

`--enable-portfolio-intelligence` exists for symmetry (env
`PORTFOLIO_INTELLIGENCE=0` disables); `ALERT_REFRESH_INTERVAL` sets the
interval. When disabled, the hooks are no-ops, every intelligence endpoint
returns **503**, and the widget script is not included in pages.

---

## Persistence

When a database is configured (`config/database.yaml` /
`FORWARD_TEST_DB_URL`), the app attaches an `IntelligencePersister`:

| Table | Written |
|---|---|
| `alerts` | every alert create / update / dismiss / review / resolve (upsert by `alert_id`) |
| `portfolio_greeks_history` | Greeks + concentration snapshot every `greeks_history_s` (60 s) |
| `market_regime_history` | every regime sample (5 min) and every transition |

Migration: `db/migrations/005_portfolio_intelligence.sql` (PostgreSQL, JSONB),
`005_portfolio_intelligence.sqlite.sql`, and Alembic revision
`20260926_1200_005_portfolio_intelligence`. On SQLite the persister creates
the tables itself if missing. Without a database everything still works;
alert history is then in-memory (last 2,000 alerts) and
`/api/alerts/history` reports `"source": "memory"`.

---

## API

All endpoints return JSON with `success`; they return **503** when
intelligence is disabled. `mode` is `paper`, `live` or omitted (all).

| Method | Path | Notes |
|---|---|---|
| GET | `/api/portfolio/greeks?mode=` | net Greeks, bias, scenarios, per-strategy breakdown, warnings |
| GET | `/api/portfolio/concentration?mode=` | `by_underlying`, `by_strike`, clusters |
| GET | `/api/portfolio/correlation?mode=&group_by=runner\|strategy&refresh=1` | `ids`, `labels`, `values` matrix, `alerts` |
| GET | `/api/portfolio/intelligence?mode=` | everything above + regime, market activity, alerts, thresholds (one call for the Risk Board) |
| GET | `/api/market/regime` | regime, VIX/proxy value, source, transition, history, strategy fit |
| POST | `/api/market/vix` | `{"value": 18.4, "source": "nse"}` — feed a VIX print |
| GET | `/api/market/oi-activity?symbol=NIFTY` | OI anomalies + liquidity events |
| POST | `/api/market/chain-activity` | `{"underlying": "NIFTY", "rows": [{strike, option_type, oi, bid, ask}]}` |
| GET | `/api/alerts/active?include_dismissed=1` | open alerts + counts + `version` |
| GET | `/api/alerts/history?from=24h&type=&severity=` | `from`: ISO timestamp or `30m`/`24h`/`7d` |
| GET | `/api/alerts/subscriptions` | which runners subscribe to which types |
| GET | `/api/alerts/<id>` | full detail incl. what-it-means, typical responses, subscribers |
| POST | `/api/alerts/<id>/dismiss` · `/review` · `/resolve` | body `{"by": "name"}` optional |

The portfolio SSE stream (`/api/portfolio/stream`) also carries a compact
`intelligence` object (net Greeks, scenarios, per-strategy rows, alert
counts + version) on each frame.

---

## Tests

```bash
cd src
python -m pytest ../tests/alerts ../tests/intelligence ../tests/db/test_migrations_005.py -q
node ../tests/js/test_alert_widget.mjs      # also run via tests/test_web_components.py
```

* `tests/alerts/test_alert_broker.py` — lifecycle, dedup, escalation,
  auto-dismiss/expiry, resolve, failing callbacks, lock discipline.
* `tests/intelligence/test_analytics.py` — Greeks (ATM call Δ ≈ 0.5, short
  strangle Γ < 0, finite-difference gamma check, scenarios, missing IV,
  expiry day, **50 positions < 500 ms**), concentration, correlation, regime,
  market activity, config.
* `tests/intelligence/test_integration.py` — real `PortfolioManager` +
  strangle runners: gamma alert → pause → resolve → resume, exit request,
  unsubscribed runner untouched, feed-stale, regime change, API lifecycle,
  page rendering, SQLite persistence.
* `tests/js/test_alert_widget.mjs` — widget and Risk Board rendering in a
  stub DOM.
