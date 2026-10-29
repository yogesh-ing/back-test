# Branch → source-code matrix

Snapshot date: **2026-10-20** · local refs only · regenerate with `bash tools/out/_branch_matrix.sh`

This exists because the repo has two lines of development that have **not** been merged with
each other, and it is no longer possible to tell from branch names which one a given feature
lives on. Check before starting work: `git rev-parse --abbrev-ref HEAD` + the table below.

## Read this first

- **v2 "Strategy-Owned Execution" code is on exactly two branches**: `feature/options-trading-dev`
  and its child `feature/trades-db-fallback`. `main` still has the legacy strategy base —
  `EmaPullback(Strategy)` on `main` vs `EmaPullback(EquityStrategy)` on the refactor branch.
- **`main` is not an ancestor of the refactor branches and vice versa**: main has 18 commits the
  refactor line lacks (Upstox + data-era work), the refactor line has 23 (24 with the DB-fallback
  commit) that main lacks. Neither side has been merged into the other.
- Anything else in `refs/heads/` is already merged into `main` (ahead count `+0`) — historical,
  no action needed.

## Active branches

| branch | last | vs main | worktree | role |
|---|---|---|---|---|
| `feature/trades-db-fallback` | 2026-10-19 | +24/−18 | `C:/learning/back-test-trades-fallback` | **newest tip** = full v2 refactor + trades DB-fallback endpoint |
| `feature/options-trading-dev` | 2026-10-17 | +23/−18 | `C:/learning/back-test` (main checkout, currently checked out) | v2 refactor, PR #45 landed here |
| `main` | 2026-10-18 | — | none (not this checkout) | Upstox + data-fetch line, legacy strategies |
| `feature/upstox-login-testing` | 2026-10-18 | +0/−4 | `.worktrees/upstox-login` | merged into main via `a3ed163` |
| `feature/upstox-historical-data` | 2026-10-15 | +7/−18 | none | superseded — tip `eedf995` is a parent of `f49e89a` |
| `options-work` | 2026-10-15 | +4/−18 | `C:/learning/back-test-opt-dev` | superseded — its 4 commits (WS depth, gate UI, forward fixes) are inside the refactor line |
| `feature/options-strategy-builder` | 2026-10-08 | +0/−37 | none | merged into main |

## Which branch has which source

Marker = file exists / symbol present in that branch's tree. `Y` present, `.` absent.

| branch | v2 `execution.py` | v2 `templates/` | strategy cards | trades DB fallback | Upstox fetcher | coverage table `020` | WS depth `mticker` | broker alerts | holiday seeding |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| `feature/trades-db-fallback` | Y | Y | Y | Y | . | . | Y | Y | Y |
| `feature/options-trading-dev` | Y | Y | Y | . | . | . | Y | Y | Y |
| `main` | . | . | . | . | Y | Y | . | Y | Y |
| `feature/upstox-login-testing` | . | . | . | . | Y | Y | . | Y | Y |
| `feature/upstox-historical-data` | . | . | . | . | . | . | Y | Y | Y |
| `options-work` | . | . | . | . | . | . | Y | Y | Y |

Evidence paths per marker:

| marker | path checked |
|---|---|
| v2 execution contract | `src/backtest/strategy/execution.py`, `src/backtest/strategy/templates/__init__.py` |
| strategy cards | `description_sections` in `src/backtest/strategies/ema_pullback.py` (commit `245b29e`) |
| trades DB fallback | `_db_closed_trades` in `src/backtest/api/portfolio.py` (commit `a82dec2`, +101 lines + `tests/test_trades_db_fallback.py`) |
| Upstox historical fetcher | `Upstox` in `src/backtest/api/data_manager.py` (commits `1b7abe2`…`95e536b`) |
| coverage summary table | `migrations/020_coverage_summary_table.sql` (commit `bff3cf8`) |
| mStock WebSocket depth | `src/backtest/data/mticker.py` (commit `2d47e4a`) |
| broker-session alerts | `src/backtest/alerts/types.py` |
| NSE holiday seeding | `tools/seed_market_holidays.py` |

Note: `src/backtest/strategy/intent.py` and `signal.py` exist on every branch including `main` —
they predate the refactor and are **not** v2 markers.

## What each line of development contains

### Line A — Strategy-Owned Execution refactor v2 (23 commits, `26259ed` → `f49e89a`, PR #45)

```
f49e89a  Merge arena/2582821d: Phases 0-4 + strategy cards (PR #45)
245b29e  Strategy cards: structured descriptions on the backtest page
80e10ad  Phase 4: legacy options path deleted, strict end state
08a1930  Phase 3: UI — selections-only spawn, contract rendering, fingerprints
c0f9f5b  Phase 2: strategy rewrite complete (plan v2.1 §9)
4153278  Phase 1: one Risk Gate + strict bridge + spawn resolution
1af6fbd  Phase 0: strategy-owned execution foundations
0449265  chore(snapshot): freeze all 38 pre-rewrite strategies
50f88fc  docs(plan): Strategy-Owned Execution refactor v2.1
+ 4 commits inherited from options-work (WS depth, gate-on-broker-auth,
  daily-loss anchors/NEUTRAL mapping, the plan doc)
```

Phase 2 in short: `atm_instant_buy`/`immediate_entry`/`nifty_instant_buy` replaced by the CE/PE
instant-buy split; `immediate_strangle` moved onto `StructureStrategy`; directional options onto
`DirectionalOptionStrategy`; 10 builtin + 6 plugin equity strategies ported to `EquityStrategy`
signal-identical; six duplicate Pine supertrend copies collapsed into
`plugins/strategies/supertrend_equity.py`.

### Line B — data & broker era (18 commits, on `main`, `83825b3` → `95e536b`)

```
95e536b  fix(upstox): use requested broker's token, not always mStock
fde8376  revert(freshness): show worst-case staleness again
846a4fa  fix(freshness): show current based on best timeframe (the reverted commit)
a3ed163  Merge branch 'feature/upstox-login-testing' into main
bff3cf8  perf(data): coverage summary table for instant data-freshness checks
1b7abe2  feat(data): Upstox historical data API integration
7612678  feat(data): multi-broker historical fetching
156e711  refactor(data): if/else broker routing → dispatcher pattern
+ OAuth/auth-code fixes, Upstox instrument-key resolution, date format fix,
  CircuitBreaker.record() fix, dual-venue failover plan doc
```

## If you need X, go to branch Y

| you want | branch |
|---|---|
| v2 strategies (`EquityStrategy` / `StructureStrategy` / `DirectionalOptionStrategy`) | `feature/trades-db-fallback`, or `feature/options-trading-dev` if you don't need the new trades endpoint |
| Upstox historical fetching, coverage-summary perf, broker dispatcher | `main` (or `feature/upstox-login-testing`, same content) |
| trades aggregation with DB fallback (permanent closed-trade history via API) | `feature/trades-db-fallback` only |
| both v2 **and** the Upstox line | nobody has it — a merge is required |

## Hazards

- **Duplicate alembic revision `020`, forked at `019`** on *every* branch:
  `20261008_1200_020_option_intent_journal.py` and
  `20261013_1200_020_add_upstox_source_to_portfolios.py` both declare `down_revision = "019"`.
  Plus a third `020` that is not alembic at all: `migrations/020_coverage_summary_table.sql`
  (main only, and only as a raw `.sql` — a second migration mechanism). Expect two heads;
  renumber before merging Line A and Line B.
- **Local `main` is not pushed `main`**: local `main` is 42 ahead / 1 behind `origin/main`, and
  `feature/trades-db-fallback` is 48 ahead / 1 behind it. The one missing commit is the same on
  both sides (`935e6d3 docs(plan): Upstox dual-venue failover architecture plan`), whose local
  twin `83825b3` has the same subject but a different SHA — i.e. that commit was rewritten, not
  merged. Push state must be checked before quoting "what's on main".
- **Uncommitted work sits in the `feature/options-trading-dev` checkout** (`C:/learning/back-test`):
  this doc, plus the trade-reconciliation scripts and outputs from 2026-10-20
  (`docs/BRANCH-SOURCE-MATRIX-2026-10-20.md`, `tools/out/_reconcile_trades.py`,
  `tools/out/TRADE_RECONCILIATION_2026-10-20.csv`, `tools/out/_branch_matrix.sh`,
  `_recon_*.json` dumps) are on no branch. Same for the runtime artifacts
  `src/data/portfolio_state.json` and `*.log`, which must never be committed (see `agents.md`).
- **The running server is on the refactor branch** (3 PIDs were listening on :5000 on 2026-10-20),
  so live behaviour reflects `feature/options-trading-dev`, not `main`.

## Regenerating this matrix

```bash
bash tools/out/_branch_matrix.sh   # per-branch marker table + ahead/behind vs main
git worktree list                  # current worktree ↔ branch mapping
```

The marker probe is file/symbol presence only — it tells you *which tree has the code*, not that
the code works. Verify behaviour on the branch you pick.
