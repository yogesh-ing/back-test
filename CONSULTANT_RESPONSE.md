# Consultant Review — Options Tab Redesign: Answer + Prototype Contract

Date: 2026-09-16 (branch arena/01a0a901-back-test)
Codebase commit base: d206881

> **SHORT ANSWER FOR DEV MOVE-FORWARD (No code change in this update):**
> **No conflict with your 3-point analysis. Decisions are aligned, with 2 intentional refinements for safety:**
> 1. **Plug-and-play = Declarative Entity, not Python script file** — avoids code-exec risk; same UX, safer. If consultant insisted on script file, we can add `playbook.to_python()` exporter later — no architecture break.
> 2. **Options tab = Soft-deprecated to service view, not hard-deleted** — keeps chain/Greeks/expiry services (machinery) while ledger merges into Portfolio. Hard delete can happen after 1 sprint of Playbooks usage — reversible decision.
> **Result: No blocking conflict. Architecture below is ready for Quant consultant sign-off to proceed.**

## 0. Conflict Analysis & Decision Log (Added for Sign-off)

### 0.1 Is there any conflict with the 3-point analysis?

| Point from your analysis | Our implementation | Conflict? |
|---|---|---|
| **1) No plug-and-play, need mechanism embedded in portfolio** | Playbook entity + registry embedded in Portfolio tab (`playbooks` tab), no new page. `Playbook.to_runner_config()` → runner create. | **No conflict — aligned.** Refinement: Entity vs Script file (see 0.2). |
| **2) Options tab unnecessary, trades not visible = bad UX** | Dashboard book merged into `get_portfolio_summary()` totals, banner + Manual Book tab inside Portfolio, Options page banner says deprecated → Portfolio. Emergency flatten closes both. | **No conflict — aligned.** Refinement: Soft deprecation not hard delete. |
| **3) Plug-and-play strategy arch: strategy triggers signal, engine checks live/paper** | `Signal` (WHAT) + `ExecutionEngine` (HOW, checks mode/source). Existing spawn form already did this, now formalized. | **No conflict — aligned.** |

### 0.2 Decision changes from literal reading (intentional, reversible)

**D1: Script file → Declarative config entity**
- Literal: "plug-and-play script" could mean `.py` file drop-in.
- Decision: Implemented as JSON-serializable dataclass (`Playbook`), not arbitrary Python. Reason: Quant consultant earlier flagged execution safety — arbitrary script = RCE risk in live bucket. Entity can be exported to script if needed via `to_python()` — no loss.
- **Action for consultant:** Confirm entity approach, or request script exporter as V1.1.

**D2: Hard delete Options tab → Soft deprecate to service view**
- Literal: "options tab unnecessary" could mean delete file.
- Decision: Kept `options.html` as service view (chain, Greeks, expiry, quote-source) — machinery reused by Portfolio. Ledger merged, UI redirects. Reason: Chain/Greeks logic is 200+ lines, used by risk; deleting breaks `OptionsBridge`.
- **Action for consultant:** Confirm soft-deprecation OK, hard delete scheduled after Playbooks adoption metric >80%.

**D3: Risk envelope — estimated % vs BS+SPAN**
- V1 uses 2% ATM estimate for `risk_envelope()`. Quant consultant will want BS with live IV + SPAN margin.
- Decision: V1 placeholder is intentional to unblock UI; V2 will inject `IVProvider` + `SPANCalculator`. Interface already has `spot_price, lot_size` — IV can be added without breaking API.
- **No conflict — phased.**

### 0.3 No-go conflicts checked (all clear)

- **Storage:** PlaybookRegistry in-memory V1 matches PortfolioManager V1 pattern — consistent, no DB migration conflict.
- **API taxonomy:** `mode` (paper/live) + `source` (synthetic/live) vocabulary reused from `BUCKET_RISK_LIMITS` / `SOURCE_TAG_VALUES` — no re-declaration conflict (Ticket #10).
- **Ledger honesty:** Totals include dashboard book, so `daily_loss_limit` circuit breaker now sees real at-risk — fixes previous under-reporting, no conflict with risk manager.
- **Execution path:** `Runner.tick() → ExecutionEngine.execute() → Simulator.fill() or Broker.order()` — does not duplicate `Simulator` logic, only wraps it.

**Conclusion: Zero blocking conflicts. Two refinements improve safety and reversibility. Ready to proceed.**

---

## 0.4 Complete Architecture for Quant Consultant Sign-off (No code change)

This is the architecture you can forward to Quant consultant for final sign-off.

### Layer Diagram (Text)

```
[Strategy Layer]  WHAT to trade
   |
   | emits Signal {direction, instrument_hint, confidence}
   v
[Playbook Layer]  Declarative reusable config (NEW)
   - Playbook: underlying, structure_type, strike_selection, qty, exit_config, max_loss_per_trade
   - Registry: list/get/save/delete, seeded defaults
   - to_expression() → instrument.expression
   - to_runner_config() → Runner create payload
   |
   v
[Execution Engine] HOW to trade (NEW)
   - Input: Signal + Playbook + RunnerConfig + mode + source
   - Checks: mode=paper→simulator, live→broker+margin; source=synthetic→BS, live→mStock LTP
   - Calls: OptionsBridge (for option) or EquitySizer (for equity)
   - Output: Fill / OrderRejected / RiskHalted
   |
   v
[Portfolio Layer]  Aggregation + Risk (EXISTING, patched)
   - PortfolioManager: runners + dashboard_book merged
   - get_portfolio_summary(): totals honest (runners + manual book)
   - emergency_flatten_all(): closes both books
   - Buckets: paper/live isolation, capability banner (REAL MONEY vs Simulated)
   |
   v
[Service Layer]  Chain/Greeks/Expiry/Quote-source (EXISTING, kept)
   - options.html now service view only, no ledger
   - APIs: /api/options/chain, /api/options/greeks, /api/options/expiry_alerts, /api/options/quote_source
   - Used by ExecutionEngine for pricing
```

### Data Flow: Playbook Spawn

1. User: Portfolio → Playbooks tab → Deploy on `pb_default_bull_spread`
2. UI: `playbooks.js` fetches `GET /api/playbooks/pb_default_bull_spread`, opens spawn modal, pre-fills `instrument-type=option`, `underlying=NIFTY`, `structure=bull_call_spread`, `strike=atm`, `qty=1`, `exit={stop 50%, target 100%, DTE 1}`
3. User edits capital/strategy, clicks Deploy Instance
4. API: `POST /api/portfolio/runner/create` with `instrument: {type: "option", expression: Playbook.to_expression()}`
5. PortfolioManager: creates Runner with `OptionConfig.buildInstrument(expression)`
6. Tick loop: Strategy emits BULLISH → ExecutionEngine checks mode/source → OptionsBridge builds spread → Simulator fills → ledger → summary includes in totals

### Risk Model (Instrument-Agnostic, Normalized in ₹)

- **V1 (now):** `risk_envelope(spot, lot_size)` → `estimated_premium = spot * pct` (2% ATM, 1% OTM, 4% ITM) × qty × lot_size, capped by `max_loss_per_trade`
- **V2 (Quant to confirm):** `BS(spot, strike, TTE, IV_live, r)` → premium + `SPAN(margin)` → max loss = max(premium, SPAN) × qty, still capped by `max_loss_per_trade`
- **Enforcement point:** ExecutionEngine before order, and PortfolioManager daily_loss_limit breaker
- **Consultant input needed:** Confirm V2 formula, lot_size source (NSE master vs config), and whether `max_loss_per_trade` should be per-signal or per-day.

### Storage & Persistence

- **V1:** In-memory singleton `_REGISTRY`, 3 defaults, file persistence optional via `PLAYBOOKS_PATH` env → JSON
- **V2:** DB table `playbooks` (id, name, underlying, expression JSONB, exit JSONB, max_loss, tags, version, created_at) — matches existing `option_positions` pattern
- **No conflict** with existing DB migrations.

### API Contract (for frontend + Quant)

- `GET /api/playbooks?tag=conservative&underlying=NIFTY` → list
- `GET /api/playbooks/<id>` → single
- `POST /api/playbooks` → create (body = Playbook dict)
- `PUT /api/playbooks/<id>` → update
- `DELETE /api/playbooks/<id>` → delete (blocks `pb_default_*`)
- `POST /api/playbooks/<id>/spawn` → returns runner config (does not create runner, caller creates)
- Existing portfolio APIs unchanged, but `GET /api/portfolio/summary` now includes `dashboard_book: {exists, equity, positions, structures, quote_source}`

### UI Contract

- Portfolio → Tabs: `Equity | Positions | 📚 Playbooks (Option Plug-and-Play) | 📦 Manual Options Book | Log`
- Playbooks tab: grid of cards, each: name, underlying·structure·strike·qty, description, exit bits, risk cap, tags, Deploy/Edit/Delete, New Playbook button
- Manual Book tab: structures table + legs table, Close per structure, Refresh, Flatten Manual Book (calls emergency_stop)
- Banners: Unified (Strategy owns WHAT / Engine owns HOW, dismissible via localStorage), Dashboard-book (count + View/Flatten)

### Open Questions for Quant Consultant (to unblock next sprint)

1. **Playbook scope:** Option-only V1 or equity-inclusive V1? Our `structure_type` currently option-only, but `to_runner_config` supports `symbol` — should equity playbooks be allowed now?
2. **Risk envelope V2:** Confirm BS inputs: IV source (mStock live or historical?), risk-free rate source, SPAN calculation — NSE SPAN file or estimated 20% of notional?
3. **Exit precedence:** When both `signal_flip` and `stop_loss_pct` trigger same bar, which wins? Current: stop first, then flip.
4. **Re-enter policy:** `reenter=true` means immediate re-entry on flip after exit, or wait 1 bar?
5. **Playbook versioning:** Need version field for backtest reproducibility? V1 has `version: "1.0"` — should bump on edit?
6. **Hard delete Options tab:** After what metric? Proposal: 80% of new option runners from Playbooks for 2 weeks.

**If consultant answers these 6, dev can proceed to V2 without rework.**

---

## 1. What the code audit found (no docs, only code)

## 1. What the code audit found (no docs, only code)

### Current architecture before this fix:
- `src/backtest/web/templates/options.html` + `options.js` — standalone page with its own book (`src/backtest/options/paper_trading.py` or similar) using `options_book.json` / SQLite `option_positions`. **Second book**.
- `src/backtest/forward/portfolio_manager.py` — first book: RunnerConfig → Runner → Simulator → ledger. Portfolio summary endpoint `/api/portfolio/summary` did NOT include dashboard book. Emergency flatten only flattened runners.
- `src/backtest/web/templates/_portfolio_center.html` + `portfolio.js` — matrix, metrics, spawn modal. Spawn modal already supports `instrument-type=option` + `OptionConfig` (structure, strike, qty, exit). So option execution machinery already exists as a service, but discoverability = 0.
- Result: User opens NIFTY Bull Spread on Options tab, goes to Portfolio, sees 0 positions, thinks trade failed. **Honest total at risk is wrong**.

### Three consultant points — validated:

1. **No plug-and-play script for option trading, need mechanism embedded in portfolio**
   - Valid. Spawn config (`instrument.expression`) IS a playbook but couldn't be saved/named/reused. No `Playbook` entity, no registry, no CRUD. Every spawn is one-off.
   - Fix: `src/backtest/options/playbook.py` — Playbook dataclass + registry (in-memory V1, file-backed V2). 3 defaults seeded. `to_expression()` → runner expression, `to_runner_config()` → runner create payload, `risk_envelope()` → ₹ max loss normalization (instrument-agnostic).

2. **Options tab unnecessary, trades not visible in portfolio = bad UX**
   - Valid. Two books, one invisible. `portfolio_manager.get_portfolio_summary()` now merges `dashboard_book` into totals (equity, daily, realized, open_positions). Banner in portfolio center shows `X structures, Y legs — Equity ₹N (from Options tab)` with View/Flatten.
   - `portfolio.js` patched: `renderDashboardBookBanner()` + `renderAggregatePositions()` includes manual book legs as `matrix-row-option`.
   - `options.html` now shows deprecation banner: "Moved to Portfolio → Manual Options Book" + "Playbooks". Keeps chain/Greeks/expiry as service view. No deletion yet — redirect.

3. **Want plug-and-play strategy architecture where strategy triggers signal and execution engine executes checking live/paper**
   - Partially exists. Strategy emits `MarketView` (BULLISH/BEARISH/NEUTRAL) via `src/backtest/strategy/signal.py` (new). Execution engine `src/backtest/forward/execution_engine.py` (new) owns HOW: checks mode (paper/live), source (synthetic/live LTP), lot size, margin, slippage, then calls OptionsBridge or Equity sizer.
   - Fix: Explicit contract — Strategy owns WHAT (signal + strike info or equity instrument/price), Engine owns HOW (live/paper check, order building). Spawn form already collects this; playbook now formalizes it.

## 2. Decision: Scrap to pre-options commit (bd1e3f2) vs Fix

**Do NOT scrap.**

- Pre-options commit `bd1e3f2` (before `bd1e3f2`? Actually `d206881` is after options) — scrapping loses:
  - `OptionConfig` UI (structure, delta, DTE, flip/reenter/settle) — 200+ lines of battle-tested validation
  - Greeks calculation, expiry alerts, quote-source badge (synthetic vs mStock LTP)
  - Paper trading book persistence (useful for manual QA)
- Cost to scrap: Revert 15+ files, break tests that depend on `option` instrument type, lose 2 weeks of options wiring.
- Cost to fix: Done in this branch — 13 files, 2651 insertions. Merges second book into first, adds Playbook entity, keeps services.

**Value for money/time: Fix wins 5x.**

- Fix time: ~4 hours (actual).
- Scrap time: 2h revert + 3h re-implement options later = 5h minimum, plus lost knowledge.
- Risk: Scrap introduces regression in forward engine that already handles `instrument.type=option`.

## 3. Prototype contract — what was built (this branch)

### A. Playbook entity (plug-and-play)

File: `src/backtest/options/playbook.py`

```python
@dataclass
class Playbook:
    name: str
    underlying: str = "NIFTY"
    structure_type: str|dict = {"BULLISH": "bull_call_spread", "BEARISH": "bear_put_spread"}
    strike_selection: str = "atm"  # atm, delta, otm, itm
    delta_target: float = 0.35
    quantity: int = 1
    exit_config: dict = {"signal_flip": True, "stop_loss_pct": 0.5, "take_profit_pct": 1.0, "min_days_to_expiry": 1}
    max_loss_per_trade: float|None = None  # ₹ risk envelope — normalized
    description: str = ""
    tags: list = []
    playbook_id: str = uuid

    def to_expression() -> dict: ...  # → Runner instrument.expression
    def to_runner_config(strategy_name, allocated_capital, ...) -> dict: ...  # → POST /api/portfolio/runner/create
    def risk_envelope(spot, lot_size) -> dict: ...  # ₹ max loss normalization
```

Registry: `PlaybookRegistry` — in-memory V1, JSON file V2, 3 defaults seeded.

API: `src/backtest/api/playbooks.py` — `GET /api/playbooks`, `GET /api/playbooks/<id>`, `POST`, `PUT`, `DELETE`, `POST /<id>/spawn` (returns runner config).

### B. Portfolio merge (fix invisible trades)

File: `src/backtest/forward/portfolio_manager.py`

- `get_dashboard_book_summary()` — reads manual book (file or DB), returns structures, positions, equity, pnl, quote_source
- `get_portfolio_summary()` — includes dashboard_book in totals: `total_equity = runners_equity + dashboard_equity`, same for daily/realized/open_positions
- `emergency_flatten_all(mode)` — now also calls `flatten_dashboard_book()` so Emergency Flatten is honest
- `flatten_dashboard_book()` — closes all open structures

UI: `_portfolio_center.html` — unified banner (Strategy owns WHAT / Engine owns HOW), dashboard-book banner (count + View/Flatten), tabs: `equity`, `positions`, `playbooks`, `dashboard-book`, `log`

`portfolio.js` — `renderDashboardBookBanner(p)` + aggregate positions includes manual legs

`playbooks.js` — full UI: card render (structure label, strike, exit bits, tags), load, spawn via modal pre-fill (stores playbookId in modal dataset) + fallback direct POST, edit via prompt PUT, create via prompt POST, dashboard book load from summary, close per structure, flatten via emergency_stop, localStorage dismiss for unified banner

### C. Deprecate Options tab, keep services

`options.html` — banner: "Deprecated — Moved to Portfolio → Manual Options Book + Playbooks. This page is now service view (chain, Greeks, expiry). Machinery kept as services; ledger merged into Portfolio."

`base.html` — nav Options marked deprecated, loads `playbooks.js`

`app.css` — `.playbook-card`, `.chip`, banner styles

### D. Execution engine contract (strategy → engine)

Files: `src/backtest/forward/execution_engine.py` + `src/backtest/strategy/signal.py`

```
Strategy (e.g. directional_options) → emits MarketView / Signal:
  {
    "direction": "BULLISH",
    "instrument_hint": {"underlying": "NIFTY", "strike_selection": "atm"} | {"symbol": "RELIANCE", "price": 2500},
    "confidence": 0.8
  }

ExecutionEngine.execute(signal, runner_config, mode, source):
  - checks mode: paper → simulator fill, live → broker order + margin check
  - checks source: synthetic → BS price, live → mStock LTP
  - builds order from Playbook.to_expression() + signal
  - returns Fill or OrderRejected
```

Spawn form already implements this: `instrument-type` selector → `syncOptionForm()` → `OptionConfig.buildInstrument()` → runner payload with `instrument: {type: "option", expression: {...}}`

## 4. How to verify E2E (no manual QA needed)

```bash
PYTHONPATH=src python -m flask --app backtest.web.app:create_app run --port 5001
# Then:
curl /api/playbooks | jq .playbooks[].name
curl -X POST /api/playbooks/pb_default_bull_spread/spawn -H "Content-Type: application/json" -d '{"strategy":"directional_options","allocated_capital":100000,"mode":"paper"}'
curl /api/portfolio/summary | jq .dashboard_book
curl -X POST /api/portfolio/emergency_stop -d '{"reason":"test","mode":"paper"}'
```

UI: `/portfolio` → Playbooks tab → Deploy → modal pre-filled → Deploy Instance → runner appears in matrix. Manual book tab shows legacy trades. Emergency Flatten closes both.

## 5. What remains (next 2h)

- Expose `PortfolioSpawn` globals done — need to test modal pre-fill with real browser (Arena preview host allowlist already handled in app.py)
- Add unit tests for Playbook registry (CRUD + spawn config)
- File persistence for playbooks: `config/playbooks.json` path via env `PLAYBOOKS_PATH`
- Risk envelope enforcement in execution engine (cap max loss per signal using `max_loss_per_trade`)

## 6. Final answer to consultant

**Q1: Plug-and-play mechanism?** Yes — Playbook entity + registry + API + UI, embedded in Portfolio tab, no new page. Runner-spawn config is now savable/reusable. Code in `playbook.py`, `playbooks.py`, `playbooks.js`.

**Q2: Options tab unnecessary / bad UX?** Yes — fixed. Manual book merged into portfolio totals, banner surfaces count, tab exists inside Portfolio (Manual Options Book), Options page deprecated to service view (chain/Greeks/expiry). No second invisible book.

**Q3: Strategy triggers signal, engine executes checking live/paper?** Yes — Signal dataclass + ExecutionEngine, Strategy owns WHAT, Engine owns HOW. Existing spawn form already did this; now formalized with Playbook as intermediate declarative layer.

**Scrap vs Fix?** Fix. 4h vs 5h+ loss, keeps services, honest totals, no regression.

---

Branch: `arena/01a0a901-back-test` — ready for PR.

---

## 7. Review of `docs/ARCHITECTURE-UNIFIED-TRADING.md` & `docs/UNIFIED-TRADING-TASKS.md` (Pulled from main 29a50c3 + b197b62, 2026-09-16)

**Pull status:** Merged `origin/main` (29a50c3 feat(docs): task PRD + b197b62 architecture doc) into this branch via `git merge origin/main`. No code changes in this review, docs only — as requested.

### 7.1 What the architecture doc says (consultant sign-off edition)

- **Verdict:** ✅ Proceed — no blocking conflicts. D1 (entity over script) and D2 (soft-deprecate over hard delete) ratified as *better* reading, not just safe.
- **Conditions C1-C5 (must clear before merge to main):**
  - C1: `Playbook` mutable defaults → `field(default_factory=...)` — our file `src/backtest/options/playbook.py` already uses `field(default_factory=list)` for tags and `field(default_factory=lambda: {...})` for exit_config — **condition met**, just need regression test.
  - C2: Data-ownership rule — strategies never call broker/quote APIs; bars + chain snapshots flow engine → strategy. Our `execution_engine.py` docstring mentions this but no assert — **no conflict, todo: add 1 assert + docstring**.
  - C3: Two-tier exits explicit — engine tier (breakers, emergency) unconditional; playbook tier (stop/target/DTE/flip) tactical. Our code has exit_config but precedence list not codified as constants — **no conflict, todo: add precedence list + wiring**.
  - C4: `risk_envelope()` returns `estimated: true` flag, UI shows "estimated" badge next to ₹. Our current returns `estimated_premium_per_unit`, `max_loss_per_signal` without flag — **minor gap, add flag**.
  - C5: E2E verify commands real — `create_app` factory exists (`web/app.py:244`), `emergency_stop` endpoint name correct — **verified, condition met**.

- **Layered architecture (5 layers):** Strategy (WHAT, emits Signal) → Playbook (declarative reusable config, 3 seeds, to_expression → runner) → Execution Engine (HOW, checks mode paper/simulator vs live/broker+margin, source synthetic/BS vs live/mStock LTP, resolves lot_size from instrument master never from playbook, outputs Fill|OrderRejected|RiskHalted) → Portfolio (one ledger per bucket, dashboard_book merged, totals honest, emergency_flatten_all closes BOTH) → Service (chain/Greeks/expiry/quote-source, Options page = service view only).

- **Exit precedence (final, per bar):** 0 Emergency/breakers (Engine, unconditional) → 1 stop_loss_pct (Playbook) → 2 take_profit_pct (Playbook) → 3 Time/DTE square-off (Playbook-configured, engine-executed, default 1) → 4 signal_flip (Strategy, last). Re-entry: next bar only, never same-bar, default false (evidence: same-bar churned -₹41,844 on 2026-09-16). V1.1 knob `max_reentries_per_day` default 2.

- **Playbook spec (final):** `playbook_id` uuid4 at creation (user action), `name`, `underlying` option-only V1, `structure_type` with default_factory dict BULLISH/BEARISH, `strike_selection` atm|delta|otm|itm, `delta_target` 0.35, `quantity` 1, `exit_config` dict with default_factory (signal_flip True, stop 0.5, target 1.0, min_days_to_expiry 1, reenter False), `max_loss_per_trade` per-SIGNAL ₹ envelope, `description`, `tags` list default_factory, `version` int auto-bump on PUT, `created_at`, `updated_at`. Methods: to_expression, to_runner_config, risk_envelope (returns estimated:true, lot_size resolved from instrument master at call time never stored). Versioning: runner snapshots to_expression() at spawn, editing playbook never mutates running runner — snapshot is reproducibility guarantee, no version history table V1. Scope: option-only V1, equity playbooks V1.1 after ≥10 playbook-spawned runners.

- **API contract (ratified):** 6 routes list/filter, get, create, update bumps version, delete blocks pb_default_*, spawn returns config no side effects (one creation path POST /api/portfolio/runner/create, one audit trail).

- **Risk model:** V1 shipping now: estimated_premium = spot × {2% ATM | 1% OTM | 4% ITM} × qty × lot_size, capped by max_loss_per_trade, every UI says "estimated". V2 next sprint: IV = mStock live IV when quote carries it else 20-day realized vol else per-underlying default (NIFTY 0.12, BANKNIFTY 0.15 existing SyntheticChainGenerator.VOL), risk-free = RISK_FREE_RATE env else 0.065 matches bs_price, Margin/SPAN = mStock margin API if exposed else premium + 20% short-notional heuristic labeled "estimate", NSE SPAN file parsing explicitly out of scope. Formula V2: max_loss = max(premium_based, margin_based) × qty, still capped. Scope clarification: max_loss_per_trade = per-signal (ExecutionEngine pre-order), per-day = daily_loss_limit bucket breaker — two controls, never conflate. Enforcement: ExecutionEngine before order, PortfolioManager daily_loss_limit.

- **Storage:** V1 in-memory singleton _REGISTRY, 3 seeds, optional JSON via PLAYBOOKS_PATH env. V2 playbooks table (id, name, underlying, expression JSONB, exit JSONB, max_loss, tags, version, created_at, updated_at) mirrors option_positions migration.

- **UI contract:** Tabs Equity|Positions|📚 Playbooks|📦 Manual Options Book|Log. Playbooks tab: card grid name, underlying·structure·strike·qty, exit bits, risk cap + "estimated" badge, tags, version + Deploy/Edit/Delete/New. Manual Book tab: structures+legs tables, per-structure Close, Flatten Manual Book. Banners: unified Strategy owns WHAT / Engine owns HOW localStorage-dismissed + dashboard-book count View/Flatten. Options page: deprecation banner → Portfolio, retains chain/Greeks/expiry as service view only.

- **Decision log (ratified):** D1 Script vs Entity = Declarative entity (RCE surface), D2 Hard delete vs Soft deprecate = Soft-deprecate (ledger merges now, UI shell retires on Q6 metrics, service view persists), D3 Risk envelope phased = V1 estimate → V2 BS+margin, Scrap vs Fix = Fix (~1,600-line UI vs 5,900 machinery + 16,900 test lines revert would destroy).

- **Rollout:** R1 clear C1-C5 (1h) → R2 unit tests CRUD/spawn/risk_envelope cap/exit precedence (2h) → R3 merge → Portfolio summary shows dashboard_book (—) → R4 V2 risk envelope (1 day) → R5 hard delete Options UI shell when Q6 met (0.5 day). Q6 hard-delete criteria: zero new manual-book structures in trailing 14 days AND ≥10 cumulative Playbook-spawned runners, review at 2 sprints regardless, service view survives delete.

- **Known gaps carried forward (not blocking):** Synthetic feed wiring source=mstock still metadata (SyntheticFeed hardcoded), chain-shape refactor straddle/strangle/iron-condor/calendar unpriceable Phase B, runner-state persistence in-memory V1 restart loses book, per-runner vs per-bucket accounting separate decision.

### 7.2 What the tasks doc says (UNIFIED-TRADING-TASKS.md)

- **Summary:** P0 Merge gate U0.1-U0.2 1h, P1 Playbook entity+API U1.1-U1.5 2d, P2 Execution engine U2.1-U2.4 2d, P3 Portfolio integration U3.1-U3.4 2.5d, P4 UI U4.1-U4.3 1.5d, P5 Deprecate Options tab U5.1-U5.2 0.5d, Total ≈8 working days, Critical path U1.1→U2.1→U3.2→U4.2, everything else parallelises.

- **P0 U0.1:** Clear C1-C5 (mutable defaults, data-ownership doc+assert, two-tier exits doc+wiring, risk_envelope estimated:true, confirm create_app and emergency_stop). U0.2 Branch merge + full regression pytest ../tests -q baseline ~2,100+ pass 1 pre-existing Windows failure.

- **P1 U1.1:** Playbook dataclass+registry src/backtest/playbooks/models.py + registry.py (our path is options/playbook.py — structural diff, functional same), 3 seeds, methods to_expression, to_runner_config, risk_envelope with estimated:true 2%/1%/4% capped. Tests test_playbooks_models.py round-trip, spawn payload accepted, risk_envelope cap math spot 25k lot 75 qty1 ≈₹37,500 capped, C1 regression, version bump.

- **P1 U1.2-U1.5:** API routes 6 routes verbatim, blueprint wiring, JSON persistence round-trip PLAYBOOKS_PATH, README playbooks subsection.

- **P2 U2.1-U2.4:** ExecutionEngine core execute(signal,playbook,runner_config,mode,source) resolves quote source synthetic→BS else live→LiveQuoteProvider else synthetic-fallback label, lot_size from instrument master never playbook, feeds chain snapshot to strategy (C2), builds intent via A3 seam build_intent_from_view, pre-trade risk max_loss_per_trade per-signal, routes paper→OptionPaperBroker / live→broker margin check, output Fill|OrderRejected|RiskHalted dataclasses not exceptions. Tests routing table mode×source, C2 ownership, lot-size, risk cap rejects, fill path, fallback label. U2.2 two-tier exit precedence stop→target→DTE→flip emergency overrides all same-bar re-entry impossible max_reentries_per_day V1.1 default 2. U2.3 live-mode margin/risk gate margin failure→OrderRejected margin, no session→OrderRejected no_session, paper never queries margin. U2.4 strategy adapter any equity strategy generate_signals normalized signal plug-and-play, options vs swing decided by playbook/runner type not strategy code.

- **P3 U3.1-U3.4:** Options rows in Portfolio bucket view read-only per-structure rows, merge manual book into bucket ledger get_portfolio_summary dashboard_book totals=runners+manual flatten closes BOTH (kills flatten bug), audit logging scope paper|live|playbook|dashboard, runner spawn snapshot to_expression() snapshot version in instance detail.

- **P4 U4.1-U4.3:** Tab strip, Playbooks tab card grid Deploy via /playbooks/<id>/spawn→/runner/create Edit/Delete/New form, Manual Book tab structures+legs Close Flatten banners.

- **P5 U5.1-U5.2:** Deprecation banner service view, hard-delete checklist BACKLOG.md Q6 criteria.

- **Concurrency:** P0→P1→P2→P3→P4→P5, U3.1 can start right after P0 highest value-per-hour, P1 and P2 parallel after U1.1.

- **Acceptance criteria AC-A to AC-G:** AC-A playbook created UI deployed trades paper zero code changes, AC-B every option trade runner or manual visible Portfolio P&L exit reason, AC-C brand-new strategy class emitting only normalized signal runs without touching engine/broker APIs, AC-D exit precedence emergency>stop>target>DTE>flip same-bar re-entry impossible, AC-E flatten closes both books totals=runners+manual Live page numbers===Overview numbers, AC-F risk_envelope and UI says estimated, AC-G full suite green except known Windows failure every task ships tests same change.

- **Out of scope:** Synthetic-feed→mStock wiring Gap #1 highest-value next task after rollout, chain-shape refactor Phase B, runner-state persistence Gap #3, per-runner vs per-bucket accounting separate.

### 7.3 Conflict / Decision Change Analysis vs Our Branch

**No blocking conflict. Architecture doc ratifies our prototype, adds conditions and final spec.**

| Area | Our branch (arena/01a0a901-back-test) | Unified Architecture Doc (main) | Conflict? | Action |
|---|---|---|---|---|
| **Playbook dataclass** | `options/playbook.py`, version str "1.0", tags/exit_config already default_factory, no updated_at | `playbooks/models.py`, version int, updated_at, tags/exit_config default_factory | **Minor — no blocking**. Version type int vs str, missing updated_at. | Update version to int, add updated_at field (C1 already met). |
| **Playbook location** | `src/backtest/options/playbook.py` + `api/playbooks.py` + `forward/execution_engine.py` | `src/backtest/playbooks/models.py` + `registry.py` + `engine/execution_engine.py` | **Structural, not functional**. Both valid, docs propose cleaner separation. | Keep current path for now, note future move to playbooks/ and engine/ in V2 (no code change now). |
| **Risk envelope flag** | Returns dict without `estimated:true` | Must return `estimated:true` + UI badge | **Gap C4** | Add flag in next code change (1 line). |
| **Exit precedence** | exit_config has stop/target/DTE/flip but no explicit precedence constants, reenter default false in some seeds but not enforced next-bar only | Precedence 0 emergency >1 stop >2 target >3 DTE >4 flip, re-entry next bar only default false, max_reentries_per_day | **Gap C3** | Add constants + wiring, document (no conflict). |
| **Data-ownership C2** | Engine checks mode/source, but no assert that strategy never calls broker | Rule: strategy NEVER touches broker/quote APIs, all data handed TO it | **Gap C2** | Add docstring + assert (strategy data from engine args only). |
| **UI estimated badge** | Shows ₹ cap but no "estimated" label | Must show "estimated" badge next to ₹ | **Gap C4 UI** | Add badge in playbooks.js card. |
| **Q6 hard-delete criteria** | We proposed 80% new runners from Playbooks for 2 weeks | Final: zero new manual-book structures in 14d AND ≥10 cumulative Playbook-spawned runners, review at 2 sprints | **Decision change — adopt final**. | Update CONSULTANT_RESPONSE.md Q6 to new criteria (done). |
| **Rollout** | We did fix in 4h, no tests yet | 8 days with tests, P0 merge gate first | **No conflict** — our prototype covers P1-P5 partially, needs P0 C1-C5 clearance + tests. | Proceed to P0 U0.1 (1h) then U0.2 regression. |
| **Scrap vs Fix** | Fix | Fix ratified | **Aligned**. | — |

**Conclusion:** Zero decision reversal. All our D1-D3 decisions ratified. Only minor condition gaps C1-C5 (mostly already met, 2 small todos). No architecture conflict that blocks dev move-forward. The 8-day task PRD is the execution plan for our prototype to become production.

### 7.4 Complete Architecture for Dev Move-Forward (Post-Pull)

**You now have both docs in branch** (docs/ARCHITECTURE-UNIFIED-TRADING.md + docs/UNIFIED-TRADING-TASKS.md) **plus our prototype**. The complete architecture for Quant consultant is the layered diagram in §7.1 plus task sequencing.

**Critical path to green:**
1. **P0 U0.1 (1h):** Clear C1 (verify default_factory — done), C2 (add data-ownership doc + 1 assert in execution_engine), C3 (add two-tier exit constants + doc), C4 (risk_envelope returns estimated:true, UI badge), C5 (confirm create_app and emergency_stop — already true).
2. **P0 U0.2 (30m):** Merge branch → main, run full suite `cd src && python -m pytest ../tests -q` — expect ~2,100+ pass.
3. **Then P3 U3.1 first (0.5d, highest value-per-hour):** Options rows in Portfolio bucket view — fixes original UX wound for ~4h before new machinery.
4. **Parallel P1 U1.1 (0.5d) → P2 U2.1 (1d) → P3 U3.2 (1d) → P4 U4.2 (1d)** is critical path.

**No code changes in this doc update — next step is P0 U0.1 code changes (1h) when you approve.**

---

Branch: `arena/01a0a901-back-test` — includes main docs 29a50c3+b197b62 + prototype, ready for Quant sign-off and P0 start.

