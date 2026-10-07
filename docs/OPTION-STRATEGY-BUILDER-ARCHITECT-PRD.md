# PRD — Option strategy authoring and paper → live rollout

**Version:** 2.1 (supersedes v2.0 of 2026-10-06) · **Status:** Phase 0 delivered 2026-10-07 (§1.1); sign-offs A and B granted; **Phase 1 authorized** (operator, 2026-10-07) — Phases 2–4 authorized only per gate exit (§5)

**Revision note (v2.1).** Phase 0 contract audit executed against the code at `main` 063dbf2. Headline results: the paper/live decision-logic duplication feared in v2 §1 does **not** exist (one decision path already lives in `OptionsBridge`); but several capabilities the PRD described as "reuse these" were verified **not present** — the single order gateway, the engine-level structure whitelist, cross-the-spread paper fills, timestamp-based freshness SLA, no-substitute contract validity, and the append-only journal. These move from "audit assumptions" to "Phase 1 build list" (§1.1). `forward/execution_engine.py` is production-dead — the live seam is `OptionsBridge` called directly by `paper_runner`.

**Revision note.** v2 changes from v1: exit mechanics specified (new decision 7); paper fill and mark policies specified; transaction cost model added; time authority and session calendar specified; all seven architecture decisions resolved or scoped to Phase 0 calibration; acceptance gates quantified; test matrix expanded (engine-level structure whitelist, version-bound live enablement, append-only journal, parallel paper/live decision diff, regulatory drift drills); Review-screen disclosures added; Pine import gate made CI-reproducible; sign-off split into three separable approvals.

---

## Decisions requested (three separable sign-offs)

**A. Architecture direction** — one trading platform; a dedicated options authoring workflow inside it; one portfolio across instruments; phased dependency gates with paper preceding live. *Endorsed in review R1.*

**B. V1 product contract** — this document, including the resolved decisions (§3) and execution semantics (§4). Approval of this document approves the contract.

**C. Phase implementation authorization** — granted per phase at each gate exit (§5), never in advance. Approval of this document authorizes **Phase 0 only**. *(Recorded: A and B granted; Phase 0 delivered §1.1; Phase 1 authorized by the operator on 2026-10-07. Phases 2–4 remain gated.)*

---

## 1. Why this fits the current code

- `templates/option_strategy_template.py` already expresses the separation: `generate_market_view(candles) -> MarketView | None` emits bullish/bearish conviction; the bridge owns chain, strike, structure and orders. Strategies must not import broker/data modules (`docs/STRATEGY-AUTHORING.md`).
- **Decision seam (corrected in v2.1):** `src/backtest/forward/options_bridge.py` is the seam — it is called directly by the paper runner and owns both entry decisions (`on_market_view`, `options_bridge.py:274`) and exit decisions (`on_bar`, `options_bridge.py:757`). `src/backtest/forward/execution_engine.py` exists but is **production-dead**: nothing on the options path calls it. `src/backtest/playbooks/models.py` captures structure, strike, quantity and exit configuration; `src/backtest/options/{paper_trading,live_trading}.py` are the two execution adapters. **Reuse the bridge; do not invent a parallel order stack, and do not revive `execution_engine.py`.**
- **Portfolio is common to every strategy and instrument.** Equity and option fills feed the existing portfolio/segment accounting and risk controls, preserving paper/live capital boundaries. Options may carry instrument-specific execution state, but not a second authoritative portfolio, capital pool, or risk total. The portfolio exposes consolidated cash, realized/unrealized P&L, exposure and emergency flatten across equity and options; each instrument's execution book reconciles into that shared view.
- `docs/OPTIONS-FORWARD-TESTING.md` records the forward plumbing as done, **but** `docs/OPTIONS-PAPER-LIVE.md` says broker-data paper runs have not been restarted after the 2026-09-30 wipe. **Code completion is not operational proof.**
- Historical options backtests are disabled (`docs/OPTIONS-BACKTEST-PRD.md`, `src/backtest/api/backtest.py`): the DB lacks historical chains. No option backtest will be advertised and no synthetic-chain P&L will be shown as evidence of edge.
- The Pine converter is intentionally limited: v5/v6 subset; indicator scripts are not orders; persistent `var` / `:=` logic is rejected; the validate endpoint returns `SYNTAX_ONLY` without an engine. Launch is not coupled to arbitrary Pine conversion.
- **Phase 0 dedup audit — verdict (was v2's open item):** `paper_trading.py` and `live_trading.py` do **not** duplicate decision logic. Both are thin adapters that execute a structure the bridge already chose; every entry/exit decision is made once in `OptionsBridge` (`on_market_view` `options_bridge.py:274`, `on_bar` `options_bridge.py:757`) via `ExitPolicy.evaluate` (`options/exit_policy.py:294`). **No dedup prerequisite.** The remaining gap is the *gateway around* that decision path, not a second copy of it (§1.1).

### 1.1 Phase 0 audit findings (delivered 2026-10-07, against `main` 063dbf2)

Each §1 / §3 / §4 claim checked against code. "Present" means the mechanism exists **and is on the options execution path**; otherwise it is listed as Phase 1 build work.

| # | PRD claim | Verdict | Evidence / gap |
|---|---|---|---|
| 1 | One decision path | ✅ **Present** | `OptionsBridge.on_market_view:274` + `on_bar:757`; adapters do not decide. §3.1 prerequisite satisfied. |
| 2 | Single order gateway holding portfolio ledger + restart reconciliation | ❌ **Not present** | Runner constructs `OptionsBridge` directly (`forward/paper_runner.py:1095`) and calls the adapters; no gateway layer exists. **Build.** |
| 3 | Engine-level structure whitelist (long CE / long PE only) | ❌ **Not present** | `STRUCTURES` accepts `bull_call_spread`, `bear_put_spread` and `strangle` → `ShortStrangle()` (`options_bridge.py:100-109`), i.e. **short-option intents are executable today**. `DEFAULT_EXPRESSION` itself maps to spreads (`options_bridge.py:125-129`). **Build — §6 forged-intent tests.** |
| 4 | Cross-the-spread paper fills (buy ask / sell bid), never LTP | ⚠️ **Built, opt-in — off by default** | `BidAskQuoteProvider` (`quote_providers.py:544`) does exactly this via `execution_quote`, but only when a book is explicitly wrapped; unwrapped books "fill at LTP exactly as before" (`:552`). Its `get_quote` is a **passthrough**, so MTM/Greeks/dashboards keep marking off LTP — §4.2's mark = bid is not in effect. It also **fail-opens to LTP** when bid/ask is missing/zero (`:554-556`), where §4.1 requires a halt. **Build: wrap by default on the runner path, mark off bid, fail closed.** |
| 5 | Timestamp-based freshness SLA | ⚠️ **Partial — wrong clock** | A halt gate already exists (`paper_runner.py:2133-2147` blocks entries while `OptionsBridge.mark_stale`), but staleness is **failed-mark bar count** (`mark_stale` `options_bridge.py:551`, `OptionPosition/Structure.is_mark_stale` `paper_trading.py:124`/`:282`, `stale_after_bars`) — not elapsed exchange time. `mark_ts` (`options_bridge.py:544`, ISO-8601 of the last successful mark) is the right hook: keep the gate, change the metric to §8.1 thresholds. **Build.** |
| 6 | No-substitute contract validity, chain-derived step / lot size | ⚠️ **Partial** | Entry has a real halt: `_entry_dte_block` refuses a structure the expiry rule would immediately close (`options_bridge.py:333`, `:1540`). But expiry *selection* still substitutes — when no expiry satisfies `expiry_min_days` it falls back to the unfiltered nearest (`:1529-1538`) — and there is no fresh-quote gate per selected strike with distinct rejection codes. **Build: remove the substitution, add per-strike fresh-quote gate + chain-derived step/lot.** |
| 7 | Exit precedence: emergency > risk caps > DTE/session > stop/target/flip | ⚠️ **Wrong tiering** | `ExitPolicy._risk_decision` fires stop → target → **DTE last** (`exit_policy.py:345-382`), and there is **no risk-cap tier** inside it. `forward/execution_engine.py` (with `EXIT_PRECEDENCE`, `evaluate_emergency_exit`) is **production-dead** — only its own unit tests import it (`tests/engine/test_exit_precedence.py:101`, `:212`). **Build: move DTE above stop/target, add the risk-cap tier on the options path.** |
| 8 | Append-only intent journal + idempotency | ❌ **Not present** | Options persistence covers trade structures/chain snapshots (`options/persistence.py`, `options/chain_snapshots.py`) — no append-only intent/fill journal, no idempotency key. **Build.** |
| 9 | Blind-retry hazard | ❌ **Present defect** | `_submit_leg_with_retry` retries on any `MStockOrderError` (`live_trading.py:361-373`) without distinguishing "submit outcome unknown", violating §6 "never retry an uncertain order blindly". **Fix in #8.** |
| 10 | Versioned statutory fee stack in paper **and** live | ⚠️ **Lives, but unwired for options** | `fees.py` implements the NSE stack for equities; the bridge builds its broker without a `fee_calculator` (`options_bridge.py:157`). **Wire + unit-test.** |
| 11 | Portfolio is common across instruments | ⚠️ **Partial** | `paper_runner.flatten_all` does not close option structures (emergency flatten gap). **Build.** |
| 12 | Re-entry default false | ⚠️ **Config exists, default differs** | `ExitPolicy.should_reenter` (`exit_policy.py:278`) honours `config.reenter`; verify the runner/builder default is `False`. **Verify in build.** |
| 13 | Pine converter limits (§7) | ✅ **Present** | v5/v6 subset; `var` / `:=` rejected; validate endpoint syntax-only (`pine/api.py:96`). No change needed. |
| 14 | No historical options backtests | ✅ **Present** | `CHAIN_SOURCES = ()` and the API gate hold; synthetic chain profiles stay test-only. |

**Consequence for sequencing.** Phase 0 is complete as an audit; its deliverable is this table plus the Phase 1 build list below. Nothing in §3's *decisions* changes — only the §1 claim that most of the machinery is already on the path.

**Traced seam (the §5 Phase 0 diagram, linear form):**

```
strategy.generate_market_view(df)            forward/paper_runner.py:2121
  → OptionsBridge.on_market_view(view, name)  paper_runner.py:2164 → options_bridge.py:274
      ├ expiry      _select_expiry (:1514)  + entry guard _entry_dte_block (:333/:1540)
      ├ strike      ATMSelector / DeltaSelector (:83, options/selector.py)
      ├ structure   STRUCTURES registry (:100)  ← whitelist target (§1.1 row 3)
      └ TradeIntent/OptionLeg built (:1844)
  → OptionPaperBroker.execute_structure(intent, quote_provider)  options_bridge.py:1779
      → paper_trading.py:414          (paper adapter)        ‖ live_trading.py:232 (broker adapter)
  → exit decisions, per bar           OptionsBridge.on_bar (:757) → ExitPolicy.evaluate (exit_policy.py:294)
  → portfolio roll-up                 paper_runner.py:2823 (unrealized) / :2839 (premium at risk)
```

Two facts fall out of the trace. Entry and exit decisions are made in **one place** (the bridge) — §3.1's premise holds. And there is **no layer between the bridge and the adapters** that could hold the gateway duties of §3.1 (journal, idempotency, portfolio ledger, restart reconciliation): `paper_runner.py:1095` constructs the bridge and calls through to `execute_structure` directly. That absence, not duplicated logic, is Phase 1's real work.

**Phase 1 build list (order is dependency order, not calendar):**

1. Structure whitelist at the execution gate + forged-intent tests (#13 in the tracker). **Delivered 2026-10-07:** `src/backtest/options/execution_gate.py` is checked in `OptionsBridge._execute`, `OptionPaperBroker.execute_structure` and `LiveOptionTrader.execute_structure`, defaulting to `{long_call, long_put}`; `DEFAULT_EXPRESSION` now maps BULLISH→long_call / BEARISH→long_put; multi-leg mechanics are reachable only through an explicitly requested `RESEARCH_STRUCTURES` profile (synthetic backtest driver, research tests). Proofs: `tests/test_options_execution_gate.py`.
2. Cross-the-spread fills + conservative mark policy wired into the paper runner path (#14).
3. Timestamp freshness SLA replacing bar-count staleness for decisions (#15).
4. No-substitute contract validity with chain-derived strike step and lot size (#16).
5. Exit-precedence tiers corrected (risk caps added; DTE above stop/target) (#17).
6. Append-only intent journal + idempotency keys; no blind retry on uncertain submit (#18).
7. Statutory fee stack active in the options bridge, paper and live identically (#19).
8. Single order gateway around the existing decision path; `flatten_all` closes option structures; re-entry default `False` proven (#20).

Then, and only then, the §5 Phase 1 soak (≥ 20 trading days, ≥ 40 intents, ≥ 10 each CE and PE).

---

## 2. V1 product contract

User chooses **NIFTY or BANKNIFTY**, a supported signal preset (initially EMA trend/cross; more only after signal-level testing), timeframe, and one playbook: bullish → **long call**, bearish → **long put**. Explicitly distinguished from `strategy.short` on the underlying. One open structure per runner; no simultaneous CE+PE, partial exits, user-supplied executable Python, multi-leg authoring, or arbitrary Pine in V1. The engine may support wider structures; the builder intentionally offers fewer — and the execution gate independently enforces the narrower set (§6).

**Form sections:**

1. **Signal:** preset, parameter ranges, entry condition, neutral/flip behavior. Defaults decided: **neutral = hold existing position** (stop/target/DTE manage it); **flip = exit current leg at market, remain flat, entry only on the next confirmed signal bar**; no same-bar re-entry; re-entry default false (existing precedence and churn findings in `docs/ARCHITECTURE-UNIFIED-TRADING.md`). Exact plain-language preview with indicator warm-up and bar timing.
2. **Instrument & expression:** underlying, expiry policy, ATM selector, lots. Lot size and contract resolved at execution time from broker data — **never hardcoded** (lot sizes, weekly-expiry availability and expiry calendars for Indian index derivatives have changed repeatedly; the expiry policy is derived from the live chain, not a fixed weekly/monthly assumption). Reject missing chain/quote, stale quote, invalid or near-expiry contract; no synthetic-quote substitution.
3. **Risk & exits:** playbook stop/target, DTE square-off, flip, per-trade risk cap, capital segment, maximum concurrent exposure. **Stop units are premium % by default** (underlying-point stops cannot protect against IV/theta); target may be a premium multiple or underlying points; units always displayed explicitly. Exit precedence: emergency flatten/breakers > risk caps > DTE/session square-off > stop/target/flip.
4. **Review:** immutable versioned signal configuration and playbook snapshot; resulting expression on a sample real chain (preview only, never an order); assumptions and source/quote badge. Save as **draft**. No Sharpe threshold or performance gate. **Mandatory disclosures, plain language:** (a) *a correct directional signal can still lose on the option — premium decay (theta) and volatility (IV) move against long options independent of direction; paper results validate mechanics, not strategy quality;* (b) *exits are engine-monitored and fill at market — realized exit prices can be materially worse than trigger prices on gaps* (measured slippage is reported in the Phase 1 soak).

**Definition and storage.** Validated declarative definition: `{schema_version, signal_type, params, underlying, playbook_id, playbook_version, segment}`. Compiled to a fixed, audited signal implementation; no `exec` of user expressions, no arbitrary generated code. Parameters snapshotted at runner start; edits create a new version and never mutate a running strategy. **Storage decision (resolved):** builder definitions persist as **first-class typed, immutable, versioned records** referencing `playbook_id + playbook_version` as a snapshot. This is dictated by the no-mutation invariant; migration cost informs sequencing only.

---

## 3. Architecture decisions (resolved)

| # | Decision | Resolution |
|---|---|---|
| 1 | Authoritative seam | **One decision path** (signal → contract selection → risk → intent) with two execution adapters (paper simulator, broker) behind a single order gateway. Portfolio ledger and restart reconciliation live in the gateway, not the adapters. **Phase 0 verdict: the single decision path already exists** in `OptionsBridge` (§1.1 row 1) — what is missing is the gateway around it, which is Phase 1 build work, not a dedup refactor. |
| 2 | Structures | **Buy CE / buy PE only**, ATM strike (±1 ladder offset permitted by Amendment A-1, builder design doc §0 — same failure matrix, still one leg long premium), single expiry policy. Max loss per structure = premium paid + fees, full stop. No spreads, no short options, no multi-leg in V1 — each multiplies the failure matrix (SPAN/margin, leg-level partial fills, combo-vs-leg exit semantics) before basics are proven. |
| 3 | Freshness / stale halt | Tiered mechanism decided now: **fresh → warn → halt new intents**; held positions marked conservatively (§4.3). Thresholds calibrated from Phase 0 measurements (§8). No valid expiry/strike (below min DTE, or no strike within tolerance of reference): **halt with explicit reason, never substitute.** |
| 4 | Flip and stop units | Per §2.1: flip exits at market and waits for the next confirmed signal bar; neutral holds. Stops in premium %; targets premium multiple or underlying points; DTE as time stop. Fill-price reference: cross-the-spread execution price, logged. |
| 5 | Live release gate | Fully numeric operational criteria plus parallel paper/live decision diff, executed kill-switch drill, ops runbook, two-person sign-off — §5 Phase 3. No Sharpe threshold. |
| 6 | Storage | First-class typed, immutable, versioned records (§2). |
| 7 | Exit mechanics *(added in review)* | **Engine-monitored exits; market orders on trigger; no exchange-side resting orders in V1.** Stops/targets evaluate on fresh quote snapshots for the held contract (same freshness SLA as entry); DTE and session-end square-off are calendar events. Consequences accepted and documented: gap/slippage risk is a disclosed V1 limitation, measured in the soak; restart needs no open-order reconciliation because no orders rest at the exchange. Exchange-side resting orders are a deliberate future option, not an omission. |

---

## 4. Execution semantics

**4.1 Paper fill policy.** Paper entries and exits fill **cross-the-spread** against a timestamped quote snapshot: buy at ask, sell at bid. **Never fill at LTP** — option last-traded prices are routinely stale. A comparison profile (mid + fixed slippage) may be configured for research runs, labeled as such, never the default. The decision-time quote must pass the same freshness SLA as live; stale quote at decision → halt, not simulate. Paper models lot-granular partial fills at minimum on exits, and the partial-fill path is exercised deterministically in tests. Every simulated fill is labeled simulated in journal and UI; **fill parity between paper and live is explicitly not an invariant — decision parity is.**

**4.2 Mark policy.** One policy, used everywhere (MTM, exposure, risk caps): for long option positions, mark = **bid when fresh**; when the quote is stale beyond the mark threshold, mark = min(last fresh bid, last traded − haircut), haircut calibrated in Phase 0. Conservative marks, applied consistently; no instrument-specific special cases.

**4.3 Transaction cost model.** A per-order, broker-specific, **versioned fee schedule active identically in paper and live**: brokerage, STT (sell-side premium), exchange transaction charges, SEBI turnover fees, GST, stamp duty (buy-side). The free-cash pre-trade check covers premium **plus estimated fees**. P&L attribution decomposes **entry premium / exit premium / fees**, and each trade journal records holding period and underlying move so premium-vs-underlying divergence (theta/IV bleed) is visible in soak review. Fee-schedule changes are data updates with a test, not code edits.

**4.4 Time authority and session calendar.** The exchange timestamp is authoritative for bar close, quote freshness and staleness measurement — never local receive time. Server clocks are NTP-synced with a max tolerated skew (calibrated, proposed ≤ 250 ms) enforced at startup and per session. Session calendar, holidays, expiry schedules and broker auto-square-off windows are **versioned data, not code constants**; the engine treats expiry-day close and square-off windows as calendar events.

---

## 5. Delivery sequence and acceptance gates

Dependency gates, not calendar promises. **Paper readiness precedes live; builder UI is not execution readiness.** Where criteria are numeric, soak duration extends if the market does not cooperate — **criteria never relax.**

| Phase | Deliverable | Exit criterion / proof |
|---|---|---|
| **0. Contract audit & calibration** *(delivered 2026-10-07 — §1.1)* | Traced `MarketView → playbook → OptionsBridge → paper/live broker → portfolio` seam; failure matrix; dedup verdict on paper/live decision logic; **measured** quote-latency distribution (REST vs websocket), broker API rate limits; proposed calibrated numbers for §8 | Architect-reviewed artifacts. No assumed readiness from docs. **Delivered verdict:** decision path single ✅, gateway/whitelist/fills/SLA/journal ❌ → Phase 1 build list (§1.1). Quote-latency and rate-limit measurement is the remaining §8 item and runs during the Phase 1 soak. |
| **1. Paper reliability** | Real broker-sourced chains/quotes in paper; contract/lot verification; fills/rejections; independent MTM and close/expiry accounting; restart recovery; segment risk; fee model live | **(a)** ≥ 20 consecutive trading days of market-hours soak spanning ≥ 1 full expiry cycle per underlying. **(b)** ≥ 40 executed intents, both CE and PE represented (≥ 10 each). **(c)** 100% intent→fill→journal reconciliation; zero unexplained cash breaks above fee-rounding tolerance (§8); zero unrecovered restarts. **(d)** Every failure class observed or injected ≥ 1×: stale-feed halt-and-recover, broker reject, partial fill, restart mid-position, expiry-day square-off, competing runners on one segment, funds-insufficiency reject, **lot-size change drill**, **expiry-calendar change drill**. **(e)** Slippage report: realized paper fills vs decision-snapshot mid, distribution by exit reason. Stale/missing data halts rather than simulates. |
| **2. Guided builder MVP** | Typed config, exact rule preview, supported preset → `MarketView`, linked versioned playbook, draft and paper launch | Golden-bar signal tests; draft and paper produce identical rules; unsupported combinations rejected; **structure whitelist at the execution gate proven against forged short/multi-leg intents**; preview disclosures (§2.4) render; no live button enabled by a syntax-only check. |
| **3. Live controlled rollout** | Same signal/playbook contract on the broker-backed adapter; manual enable, 1 lot, kill switch | **All of:** broker sandbox order-state machine tests passing (ack/fill/partial/reject/timeout/duplicate/restart); **parallel paper/live decision diff over the pilot with zero divergences** (any divergence is a release blocker); pilot capped at 1 lot per runner with daily-loss circuit breaker; **kill-switch drill executed** — including restart mid-position and emergency flatten — not merely documented; ops runbook exists (break-glass, incident classes, escalation); two-person sign-off recorded in the audit journal; **live enablement bound to the exact (signal config version, playbook version, engine version) tuple — any bump resets live evidence to paper-only.** |
| **4. Optional Pine import** | Pine as a *suggestion* for supported signal fields; explicit user review; no direct deployment | Comparison gated on **recorded exported Pine output fixtures** (CI-reproducible), not live conversion. Mapping restricted to indicators whose semantics match a preset exactly (EMA cross: yes; `:=` state/history: blocker, shown as such, never guessed). |

---

## 6. Critical invariants / test matrix

- **One source of truth:** strategy expresses directional intent; expression resolves broker-supplied contracts; execution owns fill state. A PE buy is a long put — not a short underlying, not a naked short option. All fills and positions roll up into the **same portfolio across strategy types** under existing segment and paper/live separation; option execution books are not separate portfolios.
- **Engine-level structure whitelist (new):** the execution gate accepts long CE / long PE only, from **any** source — builder, API, imported config. Short-option and multi-leg intents are rejected at the gate. The playbook layer being wider than the UI is not a defense; this is defense in depth, tested with forged payloads.
- **No lookahead:** signals use trailing candles; action follows next-bar policy; historical and forward signal timing match on identical bars; bar timestamp, candle source and decision version logged.
- **Paper/live parity of decisions, not fabricated fill parity:** same signal, selector, risk and exit decisions under identical input snapshots; fills labeled by source. **Never infer a fill from order submission.**
- **Ordered pre-trade checklist (new):** freshness → contract validity → risk cap → exposure cap → free cash (premium + fees) → submit. Each rejection is a distinct, logged, testable reason.
- **Append-only journal (new):** intent/fill/exit journal is append-only; portfolio state is derivable by replay; restart recovery = replay + broker reconciliation. Provably, not hopefully.
- **Failure safety:** stale chain/LTP, absent contract, feed loss, session expiry, risk-cap breach, broker reject, partial fill, uncertain timeout, restart and emergency flatten all have specified halt/reconcile behavior. Idempotency keys per intent; broker order IDs recorded; **never retry an uncertain order blindly.** Expiry-day square-off failure: for long cash-settled index options, **hold to cash settlement is the defined fallback** — stated and tested.
- **Test levels:** pure signal golden vectors; contract selection against recorded chains; exit-precedence/property tests; paper ledger/cash invariants; mock broker state machines; supervised real-feed soak; limited live pilot with manual sign-off. Both CE and PE, expiry rollover, gaps, competing runners on one segment, funds-insufficiency rejection, and regulatory drift drills.
- **Observability:** intent ID, config/playbook versions, contract token, quote timestamp/source, requested and filled lots, broker order ID, risk decision, exit reason, slippage vs trigger, reconciliation state. Builder config validated server-side only; **live-enable and kill-switch actions attributed to a named operator** in the audit journal. No credentials in logs.

---

## 7. Non-goals and trade-offs

- **No second platform:** it duplicates feed, broker session, portfolio, risk and operational controls and creates divergent paper/live logic. A separate options workspace inside the same app is fine.
- **No historical options performance claim:** chain history is missing. Synthetic chains exercise mechanics in a testing profile only — never as backtest evidence or paper fills.
- **No broad Pine transpilation in the critical path:** the supplied CE/PE v6 example is an `indicator` whose plots/SL lines are not orders; the consecutive-bars example relies on `:=` history. Their true trade lifecycle requires a user-defined execution contract. The form can represent a constrained equivalent after human review; it must not silently rewrite either script.
- **No profitability gate:** Sharpe belongs to research results. Correct conversion/execution is verified with signals, contracts, state transitions and accounting — never a minimum return.
- **No exchange-side resting orders in V1** (per decision 7): engine-monitored exits only; gap risk disclosed and measured. Resting orders are a future option requiring open-order reconciliation design.
- **No spreads / short options / multi-leg in V1** (per decision 2).

---

## 8. Open items pending Phase 0 calibration (numbers, not decisions)

The mechanisms are decided (§3–§4); only these values await measurement:

1. Quote freshness warn/halt thresholds per underlying, from the measured latency distribution.
2. Broker API mode (REST polling vs websocket) and rate limits → the feasible SLA.
3. Stale-mark haircut %.
4. Cash reconciliation tolerance (fee-rounding), proposed ≤ ₹1 per order or 0.05% of premium, whichever greater.
5. Max tolerated NTP skew (proposed ≤ 250 ms).
6. Minimum DTE and strike tolerance for contract validity.

---

## 9. Sign-off

Approve **A** (architecture direction) and **B** (this V1 contract) as one action; **C** (phase authorization) is granted per gate. Phase 0 is authorized on approval of this document; Phases 1–4 are authorized only on prior gate exit per §5, recorded by a named operator in the audit journal. This document is intentionally smaller than a universal strategy builder and more directly supports the high-value option paper/live goal; **scope pressure against §7 during implementation is escalated, not absorbed.**




