# Plan: Upstox as co-equal data source + separate trading account, with data-source failover

**Status:** DRAFT for architect review — no implementation yet.
**Branch/worktree:** `feature/upstox-login-testing` @ `.worktrees/upstox-login`
**Scope:** Keep mStock intact. Add Upstox as a *second, co-equal* venue for (a) market data and (b) order execution, with automatic data-source failover and a human alert path so portfolio risk stays manageable if one source dies.

---

## 1. Why (the problem)

Today the live stack has a single point of failure per function:
- **Data** for live paper/live runners resolves to one broker feed (`default_broker_source()` → mStock). If that feed goes stale or the session dies, the runner has nothing to price the portfolio on, and the options path *refuses to run* rather than degrade (by design — a fake chain shows fake P&L). Either way, we lose the ability to watch and manage open risk.
- **Execution** routes to the active broker only. There is no second venue to fall back to for closing/adjusting a position.

The goal is **resilience, not replacement**: if one data source fails we switch to the next to at least keep monitoring the portfolio and fire an alert so the user can manually close/adjust. Upstox becomes that second venue — for data *and* for a separate trading account — while mStock stays fully in place.

**Non-goal (important for the architect):** this is *not* autonomous cross-venue arbitrage or auto-hedging. The failover keeps the portfolio *visible and manageable*; the human closes/adjusts. Execution automation stays within what each venue already does today.

---

## 2. What already exists (grounded in the code)

We are not starting from zero. Verified in this worktree:

| Capability | State | Where |
|---|---|---|
| Upstox as a data source | **Enabled + working.** Live quotes (`get_ohlc`) and candles (`get_candles`) verified against a live session today. | `src/backtest/data/upstox_live_feed.py`, `config/data_sources.yaml` (`upstox.enabled: true`) |
| Upstox as a broker | **Exists.** `place_order`, `cancel_order` implemented; OAuth session verified. | `src/backtest/brokers/upstox.py` |
| Broker session manager | Multi-broker, one session per broker, in-memory. `ui_active` is display-only. | `src/backtest/brokers/session_manager.py` |
| Data-source policy + tag | `synthetic|csv|mstock|dhan|db|upstox` all first-class; `upstox` mapped. | `src/backtest/data/sources_policy.py`, `source_tags.py` |
| Feed registry | Keyed `(source, symbol, timeframe)`; broker source acquires shared `LiveChainProvider`. | `src/backtest/forward/feed_registry.py` |
| **Staleness/health monitoring** | **Already exists, per broker.** Staleness, gap, stale-repeat detection + a `/api/broker/feed-quality` endpoint and a rolling `data/feed_quality.log`. This is the failover *lever*. | `src/backtest/forward/feed_quality.py` |
| Boot-time source fallback | `_FALLBACK_PREFERENCE = ("db","mstock","dhan","upstox","csv")` — but this runs **once at boot**, not live. | `src/backtest/web/app.py:resolve_source` |
| Alerts/notifier | Telegram + email + in-app, off-switches, profiles. | `config/alerts.yaml`, alert modules |

**The gap is not "add Upstox."** Upstox is already there. The gaps are:

1. **No *live* failover.** A `PaperRunner` binds `self.source` once at construction and never re-resolves. Boot-time fallback ≠ runtime failover. `self.source` is a plain attribute → swappable, but nothing swaps it.
2. **Data source and execution broker are conflated.** `default_broker_source()` ties "which feed" to "which broker." We need them independently selectable: *monitor on source X, execute on account Y*.
3. **No consolidated cross-venue risk.** Two accounts/two feeds → one combined portfolio + risk view, else we double-count or miss exposure.
4. **No failover alert.** When a source is declared dead we must notify the user to manually close/adjust.

---

## 3. How (technical blueprint)

Four decoupled components. Each is testable in isolation; none rewires the engine's bar path (`source.get_candles(...)` stays the single seam — the engine never branches on source, which is exactly what makes failover safe).

### 3.1 Source health + failover supervisor (new: `src/backtest/forward/source_supervisor.py`)
- Wraps the existing `FeedQualityMonitor` per `(source, symbol, timeframe)`.
- State machine per feed: `HEALTHY → SUSPECT → DEAD`, driven by `staleness_s`, `stale_repeat_run`, and poll errors (all already computed in `feed_quality.py`).
- Thresholds (config `config/source_failover.yaml`, defaults proposed):
  - `suspect_after_s: 30` (no fresh bar), `dead_after_s: 90`, `min_polls: 3`.
  - `recover_after_s: 60` healthy before returning to primary (avoid flapping).
- On `DEAD`, it picks the next **enabled + authenticated** source from an ordered `failover_chain` (e.g. `["mstock","upstox"]`) and **hot-swaps** the runner's `source` object. The runner keeps calling `source.get_candles(...)` — only the object behind it changes.
- Emits a `SourceFailoverEvent` (see 3.4). Hysteresis guards included so we don't thrash between two flaky feeds.

### 3.2 Independent "monitor source" vs "execute account" (config + small wiring)
- Introduce an explicit **venue profile** (config block or per-runner fields):
  ```
  venue:
    monitor_source: mstock        # data feed for pricing/watching
    execute_account: upstox       # broker that routes orders
    failover_chain: [mstock, upstox]
  ```
- Decouple `default_broker_source()` usage in the live path: data source resolved from `monitor_source`, order routing resolved from `execute_account`. Each runner/portfolio carries both.
- Reuses the existing per-broker sessions in `session_manager` — both mStock and Upstox sessions can be live simultaneously (already supported; `ui_active` is display-only).

### 3.3 Separate Upstox trading account (reuse existing broker, add account scoping)
- Upstox `place_order`/`cancel_order` already exist. Scope them to a named **account** (`upstox-live`) so positions/orders are tagged by venue account, not mixed into mStock's book.
- An **account-scoped order router**: given `execute_account`, route `place/cancel/positions` to that broker's session. Paper vs live flag stays as today (`paper_runner` mode).
- Positions/holdings pull from Upstox (`get_positions`/`get_holdings`) to reconcile the paper/live book against the real account — this is the "keep an eye on the portfolio" requirement.

### 3.4 Consolidated risk + failover alerting (new: `src/backtest/forward/risk_consolidator.py`)
- Aggregates open risk across **both** venues (mStock book + Upstox book) into one view: net exposure, per-symbol/underlying, Greeks if available, drawdown.
- On any `SourceFailoverEvent` or when a venue is `DEAD`:
  - Fire the existing notifier (Telegram/email/in-app) with a **human-actionable message**: "Data source mstock DEAD for NIFTY50 — switched to upstox for monitoring. Open risk: X. **Action: manually review/close/adjust.**"
  - The alert carries the consolidated risk snapshot so the user can decide, per the requirement ("trigger the alert to user to manually close or manually adjust").
- Guardrail: failover changes **monitoring only**. It does **not** auto-close or auto-hedge. Execution stays human-directed (or within existing per-venue automation).

### Data flow (target)
```
feeds ─▶ FeedQualityMonitor (per source,symbol,tf) ─▶ SourceSupervisor
                                                          │ DEAD? pick next from failover_chain
                                                          ▼
                                        hot-swap runner.source  ─▶ engine keeps calling get_candles()
                                                          │
                                        SourceFailoverEvent ─▶ RiskConsolidator ─▶ Notifier (Telegram/email)
orders ─▶ AccountRouter(execute_account) ─▶ upstox.place_order / mstock.place_order
positions ◀─ account-scoped get_positions (both venues) ─▶ consolidated risk view
```

---

## 4. Gotchas & risk controls (risk-first — read before approving)

1. **Stale-price execution is the real danger.** If we fail over the *data* feed but a *live* order is mid-flight, we must not route on a stale price. Control: the supervisor only swaps the monitoring feed; **order routing revalidates price freshness at submit time** and refuses (like the current options path already refuses on synthetic) rather than trading on a dead feed.
2. **Never auto-trade on a fallback feed for a venue that can't support it.** The options chain path is intentionally strict (refuses generated chains). Failover for *options* must only go broker→broker (mstock↔upstox live chains), never →synthetic. Keep that refusal intact.
3. **Flapping.** Two flaky feeds can ping-pong. Control: hysteresis + `recover_after_s` + a max-switches-per-minute cap; after N switches, latch to the surviving source and alert rather than keep switching.
4. **Double-counting risk across two accounts.** Consolidated view must key positions by `(venue_account, symbol)` and net deliberately; a symbol held on both venues is two lines, not one merged silently.
5. **Session divergence.** Both mStock and Upstox sessions expire independently (Upstox at 03:30 IST). Failover needs *both* authenticated to be a real fallback; if the backup session is also dead, the alert must say "no healthy source" explicitly, not silently do nothing.
6. **Provenance/certifiability.** Bars from a failed-over source change provenance mid-run. Tag the run's bars with the *actual* source used per bar (the `source_tag`/provenance machinery already exists) so results stay honest and certification-grade labeling is preserved.
7. **Real-money safety.** Per the quant mandate: validate on paper first. Phase the Upstox *execution* behind the paper/live flag until order margin, fees (STT, stamp), lot sizes, and fills are reconciled against Upstox's actual responses.

---

## 5. Suggested phasing (for the architect to sequence)

- **Phase 0 — Validate Upstox execution on paper.** Drive `place_order`/`cancel_order`/`get_positions` against the live Upstox session in paper mode; reconcile fills/margins. No failover yet. *De-risks gotcha #7.*
- **Phase 1 — Source supervisor + failover + alerting (monitoring only).** Wire `FeedQualityMonitor → SourceSupervisor → hot-swap → Notifier`. Prove a forced mStock outage switches to Upstox and alerts, with **no change to order routing**. *Smallest valuable slice; touches the least.*
- **Phase 2 — Decouple monitor-source from execute-account + separate Upstox account.** Venue profiles, account-scoped order router, positions pull.
- **Phase 3 — Consolidated cross-venue risk view.** Combined exposure/drawdown + the human-actionable failover alert payload.

Each phase is independently shippable and testable; Phase 1 delivers the core resilience ask on its own.

---

## 6. Open questions for the architect

1. Should failover be **per-runner** (each portfolio picks its own chain) or **global** (one deployment-wide active source)? Leaning per-runner for isolation.
2. Do we want **automatic execution failover** (route the close to the backup venue) or **monitor-only + human executes**? The stated requirement reads monitor-only + alert; confirm.
3. Is a **shared `LiveChainProvider` across brokers** acceptable, or must each venue keep a fully independent provider to avoid cross-contamination on failover?
4. Config surface: a single `venue` block per deployment, or per-strategy/per-instance overrides?
5. Backout plan if a phase regresses: is a feature flag on `SourceSupervisor` (fail-open to today's single-source behavior) the right safety net?

---

*Prepared for review. No code changed yet beyond the worktree's existing Upstox login work. Next step after approval: implement Phase 0/1 behind a feature flag.*
