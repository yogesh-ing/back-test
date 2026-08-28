# PRD V1 — Verification Pass (2026-08-28)

Audits `instructions/PRDandTASK_DECOMPOSITION.md` (37 tasks, 6 epics) **task by task** against
the code as built, and records the gap backlog (`G1…G14`) that `instructions/TASK-TRACKER.md`
now tracks. Companion doc: `instructions/ENGINEERING-NOTES.md`.

**Method** — every task was checked three ways: (1) read the named file, (2) exercised the
running app/API (`/home/user/.venv/bin/python`, `PYTHONPATH=src`), (3) ran the suite.

```
PYTHONPATH=src pytest tests/ -q            →  1813 passed, 4 skipped, 3 FAILED   (at the pass)
```

### Resolved after the pass (same day)

The verdicts below are kept as they were found. Three things have since changed:

| Gap | Outcome |
|---|---|
| **U1** logging | Done — `backtest/logging_config.py`, `--log-level`/`--log-file`, request ids tying a UI toast to a traceback, log lines on every previously-silent path. See `docs/LOGGING.md`. |
| **G5** red suite | Done — forward date contract settled (optional-but-reported), stale Node harness fixed and extended (11 tests), Postgres-driver tests skip with a reason instead of failing falsely. **Suite is green.** |
| **G3 + G4** forward stack | Done — the page's live equity chart / metric cards / progress / positions now render from the snapshot (and were *structurally* unable to before, because `_prefix_result` passed an empty metrics dict), the payload no longer leaks future signals, sessions are keyed by `state_id` in a bounded registry, and the replay advances on a **server-side clock** (`--replay-speed`, 0 = manual) so `/status` is a pure read. New: `GET /api/config`, `GET /api/forward/sessions`, shared `Money` formatter with `--currency` (₹ default). |
| **G1 + G2** metric bugs | Done at the root — new `engine/trades.py` is the single source of truth for both `compute_metrics` and `BacktestAdapter.to_trades()`, so the cards and the table cannot disagree; `win_rate` is realised P&L over **closed** trades, `num_trades` counts round trips (open position included, marked to the final close), and **G14** was superseded by the same change (no price reconstruction left). The adapter now asserts `Σ trade P&L == total P&L`. |

Everything else below is still open; the live list is the **Gap backlog** in
`instructions/TASK-TRACKER.md` (next up: **G3** — forward page live widgets).


The three failures are all real and are tracked below (G5):

| Failing test | Cause |
|---|---|
| `tests/test_api_forward.py::test_start_missing_dates_returns_400` | `/api/forward/start` was changed to default missing dates to `2020-01-01 … today` (`api/forward.py:325-330`) but the contract/test still says 400. **Reconcile one way or the other.** |
| `tests/test_broker_ui.py::test_forward_auth_gate_js_behaviour` | `startBot()` now requires a non-empty `#symbol` (`forward.js:194`) and the Node harness never sets one → the "enabled Start calls startBot" assertion fails. Harness is stale, not the gate. |
| `tests/test_db_manager.py::…unreachable_database_raises_connection_error` | Environment only: `psycopg2` not installed in this sandbox, so the error is `No module named 'psycopg2'`, not `unreachable`. Not a code defect. |

### Verdict legend

`✅ complete` · `🟡 built but incomplete / wrong values` · `❌ required by PRD, not implemented`

---

## Epic 1 — Foundation

| Task | Requirement (PRD) | Verdict | Evidence / gap |
|---|---|---|---|
| 1.1 | `BaseStrategy` contract: `name/description/version/author/params`, `generate_signals`, param-schema validation (type/min/max/default/label/tooltip), `validate()` → `StrategyContractError` | ✅ | `strategy/base.py:194-249` (`validate` + `_validate_param`), dual flat/schema form (`param_schema`, `default_params`). Deviation: extends the existing `Strategy`, signals take `(candles)` with params bound to instance attrs. Cosmetic: `_infer_schema` has a dead `if False else None` branch (`base.py:88`). |
| 1.2 | Auto-discovery via `pkgutil.iter_modules`, skip invalid with a logged warning, catalogue `get_all/get/get_params` | ✅ | `strategy/registry.py:26-41`. **Verified live**: dropped a good file + a syntax-broken file + a schema-invalid file into `strategies/`, app booted, good one appeared *without a restart*, invalid ones skipped with warnings. Minor inconsistency: `GET /api/strategies/<name>/params` does **not** run `validate()`, so a strategy hidden from the catalogue still answers `/params` with 200 (G13). |
| 1.3 | `GET /api/strategies`, `GET /api/strategies/<name>/params`, 404 unknown | ✅ | `api/strategies.py`. Verified: 4 entries sorted alphabetically, full `{name,description,version,author}`, params schema `{default,min,max,type,label,tooltip}`, `nope` → 404. |
| 1.4 | Adapter: `to_metrics/to_equity/to_drawdown/to_trades/to_signals/to_compare` — "correct shape **and values**" | 🟡 | All six methods present, shapes correct, JSON-native (`adapters/backtest_adapter.py`). Values are wrong in two places because they are lifted from `engine/metrics.py`: `total_trades` = position *transitions* not round trips (**G2**), `win_rate_pct` = sign of position not P&L (**G1**). `to_trades()` short P&L uses `entry/exit-1` and scales by equity, not notional → approximate trade rows (**G14**). |
| 1.5 | `POST /api/backtest/run` + errors for unknown strategy / bad dates / insufficient data | 🟡 | `api/backtest.py:120-175`. Verified: full payload; unknown strategy 400, `from>to` 400, short range 400 (`data error: synthetic range must be > 50 rows`). Gap: out-of-range params are silently accepted (`fast=9999` → 200 with a vacuous 0-trade result) — no server-side min/max check (**G11**). Smell: duplicate `"1D"` key in `_TIMEFRAME_TO_INTERVAL` (`backtest.py:30`) — G13. |
| 1.6 | `POST /api/backtest/run-many` with `ThreadPoolExecutor`, per-slot results keyed by id, failed slot isolated | ✅ | `api/backtest.py:177-251`. Verified: 4 slots → `{'1','2','3','4'}`, broken slot `{'error': …}` while 3 returned results, 0.17 s wall. >4 slots rejected. |

## Epic 2 — Backtest Page

| Task | Requirement | Verdict | Evidence / gap |
|---|---|---|---|
| 2.1 | `GET /backtest` route; passes available symbols to template | 🟡 | Route + `/` redirect exist (`web/app.py:80-87`). Symbols are **not** passed from the view; an inline script in `backtest.html:112-140` fetches `/api/symbols` **only when `SOURCE == "db"`**, and Compare has no equivalent (**G12**). |
| 2.2 | Template: two-column, config (strategy/symbol/TF/dates/capital), dynamic params, hidden results, metric cards, 3 chart tabs, trade table, 3 action buttons | ✅ | `web/templates/backtest.html` — all elements present. |
| 2.3 | JS: fetch strategies → dropdown; on change fetch params → typed inputs respecting min/max/default/label/tooltip; run → POST → spinner → render | ✅ | `backtest.js` + `components/params_form.js` (number/text/checkbox, `min`/`max`/`step` attrs, tooltips). Note: dropdown shows the raw id (`sma_crossover`) rather than a human name — acceptable deviation, `description` is unused in the UI. |
| 2.4 | Equity curve + grey buy&hold benchmark + tooltip + responsive, `renderEquityChart(id, data)` | ✅ | `charts/equity_chart.js`. |
| 2.5 | Drawdown area chart, **worst drawdown point annotated**, `renderDrawdownChart(id, data)` | 🟡 | `charts/drawdown_chart.js` — area + fill ✅, but the worst-DD annotation is **not implemented**: `worst_dd_pct`/`worst_dd_date` are returned by the adapter and only mentioned in the file's docstring (**G7**). The tracker's claim "Drawdown chart (+ worst-DD marker)" is therefore not true today. Also `y.reverse:true` on the single chart vs non-reversed compare chart → the same metric points two directions on two pages (**G7**). |
| 2.6 | Candlestick/line price chart, green up-arrow buys, **red down-arrow sells**, OHLC tooltip | 🟡 | `charts/signals_chart.js` — line + `triangle` buys ✅, sells use `rectRot` (diamond) not a down arrow, tooltip shows `Close` only, no OHLC (**G10**). |
| 2.7 | Metrics cards: P&L green/red, drawdown coloured by severity, reusable | ✅ | `components/metrics_cards.js` (`dd-sev-1/2/3` buckets). Values inherit G1/G2. |
| 2.8 | Paginated (20/page) sortable trade table, P&L coloured, ✅/❌ result | ✅ | `components/trade_table.js`. Caveat: `render()` resolves `#tradeTable-wrap`/`#pagination` by hardcoded id, so it is single-instance-per-page despite the `containerId` argument (G13). |
| 2.9 | Save to Compare → `sessionStorage`, max 4, warn when full, toast slot N/4, pre-fill Compare | ✅ | `backtest.js:88-93` + `SessionState.addCompareSlot` + `compare.js:234-241`. PRD §4.2 ("oldest dropped") contradicts Task 2.9 ("show warning"); the build follows Task 2.9 — deliberate. |
| 2.10 | `GET /api/backtest/export?session_id=X` returns trades CSV; frontend triggers download | 🟡 | Frontend export ✅ (client-side, `backtest.js:95-108`). **Server endpoint does not exist** (`grep export src/backtest/api/*.py` → nothing), and the CSV is trades-only (no metrics/config block) (**G9**). |

## Epic 3 — Compare Page

| Task | Requirement | Verdict | Evidence / gap |
|---|---|---|---|
| 3.1 | `GET /compare` | ✅ | `web/app.py:89-91`. |
| 3.2 | Shared config bar + 2-4 slot cards + Add/Run All + 3 result tabs + per-slot actions | ✅ | `templates/compare.html`. Symbol list hardcoded (`DEMO/BTCUSD/ETHUSD/NIFTY/INFY`) and different from Backtest's → G12. |
| 3.3 | Slot mgmt: start 2, add max 4 (button hidden at 4), remove min 1, per-slot strategy+params, sessionStorage pre-fill | ✅ | `compare.js:31-99,234-241`. Params are per-slot and independent. |
| 3.4 | Run All: validate, POST run-many, per-slot loading state, populate + highlight winners | ✅ | `compare.js:135-186`; per-slot ✓/⚠ status line. |
| 3.5 | N-column metrics table, best-per-row green + 🏆 | 🟡 | `compare/metrics_table.js` — rows/labels/🏆 ✅. The "Win Rate" row is ranked on G1's bogus number, so that trophy is currently meaningless; "Total Trades" deliberately unhighlighted. |
| 3.6 | Overlaid equity, fixed palette, legend = label + final return, **tooltip shows all slot values at same date** | 🟡 | `compare/equity_compare_chart.js` — palette/union-date alignment/legend ✅. Tooltip callback returns `` `${item.dataset.label}` `` only — **no value at the hovered date** (**G8**). |
| 3.7 | Overlaid drawdown, same colours, **worst point per line annotated** | 🟡 | `compare/drawdown_compare_chart.js` — same palette ✅; worst-DD only inside the legend string, nothing annotated on the chart (G7); `fill:false` vs `fill:true` on the single-page chart (G7). |
| 3.8 | Per-slot "Open in Backtest" / "Promote to Forward" via sessionStorage + navigate | ✅ | `metrics_table.js` actions row → `compare.js:193-204`. |

## Epic 4 — Forward Test Page

| Task | Requirement | Verdict | Evidence / gap |
|---|---|---|---|
| 4.1 | On load read `forward_prefill` → pre-fill strategy, params, symbol; clear after read; banner | 🟡 | `forward.js:315-341`. Works for strategy/symbol/capital/params + banner + clear ✅. Two defects: **date range is never applied**, so a promoted run silently replays 2024-01-01→(template default 2026-08-28); and the forwarded `timeframe` (`"1D"`) has no matching `<option>` in forward's select (`1min`/`day`), so it silently falls back (G6/G11). |
| 4.2 | Consistent nav, config matching Backtest, dynamic params, **live equity chart**, **live metrics cards**, trade feed, Start/Stop, status indicator | ❌ | `templates/forward.html` + `forward.js`. Nav/config/params/status ✅. Three required widgets are broken or never wired: (1) live equity chart — `fetchEquity()` passes a **bare array** (`data.map(d => d.equity)`) into `renderEquityChart(id, {dates,values})`, which early-returns → **the chart never draws**, even though `/api/forward/status` already returns a ready `equity` object; (2) `#metricsCards` div + `metrics_cards.js` are loaded but `renderMetricsCards()` is **never called** → no running return/drawdown/win-rate; (3) `#progressText` exists but is never updated → no replay progress. Also `₹` hard-coded here vs `$` on Backtest/Compare, positions always show `entry == current` / `0%` unrealized, and the stop toast uses an unstyled `"info"` type (**G3**). |
| 4.3 | `POST start` / `POST stop` / `GET status` (`{status,metrics,equity,positions,trades}`) with adapter-shaped payload so components are reusable | 🟡 | `api/forward.py` — all three plus `/trades` and `/equity`, shape matches the adapter ✅ (re-verified live). Defects: `_signals_upto()`'s filter `if b in self.signals["buys"]` is a tautology, so the payload **advertises future signals** (at `revealed=12`, buys for `2024-03-29` and `2024-09-03` are already listed) → violates the forward no-lookahead invariant (**G4**); `state_id` is always `None` with a single process-global `_SESSION`, so `/stop` ignores the id and every poll advances the shared cursor (two tabs double-advance); state is in-memory only → survives refresh (✅ PRD reliability) but not a server restart (V2 #3). Plus the date-default contract break (G5). |

## Epic 5 — Cross-Page

| Task | Requirement | Verdict | Evidence / gap |
|---|---|---|---|
| 5.1 | Base template: shared nav (Dashboard/Backtest/Compare/Forward), active highlight, consistent header/footer/CSS | 🟡 | `templates/base.html` + `app.css:53-56,320`. Nav/active ✅ via `body[data-active]`; **`/data` has no highlight rule** (nav item added later than the CSS) and there is **no footer** at all (G13). |
| 5.2 | sessionStorage wrapper, 3 keys, max-4 enforcement, expiry on close | ✅ | `js/session_state.js`. |
| 5.3 | `showToast(msg, success|warning|error)`, 3 s auto-dismiss, stacks | ✅ | `components/toast.js`; `.toast.info` used by `forward.js:240` has no style (G13). |
| 5.4 | `showLoader(containerId, msg)` / `hideLoader(containerId)` | 🟡 | `components/loader.js` — `showLoader` used by both Run flows; `hideLoader` is **never called anywhere** (results/`innerHTML` are overwritten ad hoc instead), so half the component's API is dead (**G13**). |

## Epic 6 — Integration & Testing

| Task | Requirement | Verdict | Evidence |
|---|---|---|---|
| 6.1 | Contract + discovery tests (7 bullets) | ✅ | `test_strategy_base.py` — 16 tests covering every listed bullet. |
| 6.2 | Adapter tests | 🟡 | `test_backtest_adapter.py` — 9 tests, **shapes only**. No test pins `to_trades()` row count/`pnl` against `metrics.num_trades`/`win_rate` → that is exactly how G1/G2 escaped. Needs value-level assertions. |
| 6.3 | Backtest API tests (run/run-many/error cases) | ✅ | `test_api_backtest.py` — 8 tests, all listed cases. |
| 6.4 | Strategy API tests | ✅ | `test_api_strategies.py` — 4 tests incl. 404 + alphabetical order. |
| 6.5 | E2E: load→run→adapt, 4-slot parallel + winner, promote→forward | ✅ | `test_e2e_workflow.py` — 5 tests with a stub broker. |
| 6.6 | Migrate all existing strategies (metadata + schema + registry presence) | ✅ | All 4 in `strategies/` carry `description/version/author` + full schemas; `test_migrated_strategies_have_full_metadata` / `…_validate_and_run` assert it. |

**Also failing today:** the three regressions listed at the top (G5). No JS harness exists for
Backtest/Compare/session-state (only the three broker-auth ones under `tests/js/`).

---

## PRD §1.3 success criteria — where we actually stand

| # | Criterion | Verdict |
|---|---|---|
| 1 | Run a backtest and see results in the dashboard | 🟡 Dashboard shows strategy count + **live forward bot** only; there are no backtest results/history (needs V2 #3 persistence). |
| 2 | Compare up to 4 combos side by side | 🟡 Works, but under `--source synthetic` (the only credential-free source) every timeframe returns identical bars — slot 1 (`1D`) and slot 2 (`1H`) both returned 262 bars / −2.40% in the live check → "across timeframes" is cosmetic today (**G6**). |
| 3 | Promote a winning backtest to forward test | 🟡 Promote + prefill works, but date range is dropped and the timeframe doesn't exist in forward's `<select>` (G11). |
| 4 | Drop a strategy `.py` → UI auto-populates | ✅ Verified live. |
| 5 | Forward page shows live updates from a running bot | ✅ **Fixed after the pass** (G3+G4) — see the table above. |
| 6 | All pages share one registry + data source | 🟡 One registry ✅ (`/api/strategies` on all pages); symbol lists are per-page hardcodes (G12). |

---

> Rows marked 🟡/❌ above for Epic 4 (4.2, 4.3), Epic 1 (1.4) and Epic 3 (3.5) were
> **fixed later the same day** — see "Resolved after the pass". 4.1's promote-dates half and
> everything else is still open in the tracker's Gap backlog.

## Gap backlog (now tracked in `TASK-TRACKER.md`)

One line each here; full acceptance criteria live in the tracker's **Gap backlog** table.

| ID | P | Gap | Files |
|---|---|---|---|
| G1 | P0 | `win_rate` computed from position **sign**, not trade P&L → 100% for every long-only run (rsi_reversion: 2 trades, 0 winners, card says 100%) | `engine/metrics.py:36-47` |
| G2 | P0 | `num_trades` counts entry+exit **transitions**; trade table counts round trips (4 vs 2 for the same run) | `engine/metrics.py:28-34`, `adapters/backtest_adapter.py:60` |
| G3 | P0 | Forward page: equity chart never renders (array vs `{dates,values}`), `renderMetricsCards` never called, `#progressText` never updated | `web/static/js/forward.js:165-176,186-190`, `web/templates/forward.html:58,66` |
| G4 | P1 | `/api/forward/status` leaks future buys/sells (`_signals_upto` tautological filter) — breaks forward no-lookahead; single global session + `state_id=None` (poll advances shared cursor) | `api/forward.py:232-240,246-300` |
| G5 | P1 | 3 failing tests: forward date-range contract, stale Node harness (no `symbol`), missing `psycopg2` in the dev env | `api/forward.py:325-330`, `tests/js/test_forward_auth_gate.mjs`, `requirements.txt` |
| G6 | P1 | Timeframe is a no-op on Synthetic/CSV and unvalidated for mStock (`4hour` passed through); forward `<select>` values (`1min`/`day`) don't line up with backtest's (`1D/1H/4H/1W`) | `data/synthetic.py`, `data/csv_source.py`, `api/backtest.py:29-35`, `web/templates/forward.html:38-41` |
| G7 | P2 | Worst-drawdown annotation missing on both drawdown charts; drawdown axis direction/fill inconsistent between Backtest and Compare | `charts/drawdown_chart.js`, `compare/drawdown_compare_chart.js` |
| G8 | P2 | Compare equity + drawdown tooltips show the series label only — no value at the hovered date | `compare/equity_compare_chart.js`, `compare/drawdown_compare_chart.js` |
| G9 | P2 | Task 2.10 server export (`GET /api/backtest/export`) missing; CSV is trades-only | `api/backtest.py`, `web/static/js/backtest.js:95-108` |
| G10 | P2 | Sell markers are diamonds not red down-arrows; signals tooltip lacks OHLC | `charts/signals_chart.js` |
| G11 | P2 | No server-side param min/max validation (`fast=9999` → 200); promote drops `from_date/to_date` | `api/backtest.py`, `web/static/js/forward.js:315-341` |
| G12 | P2 | Symbol lists hardcoded per page; Compare never calls `/api/symbols`; only Backtest/Forward know about the DB | `web/templates/{backtest,compare,forward}.html` |
| G13 | P3 | Polish: `/data` nav never highlighted, no footer, `.toast.info` unstyled, `hideLoader` dead code, duplicate `"1D"` key, dead `if False` branch, `get_params` skips `validate()`, `TradeTable` hardcoded ids | various |
| G14 | P3 | Adapter trade P&L is reconstructed (short uses `entry/exit-1`, scaled by equity) — should come from the engine's actual fills/turnover | `adapters/backtest_adapter.py:120-180` |

## How to reproduce the checks

```bash
# venv used for this pass: /home/user/.venv (python3.11 + pandas/flask/pytest/sqlalchemy/pyyaml)
cd /home/user/back-test
PYTHONPATH=src pytest tests/ -q                                   # 1813 passed / 4 skipped / 3 failed
PYTHONPATH=src python -m pytest tests/test_api_forward.py -q       # the date-range break

# metrics evidence for G1/G2 (win_rate 100% with 0 winning rows, 4 trades vs 2 rows)
PYTHONPATH=src python - <<'PY'
from backtest.web.app import create_app
c = create_app(source="synthetic").test_client()
j = c.post("/api/backtest/run", json={"strategy": "rsi_reversion", "symbol": "DEMO",
        "timeframe": "1D", "from_date": "2024-01-01", "to_date": "2024-12-31",
        "capital": 10000, "params": {}}).get_json()
print(j["metrics"]["win_rate_pct"], j["metrics"]["total_trades"], len(j["trades"]),
      [t["result"] for t in j["trades"]])
PY
```
