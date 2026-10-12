# Cost & Risk Settings Panel — Architecture (v2, architect-certified)

> **Status:** CERTIFIED 2026-09-28 — verdict **MUST BUILD (8/10 priority)**.
> v1 draft below is retained for history; the certified decisions are in §0.

## §0 Certified decisions (v2)

1. **Name:** "Cost & Risk Settings" (not "Broker & Capital").
2. **Risk limits belong to SEGMENTS, not brokers.** A segment = capital allocation
   + mandate + broker (execution venue) + mode (paper|live) + risk_limits. The
   panel edits cost models + segment risk limits; it does NOT duplicate capital
   allocation (segments own `allocated_capital`).
3. **Cost policy per run type uses EXPLICIT broker names**, never "active":
   backtest default `zero` (determinism > realism for history), paper default
   `broker` (realism; closes the equity-zero/options-mstock inconsistency).
   Per-backtest override allowed (gross vs net comparison).
4. **Two-tier live safety:** global kill-switch (panel, default OFF, "pause ALL
   live NOW") AND per-segment `mode`. Live runs require: global ON AND segment
   mode=live AND segment daily-loss limit set (fail closed).
5. **Persistence: DB-first, YAML bootstrap/export.** Structured tables
   (`broker_profiles`, `broker_profile_audit`), not key-value. Startup: load
   YAML → populate DB if empty; runtime edits go to DB; export DB → YAML for
   version control / disaster recovery.
6. **Contract-note validation:** advisory for paper (warning banner),
   MANDATORY hard gate for live (unvalidated fee model refuses live arming).
7. **Fee reconciliation report** (added by architect): monthly
   platform-calculated vs broker-charged fees per component, tolerance flag
   (<1% OK).
8. **One panel** (Option A), split into two if crowded later.
9. **Broker connection management also lives here** (owner request): session
   status, selection, re-auth entry point — one place for all broker setup.

### Certified build order

* **Phase 1 (3d) — Core cost models:** `broker_profiles` + audit tables; cost
  model editor UI; **contract-note validator FIRST**; active broker selector.
* **Phase 2 (2d) — Risk & capital:** per-segment risk limits editor; global
  live kill-switch; paper capital per segment.
* **Phase 3 (1d) — Integration:** cost policy per run type; audit log UI;
  YAML export; fee reconciliation report.

---

# v1 DRAFT (historical) — Broker & Capital Settings Panel (UI)

> **Status:** DRAFT for architect certification. Not implemented — no code changed for this.
> **Origin:** 2026-09-28 session that (a) added the R-E1 cost-haircut CI sweep and
> (b) exposed the cost inconsistency between the options paper path (mstock fees wired)
> and the equity paper/backtest paths (zero-cost `free_executor`).
> **Goal:** manage broker cost models, per-broker capital and risk limits from a UI
> panel instead of hand-editing `config/*.yaml` — for paper AND live.

---

## 1. Problem statement

Today the settings that decide what a run *costs* and how much *capital/risk* it gets
live in files that must be edited by hand:

| Setting | File today | Who reads it |
|---|---|---|
| Broker cost model (brokerage + statutory stack) | `config/brokers.yaml` | `load_broker_profile()`, options paper broker |
| Active broker | `active_broker:` in brokers.yaml | `CommissionCalculator.from_config()` |
| Execution realism / slippage | `config/execution.yaml` | `load_execution_config()` |
| Per-bucket risk limits | `simulator/bucket_risk.py` (code!) + `risk.buckets` in forward_testing.yaml | forward engine, PortfolioManager |
| Paper capital | hardcoded (e.g. `OptionPaperBroker(capital=1_000_000)`) | options paper path |
| Live risk config | `config/risk.yaml` profiles | standalone RiskManager path |

Consequences: the UI can spawn runs but cannot choose what they cost; the paper-bucket
defaults live in Python source; changing a broker's STT requires a repo edit + restart.

## 2. Proposed panel — one tab, five sections

New tab: **Settings → Brokers & Capital**. Server-persisted (DB settings table),
hot-applied where safe, restart-flagged where not.

### 2.1 Brokers (cost models)

* List of broker profiles: built-in presets (zerodha, mstock, upstox, ibkr, …) shown
  read-only, plus **user-defined overrides** editable in the UI.
* Per-broker editable fields (mirror `BrokerProfile` / `IndiaEquityFees`):
  * Commission model: `percentage (rate, max/min)` | `flat per order` | `per share` |
    `options flat per order` | `zero` — separate overrides for **delivery** and
    **options** segments.
  * Statutory stack: STT (delivery/intraday/options), exchange txn, SEBI, IPFT,
    stamp duty, GST rate, DP charges — all shown with their FY label and a
    "verify against contract note" warning banner (rates change mid-year).
* **Contract-note validator** (already exists as
  `CommissionCalculator.validate_against_contract_note`): paste the component amounts
  from a real contract note + trade value; the panel shows pass/fail per component and
  stamps the profile "validated on <date> vs note <id>". This is the killer feature —
  it turns fee config from guesswork into a reconciled audit trail.
* **Active broker** selector (replaces `active_broker:` in yaml).

### 2.2 Cost policy per run type

Which executor/cost model each run type uses — this is the switch the R-E1 work added:

| Run type | Cost mode options |
|---|---|
| Backtest | `zero (deterministic)` — default, historical behaviour · `broker: <active>` → `costed_executor` |
| Paper (equity) | `zero` (today) or `broker: <active>` — closes the options-vs-equity paper inconsistency |
| Paper (options) | already mstock-costed; keep, but make broker visible/choosable instead of hardcoded |
| Walk-forward | `zero` or `broker` |
| **Live** | read-only display: real broker charges apply; the ledger records actual fills |

Recommended default: **backtest = zero, paper = broker** (gross vs net visible side by
side), flagged for architect decision.

### 2.3 Capital & risk per broker (paper + live)

Per broker × bucket (paper / forward / live) card:

* **Paper capital**: starting capital for spawns on this broker (replaces the hardcoded
  ₹1,000,000 in the options paper path and per-spawn capital entry).
* **Daily loss limit %** — the "daily stoploss" breaker.
* **Max drawdown %**, **weekly/monthly loss limits**.
* **Max open positions**, **max position value / % of equity**, **max gross exposure
  (leverage cap)**.
* **Order-level guards**: min/max order value, max % of daily volume.
* **Circuit-breaker behaviour**: halt-all vs pause-new-entries, auto-resume reset time.
* Clear visual separation: **paper limits are generous by design; live limits fail
  closed** — the panel must refuse to save a live profile with no daily-loss limit set
  (mirrors the engine's "live bucket requires configured sizer, fail closed" rule).

### 2.4 Live-session settings

* Broker connection (mstock/dhan): session status display, re-auth reminder — no
  credentials stored in the panel itself; link to the existing broker-auth flow.
* **Live trading master switch** per broker (default OFF): enabling requires a
  confirmation dialog + shows the current risk profile summary ("you are about to arm
  live trading with: daily loss 2%, max positions 5, …").
* Fill source reminder: live runs route through `BrokerFillProvider` — real fills,
  real charges; panel shows the last reconciliation of simulated vs actual fees.
* Optional: trading-hours / square-off time overrides (defaults from
  `config/execution.yaml`).

### 2.5 Slippage & execution realism (advanced)

* Per run type: `disabled` (deterministic, R-E1 baseline) | `hybrid` | `fixed bps`.
* Session times, participation cap (`max_participation`), latency band for reporting.
* Kept in an "Advanced" collapsible — easy to get wrong, low frequency of change.

## 3. Persistence & application semantics

* New DB `settings` storage (key-value or a `broker_profiles` table) — the panel writes
  there; `load_broker_profile()` gains a DB-backed first layer with yaml as fallback.
  Yaml files remain the bootstrap/audit source; the panel is the editor.
* **Hot-apply vs restart:**
  * Hot: active broker, cost mode for new spawns, paper capital for new spawns,
    risk limits (the forward engine already re-reads bucket limits per run).
  * Restart-flagged: statutory rate edits (calculator instances are built at runner
    construction) — panel shows "applies to runs started after save".
* Already-running runners are NEVER mutated mid-run (determinism + audit); the panel
  states this explicitly next to the save button.
* Every change: audit-log entry (who/when/old→new) — the app already has an audit-log
  pattern in PortfolioManager to follow.

## 4. Backend touchpoints (for sizing, not yet agreed)

* `simulator/fees.py` — `load_broker_profile` gains DB layer; no calculator changes.
* `simulator/execution.py` — `costed_executor` (exists) used by spawn path when cost
  mode = broker.
* `api/portfolio.py` / `forward.py` — spawn endpoints accept `broker` + `cost_mode`
  (run_backtest already accepts `broker`).
* `web/options_api.py` — replace hardcoded `CommissionCalculator.for_broker("mstock")`
  with the panel-driven profile.
* New: `api/settings.py` (GET/PUT profiles, POST validate-against-note), one Flask
  blueprint + JS panel component following `risk_page.js` structure.

## 5. Explicitly out of scope (v1)

* Editing built-in presets in place (overrides only, preset stays read-only).
* Multi-currency handling (INR-only equity/FO today; IBKR USD preset display-only).
* Auto-fetching statutory rates from any source — paste-from-contract-note only.
* Per-strategy cost overrides (per-broker/per-bucket only).

## 6. Open questions for the architect

1. Default cost mode for equity **paper**: zero (status quo) or broker-costed?
   (Recommendation: broker-costed, since options paper already is — consistency.)
2. Should risk limits stay per-bucket (current model) or become per-broker-per-bucket?
3. DB settings layer vs continuing with yaml + panel-as-yaml-editor: architect's call
   on persistence strategy.
4. Should the live master switch also gate the *spawn form* for live buckets, or only
   the engine start path?
5. Contract-note validation: mandatory before activating a broker for live, or advisory?

---

*Draft ends. Nothing in this document is implemented; produced for review.*
