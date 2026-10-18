# MTM Staleness Observability — Design Draft

- **Status:** BUILT — implemented in code (verified 2026-10-06): R1/R3/R4 in `src/backtest/forward/options_bridge.py` + `src/backtest/options/paper_trading.py` (`mark_stale`, `quote_error`, throttled logging, stale-mark alerts); R2/R7 UI badge + Refresh marks in `web/static/js/`; R6 watchdog endpoint `POST /api/portfolio/runner/<id>/refresh-marks` in `src/backtest/api/portfolio.py`
- **Revised 2026-10-07:** the staleness *metric* moved from failed-bar count to **market-session seconds** since the last successful mark (`STALE_AFTER_BARS` → `STALE_AFTER_SECONDS`, grace 180 s; each runner's threshold is its bar cadence + that grace, overridable by `stale_after_seconds` in the expression), evaluated only while the session is trading — a book carried overnight has not aged. The session check is pure clock arithmetic (weekdays, 09:15–15:30 IST): the holiday calendar is a database read and sits on every mark, so it is deliberately not consulted — a market holiday reads as a normal session and ages marks. See `docs/OPTION-STRATEGY-BUILDER-ARCHITECT-PRD.md` §1.1 row 5. Everything else in this document (fields, watchdog, badge, alerts) is unchanged; `mtm_failures`/`last_quote_error` stay as logging and exit-suppression signals.
- **Date:** 2026-10-01
- **Severity:** High (silent wrong data on the Open Positions tab; risk exits operate on frozen marks)
- **Related:** `docs/DATA-FEED-IMPROVEMENTS.md`, `docs/MULTI-BROKER-PRD.md` §8.4 (deployed-view honesty), fail-closed marking guard (2026-09-23)

## 1. Problem statement

Today (2026-10-01) a restored NIFTY bull-call-spread sat in the Open Positions tab with leg
marks frozen at 373.30 / 343.85 for ~2.5 hours while the real mStock LTP fell to ~283.
Nothing in the UI said the marks were untrustworthy: the runner showed `RUNNING`,
`last_mtm_ts` updated every bar, and no error surfaced. The operator caught it only by
comparing against a broker screenshot.

The system *was* re-pulling quotes every bar. Every pull failed, and the design chose to
hold the last known mark rather than book a fake ₹0 — which is correct — but it held it
**silently**. That is the gap this document closes.

## 2. Root-cause chain (verified live today)

Three independent layers each contributed:

1. **Lost translation registry.** mStock's LTP endpoint keys on the TRADING SYMBOL
   (`NFO:NIFTY26OCT22550CE`); a numeric instrument token returns "Invalid symbol".
   `LiveChainProvider` keeps the token→contract map in per-process memory
   (`quote_providers.py`). A restart builds a fresh, empty map; the restored book's legs
   carry numeric tokens, so every `get_quote(token)` failed with an error row.
2. **Silent no-op recovery hook.** `OptionsBridge._rebind_restored_contracts()`
   (`options_bridge.py:888`) exists precisely for this, but it looks up
   `register_contract` on the provider — a method that existed only on
   `SyntheticQuoteProvider`. On the live provider the lookup returned `None`, the hook
   marked itself "rebound" and returned. Recovery ran, did nothing, and reported nothing.
3. **Fail-closed hold with no signal.** `OptionPaperBroker.update_mtm()`
   (`paper_trading.py:610-636`): on a failed quote (error row / empty), the leg keeps its
   last known `current_price`. Honest by itself (a ₹0 flash would fake a −100% loss and
   could trigger bogus exits), but the *held* state was invisible: no counter, no
   timestamp, no payload field, no log line, no UI badge.

**Fixed already (this session, uncommitted):** `LiveChainProvider.register_contract()`
added + regression test (`tests/test_live_options_wiring.py`). Verified live after restart:
marks realigned (system 352.00 vs broker 350.05 within one bar). Layers 2 and 3 are NOT
fixed — they are what this draft proposes.

## 3. Impact beyond display (why this matters to risk)

A frozen mark is not just a wrong number on the dashboard. While marks are held:

- **Manual stop/target checks** (`_maybe_manual_exit`) and **risk exits** evaluate against
  the frozen net premium — a stop that should have fired on a −15% premium move never
  sees it. The breaker and MTM curve (`_mark_to_market`) also consume the frozen value.
- **Strategy decisions** that read the book (exit logic, re-entry gates) act on stale P&L.
- **Persistence/mirroring** writes stale marks to the DB, so the corruption survives
  further restarts (the frozen 373.30 became the "last known good" the next restore holds).

So the fix must make staleness *visible* (R1-R4) and decide policy on *what may act on a
stale mark* (R5, open question).

## 4. Proposal

### R1 — Mark freshness contract (backend payload)

Per leg and per structure, add to the existing detail/aggregate rows:

| Field | Type | Meaning |
|---|---|---|
| `mark_ts` | ISO-8601 UTC | wall-clock time of the last SUCCESSFUL quote mark (`OptionPosition.last_updated` already exists, `paper_trading.py:163` — currently never exposed) |
| `mark_stale` | bool | runner is RUNNING **and** the market session is open **and** the last successful mark is older than `stale_after_seconds` — elapsed time, not bar count (2026-10-07) |
| `quote_error` | str \| None | last failure reason (`"empty quote row"`, `"Invalid symbol"`, exception text), for tooltips/logs |

Implementation sketch:
- `update_mtm` failure branch: increment `position.mtm_failures` and store
  `position.last_quote_error`; success branch clears the error. (Counter exists nowhere
  today; add to `OptionPosition` + persistence columns are optional — in-memory is enough
  for display, DB columns only if R4 alerting needs history.)
- `options_bridge.open_structures_snapshot` / `options_summary`: surface the three fields;
  `portfolio_manager` aggregate rows pass them through unchanged (it already merges
  `positions_detail()` and `open_structures_detail` rows).
- Equity legs are out of scope: an equity mark comes from the runner's own bar stream,
  whose staleness is already modeled (`data_feed_stale`, runner `stale` flag).

### R2 — UI staleness badge

- `portfolio.js` position rows + `deep_dive.js` leg rows: when `mark_stale`, render a
  `⚠ stale mark` badge next to the price (same visual language as the existing `⏸`
  frozen badge), tooltip: `last good quote <mark_ts local> — <quote_error>`.
- The aggregate P&L summary line counts stale positions ("2 pos · 1 stale mark") so the
  number can't be trusted silently.
- Honesty rule (PRD §8.4 spirit): a stale mark renders with its age; it is never
  restyled to look current, and never replaced by ₹0.

### R3 — Fail-loud recovery hooks

- `_rebind_restored_contracts`: if the provider lacks `register_contract`, log **ERROR**
  ("restored legs cannot rebind — <provider> lacks register_contract") instead of
  silently completing. The same applies to any duck-typed hook that resolves to None.
- Make `register_contract` part of the documented quote-provider protocol
  (`paper_trading.QuoteProvider`), and add a conformance test that instantiates every
  provider class and asserts the method exists — the exact class of bug that bit today
  (synthetic provider had it, live provider didn't).

### R4 — Quote-failure observability

- Rate-limited WARNING log: first failure per (runner, symbol) immediately, then at most
  one per 10 minutes while it persists (MTM runs every bar — unthrottled logging at
  1/min/leg is noise; today's incident produced zero log lines, which is worse).
- Optional (architect's call): emit an alert through the existing alerts/notifier when a
  RUNNING runner holds a stale mark for > 5 bars. The plumbing exists
  (`backtest/alerts/`); this is a policy question, not a technical one.

### R5 — Registry durability (alternative considered)

Options for surviving restarts, in order of preference:

1. **Rebind from the leg's own `trading_symbol`** (what the shipped fix does): restored
   legs carry the symbol; `register_contract` maps `token → symbol` on the first bar.
   Cheap, self-healing, no schema change. ✔ implemented
2. Persist the token→symbol map in the DB alongside structures. Rejected for now:
   duplicates data the book already carries; adds a migration for no extra guarantee.
3. Make `LiveChainProvider.get_quote` fall back to "key looks numeric → try the symbol
   registry built from ANY chain fetch". Rejected: half-fixes the symptom, keeps the
   silent-failure window between restart and first chain fetch.

### R6 — Position watchdog (auto-repair + operator triggers)

A periodic self-check, independent of the bar clock, that *does the needful* rather than
only reporting:

```
 every 60s (timer)  ─ or ─  manual "Refresh marks" button (per runner)
 ┌────────────────────────────────────────────────────────────────┐
 │ for each RUNNING runner with open positions:                   │
 │   mark_age = now − last successful mark (per leg)              │
 │   if mark_age > 2 min:                                         │
 │     1. re-bind leg via its stored trading_symbol               │
 │        (bypasses the token→contract registry entirely —        │
 │         the symbol is always valid; today's failure mode       │
 │         cannot recur through this path)                        │
 │     2. re-quote the leg and update the mark (same fail-closed  │
 │        rules — repair, never invent prices)                    │
 │     3. still failing → set mark_stale, keep quote_error,       │
 │        log once, notify once                                   │
 └────────────────────────────────────────────────────────────────┘
```

Guardrails (agreed 2026-10-01):

- **One repair routine, two triggers.** The button calls the same watchdog endpoint
  (`POST /api/portfolio/runner/<iid>/refresh-marks`); both paths write the same audit
  entry, so a manual press can never mask what the timer would have caught.
- **Highlight is state, notification is an event.** The stale badge (R2) renders for as
  long as the mark is stale; the toast/alert fires **once per staleness episode**
  (on transition healthy→stale), never on every 60s cycle. If the user misses it, the
  badge + the watchdog's auto-repair attempts still cover them.
- **Rate-limit the manual trigger** (~10 s per runner): it hits the broker API and mStock
  dies at ~1 req/s — a spamming user must not become an outage.
- **The refresh result is shown inline**, not just a spinner: `refreshed 14:32:05 —
  still failing: Invalid symbol`. A press that doesn't fix it must be informative.

### R7 — UI affordances (extends R2)

- Row highlight + `⚠ stale mark` badge (R2) plus a **Refresh marks** button on the
  runner card while any of its marks is stale.
- Notification via the existing toast/alert widget on the healthy→stale transition.

## 5. Edge cases and non-goals

- **Genuine ₹0 marks** (deep-OTM worth exactly 0) must NOT be flagged stale. Distinguish
  via the `error` key on the quote row — the existing guard already does; the staleness
  field must reuse that same distinction, never `price <= 0`.
- **Paused/stopped runners:** marks are legitimately frozen; the existing `⏸` badge
  covers them. `mark_stale` is defined only while RUNNING.
- **Market close:** no bars arrive, MTM never attempts, so bar-count-based staleness
  (R1) stays false after 15:30 — correct. Wall-clock-based staleness would light up the
  whole evening; this is why R1 counts MTM *attempts*, not minutes. The R6 watchdog uses
  a wall-clock `mark_age`, so it must **gate itself to market hours** (reuse the
  runner's existing trading-window check) — otherwise it hammers the broker API and
  notifies all night.
- **Synthetic providers:** `mark_ts`/`mark_stale` work unchanged (same `update_mtm`
  path); no special-casing.
- **Non-goals:** changing fail-closed hold semantics; bid/ask execution realism
  (`BidAskQuoteProvider`, already separate); feed poll cadence; option-chain snapshot
  TTL (900 s) tuning.

## 6. Acceptance criteria

1. Integration (regression for today): restart with an open structure → first bar after
   resume refreshes leg marks from live quotes. *(test exists: `test_register_contract_binds_numeric_token_to_trading_symbol` + bridge-level test to add)*
2. Force a quote provider to fail for ≥ 2 bars while RUNNING → payload shows
   `mark_stale: true`, `quote_error` set, `mark_ts` = last success; UI shows the badge.
3. A quote row with `ltp: 0` and NO error key → mark updates to 0, `mark_stale` stays
   false (genuine zero).
4. Provider without `register_contract` → ERROR logged at rebind time; conformance test
   fails for a provider missing the method.
5. 10 consecutive failed MTM cycles produce ≤ 2 WARNING log lines (throttle proof).
6. Watchdog: with a broken token→contract binding injected, the 60s pass repairs marks
   within 2 cycles WITHOUT a restart (this is the property today's fix only half-provides —
   it repairs on the next bar only if the rebind path itself works).
7. Manual refresh: button press calls the same repair routine, respects the 10 s
   rate-limit (second press shows cooldown, no broker call), and writes an audit entry
   identical in shape to the timer's.
8. Notification fires exactly once on a healthy→stale transition across a 10-minute
   stale episode (no repeat toasts).
9. Outside market hours the watchdog performs zero broker calls and emits zero
   notifications.
10. Live drill before close of business: kill the broker session (logout), wait 3 bars,
   confirm the dashboard visibly says "stale mark" instead of showing yesterday's price
   as current.

## 7. Rollout & rollback

- R1/R3/R4 are additive (new JSON fields, new log lines) — no API contract breaks; old UI
  ignores unknown fields. Ship together.
- R2 is static JS/CSS — no server restart semantics beyond the usual; rollback = revert.
- R6 adds one background timer + one endpoint; ship behind a simple config flag
  (`MTM_WATCHDOG=on/off`) so it can be disabled without reverting R1-R5. R7 depends on
  R1+R6.
- No DB migration unless R4 alerting needs failure history (decide in review).

## 8. Open questions for the architect

1. **Should risk machinery refuse stale marks?** Minimum proposal: while `mark_stale`,
   manual stop/target checks and strategy exits log loudly and the audit trail records
   the blind spot. Stronger option: freeze *new* entries (not exits) on a runner whose
   book is stale. Which side of the honesty/safety line do we want?
2. ~~`STALE_AFTER_BARS` default: 2 or 3?~~ **Superseded 2026-10-07:** the counter is no
   longer the staleness clock. `STALE_AFTER_SECONDS` starts at a 180 s grace (sized to
   clear the 60 s R6 watchdog pass) and each runner's threshold is that grace plus its
   bar cadence in market minutes, overridable by `stale_after_seconds` in the
   expression; per-underlying values come from the §8.1 latency calibration in the
   options PRD.
3. Alert integration (R4): wire into `backtest/alerts/` now, or wait until the badge has
   earned trust in paper for a week?
4. Should `mark_stale` gate the equity-curve `record=True` MTM points (today the curve
   silently records frozen equity for every stale bar)?
