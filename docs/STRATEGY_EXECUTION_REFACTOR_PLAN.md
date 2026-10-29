# Strategy-Owned Execution — Architectural Refactor (v2)

**Status**: Approved — **Phases 0–1 implemented** (2026-10-15); MVP CE/PE split + Phases 2–4 pending phase-by-phase approval
**Date**: 2026-10-15 · **Supersedes**: v1 of this document (same date)
**Author**: Quant Engineering Team · **Reviewer/Owner decisions**: Strategy owns intent; one Risk Gate owns permission; spawn freezes a fingerprinted snapshot.

> **v2 change summary (what the review changed):**
> 1. **B1** — The execution gate is *kept*, but consolidated into **exactly one Pre-Trade Risk Gate** with one rulebook. All other scattered checkers/guessers (UI defaults, bridge fallbacks, API silent clamps) are **deleted**, not retained as parallel gates.
> 2. **B2 + Gap #2** — Direction→rule resolution lives in the **template base class** (strategy side) at end state. The bridge's `_transitional_direction_rule_for(view)` is a **Phase 1–2 migration shim only, deleted in Phase 4**; `_operator_level_for` rename resolves the name collision (§6.2).
> 3. **B3** — **NEUTRAL redefined: it can never open a trade.** `atm_instant_buy` is abolished and split into `CE_instant_buy` / `PE_instant_buy`. A strategy must name the actual trade it wants at trigger time.
> 4. **B4** — Every opening intent must carry an **`ExitPlan` with a mandatory hard stop** (price / % / points, or a structure with inherently capped loss). Target is optional. Indicator exits are allowed as *strategy exits*, never as the sole protection on undefined-risk trades.
> 5. **B5** — Configuration layering follows the institutional model: **hard ceilings, menu-bounded overrides, risk-reducing-only intraday control** (§7).
> 6. **B6 + Gap #3** — Spawn freezes a snapshot fingerprinted with config hash + a **transitive strategy-bundle hash** (strategy file + template/base/execution modules it inherits, §8); code upgrades affect new runners only, and a bundle mismatch on rehydration **pauses the runner** rather than silently trading new code under old paperwork (§8).
> 7. **Gaps #4–#7 (review round 2)** — Layer-1 risk limits get an explicit home and phase (§7.1); intraday "reduce" is **descoped** to capabilities that exist today (§7.2); the Phase-3 migration wall is stated as an operational gate (§10); `DefinedRiskStructureStrategy` renamed **`StructureStrategy`** (§5.2). Architect Q&A with binding answers is in **Appendix C**.
> 8. **New workstreams** — a **template system + strategy-authoring manual** (§5), and a **full strategy rewrite** (§9) against a frozen pre-rewrite snapshot (§9.1). Existing strategies are *not* assumed as the starting template.

---

## 1. Executive Summary

Execution behaviour (direction→structure mapping, strike/expiry selection, exits, sizing) is today fragmented across strategy code, UI JavaScript, backend bridge fallbacks, and playbooks. The result is silent divergence: the same strategy class traded on NIFTY and silently failed on BANKNIFTY in the same session (incident, §2).

**v2 position:** Most decisions move *into the strategy*, expressed as a complete trade intent at trigger time. The two things that deliberately do **not** move into the strategy are (a) **permission** — one independent Pre-Trade Risk Gate (§6) — and (b) **history** — a frozen, fingerprinted spawn snapshot (§8). Everything else that currently guesses, merges, falls back, or clamps is removed.

Because current strategies are not written to this contract (some actively violate it, e.g. NEUTRAL-as-entry), **all option strategies will be rewritten** onto a small set of templates (§5, §9). A snapshot of every existing strategy has been frozen first (§9.1) so originals can be deleted safely as replacements land.

---

## 2. Problem Statement

### 2.1 Current failure (verified against code, 2026-10-15)

| Layer | Location | Behaviour today | Failure |
|---|---|---|---|
| Strategy | `plugins/strategies/atm_instant_buy.py` | Returns `Direction.NEUTRAL` unconditionally; says nothing about *what* to trade | Intent is a lie by construction: NEUTRAL secretly means "buy a call" |
| UI | `src/backtest/web/static/js/portfolio.js:1424` (`submitSpawn`) | Hardcodes `{BULLISH: bull_call_spread, BEARISH: bear_put_spread}` | Invents a mapping the strategy never declared; no NEUTRAL key |
| Bridge | `src/backtest/forward/options_bridge.py:1835–1837` (`_structure_type_for`) | Missing NEUTRAL key → falls back to the BULLISH mapping | Guesses intent instead of enforcing a contract |
| Bridge | `options_bridge.py:162` (`DEFAULT_EXPRESSION`) | `{BULLISH: long_call, BEARISH: long_put}` — **different** from the UI default (spreads) | Two "defaults" disagree with each other |
| Bridge | `options_bridge.py:2160` (`_pick_strikes`) | Any unrecognised `strike_selection` silently becomes ATM | Another silent guess |
| Exec gate | `src/backtest/options/execution_gate.py` (`V1_ALLOWED_STRUCTURES`) | Only `long_call`/`long_put` permitted; `paper_runner.py:1098` never widens it | Spreads the UI defaults to are refused **at execution time**, after spawn said "fine" |
| API | `src/backtest/api/portfolio.py:682` (`create_runner`) | Silently clamps `quantity` into [1, 100] | Silent correction of user input |
| Exit parsing | `src/backtest/options/exit_policy.py:234` (`ExitConfig.from_expression`) | Deliberately tolerant: unknown keys warned & skipped | Correct for legacy compat; **cannot** serve as the strict validator for new configs |
| Playbooks | `src/backtest/playbooks/models.py:171` (`to_expression`) | Strategy-agnostic layer *above* the strategy | Inverted ownership: a config file outranks the strategy's own intent |

### 2.2 Incident

Both deployments ran `atm_instant_buy` (always NEUTRAL). NIFTY's expression had been hand-edited with `"NEUTRAL": "long_call"` → traded. BANKNIFTY used the UI default (no NEUTRAL key) → bridge fell back to the BULLISH spread → gate/chain failure, silently. **Same strategy, same session, opposite outcomes — decided by who hand-edited which browser form.**

### 2.3 Root cause

The strategy↔engine execution contract is implicit and owned by no one. v2 makes it explicit, strategy-owned, template-enforced — and removes every fallback that let an incomplete contract appear to work.

---

## 3. Design Principles

1. **Strategy owns intent.** The strategy names the actual trade — structure, legs, strike rule, expiry rule, entry trigger, exit plan, default sizing — at trigger time. No other layer invents, maps, or completes it.
2. **One gate owns permission.** A single Pre-Trade Risk Gate (§6) is the only runtime authoriser. There are no other runtime gates, clamps, fallbacks, or "helpful" corrections.
3. **NEUTRAL means silence.** It never opens a trade (§4.2). No exceptions, no per-strategy redefinition.
4. **No trade without a hard stop.** Every opening intent carries an `ExitPlan` whose worst case is a concrete ₹ number at entry (§4.3).
5. **Fail closed, once, loudly.** Invalid/missing config refuses at authoring or spawn time; a runtime refusal is a coded, edge-triggered halt — never a guess, never repeated log spam.
6. **Spawn is a photograph.** The resolved config + strategy code fingerprint is frozen at spawn; nothing about a live runner changes because code or playbooks changed later (§8).
7. **Templates, not blank pages.** New strategies are written by filling a template (§5) under an authoring manual (§5.3). Freedom inside the frame; the frame is not optional.
8. **Reversible until cutover, then final.** Legacy strategies keep working until each rewritten replacement passes the cutover gate (§9.3); then the original is deleted and the legacy path for that strategy closes. No permanent dual system.

---

## 4. The Strategy Contract (New)

### 4.1 Complete Trade Intent

Strategies stop emitting a bare direction and hoping for a mapping. At trigger time the strategy (via its template, §5) produces a complete, self-describing intent:

```python
# Illustrative shape — final dataclass layout is a Phase-0 design task.
TradeIntent:
    action: OPEN | CLOSE | NONE          # NONE replaces "NEUTRAL as entry signal"
    structure: str                        # e.g. "long_call" — must be real & executable
    legs / strike_rule / expiry_rule      # atm | delta(target) | fixed_distance(pct) | target_price
    exit_plan: ExitPlan                   # §4.3 — mandatory, validated at intent construction
    sizing: SizingIntent                  # default lots + declared allowed range (see §7)
    strategy_ref: (name, version)         # code hash is stamped by the system, §8
```

`MarketView.direction` is retained during migration for directional templates, but under v2 semantics a direction is only ever an *input to the strategy's own declared mapping inside its template* — the bridge no longer maps directions to structures at end state (§6.2).

**Resolution ownership (normative, resolves the v2 §4.1/§6.2 contradiction):** direction→`DirectionRule` resolution is a method of the **template base class** (working name `TemplateBase.direction_rule_for(view)`), executed strategy-side before the intent reaches the bridge. Template-derived strategies therefore hand the bridge a *complete* intent and never trigger a bridge lookup. The bridge-side `_transitional_direction_rule_for(view)` (§6.2) exists **only** for not-yet-rewritten legacy strategies during Phases 1–2, is marked deprecated at birth, carries a removal test, and is **deleted in Phase 4**. No new code may call it after Phase 2.

### 4.2 NEUTRAL Redefinition (B3 — decided)

| Situation | NEUTRAL view behaviour (strict mode, normative) |
|---|---|
| No open position | **Nothing.** No entry, ever. (Was: could be smuggled into a trade via fallbacks.) |
| Open position, exit plan counts neutral bars | Counts toward `neutral_bars` exit, exactly as a viewless bar does today (`_is_neutral_view` semantics preserved for *exits only*) |
| Open position, no neutral-bar rule | Hold. Exit plan's other rules govern. |
| Strategy wants an immediate trade | It must emit an explicit opening intent (e.g. via `InstantLongOptionStrategy`, §5) — **not** NEUTRAL |

Consequences:
- `atm_instant_buy` is **abolished**, replaced by **`CE_instant_buy`** and **`PE_instant_buy`** (explicit call/put at trigger). Same for `immediate_entry` (triage in §9.2 decides its CE/PE/resting form) — any strategy whose design is "trade now" names the trade.
- `immediate_strangle` emits an explicit `OPEN short_strangle` intent from the `StructureStrategy` template, not NEUTRAL. (Note: its loss is *not* inherently capped — see §4.3 rule for its mandatory ₹ hard stop.)

### 4.3 ExitPlan & the Mandatory Stop (B4 — decided)

```text
ExitPlan (built by the strategy/template at trigger time; frozen into the trade at entry)
├── stop_loss      MANDATORY — exactly one hard form:
│                    • price-based   (premium/structure price level), or
│                    • pct-based     (% of premium paid), or
│                    • points-based  (absolute structure P&L ₹), or
│                    • structure-capped (long option / defined-risk spread:
│                      worst-case ₹ computed from legs at entry and recorded)
├── take_profit    optional — absent means "ride" (author's explicit choice)
├── strategy_exits optional — indicator/rule exits (e.g. Supertrend flip, EMA cross).
│                    Evaluated when the strategy next speaks; labelled as strategy
│                    exits in logs. NEVER the sole protection on an undefined-risk
│                    trade (naked shorts, short strangles/condors).
└── time rules     optional — max_bars, min_days_to_expiry, square-off window
```

Rules:
- An intent that cannot state a concrete worst-case ₹ loss at entry **cannot be constructed** — templates raise at intent-build time (authoring/contract layer), and the Risk Gate (§6) independently verifies the stated worst case against account limits (permission layer). Two layers, two *different* questions — not duplicate gates.
- Unknown/misspelled `ExitPlan` keys in a v2 config are a **spawn refusal (400)**, via a new strict parser. The existing tolerant `ExitConfig.from_expression` is **retained unchanged for legacy/v1 expressions only** and is explicitly not reused for v2 (resolves the v1 plan's self-contradiction).
- Existing informal stops living in expressions today (e.g. `immediate_strangle`'s ₹100 stop/target) move **into the strategy's ExitPlan** during rewrite — visible, owned, versioned with the strategy.

### 4.4 Strategy Declaration (`execution` spec)

The v1 plan's `ExecutionSpec` is retained in purpose, tightened in form:

```python
# src/backtest/strategy/execution.py (NEW)
ExecutionSpec:
    emits: frozenset[Direction]        # directions the strategy CAN emit (declared superset)
    structures: Mapping[Direction, DirectionRule]   # strategy's OWN mapping, inside the strategy
    exit_plan_template: ExitPlan        # default exit plan (§4.3); intents may refine, never remove the stop
    sizing: {default_lots, min_lots, max_lots}      # a menu for §7 overrides, NOT a risk limit
    allowed_overrides: frozenset[str]   # which fields deployment may choose, and within what range
```

- Direction keys are canonicalised **once** at parse (single enum, no `neutral`/`NEUTRAL` string drift anywhere downstream).
- A runtime view whose direction ∉ `emits` is a **strategy contract breach** (coded halt `STRATEGY_EMITS_UNDECLARED_DIRECTION`); a direction in `emits` with no rule is an **authoring error** caught at registration. Different codes, different owners.
- The parsed spec is **immutable** (`frozen=True`, mapping proxies) and parsed/validated once at registration — never re-derived from a mutable class dict per runner, and never mutated by spawn.
- Validation is against what can actually execute (§6): structure must exist in the bridge registry **and** be permittable by the Risk Gate for the target mode/account. Spawn must never approve a structure that the gate will refuse on the first bar (v1 plan gap B1).

---

## 5. Template System & Authoring Manual (New Workstream)

### 5.1 Rationale

No strategy is written from a blank file again. Templates encode the contract (§4) so a correct strategy is the *easy* outcome; the conformance battery (§5.4) makes an incorrect one un-registerable.

### 5.2 Templates (initial set)

| Template | Author supplies | Template guarantees |
|---|---|---|
| `DirectionalOptionStrategy` | signal logic → BULLISH/BEARISH; per-direction `DirectionRule` | own direction→structure mapping; NEUTRAL = no trade |
| `InstantLongOptionStrategy` | CE or PE (`CE_instant_buy`, `PE_instant_buy` are instances) | trades on first eligible bar; explicit opening intent; no signal theatre |
| `SpreadStrategy` | both legs, width/selection rule, exit plan | defined-risk worst case computed at entry |
| `StructureStrategy` | legs for strangle/condor-type structures, exit plan | explicit OPEN intent (never NEUTRAL); mandatory ₹ hard stop (loss uncapped by structure). *Named neutrally on purpose: a short strangle is undefined-risk, and a template name must never claim a risk property the structure may not have — the same misleading-name failure §9.2 flags in `banknifty_straddle`* |
| `EquityStrategy` | existing equity signal logic | equity path unchanged; option contract not imposed |

Templates are base classes in `src/backtest/strategy/templates/` (name bikeshed in Phase 0); a strategy customises by overriding signal/intent hooks, never by bypassing intent construction.

### 5.3 Strategy-Authoring Manual (deliverable: `docs/STRATEGY-AUTHORING.md` v2 update)

Mandatory contents:
1. Pick-a-template decision tree.
2. The non-negotiables checklist: explicit intent; NEUTRAL = silence; mandatory hard stop; declared `emits` ⊇ every direction the code can produce; sizing is a menu not a risk limit; no broker/quote/data calls from strategy code (existing C2 data-ownership rule).
3. Worked examples: `CE_instant_buy` (instant), one directional, one spread — each < 1 page, each passing conformance.
4. "Who owns what" table (strategy / Risk Gate / runner / operator) mirroring §6–§8.
5. Migration notes: how to read the pre-rewrite snapshot (§9.1) and port signal logic without porting its contract violations.

### 5.4 Conformance Battery (extended)

`src/backtest/plugins/__init__.py` conformance checks additionally refuse registration when: spec missing/invalid; `emits` ⊄ `structures`; stop-less exit plan; structure not executable; declared name collides with an already-registered name (collisions are **errors**, never silent precedence — see §9.2); strategy not derived from a template (option strategies only, post-cutover).

---

## 6. Execution: One Risk Gate, a Pure Bridge (B1, B2 — decided)

### 6.1 Decision: the gate stays — consolidated to exactly one

Real trading systems (and SEBI pre-trade risk requirements: margin, order-value/quantity/price limits independent of the order's originator) require a permission check the strategy cannot grant itself. A strategy that both decides and authorises turns any single bug into unbounded account damage. Therefore:

- **Contract validation** (authoring/registration/spawn, §5.4/§4.4): "is this strategy/config *well-formed*?" — checked once, before anything runs.
- **Pre-Trade Risk Gate** (trade time, **one module** — evolve `src/backtest/options/execution_gate.py` into it): "is *this specific trade* allowed right now?" — margin & capital, worst-case ₹ loss vs per-trade/daily limits, quantity ceilings, kill switch & runner state, paper/live & account entitlement for the structure, instrument eligibility. Coded refusals (`STRUCTURE_NOT_PERMITTED`, `MARGIN_INSUFFICIENT`, …), journaled via the existing intent journal.
- **Entitlement is account/mode property, not strategy property.** A strategy may *request* `bull_call_spread`; whether this account/mode may trade spreads is the gate's list. Spawn validates the request is *potentially* permissible for the chosen mode so the first bar is never the first refusal.

Everything else that checks-and-corrects today is **deleted**: UI hardcoded expression, bridge `DEFAULT_EXPRESSION` merge, NEUTRAL→BULLISH fallback, `_pick_strikes` silent-ATM, API quantity clamp. The bridge's `allowed_structures` parameter is henceforth **supplied by the gate's decision**, not defaulted inside the bridge.

### 6.2 Bridge becomes a pure executor (strict mode)

- Gated on `expression_version == 2` during migration; unconditional after §10 Phase 4.
- No `DEFAULT_EXPRESSION` merge in strict mode; incomplete config cannot reach the bridge because spawn refused it (§7.3).
- Strike/expiry selection uses the **existing factories** (`selector.py` `create_selector`-style factory incl. `FixedDistanceSelector`/`TargetPriceSelector`; `expiry_policy.py` policies) wired per-direction from the intent. A plan-word → implementation mapping table is a Phase-0 deliverable; in particular two-sided fixed-distance (strangle: CE +x%, PE −x%) must be verified against `FixedDistanceSelector` semantics before `immediate_strangle` is rewritten.
- Transitional shim & renames (B2, Gap #2): the bridge gains **`_transitional_direction_rule_for(view)`** — Phase 1–2 only, legacy strategies only, deprecated at birth, deleted in Phase 4 (with a test that fails if any caller remains). End-state resolution is `TemplateBase.direction_rule_for(view)` (§4.1). Existing manual-level helper `_rule_for(structure_id, level)` → **`_operator_level_for(...)`**. No other `_rule_for` may be introduced.
- A strict-mode refusal at runtime is **edge-triggered**: journal + log once per runner/reason/session, runner flagged (`OPTION_BLOCKED`), subsequent bars silent until the state clears. Exits for already-open structures are **never** blocked by entry strictness.

### 6.3 Second bridge consumer (scope decision)

`src/backtest/replay/harness.py` constructs an `OptionsBridge` directly and `engine/option_backtest_driver.py` has its own structure choice. Decision: both route through the same spec validation + gate interface as the forward path (no private mapping logic). The frozen options-backtest driver stays frozen, but gains a header note pointing at the strict path so it cannot drift back into use unnoticed.

---

## 7. Configuration Layering & Overrides (B5 — decided)

Institutional model: **ceilings above, menus below, risk-reducing-only at runtime.**

```text
1. Firm/account risk limits     (risk owner; nobody below may exceed: daily loss, per-trade max ₹, kill switch)
2. Mode/account entitlement     (which structures this paper/live account may trade — feeds the Risk Gate)
3. Strategy manifest            (ExecutionSpec defaults + allowed override menus, versioned with code)
4. Deployment selection         (playbook bundle and/or user at spawn: chooses WITHIN layer-3 menus)
5. Runtime operator control     (pause / close / tighten stop only — the capabilities that exist today; see §7.2 — can never loosen, enlarge, or widen)
```

### 7.1 Where Layer-1 limits live (Gap #4 — decided)

Layer 1 is the Risk Gate's rulebook; without a concrete home it ships hollow. Decision:

- **Home: a version-controlled YAML file, `config/risk_limits.yaml`** (new), sibling to the existing `config/risk.yaml`. Rationale: limits are *policy*, and policy changes must be reviewable diffs with an author and a reason — git gives that for free; a DB would need a change-approval workflow we do not have, and env vars are invisible, untyped, and unreviewable (env remains for secrets only, never limits).
- **Contents (v1 schema):** per-trade max loss ₹; per-runner and per-bucket daily loss ₹/%; max lots & max order value per mode; per-mode structure entitlement (`paper:` / `live:` → permitted structure sets, feeding §6.1); kill-switch defaults. It *references* — does not duplicate — the existing drawdown/daily-loss surfaces (`GlobalRiskConfig`, bucket limits in `config/forward_testing.yaml`); one value lives in exactly one file, and the loader refuses on overlap ambiguity.
- **Lifecycle:** loaded and validated once at process start (fail-closed: unreadable/invalid limits file ⇒ option runners refuse to spawn, with a coded error); its SHA-256 is stamped into every spawn snapshot (§8) so any runner's governing limits are reproducible. **Changes require restart and take effect for new spawns and new trades only** — never retroactively re-permit an open position. (A runtime-editable, dual-controlled limits API is a legitimate future need; it is a non-goal for v2 and must not be improvised into Phase 1.)
- **Phase ownership:** schema + loader + validation tests are a **Phase 0** deliverable; the Risk Gate consuming it is Phase 1; a **read-only** limits view in the UI is Phase 3. Until Phase 0 lands this file, Phase 1 must not start — the gate without its rulebook is the B1 failure reproduced.

### 7.2 Intraday operator control — scoped to what exists (Gap #5 — decided)

v2 of this plan listed "reduce" (partial quantity reduction) as an existing runtime capability. It is not one for options: the bridge today supports `set_manual_rule` (arm/re-price/clear operator stop & target levels) and full close; partial close exists only on the equity path (`can_partial_close` is `False` for option structures in `portfolio_manager.py`). Therefore:

- **In scope (existing capabilities, restated honestly):** pause runner, close position fully, tighten an operator stop (moving it only toward the safer side — this asymmetry must be enforced in the operator path if not already), never loosen/enlarge/widen.
- **Out of scope (explicit non-goal):** partial close / quantity reduction for option structures. If wanted, it is a separate feature with its own design (leg-level partial unwind, margin release, ExitPlan re-pricing on the residual) — it must not ride along inside this refactor disguised as a restatement of existing behaviour.

Rules:
- **Ceilings refuse, never clamp.** An override above a ceiling → spawn 400 naming the ceiling hit. (Deletes today's silent [1,100] quantity clamp.)
- **Overrides are field selections, not document merges.** The strategy declares the fields and ranges (e.g. `lots ∈ [1,5]`, `stop_pct ∈ [0.2,0.5]`); deployment records per-field selections. An override can change a declared field's value within range; it **cannot delete a direction rule, remove a stop, or introduce a new field**. Per-field provenance (`strategy|playbook|user`) is stored in the snapshot (§8).
- **Raising risk = new deployment.** More lots or a looser stop intraday is impossible by design; stop the runner and spawn a new one (new snapshot, clean audit trail).
- **Playbooks are demoted to what they actually are:** a named, saved bundle of layer-4 selections. They no longer outrank the strategy; a playbook selection outside the strategy's menu is a spawn refusal naming the conflict. `max_loss_per_trade` from the playbook layer survives as a layer-1/4 ₹ ceiling input to the Risk Gate — it is not dropped when a strategy spec exists (v1 gap).
- **Sizing truth:** `max_lots` in the spec is an authoring menu bound, **not** a risk control. Real sizing protection is ₹-based at the gate: worst-case loss + margin vs allocated capital, using lot size from the instrument master at trade time (never stored — NSE revises lot sizes).

---

## 8. Spawn Snapshot & Fingerprinting (B6 — decided)

At spawn, resolution (§7) produces one final config document, frozen with the runner:

```json
{
  "expression_version": 2,
  "resolved_expression": { "...": "final, complete, no blanks" },
  "provenance": { "type.BULLISH.structure": "strategy", "quantity": "user", "exit.stop_loss_pct": "playbook" },
  "strategy": {"name": "CE_instant_buy", "version": "1.0",
               "bundle_sha256": "<hash of the transitive strategy bundle, §8>",
               "bundle_files": [{"path": "plugins/strategies/ce_instant_buy.py", "sha256": "..."},
                                 {"path": "src/backtest/strategy/templates/instant_long.py", "sha256": "..."},
                                 {"path": "src/backtest/strategy/templates/base.py", "sha256": "..."},
                                 {"path": "src/backtest/strategy/execution.py", "sha256": "..."}]},
  "engine_git_sha": "<git commit the engine itself is running>",
  "risk_limits_sha256": "<hash of config/risk_limits.yaml at spawn, §7.1>",
  "config_sha256": "<hash of resolved_expression + provenance-relevant inputs>"
}
```

Rules:
- **Runners read their snapshot only.** Rehydration after restart never re-resolves from live strategy code or edited playbooks.
- **Code upgrades affect new spawns only.** Runner detail surfaces "running `name vX (code a91f…)`, catalogue latest `vY`" — drift is visible, never silent.
- **Open positions are unaffected by everything above them changing:** their `ExitPlan` was frozen into the trade at entry (§4.3).
- **Moving a runner to a new strategy version is an explicit operator action** (stop + respawn; a guided "migrate" flow is a post-v2 candidate). Never automatic.
- The declared `version` string alone is not trusted as identity — the bundle fingerprint is what cannot be forgotten by an author.
- **Bundle hash covers the transitive surface (Gap #3 — decided).** `bundle_sha256` is computed over the canonical, sorted list of `(repo-relative path, sha256)` for: the strategy's own file; every module in its template/base class MRO (`templates/*`, `strategy/base.py`); and `strategy/execution.py`. This is the minimum closure that determines strategy-side behaviour — editing a template (the single most likely edit post-rewrite, touching every inheriting strategy at once) changes the bundle hash of *all* inheritors. Engine-side code (bridge, gate, selectors) is *not* hashed per-strategy; it is covered once, for every runner, by `engine_git_sha`. The constituent `bundle_files` list is stored so any hash can be explained file-by-file.
- **A snapshot freezes configuration, not Python code — so rehydration verifies.** On runner rehydration the bundle hash is recomputed from the code actually being loaded and compared with the snapshot. Mismatch ⇒ the runner starts **PAUSED** with a visible `STRATEGY_CODE_CHANGED` state and trades nothing until an operator explicitly acknowledges (resuming on new code is then a conscious migration, auditable as such) — otherwise a template edit would silently change a live runner's behaviour while its paperwork claimed otherwise. This verification step, not the hash alone, is what makes "drift is visible, never silent" true.

---

## 9. Strategy Rewrite Workstream (New)

### 9.1 Pre-rewrite snapshot (DONE at v2 drafting)

Frozen copy of all 38 strategy files (24 plugin + 14 built-in incl. `__init__.py`):

- Location: `docs/archive/strategy-snapshot-2026-10-15/` (`plugins/`, `builtin/`, `manifest.json` with SHA-256 per file, `README.md` with inventory).
- It is **not live code**: discovery scans only `plugins/strategies/` and `src/backtest/strategies/`, so the snapshot is never imported or registered.
- **Freeze status (honest, Q4):** at v2 drafting the snapshot exists only as uncommitted files in this working tree. That is *not* yet a freeze — an uncommitted copy can be edited or lost with the checkout. **Pre-Phase-0 gate (blocking):** (a) commit the snapshot directory as a dedicated commit whose message records the manifest hash; (b) verify `manifest.json` SHA-256s recompute clean from that commit; (c) export a tarball of the snapshot directory plus its manifest to a location outside both repository checkouts (owner-chosen) and record that location + tarball SHA-256 in `docs/archive/strategy-snapshot-2026-10-15/README.md`, checking off (a)–(c) there. No Phase 0 work starts until this is done. A git tag on the snapshot commit is recommended where the hosting setup permits it.
- Originals remain live and untouched until each replacement passes §9.3; then the original file is deleted from its live directory. The snapshot makes that deletion lossless and auditable.

### 9.2 Triage of the frozen inventory (38 files → dispositions)

Each snapshotted strategy gets exactly one disposition before any rewrite code is written. Indicative triage (to be confirmed file-by-file in Phase 2 kickoff):

| Group | Files (from snapshot inventory) | Disposition |
|---|---|---|
| NEUTRAL-abuse instant entries | `plugins/atm_instant_buy.py`, `plugins/immediate_entry.py`, `plugins/nifty_instant_buy.py` | **REWRITE-SPLIT** → `CE_instant_buy` / `PE_instant_buy` (`InstantLongOptionStrategy`); originals deleted at cutover |
| Structure-as-NEUTRAL | `plugins/immediate_strangle.py` | **REWRITE** → `StructureStrategy`, explicit OPEN intent, ₹ hard stop moved into ExitPlan |
| Directional option | `plugins/ema_reversion_pob.py`, `plugins/momentum_burst.py`, `plugins/time_alternator.py`, `plugins/delta_momentum.py`, `builtin/option_directional.py`, `builtin/banknifty_straddle.py` (name vs actual behaviour to verify — it emits BULLISH/BEARISH, not a straddle) | **REWRITE** → `DirectionalOptionStrategy` (spread variants via `SpreadStrategy` where the original intent was a spread) |
| Duplicate-name built-in | `builtin/atm_instant_buy.py` (`AtmInstantBuy`, equity-signal, same name as plugin option strategy) | **RESOLVE COLLISION** — rename (equity test strategy) or RETIRE; silent registry precedence is abolished (§5.4) |
| Shadow duplicates | `outside_bar_strategy_imported.py` vs `outsidebarstrategy_imported.py`; `supertrend_strategy_imported.py` vs `supertrendstrategy_imported.py` (identical declared names) | **CONFIRM-AND-DELETE** the shadow of each pair (snapshot preserves both) |
| Imported/opaque | `v1/v2/v3_imported`, `my_supertrend_v1_imported`, `supertrend*`, `outside*` survivors | **TRIAGE**: rename + document + template-port, or **RETIRE** |
| Equity strategies | remaining plugins (`swing_*`, `vwap_ema_rsi`, `volume_oi_surge`, `hf_equity_scalp`, `one_min_scalp`, …) and built-ins (`sma_crossover`, `rsi_reversion`, …) | **PORT** to `EquityStrategy` template (mechanical; signal logic unchanged, diffed against snapshot) or **RETIRE** if unused |

### 9.3 Cutover gate (per strategy — all must pass before its original is deleted)

1. Rewritten strategy registers and passes the extended conformance battery (§5.4).
2. Signal-logic equivalence check vs the snapshotted original on a fixed candle fixture (same views/intents on the bars where the original's contract was *honest*; documented, intentional divergences — e.g. NEUTRAL entries — listed in the PR).
3. Spawn from strategy defaults alone (no playbook, no user override) produces a valid v2 snapshot and, in paper, the intended first trade.
4. Risk-Gate refusal drill: an out-of-menu override and an over-ceiling sizing attempt both refuse with the expected coded message.
5. Restart drill: runner rehydrated mid-position from snapshot; exits fire per the frozen ExitPlan.
6. Only then: delete the original from its live directory in the same PR; snapshot (§9.1) is the permanent record.

### 9.4 Sequencing within the rewrite

Risk-first: (1) `CE/PE_instant_buy` split, (2) `immediate_strangle`, (3) directional option set, (4) collisions/shadows deletion, (5) equity ports, (6) imported/opaque triage. Equity ports are last because the equity path is not implicated in the incident.

---

## 10. Migration Path (Phases)

### Phase 0 — Foundations (zero behaviour change) ✅ DONE 2026-10-15

*Delivered: `strategy/execution.py` (spec/rule/strict ExitPlan/resolve/validate), `Strategy.execution`+`execution_spec()`+validate wiring, `strategy/templates/` skeleton (5 templates, `TemplateBase.direction_rule_for`), `strategy/bundle.py` (transitive bundle hash + `STRATEGY_CODE_CHANGED` constant), `config/risk_limits.yaml` + `backtest/risk_limits.py` (fail-closed loader, overlap refusal), catalogue/API `execution` block, `RunnerConfig.strategy_bundle` + spawn-time stamping in `create_runner` (additive, best-effort), authoring-manual v2 addendum (§A–E incl. selector/expiry wiring). Tests: `tests/test_strategy_execution.py`, `tests/test_risk_limits.py`, `tests/test_strategy_bundle.py` — 36 passed; regression sweep (api/options/strategies suites) shows zero new failures vs clean tree. Remaining Phase-0-adjacent item: §9.1 freeze item (c) off-checkout tarball — owner's location still pending (gates nothing in Phase 0 code, recorded here honestly).*

*Post-verification fixes (review of 1af6fbd, all closed): catalogue test allowlist gained the intentional `execution` key; bundle display paths are POSIX-canonicalised so `bundle_sha256` is platform-independent (Linux stamp ≡ Windows rehydration); Phase 0 test files purge `phase0_*` registrations at setup+teardown (collection-time registration made a before/after diff insufficient); spawn now stamps `risk_limits_sha256` (§7.1 loader) and `config_sha256` (canonical-JSON fingerprint of the spawn expression) alongside the bundle fields; reserved bridge names recorded as a code comment at `_structure_type_for`; legacy-surface round-trip is pinned by tests and `ExecutionSpec` can declare defaults for `squareoff_minutes_before`/`max_loss_per_trade`/`stale_after_seconds` with no key loss. **Semantic decision (verifier flag):** `expiry_min_days` has one canonical home — `DirectionRule.expiry_min_days`; the top-level key is no longer selectable/declared in v2 and survives only as an opaque legacy passthrough in resolution.*
- `src/backtest/strategy/execution.py`: `ExecutionSpec`, `DirectionRule`, strict `ExitPlan` parser, `resolve_expression` implementing §7 field-selection semantics, `validate_expression` (gate-aware, §4.4).
- `Strategy.execution` + immutable `execution_spec()`; `Strategy.validate()` extended.
- Templates skeleton (§5.2, incl. `TemplateBase.direction_rule_for`) + authoring manual v2 (§5.3) drafted.
- **`config/risk_limits.yaml` schema + loader + overlap validation + tests (§7.1)** — the Risk Gate's rulebook; Phase 1 may not start without it.
- Bundle-hash computation (§8): static MRO/module closure, canonical hashing, rehydration comparison yielding `STRATEGY_CODE_CHANGED`.
- `/api/strategies` gains the `execution` block; snapshot/fingerprint stamping (§8) added to runner state (additive).
- Selector/expiry wiring table (§6.2) written; `_transitional_direction_rule_for`/`_operator_level_for` names reserved in a comment-level note.
- Tests: spec parse/canonicalisation, emits↔structures coverage, strict-vs-legacy exit parsing, resolve/provenance, round-trip **with no key loss** across the full current expression surface (`exit`, `expiry_min_days`, `squareoff_minutes_before`, `max_loss_per_trade`, `stale_after_seconds`, …); risk-limits loader (valid / invalid / overlapping keys — §7.1); bundle hash changes when a template file changes and is stable otherwise (§8).

### Phase 1 — One Gate + strict bridge (behind `expression_version == 2`) ✅ DONE 2026-10-15

*Delivered: `PreTradeRiskGate` in `options/execution_gate.py` (entitlement from `config/risk_limits.yaml`, kill switch, quantity/loss/order-value ceilings — the only runtime permission vocabulary; legacy `check_*` broker-boundary checks unchanged). Bridge strict mode gated on `expression_version == 2`: no `DEFAULT_EXPRESSION` merge, gate-aware construction refusal, strict ExitPlan→ExitConfig mapping, `_transitional_direction_rule_for` with coded edge-triggered halts (NEUTRAL = silence, never a halt, never an entry), per-direction strike selection via `create_selector` and expiry via `create_expiry_policy` (per-rule `expiry_min_days` floor), `_rule_for` → `_operator_level_for`, runner `mode` passed to the bridge. `create_runner` resolves spec'd/v2 spawns server-side (provenance stamped, out-of-menu and gate refusals are 400s; the silent quantity clamp now applies to the legacy v1 path only). Rehydration bundle comparison: mismatch → PAUSED + `STRATEGY_CODE_CHANGED`, `resume()` restamps as a conscious migration. Replay harness validates v2 expressions through the same gate-aware path. Tests: `tests/test_options_bridge_strict.py` + `tests/test_runner_create_validation.py` (21) — 341 passed across Phase 0/1 + options/api suites; forward-suite failure set byte-identical to the clean tree (fresh-venv environment). Note: two-sided fixed-distance strike picking for short strangles remains a Phase-2 item (verified selector semantics still owed there, per §6.2).*

*Phase 1 verification flags — all dispositioned before any v2 runner trades (recorded at the Phase 2 cutover gate, closed immediately): (1) 🔴 `stop_loss_price` is **refused** by strict validation until the exit engine can represent price stops — never silently dropped by the bridge mapping. (2) 🟠 Dormant ceilings **wired at entry**: `_gate_check_at_entry` computes gross BUY premium (order value) and net debit (worst case for debit structures) from live quotes pre-trade; short structures with an unpriceable worst case and no declared `max_loss_inr` refuse `WORST_CASE_UNPRICEABLE`. The shipped `per_trade_max_loss_inr` was raised ₹5,000 → ₹25,000 after the entry wiring proved one ATM NIFTY lot's premium (₹17,305) exceeded it — the gate must not refuse the MVP's own first trade. (3) 🟡 `create_runner` now treats a client-sent full v2 expression identical to the strategy default as "no selections" (envelope key stripped), matching its comment. (4) 🟡 Tests now pin strict string-form `type` and the restart-mid-position case (§9.3 #5): open v2 structure → capture/restore → identical structure id → frozen signal-flip exit still fires.*

### Phase 1 — One Gate + strict bridge (behind `expression_version == 2`)
- `execution_gate.py` evolved into the single Pre-Trade Risk Gate (§6.1); bridge consumes gate-supplied `allowed_structures`.
- Bridge strict mode: no default merge, `_transitional_direction_rule_for` with coded edge-triggered halt (legacy strategies only — §4.1), selector/expiry factories wired, `_rule_for` → `_operator_level_for` rename. Template-derived strategies bypass the shim entirely by construction.
- API `create_runner`: §7 resolution, ceiling refusals (clamp deleted), §8 snapshot persisted. Legacy v1 path untouched for old clients/state.
- Replay harness routed through the same validation (§6.3).
- Tests: strict unmapped-direction halt (once, journaled), no cross-direction fallback, gate refusal at spawn vs at trade, legacy string-form back-compat, restart-mid-position (§9.3 #5 pattern).

### Phase 2 — Strategy rewrite (per §9)
- Triage table (§9.2) confirmed; templates finalised; strategies rewritten family-by-family in §9.4 order, each passing the §9.3 cutover gate and deleting its original in the same PR.
- Conformance battery extended (§5.4) and enforced for rewritten strategies first, then globally.

*Phase 2 — completed (2026-10-16). All §9.4 steps cleared: (1) `CE_instant_buy`/`PE_instant_buy` split shipped, replacing `atm_instant_buy` (plugins + builtin), `immediate_entry`, and `nifty_instant_buy` — originals deleted, snapshot-retained; the incident is pinned as a test (identical CE strategy on NIFTY+BANKNIFTY with zero overrides spawns identically). (2) `immediate_strangle` rewritten on `StructureStrategy` with an explicit `opening_structure` and stateable worst case (stop_loss_points); short premium is paper-entitlement only in the shipped rulebook. (3) Directional set rewritten on `DirectionalOptionStrategy` with declared specs (ATM long_call/long_put, 50% stop / 100% target / signal flip): ema_reversion_pob, momentum_burst, time_alternator, delta_momentum, directional_options, banknifty_ema_momentum (renamed from the misleading `banknifty_straddle`), volume_oi_surge, vwap_ema_rsi. Note: volume_oi_surge's claimed "volume" strike selection never existed in the bridge — it silently degraded to ATM; ATM is now its declared behaviour. (4) Collisions/shadows deleted. (5) Equity ports: all 10 builtins + 6 plugins on `EquityStrategy`, signal logic untouched. (6) Imported triage: the six "imported" files were one identical Pine-supertrend conversion under six names — survivor renamed `supertrend_equity` (signal-equivalence vs snapshot pinned by test), five retired. Infrastructure: template strategies hand the bridge complete intents via `OptionsBridge.on_intent` (no fallback possible); the gate's entitlement is narrowed to each strategy's declared structures; conformance gained the execution-contract check. §9.3 cutover tests: `tests/test_phase2_rewrites.py`. **Every currently-registered option strategy now declares a spec — the §10 migration wall's actively-traded precondition is met for the registered set as of this date; Phase 3 may proceed.***

*Phase 2 verification flags — dispositioned before Phase 3: (a) **Strangle distance wiring.** `ShortStrangle` selected legs from its own `OTM_PCT = 2.0` class constant; the strategy's declared `distance_pct` never reached it (legs were right only because the values coincided). The declared distance now threads through `OptionStructure.build(..., distance_pct)` from the bridge's active DirectionRule; `OTM_PCT` remains only as the legacy default. A test opens the strangle at 2% vs 5% and pins wider legs for the wider declaration. (b) **Restore hazard.** `registry.get_strategy` raises `KeyError` for a retired/renamed strategy, and `_apply_state_payload` caught only `ValueError` per runner — one retired name in saved state (exactly what Phase 2's deletions produce) aborted the restore loop mid-way, dropping later runners and manager scalars under a misleading "starting clean" log. The per-runner catch now covers `KeyError`, skips and logs the offending runner, and the restore log reports restored-vs-saved counts. A regression test restores state whose first runner names a retired strategy and pins that the second runner and the manager scalars survive.*

### ⚠️ Migration wall between Phase 2 and Phase 3 (Gap #6 — operational consequence, normative)

Phase 3 removes the UI's default expression; §11 then refuses any option spawn that has neither a strategy spec nor valid selections. Combined effect, stated plainly: **any option strategy that has not cleared the Phase 2 cutover gate (§9.3) becomes undeployable the day Phase 3 lands.** That is the intended fail-closed behaviour — but it is an operational cliff, so it is gated:

- Before Phase 3 starts, the owner produces the **actively-traded list** (option strategies with live/paper runners, or any deployment in the trailing 30 days).
- Phase 3 may not start until **every strategy on that list** has passed §9.3. Strategies not on the list may trail (§10.1) in the knowledge that they become undeployable until rewritten — this consequence must be communicated to their owners *before* Phase 3, not discovered at spawn time.
- No exceptions and no temporary UI fallback: re-adding a default "just for one strategy" re-opens the incident.

### Phase 3 — UI
- `submitSpawn` deletes the hardcoded expression; spawn sends **field selections only** (or none → strategy defaults).
- Strategy execution summary rendered from the catalogue ("NEUTRAL → no trade · BULLISH → bull_call_spread (ATM) · stop −30% · 1 lot (max 5)").
- Override UI = menu-bounded pickers for declared fields only, with provenance labels and ceiling messages from the API (no client-side silent fixes).
- Runner detail shows snapshot fingerprints and version drift (§8).
- JS contract tests (`tests/test_options_spawn_ui.py`) updated.

*Phase 3 — completed (2026-10-16). The spawn form now sends field selections only: `submitSpawn`'s hardcoded direction-aware-spread expression is deleted (not replaced), and the second, dormant expression builder in `components/option_config.js` (`buildExpression`/`buildInstrument`, with its own hardcoded spreads) is deleted too — that module is now the read-only structure vocabulary the book views label with, pinned by `tests/js/test_option_config.mjs`. The spawn modal renders the strategy's execution summary from the catalogue (`execution_summary`) with menu-bounded override pickers for declared fields only (lots bound to the strategy's menu with provenance text; stop/target/expiry-day pickers when `exit` is in `allowed_overrides`); out-of-menu values travel to the server and return as refusals — the lots silent clamp in the submit path is gone. Playbooks travel as layer-4 selection bundles: `Playbook.to_selections()` (the playbook's per-trade rupee cap translates to the ExitPlan's enforced `max_loss_inr`, its only binding v2 home), the playbook-spawn endpoint emits `playbook_selections` for spec'd strategies, and `create_runner` accepts that field. Menu enforcement is now leaf-level: restating the strategy's own values for a non-menu field is a no-op (a playbook naming the strategy's own structures no longer refuses against itself), while any leaf that would move the value refuses. Runner detail (deep-dive Config tab) shows the §8 spawn fingerprints (bundle/engine/risk-limits/config hashes), selection provenance, and a `STRATEGY_CODE_CHANGED` drift banner; the risk page gained a read-only Layer-1 rulebook card backed by `GET /api/portfolio/risk/limits` (same fail-closed loader; no mutation verb). One latent Phase 0 bug was found and fixed on this path: the strict ExitPlan parser bounded `take_profit_pct` to (0, 1] by conflating it with `stop_loss_pct` — a premium can more than double (seeded playbook target +150%), so targets are now `> 0` while stops remain bounded. Tests: `tests/test_phase3_ui_contract.py` (selections-only spawn, provenance per layer, menu refusals, restate-is-noop, playbook bundle resolution + out-of-menu refusal, read-only rulebook, detail fingerprints) plus updated `test_options_spawn_ui.py` JS contract tests. The §10 wall precondition held: every registered option strategy declares a spec, so no actively-traded strategy became undeployable.*

### Phase 4 — Cleanup
- Delete `DEFAULT_EXPRESSION`, bridge default merge, NEUTRAL→BULLISH fallback, silent-ATM strike fallback, API quantity clamp (most already dead in strict mode; this removes the legacy paths once no v1 runners remain).
- Strict mode unconditional for option runners; legacy tolerant exit parser fenced to state-file loading of pre-v2 runners only.
- **`_transitional_direction_rule_for` deleted from the bridge** with its last legacy caller; a regression test asserts the symbol does not exist (§4.1/§6.2).
- `Strategy.stop_loss`/`take_profit` legacy attrs (equity backtest path) deprecated with a removal date.

*Phase 4 — completed (2026-10-17). The legacy option path is deleted, not deprecated. `OptionsBridge` now refuses any non-v2 expression at construction (`ExecutionSpecError`, a `ValueError` — so a pre-v2 runner snapshot is skipped-and-logged by the Phase 2 per-runner restore isolation while its peers restore, pinned by test); strict mode is unconditional (`bridge.strict` remains a constant `True` for probes). `DEFAULT_EXPRESSION` and the bridge default merge are gone; `on_market_view` manages exits of an open structure only and returns `None` when flat — NEUTRAL = silence, and a bare directional view is silence too: entries arrive exclusively as complete intents via `on_intent`, and direction→rule resolution lives only in `TemplateBase.direction_rule_for`. `_transitional_direction_rule_for`, `_structure_type_for` (and its NEUTRAL→BULLISH fallback), and the auto-reentry machinery (`_should_reenter`, the per-day reversal counter) are deleted; re-entry is a fresh strategy intent, still bounded by the same-bar exit guard (the U2.2 churn lesson) and by the one Pre-Trade Risk Gate per trade. `ExitPlan.reenter`/`max_reentries_per_day` remain declared fields — strategy bookkeeping now, not bridge policy; no registered strategy declares them. An emitted direction with no rule in the spec raises `STRATEGY_EMITS_UNDECLARED_DIRECTION` strategy-side instead of ever being bridge-mapped. `_pick_strikes` requires an active rule (no silent ATM); the DTE entry block fires on the intent path; `_select_expiry` floors at the strictest declared rule minimum. `create_runner`'s [1,100] quantity clamp is deleted together with the legacy spawn path (spec-less option spawn without a valid v2 expression refuses with the §11 remediation), and bundle stamping is fail-closed at spawn; the bundle hasher tolerates orphaned plugin classes (module evicted by re-import) instead of making the strategy unspawnable. The replay harness's default-expression copy is deleted (it resolves the strategy's spec defaults at run time; its pre-existing constructor drift is unchanged and out of scope). `ExitConfig.from_expression` is fenced as legacy (tactical-exit evaluation and tests only; the bridge never calls it). `Strategy.stop_loss`/`take_profit` carry a deprecation warning from `validate()` (removal 2027-03-31). Tests: `tests/test_phase4_cleanup.py` (absence pins, construction refusal, bare-view silence, unmapped-direction raise, pre-v2 restore skip) plus migration of ~25 suites to intent-driven entries; targeted suites and the full suite pass with the failure set identical to the pre-Phase 4 baseline (per-bar timing-budget params rotate at baseline too). Still awaiting the user's explicit sign-off, untouched: the ₹25,000 per-trade ceiling and the paper-only `strangle` entitlement in `config/risk_limits.yaml` — note one ATM NIFTY long premium (~₹28k on the synthetic chain) exceeds the ₹25k ceiling, so under the shipped rulebook ATM directional entries refuse at the gate until that decision lands.*

**Rollback (all phases):** v1 runners/state keep the legacy path until Phase 4; rewritten strategies can be reverted per-PR to their snapshotted originals (§9.1) with original behaviour restored exactly (files are byte-identical copies).

### 10.1 Minimum viable cut (decided — the incident does not wait for 38 rewrites)

The full programme and the incident fix are **decoupled**:

- **MVP (ships first):** pre-Phase-0 snapshot freeze (§9.1) + Phase 0 + Phase 1 + the `CE_instant_buy`/`PE_instant_buy` split (with `atm_instant_buy` retired via §9.3) + the Phase 3 UI change **scoped to v2-ready strategies only** (the legacy spawn path remains, visibly labelled legacy, for unmigrated strategies — and is removed per-strategy at its cutover).
- **Trailing:** the remaining §9.2 rewrite in §9.4 order. The legacy path is a *scheduled demolition*, not a permanent dual system: each strategy's cutover deletes its legacy eligibility, and Phase 4 (global strictness + shim deletion) fires when the actively-traded list (§10 wall) is fully migrated, on a named date set at Phase 2 kickoff.
- **Non-negotiable even in MVP:** the Risk Gate ships with its §7.1 rulebook (a hollow gate fixes nothing), and NEUTRAL-as-entry dies with `atm_instant_buy` inside the MVP itself — it is not a trailing item.

---

## 11. Validation Rules (normative)

| Case | Where caught | Behaviour |
|---|---|---|
| Direction in `emits` has no structure rule | Registration (conformance) + spawn `validate_expression` | Strategy un-registerable; spawn 400 naming the direction |
| Runtime view direction ∉ `emits` | Bridge strict mode | Coded halt `STRATEGY_EMITS_UNDECLARED_DIRECTION`, edge-triggered, journaled; no trade |
| NEUTRAL view, no open position | Bridge strict mode | Nothing (by definition, §4.2) |
| Structure unknown, or not permitted for this mode/account | Spawn (gate-aware validation) + Risk Gate at trade (defence in depth at the *one* gate) | 400 at spawn; coded gate refusal at trade |
| Opening intent without a hard stop / unpriceable worst case | Template intent construction + Risk Gate | Intent unbuildable; gate refusal if ever constructed |
| Override outside strategy menu, or above a ceiling | Spawn | 400 naming field, menu/ceiling, and provenance layer |
| Override attempting to delete a direction/stop | Spawn (schema) | 400 — deletions are not expressible (§7) |
| Unknown exit keys (v2) | Spawn strict parser | 400 (legacy v1 expressions: existing warn-and-skip, unchanged) |
| `delta_target` ∉ (0,1]; `fixed_distance` without `distance_pct` | Spec parse + spawn | `StrategyContractError` at load / 400 at spawn |
| Option spawn with no strategy spec and no valid selections | Spawn | 400 with remediation text |
| Duplicate strategy name (incl. snapshot collisions, §9.2) | Registration | Refused with both file paths named |

**Unifying rule:** every silent default becomes either an explicit strategy declaration or a loud, coded refusal. No third option, in any layer, including the UI.

---

## 12. Testing Strategy

- **Unit:** execution spec (parse, canonicalisation, immutability), strict ExitPlan parser, resolve/provenance (§7), expression round-trip with zero key loss, snapshot hash stability (§8).
- **Contract/conformance:** every rewritten strategy passes §5.4; collision refusal; emits-coverage; stop-less plan refusal.
- **Bridge (strict):** no-fallback, NEUTRAL table (§4.2 — all four rows), edge-triggered halt (exactly one journal entry over N bars), per-direction strike/expiry selection incl. two-sided fixed-distance, gate-refusal codes, exits never blocked by entry strictness.
- **API:** strategy-default spawn, menu override spawn, ceiling refusal (no clamping), playbook-outside-menu refusal, snapshot persisted & fingerprinted, lot-size ₹-risk check via instrument master.
- **Cutover (per strategy, §9.3):** fixture equivalence vs snapshot original, first-trade paper drill, refusal drills, restart-mid-position drill.
- **Regression:** v1 state files load and behave identically at every phase until Phase 4; NIFTY+BANKNIFTY `CE_instant_buy` with zero overrides both trade identically (the incident, as a test).
- **UI contract:** summary rendering, menu-only pickers, selections-only submit, provenance/drift display.

---

## 13. Backward Compatibility

- **State files:** v1 expressions load leniently, behave as today, exit per their original (legacy-parsed) rules. Strictness attaches to `expression_version: 2` snapshots only.
- **Playbooks:** existing snapshots keep working as layer-4 selections (§7); a playbook conflicting with a rewritten strategy's menu refuses loudly at spawn rather than silently winning.
- **API clients:** old clients sending full v1 expressions keep the legacy path during migration; new clients may send selections only.
- **Equity strategies:** unaffected except template port (§9.2, last); `execution` ignored for non-option runners.

---

## 14. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Rewrite changes signal behaviour unintentionally | Snapshot (§9.1) + fixture equivalence per strategy (§9.3 #2); intentional divergences documented per PR |
| One-gate consolidation becomes a bottleneck/god-module | Rulebook is data (limits/entitlements), gate is a pure function of (intent, account state); contract vs permission questions kept separate (§6.1) |
| Mandatory-stop rule blocks legitimate ride-to-settlement designs | Structure-capped stop form (§4.3): a bought option's worst case *is* its premium — recorded, not waived |
| Indicator-only strategies forced into unwanted hard stops | Hard stop may be set wide (author's choice, in-menu); indicator exit still fires first in the normal course — the hard stop is gap insurance, disclosed as such in the manual |
| Users confused by refusals replacing silent fixes | Every refusal names the field, the ceiling/menu, and the fix; UI renders API messages verbatim |
| Dual-system drift during migration | Legacy path is frozen (no new features), per-strategy cutover deletes originals, Phase 4 removes legacy option paths entirely |
| Strategy *or template* code edited without a version bump | Transitive `bundle_sha256` (§8) covers the template surface; a rehydration mismatch pauses the runner (`STRATEGY_CODE_CHANGED`) instead of trading new code under old paperwork |

---

## 15. Success Metrics

- Incident test passes: identical `CE_instant_buy` deployments on NIFTY and BANKNIFTY, zero overrides, identical behaviour.
- `grep` for `DEFAULT_EXPRESSION`, NEUTRAL→BULLISH fallback, and quantity clamping in the option path returns nothing.
- Every live option strategy is template-derived, conformance-passing, and has a snapshotted predecessor; zero duplicate declared names in the registry.
- Zero runtime structure refusals for v2 runners (anything refusable was refused at spawn).
- Any historical runner's behaviour is fully explained by its stored snapshot + code hash.

---

## 16. Decisions Log (B1–B6 + open questions from v1)

| # | Question | Decision |
|---|---|---|
| B1 | Keep the execution "bouncer"? | **Yes — as exactly one Pre-Trade Risk Gate** (§6.1). SEBI pre-trade checks and bug containment require permission to live outside the strategy; all *other* scattered checkers are deleted |
| B2 | `_rule_for` name collision | Existing → `_operator_level_for(...)`; bridge shim is `_transitional_direction_rule_for` (Phase 4 deletion), end-state resolution in the template (§4.1/§6.2) |
| B3 | NEUTRAL's meaning | **Silence only; never an entry.** `atm_instant_buy` split into `CE_instant_buy`/`PE_instant_buy`; §4.2 table is normative |
| B4 | Stop/target ownership | Target optional; **hard stop mandatory** in the strategy's `ExitPlan` (§4.3); strict parser for v2, legacy tolerant parser fenced to v1 |
| B5 | Override precedence | Layered ceilings + menu-bounded field selections + risk-reducing-only runtime control (§7) |
| B6 | Runner vs live code drift | Frozen spawn snapshot with `config_sha256` + strategy `code_sha256`; upgrades affect new runners only (§8) |
| v1-Q1 | `quantity` in spec or runner? | Both, asymmetrically: strategy declares default + menu range; runner selects within it; ₹-risk enforced at the gate (§7) |
| v1-Q2 | Version exit vocabulary separately? | **Yes** — `ExitPlan` carries its own schema version inside the v2 expression; `expression_version` bumps only for envelope changes |
| v1-Q3 | Plugin vs built-in name collisions | Collisions are registration **errors** naming both files (§5.4); existing collisions resolved in triage (§9.2) |
| v1-Q4 | Conditional structures (IV-dependent etc.) | **Explicit non-goal for v2.** Strategies needing regime-dependent structures express it in signal logic choosing among declared rules; no conditional-structure DSL |
| v1-Q5 | Playbook deprecation | Not deprecated; demoted to layer-4 selection bundles (§7). Revisit only if usage data shows redundancy |
| Gap #2 / Q1 | Where does direction→rule resolution live at end state? | **Template base class** (`TemplateBase.direction_rule_for`, §4.1). The bridge's `_transitional_direction_rule_for` is a Phase 1–2 shim for legacy strategies, deleted in Phase 4 |
| Gap #3 / Q2 | What does the code fingerprint cover? | **Transitive bundle**: strategy file + template/base MRO modules + `strategy/execution.py`, stored as a per-file list plus aggregate hash; engine covered separately by `engine_git_sha`; rehydration mismatch pauses the runner (§8) |
| Gap #4 / Q3 | Where do Layer-1 limits live, and when are they built? | **`config/risk_limits.yaml`** (version-controlled YAML; not DB, not env for limits). Schema + loader in **Phase 0**, consumed in Phase 1, read-only UI in Phase 3 (§7.1) |
| Gap #5 | Is intraday "reduce" in scope? | **No.** Scoped to existing pause / close / tighten-stop; option partial-close is a separate future feature (§7.2) |
| Gap #6 | Phase-3 deployability cliff | Normative gate: the actively-traded list must clear Phase 2 before Phase 3 starts (§10) |
| Gap #7 | `DefinedRiskStructureStrategy` name | Renamed **`StructureStrategy`** — a template name must not assert a risk property the structure may lack (§5.2) |
| Q4 | Is the §9.1 snapshot frozen outside both checkouts? | **Not yet** — an uncommitted working-tree copy at v2 drafting. Dedicated commit + verified manifest + off-checkout tarball are a blocking pre-Phase-0 gate (§9.1) |
| Q5 | Minimum viable cut, or full programme before ship? | **MVP first** (§10.1): freeze + Phase 0 + Phase 1 + CE/PE split fixes the incident; the 38-strategy rewrite trails in §9.4 order under a scheduled legacy demolition |

---

## Appendix A: File Touchpoints

**New:** `src/backtest/strategy/execution.py` (spec, strict ExitPlan parser, resolve/validate) · `src/backtest/strategy/templates/` (5 templates, §5.2) · `tests/test_strategy_execution.py`, `tests/test_options_bridge_strict.py`, `tests/test_runner_create_validation.py` · `docs/archive/strategy-snapshot-2026-10-15/` (DONE, §9.1)

**Modified:** `src/backtest/strategy/base.py` (execution attr/spec, validate) · `src/backtest/options/execution_gate.py` (→ the one Risk Gate) · `src/backtest/forward/options_bridge.py` (strict mode, transitional `_transitional_direction_rule_for` [legacy-only, Phase-4 deletion], renames, selector/expiry wiring, no default merge) · `src/backtest/api/portfolio.py` (§7 resolution, ceiling refusals, §8 snapshot; clamp deleted) · `src/backtest/api/strategies.py` (execution block in catalogue) · `src/backtest/plugins/__init__.py` (conformance §5.4) · `src/backtest/web/static/js/portfolio.js` (selections-only spawn, summary, menu pickers) · `src/backtest/replay/harness.py` (shared validation, §6.3) · `docs/STRATEGY-AUTHORING.md` (manual, §5.3) · `tests/test_options_spawn_ui.py` (extended)

**Deleted at cutover/Phase 4 (per §9.3/§10):** each rewritten strategy's original live file (snapshot-retained) · shadow duplicates (§9.2) · `DEFAULT_EXPRESSION` + bridge merge/fallbacks · UI hardcoded expression · API quantity clamp

## Appendix B: Glossary
- **Intent** — the strategy's complete, self-describing trade order (§4.1). **Risk Gate** — the single trade-time permission checker (§6.1). **ExitPlan** — a trade's frozen exit contract, hard stop mandatory (§4.3). **Menu** — the fields/ranges a strategy allows deployment to select (§7). **Ceiling** — an upper layer's limit that lower layers may narrow but never exceed. **Snapshot** — a runner's frozen config + fingerprints (§8). **Cutover gate** — the per-strategy checklist (§9.3) that must pass before its original file is deleted.

---

## Appendix C: Architect Q&A (binding answers, review round 2)

1. **Where does `_direction_rule_for` live at end state — bridge or template?** Template. `TemplateBase.direction_rule_for(view)` (strategy side) is the end-state owner; the bridge's `_transitional_direction_rule_for` serves legacy strategies only during Phases 1–2 and is deleted in Phase 4, with a regression test asserting its absence (§4.1, §6.2). Template-derived strategies never call it — they hand the bridge a complete intent, which is what §4.1 promised.
2. **What does the code fingerprint cover — file only, or transitive closure?** Transitive closure, minimum: the strategy file + every template/base module in its class MRO + `strategy/execution.py`, hashed as a canonical per-file list (`bundle_sha256` + `bundle_files`, §8). Engine-side code is covered once, for all runners, by `engine_git_sha`. A template edit therefore visibly changes every inheriting strategy's fingerprint — and because a snapshot freezes configuration rather than Python code, rehydration recomputes the hash and a mismatch starts the runner PAUSED (`STRATEGY_CODE_CHANGED`) until an operator consciously migrates it.
3. **Where do Layer-1 limits live — DB, YAML, env? Which phase builds them?** `config/risk_limits.yaml` — version-controlled YAML. Policy must be a reviewable diff; a DB would need a change-approval workflow that does not exist yet, and env vars are for secrets, never limits. Schema + loader + validation are **Phase 0**; the Risk Gate consumes it in Phase 1; a read-only UI view lands in Phase 3 (§7.1). Phase 1 must not start without the rulebook — a hollow gate reproduces B1.
4. **Was the §9.1 snapshot actually frozen somewhere outside both checkouts?** No — honestly, not yet. At v2 drafting it is an uncommitted working-tree copy inside this checkout, which can be edited or lost. §9.1 now makes a dedicated snapshot commit + manifest re-verification + an off-checkout tarball (location and SHA-256 recorded in the snapshot README) a **blocking pre-Phase-0 gate**.
5. **Is a minimum viable cut acceptable, or is full-programme-before-ship the intent?** Minimum viable cut, explicitly (§10.1): snapshot freeze + Phase 0 + Phase 1 + the CE/PE split (with scoped Phase 3 for v2-ready strategies) fixes the incident. The 38-strategy rewrite trails in risk order — subject to the §10 migration wall: whatever is actively traded must be migrated before the UI default is removed for it.

---

## Approval

**Reviewed by**: _________________  **Date**: _________________
**Status**: ☐ Approved  ☐ Approved with changes  ☐ Rejected
**Comments**:

*End of document (v2)*
