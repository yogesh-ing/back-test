# Options Forward Testing — Task Tracker

> **Goal:** make `instrument.type == "option"` runners a *real* forward test —
> a book that re-prices every bar, exits on its own, and whose P&L is visible
> everywhere the equity book already is.
>
> **Status legend:** `TODO` · `WIP` · `DONE` · `BLOCKED`
>
> Companion docs: [OPTIONS-PAPER-LIVE.md](OPTIONS-PAPER-LIVE.md) (options
> pipeline) · [FORWARD-TESTING.md](FORWARD-TESTING.md) (forward engine) ·
> [Gap-Analysis-Remediation-PRD.md](Gap-Analysis-Remediation-PRD.md) (§P2 is
> the same finding as A2 below).

## Why this tracker exists

The Gap remediation (G3.1/G3.2) wired a strategy's `MarketView` into the
options expression layer **and** into the portfolio manager, so an option
runner *can* open a structure:

```
strategy view → OptionsBridge → chain → selector → structure → OptionPaperBroker.execute_structure()
```

Review finding (2026-09-15, verified end-to-end): that is where the loop stops.

| Verified symptom | Evidence |
|---|---|
| Book never re-prices | `OptionsBridge` calls `set_spot()` only at entry (`forward/options_bridge.py`); nothing calls `broker.update_mtm()` in the forward path |
| Nothing can exit | no exit policy in the bridge; `ExpiryManager.process_expiries()` is only called by `engine/option_backtest_driver.py` and `web/options_api.py` |
| Option P&L invisible | `StrategyRunner.equity()/unrealized_pnl()/daily_pnl()` read the **equity** `Portfolio`; option numbers live only in `get_state()["options"]` |
| Unreachable from the UI | `POST /api/portfolio/runner/create` accepts `instrument`, but `portfolio.js` never sends it; `/api/forward/start` rejects it (400) |
| Unrealistic prices | `SyntheticFeed` seeds NIFTY at ~₹392 (not ~24,800); theta is discounted from wall-clock `datetime.now()` while bars replay on synthetic timestamps |

Reproduction (before A1/A2, on the commit this tracker starts from):

```
rally 20 bars → bull_call_spread opens (25200/25250 CE, ₹2,063 debit)
then NIFTY collapses 7,000 pts over 24 bars:
  structure unrealized pnl : 0.00        <-- still zero
  broker total_equity      : 999,960.00  <-- only the ₹40 fees moved
  runner equity/open_pnl   : 1,000,000.00 / 0.00
  structures open          : 1           <-- and still 1 after a fresh rally
```

## Task board

| ID | Task | Phase | Status | Depends on |
|----|------|-------|--------|-----------|
| **A1** | Per-bar mark-to-market for forward option books | A · Live | DONE | — |
| **A2** | Fold option P&L into runner equity, buckets and breakers | A · Live | DONE | A1 |
| **B1** | Exit policy: view flip / neutral, stop, target, time stop | B · Exits | DONE | A1 |
| **B2** | Expiry square-off + roll inside the forward loop | B · Exits | DONE | B1 |
| **B3** | Close plumbing: `OPTION_EXIT` signals, closed-structure log, metrics | B · Exits | DONE | B1, B2 |
| **C1** | Spawn UI: instrument / structure / strike / quantity controls | C · Reach | DONE | A2 |
| **C2** | Portfolio matrix + deep-dive: option columns, premium, Greeks | C · Reach | DONE | C1 |
| **D1** | Index-scale synthetic spot for NIFTY / BANKNIFTY | D · Realism | TODO | A1 |
| **D2** | Injectable quote provider (`synthetic` \| `live:mstock`) + badge | D · Realism | TODO | A1 |
| **D3** | Persist forward option books across restarts | D · Realism | TODO | A2 |
| **E1** | Fix lint baseline (`options/expiry.py` unused imports) | E · Hygiene | DONE | — |
| **E2** | Stop options tests leaking state through the dev SQLite DB | E · Hygiene | TODO | — |

Phases: **A** makes the numbers move, **B** makes the runner able to leave a
trade, **C** makes it reachable from the browser, **D** makes the numbers
honest, **E** keeps the gate green.

**Progress: A1, A2, B1, B2, B3, C1, C2 and E1 are done** — an option runner prices
every bar, is counted by the equity/bucket/breaker maths, opens *and closes*
on its own rules, settles at expiry and rolls, reports what it did in the trade
log, the metrics and the equity curve, and can now be **spawned from the
browser** with a structure, strike selection and exit plan. What remains is
reading **and** writing directions: an option runner can be spawned from the
form and read back from the matrix and the deep-dive drawer. What remains is
**D** — realism and durability: an index-scale synthetic spot (a synthetic
NIFTY still prices at ~₹392, not ~24,500), an injectable live quote provider,
and persistence across restarts.

---

## A1 — Per-bar mark-to-market for forward option books

**Status:** DONE

**Problem.** `OptionsBridge._execute()` syncs the synthetic market to the
view's spot once, at entry. From then on nothing re-prices the book, so every
leg keeps `current_price == entry_price` and `unrealized_pnl == 0` forever —
the runner holds a "position" that cannot win or lose.

**Change.**

1. New `OptionsBridge.on_bar(symbol, price, ts=None)` — the per-bar hook:
   - move the synthetic spot to the bar close (so premiums follow the
     underlying),
   - pin the quote provider's pricing clock to the **bar timestamp**
     (`SyntheticQuoteProvider.set_reference`) so time decay follows the replay
     clock, not the wall clock,
   - `OptionPaperBroker.update_mtm(provider)`,
   - return the book's unrealized P&L for the bar.
2. `StrategyRunner` calls it from `process_candle_event()` (single **and**
   pool runners) and from `apply_markdown()` whenever a bridge exists — before
   equity is marked, so the curve is built from fresh prices.
3. `OptionsBridge.summary()` gains `unrealized_pnl` / `last_spot` /
   `last_mtm_ts`; the runner's `OPTION_*` signal log gains a throttled
   `OPTION_MTM` heartbeat when the book is open.

**Files.** `src/backtest/forward/options_bridge.py`,
`src/backtest/forward/paper_runner.py`,
`tests/forward/test_options_forward_mtm.py` (new).

**Acceptance criteria.**

- [x] A rally-then-crash feed moves the open structure's unrealized P&L off
      zero and in the right direction (long spread → loss on a collapse).
- [x] Book equity on the crash bar differs from entry equity by more than the
      fee stack alone.
- [x] With a **flat** spot, advancing bars decays the premium (theta) because
      the pricing reference follows the bar clock.
- [x] Equity runners are untouched (`options_bridge is None` → zero behaviour
      change); all pre-existing forward/options tests still pass.
- [x] The hook is exception-safe: a broken quote or bad timestamp logs and
      leaves the runner alive.

**Out of scope (A2 owns it).** Folding the new P&L into `equity()`,
`daily_pnl()`, `deployed_capital()` and the instance breakers; bucket
aggregates therefore still show equity-only numbers after A1.

**Result — the review reproduction, re-run after A1.** Same runner, same feed
(rally 20 bars → bull call spread 25200/25250 CE → NIFTY collapses ~7,000 pts):

| | before A1 | after A1 |
|---|---|---|
| structure unrealized P&L | `0.00` | `-2,063.25` (the spread's full debit — correct for a 28% collapse) |
| book equity | `999,960.00` (fees only) | `997,896.75` |
| leg marks | frozen at entry premium | long `229.36 → 0.00`, short `201.85 → 0.00` |
| signal log | `OPTION_ENTRY` | `OPTION_ENTRY` + throttled `OPTION_MTM` heartbeats |

Tests: `tests/forward/test_options_forward_mtm.py` — 19 cases (spot response,
theta on the bar clock, tz-aware and unparseable timestamps, exploding quote
provider, pool runners, stress markdown, throttle, equity-runner invariance).
Targeted gate: **221 passed** (`tests/forward`, options suite, `tests/engine`).

---

## A2 — Fold option P&L into runner equity, buckets and breakers

**Status:** DONE

**Problem.** `StrategyRunner.equity()` / `unrealized_pnl()` / `daily_pnl()` /
`deployed_capital()` read the equity `Portfolio` only, so an option runner's
card, bucket aggregate and circuit breakers were blind to its option book —
Gap **P2** in the remediation PRD, still open after the Gap remediation.

**Change.** The book became a first-class contributor at the **runner** level
(so every `_bucket_*` aggregate inherits it — nothing else had to change):

| Surface | Now |
|---|---|
| `equity()` | equity portfolio + `bridge.net_pnl` (`total_equity − capital`, i.e. realized + unrealized − commission − statutory fees) |
| `unrealized_pnl()` | portfolio mark + open option legs (`OptionPaperBroker.total_unrealized_pnl`, new) |
| `realized_pnl` | portfolio realized + option legs' booked P&L (gross of costs — the options dashboard's convention; fees live in `option_pnl`/`equity`) |
| `deployed_capital()` | position value + `premium_at_risk` (net debit of open structures = their max loss; credit structures report 0) |
| `_check_instance_risk()` | unchanged code — it reads `equity()`/`daily_pnl()`, so option drawdown now trips halt/pause |
| `get_state()` | `equity` / `deployed_capital` / `realized_pnl` combined; `options` + `option_pnl` remain the drill-down |

**Acceptance criteria.**

- [x] An option loss trips the instance drawdown breaker (test uses a 1% limit
      so the assertion is not boundary-flaky; the control case with a 90%
      limit stays RUNNING).
- [x] `state["equity"]` equals `state["options"]["equity"]` for a pure option
      runner; bucket `equity` / `daily_pnl` / `deployed_capital` and
      `get_portfolio_summary()["total_equity"]` all include the book.
- [x] Equity runners' numbers are bit-identical (asserted against the raw
      portfolio properties).

**Known limit (deliberately out of scope).** A 50-point-wide spread on a ₹1M
allocation risks ~0.2% of capital, so realistic breakers need either a tight
limit or position sizing — the real risk control for option runners is the
**exit policy** in B1 (a stop on structure P&L), not the drawdown breaker.
Also: `win_rate()` / `wins` / `losses` still count equity round-trips only;
option structures join that ledger in B3.

**Result — the review reproduction, re-run after A2:**

| | before A2 | after A2 |
|---|---|---|
| runner card equity (after ~7,000 pt collapse) | `1,000,000.00` (frozen) | `997,896.75` |
| `state["equity"]` vs `state["options"]["equity"]` | `1,000,000.00` vs `997,896.75` | equal |
| bucket `paper.equity` | equity portfolio only | `997,896.75` |
| `deployed_capital` | `0.00` | `2,063.25` |

Tests: `tests/forward/test_options_equity_integration.py` — 16 cases.
Full gate: **2194 passed, 4 skipped** (at the time; E1's lint failure was still open — closed by B2).

## B1 — Exit policy

**Status:** DONE

**Problem.** V1 ignored every view that arrived while a structure was open —
it could never close one. Exits required a human clicking the dashboard (which
holds a *different* book, so it cannot even see a runner's positions). A
stop-loss that cannot stop out is not a forward test.

**Change.** New `src/backtest/options/exit_policy.py` — a pure rule evaluator
(`ExitConfig` / `ExitPolicy` / `ExitDecision`) plus the bridge lifecycle:
`expression["exit"]` is parsed (tolerantly — junk keys warn, they never block a
spawn), and every bar the bridge manages an open structure.

| Rule | Key | Fires when |
|---|---|---|
| stop loss | `stop_loss_pct` / `stop_loss_points` | mark ≤ −X% of net premium, or ≤ −₹X |
| take profit | `take_profit_pct` / `take_profit_points` | mark ≥ +X% of net premium, or ≥ +₹X |
| days to expiry | `min_days_to_expiry` (default **1**) | `(expiry − bar_date).days ≤ N` → squares off with the established `auto_square_off` reason |
| time stop | `max_bars` | held ≥ N bars |
| signal flip | `signal_flip` (default on) | view turns against the structure's direction |
| signal neutral | `neutral_bars` (default off) | N consecutive bars with no view / a NEUTRAL view |
| reverse | `reenter` (default off) | a flip-close immediately opens the opposite structure |

Precedence is risk-before-opinion (stop → target → DTE → time → flip →
neutral), so a gap-down reports the stop, not the flip. Percent rules are
skipped for credit structures (a percentage of a negative premium is
meaningless).

Supporting behaviour, all deliberately conservative:

- **No same-bar re-entry** after a close; `reenter` bypasses it only for a
  genuine flip. A stop-out cannot silently re-open the same trade.
- **Entry-side DTE guard**: a fresh structure is refused if the expiry rule
  would close it immediately, so a 1-DTE trade cannot be opened.
- **Two call sites**: risk rules run in `on_bar` (the pricing hook, so pool
  runners get them too) and are drained by the runner via `pop_exit_event()`;
  signal rules run in `on_market_view`, which the runner now calls on **every**
  bar — a viewless bar is how silence and flips are detected.
- **`OPTION_EXIT`** signal kind in the runner log, `closed_count` /
  `last_exit` / `exit_policy` / `bars_in_trade` in the summary.

**Acceptance criteria.**

- [x] Every rule has a unit test (percent, points, time, DTE, flip, neutral,
      precedence, credit-structure skip, disabled rules).
- [x] A bullish→bearish flip closes the bull call spread on that bar;
      `reenter: true` opens the bear put spread on the same bar.
- [x] `exit_reason` is set on the closed structure (`signal_flip`,
      `stop_loss`, `take_profit`, `time_stop`, `auto_square_off`).
- [x] A stop-out is logged as `OPTION_EXIT` with its P&L, and the runner is
      flat and free to take the next signal.

**Result — a full lifecycle, from one runner (rally → crash → sideways drift),
stop 40% / target 150% / neutral 4 bars / DTE 2:**

```
OPTION_ENTRY    bull_call_spread 25200/25250 expiry=2026-09-24 legs=2
OPTION_EXIT     bull_call_spread closed after 3 bars — stop -1,176 ≤ -40% of premium 2,063 — pnl -1,186
OPTION_ENTRY    bear_put_spread 24300/24350 expiry=2026-09-24 legs=2
OPTION_EXIT     bear_put_spread closed after 6 bars — 2d to expiry ≤ 2d — pnl +1,637
OPTION_BLOCKED  1d to expiry 2026-09-24 ≤ 2d — new entries paused
OPTION_BLOCKED  0d to expiry 2026-09-24 ≤ 2d — new entries paused
OPTION_ENTRY    bear_put_spread 19800/19850 expiry=2026-10-29 legs=2   ← rolls to the next expiry
```

### Two pre-existing expiry-calendar bugs found by this work

Both were unreachable while the calendar was pinned to `date.today()`; wiring
the replay clock (above) exposed them:

1. **`next_monthly_expiry(November ref)` crashed** — `date(year, month + 2, 1)`
   raised `month must be in 1..12` (and for December it skipped January).
   Now month arithmetic goes through `_first_of_month()` (year-safe).
2. **The post-expiry rollover skipped a month** — when the current month's
   expiry had passed it jumped *two* months ahead, so a reference of Nov 30
   resolved to January's expiry instead of December's. Now it is "the nearest
   monthly expiry on or after the reference", matching the duplicate
   implementation in `engine/option_backtest_driver.py` and the docstring.

Consequence of (1)+(2) before the fix: any forward test replaying past the next
monthly expiry squared off correctly and then paused entries **forever**
(`-13d to expiry … new entries paused` on every subsequent bar).

Tests: `tests/forward/test_options_exit_policy.py` — 44 cases (config parsing,
every rule, precedence, bridge lifecycle, runner wiring, expiry-calendar
regressions). Full gate: **2238 passed, 4 skipped**.

## B2 — Expiry square-off + roll

**Status:** DONE

**Problem.** Nothing in the forward path could **settle** a contract at expiry:
`ExpiryManager.process_expiries` was called only by the backtest driver and the
dashboard. B1's DTE rule squared positions off a day early (a proxy), but a
runner configured to ride into settlement held a position whose only mark was a
Black-Scholes price — cash settlement never happened and the book never booked
the result.

**Change.** The bridge now drives the canonical pipeline from its bar clock:

- `_maybe_expiry_settlement()` runs every bar after MTM and calls
  `ExpiryManager.process_expiries(..., as_of=bar_time, include_today=True)`;
- **settles on the expiry bar** — index options settle on the expiry-day close,
  which is exactly the price the bar carries (new `include_today` parameter;
  `False` keeps the old strict "after expiry" behaviour);
- settlement spot = the synthetic market's spot for that bar
  (`_GeneratorSettlementProvider`, mirroring the backtest driver's);
- the result reuses the B1 event shape: `exit_reason = "expiry_settlement"`
  (new shared constant `EXIT_REASON_SETTLEMENT`), `EXPIRED` legs, cash moved by
  intrinsic, `settled_count` in the summary;
- **roll** is the natural next entry: the calendar already follows the bar
  clock (B1), so the next view opens the next monthly expiry with a new
  structure id. No new config.
- `expression["squareoff_minutes_before"]` (default 30) passes through to the
  manager for intraday bar cycles.

### Settlement P&L bug fixed (found by wiring this in)

`settle_expired` booked **gross intrinsic** as `realized_pnl`:

| case | old | now |
|---|---|---|
| long, 100 premium, 200 intrinsic, 25 lots | `+5,000` | `+2,500` = (200 − 100) × 25 |
| long OTM (expires worthless) | `0` | `−2,500` = the premium lost |
| short ITM (100 premium, 200 intrinsic) | `−5,000` | `−2,500` |

`total_equity` anchors on starting capital + realized P&L, so the gross figure
overstated book equity by a full premium **per settled leg** — verified on a
book that should have ended at `₹107,237.50` but reported `₹122,500`. Cash
movements were always correct and are unchanged; only the P&L attribution was
wrong. It now matches `OptionPosition.close()`, which the close path always
used.

**Acceptance criteria.**

- [x] A structure held to expiry settles exactly once, `status="expired"`,
      `exit_reason="expiry_settlement"`, realized P&L in the book.
- [x] Settlement happens on the expiry bar (and still settles if the runner
      only reaches that date later).
- [x] Works with no view at all (pool runners never route views to options).
- [x] Book reconciles after settlement: `capital + realized − fees == cash`.
- [x] A roll produces a new structure id on a later expiry and the runner keeps
      trading.
- [x] The default `min_days_to_expiry=1` path still squares off early, so the
      shipped default never rides into settlement by accident.

**Result — one runner riding to settlement:**

```
rally   -> equity 1,001,176.50   open=1
through -> equity 1,003,112.00   open=1  closed=1  settled=1
           realized 1,686.75  unrealized 1,505.25

OPTION_ENTRY bull_call_spread 25200/25250 expiry=2026-09-24 legs=2
OPTION_EXIT  bull_call_spread closed after 12 bars — cash settled at expiry 2026-09-24 — pnl +1,687
OPTION_ENTRY bull_call_spread 26100/26150 expiry=2026-10-29 legs=2   <- roll
```

Tests: `tests/forward/test_options_expiry_settlement.py` (11 cases) plus the
`include_today` case in `tests/test_options_expiry.py`. Five pre-existing
settlement assertions moved to net-of-premium semantics, and five forward
fixtures were re-anchored inside a single expiry cycle (they implicitly assumed
an immortal position — B2 correctly settles one that reaches expiry).

Full gate: **2252 passed, 4 skipped** — including `tests/test_lint_baseline.py`,
so E1 is genuinely closed.

### Config bug found while testing this

`ExitConfig.from_expression` treated an **omitted** `min_days_to_expiry` as
`None`, so passing any other exit key (e.g. `{"signal_flip": false}`) silently
disabled the default square-off. An omitted key now keeps its default; only an
explicit `null` means "ride into settlement".

## B3 — Close plumbing

**Status:** DONE

**Problem.** B1/B2 taught the runner to close and settle; nothing reported it.
`closed_trades` / `get_detail()["trades"]` listed equity round-trips only (a
full option lifecycle showed an empty Trades tab), `win_rate()` counted equity
trades only, the option book's own win rate was not exposed anywhere, and
**`equity_curve` was always empty** — nothing in production code ever called
`_mark_to_market(record=True)`, so the deep-dive Equity chart had no data for
*any* runner, equity or option.

**Change.**

| Surface | Now |
|---|---|
| `closed_trades` | equity positions **+ closed option structures**, merged and sorted by exit time; equity records only gain `kind: "equity"` |
| `closed_option_trades` | one record per structure: symbol/label, `structure_type`, strikes, expiry, legs, `qty` (lots), `units` (contracts), **net premium per unit** entry/exit, pnl, commission, `win`, `exit_reason` |
| `get_detail()["trades"]` | both kinds, so the deep-dive Trades tab shows option structures |
| `win_rate()` / `wins` / `losses` | count option structures too |
| `state["options"]` | `closed_structures`, `wins`, `losses`, `win_rate`, `total_pnl`, `avg_pnl`, `costs_paid` |
| `equity_curve` | recorded **every bar** (equity *and* option runners) plus one point per option close |
| signal kinds | `OPTION_SETTLED` for settlements, `OPTION_EXIT` for decisions (stop/target/time/flip/neutral), so a reader can tell "we chose to close" from "it expired on us" |

Two details worth knowing:

- **Net premium is signed by side.** A debit spread's `entry_price` is positive,
  a credit structure's negative — so `exit_price − entry_price` reads in the
  same direction as P&L, like an equity trade.
- **The curve downsamples instead of freezing.** `MAX_EQUITY_POINTS` used to be
  a hard stop, so past 500 bars the chart silently showed only the start of the
  run. It now keeps every other point and continues (progressively coarser, full
  history).

### Bug found: the deep-dive equity chart was dead for every runner

`_mark_to_market(record: bool = False)` defaulted to *not* recording, and no
caller ever passed `True`. `get_detail()["equity_curve"]` was therefore always
`[]`, and `deep_dive.js` skips the chart entirely when the array is empty — so
the "Equity" panel rendered nothing for equity and option runners alike. Fixed
by recording on every bar.

**Acceptance criteria.**

- [x] A closed structure appears in `closed_trades` **and**
      `get_detail()["trades"]`, with its `exit_reason`.
- [x] `win_rate` / `wins` / `losses` include option structures.
- [x] Book metrics reconcile with the broker's closed structures
      (`total_pnl == Σ structure P&L`).
- [x] Equity runners get a curve too, and their trade records keep their shape.
- [x] The curve moves with option P&L, ends at live equity, and survives
      `3 × MAX_EQUITY_POINTS` of bars (`test_curve_downsamples_instead_of_freezing`).
- [x] Settlements are `OPTION_SETTLED`; stops/targets/flips are `OPTION_EXIT`.

**Result — verified over HTTP** (`deep_dive` on a running option runner):

```json
{"kind": "option", "symbol": "NIFTY bull_call_spread",
 "label": "NIFTY bull_call_spread 20450/20500 CE", "legs": 2, "qty": 1, "units": 75,
 "entry_price": 28.12, "exit_price": 0.0, "exit_reason": "stop_loss",
 "pnl": -2109.0, "win": false, "expiry": "2026-09-24"}
options: {"closed_structures": 4, "wins": 0, "losses": 4, "win_rate": 0.0,
          "total_pnl": -8051.25, "avg_pnl": -2012.81}
equity_curve: 44 points, last = current equity
signal kinds: OPTION_ENTRY, OPTION_EXIT, OPTION_BLOCKED
```

Tests: `tests/forward/test_options_trade_log.py` — 19 cases. Full gate:
**2271 passed, 4 skipped**.

## C1 — Spawn UI for option runners

**Status:** DONE

**Problem.** The engine took `instrument: {"type": "option", …}` from A1
onwards, but `portfolio.js` built its create payload from the spawn form's own
fields and **never sent `instrument` at all** — so every runner spawned from the
UI was an equity runner, and an option runner could only be created by
hand-writing JSON against the API. Exactly the review finding:

> Unreachable from the UI — `POST /api/portfolio/runner/create` accepts
> `instrument`, but `portfolio.js` never sends it.

**Change.** Three seams:

| Seam | What changed |
|---|---|
| Payload translation | new pure module `web/static/js/components/option_config.js` — `buildExpression` / `buildInstrument` / `validate` / `summarize`, loaded from `base.html` so every page with the form has it |
| The form | `_portfolio_center.html`: Instrument selector (Equity / Options), and an option panel — structure, strike selection (`atm`/`delta`) + delta target, lots per leg, stop / target, flat bars, max bars, days-to-expiry, and flip / ride-to-settlement / re-enter toggles, plus a live one-line summary of what will be deployed |
| The API | `api/portfolio.py` parses `instrument` for **every** spawn and refuses option + `TARGET_POOL` (V1 option runners price one underlying), instead of silently discarding the block |

Details that matter:

- **The form mirrors the engine's defaults, it does not invent them.** Blank
  stops/targets/flat-bars/max-bars are *omitted*, never sent as `0` (a zero stop
  would arm on the first bar); an unchecked flip-exit sends `signal_flip: false`
  while a checked one sends nothing (the engine default is flip-exit on).
- **`min_days_to_expiry` is deliberately `null`-able.** `1` is the shipped
  default, an explicit `null` means *ride into settlement* — the two states the
  exit-policy parser distinguishes. "Ride to settlement" therefore sends
  `null` even when a days value is still sitting in the (disabled) input.
- **Every structure the panel offers exists in `options_bridge.STRUCTURES`**
  (`long_call`, `long_put`, `bull_call_spread`, `bear_put_spread`,
  `direction_aware`); a test pins the two lists together so the UI cannot drift
  from the bridge.
- **A synthetic chain only exists for the index underlyings** (`NIFTY`,
  `BANKNIFTY`). Picking another symbol with `source=synthetic` is blocked with a
  reason instead of spawning a runner that can never price a strike (a live
  source is still allowed through).
- **The audit line and toast use `summarize()`** — e.g.
  `option · Bull call spread · ATM · 1 lot(s) · stop 30%, target 80%, square off 2d early`
  — so the activity log says what was deployed, not just "runner created".

**Acceptance criteria.**

- [x] The form can spawn an option runner; the resulting `RunnerConfig` carries
      the `instrument` block (structure, strike selection, quantity, exit plan).
- [x] Equity spawning is unchanged: no `instrument` posted ⇒
      `{"type": "equity"}`, and the classic payload still returns 201.
- [x] Structure, strike selection, quantity and the full exit policy survive the
      form → JSON → API trip.
- [x] Option + pool mode is refused with an actionable error.
- [x] Unknown `instrument.type` is still rejected.
- [x] The payload builder is pure and unit-tested without a browser.

**Result — verified over HTTP**, with the payload built by the *browser module*
and posted verbatim (probe: `/tmp/exp/c1_api.py`):

```text
form → node: {"type":"option","expression":{"type":"bull_call_spread",
             "strike_selection":"atm","quantity":1,
             "exit":{"neutral_bars":2,"stop_loss_pct":0.3,
                     "take_profit_pct":0.8,"min_days_to_expiry":2}}}
create → 201
exit_policy: {"min_days_to_expiry":2,"neutral_bars":2,"signal_flip":true,
              "stop_loss_pct":0.3,"take_profit_pct":0.8,"reenter":false}
deep_dive → 200, 4 closed structures, equity curve 44 pts (B3 log shape intact)
trade: {"kind":"option","label":"NIFTY bull_call_spread 20450/20500 CE",
        "exit_reason":"stop_loss","pnl":-2108.25,"expiry":"2026-09-24"}
```

Tests: `tests/js/test_option_config.mjs` (29 cases, node harness) +
`tests/test_options_spawn_ui.py` (18 cases: harness wrapper, rendered-form
assertions, create-API contract). Full gate: **2289 passed, 4 skipped**.

## C2 — Matrix + deep-dive option columns

**Status:** DONE

**Problem.** B3 made the runner *report* its option book; nothing *rendered* it.
The matrix row and the deep-dive drawer are equity-shaped — `symbol`, `qty`,
`entry_price`, `unrealized_pnl` — and options live in the bridge, never in
`runner.positions`. So a runner holding a live ₹26,667 bull call spread showed
**Positions 0** (reading as "flat"), an empty *Active Open Positions* table (the
one place the spread should have been), and a Trades tab whose rows were the
string `NIFTY bull_call_spread` with no strikes, no premium and no reason for
closing. Nothing on the page said "options" at all.

**Change.**

| Layer | What changed |
|---|---|
| `components/option_view.js` (new) | pure, DOM-free: open structures → cells, sub-lines, stat strip and table rows; exit reasons → English; trade records → labels. One vocabulary, shared by both views |
| Matrix (`portfolio.js`) | option runners get a **Structure** badge in the Type column, the structure + strikes + lots + expiry + premium under the asset, structures (with legs) in **Positions**, and a leg-by-leg *Aggregate Open Positions* tab |
| Deep dive (`deep_dive.js`) | an **Options Book** section: stat strip (open structures, legs, **premium at risk**, marked P&L, next expiry, closed structures + win rate), a structure table with a per-leg breakdown, option rows in Trades carrying **exit reason**, and an Instrument/Structure/Strike/Lots/**Exit policy** block in Config |
| `paper_runner.get_state()` | `open_positions` counts equity positions **+ open structures** (was 0 for a runner with a spread on); `equity_positions` keeps the old number, `options` carries the detail |

Why it looks this way:

- **Net premium is the entry/exit price.** A structure's `entry_price` /
  `current_price` are the signed net premium *per unit*, so `(current − entry) ×
  units` is the structure's P&L and the table reads like an equity position
  (B3's flattening, finally surfaced). P&L is also shown as a **% of premium
  paid**, which is the number an option trader actually judges.
- **Legs are shown, not hidden.** A spread's value is not one number: the table
  lists each leg with its trading symbol, side, strike and mark, and the
  aggregate tab shows the legs plus a `(net)` row per structure.
- **"Flat" is explicit.** An option runner with nothing on says
  *"flat · waiting for a view"* (or *"flat · N closed"*), so it cannot be
  mistaken for an equity runner that is merely idle.
- **Premium at risk ≠ deployed capital** in wording: it is the net premium
  actually paid for what is open — for V1's debit structures, the maximum loss.

**Acceptance criteria.**

- [x] An option runner shows its structure, strikes, lots, net premium, mark,
      P&L, next expiry and bars held — on the matrix row and in the drawer.
- [x] Positions counts the structure (1 per spread), not 0 and not the legs;
      open legs are visible as the sub-line and in the aggregate tab.
- [x] Closed structures are distinguishable from equity trades and state *why*
      they closed (stop / flip / time stop / pre-expiry square-off / settlement).
- [x] Equity runners render byte-for-byte as before (no badge, no Options Book).
- [x] A runner with no `options` payload (or a flat book) renders without
      throwing — the helpers return zeros/empty and the views say "flat".

**Result — the live payload rendered.** `tests/js/render_option_views.mjs` loads
the real `portfolio.js` and `deep_dive.js` against a dependency-free DOM and
renders what the **HTTP endpoints** return (`/api/portfolio/summary` row + the
`deep_dive` control action) for a runner holding a bull call spread bought at
₹29.63/unit with one structure already closed on the time stop:

```text
matrix row      badge-option · "Bull call spread" · "premium at risk" ·
                Positions 1 / "2 legs" · 12 lots · exp 29 Oct · premium 29.63 → 38.05
aggregate tab   NIFTY261024950CE LONG 900 545.65 625.85 ↔ NIFTY261025000CE SHORT ...
                + "NIFTY bull_call_spread (net) 12 lots 29.63 38.05 +7,578 exp 29 Oct"
drawer          Options Book: Open Structures 1 · Open Legs 2 · Premium at Risk
                ₹26,667 · Book P&L (Open) +₹7,578 · Next Expiry 29 Oct
                structure table + "↳ leg" rows + Trades row reason "Time stop"
config tab      Instrument Options · Strike selection ATM · Lots per leg 12 ·
                Exit policy "max 2 bars, square off 1d early"
```

Tests: `tests/js/test_option_view.mjs` (24 cases) +
`tests/test_options_view.py` (19 cases: harness wrapper, row/book contract, the
rendered markup, an equity-runner regression check, and the end-to-end
API → render path). Full gate: **2308 passed, 4 skipped**.

**Not in this task:** Greeks per structure and a dedicated Options Forward view.
Greeks exist in `options/greeks.py` and `options/portfolio_greeks.py` but are not
carried on the forward row; the matrix has room for the columns we added, so the
separate view stays a "consider" until the columns are crowded (D3's persistence
work is the more pressing gap).

## D1 — Index-scale synthetic spot

**Status:** TODO

**Problem.** `SyntheticFeed` seeds NIFTY at ~₹392, so an "option forward test"
builds chains at strikes ~350–450 (`MOCK-NIFTY-400-CE`) — nothing like
`NIFTY26SEP25200CE`. `SyntheticChainGenerator` already knows the right levels
(`DEFAULT_SPOTS`, `STRIKE_STEPS`, `LOT_SIZES`).

**Change.** Give the feed an index-aware scale (per-symbol seed with sane
bands for NIFTY / BANKNIFTY, e.g. 24,500–25,500 / 51,000–53,000), so strikes,
premiums and lot sizes line up with the real contracts.

**Acceptance criteria.** A synthetic NIFTY runner's chain generator reports
~24,800–25,500 and the strikes are 50-point steps; equity symbols keep their
current (small-cap) band so existing tests and demos are unaffected.

## D2 — Injectable quote provider

**Status:** TODO

**Change.** `expression["quotes"] = "synthetic" | "live"` (live requires an
authenticated broker session) resolved through the existing
`web/options_api.get_quote_provider()` chain, with the result surfaced as
`quote_source` on the runner state so nobody reads synthetic percentages as
real fills.

## D3 — Persist forward option books

**Status:** TODO

**Change.** Reuse `options/persistence.StructurePersistence` for runner books
(keys or a parent id per runner), rehydrate on `PortfolioManager` bootstrap.

## E1 — Lint baseline

**Status:** DONE

**Problem.** `tests/test_lint_baseline.py` failed on `main`: F401 `uuid`, F401 +
F811 `datetime.timedelta` in `src/backtest/options/expiry.py`. The gate had been
red since before this work started, which is exactly the "gate that quietly
stops running is not a gate" failure mode the test's own docstring warns about.

**Change.** Dropped the unused `uuid` import and the shadowing re-import of
`timedelta` inside `_timedelta_minutes` (the module already imports it at the
top). Done while editing that file for B2.

**Verified.** `.venv/bin/python -m flake8 src/` → exit 0, and
`tests/test_lint_baseline.py` passes inside the normal pytest gate.

## E2 — Options tests leak state through the dev SQLite DB

**Status:** TODO

**Problem (found 2026-09-15 while validating A1).** `tests/test_options_integration.py`
is not idempotent: `OPTIONS_PERSISTENCE` defaults to `auto`, so the options API
attaches `config/database.yaml`'s `sqlite:///forward_test.db` and rehydrates
whatever previous runs persisted. Proven sequence:

```
rm -f forward_test.db
pytest tests/test_options_integration.py   ->  13 passed   (creates forward_test.db)
pytest tests/test_options_integration.py   ->  1 failed    (assert 3 == 1: stale structs restored)
```

Unrelated to any feature work — a fresh clone is green, a second local run is
not. `tests/test_options_gap_remediation.py`, `test_options_web.py` and
`test_options_persistence.py` already guard with
`monkeypatch.setenv("OPTIONS_PERSISTENCE", "off")`; this module simply missed it.

**Change.** Add the same `autouse` fixture (or a `tests/conftest.py` default)
so the dashboard tests never touch a developer's DB. Also consider pointing
`FORWARD_TEST_DB_URL` at `tmp_path` for persistence tests that *do* want a DB.

---

## Decision log

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Per-bar MTM lives in `OptionsBridge.on_bar()`, not in `OptionPaperBroker` | The broker is a pure book (the backtest driver drives it explicitly); the bridge is the forward loop's adapter and already owns the spot/clock sync |
| 2 | Pricing clock pinned to the **bar** timestamp | Otherwise `price_contract()` measures time-to-expiry against `datetime.now()` while bars replay 2024 data → zero/negative theta |
| 3 | MTM runs *before* `_mark_to_market()` in the bar handler | The equity curve must be built from fresh option prices or A2's numbers will lag a bar |
| 4 | A1 ships without touching `equity()` | Keeps the risk-visible change (A2) reviewable on its own, and keeps equity runners bit-identical while the hook is validated |
| 5 | Entry premiums are also priced on the view's `bar_timestamp` | Otherwise entry uses the wall clock and the next bar re-prices on the replay clock — a one-bar jump that looks like instant P&L. Falling back to wall clock when the view has no timestamp keeps the old behaviour for programmatic views |
| 6 | MTM is skipped entirely when no structure is open | Nothing to price, and the spot is already synced from the view at entry — avoids per-bar Black-Scholes work on flat books |
| 7 | Fold option P&L at the **runner**, not in `PortfolioManager._bucket_*` | Every bucket/portfolio aggregate is derived from `runner.equity()` / `deployed_capital()` / `daily_pnl()` already, so one change makes the runner card, buckets, portfolio totals and breakers consistent — and nothing can be missed twice |
| 8 | `realized_pnl` stays **gross of costs**; the fee stack lives in `equity()`/`option_pnl()` | Matches `OptionPaperBroker.total_equity` and the `/options` dashboard. Netting fees into `realized_pnl` would show a negative realized P&L the moment a structure opened |
| 9 | `premium_at_risk` (net debit), not short-leg margin, is the deployed-capital analogue | For all four V1 structures the net debit **is** the maximum loss, so it is exact; the broker's margin model (sell-side notional) would overstate a defined-risk spread |
| 10 | Exit rules split by *who needs the view*: risk rules in `on_bar`, signal rules in `on_market_view` | `on_bar` runs for every runner (including pool mode, which never routes views to options), so a stop still protects a position nobody is watching; signal rules stay where the view is |
| 11 | Exit evaluation is a pure `ExitPolicy`; the bridge owns all counters | Rules become unit-testable without a broker or a clock, and the bar-index/neutral bookkeeping lives in exactly one place |
| 12 | Default `min_days_to_expiry = 1`; stops/targets opt-in | Matches the README's "auto-squared-off before expiry" claim and avoids settlement risk (B2 takes over real settlement). A wrong *default* stop would silently cap every trade |
| 13 | Expiry selection now takes its reference from the **bar clock** | Same principle as A1's pricing clock: a replay of October bars must trade October's expiry. Note this partially pre-empts D1 — the spot-scale half is still open |
| 14 | Settlement settles **on** the expiry bar (`include_today=True`), opt-in | Index options are cash-settled on the expiry-day close — exactly the price the bar carries. Opt-in keeps the strict "after expiry" behaviour for existing callers (the dashboard rides the wall clock) |
| 15 | Settlement reuses `ExpiryManager` rather than a forward-only implementation | One settlement code path for backtest, dashboard and forward: same cash movements, same `expiry_settlement` stamp, same broker observers (which persistence depends on) |
| 16 | Exit-config keys: omitted keeps the default, explicit `null` disables | Otherwise the default policy changes meaning depending on which *other* keys a user set — the bug in the section above |
| 17 | Option records are flattened into the *existing* trade-record shape (+ a `kind` tag) | The deep-dive table, win-rate maths and any JSON consumer keep working with no branching; richer fields (`strikes`, `expiry`, `exit_reason`, `label`) are additive for C2 |
| 18 | `pnl` stays gross of costs; `commission` is per structure, statutory fees remain per book | Consistent with the equity trade log. Statutory fees are booked per fill, not per structure, so attributing them would mean new bookkeeping in the broker — out of scope for B3 |
| 19 | `equity_curve` records every bar rather than only on closes | A curve of closes alone is a step function that hides the drawdown path; per-bar marks are what the chart is for. Downsampling keeps a long run readable |
| 20 | The spawn payload builder lives in a standalone JS component, not inline in `portfolio.js` | It is pure (form object in, `instrument` object out) so it can be unit-tested in node without a DOM, and the browser gets the same file — no build step, no duplicate logic |
| 21 | Instrument-aware spawning is validated in the browser **and** re-checked by the API | The browser gives a fast, specific error; the API guard is what actually protects the engine from a hand-written (or stale-cached) payload |
| 22 | Blank numeric exits are omitted rather than defaulted to `0` | `0` is meaningful in this engine (`stop_loss_pct: 0` arms on the first bar, `min_days_to_expiry: 0` squares off on expiry day), so "left empty" must stay distinguishable from "deliberately zero" |
| 23 | Option rows reuse the equity-shaped keys (`entry_price`, `current_price`, `unrealized_pnl`) with the net premium in them, plus option-only keys | Every existing renderer (matrix cells, drawer tables, any JSON consumer) keeps working, and the extra keys are additive — the alternative was a second parallel shape in three views |
| 24 | `open_positions` on a runner row counts structures; `equity_positions` and `options.open_positions` (legs) keep the other two numbers | The matrix's Positions column asked "is this runner exposed?", and 0 for a live spread answered it wrongly. The bridge's own `open_positions` contract (legs) is untouched, and B1's "one structure at a time" keeps the count honest |
| 25 | The book renderer is a shared pure component (`option_view.js`), not helpers in each view | The matrix needs cells and sub-lines, the drawer needs tables and stats; one tested source of labels/numbers keeps them from disagreeing — and node can test it with no DOM |
| 26 | Greeks and a dedicated Options Forward view are deferred out of C2 | The forward row does not carry Greeks yet (they live in the options layer), and the matrix had no crowding emergency; inventing a view before the columns exist would be the wrong order |

## How to run the gates for this work

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/forward -q          # forward + options-forward
PYTHONPATH=src .venv/bin/python -m pytest tests/ -q                 # full gate
.venv/bin/python -m flake8 src/                                     # lint baseline (E1)
```
