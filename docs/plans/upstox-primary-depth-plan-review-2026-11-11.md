# Review — `upstox-primary-depth-plan.md` (architect + quant research analyst)

**Reviewer note:** every load-bearing claim in §4 of the plan was re-derived against the code
in *this* checkout (`arena/16087b3a-back-test` @ `75b7cc8`), not taken on trust. Line numbers
below are this tree's; they match the plan's citations closely enough to confirm the plan was
written against real code.

**Verdict (first pass, plan @ `75b7cc8`): approve Phase 0 to start, subject to the five
amendments in §6.** The plan's risk ordering (gate before socket, provenance before comparison,
shadow before enforce) is correct and matches how D/F were introduced. The open questions Q1–Q4
all have right answers already sketched in the plan; my recommendations confirm them with two
clarifications.

**Verdict (second pass, plan @ `9ee25c5` — see §10): APPROVED, Phase 0 may start.** All five
amendments and all nine corrections are resolved or explicitly scheduled into a phase. No open
decisions remain.

---

## 1. Claim verification (what I checked, and what held)

| Plan claim | Verified in this tree | Status |
|---|---|---|
| **B1** — mStock fills go through a serve gate (`get_ws_quote`: two-clock age, subscribed-set, fail-closed, `_expire_ladder`) that Upstox lacks | `brokers/mstock.py:1058-1174` (gate) vs `data/upstox_ws_wrapper.py:736-740` (raw `get_quote`, cache-dump, no freshness, no liveness) | ✅ confirmed — this is the live-money blocker and the plan ranks it first correctly |
| **B2** — `HotStrikeRing` drops non-numeric tokens | `quote_providers.py` `recenter` skips `not raw_tok.isdigit()`; `pin_tokens`/`unpin_tokens` `int(...)`-cast and `continue` on failure; same family at `:472`, `:2107`, `:2352` | ✅ confirmed — `NSE_FO|…` keys would silently subscribe nothing |
| **B3** — no tick listener/coalesced bus on Upstox wrapper; no `_ws_feed`/WS surface on `UpstoxBroker`; no reconnect purge | `mstock_ws_wrapper.py:31,117,176,246` has all three; `upstox_ws_wrapper._on_open` (`:371`) re-subscribes but never clears cache; `brokers/upstox.py` has zero WS surface (its `get_option_quote` at `:776` is REST LTP) | ✅ confirmed |
| **B4** — provenance hardcoded | `quote_providers.py:437,1945` → `"live:mstock"`; `execution_engine.py:206` → `"live:mstock"`; `chain_snapshots.py:185` → `source="mstock"`; `db/models.py:741` → `server_default 'mstock'` | ✅ confirmed — and `+bidask` really is load-bearing: `_fills_against_live_depth` (`paper_trading.py:674-699`) requires it before a fill may claim two-sided depth |
| **B5** — shadow witness self-refuses on upstox source; sweep hardcodes mStock-primary | `cross_broker_shadow.py:364-373` (self-refusal), `:290-330` (`"mstock"`/`"upstox"` baked into the diff record) | ✅ confirmed |
| **B6** — `ChainBus._live` keyed by underlying only | `feed_registry.py:226-240`: `entry = self._live.get(key)` where `key` is the underlying; `key=(key, source_key, "")` is stored in the entry but **not** part of the lookup | ✅ confirmed — first acquirer freezes the venue |
| D/F "built but not yet live" | Collar (`EXIT_COLLAR_*`), quote/parity sanity modes all present in `paper_trading.py` | ⚠️ confirmed built; **"not yet live" needs reconciliation** — in this tree `DEFAULT_EXIT_COLLAR_MODE = "enforce"` (`paper_trading.py:193`), i.e. the collar is on by default here. The plan (written against `bt-mainmerge` @ `975f8d9`) must mean "not yet exercised in a session" or a different default in that worktree. Say which in the plan's status line. |
| Evidence: "10 structurally impossible ladders (mStock) vs 0 (Upstox)" | `tools/out/count_impossible_ladders_20261109.py`, the `logs/feed_depth/*.jsonl` tapes, and `docs/broker-reference/upstox/` are **not in any branch of this repo** (untracked runtime artifacts / mirrored docs) | ⚠️ not reproducible from this checkout — see §6 amendment 1 |

The citation discipline of the plan is high: every blocker points at a real line and the
consequences it draws are the right ones. I found no claim I could refute.

---

## 2. Quant research analyst view

### 2.1 What the evidence proves — and what it doesn't

The two evidence items support different strengths of conclusion:

- **"10 structurally impossible vs 0"** is a *categorical* defect measure (crossed books,
  folded ladders — shapes that cannot exist), not a stochastic one. One session is sufficient
  to prove the *class* exists on mStock and was not observed on Upstox. It is **not**
  sufficient to estimate the *rate* (10/day? 10/week?) that the risk budget should size
  against, nor to claim Upstox is a better *fill basis* — absence of impossible books is
  necessary, not sufficient. A feed that prints nothing is never wrong.
- **The 2026-11-08 junk ask** is the decisive item, and the plan draws exactly the right
  strategic conclusion from it in §1: *a wrong price from one broker is bounded by another
  broker's book only if we are willing to trade on the other broker's book.* No single-venue
  price band could have caught ₹493.65 — it was internally coherent (fresh row, live ladder,
  only the ask field poisoned). Cross-venue corroboration is the only class of gate that sees
  it, and venue *choice* is the only fix that removes the dependency rather than policing it.

One more asset the plan under-sells: **the exit collar band is already calibrated on dual-venue
tape** (`paper_trading.py:165-186`: 135,141 consecutive two-sided pairs from the 2026-11-09
dual capture, mStock + Upstox; legitimate 5-second moves ≤ ₹11.25 (mStock) / ₹11.10 (Upstox);
the three known junks an order of magnitude above). So D's ceiling is not mStock-shaped — the
Upstox-primary switch does not invalidate it. That is a real, quiet argument for the plan's
core claim that "no rule knows which venue it is guarding."

### 2.2 The Phase 4 comparison must be a scorecard, not a P&L diff

The plan's Phase 4 says "compare three tapes… and P&L difference vs the mStock-marked book."
At n = 1 session, P&L difference is noise (the 2026-11-08 loss itself was a 5-second,
two-tick event; session P&L is dominated by path luck). The informative statistics are
*feed-behaviour* statistics, all of which the infrastructure already records:

| Statistic | Source already in tree | Why it decides |
|---|---|---|
| Coverage of held/open legs (frames, `frames_for`) | `upstox_ws_wrapper` counters | an empty book is free of garbage and useless |
| Quote age p50/p99 per strike (mono clocks) | rows carry `recv_mono`/`ltp_mono` | freshness is the serve gate's input — see §3.2 B1 |
| Blank-ladder rate (`blank_ladder_count`) | wrapper counter | how often the venue offers no tradable book |
| Spread distribution, per strike, vs mStock | `shadow_diff` rows (B5 fix makes them symmetric) | marking off a systematically wider book changes every fill and the collar's base rate |
| **Cross-venue basis** (mStock − Upstox L1) distribution | `shadow_diff.delta` | the collar-basis confounder of Q2(a), quantified |
| Impossible-ladder count, both venues, same definition | the (to-be-committed) counter script | the headline metric, kept running |
| L1 veto counts, refused exits, and **why** (garbage vs venue basis) | F/D shadow logs | the accept/reject signal for going enforce |

**Amendment: write this scorecard and its accept criteria down before Phase 4 opens**, e.g.
"Upstox coverage of held legs ≥ mStock's on the same session, median basis inside the collar's
own reference tolerance, zero impossible ladders, refused-exit count attributable to venue
basis ≤ N". Thresholds get *measured from the shadow session*, per the standing rule — but the
*decision rule* ("what would make us flip to enforce") must exist before the data does, or the
session will be read post-hoc.

### 2.3 The asymmetry being traded, stated plainly

Switching primary exchanges a **measured** failure mode (mStock's garbage rate: known class,
known count, gated by F/D) for an **unmeasured** one (Upstox-as-fill-basis has never been
exercised; the wrapper is proven only as a witness). The plan's phasing is precisely the
instrument for managing this asymmetry — Phase 0 buys the clock measurement, Phase 4 buys a
full session of shadow before any enforcement — so I endorse the ordering. Two specific
unmeasured items to fold into Phase 0's live session:

1. **Quiet-leg print cadence.** mStock's serve gate has a calibrated bypass: a subscribed OTM
   leg whose book is quiet >10 s still serves depth *because mStock's print cadence for quiet
   strikes was observed to be slower than that* (`mstock.py` get_ws_quote regime 1). Upstox's
   cadence for the same quiet strikes is unknown; a naive port of the 10 s ceiling either
   false-refuses quiet legs or over-trusts stale ones. Measure it in Phase 0 and give the
   shared helper **per-venue defaults** — this is a "threshold from the tape" item, not a
   hand-calibration.
2. **Frame/latency profile around the open** (09:15–09:20) — the Muhurat loss lived in the
   thin-open regime; the shadow session must be scored by regime, not just session-wide.

### 2.4 Small degradations to price in (accepted, but quantify)

- **No per-level order counts (`bno`/`ano`) on Upstox WS.** `_top_of_book_quantity`
  (`paper_trading.py:702`, consumed at `:1794` for sizing visibility) returns `None` for
  Upstox — the "no evidence, claim nothing" behaviour is correct, but during the shadow day
  count how often mStock *would* have provided a size claim and Upstox cannot, so the sizing
  degradation is a known number rather than a surprise.
- **Index spot from ltp only.** Correct in the plan: `IndexFullFeed` has no book, so
  `_infer_spot_from_ws` must not be ported as a depth reader; spot-for-parity should read
  index ltp and the parity gate must tolerate that.
- **Paper-mode semantics:** in paper, the quote row *is* the fill, so `OPTION_DEPTH_SOURCE=upstox`
  literally changes fill prices, P&L, and every downstream calibration — intended (Phase 4
  measures exactly this), but any strategy threshold calibrated on mStock-marked history
  (entry bands, dedupe windows — the collar is exempt, dual-tape) should be re-checked on
  Upstox marks before enforcement.

---

## 3. Architect view

### 3.1 Phase ordering — correct as written

- **Gate before socket (Phase 0 ⊃ B1).** Promoting `upstox_ws_wrapper.get_quote` to primary
  as-is is precisely the 2026-11-03 "fabricated ₹71,639" class: a cached row from a socket
  that died an hour ago, served with full confidence. Making the *gate* the deliverable and
  keeping the wrapper witness-only during Phase 0 means **nothing in that phase can change a
  fill** — the right property for a change that touches the fill seam.
- **Provenance before comparison (Phase 2 ⊂ 3–4).** `+bidask` decides whether D's collar
  binds and whether spread-vs-bps double-counting is stopped (`_fills_against_live_depth`).
  A mislabel silently changes *which fills are bounded* — so Phase 2 must land before the
  Phase 4 diff is read, or the comparison itself is mislabelled. The plan already sequences
  this correctly; I state the dependency so nobody "optimizes" Phase 2 into Phase 3.
- **Shadow before enforce (Phase 4)** mirrors how B/D/F were introduced and is the house rule.

### 3.2 Per-blocker engineering notes

- **B1 (serve gate):** extract the *one* rule the plan describes — two-clock age + subscribed
  set + socket liveness + ladder TTL + fail-closed — into a venue-neutral helper (sibling of
  `ladder_shape.py`), with clocks/registry injected so `MStockBroker.get_ws_quote` and
  `UpstoxBroker.get_ws_quote` become thin callers. Ratchet: `tests/test_option_ladder_gates.py`
  `TestServeGate` (`:321-399`, incl. `test_quiet_leg_on_a_live_subscribed_socket_still_serves_depth`,
  `test_ladder_past_its_ttl_serves_price_without_depth`, `test_unsubscribed_token_cannot_use_the_socket_liveness_bypass`)
  stays green unchanged, and an Upstox twin table is added. Note the wrapper rows are already
  mStock-shaped (carry `recv_mono`/`ltp_mono`/`ladder_status`), so the mono-clock half ports
  nearly as-is; the wall-stamp half depends on the Phase-0 measurement (Upstox `timestamp`/
  `currentTs` units and clock source are undocumented — the plan's insistence on *measuring*
  before trusting is exactly right).
- **B2 (token typing):** do **not** spot-fix `recenter` alone. The `int(...)`/`isdigit` family
  spans at least `:472, :1581, :1603, :1761-1764, :2035, :2107, :2352` — one normalization
  pass: registry keys are opaque strings, numeric mStock tokens remain valid keys, per-venue
  subscribe mapping translates key→wire format at the broker boundary only. Because the
  failure mode is *silent* (entries never fire, no error), add an assertion/log when a recenter
  yields an empty ring — "strategy mysteriously never trades" is the worst bug class to debug
  live.
- **B3 (feed surface):** the `_purge_all` twin on `_on_open` is a correctness must — the mStock
  invariant "no row may outlive the socket that wrote it" (`mstock_ws_wrapper.py:176`) exists
  because a cached book from a dead socket is indistinguishable from a live one; Upstox
  currently has exactly that hole. The mStock tree already pins this with
  `test_reconnect_starts_a_new_cache_generation` (`test_option_ladder_gates.py:302`) — the
  Upstox twin test belongs with it. Port `add_tick_listener` + the 150 ms coalesced bus +
  `forget_token` symmetry in the same pass.
- **B4 (provenance):** one rule, applied everywhere: *the label comes from the broker that
  produced the row.* Sites: `quote_providers.py:437,1945`, `execution_engine.py:206`,
  `chain_snapshots.py:185`, `db/models.py:741` (and audit every insert into `quotes` that
  relies on the `'mstock'` server default — a server default is how provenance lies by
  omission). The `options_api.py` probe order (`("mstock","dhan","upstox")`) should become
  "the configured source first", not a fixed priority.
- **B5 (symmetric witness):** the self-refusal invariant is right; make it symmetric —
  *primary never mirrors itself*, in both directions — and have the `ee0372b` L1 corroboration
  read the peer whichever venue is primary. Keep the AST rule intact: no trading path imports
  the witness. The sweep's `record_shadow_diff` schema should label columns `primary`/
  `secondary` + the two venue names, not the literals `mstock`/`upstox`, or Phase 4's diff
  tables silently flip meaning when the switch flips.
- **B6 (provider keying):** rekey `ChainBus._live` to `(underlying, source)` — this preserves
  the documented one-object-per-pair identity/refcount contract while removing the
  first-acquirer-wins freeze. The plan's acceptance test (two sources ⇒ two providers, two
  `source_name`s, both `+bidask`) is the right one. **Budget note tied to Q1:** two
  `LiveChainProvider`s for the same underlying means two rings' worth of `full` keys against
  the per-socket cap (2000 `full`, combined 2000/2000/1500); `check_limits` must account
  across rings sharing one socket, and a mixed-source process must be measured against the cap
  before it is allowed to run unattended.

### 3.3 Answers to Q1–Q4 (reviewing architect)

- **Q1 — socket budget → (a), share the socket**, as recommended. The independence worry is
  answered structurally: the witness for an Upstox-primary book is **mStock** (a different
  broker and socket), so one Upstox socket per process can serve both "primary for upstox
  runners" and "witness for mstock runners" without ever mirroring a venue onto itself (B5's
  invariant polices that). Document the 2-socket cap anyway (option (b)'s deliverable is cheap
  and belongs in the runbook).
- **Q2 — execution venue → (a) for Phases 0–4**, as recommended; (b) stays task #18. One
  addition: in shadow, log the **peer mark at every collar decision** so Phase 4 can separate
  "collar tripped by venue basis (mStock fill vs Upstox mark)" from "collar tripped by
  garbage". Without that line, the collar's shadow trip count is uninterpretable and the
  enforce decision inherits the ambiguity. (In paper mode this confounder is small — the fill
  *is* the primary's row — but the log costs one line and protects the live-trading reading.)
- **Q3 — one knob → yes, `config.source`**, as recommended. **But the plan currently names two
  selectors** (`config.source` *and* env `OPTION_DEPTH_SOURCE`) without precedence. Resolve in
  one sentence, e.g. "env supplies the default for runners that don't set `config.source`, and
  an explicit runner config always wins" — and pin it with a test. Left ambiguous, Phase 3
  ships a "which one wins" bug in the exact seam the plan is trying to make honest.
- **Q4 — audit provenance → write the real source**, as recommended. Fold it into B4's single
  rule. A certification run must state its own venue; `server_default 'mstock'` makes every
  careless insert lie, and the `+bidask` semantics make the lie load-bearing.

---

## 4. What the plan deliberately does NOT do — checked, agree

- mStock is not retired: its serve gate and `test_mticker_ws_fixes.py` (AGENTS.md fragile-code
  rule) stay untouched. ✅ correct per repo policy.
- No new hand-calibrated thresholds: the collar constants themselves cite their measuring
  script and population (§2.1 above). ✅ consistent.
- No invented prices: the synthetic-data ban applies per-venue. ✅.
- No failover supervisor: selection is operator-set, restart-applied. ✅ — this plan answers
  the depth question *before* the order-lifecycle question (task #18) enters the diff, which
  is the right size of decision.

---

## 5. Residual risks after all phases

1. **Two-venue basis becomes the new junk-ask.** With marks on Upstox and fills on mStock
   (Q2a), a genuine mStock dislocation now trips the collar (good) but a genuine fast market
   can also trip it via basis (the shadow logs from §3.3 answer this — that is why they are
   mandatory, not optional).
2. **Token expiry (03:30) + 401-stops-reconnect** can silently demote the primary mid-session.
   Fail-closed protects fills (B1) but the operator needs a **primary-feed-loss alert** —
   wire it to the existing staleness/feed-quality machinery (`forward/feed_quality.py`, the
   `/api/broker/feed-quality` lever from the 2026-10-15 plan) rather than a new subsystem.
3. **Evidence rot:** the plan's headline numbers live in untracked artifacts on another
   machine. Until amendment 1 lands, the next reader cannot audit the decision.
4. **Divergent defaults across worktrees** (the "not yet live" vs `DEFAULT_EXIT_COLLAR_MODE
   = "enforce"` discrepancy, §1) — the plan's status line should state the *deployed* mode,
   not the built mode.

---

## 6. Amendments required before Phase 0 starts

1. **Make the evidence reproducible.** Commit `tools/out/count_impossible_ladders_20261109.py`
   (with its definition of "structurally impossible" stated in the docstring) and cite the tape
   filenames/dates in the plan; the mirrored Upstox docs may stay untracked but the plan should
   say where a reviewer gets them. The 10-vs-0 headline is the plan's foundation — it must be
   re-derivable from the repo.
2. **Write the Phase 4 scorecard now** (§2.2 table + accept criteria), before the session is
   run. Decision rule before data.
3. **Primary-feed-loss alerting** (§5.2) added to Phase 3/4 scope — cheap, uses existing
   machinery, and the 03:30/401 behaviour makes it not-optional for an Upstox-primary day.
4. **Quiet-leg cadence measurement** (§2.3.1) folded into Phase 0's live session; the shared
   serve-gate helper takes per-venue defaults as a result.
5. **Reconcile the status line** with the actual deployed defaults of D/F (§5.4) and note the
   branch this review ran against (`arena/16087b3a-back-test` @ `75b7cc8`), since the plan
   references `975f8d9` on a different worktree.

With those five, Phase 0 is unambiguous and can start immediately: it is one shared helper,
one live measurement session, and two test tables — and by construction it cannot change a
fill.

---

*Review scope: plan §1–§8 as committed at `75b7cc8`; code verified against
`src/backtest/brokers/mstock.py`, `src/backtest/brokers/upstox.py`,
`src/backtest/data/upstox_ws_wrapper.py`, `src/backtest/data/mstock_ws_wrapper.py`,
`src/backtest/data/cross_broker_shadow.py`, `src/backtest/options/quote_providers.py`,
`src/backtest/options/paper_trading.py`, `src/backtest/forward/feed_registry.py`,
`src/backtest/engine/execution_engine.py`, `src/backtest/db/models.py`,
`tests/test_option_ladder_gates.py`, `docs/OPTION-JUNK-ASK-INCIDENT-2026-11-08.md`.*

---

## 10. Second-pass re-review — plan @ `9ee25c5` (2026-11-11)

The revised plan folds in all nine review items (its §8 maps each to where it lives) and adds
§1b: a measured, same-session comparison from both brokers' own tapes. I checked the new
artifacts against their claims, not just their existence.

### 10.1 The five amendments — resolved

| Amendment | Resolution | Verified |
|---|---|---|
| 1 — evidence reproducible | `tools/out/count_impossible_ladders.py` committed, date-parameterised, with "structurally impossible" defined in the docstring (bids step down, asks step up, book not crossed — no calibration, no second broker), and the 1.5× off-touch ratio explicitly excluded from the headline and reported as context only. Outputs `impossible_20261109.txt` / `impossible_20261111.txt` carry per-venue packet denominators and reproduction commands; the 272 MB/day tapes stay runtime artifacts per AGENTS.md | ✅ the definition is exactly the calibration-free invariant a junk-ask gate needs |
| 2 — Phase-4 scorecard | Scorecard **S1–S7** with pass conditions fixed before the bell (§5 Phase 4, new Q5): coverage ≥ same-session mStock baseline, 0 impossible ladders, 0 venue-attributable seam refusals, every L1 veto split basis-vs-garbage, 100 % peer-mark journaling, alert fires on primary loss only, basis fills listed with both marks | ✅ the n=1 P&L framing is gone; every criterion is a count with a denominator |
| 3 — selector precedence | §7 Q3: *"the env default applies only when the runner config carries no explicit source; an explicit `config.source` always wins"* + a three-case test (unset→mstock; env=upstox+unset→upstox; env=upstox+config=mstock→**mstock**) | ✅ unambiguous |
| 4 — quiet-leg cadence measured | §1b `measure_feed_cadence.py` over the 2026-11-11 tape: per-leg p99 gap **16.2 s mStock vs 1.87 s Upstox**, quietest leg 106.6 s vs 41.5 s; Phase 0 takes **per-venue defaults from the tape** and decides the socket-liveness rescue explicitly, with tests pinning both venues' ceilings to separate constants | ✅ done one better than asked — see §10.3 |
| 5 — status-line reconciliation | §1 rewritten to state **deployed** mode: D/F live (`OPTION_EXIT_COLLAR=enforce`), booted 09:23:34 IST 2026-11-11, with observed log counts (`SHADOW_UPSTOX_ON` ×2, zero `EXIT_REFUSED`/`EXIT_COLLAR_SHADOW` — the collar has not yet had an exit to bound); header names the reviewed commit and the implementation worktree | ✅ "built vs deployed" is now the stated distinction |

### 10.2 Evidence quality check (the artifacts, not just their existence)

- **Denominators, unequal ones handled honestly.** 2026-11-09 was 10/299,318 (mStock) vs
  0/48,008 (Upstox) — the plan now *states* that Upstox carried far fewer subscriptions that
  day and adds the 2026-11-11 capture at comparable scale: **1/491,387 vs 0/565,297**. My
  first-pass concern (n≠m comparison) is answered by construction, not by smoothing.
- **The sampled breaches are real, and they are the known failure family.** The
  2026-11-09 prints show folded ladders (bids `[330.05, 330.00, 329.95, 1640.62, …]`) and
  mid-ladder level swaps — the same class as the 2026-11-03 "priced itself five times over"
  packet. mStock still produces one on 2026-11-11 (a mild ₹0.79 bid fold, tok 62338); Upstox
  none across ~10.5 lakh packets total. The metric is doing what a proof-type invariant
  should: catching the class regardless of magnitude.
- **The cadence script's clock analysis is the part the design actually needed.** It separates
  stamp taxonomy (mStock: 53.13 % Go-zero `1980-01-01` sentinels, 46.87 % naive no-offset
  strings; Upstox: 100 % ISO with `+00:00`) and refuses to compute a skew percentile from
  sentinels — the right call.

### 10.3 New finding the tape surfaced — and its sharp edge for Phase 0

**mStock's own per-leg p99 (16.2 s) exceeds its serve-gate ceiling (10 s), and fills survive
only because of the socket-liveness rescue at `brokers/mstock.py:1118-1143`.** The safety of
today's gate was never the 10 s constant — it was the bypass. That makes the rescue rule
**load-bearing for every mStock fill**, and gives Phase 0's "nothing can change a fill" claim a
precise edge: extracting the shared helper must preserve mStock's rescue behaviour
*bit-for-bit* (the plan's "decide the rescue explicitly" + pinning test covers the decision;
add "mStock serves/refuses exactly as before across the quiet-leg cases" to the acceptance if
it is not already implied by the unchanged `TestServeGate` pin). The corollary for Upstox is
already in §1b point 3: with a 1.87 s p99, a 10 s ceiling there is ~5× the normal interval,
and whether the rescue is even needed is a question the tape can answer — do not port a
bypass whose safety was unexamined.

Two smaller confirmations worth keeping in the record: the sentinel/no-book correlation
(257,628 of 257,636 sentinel frames carry no bid and no ask — *the absence of a book and the
absence of a clock are the same event*), which explains mechanistically why fail-closed is
cheap on mStock; and Upstox's ±0.4 s stamp-vs-receipt agreement, which the plan correctly
labels **feed time, not exchange time** — `currentTs`-vs-`ltp` field separation stays a Phase-0
measurement, as it should.

### 10.4 Residual nits (cosmetic; do not block Phase 0)

1. **Definitional drift between the two tools:** `count_impossible_ladders.py` counts a
   one-sided row under `empty_ladder`, while `measure_feed_cadence.py` splits one-sided from
   no-book (1 vs 257,628). Same event, two vocabularies. Pick one (the cadence split is more
   informative) so cross-day coverage numbers stay comparable — matters for scorecard S1,
   which must state which definition its baseline uses.
2. **S1's wording** ("≥ the mStock baseline of the same session, from the §1b tape") should
   name the script and the coverage definition alongside, so the pass condition is
   reproducible without rereading §1b.

### 10.5 Final position

The plan at `9ee25c5` is approved as written. The decisions (§7) are binding, the phase
acceptances are testable, the evidence chain is committed and re-derivable, and the one new
risk the measurement surfaced (the load-bearing rescue) is already given an explicit decision
point inside Phase 0. **Phase 0 can start immediately**; its only open items are the two
measurements the plan itself names (`currentTs`-vs-`ltp` separation; a deliberately-quiet
token), which are Phase-0 work, not prerequisites to it.
