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
| **A2** | Fold option P&L into runner equity, buckets and breakers | A · Live | TODO | A1 |
| **B1** | Exit policy: view flip / neutral, stop, target, time stop | B · Exits | TODO | A1 |
| **B2** | Expiry square-off + roll inside the forward loop | B · Exits | TODO | B1 |
| **B3** | Close plumbing: `OPTION_EXIT` signals, closed-structure log, metrics | B · Exits | TODO | B1, B2 |
| **C1** | Spawn UI: instrument / structure / strike / quantity controls | C · Reach | TODO | A2 |
| **C2** | Portfolio matrix + deep-dive: option columns, premium, Greeks | C · Reach | TODO | C1 |
| **D1** | Index-scale synthetic spot for NIFTY / BANKNIFTY | D · Realism | TODO | A1 |
| **D2** | Injectable quote provider (`synthetic` \| `live:mstock`) + badge | D · Realism | TODO | A1 |
| **D3** | Persist forward option books across restarts | D · Realism | TODO | A2 |
| **E1** | Fix lint baseline (`options/expiry.py` unused imports) | E · Hygiene | TODO | — |
| **E2** | Stop options tests leaking state through the dev SQLite DB | E · Hygiene | TODO | — |

Phases: **A** makes the numbers move, **B** makes the runner able to leave a
trade, **C** makes it reachable from the browser, **D** makes the numbers
honest, **E** keeps the gate green.

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

**Status:** TODO

**Problem.** `StrategyRunner.equity()` / `unrealized_pnl()` / `daily_pnl()` /
`deployed_capital()` read the equity `Portfolio` only, so an option runner's
card, bucket aggregate and circuit breakers are blind to its option book —
this is Gap **P2** in the remediation PRD, still open.

**Change.** Make the option book a first-class contributor: `equity() +=
bridge.equity − bridge.capital` (i.e. option realized + unrealized − costs),
`deployed_capital() += premium at risk`, and let `_check_instance_risk()`
compare against the combined equity. Keep `get_state()["options"]` as the
drill-down; update `_bucket_*` only if a runner cannot be fixed at the source.

**Acceptance criteria.**

- [ ] A runner whose option book loses 10% trips the instance drawdown breaker.
- [ ] `state["equity"]` equals `state["options"]["equity"]` for a pure option
      runner; bucket `equity` / `deployed_capital` include option P&L.
- [ ] Equity runners' numbers are bit-identical to pre-change values.

## B1 — Exit policy

**Status:** TODO

**Problem.** V1 ignores new views while a structure is open — it can never
close. Exits today require a human clicking the dashboard (and the dashboard
holds a *different* book, so it cannot even see a runner's positions).

**Change.** `expression["exit"]` policy evaluated per bar: view flip, view
neutral for N bars, stop-loss / take-profit on structure P&L (percent or
points), and a time stop (N bars / days before expiry). Close via
`broker.close_structure()` with `exit_reason` set.

**Acceptance criteria.** Each rule has a unit test; a flip from bullish to
bearish closes the bull call spread on that bar and (if re-entry is enabled)
opens the bearish structure; `exit_reason` is populated and persisted.

## B2 — Expiry square-off + roll

**Status:** TODO

**Change.** Drive `ExpiryManager.process_expiries()` from the runner's bar
clock (with a synthetic settlement price off the generator spot), so a
structure reaching expiry is settled inside the forward loop; optional roll to
the next expiry via `NearestExpiryPolicy`.

**Acceptance criteria.** A structure held past expiry settles exactly once,
realized P&L lands in the book, `status="expired"`, and a roll produces a new
structure id on the next expiry.

## B3 — Close plumbing

**Status:** TODO

**Change.** `OPTION_EXIT` / `OPTION_EXPIRY` signal kinds, closed-structure
records in `get_detail()["trades"]`, option win-rate / total P&L in the
summary, and one equity-curve point per structure close.

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

**Status:** TODO

**Problem.** `tests/test_lint_baseline.py` fails on `main`: F401 `uuid`, F401 +
F811 `datetime.timedelta` in `src/backtest/options/expiry.py`.

**Change.** Delete the unused imports / the shadowing re-import. Three lines.

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

## How to run the gates for this work

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/forward -q          # forward + options-forward
PYTHONPATH=src .venv/bin/python -m pytest tests/ -q                 # full gate
.venv/bin/python -m flake8 src/                                     # lint baseline (E1)
```
