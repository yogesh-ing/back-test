# Live-Market QA Runbook: Paper Portfolio (`/portfolio/paper`), Child Tabs & Phases 1–4

**Target Environment:** Local Windows / Linux server (`http://127.0.0.1:5000`) during **live NSE market hours (09:15–15:30 IST)**  
**Target Branch:** `feature/options-trading-dev` / `arena/f7407685-back-test` (`85791e7`+)  
**Scope:** End-to-end functional, data-integrity, and UX validation of **Paper Trading (`/portfolio/paper`)**, every parent/child tab, slide-over drawer, action modal, and **Phases 1–4 (`P1`–`P4`)** real-time plumbing.  
**Test Policy:** **Active / destructive paper testing is explicitly approved.** Today's paper P&L does **not** need to be preserved — intentionally force entries, partial/full manual closes, tight SL/TP triggers, simulated `-25%` stress crashes, pauses, resumes, and a mid-session server restart so nothing goes untested.

> **⚠️ Safety Rule #1 for the QA Tester:**  
> Stay in the **PAPER** bucket (`Mode = PAPER (free play)`). Never deploy a `LIVE` runner unless testing the negative gate check in Test `P4-3` (which verifies that `live` blocks unentitled structures). Paper actions never send real money orders to mStock.

---

## 0. Executive Due-Diligence Findings (Read Before Testing!)

Codebase inspection of `_portfolio_center.html`, `portfolio.js`, `risk_board.js`, `portfolio_intelligence.js`, `orders_tab.js`, `deep_dive.js`, `playbooks.js`, `paper_runner.py`, and `options_bridge.py` confirmed **exact root causes** for the three issues already observed — plus four related UX gaps the tester will encounter today:

| ID | Observed Symptom | Exact Root Cause (Verified in Code) | What QA Should Expect Today |
|---|---|---|---|
| **UX-1** | **`Trade history` tab shows Exit Time only — Entry Time is missing** | `GET /api/portfolio/trades/aggregated` **does** return both `entry_ts` and `exit_ts`, **but** `_portfolio_center.html:461` & `risk_board.js:252` only render 9 columns ending with `Exit Reason / Time` (`(t.exit_ts \|\| "").slice(0,16)`), dropping `entry_ts` and hold duration completely. Also, Deep-Dive → `Trades` (`deep_dive.js:173`) shows **neither** entry time nor exit time. | Confirmed UI defect (data exists in API; verify via DevTools Network tab in `TC-5.2`). |
| **UX-2** | **`Risk & intelligence` bucket dropdown (`#risk-mode`) does not work properly** | **Two JS controllers conflict over `#risk-mode`:**<br>1. `portfolio_intelligence.js:50–56` (`mode()`) checks `if (pageMode) return pageMode;` **before** reading `#risk-mode`. On `/portfolio/paper` (`data-mode="paper"`), changing `#risk-mode` is **100% ignored** by all 5 top Intelligence sections (Greeks, Concentration, Correlation, Regime, Activity).<br>2. `risk_board.js:214,230` (bottom 4 Risk Gauges + Trade History) ignores `data-mode="paper"` on initial load, starting on `All Buckets` (`""`) until manually switched! | Confirmed bug. On `/portfolio/paper`, test dropdown behavior explicitly (`TC-4.1`), and also test `/portfolio` (combined page where `data-mode=""`) or `/risk` to verify cross-bucket switching. |
| **UX-3** | **Running trade SL & TP not identifiable — unclear if set by strategy or missing** | **Both truths apply depending on strategy + UI column design:**<br>1. The `Target` and `Stop` columns in `Positions` (`paper_runner.py:2079–2080`, `options_bridge.py:1091–1092`) **only display manual operator LOM overrides** (set via `🛑 SL` / `🎯 TP` row buttons), **never** the strategy's automatic `ExitPlan`!<br>2. Diagnostic strategies (`CE_instant_buy`, `PE_instant_buy`) genuinely have **no SL/TP** ("ride to settlement").<br>3. Strategies like `immediate_strangle` (±₹100 SL/TP), `directional_options` (`-50%` SL / `+100%` TP / signal flip), and `hf_equity_scalp` (`+3 pt` TP / `-2 pt` SL / 5-bar stop) **do** enforce automatic exits in engine code, but option `ExitPlan` is currently visible **only** inside `🔍 Deep-Dive → Config → Exit policy`. | Confirmed major UX gap. Verify hidden automatic exits in `🔍 → Config` (`TC-6.3`) and manual LOM `🛑 SL` / `🎯 TP` overrides in `TC-2.3`–`TC-2.4`. |
| **UX-4** | **Clock mismatch (UTC vs IST) across tabs** | `Positions` (`portfolio.js:424` `execTimeLabel`) converts timestamps to local **IST** (`12:15:12`), whereas `Orders` (`orders_tab.js:91` `clock()`) and `Trade history` (`risk_board.js:252`) slice the raw ISO UTC string (`06:45:12` — 5h30m behind IST!). | Expect a `-5h 30m` display offset in `Orders` and `Trade history` vs `Positions` — log screenshots for the fix sweep. |
| **UX-5** | **`Trade history` ignores `PAPER` page scope on first open** | `risk_board.js:230` reads `#risk-mode` (default `""` = All Buckets) instead of `#portfolio-page[data-mode="paper"]`, so `Trade history` queries `mode=` (all buckets) unless `Paper Only` is selected in `Risk & intelligence`. | Check Network call `GET /api/portfolio/trades/aggregated` in `TC-5.1`. |
| **UX-6** | **Playbooks `Deploy` cold-load & `prompt()` UX** | `playbooks.js:145` checks `typeof loadSpawnForm === "function"`, which is private inside `portfolio.js`. If `+ Add instance` was never opened first on the page, clicking `Deploy` on a playbook opens `#spawn-modal` with an empty Strategy dropdown. Also `＋ New Playbook` uses raw browser `prompt()` dialogs and omits `long_straddle`/`short_straddle` in the prompt hint text. | Open `+ Add instance` once (and close it) before testing `Playbooks → Deploy` (`TC-7.2`). |
| **UX-7** | **Phase 4 Straddles have engine/playbook support, but no standalone Strategy plugin yet** | `LongStraddle` & `ShortStraddle` shipped in `options/structures.py` and `config/risk_limits.yaml` (`paper`), while `plugins/strategies/*.py` remained locked — so there is `immediate_strangle` in the Strategy dropdown, but no `immediate_straddle` strategy yet, and `/api/options/trade` `_STRUCTURE_REGISTRY` only lists V1 4 structures. | Test multi-leg live mechanics via `immediate_strangle` in UI (`TC-1.4`) and track adding `immediate_straddle` plugin in Section 9. |

---

## 1. Pre-Flight Setup (10 Minutes Before Starting)

Junior QA must verify all 5 items before running test cases:

1. **Confirm `.env` settings** (in repo root):
   - mStock credentials configured; `OPTIONS_INTRADAY_SQUAREOFF_MINUTES=20` set (or note current value for EOD test).
2. **Start server & tail logs in a second terminal** (keep visible throughout testing!):
   ```bash
   cd src && python -m backtest.web.app --port 5000
   ```
   Open a second terminal to watch live execution & WebSocket markers:
   - Look for: `INTENT_RECEIVED`, `ENTRY_SELECT`, `ENTRY_FILLED`, `OPTION_EXIT`, `QUOTE_ONE_SIDED`, `NO_EXECUTION_QUOTE`, `tick profile`, `RISK_UTILIZATION`.
3. **Open Browser + DevTools (`F12`)**:
   - Navigate to `http://127.0.0.1:5000/portfolio`
   - Keep **DevTools → Console** (check for zero uncaught red JS errors) and **Network** open.
4. **Authenticate mStock (Live Feed + WebSocket)**:
   - Click the topbar **Broker status** chip → log in to **mStock** with TOTP.
   - **Pass Criteria:** Broker chip turns green/connected; topbar **Market chip** shows NSE `OPEN` (`09:15–15:30 IST`); topbar **Freshness chip** loads without error.
5. **Check Landing Page (`/portfolio`) → Paper Card**:
   - Record baseline numbers on the **PAPER** bucket card (`Portfolio equity`, `Allocated capital`, `Daily P&L`, `Realized P&L`, `Instances`, `Running`, `Open positions`).
   - Click **Open paper sandbox** → lands on `http://127.0.0.1:5000/portfolio/paper`.

---

## 2. Test Suite A — Paper Command Center Chrome & Runner Spawning (`TC-1.x`)

### `TC-1.1` · Page Scope, Header Chrome & Hidden Live Controls
1. On `/portfolio/paper`, inspect the top banner, header card (`Paper command center`), and bottom scope banner.
2. Expand/collapse the top **`Deployment snapshot`** `<details>` accordion.
3. Click **`Dismiss`** on the **`Unified execution`** info banner, then press `F5` (reload page).
4. **Expected Result:**
   - Feed dot (`#feed-dot`) turns green (`🟢`) with live feed label and bar countdown (`#bar-countdown`).
   - **`Emergency flatten` button (`#btn-emergency`) MUST BE HIDDEN** on `/portfolio/paper` (safety design: paper has no real capital at risk; verify `hidden` attribute).
   - Bottom banner states: *"Scoping this command center to the PAPER bucket only — other buckets never appear here."*
   - `Unified execution` banner stays dismissed after reload (`localStorage`).

### `TC-1.2` · Spawn Archetype #1 — Fast 1-Min Equity Scalper (`hf_equity_scalp`)
1. Click **`+ Add instance`** (`#btn-add`).
2. Select:
   - **Strategy:** `hf_equity_scalp`
   - **Target type:** `Single Symbol`
   - **Timeframe:** `1min`
   - **Bucket mode:** `PAPER (free play)`
   - **Data source:** `MSTOCK`
   - **Instrument:** pick `RELIANCE` (or `ICICIBANK` / `SBIN` — high-volume stock) via SymbolPicker tabs.
   - **Allocated capital:** `200000`
3. Click **`Deploy Instance`**.
4. **Expected Result:**
   - Toast confirms creation; new row appears immediately in **Strategy instances** matrix with status `🟢 RUNNING`.
   - Within 1–3 minutes of live market ticks, `hf_equity_scalp` opens and closes equity trades (+3 pt TP / -2 pt SL / 5-bar time exit).

### `TC-1.3` · Spawn Archetype #2 — Multi-Timeframe & Pool Equity Runner (`P1` Warmup + Aggregator)
1. Click **`+ Add instance`** and deploy **two** runners:
   - **Runner A (Pool + 5min):** Strategy = `ema_pullback`, Target type = `Symbol Universe (Pool)`, Universe = `nifty_50` (or smallest available curated pool), Max pool positions = `5`, Timeframe = `5min`, Mode = `PAPER`, Source = `MSTOCK`, Capital = `500000`.
   - **Runner B (Single + 15min):** Strategy = `sma_crossover`, Target type = `Single Symbol`, Instrument = `TCS`, Timeframe = `15min`, Mode = `PAPER`, Source = `MSTOCK`, Capital = `300000`.
2. Immediately watch their **Status** cell in the matrix and open each runner's **`🔍` Deep-Dive** drawer.
3. **Expected Result (`Phase 1` Verification):**
   - Browser/UI **does NOT freeze** during spawn (warmup runs asynchronously in background).
   - Runner briefly shows `WARMING_UP` (if seeding) and transitions to `RUNNING` (or shows a visible **`WARMUP DEGRADED`** warning badge if DB lacks enough historical bars — verify no silent blind trading!).
   - `5min` runner processes bars **only on 5-minute clock boundaries** (`09:20`, `09:25`, `09:30`…), and `15min` runner **only on 15-minute boundaries** (`09:30`, `09:45`, `10:00`…) — verify `Bars processed` in `🔍 → Config` does **not** increment every 1 minute!

### `TC-1.4` · Spawn Archetype #3 — Live WS Single-Leg & Multi-Leg Option Runners (`P2` + `P3` + `P4`)
1. Deploy **four** Paper option runners (`Mode = PAPER`, `Source = MSTOCK`, `Lots = 1`, `Capital = 1000000`):
   - **Opt-1 (`CE_instant_buy` · `NIFTY` · `1min`)** — immediate ATM Call buy on first bar; holds with no strategy exit.
   - **Opt-2 (`PE_instant_buy` · `BANKNIFTY` · `1min`)** — immediate ATM Put buy on first bar; holds with no strategy exit.
   - **Opt-3 (`immediate_strangle` · `NIFTY` · `1min`)** — immediate 2%-OTM Short CE + Short PE (2-leg credit structure) with automatic `₹100` SL / `₹100` TP in `ExitPlan`.
   - **Opt-4 (`banknifty_ema_momentum` or `directional_options` · `BANKNIFTY` · `5min`)** — directional option strategy with `-50%` SL / `+100%` TP / signal-flip exit.
2. Check server terminal logs as Opt-1, Opt-2, and Opt-3 enter on the next 1-minute bar.
3. **Expected Result (`Phases 2 & 3` Verification):**
   - Server log prints `INTENT_RECEIVED` → `ENTRY_SELECT` → `ENTRY_FILLED` with **real two-sided WS bid/ask fills** (BUY fills at Ask, SELL legs on strangle fill at Bid).
   - **`QUOTE_ONE_SIDED = 0`** and **`NO_EXECUTION_QUOTE = 0`**; quote source is live mStock WS depth (**never** `synthetic:bs`).
   - Top **7 Metric Cards** (`Total Capital`, `Total Equity`, `Deployed`, `Daily P&L`, `Realized P&L`, `Open Positions`, `Daily Loss Limit`) equal the exact sum of the paper rows in the Strategy Instances matrix.

### `TC-1.5` · Strategy Instances Matrix Controls (Search, Filter, Sort, Row Controls)
1. Type `NIFTY` in **`Search strategy or instrument…`** (`#matrix-search`) → only NIFTY runners remain; clear search.
2. Test **`Sort:`** dropdown (`Daily P&L`, `Open P&L`, `Capital Allocation`, `Name`, `Status`) → verify order updates cleanly on 1 Hz SSE refreshes without jumping back.
3. Click **Pause** (`⏸`) on `Opt-2 (PE_instant_buy)` → status turns `🟡 PAUSED`; filter dropdown `🟡 Paused` shows only `Opt-2`.
4. Reset filter to `All Statuses`. (Keep `Opt-2` paused for `TC-2.2` frozen-mark test!)

---

## 3. Test Suite B — All 7 Consolidated Bottom Tabs (`TC-2.x` – `TC-8.x`)

### Tab 1: `Positions` (`data-tab="positions"` — Default Open Tab)

#### `TC-2.1` · Live Sub-Second MTM, Leg Hierarchy & Greeks (`P2` + `P3`)
1. Open **`Positions`** tab with `Opt-1`, `Opt-3 (immediate_strangle)`, and an equity position open.
2. Watch `Current` and `Unrealized P&L` columns for 30 seconds during active market ticks.
3. **Expected Result:**
   - Equity row shows single flat row (`Side = LONG`, share `Qty`, `Entry`, `Current`, `Unrealized P&L`).
   - Option runner shows **1 parent structure row** badged `OPTION ... (net)` + **`↳ leg` child rows** underneath (1 leg for `CE_instant_buy`, **2 legs** for `immediate_strangle` showing Short CE + Short PE strikes, individual leg marks, leg P&L, and `Δ / Θ` Greeks).
   - Sub-title shows `exp <date> · <N> bars · in HH:MM:SS` (verify `in HH:MM:SS` matches actual **IST** entry clock!).
   - Summary strip (`#pos-summary`) accurately counts `<X> open · <Y> option · <Z> equity · <M> with a manual level`.

#### `TC-2.2` · Frozen / Paused Badge & Stale-Mark Honesty
1. Look at the open position row belonging to **`Opt-2` (`PAUSED` in `TC-1.5`)**.
2. **Expected Result:**
   - Row is **still visible** (never hidden!), prefixed with `⏸`, styled frozen, and bottom footnote warns: *"⏸ 1 row(s) belong to a paused/stopped runner — their mark is frozen (actions still work; resume the runner to see live P&L)."*
3. Resume `Opt-2` from the matrix → `⏸` disappears and live MTM resumes.

#### `TC-2.3` · Manual Stop-Loss (`🛑 SL`) — Validation Refusal, Arming & Live Trigger
1. Pick **`Opt-1` (`CE_instant_buy`)** in `Positions` (notice `Target` and `Stop` initially say `—` per `UX-3`).
2. Click **`🛑 SL`** on that row → opens **Modify Stop-Loss** modal (`#pos-sl-modal`).
3. **Negative test (through-the-market stop):** For a `LONG` option trading at e.g. `₹180`, enter `250` (above current mark!) and click **`Arm Stop`**.
   - **Expected:** Server **refuses** with a clear validation error toast (a long stop above market would fire immediately by mistake).
4. **Positive test:** Enter a valid stop **just 1.5–2.0 points below** current mark (e.g. `178.50` when mark is `180.00`) and click **`Arm Stop`**.
   - **Expected:** `Stop` column updates from `—` to `178.50`; check **`SL/Target only`** checkbox (`#pos-rules-only`) → table filters to show only armed rows! Uncheck `SL/Target only`.
5. Wait for live NIFTY tick to dip to `≤ 178.50` (or test **`Clear level`** button first, then re-arm tight).
   - **Expected:** Position auto-closes at market without strategy intervention; disappears from `Positions`; logs `MANUAL_EXIT` in runner Signal Log and records fill legs in `Orders` + `Trade history`!

#### `TC-2.4` · Manual Target (`🎯 TP`), Partial Close (`◐ 50%`) & Full Close (`🚪 Close`)
1. **Manual Target (`🎯 TP`):** On an open position, click **`🎯 TP`**, set target **1–2 points favourable** to current mark → verify `Target` column updates and auto-books profit when touched.
2. **Partial Scale-Out (`◐ 50%`):**
   - Check an **Option** row (`immediate_strangle` or `PE_instant_buy`): verify the **`◐` button is DISABLED** with tooltip *"Multi-leg structures close atomically — 100% only"*.
   - On an **Equity** position with `Qty > 1` (if `hf_equity_scalp` isn't holding one, deploy `buy_and_hold` on `SBIN`, `1min`, `Capital = 100000` so it buys >100 shares and holds!):
     - First arm a valid `🛑 SL` on the `SBIN` row.
     - Click **`◐ 50%`** → click **`50%`** preset (`0.5`) → **`Close Fraction`**.
     - **Expected:** Half the shares close immediately into `Realized P&L` / `Orders` / `Trade history`, while the remaining 50% stays open in `Positions` **with its `🛑 SL` level still intact**!
3. **Full Manual Close (`🚪 Close`) while PAUSED:**
   - Pause `Opt-2`, then click **`🚪 Close`** → **`Close Now`**.
   - **Expected:** Closes cleanly even while runner is `PAUSED`!

---

### Tab 2: `Equity` (`data-tab="equity"`)

#### `TC-3.1` · On-Demand Portfolio Equity Snapshot & Per-Runner Table
1. Click the **`Equity`** tab.
2. Click **`↻ Refresh snapshot`** (`#btn-equity-snapshot`).
3. **Expected Result:**
   - Network fires `GET /api/portfolio/equity/snapshot?mode=paper` (`200 OK`).
   - `#equity-session-stats` strip un-hides with session summary metrics.
   - `#portfolio-equity-chart` renders the equity curve canvas cleanly.
   - Per-runner table (`Runner`, `Strategy`, `Target`, `Equity`, `Realized P&L`, `Today`, `Win rate`, `Trades (today/all)`) lists **only PAPER runners**, and `Equity` / `Today` match the Strategy Instances matrix.

---

### Tab 3: `Orders` (`data-tab="orders"`)

#### `TC-3.2` · Order Ledger, Option `Lots` Column, Slippage & Filters
1. Check the **`Orders`** tab button — verify `#orders-tab-badge` shows the count badge. Click **`Orders`**.
2. Inspect rows across both Equity fills (`hf_equity_scalp` / `SBIN`) and Option leg fills (`CE_instant_buy` / `immediate_strangle`).
3. Test toolbar controls:
   - Status dropdown (`All statuses`, `⏳ Pending`, `✅ Filled`, `⛔ Rejected`, `🚫 Cancelled`)
   - Search input (`Filter symbol / instance…`)
   - `⏰ Aging only` checkbox
   - Limit selector (`50`, `100`, `200`, `500`) + `↻ Refresh`
4. **Expected Result:**
   - Every 2-leg strangle entry creates **2 leg rows** (`SELL ...CE` and `SELL ...PE`), and its exit creates **2 closing leg rows**.
   - **Option rows** display integer **`Lots`** (e.g. `1`) AND contract **`Qty`** (`lots × lot_size`, e.g. `65` or `75` for NIFTY, `30` for BANKNIFTY).
   - **Equity rows** display share **`Qty`** and `—` for `Lots`.
   - `Broker` column reads `paper`; `Ordered @`, `Filled @`, and `Slippage` are populated (no `NaN` or `undefined`).
   - **Known Bug Check (`UX-4`):** Compare the `Time` column (`HH:MM:SS`) against current IST clock — note that it currently shows **UTC** (`IST − 5h30m`).

---

### Tab 4: `Risk & intelligence` (`data-tab="risk"`) + All 5 Child Accordions

#### `TC-4.1` · Bucket Dropdown (`#risk-mode`) Bug Reproduction & Scope Check (`UX-2`)
1. Open **`Risk & intelligence`** on `/portfolio/paper`.
2. Open **DevTools → Network** (filter `Fetch/XHR`).
3. Change **`#risk-mode`** dropdown from `All Buckets` → `Paper Only` → `Live Only` → `Paper Only`.
4. **Observe & Record (`UX-2` Verification):**
   - Look at the Network requests fired by **Portfolio Intelligence** (`/api/portfolio/greeks`, `/api/portfolio/concentration`, `/api/portfolio/intelligence`, `/api/portfolio/correlation`): they **always send `?mode=paper`** regardless of dropdown selection because `portfolio_intelligence.js` hard-returns `data-mode="paper"`.
   - Look at the Network request fired by **Risk Board** (`/api/portfolio/risk`): it changes `?mode=`, updating **only** the 4 small `#risk-gauges` at the bottom of the tab!
   - Next, open `http://127.0.0.1:5000/portfolio` (combined overview where `data-mode=""`) or click **`🛡️ Open full Risk page →`** (`/risk`) and verify bucket switching there for comparison. Leave `#risk-mode` on **`Paper Only`** before proceeding!

#### `TC-4.2` · Child Section 1 — `📊 Portfolio Greeks`
1. Ensure at least one option runner (`immediate_strangle` or `CE_instant_buy`) has an open structure.
2. Inspect **`📊 Portfolio Greeks`** (`#pi-greeks`):
   - Top 4 cards: `Net Delta`, `Net Gamma`, `Net Vega`, `Net Theta` (updating every ~1s while tab is open).
   - Left table: `Breakdown by strategy instance` (`Δ`, `Γ`, `Vega`, `Θ / day`, `Pos`, `Γ share`).
   - Right table: `Scenario analysis (full revaluation)` P&L table.
   - Footer: `<N> positions · <X>/<Y> legs priced · model ... · computed in <ms>`.
3. Click the section header **`📊 Portfolio Greeks`** to collapse it, reload page (`F5`), re-open `Risk & intelligence` → verify collapsed/expanded state persisted in `localStorage`, then expand it again.

#### `TC-4.3` · Child Section 2 — `🎯 Concentration`
1. Inspect **`🎯 Concentration`** (`#pi-concentration`):
   - Left: `By underlying` exposure bars vs threshold (`alert above 60%`).
   - Right: `By strike (clustering)` table (`Strike`, `Positions`, `Types`, `Held by`, `Exposure`).
2. Verify NIFTY / BANKNIFTY strikes held in `Positions` match the strike clustering rows.

#### `TC-4.4` · Child Section 3 — `🔗 Strategy Correlation`
1. Inspect **`🔗 Strategy Correlation`** (`#pi-correlation`).
2. Switch **`Group by`** dropdown from `Runner` → `Strategy`, then click **`↻ Recalculate`**.
3. **Expected Result:** `/api/portfolio/correlation?...&refresh=1` returns `200 OK` and renders the correlation matrix/heatmap (or honest empty/insufficient-bars note if newly spawned).

#### `TC-4.5` · Child Section 4 — `🌡️ Market Regime` & Manual VIX Alert Reaction
1. Inspect **`🌡️ Market Regime`** (`#pi-regime`) and the right-hand **`Strategy regime fit`** table (note `immediate_strangle` is designed for calm VIX `10–15`).
2. In **`Set India VIX`** (`#pi-vix-value`), type **`26.5`** (High Volatility) and click **`Apply`**.
   - **Expected:** Toast confirms VIX `26.5` applied (`high_vol` regime); Regime chart & fit badges update (`immediate_strangle` shows poor/mismatch fit and pauses new strangle entries per its `vix_regime_change` subscription!).
3. Now type **`13.2`** (Calm Volatility) in **`Set India VIX`** and click **`Apply`** to restore normal regime.

#### `TC-4.6` · Child Section 5 — `📈 Market Activity (OI & liquidity)`
1. Click **`📈 Market Activity (OI & liquidity)`** (`#pi-activity` — collapsed by default!) to expand it.
2. **Expected Result:** Renders OI anomalies / spread health content without JS error.

#### `TC-4.7` · Simulated `-25%` Crash Stress Test, Circuit Breaker Halt & Recovery
1. Ensure `#risk-mode` is set to **`Paper Only`**.
2. Click **`💥 Stress Test (-25%)`** (`#btn-risk-stress`) → accept confirmation dialog.
3. **Expected Result:**
   - Top red **`🚨 CIRCUIT BREAKER HALTED`** banner (`#cb-banner`) appears at the top of the command center AND inside `#risk-cb-status`!
   - Click **`Export CSV`** (`#risk-export-csv`) → downloads `risk_audit.csv` containing the breach event.
4. Click **`Reset Breaker`** (`#btn-risk-reset-breaker`) OR top banner **`Reset & Resume`** (`#cb-reset-btn`).
   - **Expected:** Breaker clears (`✅ Normal — no breaker tripped`), halted paper runners can resume trading, and event is logged in **`Activity log`**.

---

### Tab 5: `Trade history` (`data-tab="aggregated-trades"`)

#### `TC-5.1` · Closed Trade Ledger, Commission Netting (`P3`) & Equity Qty Fix
1. Click **`Trade history`** (`data-tab="aggregated-trades"`).
2. Test filters: `All Kinds` → `Equity Only` → `Option Only`, and `Limit` (`50`, `100`, `200`). Click **`↻ Refresh`**.
3. Cross-check closed rows:
   - **Equity closed rows (`hf_equity_scalp` / `SBIN`):** Verify **`Qty` is non-zero** (actual share count closed, **NOT `0` or `0.0`** — regression check for `599be5d`!).
   - **Option closed rows (`CE_instant_buy` / `immediate_strangle`):**
     - Verify `Symbol` cell displays `🎯 <Underlying> <structure> <strikes> exp <YYYY-MM-DD>`.
     - Verify `Qty` shows `<N> lot(s) (<units> u)`.
     - **Commission Netting Check (`Phase 3`):** Check `PnL`: for a 1-lot single-leg option closed at `(exit − entry) × lot_size`, verify `PnL` deducts entry + exit brokerage (`₹20 × 2 = ₹40` for 1-leg round-trip; `₹80` for 2-leg strangle round-trip) and reconciles with `Realized P&L`!

#### `TC-5.2` · Verify `Entry Time` API vs UI Gap (`UX-1` & `UX-5`)
1. In **DevTools → Network**, inspect `GET /api/portfolio/trades/aggregated?...` response JSON.
2. Confirm each trade object in `data.trades[]` contains `entry_ts` and `exit_ts`, while the HTML table only shows `Exit Reason / Time` (raw UTC slice). Record screenshot for the UI fix ticket.

---

### Tab 6: `Playbooks` (`data-tab="playbooks"`)

#### `TC-7.1` · Default Playbooks Protection & Custom Playbook CRUD + Deploy
1. *(Workaround for `UX-6`)* Make sure you have opened and closed **`+ Add instance`** at least once since loading the page.
2. Open **`Playbooks`** tab → verify default cards (`pb_default_*`) load with structure, strikes, lots, exit summary, and `estimated` risk cap badge, and **do NOT** show a `Delete` button.
3. Click **`＋ New Playbook`** (`#btn-create-playbook`) and enter in prompts:
   - Name: `QA NIFTY Bull Spread`
   - Underlying: `NIFTY`
   - Structure: `bull_call_spread`
   - Strike selection: `atm`
   - Lots per leg: `1`
4. Verify the new card appears → click **`Edit`** to change Lots to `2` → verify card updates to `2 lot(s)`.
5. Click **`Deploy`** on `QA NIFTY Bull Spread` → verify `#spawn-modal` opens with Playbook pre-selected → ensure `Bucket mode = PAPER`, `Source = MSTOCK`, and click **`Deploy Instance`**.
6. Verify runner spawns in matrix; afterwards test **`Delete`** on `QA NIFTY Bull Spread` in `Playbooks` (existing spawned runner keeps its snapshotted playbook config!).

---

### Tab 7: `Activity log` (`data-tab="log"`)

#### `TC-8.1` · Audit Trail Completeness
1. Click **`Activity log`**.
2. **Expected Result:** Chronological audit lines exist for every operator action performed today: instance spawns, pause/resume, manual `🛑 SL` / `🎯 TP` edits, partial close, full close, `-25%` stress test breach, and breaker reset.

---

## 4. Test Suite C — Instance Deep-Dive Drawer & All 3 Child Sub-Tabs (`TC-6.x`)

Click the **`🔍`** action icon on **(a) an active Option runner (`immediate_strangle` or `banknifty_ema_momentum`)** and **(b) the Equity Pool runner (`ema_pullback` · `nifty_50`)**:

### `TC-6.1` · Drawer Header, Banners, Charts & Active Book
1. Verify header title, subtitle (`strategy · target · timeframe · status`), and 6-stat strip (`Equity`, `Daily P&L`, `Open P&L`, `Realized`, `Positions`, `Win Rate`).
2. Verify **`Instance Equity Curve`** canvas renders.
3. On a live mStock option runner that has processed ≥2 bars, verify **`Spot vs Option P&L (watch)`** twin-axis chart (`#dd-watch-section`) appears! Also click header **`Export charts`** (`#btn-watch-export`) and confirm PNG + CSV export succeeds.
4. On the Option runner, verify **`Active Option Book`** shows structure row, `% of premium`, `Bars`, and indented `↳ leg` rows.
5. On the Pool Equity runner, scroll to bottom and verify **`Universe Symbols (N)`** chip cloud renders (active held symbols highlighted).

### `TC-6.2` · Child Sub-Tab 1 (`Signal Log`) & Child Sub-Tab 2 (`Trades`)
1. Click **`Signal Log`** (`data-ddtab="signals"`) → verify live signal evaluations and `MANUAL_EXIT` entries appear with symbol + reason.
2. Click **`Trades`** (`data-ddtab="trades"`) → verify closed trades for this specific runner match its `Realized` stat (and note `UX-1`: timestamps are absent here too).

### `TC-6.3` · Child Sub-Tab 3 (`Config`) — Automatic Strategy SL/TP & Fingerprints (`UX-3`)
1. Click **`Config`** (`data-ddtab="params"`).
2. **Verify where Strategy SL/TP currently lives (`UX-3` proof):**
   - On `CE_instant_buy`: `Exit policy` reads `hold through flips, ride to settlement` (proves strategy sets **no** SL/TP!).
   - On `directional_options` / playbook runner: `Exit policy` reads `stop 50%, target 100%, square off 1d early` (proves strategy **does** have automatic SL/TP, even though `Positions` showed `—`!).
3. Verify governance metadata rows: `Code fingerprint (bundle)`, `Engine git SHA`, `Risk-limits fingerprint`, `Config fingerprint`, and `param.*` table.
4. Press **`Esc`** (and test clicking backdrop overlay + `✕`) → drawer closes cleanly.

---

## 5. Test Suite D — Phases 1–4 (`P1`–`P4`) Deep Live Plumbing Checks

Run these 6 targeted architectural checks during the live session to sign off Phases 1–4:

| Test ID | Phase | How to Execute During Live Market | Pass Criteria |
|---|---|---|---|
| **`P1-DB`** | Phase 1 (Shared DB Pool) | Check PostgreSQL active connections at start vs after 45+ mins of multi-runner paper trading:<br>`SELECT count(*), state FROM pg_stat_activity WHERE datname = current_database() GROUP BY state;` | Connection count stays flat/bounded (typically `< 15`, **never** climbing toward `99/100`). |
| **`P2-RING`** | Phase 2 (`HotStrikeRing` + WS Depth) | Watch server logs while `NIFTY` and `BANKNIFTY` option runners are active; if spot moves by ≥2 strike steps (e.g. ≥100 pts on NIFTY), observe ring recentering while an open option leg stays pinned. | Open option leg never loses live depth (`mark_stale = false`, `mtm_failures = 0`) even after ATM shifts away from entry strike. |
| **`P3-TICK`** | Phase 3 (Sub-Second Tick Exit & Fee Netting) | Deploy `immediate_strangle` on **`15min`** timeframe (so bars only close every 15 mins!). Watch `Positions` while NIFTY moves enough to hit the ±₹100 strangle bracket **mid-candle** (e.g. at `11:04`, 11 mins before the `11:15` bar close). | Structure exits **immediately mid-bar on the WS tick** (`OptionsBridge.on_tick()`), NOT waiting until `11:15`! Closed + open `daily_pnl` includes ₹20/leg option brokerage (`7b48448`). |
| **`P3-EOD`** | Phase 3 (Intraday Auto Square-Off) | Keep at least one intraday option runner open past **`15:10 IST`** (with `OPTIONS_INTRADAY_SQUAREOFF_MINUTES=20` in `.env`). | At `15:10 IST`, open intraday option structures auto-flatten cleanly before the `15:30 IST` close (`mark_stale = 0`). |
| **`P4-GATE`** | Phase 4 (Live Entitlement Gate) | Attempt to create a `mode: "live"` `immediate_strangle` via API/UI (or inspect gate response):<br>`curl -s -X POST http://127.0.0.1:5000/api/portfolio/runner/create -H "Content-Type: application/json" -d '{"strategy":"immediate_strangle","symbol":"NIFTY","timeframe":"1min","allocated_capital":100000,"mode":"live","source":"mstock"}'` | **Blocked / Refused** by `config/risk_limits.yaml` (`entitlement.live` only permits `[long_call, long_put]`; straddles/strangles remain paper-only). *(If created paused/blocked, delete immediately.)* |
| **`P4-RESTORE`** | Phase 4 + Clock Policy (`85791e7`) | With open paper positions + closed paper trades on the board, **stop Flask (`Ctrl+C`) and restart `:5000`**. Reload `/portfolio/paper`. | All paper runners restore cleanly from `portfolio_state.json` (no runner erased on restore per `a0b8e8a`), `Daily P&L` / `Realized P&L` match pre-restart numbers to the rupee, and holding times don't jump by `+330` mins (`timekeeping.py` UTC-at-rest check!). |

---

## 6. Recommended UI/UX Fix Backlog (Ready for Agent Implementation)

Based on user observations + this due-diligence audit, here is the prioritized UI/UX polish list to implement next so `/portfolio/paper` is effortless for end users:

1. **[High · `UX-3`] Surface Automatic Strategy SL / TP & Exit Policy Directly in `Positions` Table**
   - **Problem:** `Stop` and `Target` columns show `—` unless the user manually sets LOM levels, making strategies with built-in exits (`immediate_strangle` ±₹100, `directional_options` -50%/+100%, `hf_equity_scalp` -2/+3 pts) look indistinguishable from naked "hold forever" trades (`CE_instant_buy`).
   - **User-Centric Fix:** Enrich `list_positions()` / `open_structures_snapshot()` / `positions_detail()` with `auto_stop_label` & `auto_target_label` (computed effective price or `%`/`₹` rule from `ExitPlan`, e.g. `Auto ₹85.20 (-50%)` in muted pill when no manual level is set, `Manual ₹120.00` when overridden by operator, and `None (Hold)` badge when strategy has no SL/TP).
2. **[High · `UX-1` + `UX-4` + `UX-5`] Fix `Trade history` (and Deep-Dive `Trades`) Columns, IST Formatting & Bucket Scope**
   - **Problem:** `entry_ts` is omitted in `Trade history` (and both timestamps are omitted in `🔍 → Trades`), timestamps are raw UTC slices instead of IST, and `Trade history` defaults to `mode=""` (all buckets) on `/portfolio/paper`.
   - **User-Centric Fix:**
     - Add **`Entry Time (IST)`**, **`Exit Time (IST)`**, and **`Hold Duration`** (`2m 14s` / `18m`) to both `Trade history` and `🔍 Deep-Dive → Trades`.
     - Format `Orders → Time`, `Trade history`, and `Deep-Dive → Trades` in **IST (`Asia/Kolkata`)** via one shared formatter (matching `Positions`).
     - Initialize `risk_board.js` `state.mode` and `#risk-mode` from `#portfolio-page[data-mode]` so `/portfolio/paper` defaults strictly to `Paper Only`.
3. **[High · `UX-2`] Fix `Risk & intelligence` Bucket Dropdown (`#risk-mode`) Conflict**
   - **Problem:** `portfolio_intelligence.js` ignores `#risk-mode` whenever `data-mode="paper"` is set on `#portfolio-page`, while `#risk-mode` still renders as an interactive `<select>` offering `All Buckets / Paper Only / Live Only` — so top sections ignore user selection while bottom gauges change.
   - **User-Centric Fix:** Either **(a)** lock/hide `#risk-mode` to `Paper Only` badge on `/portfolio/paper` (matching the page's strict *"other buckets never appear here"* contract, keeping the dropdown active on `/portfolio` & `/risk`), **or (b)** let `#risk-mode` override `pageMode` in `portfolio_intelligence.js` and pre-select `value="paper"` on load.
4. **[Medium · `UX-6` & `UX-7`] Upgrade Playbooks Modal & Unlock Straddle Strategy Plugin**
   - Expose `loadSpawnForm` cleanly so `Playbooks → Deploy` works on cold page load without opening `+ Add instance` first; replace browser `prompt()` dialogs in `＋ New Playbook` with a proper modal supporting `long_straddle` & `short_straddle`; and (when unlocked) add `immediate_straddle` in `plugins/strategies/` for 1-click UI testing of Phase 4 ATM straddles.

---

## 7. Junior QA Sign-Off Sheet

| Module / Flow | Test IDs | Pass / Fail | Notes / Screenshots / Log Snippets |
|---|---|---|---|
| Pre-Flight (mStock Auth, WS Open, Landing Card) | §1 | `[ ]` | |
| Paper Header, Hidden Flatten & 4 Archetype Spawns | `TC-1.1` – `TC-1.5` | `[ ]` | |
| Tab 1: `Positions` + LOM (`🛑 SL`, `🎯 TP`, `◐ 50%`, `🚪 Close`) | `TC-2.1` – `TC-2.4` | `[ ]` | |
| Tab 2: `Equity` Snapshot, Chart & Runner Table | `TC-3.1` | `[ ]` | |
| Tab 3: `Orders` Ledger, Option `Lots`, Slippage & Filters | `TC-3.2` | `[ ]` | |
| Tab 4: `Risk & intelligence` + 5 Child Sections + `-25%` Stress | `TC-4.1` – `TC-4.7` | `[ ]` | |
| Tab 5: `Trade history` (Fee Netting, Equity Qty, `entry_ts` check) | `TC-5.1` – `TC-5.2` | `[ ]` | |
| Tab 6: `Playbooks` (CRUD + Deploy to Paper) | `TC-7.1` | `[ ]` | |
| Tab 7: `Activity log` Audit Trail | `TC-8.1` | `[ ]` | |
| `🔍` Deep-Dive Drawer + 3 Child Tabs (`Signal Log`, `Trades`, `Config`) | `TC-6.1` – `TC-6.3` | `[ ]` | |
| Phase 1–4 Live Plumbing (`P1-DB` to `P4-RESTORE`) | §5 | `[ ]` | |
