# Swing trading on a multi-stock pool — milestones

Drafted 2026-10-03. Planning doc, not a spec-of-record: each milestone below
gets its own enforceable PRD section before it is built. Discussion source:
multi-select picker idea (Backtest/Compare/Optimize), batch-vs-portfolio
decision, Nifty50/100/200 presets.

## Decisions already taken

1. **Batch first, portfolio later.** Multi-symbol selection fans out to N
   independent runs recorded in the run ledger; one combined equity curve
   (shared capital) is deferred to the execution milestone and needs its own
   allocation spec.
2. **Per-page semantics.** Backtest = batch runs; Compare = one strategy
   across symbols (and today's strategies-on-one-symbol mode, clearly
   labelled); Optimize = per-symbol grid with a cost warning; Execution
   (forward/paper) = pool runner last.
3. **Data-only invariant stays.** The pool picker lists only symbols with
   cached bars (PRD backTest-enhance §1.3, 2026-09-30 direction change).
   Never selectable-without-data.
4. **None-ticked = active curated universe**, mirroring the Data tab's
   fetch-scope rule. Whole-catalogue mode remains banned.
5. **Presets ship as static membership files.** Nifty50 ⊂ Nifty100 ⊂ Nifty200
   (+ Indices). The shipped CSV (`stock-list/nse_ind_nifty200list.csv`) has
   no membership column, so 50/100 constituents are added as a small
   versioned file; NSE constituents API is a later upgrade (Akamai handshake
   needed, same as the holiday API).

## Branch strategy

- New branch `feature/swing-multi-pool`, worktree alongside the others (e.g.
  `C:/learning/back-test-swing-pool`), based on current `main`.
- One milestone = one slice-sized PR into `main`; rebase/merge `main` into the
  branch **before every slice**, never after a long drift. Files shared with
  active main work (`symbol_picker.js`, `data_manager.py`,
  `optimize_setup.js`, ledger endpoints) are the conflict hotspots — keep
  slices small so they stay trivial.
- The old `feature/swing-strategies` worktree (90d52ca) is prunable; round-2
  swing experiments (trail/blend on donchian_1d) continue separately under
  the strategy-battery track and do NOT block M1–M3.

## Pre-flight (before M1, tracked as its own fix)

- **DB connection exhaustion** — the live Flask process held ~99 of 100
  Postgres connections (2026-10-03); batch runs multiply DB demand, so the
  leak (likely repeated engine/`DbSource` creation) must be found and fixed,
  or `max_connections` raised deliberately, before any fan-out exists.

## M0 — Daily data for the pool

Swing = daily-timeframe strategies, and after the 2026-10-03 cache purge
`market_data_cache` holds NSE 1-min only. Nothing downstream is testable
without this.

- Backfill 1day bars, ~3 years, for the curated universe (NIFTY 200 +
  indices) via the existing Data-tab fetch path.
- Verify day-by-day coverage (the "job done ≠ coverage" rule), not just job
  success.
- Exit criteria: coverage endpoint reports ≥ 95 % of the curated universe
  with `1day` bars; a spot-check of 5 symbols matches an external source.

## M1 — Pool picker (shared multi-select)

- Extract the Data tab's checkbox list into a reusable component; add
  preset chips (Nifty50 / Nifty100 / Nifty200 / Indices / None), tick/deselect
  relative to the chip, search still server-backed (`q`, `available=1`).
- Summary line must state: selected count, with-data count, hidden count
  ("N symbols hidden — load data first") — same honesty rules as the current
  single picker.
- Backtest page adopts it first: selection → the scope the run will use;
  none ticked = curated universe-with-data.
- Exit criteria: same component mounted on Backtest; fetch page unchanged in
  behavior; no page can select a no-data symbol.

## M2 — Batch backtest through the run ledger

- `/run` accepts a symbol list and fans out to one ledger run per symbol,
  tagged with a batch id + preset name used (preset lineage already modelled
  in the ledger).
- Sequential execution first (single worker), progress surface reusing the
  fetch-job pattern; partial failure = failed rows in the ledger, not an
  aborted batch.
- History/compare view gains a batch grouping: per-symbol table (return,
  DD, trades, win rate) + distribution summary across the pool.
- Out of scope: shared capital, combined curve, cross-symbol portfolio
  metrics.
- Exit criteria: a 5-symbol batch produces 5 comparable ledger rows with one
  click; a symbol failing mid-batch leaves the other four complete.

## M3 — Optimization over a pool

- Optimizer gains a symbol axis: parameter grid × symbol list, run as
  per-symbol grids sharing one job id.
- Cost guard: the form shows combos × symbols before submit and requires
  confirmation above a threshold.
- Results: best-params-per-symbol leaderboard + robustness view (params that
  win across many symbols beat params that win on one — the swing battery
  gate already demands cross-symbol evidence, this feeds it).
- Exit criteria: a small grid (e.g. 20 combos × 10 symbols) completes, each
  combination recorded with its symbol, and the leaderboard distinguishes
  per-symbol bests from pool-robust params.

## M4 — Compare across the pool

- Compare page gains an explicit second mode: one strategy × N symbols
  (today's N strategies × one symbol stays).
- Mode chosen in the UI, never inferred; shared timeframe rules follow the
  intersection of symbols' available granularities.
- Exit criteria: donchian_1d vs buy-and-hold rendered for a 20-symbol subset
  in one view.

## M5 — Execution: paper forward-test on a pool

- Pool runner (spawn form selects via M1 component): one position budget per
  symbol, shared capital bucket, entry on daily close logic, managed on
  live/1-min where needed.
- Needs its own allocation + risk spec before build: position sizing, max
  concurrent holdings, per-symbol exposure cap, interaction with the
  daily-loss breaker (known UTC-window bug must be fixed first — tracker A6).
- Exit criteria: N-symbol paper run for 5 trading days with ledger-quality
  trade records and no manual babysitting.

## Sequencing

M0 → M1 → M2 is the trunk; M3/M4 can start once M1 lands and interleave;
M5 is gated on M2 + the allocation spec + the daily-loss fix. Each milestone
is worth shipping alone — none of them holds the previous one hostage.
