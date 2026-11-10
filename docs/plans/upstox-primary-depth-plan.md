# Plan: Upstox as the primary option-depth source, mStock demoted to witness — and the switch that makes it a setting

**Status:** DRAFT for architect + operator review. No code written.
**Worktree / branch:** `C:/learning/bt-mainmerge` @ `tmp/merge-main-verify`, HEAD `975f8d9`
(= `feature/options-trading-dev` after the 2026-11-11 local fast-forward merge).
**Supersedes nothing:** this is the *depth-source* half of
`docs/plans/upstox-dual-venue-failover-plan.md` (2026-10-15), narrowed and re-grounded in the
2026-11-08 junk-ask incident. That plan's failover supervisor is still unbuilt and is NOT
required here — this plan makes the source selectable, not auto-failing-over.

---

## 1. Why now (the evidence, not a vibe)

- mStock published **10 structurally impossible ladders** in one session; Upstox published **0**
  (`tools/out/count_impossible_ladders_20261109.py` over `logs/feed_depth/feed_*_2026-11-09.jsonl`).
- The 2026-11-08 Muhurat loss (−₹11,794.25 in five seconds) came from one mStock ask ₹177.50/share
  off parity that no gate could see — `docs/OPTION-JUNK-ASK-INCIDENT-2026-11-08.md`.
- The two mitigations the incident demanded are built but *not yet live*: **F** = cross-broker L1
  corroboration at the fill seam (`ee0372b`), **D** = collar-bounded protective exits (`975f8d9`).
  Both are written so that the primary venue may change without touching the rules.
- The deeper fix is venue choice itself: a wrong price from one broker is bounded by another
  broker's book only if we are *willing to trade on the other broker's book*.

**Goal.** One switch selects which broker's depth marks and prices our option fills, the other
broker stays a corroborating witness, and no rule in the fill seam or the exit collar knows or
cares which venue it is guarding.

**Non-goals.** No automatic failover, no cross-venue hedging, no order-routing change in the
first phases, no new hand-calibrated threshold (standing rule: thresholds come out of the tape).

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
| **Not documented anywhere:** timestamp clock source (exchange vs feed server), periodic token refresh, WS behaviour when a limit is exceeded | grep found nothing in either source | The two-clock staleness gate must **measure** Upstox stamps against a live session before it may refuse a fill on them (§5, Phase 0). |

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

**Phase 0 — Measure the Upstox clock, and build the serve gate (no behaviour change).**
Port B1 as one shared rule, not a second copy: extract mStock's two-clock age + subscribed-set +
fail-closed + ladder-TTL logic into a venue-neutral helper (next to `ladder_shape.py`) and have
`MStockBroker.get_ws_quote` call it, then add `UpstoxBroker.get_ws_quote` on top of the same helper.
Before any Upstox row is ever *served*, prove on a live session what its `timestamp`/`currentTs`
units and clock actually are (the docs do not say, §2) and set the age ceilings from that tape.
Acceptance: `tests/test_option_ladder_gates.py::TestServeGate` (the mStock pin at `:321-399`) still
green + an Upstox twin table (stale row refused, disconnected socket refused, unparseable stamp
refused, `expired:*` ladder has bid/ask zeroed). **Nothing in this phase can change a fill** —
the wrapper stays witness-only.

**Phase 1 — Make Upstox a feed-capable broker (still not selected).**
`_ws_feed` attached on login, `subscribe_tokens_ws`/`ws_subscribed_tokens` over instrument_keys,
`add_tick_listener` + coalesced bus, reconnect purge (`_purge_all` twin), `forget_token`
symmetry (B3); `HotStrikeRing` accepts a venue-neutral key and stops dropping non-digit tokens
(B2). Acceptance: `has_ws_ring_support()` True for a fake Upstox broker; a ring test that subscribes
and recenters with `NSE_FO|…` keys; the mStock numeric path unchanged.

**Phase 2 — Provenance and separation (the switch becomes honest).**
`source_name` derived from `broker.broker_name` at both sites + `execution_engine.py:206`;
`ChainBus._live` keyed by `(underlying, source)` (B6); snapshot/DB labels written from the actual
source (B4). Acceptance: a test that acquiring the same underlying from two sources yields two
providers with two `source_name`s, each still `+bidask`-suffixed; `tests/invariants/…:195`'s stub
updated to the derived label rather than the literal.

**Phase 3 — Symmetric witness + the switch.**
Generalize `cross_broker_shadow` to `(primary, secondary)` chosen from config, mStock becomes the
mirrored witness when Upstox is primary, and the `ee0372b` L1 corroboration reads the *peer*
whichever it is (B5). Switch shape: **one selector per runner, the existing `config.source`**
(already routed through `feed_registry.option_quote_provider_for(source, …)`, `:381-436`, and
already decoupled from `execution_broker` at `api/portfolio.py:884`) — no new knob, no schema
change, and `sources_policy`/`segments.resolve_execution_broker` stay the only place a default is
decided. Kill-switch stays env-shaped and mode-defaulted like D/F:
`OPTION_DEPTH_SOURCE=upstox|mstock` (default **mstock**, i.e. today), so flipping primary is an env
line + restart, never a merge. Acceptance: `tests/test_cross_broker_shadow.py:336` (the
self-refusal test) rewritten to assert the *symmetric* refusal (primary never mirrors itself) with
both directions, and the AST rule at `:386` (no trading path imports the witness) kept intact.

**Phase 4 — Shadow day on Upstox primary, then decide.**
Run a full session with `OPTION_DEPTH_SOURCE=upstox`, execution still mStock, D and F in **shadow**
mode, and compare three tapes: refused exits that should have filled, L1 vetoes, and P&L difference
vs the mStock-marked book. Only then go enforce. This mirrors how B/D/F were each introduced.

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

## 7. Questions for the architect / operator before Phase 0 starts

**Q1 — Socket budget.** Normal tier = 2 WS sockets/user. Primary Upstox depth + a second Upstox
witness socket would consume both, leaving none for a future equity feed. Option (a) reuse the
primary socket for the witness (`_ws_feed` shared, no second connection) — my recommendation;
option (b) accept 2 and document the cap. Which?

**Q2 — Execution venue during the Upstox-primary trial.** (a) marks+depth from Upstox, orders and
margin still mStock — cheapest, but we pay mStock's brokerage while trusting another venue's book,
and the collar's reference mark then comes from a venue we don't trade on; (b) move both, which
needs `LiveOptionBrokerAdapter`/`LiveOptionTrader` generalized over the broker's order client
(`options/live_trading.py:216-234,1322-1379`) plus Upstox's `calculate_order_margin` (`brokers/upstox.py:877`)
and commission profile (`options_api.py:126` hardcodes `for_broker("mstock")`). I recommend (a) for
Phases 0-4 and (b) as a separate task #18 deliverable, so the depth question is answered before the
order-lifecycle question enters the diff.

**Q3 — Is `config.source` the right single knob**, or do you want an explicit
`depth_source` distinct from `source` (which today also implies "which broker's chain terms and
historical bars")? One knob keeps the seam honest; two knobs allow "Upstox depth + mStock chain",
which is exactly the mixed-venue state that produced the 2026-11-05 fake take-profit. I recommend one.

**Q4 — Where the primary is recorded for audit.** `quotes.source` defaults `'mstock'` in the DB
(`db/models.py:741`); per-bar provenance should say which venue priced it. Do we write the real
source (needs the B4 sweep) or leave provenance to the depth jsonl? I recommend writing it — a
certification run must state its own venue.

---

## 8. Suggested review order

Read §4 (blockers) and §7 (four questions) first; §5 only after Q1-Q3 are answered, because Q3
decides whether Phase 3 is a 30-line wiring change or a new config surface. Effort estimate:
Phase 0 and 1 are the bulk (each ~1 day of code + tests); 2 and 3 are mechanical once 0-1 land;
Phase 4 is one market session.
