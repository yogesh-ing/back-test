# PRD & Task Decomposition — Market Data Simulator (U2)

**Status:** proposal under revision — Q1 and Q2 are **decided** (below); Q3–Q5 default to
the recommendation unless you object. No code written yet; the shape is still open to change.
**Author:** agent verification pass, 2026-08-28–29. **Tracker:** `instructions/TASK-TRACKER.md` → gap `U2`, closes `G6`.
**Sizing:** ~6 phases, 21 tasks, ≈ 5–7 focused days. Phase 1 alone ships value.

> **How to review this:** read §3's five decisions (that is where the disagreement should
> happen), then §8's open questions. Everything else is mechanics that follows from them.
> Phases 0–1 are worth approving on their own: they close `G6` with no scenario machinery.

---

## 1. Why this exists

The synthetic source is a placeholder, and it silently breaks three product promises:

| Evidence (verified live, 2026-08-28) | Consequence |
|---|---|
| `SyntheticSource.get_candles()` ignores `interval` entirely — always `pd.date_range(freq="B")` | The Timeframe selector on Backtest/Compare does nothing. Slot `1D` and slot `1H` returned **identical 262 bars / −2.40%** → PRD §4.3 "compare across timeframes" is untestable |
| Prices are a seed-derived random walk | Nothing about the walk relates to the strategies. `sma_crossover` with default params over 2024 produced **3 trades, 2 closed, both losers**; `buy_and_hold` produces exactly one open trade. To exercise a fill, a stop, a take-profit or a forward replay you must get lucky |
| `CsvSource` needs a file per symbol; `DbSource` needs credentials + a populated DB | There is **no way to develop or demo intraday behaviour with the market closed** and no CI path that is both fast and deterministic |
| `SyntheticSource` raises `"synthetic range must be > 50 rows for validation"` | Short intraday windows — the interesting ones for 1-min bars — are rejected outright |

The goal the user asked for, verbatim: *bars that trigger the selected strategy's entry, then move enough within the next 1–2 minutes to close the trade — profit or loss doesn't matter — so the system can be tested even when the market is not open.*

That is a different thing from "nicer random data". It is **a test fixture that happens to look like a market**: for any strategy and any params, we can demand "you will get ≥1 entry and ≥1 exit, inside this window, at this timeframe", and rely on it.

---

## 2. Goals / non-goals

**Goals**
1. `--source simulator` honours **every** interval the UI offers (`1min, 5m, 15m, 1H, 4H, 1D, 1W`) → closes `G6`.
2. A **scenario** guarantees a trade: entry, then a chosen exit kind (risk target / risk stop / indicator exit / time exit) within N bars.
3. Deterministic and reproducible: same (scenario, symbol, timeframe, window, params) → byte-identical bars; a printed `manifest` pins any run.
4. Credential-free and fast enough for CI and for the live preview demo.
5. Realistic enough to exercise the machinery we already built: intrabar stop/target logic, `marketdata/quality.py` anomaly checks, NSE session timestamps, forward replay reveal order, portfolio pool scanning.
6. Replaceable: the same interface a real feed will use, so nothing needs to change when mStock is wired.

**Non-goals (explicitly deferred)**
- Tick-level order books, queue position, partial fills, latency simulation — the `simulator/` package models these already (`execution.py`, `slippage.py`); driving them from bar generation is **Phase 6, optional**, not core.
- ML/statistical price realism. We want *coverage of code paths*, not believability.
- Replacing `SyntheticSource` (keep it: 1571 existing tests depend on its exact shapes).
- Broker/live trading paths — auth-gated and credential-gated as today.

---

## 2b. Prototype evidence (measured on this box, 2026-08-29)

Not a mock-up — these numbers came from throwing 20 lines of numpy at the existing
engine, so the budgets and the core mechanism below are measurements, not hopes.

**Resampling makes timeframes real.** One generated 1-min walk for calendar 2024,
resampled, run through `sma_crossover(fast=20, slow=50)`:

| Interval | Bars | Trades | Win rate | Return | Cost of the run |
|---|---|---|---|---|---|
| `1D` | 262 | 1 (open) | — | **+355.6%** | 4.4 ms |
| `1H` | 1,834 | 11 (10 closed) | 90% | +331.8% | 8.7 ms |
| `15M` | 6,550 | 57 | 61% | +230.3% | 24.3 ms |
| `1min` | 98,250 | 1,078 | 30% | **−48.0%** | 339 ms |

The same market, four verdicts — including the honest one at the bottom: at minute
granularity a 20/50 crossover over-trades and commission+slippage turn a winner into
a −48% year. No amount of `SyntheticSource` tinkering can show that, because it has
no sub-daily data to lose money on. (The +355% is a 100%-of-capital hold on a
drifting walk — it illustrates *divergence*, not a strategy edge.)
Generation of the 98k-bar frame took **14.9 ms**; each resample **~7 ms**.

**The "must trade" mechanism, and the trap in the obvious version.** Forcing an exit
by "put a big down-move a few bars after the expected entry" **fails**: with the naive
version (spike 2 bars after a guessed entry bar) the stop never tripped at either
resolution — the strategy entered earlier than guessed, at a lower price, so the stop
level was nowhere near the spike. Computing it in two passes works:

1. run the strategy once → first non-zero held bar `e`, engine entry price
   `close[e-1]`, `stop = entry × (1 − stop_loss)`;
2. bend bar `e + gap` so `low = stop × 0.985`.

| Resolution | Entry | Forced exit | Outcome |
|---|---|---|---|
| `1min` | bar 88, 10:43 @ 91.69 | bar 90, low 87.11 → 85.80 | closed **Loss** −6,574.46 in ~2 min |
| `1D` | bar 38, 2024-02-22 @ 93.68 | bar 39 (next session), low 89.00 → 87.67 | closed **Loss** −6,574.37 |

Same trade, same P&L to within 9 cents, at minute and daily resolution — which is the
D1 claim (a minute-level risk event survives resampling into the coarse bar that
contains it) demonstrated rather than asserted. Note both runs also produced a
*second*, still-open trade: forcing an exit changes what the strategy sees
afterwards, which is why `expect` must be ranges (§D2).

---

## 3. Design decisions

Each has the recommendation first, then what was rejected and why. These are the five decisions most worth arguing about in review.

### D1 — Generate at one atomic resolution (1 min), resample up
`DbSource` already proves the pattern: find the finest stored timeframe, then `_resample()` to the request (`data/db_source.py`). The simulator mirrors it — generate 1-min bars as the single truth, then aggregate to `5m/15m/1H/4H/1D/1W` with `open=first, high=max, low=min, close=last, volume=sum`.

Why this matters beyond tidiness: a daily bar **cannot** express "the stop was hit 90 seconds after entry", yet the engine's risk path reads exactly that (`_run_with_risk` compares `row["high"]/row["low"]` against `entry*(1±pct)`). Resampling a 1-min path into a daily bar *automatically* produces the intrabar high/low that fires the stop — so the scenario author writes intent at minute resolution and the daily run still behaves correctly.

Reuse: `marketdata/bars.py` (`Timeframe`, `align_to_boundary`) for boundaries; `DbSource._INTERVAL_TO_RULE`-style mapping extracted to one shared table instead of a third copy (it is currently duplicated in `api/backtest.py` and `api/forward.py` — see gap `G6`'s "one shared interval map").

- *Rejected:* emitting native bars per interval (each timeframe generated separately) — cheap, but then a "1H entry, stopped out at 14:07" scenario is not expressible, and cross-timeframe Compare comparisons become meaningless rather than merely coarse.
- *Rejected:* a 15-second base — 4× the data for no code-path coverage the minute bar doesn't already give; make the atomic step a config value (`base_seconds: 60`) so it can change without redesign.

### D2 — "Must trade" is *constructed*, then *verified*, never hoped for
Rejection sampling alone (random walk, retry until the strategy trades) fails for wide params and burns CPU; pure construction (bend the path by hand per strategy) drifts when a strategy changes. Do both, in that order:

> The naive version of step 1 — guess the entry bar, spike a few bars later — was
> tried first and **did not fire** (§2b). Construction must be *two-pass*: locate the
> entry the strategy actually took, then bend relative to that bar and that price.

1. **Construct (pass 1 — shape).** Each scenario carries a *shape recipe*: e.g. for
   `trend_cross`: `ramp_up(k bars, slope)` until `fast_sma > slow_sma`, hold `h` bars,
   `ramp_down(m)` for the indicator exit. One builder per **family**
   (`trend_cross`, `mean_reversion`, `channel_breakout`, `always_in`, `none`), not per
   strategy, so a user strategy in a known family needs zero new code.
2. **Force (pass 2 — relative to reality).** For `exit_reason: stop|target`, re-derive
   from the pass-1 run: `e = first held bar`, `entry = close[e-1]` (exactly what
   `_run_with_risk` books), `level = entry × (1 ∓ stop_loss|take_profit)`, then set the
   bar at `e + after_entry` to cross `level` on its low/high. Never a hard-coded
   percentage of an assumed price.
3. **Verify on the final frame.** `generate_signals` + the trade walk (engine/trades.py)
   on the *bent* data — `expect` is checked against what the run really produced, not
   against pre-forcing state. Satisfiable-ness is re-checked after forcing because
   forcing can move the entry.
4. **Re-roll, then fall back.** On failure: up to `max_attempts` (12) seeded noise
   re-rolls (each re-running passes 1–3), then zero-noise construction. Measured cost
   of a full re-roll ≈ 15 ms per generation + ~4–25 ms per verify run, so the 12-attempt
   worst case is ~200 ms — cheap enough to keep this honest rather than clever.
5. **Report, don't lie.** Still unsatisfied → `on_unsatisfied: warn` (default; WARNING
   names the params that defeated it, bars still returned) / `error`
   (`ScenarioUnsatisfied`) / `fallback_flat`. A scenario never claims to have fired.

**Why `expect.*` is ranges, not exact counts:** forcing an exit rewrites what the
strategy sees downstream, so a second entry after a forced stop is normal (both §2b
runs produced one). `expect: {entries: [1,3], exits: [1,3]}` says "at least one, and a
bounded amount of incidental activity"; `post_flat: N` can append N quiet bars to
discourage re-entry, but it is a courtesy, not a guarantee.
3. **Report, don't lie.** If still unsatisfied, behaviour is configurable: `on_unsatisfied: warn` (default — log a WARNING with the params that defeated it, keep the bars) / `error` (raise `ScenarioUnsatisfied`) / `fallback_flat`. Never return a "triggered" scenario that didn't.

This is what makes the promise testable: `assert scenario_report.entries >= 1` in a property test, not a prayer.

### D3 — Determinism by construction, with a printable manifest
`seed = fnv1a(f"{scenario}|{symbol}|{timeframe}|{from}|{to}|{sorted(params.items())}")`. Two calls, two processes, same bars. Every generation returns a manifest (via `/api/scenarios/preview` and `--print-manifest`) with seed, rows, first/last ts, entry/exit bar indices, and a `sha256` of the OHLCV array — so a bug report can be pinned by pasting 6 numbers, and `pytest` can assert on that digest (a golden test that catches accidental data drift in a refactor).

Note the interaction with `G4`: the forward replay must not reveal a signal before its bar. A scenario with a *known* entry index turns that into an assertion — at `revealed == entry_index` the payload's `buys` gains exactly that entry and no more.

### D4 — Timestamps live inside market sessions (with a continuous escape hatch)
`TimeManager` (`marketdata/timesync.py`) already knows NSE phases, holidays and `align_to_timeframe`. Emit 1-min bars only inside the continuous session (09:15–15:30 IST), skip non-trading days, so intraday runs look like the real thing and the daily resample lands on real trading days.
- `session_mode: "anchored"` (default) | `"continuous"` (24×5, no gaps) for pure stress/volume tests where a lunch-break gap is only noise.
- The `> 50 rows` restriction does not apply here; short windows are the point. (`SyntheticSource` keeps its rule for compatibility — see §7 migration.)
- Known limitation inherited from the calendar (tracker limitation #3): special sessions (Muhurat, half-days) are unmodelled. Documented, not fixed here.

### D5 — Surfaces: source, config, API, UI
```
DataSource protocol           backtest.runner.build_source("simulator")
                              → data.simulator.ScenarioSource(scenario=…, seed=…)
config/scenarios.yaml         profiles + scenario definitions (repo YAML style:
                              `active_profile:` + named blocks, cf. position_sizing.yaml)
GET  /api/scenarios           catalogue for the picker
GET  /api/scenarios/preview   {symbol, scenario, timeframe, rows, manifest} — no backtest run
POST /api/backtest/run         gains an optional "scenario" field (source=configured default)
UI                             Scenario <select> next to Timeframe on Backtest/Compare/Forward
CLI                          --source simulator --scenario breakout-then-fade
```
Everything degrades gracefully: with no `--scenario`, the default profile is `random-walk-compatible`, i.e. behaviourally today's synthetic source but interval-honouring — so `--source simulator` is useful on day one, before scenario authoring exists.

### D6 — Cost model and budgets
Generation is **per request, lazy, vectorised** (numpy, no Python loop per bar); nothing is stored unless you opt in.
Measured on this box (§2b), against the budget in angle brackets:

| Case | Budget | Measured |
|---|---|---|
| 1 symbol, 1 year, 1-min atomic (98k rows) | < 150 ms | **14.9 ms** |
| + resample to any coarser interval | — | **~7 ms** |
| Backtest run on the resampled 1D / 1H / 15M / 1min frames | < 5 s (PRD §5) | **4 / 9 / 24 / 339 ms** |
| Verify pass (D2 step 3) per attempt | — | ~4–25 ms |
| 12-attempt re-roll worst case | < 1 s | **≈ 0.2 s** |
| 50 symbols × 1 year, 1-min | < 2 s | **0.70 s**, 4.69M rows, ~0.21 GiB |

Consequences: the 1-year/1-min case is *affordable inline*, so the "90-day interactive
sweet spot" caveat below is preference, not necessity; and D2's verification loop does
not need a cache to be fast — cache it anyway (Phase 2 risk) so Compare's four slots
don't pay 4×.
Hard cap `max_bars_per_request` (default 500 000) → 400 with a clear message and a
`--log-level DEBUG` trail, instead of a 2 GB pandas frame. (Measured worst case in the
table: 50 symbols × 1 year is 4.69M rows / 0.21 GiB in 0.70 s, so the cap is about
*someone asking for 500 symbols for a decade*, not about today's demos.)

One thing the table makes unavoidable: **a coarse run is a lossy view of a fine run.**
The `1min` result (−48%) is the *same market* as the `1D` result (+355%), differing only
in how many entries the strategy gets to take and pay costs on. That is the point — but
it also means the simulator's numbers must never be presented as "the strategy's
performance" in docs or UI copy; they are "performance on this generated walk at this
interval".
Optional `persist_to_db: true` writes the generated set into `market_data_cache` (source column `simulator`) so the **real** `DbSource` path — resampling, `list_symbols`, index use — can be exercised in dev without credentials. Off by default; it is a shared-mutable-state footgun.

### D7 — Where the code lives (layering rule respected)
```
src/backtest/data/simulator/
    __init__.py
    generate.py       # atomic 1-min OHLCV from a shape recipe + noise
    resample.py       # → any interval (shared map, no third copy)
    shapes.py         # ShapeRecipe, builders per strategy family
    registry.py       # scenario YAML ↔ ScenarioSpec
    source.py         # ScenarioSource: implements the DataSource protocol
    manifest.py       # ScenarioManifest, digest, reporting
    quality_hooks.py  # optional: inject spikes/flat bars/gaps for marketdata/quality.py
```
It may import `strategy/`, `marketdata/`, `data/base`. It must **not** be imported by `simulator/` (that package is pure in-memory domain logic per the layering rule) and it must not import `engine/` — verification of "did it trade" goes through `runner.run_on_candles` *at the call site* (CLI/API/pytest), injected as a callable, so the generator stays testable in isolation.

---

## 4. ScenarioSpec (the contract authors write)

```yaml
# config/scenarios.yaml
active_profile: default

profiles:
  default:
    session_mode: anchored        # anchored | continuous
    base_seconds: 60              # atomic generation resolution
    max_attempts: 12              # re-rolls before zero-noise construction
    on_unsatisfied: warn          # warn | error | fallback_flat
    max_bars_per_request: 500000

scenarios:
  trend-reversal:
    description: Cross up, hold, cross back down — exercises both vectorized entries.
    family: trend_cross            # drives the builder; unknown family → constructive-only
    bars: { pre: 60, hold: 25, post: 20 }   # warm-up / in-position / exit+tail (atomic bars)
    drift: { pre: -0.00002, hold: 0.00035, post: -0.00040 }
    vol: 0.0009
    volume: { base: 1800, entry_spike: 3.0 }
    expect: { entries: [1, 3], exits: [1, 3], max_hold_bars: 40 }

  breakout-stop-out:
    description: New 20-bar high triggers entry, then a spike down inside the next
      2 bars closes it at the stop — the "cut within 1–2 minutes" case.
    family: channel_breakout
    bars: { pre: 80, hold: 2, post: 10 }
    intrabar: { after_entry: 2, overshoot: 0.985 }  # bar e+2 low = stop × 0.985 (relative to the
                                                     # *measured* entry, never to an assumed price)
    expect: { entries: 1, exits: 1, exit_reason: stop }

  breakout-target-hit:
    family: channel_breakout
    bars: { pre: 80, hold: 3, post: 10 }
    intrabar: { after_entry: 3, overshoot: 1.015 }  # bar e+3 high = target × 1.015
    expect: { entries: 1, exits: 1, exit_reason: target }

  oversold-recovery:
    family: mean_reversion
    bars: { pre: 40, hold: 12, post: 12 }
    drift: { pre: -0.0012, hold: 0.0016, post: 0.0006 }
    expect: { entries: [1, 4], exits: [1, 4] }

  noisy-market:                # for data-quality + circuit-breaker tests
    family: none
    bars: { total: 400 }
    anomalies: { spike_pct: 0.25, flat_run: 6, gap_minutes: 22, zero_volume_bars: 3 }
    expect: { entries: null }   # no trade requirement — must not fail for emptiness

  always-in:                   # buy&hold-shaped, for benchmark/compare slots
    family: always_in
    bars: { total: 262 }
    drift: { hold: 0.0004 }
    expect: { entries: [1, 1], exits: [0, 1] }
```

Rules the loader enforces (each is a test in Phase 1): unknown `family` → warning + constructive-only; `expect.entries` unsatisfiable with the given `bars.pre` and a strategy's lookback → error at load time, not at run time; `intrabar` requires `expect.exit_reason ∈ {stop, target}` and a strategy that declares `stop_loss`/`take_profit`; `anomalies` are ignored unless `quality_hooks` is explicitly used (so they can't silently corrupt a backtest).

`expect.*` is a *check*, not a knob — the generator doesn't force numbers to match; it reports and the caller decides (D2 step 3).

---

## 5. Task decomposition

Legend: size **S** ≤ half day · **M** ~1 day · **L** 2 days. Every task ends green: full suite + its own new tests.

### Phase 0 — Foundations (no behaviour change yet)
| # | Task | Files | DoD | Size |
|---|---|---|---|---|
| 0.1 | One shared timeframe map. Extract `_TIMEFRAME_TO_INTERVAL` (today duplicated in `api/backtest.py` + `api/forward.py`) and `DbSource._INTERVAL_TO_RULE` into `marketdata/timeframes.py`; add `aliases()`, `to_pandas_rule()`, `is_supported(source)`. Both APIs import it. | `marketdata/timeframes.py`, `api/backtest.py`, `api/forward.py`, `data/db_source.py` | Existing tests untouched and green; `SUPPORTED_TIMEFRAMES` derives from the map (no drift); a test asserts the three former copies agree | M |
| 0.2 | Resample helper: `resample_ohlcv(df_1min, interval)` (first/max/min/last/sum), NaN-safe, DST/tz-naive-safe per the canonical frame contract. | `data/simulator/resample.py` (used by `DbSource` too) | Property test: for any generated 1-min set, `resample("day").close == last minute close`, `high == max`, and `len(day rows) == trading days`; `DbSource` switched onto it with its tests green | M |
| 0.3 | `ScenarioManifest` + digest + `seed_for()`; `on_unsatisfied` policy enum; `ScenarioSpec` dataclass with load-time validation. | `data/simulator/manifest.py`, `spec.py` | Unit tests for determinism, digest stability, validation messages | S |

### Phase 1 — Interval-honouring source (this alone closes G6)
| # | Task | Files | DoD | Size |
|---|---|---|---|---|
| 1.1 | `AtomicGenerator`: 1-min OHLCV from (drift, vol, volume, seed) — vectorised numpy, session-anchored or continuous via `TimeManager`; returns frame + bars metadata. | `data/simulator/generate.py` | 1 year ≈ 94k rows in < 150 ms; identical inputs → identical digest; no bar outside 09:15–15:30 IST in `anchored`; `low ≤ min(open,close)` and `high ≥ max(open,close)` on every row | M |
| 1.2 | `ScenarioSource` implementing the `DataSource` protocol (`get_candles(symbol, start, end, interval)`), wired into `runner.build_source("simulator")` + `--source simulator` + `/api/config` advertising it. Default profile = walk-compatible (no scenario). | `data/simulator/source.py`, `runner.py`, `web/app.py` | `1D` and `1H` on the same symbol/range now differ in row count **and** results (assert in `test_api_backtest.py`-style test); `4H`/`15M`/`1min` all produce distinct bar counts; the `[synthetic] interval not supported` warning is replaced by `[simulator] generated N × 1min → M bars @ 1H` at INFO | M |
| 1.3 | Guard rails: `max_bars_per_request`, empty range → clear error (not the `> 50 rows` message), `log warning` when a window has fewer bars than the strategy's warmup. | `source.py` | 400 with an actionable message for an over-budget request; a 10-bar window returns 10 bars, not an error | S |
| 1.4 | Close **G6** in the tracker + docs: `docs/DATA-SOURCES.md` gains the simulator section; the "timeframe is cosmetic" startup WARNING is downgraded to fire only for `synthetic`/`csv`. | docs, `web/app.py` | Grep for "cosmetic" in docs matches only the legacy-sources note | S |

### Phase 2 — Scenarios that must trade
| # | Task | Files | DoD | Size |
|---|---|---|---|---|
| 2.1 | `shapes.py`: builders per family (`trend_cross`, `mean_reversion`, `channel_breakout`, `always_in`, `none`), each returning a drift/level schedule from `bars` + `drift`. | `data/simulator/shapes.py` | For each family, a *pure construction* (zero noise) path yields the intended crossing; unit tests compute the indicator by hand on 30-bar frames | L |
| 2.2 | `intrabar` forcing — **two-pass** (see §2b: the one-pass guess did *not* fire): run pass 1, take `e = first held bar`, `entry = close[e-1]`, `level = entry × (1 ∓ sl/tp)`, then bend bar `e + after_entry` so its low/high crosses `level × overshoot`. | `generate.py`, `verify.py` | `breakout-stop-out` at 1-min: a closed Loss within `after_entry` bars of the *measured* entry (reference: −6,574.46 in the §2b prototype). At 1D the same scenario closes for −6,574.37 — agreement within 1.0 is the acceptance test for "risk events survive resampling" | M |
| 2.3 | Verification + retry loop (D2): run the strategy, count entries/exits/hold length, re-roll noise up to `max_attempts`, then zero-noise; honour `on_unsatisfied`. | `data/simulator/verify.py` | Property test over all 4 built-in strategies × 6 params sets: `entries ≥ 1` holds, or a WARNING names the params; with `on_unsatisfied: error` it raises `ScenarioUnsatisfied` | L |
| 2.4 | `config/scenarios.yaml` + registry loader (repo YAML style), `--scenario` CLI flag, `GET /api/scenarios`, `GET /api/scenarios/preview`. | `registry.py`, `cli.py`, `api/scenarios.py`, `web/app.py` | Unknown scenario → 404 with the list of names; preview returns manifest without running a backtest; YAML round-trips through the loader with validation errors as messages | M |

### Phase 3 — UI + workflow integration
| # | Task | DoD | Size |
|---|---|---|---|
| 3.1 | Scenario picker on Backtest / Compare / Forward (one shared component, next to Timeframe), disabled with a tooltip when `source != simulator`, and it carries through Promote (which also fixes `G11`'s "promote loses dates" half if we land them together). | Promote from a scenario run reproduces identical bars on the Forward page (manifest digests equal) | M |
| 3.2 | "Why no trades?" affordance: when a run yields 0 trades, show the reason the server already logs (warmup > window / params out of range / source ignores interval), fetched from the response (`diagnostics` block). | A 0-trade run explains itself in the UI, not only in `--log-file` | S |
| 3.3 | Compare: prove the feature — 4 slots differing only in timeframe on the same scenario produce 4 different results. | Golden test on `run-many` with the scenario pinned | S |

### Phase 4 — Deeper realism (each independently valuable)
| # | Task | DoD | Size |
|---|---|---|---|
| 4.1 | `quality_hooks`: inject the `noisy-market` anomalies and feed them through `marketdata/quality.py`, asserting spike/gap/zero-volume detection and repair/reject policies. | Each anomaly in the spec is detected ≥ once by the validator at `strict` and reported (not silently repaired) at `lenient` | M |
| 4.2 | Universe mode: `get_candles` for a `Universe` id → per-symbol bars sharing one market clock (common shocks), for pool scanning in the portfolio centre. | 50 symbols × 1 day 1-min in < 2 s; cross-symbol correlation sanity (all names drop on the shock minute) | M |
| 4.3 | Swap the portfolio centre's `SyntheticFeed` to consume the same `AtomicGenerator` (its `crash_symbols` becomes a scenario anomaly) — one source of synthetic truth for both the bar-by-bar replay and the live loop. | `benchmark_portfolio.py` stays within budget; `test_portfolio_engine.py` green; the feed's `warmup_bars` reads from the scenario | M |
| 4.4 | Optional `persist_to_db` writing `source='simulator'` rows, so `DbSource` (hypertable + resample) is exercisable in dev. Guarded by an explicit flag + a `DELETE … WHERE source='simulator'` cleanup helper. | Round-trip: generated == read-back for a symbol/day; cleanup leaves 0 rows | M |

### Phase 5 — Test infrastructure & docs
| # | Task | DoD | Size |
|---|---|---|---|
| 5.1 | `tests/test_simulator_*.py` suite + golden manifests committed under `tests/fixtures/simulator/`. | Determinism, digest, budgets, `expect` verification, `on_unsatisfied` modes, 4 strategies × 6 intervals matrix (skip-marked slow cases) | M |
| 5.2 | `benchmarks/benchmark_simulator.py` + CI budget assertion. | Documents measured ms/row; a 2× regression fails the bench test | S |
| 5.3 | `docs/MARKET-SIMULATOR.md` (authoring a scenario, the `family` table, budgets) + README/docs wiring + `PROJECT-CONTEXT.md` invariants (determinism rule). | A newcomer can add a new scenario from the doc alone, with no code change | S |
| 5.4 | `config/scenarios.yaml` ships with the 6 scenarios above documented and each referenced by a test. | `pytest -k scenario` covers every shipped scenario | S |

### Phase 6 — Optional, separate decision (the "full market sim")
Tick-level bars beneath the 1-min atomic layer, `simulator/execution.py` order routing on those ticks (partial fills, queue position, latency), and `fees.py`/`slippage.py` profiles driven per-fill instead of per-bar-return. Only worth doing when we start trusting the engine for sizing decisions. **Explicitly out of this epic.**

**Dependency graph:** `0.1 → 0.2 → 1.2`; `1.1 → 2.1 → 2.2 → 2.3 → 2.4 → 3.x`; `4.1`/`4.2`/`4.4` depend on `2.3`; `4.3` depends on `1.1`; Phase 5 tracks the phases it documents. Phases 0–1 are independent of scenarios and can ship first.

---

## 6. Acceptance criteria (epic DoD)

1. `--source simulator`, any of the 4 built-in strategies, **default params**, 1 year → ≥ 1 closed trade **and** ≥ 1 exit for `trend-reversal`; the run is deterministic (digest equal across two processes).
2. The user's sentence, testable: `breakout-stop-out` at `1min` — entry at the bar the
   strategy actually took, `e + after_entry`'s low trips the stop, the trade closes as a
   `Loss`; the same scenario at `1D` closes for the same amount (±1.0). `expect` checks
   are range-based, since forcing legitimately creates a later re-entry (§2b).
3. All 7 intervals produce distinct bar counts on the same window, and `1D` vs `1H` on the
   same scenario produce **different** metrics (kills the "identical slots" finding). The
   prototype already shows the scale to expect: same walk → 262/1,834/6,550/98,250 bars and
   +355% / +332% / +230% / −48% across `1D/1H/15M/1min` (§2b).
4. Compare with 4 timeframe-varying slots returns 4 non-identical result sets; the trade table and the cards agree (uses the `engine/trades.py` invariants from `G1/G2`).
5. Forward replay on a scenario: the payload never contains a signal dated after the revealed bar (`G4`'s rule), and the entry appears **exactly** at the revealed entry bar — asserted, not eyeballed.
6. `noisy-market` yields ≥ 1 validator finding per anomaly type.
7. Budgets in §D6 met on the dev box; a regression > 2× fails the benchmark test.
8. Unknown scenario / unsupported interval / over-budget request each produce a distinct, actionable message at WARNING, visible with `--log-level INFO` without a stack trace.
9. `docs/MARKET-SIMULATOR.md` lets someone add a scenario without reading code, and
   `python -m backtest run --source simulator --scenario trend-reversal …` prints the
   manifest digest alongside the metrics (task 2.4 adds the flag; 5.3 documents authoring).
10. Existing suite: **1893 passed / 4 skipped / 0 failed** stays green with no test weakened, plus the new ones. `SyntheticSource` behaviour unchanged (its `> 50 rows` rule intact) so nothing downstream churns.

---

## 7. Migration & compatibility

| Concern | Decision |
|---|---|
| Default source | stays `synthetic` for one release; `simulator` documented as recommended; flip after Phase 3 so docs/demos change together |
| `SyntheticSource`'s `> 50 rows` rule | kept as-is (tests depend on it). The new source has no such rule; the shared "warmup longer than window" WARNING moves to `runner` so both benefit |
| Existing 1571 tests / golden fixtures | unaffected: no changes to `SyntheticSource`, `CsvSource`, or the `DataSource` protocol signature |
| `G6` | closed by Phase 1 — **without** any scenario work, which is why 0.1–1.4 is worth shipping first |
| `G11` | partially absorbed: with `--scenario` in the body, scenario validation is a natural place for the param-range 400; the promote-carries-dates half is separate and can land in 3.1 |
| `G9` (export) | the manifest digest belongs in any future CSV export header — worth a line when G9 is done |
| Portfolio centre | `SyntheticFeed` stays until 4.3; no half-migrated state |

---

## 7b. Decisions log

| # | Decision | Outcome | Consequence in the design |
|---|---|---|---|
| Q1 | Atomic resolution | **60 s now, permanently a config knob** (`base_seconds`) | `base_seconds: 60` in `profiles.default`; every cost/resolution claim in §2b/§D6 is stated per atomic bar, so flipping the knob re-derives them by multiplying, and nothing in the code assumes 60. A 15 s change is a config edit + re-measure, not a phase. |
| Q2 | Scenario authoring surface | **YAML + Python registry underneath** | `config/scenarios.yaml` is the authoring format; `data/simulator/registry.py` is the loader and the place other code registers programmatically; validation errors are load-time and worded for a human editing YAML. |
| Q3 | Default source after Phase 3 | *recommendation stands* (keep `synthetic`, document simulator as recommended) | Revisit at 5.3 (docs) rather than at 1.2, so behaviour and docs flip together. |
| Q4 | `persist_to_db` (4.4) | *recommendation stands* (defer) | 4.4 stays in the plan but marked deferrable; the `source='simulator'` tag on rows is still designed in now, so deferring costs no rework. |
| Q5 | Extra families in 2.1 | *recommendation stands* (add `gap_open`, defer the rest) | `gap_open` lands with the family builders; `range_bound_chop` / `earnings_jump` become a follow-up task against a settled abstraction. |

## 8. Risks

| Risk | Mitigation |
|---|---|
| "Triggering" silently degrades into *fake-looking* data (spike at bar 5 with nothing around it) | `expect` reporting + `on_unsatisfied: warn` keeps it honest; `family: none` is the escape hatch for realism-first runs; never claim a scenario fired unless the strategy said so |
| Verification loop costs an extra backtest per request | Cache verification per (scenario, symbol, timeframe, window, params) for the process; skip entirely when `expect` is absent. Measured: ~4–25 ms per pass, so this is an optimisation, not a fix for a live problem |
| Naive exit-forcing silently does nothing (spike placed relative to a *guessed* entry misses the real stop level) | Two-pass forcing is mandatory (D2 step 2) and `expect` verification on the final frame is what catches any future regressions of it — this exact failure was reproduced in §2b before the design was corrected |
| Forcing an exit changes downstream signals (extra re-entries appear) | `expect` is ranges by design; `post_flat` discourages it; never assert exact trade counts on a forced scenario |
| 1-min generation for 1 year × 500 symbols in Compare/pool paths | `max_bars_per_request` + per-request lazy generation + 4.2's budget test; docs state the 90-day interactive sweet spot |
| Second implementation of resampling drifting from `DbSource` | 0.2 makes `marketdata/timeframes.py` + `resample.py` the single helper both use, with a "the three former copies agree" test |
| New strategies outside the 4 families never trigger | constructive-only path + `expect` check + WARNING naming the params; document `family: none` + how to add a builder (20 lines) |
| Session anchoring hides weekend/edge-case bugs | `session_mode: continuous` exists precisely to find them |

**Open questions for you (before Phase 0/1):**
1. Atomic step: **60 s** as proposed, or 15 s so 1-min bar *content* is also synthetic? (Adds 4× data; only buys intrabar realism inside a minute bar.)
2. Scenario definitions: **YAML in `config/scenarios.yaml`** (editable, validated, no code) vs Python-registered (typed, IDE-friendly)? Recommendation: YAML with a Python registry underneath — matches how `config/*.yaml` already works.
3. Should `--source simulator` become the **default** after Phase 3 (better demos, risk: newcomers' first result is scenario-shaped rather than neutral)?
4. `persist_to_db` (4.4): include in the epic, or defer until the DB path is a real priority?
5. Any extra scenario families worth building in 2.1 now (e.g. `gap_open`, `range_bound_chop`, `earnings_jump`) — cheap to add while the builder abstraction is fresh, expensive later.

---

## 9. What this unlocks afterwards

`G6` closes with Phase 1. Then the cheap, visible remainder is: `G11` (param 400s; partly in 2.4/3.1), `G7`–`G10` (chart annotations/tooltips/markers), `G12` (one symbol source — the scenario picker lands in the same component), `G13` (hygiene), and only then the V2 persistence item (which 4.4 grazes).
