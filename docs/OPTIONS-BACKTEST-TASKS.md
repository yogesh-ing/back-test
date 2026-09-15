# Task Tracker: Options Backtesting Engine (Phase A)

**PRD:** [`docs/OPTIONS-BACKTEST-PRD.md`](OPTIONS-BACKTEST-PRD.md)
**Last updated:** 2026-09-15 (A4 implemented; A6 ID/alert determinism pulled forward)

**Status key:** ✅ done · 🔨 in progress · ⬜ not started · ⛔ blocked · ❓ needs a decision

---

## Summary

| Phase | Tasks | Done | Remaining |
|---|---|---|---|
| Phase 0 — prerequisites | 3 | 2 | 1 (needs decision) |
| Phase A — core build | 7 | 4 | 3 |

**Next up:** `A1` (synthetic scenarios — ❓ needs the scenario-shape
decision, PRD §7.1) or `A5` (metrics — its design constraint is already
recorded). `A6` is **partially done**: the three runtime non-determinism
sources (both `uuid4` ID sites, `alert_id`) landed with A4 because the
loop's determinism test exposed them; the `date.today()` fallbacks in
`quote_providers.py` remain.

---

### ✅ A4 — The driver loop (2026-09-15)

**Added:**
- `src/backtest/engine/option_backtest_driver.py` — `OptionBacktestDriver`,
  `BacktestConfig`, `BacktestResult`, pure `next_monthly_expiry` (last-
  Thursday convention, no `date.today()`), `_max_drawdown`, and
  `_GeneratorSettlementProvider` (settlement spot straight off the
  generator).
- `tests/engine/test_options_backtest_loop.py` (24 tests).

**Two correctness-critical details the loop bakes in:**

1. **Clock seam on the quote provider (A6 pulled forward).**
   `price_contract` falls back to `datetime.now()`; over historical bars
   that yields negative time-to-expiry and every premium collapses to
   intrinsic value. `SyntheticQuoteProvider.set_reference()` now pins
   quote pricing to the bar time; the driver sets it every bar. This is
   the single change without which the loop would produce silently wrong
   numbers.
2. **Bars stamped at 15:30 IST.** Daily bars get the market-close time so
   the expiry manager's minute-based square-off window is meaningful with
   daily data.

**Design choices:**

- **Loop order:** spot → view (on the expanding window `loc[:bar]`) →
  chain *after* the view (bullish → CE chain, bearish → PE chain; V1
  chains are single-sided) → `register_chain` → seam → execute → MTM →
  expiry → snapshot. Entries are skipped (logged, not raised) on
  `InsufficientMarginError`; `UnsupportedStructureError` **propagates** —
  that's a configuration bug.
- **Position cap:** `max_open_structures` implements the PRD's
  `risk.max_positions`; over-cap entries are logged as skipped.
- **`_on_position_opened` hook** exists for per-bar entry bookkeeping
  (A5/CLI output), deliberately empty for now.
- **Injectable collaborators** — generator, broker, strategy, expiry
  manager, settlement provider — each with a production default.

**A6 debt retired with A4** (the loop's determinism test exposed all
three): `structure_id = struct_{ts}_{seq}` and
`position_id = pos_{ts}_{seq}` (per-broker monotonic counters — `hash()`
was never an option: `TradeIntent` is frozen with a dict field), and
`alert_id` is now a sha256 digest of alert type + timestamp + targets.
Leg `position_id`s are explicit at the broker's construction site; the
dataclass uuid4 default remains for ad-hoc construction outside the
broker.

**Evidence (trending frame, 45 bars):** 43 structures, 42 closed via
`auto_square_off`/settlement, win rate 76%, equity 1,000,000 →
1,334,941.75; determinism test asserts identical `to_dict()` across runs.

**Regression after the ID change:** 555 passed, 1 skipped — the only
failure is the pre-existing Windows process-pool test (`worker_pids.txt`),
which fails in isolation and touches none of this work.

---

## Phase 0 — Prerequisites

### ✅ T0.1 — Retire `net_debit`, populate `estimated_premium`

**Changed:**
- `src/backtest/strategy/intent.py` — deleted the `net_debit` property (it
  summed `lot_size`, not premium) and added an `is_debit` property.
- `src/backtest/options/structures.py` — added
  `OptionStructure._estimate_net_premium(...)` and wired it into all four
  builders, so `TradeIntent.estimated_premium` is now a real
  Black-Scholes estimate instead of a hard `0`.

**Notes:** `net_debit`, `estimated_premium` and `estimated_margin` had
**zero readers** anywhere in `src/`, `tests/`, `docs/`, `config/` — the
field was dead, which is why the placeholder went unnoticed.

The estimate returns `Decimal("0")` ("not estimated") rather than guessing,
when: no spot on the view, no `bar_timestamp` (this deliberately does **not**
fall back to `date.today()` — see PRD §9), a non-positive time to expiry, or
a contract with no modelled vol (i.e. a real vendor chain).

**Tests:** `tests/test_options_backtest_phase0.py::TestEstimatedPremium` (9)

---

### ✅ T0.2 — `exit_reason` on structures

**Changed:**
- `src/backtest/options/paper_trading.py` — `StructurePosition.exit_reason`
  field; `close_structure(..., reason="manual")` now records it.
- `src/backtest/options/expiry.py` — `auto_square_off` passes
  `reason="auto_square_off"`; `settle_expired` stamps
  `"expiry_settlement"` on the parent structure when the last leg expires.

**Notes:** Settlement bypasses `close_structure` entirely (it sets
`PositionStatus.EXPIRED` directly), so the `settle_expired` stamp is the only
place a settled structure can be labelled. `expiry_settlement` is the
structure's *reason*; the position's *status* is `EXPIRED` — trade logs must
read both.

**Tests:** `tests/test_options_backtest_phase0.py::TestExitReason` (5)

---

### ❓ T0.3 — Sanitise the demo data

**File:** `data/live/nifty_index_2024_h1.csv` — 124 daily rows, `close ≈ 19.56`.
Not NIFTY (should be ~22,000–25,000); a real ingestion would silently
produce nonsense.

**Proposed:** rename to `data/live/DEMO_DATA_INVALID_nifty_index_2024_h1.csv`
(or delete).

**Status:** ⛔ **Not actioned — awaiting owner decision.** The owner's
standing instruction is to mark rather than delete files so they can
decide. No code path currently ingests this file, so it is not urgent.

---

## Phase A — Core build

### ⬜ A1 — Synthetic scenarios

**Effort:** 1d · **Depends on:** §7.1 decision

Three canonical scenarios: trending (+10% / 60d), range-bound (±2% / 60d),
volatile drop (−15% at day 10). Seeded for determinism.

**Blocker:** `SyntheticSource` is an *instance* `DataSource`
(`get_candles(symbol, start, end, interval)`), so `@staticmethod` scenario
generators do not fit its shape. Pick one:
module-level functions, a scenario parameter on `SyntheticSource`, or a
separate `scenarios.py`.

---

### ✅ A2 — Backtest broker wiring (closed-structure export)

**Effort:** 1d · **Depends on:** T0.1, T0.2

**Changed:**
- `src/backtest/options/paper_trading.py` — added
  `get_closed_structures()`, the mirror of `get_open_structures()`. Without
  it closed structures were **unreachable**, so a trade log had nothing to
  read.
- `src/backtest/engine/option_backtest_driver.py` — added
  `StructureTradeRecord` / `structure_to_record()` / `build_trade_log()`
  and `EquityPoint` / `capture_equity()`.

**Notes:**

- **No isolation wrapper was needed.** `OptionPaperBroker`'s cash and
  position dicts are instance attributes, so a second instance is already
  fully isolated. The task was really the export and the missing query.
- **A settled structure counts as closed.** `get_closed_structures()`
  keys off `is_open`, so expiry settlement (which sets legs to `EXPIRED`)
  surfaces in the log with `exit_reason == "expiry_settlement"`.
- **Money serialises as a string**, not a float — the options layer is
  Decimal-exact and round-tripping ₹ through a float is how rounding bugs
  are introduced. Numeric consumers read the dataclass fields; the dict is
  for the JSON report.
- **Log ordering involves no identifier.** It relies on `list.sort` being
  stable plus dict insertion order, so it is already deterministic before
  A6 lands — otherwise A6 would have to fix the ordering too.
- Deterministic IDs were deliberately **left to A6** rather than done here.

**Design constraint discovered (feeds A5):** the equity engine's
`walk_trades` reconstructs a trade as a run of consecutive bars holding the
same position sign. That cannot describe an options book — several
structures open at once, each with legs, expiry and exit reason — so the
per-trade half of `compute_metrics` is **not** reusable for options. See A5.

**Tests:** `tests/engine/test_options_trade_log.py` (22) — the closed query
(including the settled and partition cases), record fields, JSON
serialisability, money-as-string, log ordering/determinism, and equity
capture responding to a spot move.

---

### ✅ A3 — Expression seam (critical path)

**Changed:**
- `src/backtest/engine/option_backtest_driver.py` (new) —
  `build_intent_from_view()`: `MarketView` →
  `create_selector().pick_strikes()` → `create_structure().build()` →
  `TradeIntent`, plus `default_structure_decider()`,
  `PHASE_A_STRUCTURES`, and `UnsupportedStructureError`.

**Notes:** This is the first production caller of the expression layer —
until now `create_selector` / `create_structure` were reached only by
tests and the `/options` web API.

Design choices worth recording:

- **The structure decision is injected, not hardcoded.** The engine takes a
  `decider: Callable[[MarketView], str | None]`; the built-in default is
  documented as *policy, not mechanism*. This keeps the engine free of
  strategy opinion.
- **Direction is compared against the `Direction` enum**, never a string.
  A string comparison is always `False` and produces a silently empty
  backtest — there is an explicit regression test for this.
- **Out-of-scope structures raise** `UnsupportedStructureError` (a
  `ValueError`) naming the chain-shape reason, rather than returning
  `None`. The structure choice is configuration, not a market condition;
  silently skipping would hide the bug.
- **`count` comes from `structure.max_legs`**, so the seam cannot drift
  from the builder's own leg requirements.
- No clock, no randomness: `expiry` is supplied by the caller and there is
  no `date.today()` fallback.

**Deviation:** implemented before A1/A2 (its nominal dependencies). The
seam is testable in isolation against a hand-built chain, so A1's
scenario-shape decision no longer blocks the critical path.

**Tests:** `tests/engine/test_options_expression_seam.py` (25) — no-trade
paths, view→structure mapping for all four structures, scope enforcement
(parameterised over the four Phase-B names), real-contract leg checks, and
determinism.

**End-to-end evidence** (view → intent → broker → close):

```
intent: bull_call_spread  est_premium: 2065.93
legs opened: 2  | cash: 997859.25
equity after open: 999960.00  (capital 1,000,000 — down only the ₹40 commission)
close pnl: 481.50 | exit_reason: strategy_signal | is_open: False
```

---

### ✅ A4 — The driver loop

**Effort:** 1d · **Depends on:** A3

Per bar: `set_spot` → `generate_chain` → **`quote_provider.register_chain`**
(skipping this prices every structure at ₹0) → view → intent → execute →
`update_mtm` → `process_expiries`.

`ExpiryManager(broker)` takes the broker at construction;
`process_expiries(quote_provider, settlement_provider, as_of)` needs a
**settlement provider** too.

See the A4 completion section at the top of this file for what shipped.

---

### ⬜ A5 — Metrics

**Effort:** 0.5d · **Depends on:** A4

**Split the reuse — this is not the straight reuse v3 assumed:**

- ✅ **Portfolio-level metrics are reusable in spirit.** `compute_metrics`
  derives total return, CAGR, volatility, Sharpe, max drawdown and Calmar
  from an `equity` Series plus `return`s. An options backtest can produce
  the same series from `capture_equity()` snapshots.
- ❌ **Per-trade metrics are not.** `compute_metrics` gets its trade stats
  from `walk_trades(equity, position)`, which defines a trade as a run of
  consecutive bars holding the same sign. That model cannot represent
  simultaneous multi-leg structures and would collapse an entire book into
  one "trade" while losing `structure_type` and `exit_reason`.

So options trade statistics come from `build_trade_log()` (A2) instead:
count, win rate over **closed** structures, average/best/worst P&L, and fee
share. Mirror the equity convention of excluding open structures from
win/loss counts (`StructureTradeRecord.is_win` already does this).

There is **no `MetricsCalculator` class** — only `compute_metrics(result)`.

**Open question:** whether to emit an options-shaped result object that
`compute_metrics` can consume directly (equity + returns Series, plus a
minimal `config` with `initial_capital` / `periods_per_year`), or to keep
the two metric paths separate. Decide at A5.

---

### 🔨 A6 — Determinism (partially done)

**Effort:** 1d · **Depends on:** A4 · **Critical**

Four named sources (PRD §9). **Three landed with A4** (2026-09-15): both
`uuid4()` ID sites → monotonic counters, `alert_id` → sha256 content
digest. Remaining:

- [ ] `date.today()` fallbacks in `quote_providers.py`
  (`next_monthly_expiry` ref, `available_expiries`, `price_contract`) —
  the driver already bypasses all three by passing explicit
  `expiry`/`reference` values, so this is now *audit-and-default*
  hardening rather than a correctness gap.
- [ ] The unconditional `last_updated = datetime.utcnow()` default on
  `OptionPosition` (clock injection cannot reach it). Note the broker now
  always passes `opened_at`/`closed_at` explicitly in the historical path;
  verify whether any non-historical caller relies on the default before
  changing it.
- [ ] Determinism test over a **full multi-expiry run** (the A4 test pins
  a single-frame run).

**Do not** use `hash(intent)` for IDs — `TradeIntent` is a frozen dataclass
with a `dict` field, so `hash()` raises `TypeError`. Use a monotonic counter.

---

### ⬜ A7 — Integration test + docs

**Effort:** 0.5d · **Depends on:** A1–A6

E2E on all three scenarios; 3 hand-calc anchors within ₹1; runbook at
`docs/options_backtesting.md`.

---

## Phase B — Deferred

| ID | Task | Blocker |
|---|---|---|
| B0 | Chain-shape refactor → two-sided, multi-expiry | **Precedes B1–B4** |
| B1 | IV smile / skew (`generator.price_contract` seam) | — |
| B2 | Greek P&L decomposition | — |
| B3 | Margin simulation (`PreTradeRiskCheck`) | — |
| B4 | Bid/ask fills | data exists; broker change only |
| B5 | Real underlying **and** real option chain data | vendor/bhavcopy |

---

## Decisions log

| # | Decision | Rationale |
|---|---|---|
| D1 | New dedicated options loop (not `run_engine_loop`) | The shared loop is keyword-only equity-typed and signal-shaped; options need a chain per bar and multi-leg atomic fills |
| D2 | Scope Phase A to **4** structures | The other 4 need a chain-shape change, not just builders |
| D3 | Per-leg bps slippage, 10 bps | Already implemented in the broker |
| D4 | Delete `net_debit`; use `estimated_premium` | The field already existed and was dead |
| D5 | Synthetic scenarios for Phase A | Premiums are Black-Scholes regardless, so real bars change the scenario, not the fidelity |
| D6 | Phase B needs real underlying **and** real option data together | Either alone is still a model result |

---

## Verification

```bash
cd src && python -m pytest ../tests/test_options_backtest_phase0.py -q
```

Full options regression:

```bash
cd src && python -m pytest ../tests/test_options_*.py -q
```

**Last run:**

- `tests/engine/test_options_backtest_loop.py` (A4) → **24 passed**
- `tests/test_options_backtest_phase0.py` → 14 passed
- `tests/engine/test_options_expression_seam.py` → 25 passed
- `tests/engine/test_options_trade_log.py` → 22 passed
- options + strategy + engine + backtest slice → **555 passed, 1 skipped**
  (the pre-existing Windows process-pool failure)

**One pre-existing failure**, unrelated to this work and failing in
isolation: `test_api_backtest_parallel.py::test_multiple_backtests_run_in_process_pool`
(Windows process-pool temp file `worker_pids.txt`). The file contains zero
references to options, intent, structures or selectors.
