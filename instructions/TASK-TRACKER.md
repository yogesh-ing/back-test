# Forward Testing Simulator — Task Tracker

Tracks progress against `instructions/forword-testing.md` (24 steps, 8 phases).

> **Debugging?** See `instructions/ENGINEERING-NOTES.md` — symptom→cause playbook,
> conventions and invariants, and every bug found so far with its root cause.

**Last updated:** 2026-08-28 · **Branch:** `arena/01a0478e-back-test` (PRD verification pass) /
`arena/01a03e5a-back-test` (broker auth effort) / `arena/01a03438-back-test` (PRD effort) /
`arena/01a02caa-back-test` (simulator) · **Simulator Steps 1–20 complete · PRD Epics 1–6 built,
verification pass done → 14 gaps open (see Gap backlog) · Broker Auth Epic COMPLETE (all 5 phases,
13/13 tasks)**

> **Suite today: 1893 passed / 4 skipped / 0 failed** — G5, G1/G2, G3 and G4 are done.
>
> **2026-08-28 — PRD V1 was audited task by task.** Full evidence, per-task verdicts and repro
> commands: **`instructions/PRD-VERIFICATION-2026-08-28.md`**. Headline (pre-G5): the UI layer is
> built and wired end to end, **the suite was red (3 failures — now green)** and **PRD criterion #5 (forward page live
> updates) is not actually working** — the live equity chart never renders and the metric cards are
> never called. Fix **G1–G3** before anything else.
>
> **2026-08-28 later: G5 then G1+G2 landed — suite GREEN at 1875 passed / 4 skipped / 0 failed.**
> G1/G2 are fixed at the root behind one trade-accounting module (`engine/trades.py`);
> **next in order is G3** — the forward page's live widgets.
>
> **Also 2026-08-28: U1 (logging) landed** — the app is now debuggable (`--log-level DEBUG`,
> request ids tying a UI toast to a traceback, `docs/LOGGING.md`), which is what the G1/G2/G3
> fixes will be worked against. Suite: **1849 passed / 4 skipped / 3 failed** (the 3 are G5).

---

## Unified Trading Bot Platform (PRD — active effort)

Tracks progress against `instructions/PRDandTASK_DECOMPOSITION.md` — the unified
web UI (Dashboard / Backtest / Compare / Forward) layered on top of the mature
simulator + backtester (this tracker's Steps 1–24). Active branch:
`arena/01a0478e-back-test` (was `arena/01a03438-back-test`).

> **V1 is built (Epics 1–6 + Dashboard) — verified task by task on 2026-08-28, 14 gaps open.**
> Pick up in the **Gap backlog** below, then the V2 list.

### 🔧 Gap backlog — PRD verification findings (implement one at a time)

Source: `instructions/PRD-VERIFICATION-2026-08-28.md`. Work top-down; a task is only **Done** when
the listed acceptance check passes and the full suite is green. `P0` = correctness/user-visible
break, `P1` = contract/breaking, `P2` = PRD requirement partially met, `P3` = polish.

Legend: ✅ done · 🟡 in progress · ⬜ not started

> **Status after 2026-08-28: 6 of 14 closed** — `U1` (logging), `G5` (green suite), `G1`+`G2`
> (trade accounting), `G3`+`G4` (forward page + forward API), `G14` (superseded by G1/G2).
> **Open: G6, G7, G8, G9, G10, G11, G12, G13** — and `U2` (market data simulator), which G6 waits on.
> Next recommended: plan `U2`, then `G11` + `G7`–`G10` as quick visible wins.

**P0 — numbers are wrong / widgets don't render**

| ID | Task | Files | Acceptance (how we prove it) | Status |
|---|---|---|---|---|
| G1 | **Fix `win_rate`:** it counted the *sign* of the position, not P&L. ✅ Done 2026-08-28 (with G2): realised P&L per trade comes from `engine/trades.py::walk_trades` (equity-based, costs included), and `win_rate = wins / closed trades`. Semantics chosen for this fix: a position still open at the last bar **counts in `num_trades`** (marked to the final close) but is **excluded from `win_rate`**; P&L of exactly 0 is `Flat`, not a win. UI honesty: the Win-Rate card renders an em-dash + "nothing closed yet" instead of a fake 0.00%, trade rows read ⏳ Open, and Compare no longer crowns a slot that has nothing closed. Redefined in place (no compat aliases) with every consumer updated: `compute_metrics`, `BacktestAdapter`, `analysis/comparison.py`, forward `/status`, cards, compare table. | `src/backtest/engine/trades.py` (new), `engine/metrics.py`, `adapters/backtest_adapter.py`, `analysis/comparison.py`, `api/forward.py`, `web/static/js/components/metrics_cards.js`, `components/trade_table.js`, `compare/metrics_table.js`, `web/static/css/app.css` | `tests/test_engine_trades.py` (14) pins a losing long-only run at `win_rate == 0.0`, the flip-tiling rule and card/table agreement for all 4 strategies; `tests/js/test_metrics_cards.mjs` (9) pins the em-dash / Open rendering | ✅ **Done** (2026-08-28) |
| G2 | **Fix `num_trades`:** counted entry *and* exit transitions (4) while the table showed round trips (2). ✅ Done 2026-08-28 with G1 — the cards and the table share one trade walk, so they cannot diverge by construction. `closed_trades` / `open_trades` / `winning_trades` / `losing_trades` / `realised_pnl` / avg-best-worst are exposed alongside. The `[adapter] card/table disagree` tripwire added with U1 was replaced by a stronger invariant: **Σ trade P&L == total P&L**, warned on if it ever breaks. | same files as G1 | `metrics.total_trades == len(trades)` for every built-in strategy (asserted in `test_engine_trades.py` and `test_backtest_adapter.py`); Σ row pnl == `total_pnl` within $1 on a 100k run | ✅ **Done** (2026-08-28) |
| G3 | **Forward page live updates.** ✅ Done 2026-08-28: (a) `renderLiveEquity()` now feeds the adapter-shaped `equity` object from `/status` into `renderEquityChart` (it had been passed a bare number array, which the chart rejects — so it never drew), and the redundant `/api/forward/equity` round-trip is gone; (b) `renderMetricsCards("metricsCards", data.metrics)` is now actually called; (c) `#progressText` + a `#progressFill` bar track `progress.revealed/total/pct`; (d) positions carry real marks — entry vs current, move %, unrealised P&L (₹-grouped), bars held — with the API computing them from the open trade instead of welding entry to current; (e) the trade feed reuses the shared `TradeTable` (pagination + ⏳ Open); (f) one `Money` formatter for the whole app via `--currency`/`BACKTEST_CURRENCY` (₹ default, `en-IN` grouping), replacing the `$`-vs-`₹` split; (g) the page re-attaches to its own `state_id` after a refresh (sessionStorage) and a new **replay speed** picker sends `bars_per_second`. Deeper cause also fixed: `_prefix_result` built a result with an empty `metrics` dict, so every live metric read 0.0 regardless of performance. | `web/static/js/forward.js`, `components/currency.js` (new), `components/metrics_cards.js`, `components/trade_table.js`, `js/dashboard.js`, `web/templates/{base,forward}.html`, `api/forward.py`, `web/app.py` | `node tests/js/test_forward_widgets.mjs` → 7 tests (chart receives `{dates,values,benchmark}`, cards get the metrics, progress text/bar, marks, no extra fetch, idle snapshot safe); `tests/js/test_metrics_cards.mjs` → 10 incl. the ₹ formatter; API-side: `test_metrics_are_real_numbers_not_zeros`, `test_positions_are_marked_to_market_as_the_replay_runs`, `test_equity_payload_is_component_compatible` | ✅ **Done** (2026-08-28) |

**P1 — broken contracts / red suite**

| ID | Task | Files | Acceptance | Status |
|---|---|---|---|---|
| G4 | **Forward replay: no future signals, real sessions, server clock.** ✅ Done 2026-08-28: `_signals_upto()` now cuts candles/buys/sells at the revealed bar's **date** (the old `if b in self.signals["buys"]` filter was always true and then sliced by count, so a replay at 4% progress listed entries from month 8); sessions live in a bounded `OrderedDict` keyed by `state_id` (uuid) with `_ACTIVE_ID` fallback so no-id callers keep working and an unknown id is a 404 rather than a lie; `/stop` honours the id; **and the clock moved to the server** as a per-session daemon thread (`bars_per_second`, default `--replay-speed` 1.0, clamped 0–5000, `0` = manual via `session.advance(bars)`), so `/status` is a pure read — two tabs can no longer double-advance one run and closing the browser doesn't freeze the bot. `GET /api/forward/sessions` lists them. | `src/backtest/api/forward.py` | `test_polling_never_advances_the_clock`, `test_clock_advances_without_any_polling`, `test_signals_never_leak_beyond_the_revealed_prefix`, `test_two_sessions_are_independent`, `test_unknown_state_id_is_404`, `test_sessions_endpoint_lists_replays`, `test_negative_speed_freezes_the_clock` | ✅ **Done** (2026-08-28) |
| G5 | **Get the suite green.** ✅ Done 2026-08-28: (1) forward date contract settled — dates stay **optional** (PRD 4.3 defines `{strategy, symbol, params}`) but every filled-in value is reported as `defaults_applied` + echoed in `config`, malformed/inverted ranges now 400 instead of failing deep in the data source, and `forward.js` warns in the UI when the server defaulted the window; (2) the stale Node harness now sets the required `#symbol` and was extended to pin the payload contract (symbol upper-cased, `mode`), the missing-symbol block, synthetic-mode start and the defaulted-window toast → 11 tests, with a complete-enough stub DOM so `renderLive()` runs; (3) the psycopg2-dependent db-manager tests skip with a reason instead of reporting a false failure. | `api/forward.py`, `web/static/js/forward.js`, `tests/test_api_forward.py`, `tests/js/test_forward_auth_gate.mjs`, `tests/test_db_manager.py`, `docs/FORWARD-TESTING.md` | `PYTHONPATH=src pytest tests/ -q` → **1859 passed / 4 skipped / 0 failed** (was 3 failed); +10 date-contract tests, +3 harness tests; `node tests/js/*.mjs` → 11 / 14 / 12 passing. | ✅ **Done** (2026-08-28) |
| G6 | **Make the timeframe control real.** ⏳ **Blocked on U2 Phase 0–1** (planned; see `instructions/MARKET-SIMULATOR-PRD.md`). Until it lands the interim stays: sources log a WARNING when they ignore `interval`. Original scope:  Synthetic and CSV sources ignore `interval`, so Compare's "across timeframes" premise is cosmetic (slot `1D` and slot `1H` returned identical 262 bars / −2.40%). Options: resample synthetic/CSV bars to the requested interval, or hide/disable the TF selector when the source can't honour it; and validate the mStock interval (`4hour` is passed through verbatim today). Then align forward's `<select>` values (`1min`/`day`) with backtest's (`1D/1H/4H/1W`) and share one `_TIMEFRAME_TO_INTERVAL`. | `data/synthetic.py`, `data/csv_source.py`, `api/backtest.py:29-47`, `api/forward.py:44-58`, `web/templates/forward.html:38-41` | `1D` vs `1H` on the same symbol/range produce different bar counts; unknown TF → 400 (not silent `day`); one shared interval map for backtest+forward. | ⬜ |

**P2 — PRD requirements only partially met**

| ID | Task | Files | Acceptance | Status |
|---|---|---|---|---|
| G7 | Drawdown charts (PRD 2.5 / 3.7): annotate the worst-drawdown point (data already ships as `worst_dd_pct`/`worst_dd_date`) and make axis direction + `fill` consistent between the single and compare charts. | `web/static/js/charts/drawdown_chart.js`, `web/static/js/compare/drawdown_compare_chart.js` | A labelled marker at the min point on both charts; both plot drawdowns in the same direction. | ⬜ |
| G8 | Compare tooltips (PRD 3.6/3.7) show only the series label — no value at the hovered date. Show `label: value` for every series (`mode:'index'` already returns them). | `web/static/js/compare/equity_compare_chart.js`, `compare/drawdown_compare_chart.js` | Hovering date X lists all N slots with their $/% values. | ⬜ |
| G9 | Export (PRD 2.10): add the server endpoint `GET /api/backtest/export` (trades + a metrics/config header block) or formally retire it as a V2 item; keep the client-side download as a fallback. | `api/backtest.py`, `web/static/js/backtest.js:95-108` | `curl` on the endpoint returns `text/csv` with the same rows the table shows, including a config header. | ⬜ |
| G10 | Signals chart (PRD 2.6): red **down**-arrow for sells (`rectRot` today) and an OHLC line in the tooltip. | `web/static/js/charts/signals_chart.js` | Marker glyphs match the PRD mock; tooltip shows O/H/L/C + signal. | ⬜ |
| G11 | Validate params server-side against the declared schema (out-of-range `fast=9999` today returns a vacuous 200 → 400 with the offending param, min and max) — and make **Promote** carry `from_date`/`to_date` (forward currently ignores them and keeps its stale template default `to=2026-08-28`). | `api/backtest.py` (`run` + `run-many`), `web/static/js/forward.js:315-341`, `web/templates/forward.html:52-53` | Out-of-range → 400 from both endpoints; a promoted run replays the promoted window exactly. | ⬜ |
| G12 | One symbol source for every page: Backtest/Compare hold two different hardcoded lists and only Backtest knows about the DB. Fetch `/api/symbols` on all three pages (or pass it from the view, as PRD 2.1 literally says) and share one `<datalist>` component. | `web/templates/{backtest,compare,forward}.html`, `api/symbols.py` | Backtest, Compare and Forward offer the identical symbol list for `--source db` *and* `synthetic`. | ⬜ |

**P3 — polish / hygiene**

| ID | Task | Files | Acceptance | Status |
|---|---|---|---|---|
| G13 | Small inconsistencies: `/data` nav item never highlighted (no CSS rule) and no footer in `base.html` (PRD 5.1); `.toast.info` used but unstyled; `hideLoader()` never called (loader cleared ad hoc); duplicate `"1D"` key in `_TIMEFRAME_TO_INTERVAL`; dead `if False else None` in `_infer_schema`; `GET /api/strategies/<name>/params` bypasses `validate()` so a catalogue-skipped strategy still answers 200; ~~`TradeTable.render(containerId, …)` hard-codes `#tradeTable-wrap`/`#pagination`~~ ✅ fixed alongside G1/G2: per-container state, pinned by `tests/test_web_components.py`. | `web/static/css/app.css:53-56`, `web/templates/base.html`, `web/static/js/{components/loader.js,components/toast.js,components/trade_table.js,forward.js}`, `strategy/base.py:88`, `strategy/registry.py:92-99`, `api/strategies.py:27-34`, `api/backtest.py:30` | Each page (incl. `/data`, `/portfolio`) highlights its nav item; `hideLoader` called or deleted; `/params` 404s for a strategy hidden from the catalogue; no duplicate dict keys (`flake8 F601` clean). | ⬜ |
| G14 | Trade rows were reconstructed from prices (short P&L used `entry/exit-1`, scaled by equity). ✅ **Superseded by G1/G2** (2026-08-28): the adapter no longer reconstructs anything — `to_trades()` returns the same `walk_trades` rows the metrics use, P&L is equity-based, and `Σpnl == total_pnl` is asserted. | `adapters/backtest_adapter.py`, `engine/trades.py` | closed by the G1/G2 commit | ✅ **Done** (superseded) |

**User-requested items (added 2026-08-28 alongside the verification gaps)**

| ID | Task | Files | Acceptance | Status |
|---|---|---|---|---|
| U1 | **Make the app debuggable — logging.** "The logging is not right, doesn't show anything, so I can't debug myself." → one shared logging setup for web + CLI + engine; INFO/DEBUG levels from flag or env; request ids that tie a UI error toast to a server traceback; log lines everywhere the app used to fail silently (no signals, interval ignored, params out of range, forward replay, swallowed `except: pass` in the data fetcher). | `src/backtest/logging_config.py` (new), `web/app.py`, `api/{backtest,forward,strategies,symbols,broker_auth,data_manager,portfolio}.py`, `runner.py`, `engine/backtester.py`, `adapters/backtest_adapter.py`, `data/{synthetic,csv_source,db_source}.py`, `strategy/registry.py`, `forward/engine.py`, `cli.py`, 3 JS `fetchJSON`s, `docs/LOGGING.md` | `--log-level DEBUG` shows the path of one `/api/backtest/run`; every response has `X-Request-Id` and every `/api` error body has `request_id`; grep that id in `--log-file` output and you get the traceback; 36 tests in `tests/test_logging_config.py` (incl. a guard against re-introducing silent `except: pass`). | ✅ **Done** (2026-08-28) |
| U2 | **Market data simulator.** 📄 **Planned 2026-08-29 — `instructions/MARKET-SIMULATOR-PRD.md` (21 tasks / 6 phases), awaiting your go on the 5 open questions (§8).** Core proposal: generate **1-min atomic bars** and resample up, so every interval the UI offers is honoured (this is what closes `G6`); scenarios are **constructed then verified** against the strategy's own signals so a trade is *guaranteed* — including the user's requirement that a stop/target fires within 1–2 minutes of entry, which needs **two-pass** forcing (place the spike relative to the measured entry price, not a guessed bar — the one-pass version was tried and silently did nothing). Determinism via seed+digest manifest; NSE-session-anchored timestamps with a `continuous` escape hatch; optional `persist_to_db` so the real `DbSource` path is exercisable without credentials. Phase 0–1 (shared timeframe map + resample + `--source simulator`) ships value on its own. | `src/backtest/data/simulator/*` (new), `marketdata/timeframes.py`, `runner.py`, `api/scenarios.py`, `config/scenarios.yaml`, `web/*` picker | see the PRD §6 acceptance criteria — incl. the measured prototype result that one walk gives `1D/1H/15M/1min` → +355% / +332% / +230% / **−48%** where today all four are identical | ⬜ **Planned — needs approval** |

**Suggested order:** `U1` ✅ → `G5` ✅ → `G1`+`G2` ✅ → `G3`+`G4` ✅ →
`G6` → `G11` → `G7`–`G10`, `G12`–`G14` as polish; `U2` gets its own planning pass (it also
satisfies G6). Commit per gap, referencing the ID.

**Decisions taken 2026-08-28 (so they are not relitigated)**

| Topic | Decision |
|---|---|
| G1/G2 fix location | **Full solution at the root:** fix `engine/metrics.py` (realised round-trip P&L → `win_rate`; round-trip counting → `num_trades`) *and* have `BacktestAdapter` consume it, so CLI, API, Compare and Forward all agree. No adapter-only patch, nothing skipped — including updating `test_backtest.py`/`test_backtest_adapter.py` to pin the values. The `[adapter] … card/table disagree (gap G1/G2)` WARNING added with U1 is the tripwire that proves the fix landed; delete it once they agree. |
| G6 (timeframe) | Don't just gate it — the real answer is **U2**, the market data simulator. Until U2 lands, the sources log a WARNING when they ignore `interval`, so nobody reads a flat comparison as a result. |
| Start order | Suite-green (`G5`) before behaviour changes; `U1` (logging) came first so every later fix is observable. |

---

### 🟡 Pending / Next session (V2 backlog)


**Housekeeping (do first)**
1. **Commit & open a PR** — Epic 1–6 + Dashboard are built but **not yet committed/pushed**. Suggested target: `feature/UI-rediness`.
2. **Run it:** `PYTHONPATH=src python -m backtest.web.app --host 0.0.0.0 --port 5000 --source synthetic --log-level INFO` (see `docs/LOGGING.md`). Data is **synthetic only** — swap `--source csv|mstock|db` when real data is wired.
   - Useful switches: `--log-level DEBUG`, `--log-file logs/app.log`, `--currency INR|USD|…`,
     `--replay-speed 5` (forward replay bars/second).
   - Sandbox note: `/home/user/.venv` is **not** persisted between sessions; rebuild it in one line:
     `python3 -m venv /home/user/.venv && /home/user/.venv/bin/pip install -q -r requirements.txt pytest flake8`
     (`requirements.txt` already pins `psycopg2-binary`, needed for the two Postgres-driver tests — without it they skip, by design since G5).
   - Tests: `PYTHONPATH=src FORWARD_TEST_DB_URL="sqlite:///:memory:" /home/user/.venv/bin/python -m pytest tests/ -q`
     plus `node tests/js/*.mjs` (54 assertions across 5 harnesses).

   **Sandbox recovery (seen twice).** The sandbox can reset mid-session: the venv disappears
   *and* the branch pointer rolls back to the session's base commit while the working tree keeps
   the newer files. Nothing is lost **if you pushed** — every unit ends with
   `git push origin arena/01a0478e-back-test`. To recover:
   `git fetch origin <branch> && git diff --stat FETCH_HEAD` (untracked-but-present new files show
   as deletions there; verify a couple with `git show FETCH_HEAD:<path> | diff - <path>`) then
   `git reset --hard FETCH_HEAD`. That is exactly how the G1/G2 and G3/G4 commits were restored.

**V2 — product (PRD §7)**
3. **Persistence layer** — store run history / saved comparisons / forward state to DB (`forward/engine.py` + `db/` exist). Unblocks refresh-survives-restart, save/load compare configs, Dashboard history.
4. **Live Forward feed** — replace the replay with the real mStock polling loop (needs credentials; `live/mstock.py` exists).
5. **Compare: month-on-month returns heatmap** per slot.
6. **Export** — PDF + comparison export (client-side CSV done).
7. **Save/load comparison configurations.**
8. **Parameter sensitivity analysis** — sweep params, heatmap.

**V2 — platform**
9. **Full Dashboard** — recent backtests, saved comparisons, multi-bot, system health over time (depends on #3).
10. **Production WSGI** — gunicorn instead of Flask dev server.
11. **Retire/merge the legacy Step-19 `dashboard/app.py`** now that `/forward` exists.

**Known limitations (revisit)**
- Synthetic data only; no real market data wired into the UI yet.
- Forward test is a deterministic replay, not a live market feed.
- In-memory session state — lost on server restart (fix via #3).

`████████████████████████████████████████░░` **Epics 1–6 complete (PRD V1 = 100%)**

| Epic | Theme | Tasks | Status | Verification (2026-08-28) |
|---|---|---|---|---|
| 1 · Foundation | BaseStrategy contract, registry, BacktestAdapter, REST APIs | 1.1–1.6 | ✅ Complete (1.5 🟡) | **G1+G2 fixed** → 1.4 verified clean; G11 (params → 400) still open |
| 2 · Backtest Page | Route, template, dynamic params, charts, trade table | 2.1–2.10 | ✅ Complete (2.1/2.5/2.6/2.10 🟡) | 6/10 fully verified → **G7, G9, G10, G12, G13** |
| 3 · Compare Page | Slots, Run All, overlaid charts, per-slot actions | 3.1–3.8 | ✅ Complete (3.6/3.7 🟡) | **G6, G7, G8** open; the Win-Rate 🏆 is honest now (G1 fixed) |
| 4 · Forward Page | Pre-fill, template cleanup, forward API alignment | 4.1–4.3 | ✅ Complete (4.1 🟡) | **G3 + G4 fixed** → live widgets and payload verified; G11's promote-date piece still open |
| 5 · Cross-Page | Shared nav, session state, toast, loader | 5.1–5.4 | ✅ (5.1/5.4 🟡) | **G13** |
| 6 · Testing | Contract/adapter/API/e2e tests, strategy migration | 6.1–6.6 | ✅ Complete | **G5 done → suite green**; 6.2 now asserts values, not just shapes (`tests/test_engine_trades.py`) |

### Epic 1 — Foundation ✅ (2026-08-24)

Non-breaking layer over existing `strategy/` + `engine/`. All 1571 pre-existing
tests still pass; 35 new PRD tests added (6.1–6.4). One unrelated pre-existing
failure (`test_mstock_auth::test_login_sends_sdk_headers`, needs `MSTOCK_API_KEY`).

| # | Task | Deliverable |
|---|------|-------------|
| 1.1 | BaseStrategy contract | `strategy/base.py` — dual params form (flat + schema), `description/version/author`, `validate()` + `StrategyContractError` |
| 1.2 | Strategy auto-discovery | `strategy/registry.py` — `get_all()` / `get_params()`, skip-invalid-with-warning |
| 1.3 | Strategy API | `api/strategies.py` — `GET /api/strategies`, `GET /api/strategies/<name>/params` |
| 1.4 | BacktestAdapter | `adapters/backtest_adapter.py` — metrics/equity/drawdown/trades/signals/compare |
| 1.5 | Backtest API | `api/backtest.py` — `POST /api/backtest/run` |
| 1.6 | Parallel backtest API | `api/backtest.py` — `POST /api/backtest/run-many` (ThreadPoolExecutor) |

**New packages:** `src/backtest/adapters/`, `src/backtest/api/`, `src/backtest/web/`
(unified app factory `create_app()`; live preview on port 5000).

**Deviations from the PRD:**

| # | PRD says | We do | Why |
|---|----------|-------|-----|
| 1 | Top-level `api/`, `adapters/`, `dashboard/` paths | `src/backtest/api/`, `adapters/`, `web/` | Repo nests under `src/backtest/` |
| 2 | New `BaseStrategy` class | Extend existing `Strategy` | Avoid duplicating a working abstraction (matches existing simulator deviation #5) |
| 3 | `generate_signals(candles, params)` | `generate_signals(candles)` (params bound to instance attrs) | Existing contract; schema still drives dynamic UI forms |
| 4 | `params` = schema dict only | Accept flat (legacy) **and** schema forms | Zero regressions on the 4 existing strategies |
| 5 | `POST /api/forward/start` body is `{strategy, symbol, params}` | Dates accepted but **optional**; missing ones are defaulted (`2020-01-01 → today`) and reported as `defaults_applied` | Keeps the PRD contract, and "start from what we have" is the point of forward testing. Silent defaulting was the actual bug — now it is echoed, logged and warned about in the UI (G5) |

### Epic 2 — Backtest Page ✅ (2026-08-24)

Single-strategy deep-dive UI, end-to-end on top of the Epic 1 API. Live preview
on port 5000 → `/backtest`.

| # | Task | Deliverable |
|---|------|-------------|
| 2.1 | Backtest page route | `web/app.py` — `GET /backtest` (+ `/` redirect) |
| 2.2 | Template structure | `web/templates/backtest.html` — two-column config/results |
| 2.3 | Dynamic params (JS) | `web/static/js/backtest.js` — type-aware form from `/params` |
| 2.4 | Equity curve chart | `web/static/js/charts/equity_chart.js` (+ buy&hold benchmark) |
| 2.5 | Drawdown chart | `web/static/js/charts/drawdown_chart.js` — area + fill done; ⚠ **worst-DD marker was never built** (only named in the docstring) → tracked as **G7** |
| 2.6 | Price + signals chart | `web/static/js/charts/signals_chart.js` (▲ buys / ▼ sells) |
| 2.7 | Metrics cards | `web/static/js/components/metrics_cards.js` |
| 2.8 | Trade table | `web/static/js/components/trade_table.js` (paginated + sortable) |
| 2.9 | Save to Compare | `backtest.js` → `SessionState.addCompareSlot` (max 4) |
| 2.10 | Export CSV | `backtest.js` — client-side CSV download |

**Epic 5 (cross-page) also delivered:** 5.1 shared nav `base.html`, 5.2
`session_state.js`, 5.3 `toast.js`, 5.4 `loader.js`. Promote-to-Forward wires
`forwardPrefill` (lands in Epic 4).

**Deviations:** frontend uses Chart.js (already a dependency via the existing
forward dashboard) instead of splitting chart libs; Export CSV is generated
client-side from the run response (stateless) rather than a server `session_id`
endpoint.

### Epic 3 — Compare Page ✅ (2026-08-24)

Side-by-side comparison of 2–4 strategy/timeframe combinations, on top of the
Epic 1 parallel API. Live preview → `/compare`.

| # | Task | Deliverable |
|---|------|-------------|
| 3.1 | Compare route | `web/app.py` — `GET /compare` |
| 3.2 | Template structure | `web/templates/compare.html` — shared config bar + dynamic slots + 3-tab results |
| 3.3 | Slot management | `web/static/js/compare.js` — add/remove (2–4), independent strategy+params, pre-fill from saved slots |
| 3.4 | Run All | `compare.js` — validates, POST `/api/backtest/run-many`, per-slot status |
| 3.5 | Metrics table | `web/static/js/compare/metrics_table.js` — N-column, best-per-row 🏆 |
| 3.6 | Overlaid equity | `web/static/js/compare/equity_compare_chart.js` — date-union alignment |
| 3.7 | Overlaid drawdown | `web/static/js/compare/drawdown_compare_chart.js` |
| 3.8 | Per-slot actions | `metrics_table.js` actions row → `backtest_prefill` / `forward_prefill` + navigate |

**Deviations:** overlaid charts align series to the union of all dates (null gaps)
so slots with different timeframes/timebases compare correctly; best-per-row uses
max for return/win-rate/Sharpe/max-DD (least-negative) and leaves Trades
unhighlighted (no meaningful "best").

### Epic 4 — Forward Test Page ✅ (2026-08-24)

Live paper-trading page + forward API, completing the promote loop. Live preview
→ `/forward`. Shared param-form component extracted (`components/params_form.js`)
and now used by Backtest / Compare / Forward.

| # | Task | Deliverable |
|---|------|-------------|
| 4.1 | Forward pre-fill | `web/static/js/forward.js` — reads `forward_prefill`, pre-fills, banner, clears |
| 4.2 | Template cleanup | ✅ `web/templates/forward.html` + `forward.js` — config (now incl. replay speed), status/Start-Stop, live equity chart, live metric cards, positions with real marks, shared trade feed, progress bar. (G3 closed 2026-08-28.) |
| 4.3 | Forward API | ✅ `api/forward.py` — `POST /start`, `POST /stop`, `GET /status`, plus `/sessions`, `/trades`, `/equity`; `state_id`-keyed sessions and a server-side clock (G4 closed 2026-08-28) |

**Tests:** `tests/test_api_forward.py` — 8 tests (idle before start, valid/unknown/missing,
shape, progress→complete, stop freezes, refresh-safe). **1575 passing.**

**Deviations:** no live market feed in the sandbox (mStock needs credentials), so
forward testing is an **in-process paper-trading replay** — `/start` runs the
strategy once, `/status` reveals it bar-by-bar each poll, recomputing metrics on
the prefix via `BacktestAdapter` + `compute_metrics` (same shape as the Backtest
page, so components are reusable). Server-side state survives a page refresh;
DB persistence is deferred to V2.

### Dashboard landing page ✅ (2026-08-24)

Built as a **lightweight landing** (not in the 37-task decomposition; discussed
and scoped down from a full dashboard due to overlap with the Forward page and
the existing Step 19 dashboard, and the lack of persisted run history).

`GET /dashboard` → `web/templates/dashboard.html` + `web/static/js/dashboard.js`.
Aggregates cross-page state: strategies available (count + chips), **live
forward-bot status** (idle/running + active symbol + live P&L + replay progress,
visible from anywhere), and workflow nav cards. No persistence required.

Bug fixed along the way: forward `/status` config now carries `strategy`/`symbol`
onto the live snapshot (previously blank because `compute_metrics` rebuilds the
metrics dict) — locked in with a test assertion.

### Epic 6 — Testing ✅ (2026-08-24)

| # | Task | Deliverable |
|---|------|-------------|
| 6.1 | Strategy contract | `tests/test_strategy_base.py` — validation, schema forms, auto-discovery |
| 6.2 | BacktestAdapter | `tests/test_backtest_adapter.py` — shapes, drawdown, trades, immutability |
| 6.3 | Backtest API | `tests/test_api_backtest.py` — run/run-many, error cases |
| 6.4 | Strategy API | `tests/test_api_strategies.py` — catalogue, params, 404 |
| 6.5 | End-to-end workflow | `tests/test_e2e_workflow.py` — load→run→adapt, 4-slot parallel+winner, promote→forward |
| 6.6 | Strategy migration | 4 strategies now declare `description/version/author` + full param schemas; verified in registry |

**Strategy migration (6.6):** all four strategies converted from flat
(`params = {"period": 14}`) to full schema form
(`{"default", "min", "max", "type", "label", "tooltip"}`) with human metadata —
so the dynamic UI forms are now fully populated (labels, tooltips, ranges).
Signal logic unchanged; flat-form support retained for any future strategy.

**Final test status: 1582 passing, 0 regressions.** All 6 PRD success criteria
are satisfied and demonstrable on the live preview.

> ⚠ **Re-verified 2026-08-28 — the picture is more nuanced than the line above.** Suite is now
> **1813 passed / 4 skipped / 3 failed** (`G5`). Criteria #4 (strategy auto-discovery) and #6
> (shared registry) hold; #1, #2, #3, #5 are only partly met — see the per-criterion table in
> `instructions/PRD-VERIFICATION-2026-08-28.md`. Epic 6's own coverage is sound, but 6.2 asserts
> adapter *shapes* only, which is why the two engine-level metric bugs (**G1**, **G2**) went
> unnoticed.

---

## Generic Broker Authentication (mStock Auth UI — ✅ COMPLETE)

Tracks progress against `instructions/Generic_Broker_Authentication.md` — the
broker-agnostic auth layer (Credentials → TOTP), session store, auth status
API, and the Forward Test start guard. Active branch: `arena/01a03e5a-back-test`.

> **All 5 phases complete, all 13 tasks delivered. See dedicated tracker:**
> `instructions/BROKER-AUTH-TRACKER.md` for full details.

### Phase 1 — Generic Broker Auth Backend Layer ✅ (2026-08-25)

| # | Task | Deliverable | Status |
|---|------|-------------|--------|
| 1.1 | `BrokerAuthBase` abstract class | `src/backtest/brokers/base.py` (+ package `__init__`, status constants) | ✅ **Complete** |
| 1.2 | `MStockBroker` implementation | `src/backtest/brokers/mstock.py` | ✅ **Complete** |
| 1.3 | `BrokerSessionManager` singleton | `src/backtest/brokers/session_manager.py` | ✅ **Complete** |

### Phase 2 — Authentication API Endpoints ✅ (2026-08-25)

| # | Task | Deliverable | Status |
|---|------|-------------|--------|
| 2.1 | Auth API routes | `src/backtest/api/broker_auth.py` (mounted in `web/app.py`) | ✅ **Complete** |
| 2.2 | Session expiry background monitor | `src/backtest/brokers/session_manager.py` | ✅ **Complete** |

### Phase 3 — Authentication UI Components ✅ (2026-08-26)

| # | Task | Deliverable | Status |
|---|------|-------------|--------|
| 3.1 | Nav broker status icon | `web/templates/base.html` + `web/static/js/broker_status.js` | ✅ **Complete** |
| 3.2 | Auth popup modal | `web/templates/components/broker_auth_modal.html` + `web/static/js/broker_auth_modal.js` | ✅ **Complete** |
| 3.3 | Session expiry toast | `web/static/js/broker_status.js` | ✅ **Complete** |

### Phase 4 — Forward Test Page Guard ✅ (2026-08-26)

| # | Task | Deliverable | Status |
|---|------|-------------|--------|
| 4.1 | Gate Forward Start button on auth status | `web/static/js/forward.js` (client-side gate) | ✅ **Complete** |
| 4.2 | Server-side guard on `/api/forward/start` | `src/backtest/api/forward.py` | ✅ **Complete** |

### Phase 5 — Integration & Verification ✅ (2026-08-26)

| # | Task | Deliverable | Status |
|---|------|-------------|--------|
| 5.1 | Full auth flow integration test | `tests/manual/test_auth_flow_integration.py` (7 tests) | ✅ **Complete** |
| 5.2 | Session expiry warning test | `tests/test_broker_expiry.py` (11 tests) | ✅ **Complete** |
| 5.3 | Security verification checklist | `tests/test_security_verification.py` (29 tests) | ✅ **Complete** |

**Final test status: 1740 passed / 3 skipped / 1 failed** (the 1 failure is
pre-existing and unrelated — `test_mstock_auth::test_login_sends_sdk_headers`
needs `MSTOCK_API_KEY`). All 13 auth tasks complete, all success criteria met.

**Deviations from the PRD:** top-level `brokers/` paths map to
`src/backtest/brokers/` (repo nests under `src/backtest/`, same as PRD V1).
Task 2.2 was implemented together with 1.3 (same file), and 3.3 with 3.1
(same file).

---

## Simulator Progress

`████████████████████████████████████████░░` **20 / 24 steps complete** (83%)

| Phase | Steps | Status |
|---|---|---|
| 1 · Database | 1–2 | ✅ **Complete** |
| 2 · Core models | 3–6 | ✅ **Complete** |
| 3 · Execution | 7–9 | ✅ **Complete** |
| 4 · Live data | 10–12 | ✅ **Complete** |
| 5 · Strategy | 13–14 | ✅ **Complete** |
| 6 · Risk | 15–16 | ✅ **Complete** |
| 7 · Performance | 17–19 | ✅ **Complete** |
| 8 · Orchestration | 20 | ✅ **Complete** |
| Bonus | 21–24 | ⬜ Not started |

Build order follows the plan's own recommendation: **1–6 → 10–12 → 7–9 → 20 → rest**.

---

## Steps

Legend: ✅ done · 🟡 in progress · ⬜ not started · ⏭️ deferred

### Phase 1 — Database Design & Setup

| # | Step | Status | Deliverables | Tests |
|---|---|---|---|---|
| 1 | Database Schema Design | ✅ | `db/migrations/001_initial_schema.sql`, `.sqlite.sql`, rollback, `db/verify_schema.sql`, `src/backtest/db/models.py`, `db/alembic/`, `db/DB-IMPLEMENTATION-GUIDE.md` | 44 |
| 2 | Database Connection Manager | ✅ | `src/backtest/db/manager.py`, `src/backtest/db/config.py`, `config/database.yaml`, `db/CONNECTION-MANAGER.md` | 107 |

### Phase 2 — Core Data Models

| # | Step | Status | Deliverables | Tests |
|---|---|---|---|---|
| 3 | Portfolio Model | ✅ | `src/backtest/simulator/portfolio.py`, `position.py` (base), `errors.py`, `money.py` | 130 |
| 4 | Position Model | ✅ | `src/backtest/simulator/lots.py` (LotBook), FIFO/LIFO/average, splits, dividends, `save_to_db` | 77 |
| 5 | Order Model | ✅ | `src/backtest/simulator/order.py`, `enums.py` — state machine, 5 order types, triggers, callbacks | 115 |
| 6 | Fill Model | ✅ | `src/backtest/simulator/fill.py`, `commission.py` — immutable fills, 5 commission models, `Portfolio.apply_fill` | 106 |

### Phase 3 — Order Execution Simulation

| # | Step | Status | Deliverables |
|---|---|---|---|
| 7 | Slippage Model | ✅ | `src/backtest/simulator/slippage.py`, `config/slippage.yaml` — 5 models, 4 profiles, tiers, time-of-day · **101 tests** |
| 8 | Commission Calculator | ✅ | `src/backtest/simulator/fees.py`, `config/brokers.yaml` — NSE + US fee stacks, 10 broker presets, monthly volume tiers, FX · **109 tests** |
| 9 | Order Execution Simulator | ✅ | `src/backtest/simulator/execution.py`, `config/execution.yaml` — liquidity caps, queue position, latency, rejections · **99 tests** |

### Phase 4 — Live Data Integration

| # | Step | Status | Deliverables |
|---|---|---|---|
| 10 | Market Data Handler | ✅ | `src/backtest/marketdata/` (`ticks.py` normalization, `bars.py` NSE-anchored aggregation, `feed.py` mStock + mock feeds, `handler.py` hub), `config/marketdata.yaml` — wired to existing `live/mstock.py` · **173 tests** |
| 11 | Data Quality Validator | ✅ | `src/backtest/marketdata/quality.py`, `config/quality.yaml` — z-score spikes, gap/volume anomalies, 3 strictness levels, reject/repair, alerts + regime reset, handler integration · **114 tests** |
| 12 | Time Synchronization Manager | ✅ | `src/backtest/marketdata/timesync.py`, `config/calendar.yaml` — NSE sessions (pre-open/continuous/closing) in IST, NSE 2025–26 + NYSE 2026 holidays, next open/close, trading-day ranges, NTP sync, latency tracking · **96 tests** |

### Phase 5 — Strategy Integration

| # | Step | Status | Deliverables |
|---|---|---|---|
| 13 | Strategy Adapter | ✅ | `src/backtest/forward/strategy_adapter.py` — Bridge to existing `strategy/base.py`, Signal model, multi-symbol support · **20 tests** |
| 14 | Position Sizing Engine | ✅ | `src/backtest/simulator/position_sizing.py`, `config/position_sizing.yaml` — Fixed / %-of-portfolio / risk-based / ATR / Kelly, 8 profiles, constraints · **25 tests** |

### Phase 6 — Risk Management

| # | Step | Status | Deliverables |
|---|---|---|---|
| 15 | Risk Manager | ✅ | `src/backtest/simulator/risk_manager.py`, `config/risk.yaml` — Pre-trade checks, circuit breakers, 5 profiles · **24 tests** |
| 16 | Stop Loss & Take Profit Manager | ✅ | `src/backtest/simulator/stop_manager.py`, `config/stops.yaml` — Trailing stops, OCO, 7 profiles · **21 tests** |

### Phase 7 — Performance Tracking

| # | Step | Status | Deliverables |
|---|---|---|---|
| 17 | Performance Calculator | ✅ | `src/backtest/simulator/performance.py`, `config/performance.yaml` — Return/risk metrics, Sharpe/Sortino, VaR · **14 tests** |
| 18 | Trade Analyzer | ✅ | `src/backtest/simulator/trade_analyzer.py` — Attribution by exit reason, MAE/MFE analysis · **15 tests** |
| 19 | Real-Time Dashboard | ✅ | `src/backtest/dashboard/` (`app.py` Flask + Chart.js, `data_provider.py`), web UI with live updates · **15 tests** |

### Phase 8 — System Orchestration

| # | Step | Status | Deliverables |
|---|---|---|---|
| 20 | Main Forward Testing Engine | ✅ | `src/backtest/forward/engine.py`, `config/forward_testing.yaml`, `Dockerfile`, `forward_testing.service` — Event loop, state persistence, lifecycle hooks · **14 tests** |

### Bonus

| # | Step | Status |
|---|---|---|
| 21 | Alert & Notification System | ✅ · **33 tests** |
| 22 | Backtesting Comparison Tool | ✅ · **13 tests** |
| 23 | Configuration Manager | ✅ · **13 tests** |
| 24 | Testing & CI/CD Setup | ⬜ |

---

## Manual actions required from you

Things the agent cannot do; tracked so nothing is silently skipped.

| # | Action | From | Status |
|---|---|---|---|
| 1 | Apply `db/migrations/001_initial_schema.sql` to your PostgreSQL | Step 1 | ⬜ Pending |
| 2 | Run `db/verify_schema.sql`, confirm all PASS | Step 1 | ⬜ Pending |
| 3 | Set `FORWARD_TEST_DB_URL` in `.env` (never commit it) | Step 1/2 | ⬜ Pending |
| 4 | `pip install -r requirements.txt` (adds SQLAlchemy, Alembic, psycopg2, PyYAML, Flask) | Step 1/2 | ⬜ Pending |
| 5 | Review & merge PR #3 | — | ✅ Merged 2026-08-19 (`4e01d65`) |
| 6 | Review & merge the Phase 3 PR (Steps 8–9) | — | ✅ Merged 2026-08-23 as PR [#5](https://github.com/yogesh-ing/back-test/pull/5) (`43abb57`) — user pushed & merged manually |
| 7 | Review & merge the Phase 4 PR (Steps 10–12) | — | ✅ Merged as PR [#6](https://github.com/yogesh-ing/back-test/pull/6) |
| 8 | Review & merge the Phase 5-8 PR (Steps 13-20) | — | ⬜ Pending — current PR |
| 9 | Verify `config/calendar.yaml` NSE holiday list against the official NSE circular; add each new year's list every December | Step 12 | ⬜ Ongoing |

---

## Deviations from the plan document

Recorded so the divergence is deliberate and reviewable, not accidental drift.

| # | Plan says | We do | Why |
|---|---|---|---|
| 1 | SEC / FINRA TAF fees (US) | **Both** regimes implemented (`IndiaEquityFees` default, `USEquityFees` available) | Repo trades NSE via mStock, but the plan names US fees |
| 2 | Column named `timestamp` | Column named `ts` | `timestamp` is a SQL type name; forces quoting, confuses ORM reflection |
| 3 | NYSE calendar, 9:30–16:00 ET | NSE calendar, IST | Same reason as 1 |
| 4 | Broker: Alpaca / IBKR | mStock (already implemented in `live/`) | Existing code |
| 5 | New Strategy base class (Step 13) | Adapt existing `strategy/base.py` | Avoid duplicating a working abstraction |
| 6 | Native SQL `ENUM` types | `VARCHAR` + `CHECK` | Portability to SQLite, required by Step 2 |
| 7 | — | New `simulator/` package for Steps 3–6 | Avoids collision with existing `forward.portfolio.Portfolio` and ORM `db.models.Portfolio` |

---

## Known limitations

Deliberate gaps, recorded so they are not mistaken for oversights.

| # | Limitation | From | Impact |
|---|---|---|---|
| 1 | Tax lots are **not** persisted — the schema has no `lots` table. A FIFO position reloaded from the database collapses to one lot at the stored average price. | Step 4 | Restart mid-run loses FIFO granularity. Workaround: the `to_dict()`/`from_dict()` JSON snapshot **is** lossless; the Step 20 state manager should use it. Revisit if per-lot tax reporting is needed. |
| 2 | `Order.status_history`, `triggered` and `extreme_price` are not persisted — no columns in `orders`. | Step 5 | Survives in the `to_dict()` JSON snapshot; Step 20 should use it. |
| 3 | Special trading sessions (Muhurat, half-days, ad-hoc extensions) are not modelled — `config/calendar.yaml` only knows full trading days and full holidays. | Step 12 | 2026-11-08 Muhurat session will show as closed. Acceptable for daily-bar strategies; revisit for intraday live trading. |
| 4 | Holiday calendars require **annual maintenance** — NSE/NYSE publish next year's list each December; add it to `config/calendar.yaml`. | Step 12 | A missing year makes every weekday look like a trading day. |

---

## Architecture as built

```
src/backtest/
├── db/                  # Steps 1–2 ✅
│   ├── models.py        #   ORM: 10 tables
│   ├── manager.py       #   DatabaseManager: pooling, retries, transactions
│   └── config.py        #   Layered config
├── simulator/           # Steps 3–9, 14–18 ✅ (domain models)
│   ├── money.py         #   Decimal helpers  ✅
│   ├── errors.py        #   Domain exceptions ✅
│   ├── lots.py          #   LotBook, FIFO/LIFO ✅ Step 4
│   ├── position.py      #   Position         ✅ Steps 3+4
│   ├── portfolio.py     #   Portfolio        ✅
│   ├── enums.py         #   Order enums + FSM  ✅ Step 5
│   ├── order.py         #   Order            ✅ Step 5
│   ├── commission.py    #   5 fee models       ✅ Step 6
│   ├── fill.py          #   Fill (immutable)   ✅ Step 6
│   ├── slippage.py      #   5 slippage models  ✅ Step 7
│   ├── fees.py          #   Full fee stack     ✅ Step 8
│   ├── execution.py     #   OrderExecutor      ✅ Step 9
│   ├── position_sizing.py # PositionSizer, 6 methods, constraints ✅ Step 14
│   ├── risk_manager.py  # RiskManager, RiskConfig, circuit breakers ✅ Step 15
│   ├── stop_manager.py  # StopManager, StopType, OCO, trailing ✅ Step 16
│   ├── performance.py   # PerformanceCalculator, return/risk/ratios ✅ Step 17
│   └── trade_analyzer.py # TradeAnalyzer, MAE/MFE, attribution ✅ Step 18
├── marketdata/          # Steps 10–12 ✅ (live data layer)
│   ├── errors.py        #   MarketDataError hierarchy
│   ├── ticks.py         #   Tick/Bar + broker payload normalization
│   ├── bars.py          #   IST boundary alignment, BarAggregator
│   ├── feed.py          #   DataFeed ABC, MStockFeed, MockFeed
│   ├── handler.py       #   MarketDataHandler: buffers, reconnect, cache
│   ├── quality.py       #   DataValidator: spikes, gaps, strictness  ✅ Step 11
│   └── timesync.py      #   TimeManager: NSE calendar, NTP, latency  ✅ Step 12
├── forward/             # Steps 13, 20 ✅ (orchestration)
│   ├── strategy_adapter.py  # StrategyAdapter, Signal, sizers ✅ Step 13
│   ├── engine.py            # ForwardTestingEngine, StateManager ✅ Step 20
│   ├── broker.py        #   SimulatedBroker (legacy)
│   ├── paper.py         #   Walk-forward + live papertrade
│   └── portfolio.py     #   Legacy multi-strategy allocator
├── strategy/            # Strategy abstraction + adapter re-export
│   ├── base.py          #   Strategy base (existing, reused) ✅
│   ├── adapter.py       #   Re-export of forward + simulator sizers ✅ Steps 13–14
│   └── registry.py      #   Strategy registry
├── config/
│   ├── forward_testing.yaml # Main engine config ✅ Step 20
│   ├── position_sizing.yaml # 8 profiles for sizing ✅ Step 14
│   ├── risk.yaml            # Risk limits, 5 profiles ✅ Step 15
│   ├── stops.yaml           # Stop loss & TP, 7 profiles ✅ Step 16
│   ├── performance.yaml     # Performance calc, risk-free, VaR ✅ Step 17
│   ├── marketdata.yaml      # Market data handler config ✅ Step 10
│   ├── quality.yaml         # Data validator config ✅ Step 11
│   ├── calendar.yaml        # Trading calendar ✅ Step 12
│   ├── slippage.yaml
│   ├── execution.yaml
│   ├── brokers.yaml
│   └── database.yaml
├── dashboard/           # Step 19 ✅ (real-time UI)
│   ├── app.py           # Flask + Chart.js, API, HTML template ✅ Step 19
│   └── data_provider.py # DashboardDataProvider backend logic ✅ Step 19
├── alerts/              # Step 21 ✅ (notifications)
│   └── manager.py       # AlertManager, multi-channel ✅ Step 21
├── analysis/            # Step 22 ✅ (comparison)
│   └── comparison.py    # BacktestComparisonTool ✅ Step 22
├── config_manager/      # Step 23 ✅ (config management)
│   └── manager.py       # ConfigManager ✅ Step 23
├── docs/
│   └── LOCAL-TESTING-MANUAL.md # Local testing guide ✅ Phase 4
├── Dockerfile           # Container setup ✅ Step 20
├── forward_testing.service # systemd service ✅ Step 20
│
├── data/ engine/ live/ strategies/   # pre-existing
```

**Layering rule:** `simulator/` holds pure in-memory domain logic and must not
import from `engine/` or `forward/`. It talks to the database only through
`db.DatabaseManager`, so every model stays unit-testable with no I/O.
`forward/` and `strategy/` may import from `simulator/` — the adapter bridges
`strategy/base.py` (existing) into `simulator.Portfolio`/`OrderExecutor`.

---

## Test counts

| Suite | Tests |
|---|---|
| Pre-existing (backtest engine, mStock) | 25 (+4 skipped — need credentials) |
| `test_db_schema.py` (Step 1) | 44 |
| `test_db_manager.py` (Step 2) | 107 |
| `test_simulator_portfolio.py` (Step 3) | 130 |
| `test_simulator_position.py` (Step 4) | 77 |
| `test_simulator_order.py` (Step 5) | 115 |
| `test_simulator_fill.py` (Step 6) | 106 |
| `test_simulator_slippage.py` (Step 7) | 101 |
| `test_simulator_fees.py` (Step 8) | 109 |
| `test_simulator_execution.py` (Step 9) | 99 |
| `test_marketdata.py` (Step 10) | 173 |
| `test_marketdata_quality.py` (Step 11) | 114 |
| `test_timesync.py` (Step 12) | 96 |
| `test_strategy_adapter.py` (Step 13) | 20 |
| `test_simulator_position_sizing.py` (Step 14) | 25 |
| `test_simulator_risk_manager.py` (Step 15) | 24 |
| `test_simulator_stop_manager.py` (Step 16) | 21 |
| `test_simulator_performance.py` (Step 17) | 14 |
| `test_simulator_trade_analyzer.py` (Step 18) | 15 |
| `test_dashboard.py` (Step 19) | 15 |
| `test_forward_engine.py` (Step 20) | 14 |
| `test_alert_manager.py` (Step 21) | 33 |
| `test_comparison.py` (Step 22) | 13 |
| `test_config_manager.py` (Step 23) | 13 |
| `test_logging_config.py` (U1, 2026-08-28) | 37 |
| `test_engine_trades.py` (G1/G2, 2026-08-28) | 14 |
| `test_api_forward.py` (G3/G4 rework, 2026-08-28) | 36 |
| `tests/js/test_forward_widgets.mjs` (G3 live widgets) | 7 node |
| `test_web_components.py` + `tests/js/test_metrics_cards.mjs` (G1/G2 in the UI) | 2 + 9 node |
| PRD/API suites (`test_api_*`, `test_strategy_base`, `test_backtest_adapter`, `test_e2e_workflow`, `test_broker_*`, `test_security_verification`, `test_portfolio_engine`, `test_circuit_breakers`) | ~500 |
| **Total** | **1893 passing / 4 skipped / 0 failing** (post-G3/G4). The 4 skips are mStock-credential tests; with `psycopg2` absent the 2 Postgres-driver tests skip too (6). Node harnesses: 11 + 14 + 12 + 10 + 7 |
