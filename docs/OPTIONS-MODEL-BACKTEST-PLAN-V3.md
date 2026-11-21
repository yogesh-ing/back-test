# Review: Options Model Backtest — Corrected Plan (V2)

## Overall Assessment

This is a **significantly stronger document** than V1. The corrections in §2 show someone actually walked the tree at `8f6a049` rather than guessing from the README. The pricing-feed abstraction (§3), the quarantine section (§5), and the "ride existing work, don't duplicate" principle (§7) are the right architectural calls. The operator decisions pinned at the top prevent scope creep.

That said, there are **gaps, ambiguities, and a few risks** that will bite a Junior developer mid-build if not resolved now. I'll go section by section.

---

## Section-by-Section Findings

### §1 — Framing

**Good.** "Structure/strike/exit comparison sandbox" is the right mental model. "Not a measurement of historical option P&L" is the sentence that prevents the most dangerous misuse.

**One addition:** Consider adding one sentence that names the *primary user question* this answers:

> "Does my directional signal still produce positive expectancy after theta decay, spread cost, and the Indian fee stack, when expressed as a 7-DTE option structure?"

This gives the Junior a north star for what "correct" means when they're writing tests.

---

### §2 — Verified Current State

**Excellent table.** The corrections are well-sourced (file:line). Two observations:

1. **`OptionBacktestDriver` at `:467` with `_GeneratorSettlementProvider` at `:681`** — this implies the driver already has *some* injection point for pricing. The plan says "Phase 2 = extend this driver, not rebuild," but doesn't say *what the extension looks like*. Specifically:
   - Does `_GeneratorSettlementProvider` implement an interface that `ModelBSFeed` would also implement? Or is it a concrete class that needs to be refactored into the `OptionPricingFeed` interface first?
   - Does the driver currently call `bs_price` directly, or does it go through `_GeneratorSettlementProvider`?
   
   **Recommendation:** Add a one-paragraph "Phase 2 refactoring note" that says: *"The existing `_GeneratorSettlementProvider` will be refactored to implement `OptionPricingFeed`. The driver's constructor will accept any `OptionPricingFeed`. The existing synthetic-generator path becomes a third feed (`SyntheticFeed`) for backward compatibility with the 4,849-test suite."* Without this, the Junior will stare at the driver and not know where to cut.

2. **"BANKNIFTY backfill was stopped awaiting operator go-ahead"** — this is listed under §2 as a fact, and §9.3 asks the architect for go-ahead. But the *operator* is the one who stopped it. **Clarify who decides:** is §9.3 an architect question or an operator decision? If it's the operator, move it to the "operator decisions" list at the top (as a pending item) so the Junior doesn't block on it.

---

### §3 — Core Design: Pricing-Feed Abstraction

**The interface is clean.** Two rules I'd tighten:

**Rule 1 (driver depends only on the interface):** Add an explicit constraint:

> The driver must not import `bs_price`, `quote_providers`, or any concrete feed class. It imports only `OptionPricingFeed` (the protocol/ABC). This is enforced by an `import-linter` rule or a test that asserts the driver's import graph.

Without this, a Junior will "temporarily" hardcode a `bs_price` call in the driver for debugging and forget to remove it.

**Rule 2 (feed tag everywhere):** You say "A result that cannot state its feed is a bug." Make this *mechanically* true:

> `OptionBacktestResult` (or whatever the driver returns) has a **required** `pricing_feed: str` field with no default. If it's missing, the adapter raises. The JSON serializer refuses to produce output without it. This makes the "bug" a compile-time / import-time error, not a runtime surprise.

**Model pricing corrections — all correct and important.** A few specifics:

| # | Correction | Comment |
|---|---|---|
| 1 | Spread floor ₹0.50–1.00 | Good. But make it **per-underlying** in the config: NIFTY weeklies at 22,000 have different spread dynamics than a 24,000 OTM strike. A single global floor is a simplification; a per-strike-band floor (e.g. ₹0.50 for ATM±1, ₹1.00 for ATM±2, ₹2.00 for ATM±3) is more honest. Even if you start with a flat floor, the config should support per-band. |
| 2 | `iv_premium` default 2–3 vol pts | Correct. Add a **sensitivity output**: the results should include "P&L at iv_premium = 0 / 2 / 4 vol pts" so the user sees how sensitive the conclusion is. This is cheap to compute (re-run the same bars with 3 IV offsets) and prevents the "my strategy is great" illusion from a single IV assumption. |
| 3 | VWAP settlement | Correct. **One subtlety:** on 1-min bars, "last 30 minutes" means the last 30 bars. But if the backtest ends mid-session (e.g. the user picks `to_date` that cuts off at 14:00), there may not be 30 bars. Define the fallback: "if fewer than 30 bars remain, use all available bars; if fewer than 5, use the close." |
| 4 | Exercise STT 0.125% | **Critical verification item.** §9.4 asks whether the existing fee stack covers this. I'd escalate this: **if it doesn't, it's a Phase 1 blocker, not a Phase 1 task.** You can't build the driver (Phase 2) on top of a fee stack that overstates expiry-day P&L. Make Phase 0 include: "Run the existing fee stack against a known ITM expiry settlement and confirm the STT line item is present." |
| 5 | Bar-close stops | Correct and honest. One addition: for 1-min bars, a "stop at 30% of entry premium" checked at bar close means the *actual* stop could be much worse (the bar could gap through the stop). The disclaimer should say: *"Stops are evaluated at bar close; intrabar slippage through the stop level is not modelled."* |
| 6 | Margin via `options/margin.py` | Good. **Specify the output:** "Return-on-margin" and "Return-on-premium" as two separate metric cards. Also: "Max theoretical loss per structure" for short legs (e.g. short call in a bull spread = unlimited in theory; in practice capped by the spread width + margin call). Show it as a risk note, not a metric. |
| 7 | Liquidity flagging | "Flag strikes the real master marks illiquid" — does the broker instrument master have a liquidity flag? Or is it OI/volume-based? If it's OI-based, the ModelBSFeed doesn't have OI data (it's synthetic). **Clarify:** for ModelBSFeed, the liquidity flag is *advisory only* (a warning icon on the trade table: "this strike may be illiquid in practice"). For ChainHistoryFeed (later), it's a hard filter. |

---

### §4 — UI

**Good.** Operator decisions are clear. Two additions:

1. **The "vol-regime note"** — specify what it is:
   > A one-line label below the metrics cards: *"Avg IV during backtest: 14.2% (low-vol regime). Theta effects amplified; vega effects muted."* Classification: <15% low, 15–25% normal, >25% high. This is computed from the `iv` series the driver already produces.

2. **The "Tune This" disable** — specify the exact text:
   > Button disabled, tooltip: *"Model-priced backtests cannot be certified or deployed. Run on real data to enable."*
   
   And in the API: the response for a model run includes `"certifiable": false, "certification_reason": "model_pricing"`. The UI reads this, not a hardcoded check.

3. **Missing: what happens in Compare mode?** If a user runs a model options backtest and an equity backtest on the same strategy/symbol and hits Compare, the equity curve and the options curve are on different scales (₹ equity vs. option premium P&L). **Decision needed:** either (a) Compare is disabled for mixed equity/options runs, or (b) both are normalized to % return on capital. State which.

---

### §5 — Quarantine

**This is the strongest section.** Six doors, each with a specific mechanism. The "never trust the client flag" principle is correct.

**Two additions:**

1. **The e2e regression test (D3) needs a concrete spec:**
   > Test name: `test_model_run_quarantine_e2e`
   > Steps:
   > 1. POST a model options backtest → get `run_id`
   > 2. GET `/api/backtest/<run_id>/readiness` → assert `certifiable == false`
   > 3. POST `/api/optimize` with `baseline_run_id = run_id` → assert 403 + reason
   > 4. POST `/api/portfolio/runner/create` with `preset_from_run = run_id` → assert 403 + reason
   > 5. GET `/api/compare?runs=[run_id, <real_run_id>]` → assert both curves are labelled with their `pricing_feed`
   > 6. Export the model run to PDF → assert the banner text is present in the PDF (parse or screenshot)
   
   Without this spec, the Junior will write a unit test that checks one door and call it done.

2. **Kill switch:** Add a line:
   > `OPTIONS_MODEL_BACKTEST_ENABLED` env var (default `true`). If set to `false`, the Options mode toggle is hidden entirely and the API returns 404. This is the emergency off-switch if a quarantine hole is found in production.

---

### §6 — Build Phases

**8–10 days is realistic** if the Phase 0 verifications pass cleanly. Three concerns:

1. **Phase 0 is 0.5 days but has 3 verification items.** If the fee stack doesn't cover exercise STT (item 4 in §3 corrections), Phase 0 becomes 1–2 days (fix the fee stack + tests). **Re-estimate:** Phase 0 = 0.5–2 days depending on fee stack verification.

2. **Phase 2 (3 days) is the riskiest.** Extending an existing 467-line driver with a new feed interface, margin-aware capital, and engine-tier exits is non-trivial. The 4,849-test suite must still pass. **Add a sub-milestone:** "By end of day 1 of Phase 2, the driver accepts `OptionPricingFeed` and the existing synthetic-generator tests pass unchanged. If not, stop and escalate."

3. **Phase 4 (chain capture, 1–2 days) has a hidden dependency:** it rides on Upstox V3 WS `full` mode. If that's not operational yet, Phase 4 is blocked. **State the dependency explicitly:** "Phase 4 is conditional on Upstox V3 `full` mode being in the codebase and passing its own integration tests. If not, Phase 4 is deferred to a separate ticket."

---

### §7 — Chain Data Capture

**Correct to ride existing work.** Two points:

1. **"Parity/junk filter in front of the writer"** — specify the filter:
   > A snapshot is rejected if: (a) CE and PE at the same strike/expiry violate put-call parity by more than 2× the bid-ask spread, OR (b) LTP is outside [bid, ask] by more than 5%, OR (c) IV is negative or > 200%. Rejected snapshots are logged to `data/chain_rejected.log` with the reason, not silently dropped.

2. **The `depth_json` column** — 5-level depth per strike per expiry per bar is a lot of data. For NIFTY with ~40 strikes × 4 expiries × 5 levels × 2 sides = 1,600 data points per bar. At 1-min granularity, that's ~96,000 points per hour per underlying. **Specify the storage strategy:** is `depth_json` a JSONB column (simple, queryable) or a separate `option_chain_depth` table (normalized, more complex)? For a hypertable, JSONB is simpler. But note the write volume in the estimate.

---

### §8 — Definition of Done

**Good.** Two additions:

- **D8:** "The `OPTIONS_MODEL_BACKTEST_ENABLED` kill switch works: setting it to `false` hides the UI toggle and returns 404 from the API. Verified by a test."
- **D9:** "The 4,849-test suite passes with zero regressions. Specifically: the existing `OptionBacktestDriver` synthetic-generator tests (if any) still pass after the feed-interface refactoring."

---

### §9 — Open Questions

All five are reasonable. Two comments:

1. **§9.1 (iv_premium calibration):** Add a fallback: *"Until Upstox IV data is available (expected: [date]), use a fixed 2.5 vol pts and include the sensitivity output (§3 correction 2) in every result so the user sees the range."*

2. **§9.5 (feed selector vocabulary):** This is an API design decision that should be resolved in Phase 0, not left open. **Recommendation:** `pricing_feed` is a top-level field in the request and response, separate from `source` (data source: `db`, `synthetic`, etc.) and `mode` (engine mode: `driver`, `quick_screen`). The three axes are orthogonal:
   ```
   source: "db" | "synthetic" | "mstock" | ...   (where candles come from)
   mode: "driver" | "quick_screen" | "options"    (which engine)
   pricing_feed: "model_bs" | "chain_history"     (how options are priced)
   ```
   Resolve this in Phase 0 so the API schema is fixed before Phase 3.

---

## Gaps Not Covered in V2 (Present in V1, Dropped)

| Gap | Why it matters | Suggested fix |
|-----|---------------|---------------|
| **No "Explicitly Out of Scope" section** | A Junior will see "straddle" in the strategies list and try to add it. V1 had a clear §11. | Add a short list: no straddle/strangle/iron condor/butterfly; no multi-expiry rolling; no delta-hedging; no tick-level fills; no intraday session breaks (lunch break 13:30–14:00). |
| **No signal-extraction mechanism specified** | V1 had F1.2/F1.3. V2 says "signal comes from a real strategy" but doesn't say *how* the driver gets the signal series. Does it call `run_backtest()` and extract `result.position`? Or call `strategy.generate_signals(candles)` directly? | Add to Phase 2: "The driver calls `strategy.generate_signals(candles)` directly (not `run_backtest()`) to get the raw signal series. The equity backtest is NOT run separately; the signal is the input, the option structure is the output." |
| **No `BacktestAdapter` / serialization spec** | The results have new fields (Greeks, strikes, exit reasons, itemised fees, `pricing_feed`). Does `BacktestAdapter.to_all()` handle these, or is there a new `OptionsBacktestAdapter`? | Add to Phase 3: "New `OptionsBacktestAdapter` in `adapters/` that extends the equity adapter with `greeks_curve`, `trade_legs`, `pricing_feed`, `disclaimer`. The equity adapter is unchanged." |
| **No mention of `plugins/strategies/`** | The README says plugins are auto-loaded. Should they appear in the strategy dropdown for options mode? | Add to §4: "Strategy dropdown includes all registered strategies (built-in + plugins), same as equity mode. No filtering." |
| **No rollback plan** | If a quarantine hole is found in production, what's the response? | Add to §5: "Kill switch: `OPTIONS_MODEL_BACKTEST_ENABLED=false` (env var). Rollback: revert the migration (025) + the API/UI changes. The `OptionBacktestDriver` refactoring is additive (new interface, old path preserved), so reverting the UI/API is sufficient." |
| **No mention of `request_id` logging** | The README says every request gets an id. The options backtest should follow this. | Add to Phase 3: "The new endpoint follows the existing `request_id` pattern. Errors quote the id in the response." |
| **No data-coverage validation spec** | §2 says "Validate coverage per run and refuse with the stored-range message." But what's the exact check? | Add to Phase 2: "Before the loop starts, call `DBSource.get_candles(underlying, from, to, '1min')`. If the returned DataFrame is empty or has < 50% of expected bars, return 400 with: 'Insufficient 1-min data for {underlying} from {from} to {to}. Stored range: {min} to {max}. Use the Data tab to fetch.'" |

---

## Minor / Stylistic Issues

1. **§2 table:** "Migrations at 018, new tables 019–021" → "Head is **024**" — good correction. But the README (which the Junior will read) still says 018. **Add a note:** "The README's migration number is stale. The actual head is 024. Do not trust the README for migration numbers; check `migrations/versions/`."

2. **§3, `LegQuote` dataclass:** You list `source_tag` as a field. Good. But also add `bar_time: datetime` so the trade log can show *when* the quote was generated (important for debugging determinism issues).

3. **§6, Phase 1 estimate (2 days):** Includes "≥15 pricing tests incl. the V1 Appendix-A hand-calc (recomputed with corrected lot/settlement)." The V1 Appendix A used lot_size=25 and close-price settlement. With the corrections (broker-master lot size, VWAP settlement, exercise STT), the hand-calc numbers change. **The Junior needs the corrected hand-calc.** Either include it in the doc or assign it as a Phase 0 task: "Recompute Appendix A with: (a) lot size from broker master, (b) VWAP settlement, (c) exercise STT. The new numbers are the test oracle."

4. **§7, "migration 025":** If Phase 4 is deferred (Upstox not ready), migration 025 is also deferred. Phase 3's migration (for `pricing_feed` column on `backtest_runs`) needs a number. **Assign now:** migration 025 = `backtest_runs` schema change (add `pricing_feed`, `model_params` JSONB); migration 026 = `option_chain_history` (Phase 4, deferred).

---

## Verdict

**The plan is 90% ready for handoff.** The architecture is sound, the corrections are well-sourced, and the quarantine section is exactly what this system needs. The remaining gaps are mostly *specification-level* (exact test specs, exact API field names, exact error messages) rather than *architecture-level*.

**Before handing to the Junior, I'd add:**
1. The "Explicitly Out of Scope" list (5 bullets, 2 minutes to write).
2. The signal-extraction mechanism (one paragraph).
3. The corrected hand-calc numbers (or assign as Phase 0 task).
4. The e2e quarantine test spec (the 6 steps I outlined above).
5. The three-axis API schema (`source` / `mode` / `pricing_feed`) resolved in Phase 0.
6. The kill switch line in §5.
7. Migration number assignments (025, 026).

With those additions, the Junior can start Phase 0 on Monday and not hit a wall by Wednesday.

**One final note on tone for the handoff:** The document is written for an architect. A Junior developer reading it will be intimidated by the density and the "do not re-litigate" language. I'd add a one-paragraph "How to read this doc" at the top:

> "You are the builder, not the decision-maker. The architecture is settled. Your job is to: (1) verify §2 in your environment, (2) build Phases 1–4 in order, (3) run the tests, (4) flag any discrepancy between this doc and the actual code *before* you start coding, not after. If you find a gap, stop and ask. Do not guess."