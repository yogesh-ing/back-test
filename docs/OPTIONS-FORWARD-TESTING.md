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
| **C1** | Spawn UI: instrument / structure / strike / quantity controls | C · Reach | TODO | A2 |
| **C2** | Portfolio matrix + deep-dive: option columns, premium, Greeks | C · Reach | TODO | C1 |
| **D1** | Index-scale synthetic spot for NIFTY / BANKNIFTY | D · Realism | TODO | A1 |
| **D2** | Injectable quote provider (`synthetic` \| `live:mstock`) + badge | D · Realism | TODO | A1 |
| **D3** | Persist forward option books across restarts | D · Realism | TODO | A2 |
| **E1** | Fix lint baseline (`options/expiry.py` unused imports) | E · Hygiene | DONE | — |
| **E2** | Stop options tests leaking state through the dev SQLite DB | E · Hygiene | TODO | — |

Phases: **A** makes the numbers move, **B** makes the runner able to leave a
trade, **C** makes it reachable from the browser, **D** makes the numbers
honest, **E** keeps the gate green.

**Progress: A1, A2, B1, B2, B3 and E1 are done** — an option runner prices
every bar, is counted by the equity/bucket/breaker maths, opens *and closes*
on its own rules, settles at expiry and rolls, and reports what it did in the
trade log, the metrics and the equity curve. What remains is reachability and
realism: **C** (no UI can spawn an option runner yet — `portfolio.js` never
sends `instrument`) and **D** (index-scale synthetic spot for a live NIFTY
forward test, an injectable live quote provider, and persistence across
restarts).

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

**Status:** TODO

**Change.** `portfolio.js` + `_portfolio_center.html`: instrument type,
structure (fixed vs direction-aware), strike selection (`atm`/`delta`),
quantity, and exit policy; POST them inside `instrument`. Show the resulting
book on the spawned card.

## C2 — Matrix + deep-dive option columns

**Status:** TODO

**Change.** Structures, net premium, option P&L and next expiry on the matrix
row and the deep-dive panel. Consider a dedicated Options Forward view if the
matrix gets crowded.

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

## How to run the gates for this work

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/forward -q          # forward + options-forward
PYTHONPATH=src .venv/bin/python -m pytest tests/ -q                 # full gate
.venv/bin/python -m flake8 src/                                     # lint baseline (E1)
```
