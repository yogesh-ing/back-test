# Implementation plan — Upstox primary depth (companion to `upstox-primary-depth-plan.md`)

**Status:** constructed 2026-11-11 by the reviewing architect against plan `@9ee25c5` and the code
in `arena/16087b3a-back-test`. Phase 0 is specified to file/signature level; Phases 1–3 to task
level; Phase 4 is a session runbook. **No code is written yet — this is the build order.**
**Subject to:** plan §7 decisions (binding) and plan §6 non-goals. Where this doc and the plan
disagree, the plan wins and this doc is wrong.

## 0. Ground rules (from AGENTS.md + plan §6 — they constrain the diff, not just the review)

1. **Clock policy:** no `datetime.utcnow()`, bare `datetime.now()`, or `replace(tzinfo=None)` in
   any module that trades, prices, or persists. Everything goes through
   `backtest.timekeeping` (`now_utc`, `from_market_clock`, `duration_seconds`, `EXCHANGE_TZ`).
   `tests/test_timekeeping_policy.py` AST ratchet must not grow.
2. **Thresholds come out of the tape.** Every constant introduced below carries a comment citing
   the tape file it was measured from. No hand-calibration.
3. **mStock behavior is pinned, not redesigned.** Three suites are ratchets and must stay green
   *unchanged* throughout Phase 0:
   - `tests/test_option_ladder_gates.py::TestServeGate` (`:321-399`) — the serve-gate pin;
   - `tests/test_option_ladder_gates.py::TestLadderSanitizing` + reconnect-generation test;
   - `tests/forward/test_phase2_ws_ticks_and_quotes.py` — pins `get_ws_quote` end-to-end
     (quiet-OTM rescue at `:224`, post-close frozen-tick refusal at `:233-274`, mid-day rescue
     at `:673-683`). *(This suite is the plan's "bit-for-bit" requirement in test form.)*
4. **`data/mticker.py` is not touched** (AGENTS.md fragile-code rule) and neither is
   `tests/test_mticker_ws_fixes.py`.
5. **Fail-closed is the default.** Every new serve path returns `{}` on doubt; no invented price
   (synthetic-data ban applies per venue).
6. Commit granularity: one commit per task (T0.1 …), each with its tests green before the next.

---

## Phase 0 — the shared serve gate (no behaviour change)

**Goal:** one venue-neutral gate rule, two thin callers, per-venue policy constants measured from
the tape. After T0.1–T0.4, *no fill can change* — every existing mStock serve/refuse decision is
identical, and the Upstox twin is not reachable from any trading path yet.

### T0.1 — new module `src/backtest/data/depth_serve_gate.py`

Sibling of `src/backtest/data/ladder_shape.py` (the plan's "next to `ladder_shape.py`"). Contains
the three pieces moved verbatim from `brokers/mstock.py`, plus policy. No broker imports —
everything arrives as primitives, so the module is trivially unit-testable.

```python
# dataclasses (frozen) — the ONLY place per-venue ceilings live
@dataclass(frozen=True)
class ServeGatePolicy:
    venue: str                      # "mstock" | "upstox" — for logs only
    max_age_seconds: float          # price-freshness ceiling (per-token)
    ladder_ttl_seconds: float       # depth trust window (mStock: WS_LADDER_TTL_SECONDS=120.0)
    socket_liveness_seconds: float | None = None   # None ⇒ same as max_age_seconds
    liveness_rescue: bool = True    # "socket alive AND token subscribed" bypass (see T0.5)

MSTOCK_SERVE_POLICY = ServeGatePolicy(
    venue="mstock",
    max_age_seconds=10.0,      # unchanged, and only safe WITH liveness_rescue=True
    ladder_ttl_seconds=120.0,  # = MStockBroker.WS_LADDER_TTL_SECONDS
    liveness_rescue=True,      # load-bearing: mStock per-leg p99 is 16.2s > 10s (cadence_20261111.txt)
)
UPSTOX_SERVE_POLICY = ServeGatePolicy(
    venue="upstox",
    max_age_seconds=10.0,      # PROVISIONAL — ~5x measured per-leg p99 of 1.87s (cadence_20261111.txt);
                               # finalized by T0.5 + the full-session re-run of measure_feed_cadence.py
    ladder_ttl_seconds=120.0,  # measured longest leg silence 41.5s fits well inside (cadence_20261111.txt)
    liveness_rescue=...,       # DECIDED BY T0.5 — see the decision tree there
)

def stamp_age_seconds(ts: Any) -> float | None:
    """MOVED verbatim from MStockBroker._parse_legacy_ws_timestamp_age (:1028-1056).
    Feed stamps via from_market_clock (naive = exchange time); time-only formats
    paired with today's EXCHANGE_TZ session date."""

def expire_ladder(row: dict, key: str, now_mono: float, ttl_seconds: float) -> None:
    """MOVED verbatim from MStockBroker._expire_ladder (:1176-1216). In-place
    downgrade: zero bid/ask, empty depth, ladder_status -> 'expired:<status>'.
    Never raises. Unlabelled rows (status is None) untouched."""

def serve_ws_row(
    *,
    row: dict | None,             # raw cached row (caller passes a COPY)
    key: str,                     # token / instrument key for logs
    now_mono: float,
    is_connected: bool,
    last_any_tick_mono: float,
    token_subscribed: bool,
    policy: ServeGatePolicy,
    max_age_override: float | None = None,   # tests keep passing max_age_seconds=10.0 explicitly
) -> dict:
    """The gate. Returns the validated row (mStock-shaped) or {} — fail-closed.

    Order of checks, exactly as MStockBroker.get_ws_quote does today:
      1. row present; wrapper connected           else {}
      2. age from ltp_mono ?? recv_mono;
         over ceiling -> rescue iff (policy.liveness_rescue and
         now-last_any_tick_mono <= liveness limit and token_subscribed)  else {}
      3. no mono stamps -> stamp_age_seconds(timestamp): None / < -5.0 / > ceiling -> {}
      4. copy row, setdefault('source','ws'), expire_ladder(...) in place
    """
```

Docstring of `serve_ws_row` keeps the two-clock rationale from `MStockBroker.get_ws_quote`
(:1058-1092) — it is the specification.

### T0.2 — `MStockBroker` becomes a thin caller (`src/backtest/brokers/mstock.py`)

- `get_ws_quote(...)` keeps its **exact public signature**
  (`instrument_token, max_age_seconds=10.0, *, socket_liveness_seconds=None,
  ladder_ttl_seconds=None`) — `quote_providers.py:2026,2135` and both test suites call it this
  way. Body shrinks to: gather primitives (`self._ws_feed.get_quote`, `is_connected`,
  `last_any_tick_mono`, `self._ws_subscribed_tokens` under `self._ws_lock`) and call
  `serve_ws_row(..., policy=MSTOCK_SERVE_POLICY, max_age_override=max_age_seconds)`.
  The `except Exception → return {}` safety net stays in the method.
- `_parse_legacy_ws_timestamp_age` and `_expire_ladder` become delegations to the module
  (keep the methods — they may be referenced by name in tests/tools; grep first, delete only if
  unreferenced).
- `WS_LADDER_TTL_SECONDS = 120.0` class attr **stays** (it is part of the pinned surface);
  `MSTOCK_SERVE_POLICY.ladder_ttl_seconds` must be asserted equal to it in a test so the two can
  never drift.
- Behavior invariant to assert in review: for every input in the pin suites' tables, the refactored
  method returns the identical dict (the suites prove this; do not weaken any assertion).

### T0.3 — `UpstoxBroker.get_ws_quote` (`src/backtest/brokers/upstox.py`)

- Same public signature as mStock's (keys are instrument-key strings, not ints — type it `Any`,
  normalize with `str(...)`). Body mirrors T0.2 with `UPSTOX_SERVE_POLICY` and the *same*
  wrapper primitives (`data/upstox_ws_wrapper.py` already exposes `get_quote`,
  `last_any_tick_mono`, `is_connected`, `subscribed_keys`). Subscribed-set membership:
  `str(key) in wrapper.subscribed_keys()`.
- **Not reachable from trading yet:** `UpstoxBroker` gains no `_ws_feed` assignment in this phase
  (that is Phase 1), so `get_ws_quote` returns `{}` in production until Phase 1 attaches a feed.
  The method exists so the gate is tested against real Upstox-shaped rows before the socket is
  promoted.
- Legacy-stamp path: Upstox rows carry `"timestamp": _epoch_iso(currentTs)`
  (`upstox_ws_wrapper.py:551-624` build) and `recv_mono`, so the mono path normally serves; the
  wall-stamp fallback goes through the same `stamp_age_seconds`. The `currentTs`-vs-`ltp` question
  (which stamp prices the age) is answered by T0.5 before `UPSTOX_SERVE_POLICY` loses its
  "PROVISIONAL" comment.

### T0.4 — tests (new file `tests/test_depth_serve_gate.py` + additions to existing files)

| Test | Asserts |
|---|---|
| `TestServeGateUpstoxTwin` (in `test_option_ladder_gates.py`, mirroring `:321-399`) | stale row refused; disconnected socket refused; unparseable stamp refused; future-skewed stamp refused; `expired:*` ladder has bid/ask zeroed and depth `{}`; quiet leg on live subscribed socket serves (when `liveness_rescue=True`) |
| `TestPolicyIndependence` | constructing/altering `UPSTOX_SERVE_POLICY` cannot move `MSTOCK_SERVE_POLICY` (frozen dataclass + equality pin of `MSTOCK_SERVE_POLICY.ladder_ttl_seconds == MStockBroker.WS_LADDER_TTL_SECONDS`) |
| `TestRescueIsLoadBearing` | with `liveness_rescue=False` the 40s quiet-leg case that `TestServeGate::test_quiet_leg_on_a_live_subscribed_socket_still_serves_depth` serves **is refused** — proving the rescue, not the ceiling, is what keeps mStock fills alive (pins the plan §1b finding) |
| `test_stamp_age_epoch_and_naive_forms` | `stamp_age_seconds` handles: aware ISO, naive ISO (exchange), time-only `%I:%M:%S%p`/`%H:%M:%S`, Go-zero sentinel string, epoch-seconds ISO as Upstox writes it |
| Unchanged suites | `TestServeGate`, `TestLadderSanitizing`, `test_phase2_ws_ticks_and_quotes.py` green with zero diff to their assertions |

### T0.5 — the two Phase-0 measurements (tools, not app code)

Both run off the existing `ENABLE_FEED` recorder tapes — **no app instrumentation needed**
(`record_depth_packet` already captures raw frames: `upstox_ws_wrapper.py:507`).

1. **`currentTs` vs `ltp` field separation.** New read-only script
   `tools/out/measure_stamp_sources.py <date> --broker upstox`: per frame, compare `currentTs`,
   `ltpc.ltt`/`last_trade_ts`, and `recv_utc`; report which one tracks receipt (±0.4 s band) and
   which tracks exchange prints. Output committed as `tools/out/stamps_<date>.txt`. This decides
   which stamp `serve_ws_row`'s wall-clock fallback may trust.
2. **Deliberate quiet token.** Using `measure_feed_cadence.py -v` on a full session: pick the
   least-printing subscribed OTM leg and verify from the tape whether its silences (up to 41.5 s
   measured) are *illiquid-but-healthy* (socket frames continue for other keys) or *feed loss*.
   This is the `liveness_rescue` decision tree:
   - silences coexist with healthy socket traffic ⇒ **`liveness_rescue=True`** for Upstox too
     (quiet legs keep two-sided marks; ceiling alone would strip them every quiet spell);
   - silences coincide with socket-wide gaps ⇒ **`False`** (per-leg aging only; quiet legs
     degrade to REST LTP via `get_option_quote`'s fallback — fail-closed for entries, honest
     marks).
   - The provisional `max_age_seconds` is finalized from the full-session
     `measure_feed_cadence.py` re-run (plan §1b: "re-run after the close") — the constant's
     comment cites the committed output file, per ground rule 2.

### Phase 0 acceptance (plan §5, verbatim + additions)

- [ ] `TestServeGate` green unchanged; `test_phase2_ws_ticks_and_quotes.py` green unchanged.
- [ ] Upstox twin table green (T0.4).
- [ ] Per-venue ceilings independently mutable (T0.4) and provenance of each constant cited.
- [ ] Whichever rescue decision T0.5 takes is pinned by a test.
- [ ] No trading path can reach `UpstoxBroker.get_ws_quote` with a live feed (no `_ws_feed`
      attachment exists yet) — assert in a test that an un-attached broker returns `{}`.
- [ ] `tests/test_timekeeping_policy.py` green (no new clock call sites).

---

## Phase 1 — Upstox becomes feed-capable (still not selected)

One commit per line; each lands with tests before the next.

1. **Key-normalization pass** (`options/quote_providers.py`) — the B2 family in one change:
   `:472`, `:1581` (`pin_tokens`), `:1603` (`unpin_tokens`), `:1761-1764` (`recenter`),
   `:2035` (`ring_book`), `:2107`, `:2352`. Ring + registry store **opaque strings** end to end;
   numeric tokens remain valid keys; translation to wire format happens only inside
   `MStockBroker.subscribe_tokens_ws` (int) / the Upstox equivalent (instrument key).
   Plus: **"ring empty after recenter" assertion/log** — today's silent zero-subscription becomes
   a test failure.
   *Acceptance:* ring subscribes + recenters with `NSE_FO|…` keys (subscribed set non-empty); the
   numeric-token path subscribes the identical set as before (bit-for-bit — `tools/out/qa_ring_probe.py`
   stays a valid probe); a mixed ring with both spellings subscribes both.
2. **Upstox WS surface on `UpstoxBroker`** (`brokers/upstox.py`): `_ws_feed` attached on login,
   `subscribe_tokens_ws`/`ws_subscribed_tokens`/`pin_ws_tokens`/`forget_token` over instrument
   keys, `add_tick_listener` + coalesced 150 ms bus (port from `data/mstock_ws_wrapper.py:246-307`).
   *Acceptance:* `has_ws_ring_support()` True for a fake Upstox broker (`quote_providers.py:2118`
   tests `broker.__dict__["_ws_feed"]` — the attribute must exist in `__dict__`).
3. **Reconnect purge twin** (`data/upstox_ws_wrapper.py:371` `_on_open`): clear the cache and
   reset `last_any_tick_mono` — "no row may outlive the socket that wrote it"
   (`mstock_ws_wrapper.py:176` precedent; pin with the twin of
   `test_reconnect_starts_a_new_cache_generation`, `test_option_ladder_gates.py:302`).
4. **Wire `get_option_quote`** (`brokers/upstox.py:776`) to try `get_ws_quote` first (same
   WS-first/HTTP-fallback shape as `mstock.py:1800-1824`), so the row the gate validates is the
   row the fill seam sees.

---

## Phase 2 — provenance and separation

1. **B4 sweep, one rule: the label comes from the broker that produced the row.**
   `quote_providers.py:437,1945` (`source_name` from `broker.broker_name`),
   `engine/execution_engine.py:206`, `options/chain_snapshots.py:185`, every insert into `quotes`
   that leans on `db/models.py:741`'s `server_default 'mstock'` (set the column explicitly;
   the default stops being what a live row gets), `api/options_api.py:247` probe order →
   configured source first.
2. **`ChainBus._live` keyed `(underlying, source)`** (`forward/feed_registry.py:226-240`) — the
   stored `key=(key, source_key, "")` becomes the lookup key; refcount/identity contract holds per
   pair. *Acceptance:* same underlying from two sources ⇒ two providers, two `source_name`s, both
   `+bidask`-suffixed (`feed_registry.py:381-436` label path unchanged); `tests/invariants/…:195`
   stub updated to the derived label.

---

## Phase 3 — symmetric witness, budget, alert, switch

1. **Symmetric witness** (`data/cross_broker_shadow.py`): `maybe_start` refuses *any* primary
   mirroring itself (both directions); sweep row becomes `primary`/`secondary` roles + actual
   venue names (replace literals at `:294`, `:306-328`); tape consumers
   (`tools/out/measure_cross_broker_band.py`, `compare_feeds_20261109.py`) read venue fields.
   The `ee0372b` L1 corroboration reads the **peer** whichever venue is accused.
   *Acceptance:* `test_cross_broker_shadow.py:336` rewritten to the symmetric refusal, both
   directions; AST rule (`:386`, no trading path imports the witness) kept intact.
2. **Per-socket key budget** (`data/upstox_ws_wrapper.py:233` `check_limits`): sum keys across
   every ring sharing the socket against `MODE_KEY_LIMITS` (`:97-98`). *Acceptance:* test where
   two rings each legal jointly exceed 2000 `full` and the second subscribe is refused.
3. **Primary-feed-loss alert:** wire to `forward/feed_quality.py` staleness signal (no new
   subsystem): primary loss pages; witness loss logs one line (plan rule). *Acceptance:* fake
   primary-socket death raises the alert; fake witness death raises only a log line.
4. **Peer mark at collar decisions** (`options/paper_trading.py` collar seam, `:1261-1330`): in
   shadow mode journal the peer venue's L1 next to our mark on every collar evaluation (column
   that Phase 4's S4/S5 consume).
5. **The switch:** `config.source` remains the only knob; `OPTION_DEPTH_SOURCE` is its default
   when unset. *Acceptance:* three-case precedence test exactly as plan §7 Q3
   (unset→mstock; env=upstox+unset→upstox; env=upstox+config=mstock→**mstock**), read at spawn,
   never re-read mid-bar.

---

## Phase 4 — shadow day, scored (runbook)

1. **Before the bell:** D and F on `shadow`; selector `OPTION_DEPTH_SOURCE=upstox`; execution
   stays mStock; scorecard S1–S7 (plan §5 Phase 4) written into the session plan with each
   criterion's *computing command* fixed up front:
   - S1/S2 → `count_impossible_ladders.py` + `measure_feed_cadence.py` on the day's tapes
     (state which "coverage" definition — fix the `empty_ladder` vs `no book` vocabulary drift
     between the two scripts first, plan-review §10.4);
   - S3 → fill-seam refusal log lines (`price_used` / `sanity_flag` kinds in the recorder);
   - S4/S5 → the Phase-3 peer-mark column + `shadow_diff` rows;
   - S6 → the Phase-3 alert's own test + live evidence line;
   - S7 → paired marks recorded by the peer-mark journal.
2. **After the close:** re-run both tools on the full session (the §1b partial numbers are
   explicitly provisional); report every criterion as count + denominator.
3. **Enforce only on S1–S6 green.**

---

## Risk register (what each phase could break, and the tripwire)

| Risk | Phase | Tripwire |
|---|---|---|
| Shared-helper extraction changes an mStock serve decision | 0 | `TestServeGate` + `test_phase2_ws_ticks_and_quotes.py` green unchanged (ground rule 3) |
| The liveness rescue is dropped as "unneeded" | 0 | `TestRescueIsLoadBearing` fails; T0.5 decision tree forces an explicit, tape-cited choice |
| Key normalization changes the mStock subscribed set | 1 | bit-for-bit ring test + `qa_ring_probe.py` |
| Stale rows survive an Upstox reconnect | 1 | purge-twin test (`:302` twin) |
| A provenance label lies and changes collar semantics | 2 | two-providers test + `+bidask` assertions; every `quotes` insert sets `source` explicitly |
| Flipping the switch flips `shadow_diff` row meaning | 3 | schema test: rows written under both primaries are distinguishable |
| Two rings blow the per-socket key cap | 3 | joint-cap refusal test |
| Silent primary demotion mid-session | 3 | S6 + alert test |

## Effort (unchanged from plan §9)

Phase 0 ≈ 1 day (T0.1–T0.4) + one measurement session (T0.5); Phase 1 ≈ 1 day; Phases 2–3
mechanical once 0–1 land (~1 day combined); Phase 4 = one market session + after-close analysis.
