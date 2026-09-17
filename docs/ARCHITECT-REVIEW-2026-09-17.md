# Architect Review — Cross-Verification of `STATUS-AND-NEXT-STEPS.md`

**Reviewer:** Quant architecture review
**Date:** 2026-09-17
**Repo state reviewed:** `main` @ `1a1eb89` (= `b39da03` + the status doc itself), branch `arena/01a0b02a-back-test`
**Method:** Every material claim in the Jr engineer's doc was checked against the
committed tree — imports executed, `create_app()` booted, test suite collected and
run on a clean venv, referenced files resolved, call sites read.

> ## ✅ Restoration status (updated 2026-09-17, later the same day)
>
> The P0 restoration is **done** on `arena/01a0b02a-back-test`:
>
> * User committed `feed_registry.py` + `plugins/__init__.py` to `main`; merged here.
> * Rebuilt from the §1.4 contract and the task records:
>   `tests/forward/test_feed_registry.py` (22 tests), `templates/` (2 templates),
>   `tests/test_strategy_conformance.py` (21 tests), `tests/forward/test_mstock_live_bus.py`
>   (31 tests), `docs/STRATEGY-AUTHORING.md`, `docs/OPTIONS-FORWARD-TEST-EXPERIMENT.md`
>   (reconstructed from committed sources, provenance stated).
> * **New P0-class finding while restoring:** the F-10 zero-findings lint gate was
>   red on committed `src/` — 56 flake8 findings across 12 files (unused imports,
>   over-long lines). The lint-clean versions were *also* only in the lost working
>   tree. Fixed (mechanical + black@100); `flake8 src/` green again.
> * Fixed a pre-existing e2e test-isolation bug:
>   `test_summary_endpoint_serves_full_session` rehydrated stale open structures
>   from the shared DB (`OPTIONS_PERSISTENCE=auto`); pinned to `off` — DB round-trips
>   remain covered by `tests/test_options_persistence.py`.
> * **Measured on the committed tree now:** 2,471 passed / 4 skipped (mStock
>   credentials), `node --test` 7/7 JS harnesses, `create_app()` boots.
> * **§3.1 CI — DONE:** `.github/workflows/ci.yml` + `scripts/smoke_check.py`
>   (clean install → lint gate → full suite → boot smoke → JS harnesses).
>   Remaining user action: require the CI check in `main`'s branch protection.
> * **§3.2 fail-closed — DONE:** `LiveOptionTrader` defaults to `dry_run=True`;
>   arming real orders needs `dry_run=False` + `confirm_live=True` + env
>   `ALLOW_LIVE_ORDERS=1`, else `ValueError` before any broker call. Gate tests
>   in `tests/test_options_live_trading.py::TestFailClosedGate`.

---

## 0. Executive verdict

The Jr engineer's doc is **architecturally literate but was written from an
uncommitted working tree**. The strategy of the plan (P1 real data → P2 durability
→ P3 engine depth → P4 hygiene) is correct and I endorse it. But:

1. **🔴 The committed repo is broken at HEAD.** The U6.2 / U6.3 / U7.1
   deliverables (`feed_registry.py`, the `plugins` package, three test modules,
   two strategy templates, two docs) exist only on the author's machine — they
   were never `git add`ed. On a clean checkout the web app cannot boot and the
   test suite cannot even *collect* (28 collection errors). The claimed
   "~2,435 passed" is a measurement of an unversioned tree, not of this repo.
2. Several P4 "hygiene" items are **already done** (stale): `live_engine.py` was
   deleted, the dead config files are gone.
3. There are **safety and data-methodology gaps** the doc does not see — the
   biggest being: a fail-open default on the live-money order path, no CI on a
   clean checkout (which is *how* finding 1 happened), no corporate-action
   handling for a 202-stock NSE universe, and no plan to ever accumulate
   **historical** option-chain data (so options *backtests* stay synthetic
   forever even after P1.1 lands).

None of this changes the roadmap's direction. It changes the first week's order.

---

## 1. 🔴 P0 (new, blocks everything) — restore the uncommitted U6/U7 deliverables

### 1.1 Evidence (reproduced on a clean checkout)

```
$ PYTHONPATH=src python -c "import backtest.forward.portfolio_manager"
ModuleNotFoundError: No module named 'backtest.forward.feed_registry'

$ PYTHONPATH=src python -c "from backtest.web.app import create_app; create_app()"
ModuleNotFoundError: No module named 'backtest.forward.feed_registry'

$ PYTHONPATH=src pytest tests -q
28 errors during collection — 1,956 tests collect, 406 test functions blocked
```

`portfolio_manager.py:28` imports `feed_registry` at module scope, and
`web/app.py` imports the portfolio API — so **every web/UI/portfolio surface is
down**, not just the new bus.

### 1.2 Exactly what is missing (all referenced by committed code/docs)

| # | Missing file | Referenced by | Task of record |
|---|---|---|---|
| 1 | `src/backtest/forward/feed_registry.py` — `FeedRegistry`, `ChainBus`, `MStockBarFeed`, `option_quote_provider()`, `reset_data_bus()` | `forward/portfolio_manager.py:28,114` (top-level, fatal), `forward/paper_runner.py:577,775,809` (lazy) | U6.2 + U7.1 |
| 2 | `src/backtest/plugins/__init__.py` — plugin discovery, AST C2 import-ban, conformance battery | `web/app.py:358` (try/except — feature *silently* absent, app still boots) | U6.3 |
| 3 | `templates/equity_strategy_template.py`, `templates/option_strategy_template.py` | U6.3 record | U6.3 |
| 4 | `tests/forward/test_feed_registry.py` (18 tests) | U6.2 record | U6.2 |
| 5 | `tests/test_strategy_conformance.py` (17 tests) | U6.3 record | U6.3 |
| 6 | `tests/forward/test_mstock_live_bus.py` (18 tests) | U7.1 record | U7.1 |
| 7 | `docs/STRATEGY-AUTHORING.md` | U6.3 record, status doc §1 | U6.3 |
| 8 | `docs/OPTIONS-FORWARD-TEST-EXPERIMENT.md` | status doc §4 pointers | — |

Arithmetic sanity: 1,956 (collected here) + 406 (blocked) + the missing modules'
~53 tests ≈ 2,415+ — consistent with the claimed 2,435 on the author's machine.
The tests were committed; their subjects were not.

### 1.3 Recovery plan

1. **First choice — recover from the author's machine.** On the box where
   `b39da03` was built, `git status` will show these as untracked files.
   Commit them verbatim (plus `git status --ignored` sanity check), rerun the
   suite from a **fresh clone**.
2. **Fallback — reconstruct from the call-site contract** (§1.4 below). The
   public surface is small and fully determined by committed call sites; a
   careful reimplementation is a day of work, not a redesign.
3. **Then — make recurrence impossible** (see §4.1 CI): clean-clone pytest +
   `create_app()` smoke + `node --test tests/js/` on every PR. A repo with
   ~2,400 tests and zero CI will keep shipping "green on my machine" states.

### 1.4 Reconstructed interface contract for `feed_registry.py`

Recovered from committed call sites (`portfolio_manager.py`,
`paper_runner.py`, `UNIFIED-TRADING-TASKS.md` U6.2/U7.1) so restoration is mechanical:

```python
# Process-wide singletons
get_feed_registry() -> FeedRegistry
get_chain_bus() -> ChainBus
reset_data_bus() -> None

class FeedRegistry:            # refcounted, keyed (source, symbol, timeframe)
    subscribe(source, symbol, timeframe, feed)   # idempotent per key
    release(source, symbol, timeframe)           # evicts entry at refcount 0

class ChainBus:                # ONE shared SyntheticChainGenerator per underlying
    acquire(underlying) -> SyntheticChainGenerator
    release(underlying)

def option_quote_provider(shared_generator) -> provider   # per-runner, priced off shared gen

class MStockBarFeed:           # ONE poll thread for ALL mstock symbols (rate-limit rule)
    def __init__(feed_client=None, poll_interval_s=60)   # client duck-types latest_bar(symbol)
    on_bar, on_tick_end                # assigned by PortfolioManager
    add_symbols(list), remove_symbols(list)
    start(warmup=True), stop()         # thread runs iff ≥1 mstock runner is live
```

Manager expectations that must hold: `mstock_feed` sits beside the synthetic
`feed` with the same `add_symbols/remove_symbols/start/stop` duck-type; bars ride
the manager's `_on_bar`/`_on_tick_end` fan-out; a closed market seeds one
catch-up bar per symbol then idles; API errors are logged-and-skipped.

---

## 2. Claim-by-claim cross-verification

Legend: ✅ verified in tree · ⚠️ stale/imprecise · ❌ wrong or contradicted.

### 2.1 "What we have (working today)"

| Claim | Verdict | Evidence |
|---|---|---|
| Vectorized + risk-aware paths reconcile to 1e-5; `target.shift(1)` invariant | ✅ | `PROJECT-CONTEXT.md` invariants; `tests/test_forward_state_roundtrip.py` etc. |
| Indian cost model: STT/exchange/SEBI/stamp/GST; options flat ₹20, sell-side STT; Decimal 4dp/8dp | ✅ | `simulator/fees.py` (IndiaEquityFees, FY 2024-25 rates, contract-note mapping), `simulator/money.py` (`NUMERIC(20,4)/(20,8)`) |
| 5 equity strategies + market-view options strategy | ✅ | `strategies/`: buy_and_hold, sma_crossover, rsi_reversion, donchian_breakout, price_move + `option_directional` |
| One source of truth for trades (`engine/trades.py`) | ✅ | `engine/trades.py` header; `metrics.compute_metrics` walks it |
| CLI `list/run/compare/preflight/papertrade` | ✅ | `cli.py:209–268` |
| Web UI (backtest, compare, forward, data, health) | ✅ | `web/app.py:373–462` (+ `/portfolio*`, `/dashboard`) |
| Playbook entity + registry, 6 routes, spawn side-effect-free | ✅ | `playbooks/models.py`, `playbooks/registry.py`, `api/playbooks.py` (exactly 6 routes) |
| Exit precedence `emergency > stop > target > DTE > flip`; `reenter` default off; `max_reentries_per_day` | ✅ | `forward/execution_engine.py:52–63` (`EXIT_PRECEDENCE`, `DEFAULT_REENTER = False`) |
| C2 data-ownership assert | ✅ | `forward/execution_engine.py:140,233` |
| **"Shared Market Data Bus (`forward/feed_registry.py`)" working today** | ❌ | **File does not exist in the repo** — see §1. The claim is true only on the author's machine |
| **mStock live bars into the bus, "committed as b39da03"** | ❌ | `MStockBarFeed` lives in the missing file; not in this tree |
| mStock auth + order layer (login/TOTP, order place/modify/cancel, poll_fill, `get_option_chain`, `get_option_quote`, session manager) | ✅ | `brokers/mstock.py:167–843`, `brokers/session_manager.py` |
| `LiveOptionTrader` — "**dry-run default**" | ❌ **safety** | `options/live_trading.py:154`: `dry_run: bool = False`. The default **places real orders** when a caller omits the flag. The docstring example shows `dry_run=True` but the code fails open. This contradicts the repo's own guiding principle #4 ("Fail closed… defaults to *not trading* on doubt", `instructions/ROADMAP.md`). Must flip to `dry_run=True` default + explicit `confirm_live=True` gate before T9.5 |
| Portfolio command center: buckets, breakers, SSE, three views | ✅ | `forward/portfolio_manager.py`, `forward/risk_supervisor.py`, `web/templates/portfolio*.html`; ~113 portfolio test functions across 6 files (doc says 116 — close, spans more files) |
| Strategy plugins: drop-in `plugins/strategies/*.py`, AST import-ban, conformance battery, templates | ❌ | `backtest.plugins` package is **absent**; only the (tolerated) import hook in `web/app.py:358` remains — see §1 |
| "202 stocks / 467K daily bars + 154K instruments in Postgres" | ➖ unverifiable | Data lives in an external Postgres; nothing in-repo can prove the counts. Re-state with a `SELECT count(*)` screenshot/script output when relevant |

### 2.2 "What needs to be done" (P1–P4)

| Item | Verdict | Notes |
|---|---|---|
| P1.1 Wire live option chain + quotes | ✅ correct, **under-scoped** | Runner path is provably synthetic: `forward/options_bridge.py:60–66` hard-imports `SyntheticChainGenerator`/`SyntheticQuoteProvider`. Two extra defects the doc misses: (a) `engine/execution_engine.py:162–207` `_resolve_quote_source` "live" branch returns `MStockLiveFeed` — a **bar** feed (`latest_bar(symbol)`) — where the contract is a **quote** provider (`get_quote(instrument_token)` + `source_name`, `options/quote_providers.py:245–369`). That branch would mislabel `quote_source` and break `get_quote` callers; fix the seam while doing P1.1. (b) `MStockLiveFeed`'s own docstring says it "folds in the polling loop that lived in the old (now-deleted) live forward engine module" — see P4.12 below |
| P1.2 Exercised live dry-run (T9.5) | ✅ | Agree — and block it behind the `dry_run` default fix above |
| P1.3 Equity live fills (F-12) | ✅ | Seams confirmed: `simulator/fill_providers.py:201 BrokerFillProvider`, `brokers/mstock.py:356 poll_fill`, `forward/engine.py:1391` wiring |
| P2.4 Runner/portfolio state persistence | ✅ | Confirmed in-memory (process singletons); Gunicorn `--workers 1` trap documented in `project-overview.md §13` |
| P2.5 Portfolio-page option rows | ✅ plausible | Snapshot fields exist (`options_bridge.open_structures_snapshot`); UI polish open |
| P2.6 Options-tab hard delete criteria | ✅ | `web/templates/options.html` still present (soft-deprecated); review date 2026-09-30 in the doc |
| P2.7 Consultant sign-off on 6 questions | ⚠️ path wrong | The 6 questions exist — in **root `CONSULTANT_RESPONSE.md`** ("Open Questions for Quant Consultant", §0.4 area), not `docs/consultant quest review.md`. No file by that name exists anywhere in the repo |
| P3.8 Position sizing / fill models "todo" | ⚠️ half-stale | Sizing **exists** on the forward/simulator side: `strategy_adapter.py` exports `FixedDollarSizer, PercentagePortfolioSizer, ATRBasedSizer, KellySizer, VolatilitySizer`; `simulator/position_sizing.py` has a full `SizingConfig` engine; `engine/backtest_driver.py:52` takes a `size_fn` hook. What's actually missing: presets exposed through the **vectorized engine + CLI**, and pluggable slippage/fill model selection. Scope the ticket down |
| P3.9 Metrics: Sortino/expectancy/profit factor/monthly heatmap | ✅ | `engine/metrics.py` returns total_return, cagr, volatility, sharpe, max_drawdown, calmar, trade stats — nothing else |
| P3.10 Optimization + walk-forward, Monte-Carlo/deflated Sharpe | ✅ | Not present; legit Phase-2 work |
| P4.12 "`forward/live_engine.py` (697 lines, zero importers) — delete or wire" | ❌ already done | No such file; `data/mstock_live_feed.py` docstring records its deletion (P3.4). Its old tests (`tests/forward/test_live_engine.py`) now exercise `ForwardTestingEngine`. **Close the item** |
| P4.12 "dead config files (`market_data.yaml`, `time_sync.yaml`)" | ❌ already done | Neither exists in `config/` (14 files, all referenced). **Close the item** |
| P4.13 Gunicorn `--workers 1` until state externalized | ✅ | `project-overview.md §13` |
| P4.14 Timeframe cosmetic on synthetic/CSV | ✅ | Known gap G6 |
| P4.15 SQLite NUMERIC→float | ✅ | Consistent with `simulator/money.py` design note |
| P4.16 Broker rates FY 2024-25 | ✅ | `fees.py` rates banner says exactly this |
| P4.17 Alerts; auto-kill | ✅ | Roadmap 3d, not present |

---

## 3. New gaps the doc does not see (architect additions)

Ordered by risk to a live Indian-markets options desk.

### 3.1 No CI on a clean checkout (process gap that caused P0)
No `.github/`, no pipeline anywhere. The suite passes on one laptop with
unversioned files. Minimum gate per PR: (a) fresh `pip install -r requirements.txt`,
(b) `pytest tests -q` + flake8 baseline (`tests/test_lint_baseline.py` already
exists as an in-suite gate), (c) `node --test tests/js/*.mjs` (8 harnesses), (d)
`create_app()` smoke + one synthetic runner tick. Also root-cause the "known
Windows process-pool flake" or mark it `@pytest.mark.quarantine` with an issue —
permanent known-flakes hide real regressions.

### 3.2 Fail-open default on the live-money path (safety)
`LiveOptionTrader(dry_run=False)` (§2.1). For a system whose north star is
"read-only until proven," the live order orchestrator must default to
`dry_run=True` and require an explicit, logged, per-instance
`confirm_live=True` (ideally also a config kill-switch
`execution.yaml: allow_live_orders`). Same review should sweep every
`mode="live"` branch for fail-open defaults (`_resolve_quote_source` falls back
to synthetic *quotes* while orders still go out live — that combination is a
silent basis error: paper-priced fills, real orders).

### 3.3 Corporate actions: no split/bonus/dividend handling anywhere
`grep` across `data/` and `engine/` finds no adjustment logic. A 202-stock NSE
daily-bar universe fetched via TypeA history will contain raw prices across
splits/bonuses unless the vendor pre-adjusts (mStock TypeA is raw). One unadjusted
10:1 split produces a −90% "drawdown" and poisons every metric and any
walk-forward split crossing the date. Needed: an adjusted-price policy per source,
a corporate-action table (or vendor-adjusted series), and a data-quality rule in
`config/data_quality.yaml` that flags return outliers > ±40% as suspected splits
for manual confirm.

### 3.4 No historical option-chain capture → options backtests stay synthetic forever
P1.1 makes forward tests real but does nothing for options *research*:
`OPTIONS-BACKTEST-PRD.md` honestly admits pricing is "synthetic Black-Scholes,
flat IV" and "does not validate edge against" real premiums. Unless chain
snapshots (strikes, premiums, IV, OI, lot) are **stored from day one** of live
wiring, the project will forever price option backtests off BS. Add now (cheap
while the live wiring is open): a nightly + intraday EOD chain snapshotter →
`option_chain_snapshots` table; it is the single highest-leverage data asset this
project can accumulate, and it unblocks IV-surface research and the risk-envelope
V2 the consultant is already asking about.

### 3.5 Order-path robustness for live (beyond F-12)
The broker layer has an idempotency concept (`client_order_id`, `brokers/base.py:176`)
but no enforced retry/idempotency discipline on placement, no rate limiter on
order endpoints (the one-thread rule covers *bars* only), and no position
reconciliation loop vs. the broker book (ROADMAP Phase 4 explicitly requires
"Idempotent orders, broker-state sync"). Sequence this with F-12, not after.

### 3.6 Playbook registry persistence is opt-in
`playbooks/registry.py` persists only when `PLAYBOOKS_PATH` is set; unset, spawned
runners outlive their definitions after restart and the audit trail loses the
"what config produced this runner" answer. Fold into P2.4: default the path from
`config/app.yaml`, stamp `playbook_id` + `playbook_version` into every spawned
runner's config (version field exists — carry it through `to_runner_config`).

### 3.7 Repo hygiene
- `graphify-out/` (tool cache) and `.idea/` are **tracked** despite `.gitignore`
  entries — 52 files of IDE/tooling noise. `git rm -r --cached` them; keep
  `graphify-out` in `.gitignore` (it already is — it was committed before).
- `tests/forward/test_bucket_risk.py` imports fixtures from a sibling *test*
  module (`from forward.test_live_engine import FakeLiveBroker…`) — move shared
  fixtures to a `conftest.py`/helpers module; test-to-test imports break under
  pytest collection randomization and packaging.
- Status doc's own pointer table references three files that don't exist
  (fixed in the doc patch).

### 3.8 Secrets posture — verified OK
No hardcoded keys in `config/*.yaml` or source; `.env` and `*.session.json`
ignored; `.env.example` uses placeholders. Keep it that way when the mStock
app-key/TOTP secrets start flowing through `brokers/session_manager.py`.

---

## 4. Re-cut execution order (supersedes status doc §3)

```
Week 1 (stabilize + prove):
  1. P0  Restore missing U6/U7 files (recover from author machine; else §1.4 contract)
  2. P0  CI: clean-clone pytest + JS + create_app smoke; quarantine Windows flake
  3. P1  Live chain wiring (P1.1) — including fixing the U2 quote-seam mismatch
  4. P1  Flip LiveOptionTrader to fail-closed defaults (dry_run=True)  ← hours, do it first
  5. P1  Start EOD option-chain snapshot capture (rides P1.1; data asset accrues)
  6. P1  T9.5 exercised dry-run; F-12 equity fills + order idempotency/reconciliation

Week 2+ (as the Jr doc had it, unchanged in spirit):
  7. P2  Runner/portfolio persistence → then Gunicorn
  8. P3  Engine depth (sizing presets scoped to vectorized engine + CLI, metrics, optimization)
  9. P2  Corporate-action policy + data-quality rule (new)
 10. Consultant answers → risk-envelope V2 (IV from stored snapshots, SPAN)
```

The one philosophical note: the plan optimizes for *forward-test realism* while
options *research* remains synthetic. §3.4 is the smallest change that fixes that
asymmetry permanently — start accumulating the data before you need it.

---

## 5. What I verified and could not verify

- **Verified by execution:** clean-checkout import failure, `create_app` failure,
  pytest collection (1,956 collect / 28 errors / 406 blocked test fns), cost-model
  surfaces, playbook routes, exit-precedence constants, broker order layer,
  session manager, metric set, plugin import hook.
- **Not verifiable in-repo:** Postgres row counts (external DB), the "~2,435
  passed" figure (only reproducible with the missing files), the Windows
  process-pool flake, real mStock API behaviour (credential-gated tests skip).
