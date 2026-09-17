# Unified Trading Architecture — Consultant Sign-off Edition

**Status:** ✅ Signed off (with conditions) · **Date:** 2026-09-16 · **Base commit:** d206881 · **Implementation branch:** arena/01a0a901-back-test

**Sources:** user 3-point redesign analysis · `docs/consultant quest review.md` (dev prototype) · this document = final architecture after consultant answers.

**Task PRD:** [`docs/UNIFIED-TRADING-TASKS.md`](UNIFIED-TRADING-TASKS.md) — decomposed tasks, hours, tests, acceptance criteria.

---

## 0. Consultant Sign-off Summary

**Verdict: proceed.** No blocking conflicts. D1 (declarative entity over script) and D2 (soft-deprecate over hard delete) are ratified as the *better* reading of the requirements, not just a safe one.

**Conditions (must clear before merge):**

| # | Condition | Effort |
|---|---|---|
| C1 | `Playbook` mutable defaults fixed: `tags: list = field(default_factory=list)`, `exit_config: dict = field(default_factory=lambda: {...})` — as written they are shared-state bugs | 10 min |
| C2 | Data-ownership rule stated and enforced: strategies never call broker/quote APIs; all bars + chain snapshots flow engine → strategy | doc + 1 assert |
| C3 | Two-tier exits explicit in code: engine tier (breakers, emergency flatten) unconditional; playbook tier (stop/target/flip) tactical | doc + wiring |
| C4 | `risk_envelope()` output carries an `estimated: true` flag, rendered in UI next to the ₹ number | 15 min |
| C5 | E2E verify commands confirmed real: `create_app` factory exists; `emergency_stop` endpoint name correct (unverifiable from main — code is on the branch) | 15 min |

---

## 1. Layered Architecture

```
[Strategy Layer]  WHAT to trade
   |  emits Signal {direction, instrument_hint, confidence}
   |  RULE (C2): strategy NEVER touches broker/quote APIs.
   |  All market data (bars, chain snapshots) is handed TO it by the engine.
   v
[Playbook Layer]  Declarative reusable config (NEW)
   |  Playbook: underlying, structure_type, strike_selection, qty,
   |            exit_config, max_loss_per_trade, version
   |  Registry: CRUD + 3 seeded defaults; to_expression() → runner payload
   v
[Execution Engine]  HOW to trade (NEW)
   |  Input: Signal + Playbook + RunnerConfig + mode + source
   |  Checks: mode paper→simulator / live→broker+margin
   |          source synthetic→BS / live→mStock LTP
   |  Resolves: strikes (engine-fed chain), lot_size (instrument master,
   |            never hardcoded in the playbook), risk envelope
   |  Output: Fill | OrderRejected | RiskHalted
   v
[Portfolio Layer]  Aggregation + Risk (EXISTING, patched)
   |  One ledger per bucket (paper/live). Dashboard/manual book merged.
   |  get_portfolio_summary() totals = runners + manual book (honest).
   |  emergency_flatten_all(mode) closes BOTH books.
   v
[Service Layer]  Chain / Greeks / Expiry / Quote-source (EXISTING, kept)
      Options page = service view only. No ledger. Survives any future
      hard delete of the deprecated UI shell.
```

### Exit precedence (per bar, tactical tier inside engine emergency wrapper)

| Priority | Exit | Owner | Notes |
|---|---|---|---|
| 0 | Emergency flatten / breakers | **Engine** | Unconditional. Overrides everything. |
| 1 | `stop_loss_pct` | Playbook | Risk control beats strategy signal. |
| 2 | `take_profit_pct` | Playbook | Profit-taking is also protective. |
| 3 | Time/DTE square-off | Playbook-configured, engine-executed | `min_days_to_expiry` default 1. |
| 4 | `signal_flip` | Strategy | Last. Only if 1–3 didn't fire. |

**Re-entry:** when `reenter=true`, re-entry happens on the **next bar only** — never same-bar. Default **false**. (Evidence: same-bar re-enter churned −₹41,844 in the 2026-09-16 forward experiment.) V1.1 option: `max_reentries_per_day` (default 2).

---

## 2. Playbook Specification (final)

```python
@dataclass
class Playbook:
    playbook_id: str                 # uuid4 at CREATION (user action, not engine path)
    name: str
    underlying: str = "NIFTY"        # option-only V1 (Decision Q1)
    structure_type: str | dict = field(
        default_factory=lambda: {"BULLISH": "bull_call_spread",
                                 "BEARISH": "bear_put_spread"})
    strike_selection: str = "atm"    # atm | delta | otm | itm
    delta_target: float = 0.35
    quantity: int = 1
    exit_config: dict = field(default_factory=lambda: {
        "signal_flip": True, "stop_loss_pct": 0.5,
        "take_profit_pct": 1.0, "min_days_to_expiry": 1,
        "reenter": False})           # default false — churn guard
    max_loss_per_trade: float | None = None   # per-SIGNAL ₹ envelope
    description: str = ""
    tags: list = field(default_factory=list)
    version: int = 1                 # auto-bump on PUT
    created_at: str | None = None
    updated_at: str | None = None
```

**Methods:**

- `to_expression() -> dict` — the `instrument.expression` block for runner create
- `to_runner_config(strategy_name, allocated_capital, ...) -> dict` — full spawn payload
- `risk_envelope(spot, lot_size) -> dict` — ₹ max-loss normalization; **returns `estimated: true`** (C4). Lot size is resolved from the instrument master at call time — **never stored in the playbook** (NSE revises lot sizes).

**Versioning & reproducibility (Q5):** integer version bumps on every PUT. The runner **snapshots** `to_expression()` at spawn time; editing a playbook never mutates a running runner. That snapshot is the reproducibility guarantee — no version history table in V1.

**Scope (Q1):** option-only V1. `to_runner_config` keeps `symbol` = option underlying. Equity playbooks are V1.1 after ≥10 playbook-spawned runners exist.

### API contract (unchanged from dev proposal)

```
GET    /api/playbooks?tag=&underlying=     list
GET    /api/playbooks/<id>                 single
POST   /api/playbooks                      create
PUT    /api/playbooks/<id>                 update (bumps version)
DELETE /api/playbooks/<id>                 delete (blocks pb_default_*)
POST   /api/playbooks/<id>/spawn           returns runner config; caller creates
```

`spawn` returning config without side effects is ratified: one creation path (`POST /api/portfolio/runner/create`), one audit trail.

---

## 3. Risk Model

### V1 (shipping now)

`risk_envelope(spot, lot_size)` → `estimated_premium = spot × {2% ATM | 1% OTM | 4% ITM} × qty × lot_size`, capped by `max_loss_per_trade`. Every UI surface labels it **"estimated"**.

### V2 (next sprint — inputs answered, Q2)

| Input | Source | Fallback chain |
|---|---|---|
| IV | mStock live IV when quote carries it | 20-day realized vol → per-underlying default (NIFTY 0.12, BANKNIFTY 0.15 — existing `SyntheticChainGenerator.VOL`) |
| Risk-free | `RISK_FREE_RATE` env | default 0.065 (matches `bs_price`) |
| Margin/SPAN | mStock margin API **if exposed** | premium + 20% short-notional heuristic, labeled "estimate". **NSE SPAN file parsing is explicitly out of scope.** |

Formula V2: `max_loss = max(premium_based, margin_based) × qty`, still capped by `max_loss_per_trade`.

**Scope clarification:** `max_loss_per_trade` = **per-signal** envelope (ExecutionEngine pre-order check). **Per-day** risk remains the existing `daily_loss_limit` bucket breaker. Two controls, two scopes — never conflate.

### Enforcement points

1. ExecutionEngine — before any order (per-signal cap)
2. PortfolioManager — `daily_loss_limit` breaker (per-day, per-bucket)

---

## 4. Storage

- **V1:** in-memory singleton `_REGISTRY`, 3 seeded defaults, optional JSON file via `PLAYBOOKS_PATH` env.
- **V2:** `playbooks` table (id, name, underlying, expression JSONB, exit JSONB, max_loss, tags, version, created_at, updated_at) — mirrors the `option_positions` migration pattern. No conflict with existing migrations.

---

## 5. UI Contract (final)

**Portfolio tabs:** `Equity | Positions | 📚 Playbooks | 📦 Manual Options Book | Log`

- **Playbooks tab:** card grid (name, underlying·structure·strike·qty, exit bits, risk cap + "estimated" badge, tags) + Deploy/Edit/Delete + New Playbook.
- **Manual Options Book tab:** structures + legs tables, per-structure Close, Flatten Manual Book. This tab is what makes legacy trades visible — the original UX complaint.
- **Banners:** unified (Strategy owns WHAT / Engine owns HOW, localStorage-dismissed) + dashboard-book banner (count, View, Flatten).
- **Options page:** deprecation banner → Portfolio; retains chain, Greeks, expiry alerts as **service view only**.

---

## 5.1 Spawn Modal Contract (slim — user finding, 2026-09-16)

The Add Instance form owns **routing only** — six fields, nothing else:

| Field | Values | Notes |
|---|---|---|
| Strategy | registry list | Owns trigger/SL/target logic (plug-and-play) |
| Timeframe | 1min…1day | Runner-level, not strategy-level |
| Target type | Single Symbol \| Pool | **Auto-set and locked by the strategy's `signal_kind`**: option strategy → Single Symbol only, index-whitelisted symbols; equity → either |
| Symbol | NIFTY, BANKNIFTY (options V1) / any (equity) | Options limited to indices |
| Bucket mode | paper \| live | Routes execution |
| Data source | synthetic \| replay \| mStock | Feeds the shared bus |
| Allocation (₹) | number | Capital assigned |

Everything previously on the form (instrument, structure, strikes, stops, exits) is **Playbook territory** — removed from the modal. One Playbooks tab action ("New Playbook") is where trading logic is configured. Noise = configuration in the wrong layer.

## 5.2 Shared Market Data Bus (C2 completion)

**Rule: one feed per `(source, symbol, timeframe)`, shared by every runner subscribed to it.** Strategies receive bars/chain snapshots from the engine — they never construct feeds. For options, one chain generator per underlying is shared by all option runners on that underlying.

Motivation: today each runner builds its own feed (N runners = N polling loops); on mStock (~1 req/s rate limit) 5 runners would trip limits and overload the box. The bus makes runner count and request count independent.

- `FeedRegistry`: refcounted singleton feeds; the last subscriber to detach stops the feed.
- Runner spawn takes a feed *handle* from the registry, never a new `SyntheticFeed`.
- mStock: exactly one poller per symbol regardless of subscriber count.

## 6. Decision Log (ratified)

| Decision | Ruling | Status |
|---|---|---|
| D1 Script vs Entity | **Declarative entity.** Arbitrary Python in the live path is an RCE surface. `to_python()` exporter may be added later — no architecture break. | Ratified |
| D5 Spawn-form scope | **Form = routing, Playbook = trading logic.** Six fields max; strategy `signal_kind` auto-locks target type. | Ratified (user finding) |
| D6 Data sharing | **Shared bus, engine-owned.** One feed per (source, symbol, timeframe); strategies subscribe, never fetch. | Ratified (user finding) |
| D2 Hard delete vs Soft deprecate | **Soft-deprecate.** Ledger merges now; UI shell retires on Q6 metrics; service view persists. Reversible. | Ratified |
| D3 Risk envelope phased | **V1 estimate → V2 BS+margin.** Interface already takes `spot_price, lot_size`; IV injects without breaking the API. | Ratified |
| Scrap vs Fix | **Fix.** ~1,600-line UI surface vs 5,900 lines of machinery + 16,900 test lines a revert would destroy. | Ratified (matches 2026-09-16 analysis) |
| D7 Strategy upload | **Template-conformant file-drop, not live-paste.** Strategies are Python classes dropped in `plugins/strategies/`, discovered at startup, validated against the template conformance test. No eval/exec of uploaded text in the live path. | Ratified |

---

## 7. Rollout

| Step | Work | Est. |
|---|---|---|
| R1 | Clear conditions C1–C5 on the branch | 1h |
| R2 | Unit tests: Playbook CRUD, spawn config, risk_envelope cap, exit precedence | 2h |
| R3 | Merge → Portfolio summary shows `dashboard_book`; manual trades visible | — |
| R4 | V2 risk envelope (IV + margin) behind the same interface | 1 day |
| R5 | Hard delete Options UI shell when Q6 criteria met | 0.5 day |

**Q6 hard-delete criteria (replaces "80% for 2 weeks"):** zero new manual-book structures in trailing 14 days **AND** ≥10 cumulative Playbook-spawned runners. Review at 2 sprints regardless of metrics. Service view survives the delete.

---

## 8. Known Gaps Carried Forward (not blocking, tracked)

1. ~~**Synthetic feed wiring**~~ **CLOSED (Gap #1, this merge).** `MStockBarFeed` (in `feed_registry.py`) is the live poll thread — ONE per manager for all mstock symbols, riding the same `on_bar`/`on_tick_end` fan-out as synthetic bars. `add_runner` routes by `config.source`; the thread auto-runs only while an mstock runner is live; market-closed it seeds once per symbol then idles. Runner code unchanged (C2). Remaining live-work is operational: credentials/session in `backtest.live.auth`, and option-chain live pricing (still synthetic via the ChainBus). (Ref: `docs/OPTIONS-FORWARD-TEST-EXPERIMENT.md`)
2. **Chain-shape refactor** — straddle/strangle/iron-condor/calendar unpriceable; blocks richer option playbooks. Phase B.
3. **Runner-state persistence** — portfolio manager is in-memory V1; restart loses the book.
4. **Per-runner vs per-bucket accounting** — separate decision; do not bundle into this rollout.

## 9. Strategy Template Contract (plug-and-play, D7)

A strategy is conformant when it satisfies the template — nothing else is inspected:

```python
class MyStrategy(Strategy):          # or duck-typed equivalent
    name = "my_strategy"            # unique, becomes the registry key
    signal_kind = "option"           # "option" | "equity" — locks target type in the spawn form
    params = {...}                   # schema: {name: {default, min, max, type, label, tooltip}}

    def generate_market_view(self, candles) -> MarketView | None:   # option kind
    #   — or —
    def generate_signals(self, candles) -> pd.Series:               # equity kind
```

Rules:
1. **Signal owns the trading logic.** Trigger, and any strategy-opinion on exits (`metadata` may carry suggested stops — the engine/playbook decides whether to honour them).
2. **Data comes in, never fetched** (C2). The engine hands `candles` (and chain snapshots for option kind). A strategy that imports a broker/quote/feed module fails the conformance test.
3. **Determinism:** same candles in → same view out. No `datetime.now()`, no `random`, no file I/O.
4. **Drop-in discovery:** `plugins/strategies/*.py` scanned at startup; each file must import cleanly and pass the conformance test to register. A non-conformant file is logged and skipped — never crashes the app.
5. **Two templates ship as reference:** `templates/option_strategy_template.py` and `templates/equity_strategy_template.py`, each with a minimal working example (EMA-based) and a filled conformance test.

Implementation note: templates + conformance test + plugin discovery = one task (U6.3); the conformance test doubles as the acceptance gate for user-uploaded strategies.
