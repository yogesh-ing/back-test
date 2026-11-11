# Plan: Upstox as the primary option-depth source, mStock demoted to witness — and the switch that makes it a setting

**Status:** Reviewed 2026-11-11 — §7 questions are now **decisions**, §8 records the review's nine
corrections and which phase carries each. No implementation code written yet.
**Worktree / branch:** implementation goes in `C:/learning/bt-upstox-primary` @
`feature/upstox-primary-depth` (pushed; `origin/feature/upstox-primary-depth` → `75b7cc8`, whose
ancestors are `975f8d9` = item D and `ee0372b` = item F). Reviewed against `975f8d9` as written;
the doc commit `75b7cc8` is the one under review.
**Supersedes nothing:** this is the *depth-source* half of
`docs/plans/upstox-dual-venue-failover-plan.md` (2026-10-15), narrowed and re-grounded in the
2026-11-08 junk-ask incident. That plan's failover supervisor is still unbuilt and is NOT
required here — this plan makes the source selectable, not auto-failing-over.

---

## 1. Why now (the evidence, not a vibe)

- mStock published **10 structurally impossible ladders** in one session; Upstox published **0**
  (`tools/out/count_impossible_ladders.py`, committed, with the invariant defined in its docstring —
  bids stepping down and asks stepping up away from the touch, plus a non-crossed book; no calibration
  and no second broker involved). Computed counts for 2026-11-09, kept as
  `tools/out/impossible_20261109.txt`: mStock **10 of 299,318** packets (8 ask-not-monotone,
  6 bid-not-monotone, overlapping into 10 unique packets, 0 crossed books) against Upstox **0 of
  48,008**. Unequal denominators are stated, not smoothed over: that day Upstox carried far fewer
  subscriptions, which is why today's capture matters — the partial 2026-11-11 session
  (`tools/out/impossible_20261111.txt`) is mStock **1 of 491,387** against Upstox **0 of 565,297**,
  both venues now subscribed to comparable ladders. Reproduce with
  `cd <checkout holding the tapes> && python tools/out/count_impossible_ladders.py 2026-11-09`;
  the 272 MB/day tapes themselves are runtime artifacts and stay out of git (AGENTS.md).
- The same day mStock answered **70.66 %** of its packets with no ladder at all; today it is
  53.00 % (§1b), and Upstox answered none.
- The 2026-11-08 Muhurat loss (−₹11,794.25 in five seconds) came from one mStock ask ₹177.50/share
  off parity that no gate could see — `docs/OPTION-JUNK-ASK-INCIDENT-2026-11-08.md`.
- The two mitigations the incident demanded are **live, not merely built**: **F** = cross-broker L1
  corroboration at the fill seam (`ee0372b`) and **D** = collar-bounded protective exits
  (`975f8d9`, default `OPTION_EXIT_COLLAR=enforce`) booted onto :5000 at 09:23:34 IST on 2026-11-11
  from `C:/learning/back-test` @ `975f8d9`. First session on them: `SHADOW_UPSTOX_ON` appears twice
  (the witness socket attached, both rings mapped) and **zero** `EXIT_REFUSED` / `EXIT_COLLAR_SHADOW`
  lines — so the collar has not yet had a protective exit to bound. "Built" vs "deployed" is the
  distinction that matters here; the plan states deployment mode from now on.
- The deeper fix is venue choice itself: a wrong price from one broker is bounded by another
  broker's book only if we are *willing to trade on the other broker's book*.

**Goal.** One switch selects which broker's depth marks and prices our option fills, the other
broker stays a corroborating witness, and no rule in the fill seam or the exit collar knows or
cares which venue it is guarding.

**Non-goals.** No automatic failover, no cross-venue hedging, no order-routing change in the
first phases, no new hand-calibrated threshold (standing rule: thresholds come out of the tape).

---

## 1b. The same session, measured from both brokers' own tapes (2026-11-11)

`python tools/out/measure_feed_cadence.py 2026-11-11` over
`logs/feed_depth/feed_{mstock,upstox}_2026-11-11.jsonl`, run from the checkout that holds the live
tapes; output snapshot committed at `tools/out/cadence_20261111.txt`. **Partial session** (09:15 IST
to ~13:05 IST, 4.85 lakh mStock packets / 5.57 lakh Upstox packets) — re-run after the close as part
of the after-market validation of the capture day, since these are the numbers the ceilings come from.

| | mStock | Upstox |
|---|---|---|
| frames carrying a two-sided book | **46.87 %** (53.13 % no book at all, 1 one-sided) | **100.00 %** (0 one-sided, 0 empty) |
| per-**leg** frame gap, p99 across legs | median **16.2 s**, p90 18.5 s, worst 26.4 s | median **1.87 s**, p90 2.65 s, worst 2.8 s |
| longest silence for one leg | 106.6 s | 41.5 s |
| subscribed tokens / frames per token | 73 tokens, 220 … 116,055 (the underlying's index frames dominate) | 56 tokens, 4,134 … 16,085 |
| venue's own timestamp | 46.87 % naive strings with **no offset** (`2026-11-11T09:19:14AM`); the other **53.13 % are the Go-zero sentinel `1980-01-01T12:00:00`** | **100 % ISO with an explicit `+00:00`** |
| that stamp vs our receipt | p05 +0.56 s, p50 +3.15 s, **p95 +60.7 s** (read as exchange wall clock) | p05 −0.38 s, p50 +0.10 s, p95 +0.21 s, max +44.6 s |

Four things this settles for the design:

1. **Coverage, not just correctness, argues for Upstox primary.** mStock answers more than half its
   frames with nothing to cross. A venue that cannot show a book cannot price a spread-crossing fill,
   and the fill seam's `+bidask` requirement (`options/paper_trading.py:674-699`) is exactly what
   refuses it — which is why the collar's own reference abstains so often today.
2. **The absence of a book and the absence of a clock are the same event.** Of 257,636 sentinel-stamped
   mStock frames, 257,628 also carried no bid and no ask: the venue sends `1980-01-01` precisely when
   it has nothing to publish. So "LTP only, no depth" is not a parse failure on our side — it is the
   venue saying it has no book, and the primary's job is to have a book for those 53 % of frames.
3. **The 10 s ceiling must not be copied venue-to-venue, and the reason mStock survives it is a
   specific line of code.** mStock's own per-leg p99 is **16.2 s**, i.e. *worse* than the 10 s ceiling,
   yet fills are not routinely refused because `brokers/mstock.py:1118-1143` rescues a stale row when
   the socket is alive and the token is subscribed. Upstox's per-leg p99 is 1.87 s, where 10 s is ~5×
   the normal interval. Phase 0 therefore takes **per-venue defaults from this tape** *and* decides
   deliberately whether the socket-liveness rescue is ported, rather than inheriting a constant whose
   safety depended on a bypass nobody measured (review item 5).
4. **Upstox gives a real second clock; mStock does not.** Upstox stamps every frame with an explicit
   offset that agrees with our receipt to ±0.4 s — transport and socket death become visible. mStock's
   usable stamps carry no offset at all (so their age is only computable if this box's clock is the
   exchange clock, which AGENTS.md forbids assuming), and its p95 of +60.7 s is the quiet-leg case the
   stamp cannot distinguish from a dead socket. Caveat kept honest: ±0.4 s agreement makes Upstox's
   stamp **feed time, not exchange time**, and the `currentTs`-vs-`ltp` field separation is still
   unproved — that is Phase 0's first measurement.

---

## 2. Venue rules that constrain the design (from `docs/broker-reference/upstox/`, not assumed)

| Rule | Citation | Consequence for this plan |
|---|---|---|
| `full` mode = 5 bid + 5 ask levels **plus** greeks, IV, OI, ATP, in one Quote record per level (`bidP/bidQ/askP/askQ`) | `MarketDataFeedV3.proto:21-26`, `sections/market-data-feed-v3.md:288-320,322-328,351-354` | What we need is `full`; we do not need `full_d30` (that is a paid Plus tier, 50 keys/socket). |
| **Indices have no two-sided book at all** — `IndexFullFeed` carries `ltpc` + `marketOHLC` only | `MarketDataFeedV3.proto:71-74` | Spot must come from index **ltp**, never index depth. `_infer_spot_from_ws` (`options/quote_providers.py:2128-2144`) must not be ported as a depth reader. |
| A level can be one-sided/empty (`0/0`) | `sections/market-data-feed-v3.md:589-593` | Already handled: `_has_two_sided_book` (`data/upstox_ws_wrapper.py:666`). |
| WS depth has **no per-level order count** (`bno/ano` removed 2024-10-10); HTTP full-quotes does | `sections/market-data-feed-v3.md:64574-64603`, `sections/full-market-quotes-v3.md:68-92` | `_top_of_book_quantity` (`options/paper_trading.py`) keeps returning `None` for Upstox = "no evidence, claim nothing". Correct, not a bug. |
| Limits: 2 sockets/user (Plus 5); per socket 5000 `ltpc` / 3000 option-greeks / **2000 `full`**, combined set 2000/2000/1500 | `sections/market-data-feed-v3.md:40-45,54-56` | Primary + witness on the *same* account = 2 sockets = at the normal cap. Plan must reuse one Upstox socket for both roles where possible. |
| Auth = `Authorization: Bearer <token>` on the wss handshake; the authorize endpoint's redirect URI is **single-use**; token valid until 03:30 IST next day; 401 stops reconnect | `sections/market-data-feed-v3.md:60-63`, `sections/market-data-feed-authorize-v3.md:25,34`, `sections/get-token.md:7`, SDK `market_data_streamer_v3.py:85-88` | Reconnect must re-handshake (already implemented, `data/upstox_ws_wrapper.py:277-317`). An Upstox-primary session dies at 03:30-token expiry mid-day exactly like mStock's session does — the witness rule "missing second broker costs one log line, never a halt" must survive it. |
| HTTP depth fallback exists (`GET /v3/market-quote/quotes`, ≤500 keys, 50 req/s) but is a point-in-time snapshot | `sections/full-market-quotes-v3.md:5,137-145,180`, `:67760-67767` | Fine for the dashboard's on-demand close; not a primary feed. |
| **Not documented anywhere:** periodic token refresh, WS behaviour when a key limit is exceeded | grep found nothing in either source | `check_limits` must budget across rings sharing a socket (§7 Q1), and a primary-feed-loss alert must exist because neither answer is knowable in advance (§8 item 8). |
| Timestamp clock source (exchange vs feed server) — **measured 2026-11-11, see §1b** | `tools/out/measure_feed_cadence.py` + `tools/out/cadence_20261111.txt` over `feed_upstox_2026-11-11.jsonl` | Upstox stamps 100 % of frames with an explicit offset and agrees with our receipt to ±0.4 s, so the gate gains a real second clock. Because it tracks *receipt* rather than the exchange tick, it is **feed time**: it detects transport and socket death, not a quiet contract. mStock stamps nothing usable (46.87 % offset-less strings, 53.13 % Go-zero). |

---

## 3. What is already true in the code (so nobody re-builds it)

`data/upstox_ws_wrapper.py` is a complete V3 protobuf depth socket, not a toy:

- modes + per-mode key ceilings (`:89-98`, `check_limits :233`), `sub/change_mode/unsub` envelopes
  (`:415-451`), real reconnect that re-authorizes because the URI is single-use (`:277-367`);
- storage keyed exactly as Upstox spells it (`NSE_FO|…`, `:498-503`), row is deliberately
  mStock-shaped: `ltp/bid/ask/spread/oi/ohlc/depth/greeks/timestamp/recv_mono/ladder_status/
  ladder_mono/atp/iv/…` (`:551-624`);
- the **same** ladder vocabulary as mStock: `_sanitize_depth` calls the shared
  `ladder_shape.order_problem` (`:671-717`, `:709`) and `_retain_ladder` writes last-valid-wins
  with `kept:<reason>` (`:626-663`);
- read API `get_quote/quotes/get_depth_levels/frames_for/blank_ladder_count/subscribed_keys/stats/
  last_any_tick_mono` (`:736-769`, `:249`);
- it already records every packet through the ENABLE_FEED recorder (`record_depth_packet("upstox",…)`, `:507`)
  → an Upstox-primary session keeps writing `feed_upstox_<day>.jsonl` unchanged.

What that wrapper, and `brokers/upstox.py`, do **not** have is everything the *trading* path
consumes. That gap, ranked by risk to live money, is the rest of this plan.

---

## 4. Blockers, hardest first

**B1 — No serve gate on the Upstox side (live-money blocker).**
mStock's fills never read the wrapper directly; they read `MStockBroker.get_ws_quote`
(`brokers/mstock.py:1058-1174`): per-token `ltp_mono`/`recv_mono` against `max_age_seconds=10`
(`:1112-1118`), socket-wide liveness via `last_any_tick_mono()` (`:1119-1130`), membership in the
subscribed set (`:1131-1134`), **fail-closed `return {}`** on disconnect or unparseable stamps
(`:1094`, `:1144-1161`), and `_expire_ladder` zeroing bid/ask past `WS_LADDER_TTL_SECONDS=120`
(`:196`, `:1176-1216`). Upstox has none of this: `get_quote` (`data/upstox_ws_wrapper.py:736`)
hands back whatever row is cached, from a socket that may have dropped an hour ago.
*Promoting that raw accessor to primary is precisely the 2026-11-03 "fabricated ₹71,639" failure
mode.* **Therefore: the gate, not the socket, is the deliverable of Phase 0.**

**B2 — `HotStrikeRing` is numeric-token-only.** `recenter` drops every non-digit token
(`options/quote_providers.py:1761-1764`), `pin_tokens/unpin_tokens` cast `int(...)` (`:1581`, `:1603`),
`ring_book` casts again (`:2035`). With `NSE_FO|…` keys the ring subscribes to **nothing**, so every
entry path fail-closes. Same class of bug at `:472`, `:2107`, `:2352`.

**B3 — The wrapper has no tick listener / coalesced bus, and the broker has no WS surface.**
mStock's wrapper has `coalesce_ms=150`, `add_tick_listener`, the `mstock-tick-bus` thread, and
`_purge_all()` on *every* connect ("no row may outlive the socket that wrote it")
(`data/mstock_ws_wrapper.py:31,246-307,117,176-189`); `UpstoxBroker` has no `_ws_feed`,
`get_ws_quote`, `subscribe_tokens_ws`, `ws_subscribed_tokens` or `add_tick_listener` at all, so
`LiveChainProvider.has_ws_ring_support()` (`:2118-2126`, it tests `broker.__dict__["_ws_feed"]`)
is False and an `upstox` runner today gets REST LTP only (`brokers/upstox.py:776-808`).
Also missing: the reconnect purge (B1's twin) — `_on_open` (`data/upstox_ws_wrapper.py:371-375`)
does not clear the cache.

**B4 — Provenance would lie.** `source_name` is hardcoded `"live:mstock"` in two providers
(`:437`, `:1945`) and `engine/execution_engine.py:206` returns that literal for *any* authenticated
broker; `option_chain_snapshots` writes `"mstock"` regardless; `quotes.source` is
`server_default 'mstock'` (`db/models.py:741`); `options_api.py:247` probes `("mstock","dhan","upstox")`
in that order. `+bidask` is load-bearing (`paper_trading.py:622`, `_fills_against_live_depth :674-699`)
— it is the flag that makes D's collar bind and stops spread-vs-bps double counting — so a mislabel
is not cosmetic, it silently changes which fills are bounded.

**B5 — Corroboration collapses exactly when it is most needed.** `cross_broker_shadow.maybe_start`
self-refuses when the source already contains "upstox" (`data/cross_broker_shadow.py:364-373`, and
sweep hardcodes primary=mStock/secondary=Upstox at `:306-328`, witness label `:294`), so an
Upstox-primary book gets **no second broker** — and the `ee0372b` L1 gate (`:1118-1167`) is written
with mStock as the accused. The self-refusal rule itself is right (never mirror a venue onto itself,
`ca816b8`); it must become *symmetric* instead of inverted.

**B6 — One shared provider per underlying.** `ChainBus._live` is keyed by **underlying only**
(`forward/feed_registry.py:226-240`) while the entry carries `key=(key, source_key, "")`. The first
acquirer freezes the venue for that underlying, so a mixed-source rollout silently serves the
incumbent broker's provider to a runner that asked for the other one.

**B7 — Feed venue ≠ execution venue (decision, not a defect).** Orders, margin precheck and fill
records stay mStock (`options/live_trading.py:216-234`, `:356-394`, `:1120-1127`), and mStock numeric
tokens are useless to Upstox's key-based order path (`brokers/upstox.py:242`, `:1099`). Trading on
another venue's book is legitimate *only* because D and F bound it; the operator must choose
deliberately (§7, Q2).

---

## 5. Phases

**Phase 0 — The serve gate, with per-venue defaults (no behaviour change).**
Port B1 as one shared rule, not a second copy: extract mStock's two-clock age + subscribed-set +
fail-closed + ladder-TTL logic into a venue-neutral helper (next to `ladder_shape.py`) and have
`MStockBroker.get_ws_quote` call it, then add `UpstoxBroker.get_ws_quote` on top of the same helper.
The helper takes its ceilings **per venue, from the tape** (§1b): mStock keeps the existing 10 s —
which it only survives because of the socket-liveness rescue at `brokers/mstock.py:1118-1143`, since
its own per-leg p99 is 16.2 s — and Upstox gets its own default derived from its measured per-leg p99
(1.87 s) and longest leg silence (41.5 s), not one shared constant copied across brokers. Phase 0
must also **decide the rescue explicitly**: port `socket_alive and token_subscribed` as a venue-neutral
rule, or let Upstox age per leg alone. Two measurements still owe before an Upstox row may be
*served*: the `currentTs`-vs-`ltp` field separation (which of the two stamps the price the gate ages),
and a token that goes quiet on purpose (is 41.5 s a dead socket or an illiquid strike?). §1b already
settles the clock question §2 could not answer.
Acceptance: `tests/test_option_ladder_gates.py::TestServeGate` (the mStock pin at `:321-399`) still
green + an Upstox twin table (stale row refused, disconnected socket refused, unparseable stamp
refused, `expired:*` ladder has bid/ask zeroed), plus a test that the two venues' age ceilings come
from separate defaults and that changing one cannot move the other, and a test pinning whichever
rescue decision is taken. **Nothing in this phase can change a fill** — the wrapper stays
witness-only.

**Phase 1 — Make Upstox a feed-capable broker (still not selected).**
`_ws_feed` attached on login, `subscribe_tokens_ws`/`ws_subscribed_tokens` over instrument_keys,
`add_tick_listener` + coalesced bus, reconnect purge (`_purge_all` twin), `forget_token` symmetry
(B3). B2 is **not** a `HotStrikeRing` patch: the `int(...)`/`isdigit()` family spans seven sites
(`options/quote_providers.py:472, :1581, :1603, :1761-1764, :2035, :2107, :2352`), so Phase 1 is one
venue-neutral **key-normalization pass** — the ring and registry store opaque strings end to end and
translate to a numeric token only at the mStock broker boundary — plus a "ring is empty after
recenter" assertion that turns today's silent zero-subscription (subscribe nothing, fail-close
everything) into a test failure.
Acceptance: `has_ws_ring_support()` True for a fake Upstox broker; a ring test that subscribes and
recenters with `NSE_FO|…` keys and asserts the subscribed set is non-empty; the same test with
numeric tokens asserts the mStock path subscribes the identical set as before (bit-for-bit, so
`tools/out/qa_ring_probe.py` stays a valid probe); a mixed ring holding both spellings subscribes
both.

**Phase 2 — Provenance and separation (the switch becomes honest).**
`source_name` derived from `broker.broker_name` at both sites + `execution_engine.py:206`;
`ChainBus._live` keyed by `(underlying, source)` (B6); snapshot/DB labels written from the actual
source (B4). Acceptance: a test that acquiring the same underlying from two sources yields two
providers with two `source_name`s, each still `+bidask`-suffixed; `tests/invariants/…:195`'s stub
updated to the derived label rather than the literal.

**Phase 3 — Symmetric witness, the socket budget, the alert, and the switch.**
Generalize `cross_broker_shadow` to `(primary, secondary)` chosen from config, mStock becomes the
mirrored witness when Upstox is primary, and the `ee0372b` L1 corroboration reads the *peer*
whichever it is (B5). Three attached pieces the review made explicit:

- **Schema, not literals.** The witness row keys on `primary`/`secondary` roles plus the actual venue
  names, replacing the hardcoded `mstock`/`upstox` record labels (`data/cross_broker_shadow.py:306-328`,
  witness label `:294`), and the tape consumers (`tools/out/measure_cross_broker_band.py`,
  `compare_feeds_20261109.py`) read the venue fields instead of the literal pair. Otherwise flipping
  the switch flips the meaning of every row written before it.
- **Per-socket key budget.** `check_limits` (`data/upstox_ws_wrapper.py:233`) sums the keys of every
  ring sharing the socket, not each ring alone (§7 Q1), and a real mixed-source process is measured
  against the 2000-`full` cap before any unattended run (§8 item 9).
- **Primary-feed-loss alert.** Losing the witness costs one log line and never a halt — that rule
  stays. Losing the **primary** must page: 03:30 token expiry and "401 stops reconnect" (§2) can
  silently demote the venue that marks the book, so wire it to the existing staleness signal in
  `forward/feed_quality.py` rather than a new subsystem.

- **Peer mark at every collar decision** (§7 Q2): when D evaluates a protective exit, journal the
  peer venue's L1 alongside its own mark in shadow mode, so a trip is attributable to "venue basis"
  vs "garbage". Without this column Phase 4 cannot tell the collar defending us from the collar
  disagreeing with another broker's fair price.

Switch shape: **one selector per runner, the existing `config.source`** (already routed through
`feed_registry.option_quote_provider_for(source, …)`, `:381-436`, and already decoupled from
`execution_broker` at `api/portfolio.py:884`) — no new knob, no schema change, and
`sources_policy`/`segments.resolve_execution_broker` stay the only place a default is decided. The
env line `OPTION_DEPTH_SOURCE=upstox|mstock` (default **mstock** = today) is only the *default* the
config consults, with the precedence sentence of §7 Q3; flipping primary is an env line or a runner
field + restart, never a merge.
Acceptance: `tests/test_cross_broker_shadow.py:336` (the self-refusal test) rewritten to assert the
*symmetric* refusal (primary never mirrors itself) with both directions, the AST rule at `:386` (no
trading path imports the witness) kept intact, the three-case precedence test of §7 Q3, a
`check_limits` test where two rings on one socket jointly exceed the cap and the second is refused,
and a fake primary-socket death that raises the `feed_quality` alert while a fake *witness* death
raises only a log line.

**Phase 4 — Shadow day on Upstox primary, scored against criteria fixed beforehand.**
Run a full session with the selector on `upstox`, execution still mStock, D and F in **shadow**
mode. The review's item 4 kills the old framing: at n=1 session a P&L difference between two marked
books is noise, not evidence, so Phase 4 produces a **scorecard, not a P&L diff**. Fix these in the
session plan *before* the bell, and report each as a count with its denominator — a verdict is only
available for a quantity measured on both sides:

| # | Criterion | Pass condition |
|---|---|---|
| S1 | Frames served with a two-sided book | ≥ the mStock baseline of the same session, from the §1b tape |
| S2 | Structurally impossible ladders published by the primary | 0 (`count_impossible_ladders.py` on that day's tape) |
| S3 | Fill-seam refusals attributable to the *venue*, not to an empty book | 0 |
| S4 | L1 vetoes by the mStock witness, split by the peer-mark column into "venue basis" vs "garbage" | every veto explained; unexplained = 0 |
| S5 | Collar decisions journaling a peer mark | 100 % coverage |
| S6 | Primary-feed-loss alert fired when the socket dropped | fires; witness-only loss does **not** fire |
| S7 | Fills that would have priced differently on the other venue | listed with both marks; no threshold at n=1 |

Only after a session scores green on S1-S6 does the selector move to enforce on a live book. This
mirrors how B, D and F were each introduced.

---

## 6. What this plan deliberately does NOT do

- Does not retire mStock. It stays the default and becomes the witness; its fills, its serve gate,
  and `tests/test_mticker_ws_fixes.py` stay untouched (AGENTS.md incident rule).
- Adds no new hand-calibrated gate. Every ceiling it introduces (age, TTL, band) is ported from the
  existing measured rule or re-measured from the tape, per `docs/OPTION-JUNK-ASK-INCIDENT-2026-11-08.md` §6.
- Invents no price. If Upstox publishes no level for a strike, the entry is refused and the exit
  keeps the last good mark — the synthetic-data ban applies to the new venue exactly as to the old.
- Does not build the failover supervisor / consolidated cross-venue risk view from the 2026-10-15
  plan; selection here is operator-set, per-runner, restart-applied.

---

## 7. Decisions (taken in the 2026-11-11 review — these are now binding on Phases 0-4)

**Q1 — Socket budget: reuse ONE Upstox socket for both roles.** The witness for an
Upstox-primary book is mStock (a different socket on a different account), so a single Upstox
socket can serve "primary for upstox runners" and "witness for mstock runners" at once, and no
venue ever mirrors itself. **Condition attached:** `check_limits`
(`data/upstox_ws_wrapper.py:233`, ceilings `MODE_KEY_LIMITS :97-98`) must account key budgets
**across every ring sharing that socket** — the 2000-`full` cap is per socket, not per ring, so
two rings on one socket can jointly exceed it and each alone looks legal. Before any unattended
mixed-source run, measure the joint ring size of a real process (§8 item 9).

**Q2 — Execution venue for Phases 0-4: marks + depth from Upstox, orders and margin stay mStock.**
Accepted as the cheap, bounded option because D (collar) and F (L1 corroboration) guard the seam.
**Addition required by the review:** log the **peer mark at every collar decision in shadow**, so a
collar trip can be attributed to "venue basis" (the two venues genuinely differ) vs "garbage" (one
venue printed an impossible level). Without that column the Phase-4 scorecard cannot tell whether
the collar is protecting us or simply disagreeing with another broker's fair price.
(b) moving execution too stays task #18 — `LiveOptionBrokerAdapter`/`LiveOptionTrader` generalized
over the order client plus Upstox margin (`brokers/upstox.py:877`) and commission wiring — after the
depth question is answered, so the order lifecycle never enters this diff.

**Q3 — One selector: `config.source`.** No separate `depth_source`. "Upstox depth + mStock chain
terms" is precisely the mixed-venue state that produced the 2026-11-05 fake take-profit, so the
plan refuses to make it expressible.
**Precedence, stated once and pinned by a test:** *the env default applies only when the runner
config carries no explicit source; an explicit `config.source` always wins.* The env line is
`OPTION_DEPTH_SOURCE=upstox|mstock` (default **mstock** = today), read at spawn the same way
`sources_policy.default_broker_source()` is read today, never re-read mid-bar. Phase 3 must ship a
test for all three cases (unset env → mstock; env=upstox + config unset → upstox; env=upstox +
`config.source=mstock` → **mstock**).

**Q4 — Write the real venue everywhere.** `quotes.source`, `option_chain_snapshots.source`,
`source_name`, `execution_engine.py:206` and the `options_api.py:247` probe order all derive from
`broker.broker_name`; the `'mstock'` server-default path (`db/models.py:741`) stops being the value
a live row gets by accident. Rationale from the review: `+bidask` is load-bearing, so a mislabel
changes fill semantics, and a certification run must state its own venue.

**Q5 (new, from the review) — Phase 4 is a scorecard, not a P&L diff.** At n=1 session a P&L
difference is noise; see §5 Phase 4 and the acceptance criteria fixed *before* the session starts.

---

## 8. Review corrections folded in (2026-11-11), and where each now lives

| # | What the review caught | Where it is fixed |
|---|---|---|
| 1 | §1 claimed D/F were "built but not yet live" while this tree ships `DEFAULT_EXIT_COLLAR_MODE = "enforce"` (`options/paper_trading.py:192`) — the plan was written against a different worktree than the one under review | §1 rewritten to state **deployed** mode with the boot timestamp, PID and the observed log counts; the header now names the reviewed commit (`75b7cc8`) and the implementation worktree |
| 2 | The headline ("10 impossible ladders vs 0") rested on untracked artifacts | Committed: `tools/out/count_impossible_ladders.py` (date-parameterised, defines "structurally impossible" in its docstring) + its outputs `tools/out/impossible_20261109.txt` and `impossible_20261111.txt`, and `tools/out/measure_feed_cadence.py` + `cadence_20261111.txt` for §1b. §1 now cites packet denominators per venue and the command to reproduce; the tapes themselves stay runtime artifacts (AGENTS.md) |
| 3 | Two selectors (`config.source`, `OPTION_DEPTH_SOURCE`) with no stated precedence | §7 Q3: one sentence of semantics + a Phase-3 test pinning all three cases |
| 4 | Phase 4 compared P&L at n=1 | Phase 4 replaced by a scorecard with criteria fixed before the session (§5) |
| 5 | The 10 s freshness ceiling was calibrated on **mStock's** print cadence, and Upstox's was unmeasured | Measured today (§1b): per-**leg** p99 is 16.2 s on mStock (quietest leg silent 106.6 s) against 1.87 s on Upstox (41.5 s). mStock survives its own 10 s ceiling only through the socket-liveness rescue at `brokers/mstock.py:1118-1143`, so Phase 0 takes **per-venue defaults from the tape** *and* decides that rescue explicitly instead of inheriting a constant whose safety was unexamined |
| 6 | B2 was described as a `HotStrikeRing` fix while the `int()`/`isdigit` family spans ~7 sites | Phase 1 is now one venue-neutral key-normalization pass (`:472, :1581, :1603, :1761-1764, :2035, :2107, :2352`), registry keys opaque strings, translation only at the broker boundary, plus a "ring empty after recenter" assertion so a silent zero-subscription becomes a test failure |
| 7 | `cross_broker_shadow`'s sweep hardcodes `mstock`/`upstox` record labels (`:306-328`, `:294`), so the table's meaning flips when the switch flips | Phase 3: the row becomes `primary`/`secondary` plus the actual venue names; `shadow_diff` consumers (`tools/out/measure_cross_broker_band.py`, `compare_feeds_20261109.py`) read the venue fields, not the literal pair |
| 8 | 03:30 token expiry + "401 stops reconnect" can silently demote the primary mid-session and the plan said nothing about alerting | Phase 3 ships a **primary-feed-loss alert** on the existing `forward/feed_quality.py` staleness signal — no new subsystem; the witness rule ("missing second broker costs one log line, never a halt") stays, but losing the *primary* must page |
| 9 | Mixed-source processes mean two rings sharing one Upstox socket against a **per-socket** 2000-key `full` cap | Phase 3 condition from Q1: `check_limits` budgets across rings sharing the socket, and a real mixed-source process is measured against the cap before any unattended run |

None of the nine changes the design; 1-3 are documentation/decision fixes (this commit), 4-6 are
scope folded into Phases 0/1/4, and 7-9 are small engineering additions inside Phase 3.

---

## 9. What to read in what order

§1b (today's measured comparison) and §4 (blockers) first, then §7 (the four decisions), then the
phase that owns the decision you disagree with. Effort estimate unchanged: Phase 0 and 1 are the
bulk (each ~1 day of code + tests), 2 and 3 are mechanical once 0-1 land, Phase 4 is one market
session.
