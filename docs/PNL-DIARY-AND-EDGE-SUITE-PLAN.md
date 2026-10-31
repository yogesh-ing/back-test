# PnL Diary & Institutional Edge Attribution Suite — Architecture & Phased Execution Plan

**Date:** 2026-10-30  
**Branch:** `arena/f7407685-back-test` (synced with `origin/feature/options-trading-dev` @ `80c2246`)  
**Scope Alignment:** Incorporates operator decisions from 2026-10-30 + `docs/OPEN-ITEMS-TRACKER.md` (`E1`, `E2`, `E3`, and `B1`/`B2` ledger-integrity gates) + `2026-10-27` Clock Policy (`src/backtest/timekeeping.py`) + `agents/UI-designer.md` precision UI rules.

---

## 1. Confirmed Product & Architectural Decisions

1. **Strict Paper vs Live Segregation (`mode = "paper" | "live"`)**
   - Every query, calendar cell, attribution row, diagnostic chart, and journal entry is strictly scoped to **one book at a time** via a top-bar `Book: Paper | Live` selector (`?mode=paper` default; `?mode=live` when navigated from `/portfolio/live`).
   - Paper and Live numbers are **never** summed or blended on `/diary`.
2. **Single `Commission` Figure — Zero Sub-Component Split (`E2` Guardrail)**
   - Every trade row, instance rollup, strategy rollup, and day summary displays **one combined `Commission` number** (`brokerage + statutory taxes` = `trades.commission_total`).
   - Per explicit operator decision (`80c2246`, 30-Oct-2026): **no separate brokerage / STT / GST / stamp columns anywhere in the UI**.
   - Row-level accounting identity: **`Gross P&L − Commission = Net P&L`** (within ₹0.05 rounding tolerance, or explicitly including `slippage_total` if non-zero). Any row violating this identity is **flagged visibly (`⚠ Ledger Mismatch`)**, never silently smoothed over.
3. **IST Exchange Clock Only (`E1` + `2026-10-27` Clock Policy)**
   - A trading day is an **IST calendar date** (`[YYYY-MM-DD 00:00:00+05:30, +1 day)` converted to aware UTC via `backtest.timekeeping` for `TIMESTAMPTZ` index range scans on `trades.exit_time`).
   - Stored timestamps remain aware UTC at rest; every UI timestamp, session bucket, and hold duration renders in IST with `tabular-nums`.
   - **`Running MTM`** (`unrealized_pnl` on open equity positions and open option structures) is read live from `PortfolioManager` **for today's IST session only** (gross mark; exit fees booked only on close per `E1`/`E3`), and is `₹0.00 / —` on historical closed sessions.
4. **Strategy → Spawned Instance → Trade Attribution (`Requirement #2` + Retired-Runner Preservation)**
   - Attribution reads **persisted `portfolios` JOIN `trades`** merged idempotently with **in-memory `PortfolioManager` runners** (for not-yet-flushed closes and active runner config metadata).
   - **Critical architectural rule:** Retired/deleted runners remain in `portfolios` + `trades`, so historical diary days retain 100% of past spawned instances (`"<runner_name> [<instance_id8>]"`), while active runners additionally surface live `Running MTM` and rich config pills (`Symbol/Pool`, `Timeframe`, `Carry Policy`, `SL / TP`, `Lots`).
5. **Net-First Win & Edge Definitions**
   - A trade is a winner **iff `net_pnl > 0`** (matching `Trade.is_winner` in `src/backtest/db/models.py`, fixing the legacy `gross >= 0` distortion where fee-negative scalps appeared as wins).

---

## 2. Screen Architecture (`/diary` — Dedicated Workspace View)

Placed under **Workspace** in `base.html` (`Portfolio` → **`PnL Diary`** → `Analytics` → `Risk management` → `P&L reports`), with deep-link buttons on `/portfolio/paper` (`/diary?mode=paper`) and `/portfolio/live` (`/diary?mode=live`).

```text
+---------------------------------------------------------------------------------------------------+
| Workspace > PnL Diary   [ Book: PAPER v ] [ Strategy: All Strategies v ] [ Month: Oct 2026 < > ]  |
+---------------------------------------------------------------------------------------------------+
| [ Day Scope: 30 Oct 2026 IST ] [ Day Start Anchor ] [ Realized Net ] [ Running MTM ]              |
| [ Gross P&L ]                  [ Commission (1 sum) ] [ Trades / Net Win% ] [ Ledger: VERIFIED ]  |
+---------------------------------------------------------------------------------------------------+
| IST SESSION CALENDAR HEATMAP (Click day or Shift-click range; reacts to Strategy filter)          |
| Mon   Tue   Wed   Thu   Fri  | Weekly Net Strip + Streak / Best / Worst Session Chips             |
+---------------------------------------------------------------------------------------------------+
| VIEW TABS: [ 1. Strategy & Instances ] [ 2. Exit & Time Regimes ] [ 3. Hold & Payoff Edge ]       |
|            [ 4. Live Graduation & Journal ]                                                       |
|---------------------------------------------------------------------------------------------------|
| > Strategy Row (e.g., immediate_strangle)   Net | Running | Gross | Comm | Trades | Win% | PF     |
|   > Spawned Instance [a1b2c3d4] NIFTY 1h intraday SL/TP ...                                       |
|     - Trade / Structure rows (IST Entry, Exit, Hold, Exit Reason, Gross - Comm = Net)             |
+---------------------------------------------------------------------------------------------------+
```

**Design-System Compliance (`agents/UI-designer.md` & `tokens.css`):**
- Inter + JetBrains Mono with `font-variant-numeric: tabular-nums slashed-zero` on every price, P&L, percentage, count, and timestamp.
- Compact 28–32px table rows, sticky headers, zero layout-shift on refresh, `--success` / `--danger` always paired with explicit `+` / `−` signs (colorblind-safe), and zero `transition: all`.

---

## 3. Bite-Sized Execution Slices ("One at a Time" Roadmap)

Each slice below is **independently shippable, end-to-end testable, and gated by automated pytest + UI contract tests**. We execute **one slice at a time**, verify it together, commit, and move to the next.

---

### Slice 0 — Ledger Truth & `E1` / `E2` Foundation Gate *(Prerequisite Backend Integrity)*

**Why first:** `OPEN-ITEMS-TRACKER.md` (`80c2246`) explicitly notes two ledger hazards that would corrupt any diary if left unguarded:
1. **`B1` (Restart Duplicate Re-Inserts):** `LiveTradePersister._flushed` (`src/backtest/forward/trade_persistence.py:42`) is in-memory only; on server restart, restored `runner.closed_trades` get re-inserted into `trades`.
2. **`mode="paper"` Hardcoding Bug:** `LiveTradePersister._ensure_portfolio_row()` (`trade_persistence.py:138`) stamps `mode="paper"` even when `runner.config.mode == "live"`, risking Live trades landing in the Paper DB book.
3. **`B2` / `E2` (`Gross − Commission = Net` Visibility):** Portfolio trade history needs the single `Commission` column visible, and mismatches must be flagged rather than hidden.

**Deliverables:**
- `src/backtest/timekeeping.py`: add `ist_session_bounds_utc(start_date: date, end_date: date | None = None) -> tuple[datetime, datetime]` returning aware UTC `[00:00:00+05:30, +1d 00:00:00+05:30)` for index-friendly `TIMESTAMPTZ` range queries without SQL timezone drift across Postgres vs SQLite.
- `src/backtest/forward/trade_persistence.py`:
  - Stamp `mode = str(getattr(cfg, "mode", "paper") or "paper").lower()` (validated against `("paper", "live")`) on `PortfolioRow` creation.
  - Seed `self._flushed[runner.instance_id]` from existing `trades` rows for `portfolio_id` on first flush after boot so restored `closed_trades` **never** duplicate in DB across restarts.
  - Provide canonical `trade_dedupe_signature(...)` shared by writer and reader so historical 28-Oct duplicate rows in Postgres are collapsed deterministically on read (reporting raw vs deduped row count in ledger diagnostics).
- `E1` / `E2` Portfolio Surface Alignment:
  - Expose single `commission` (`brokerage + statutory`) on `/api/portfolio/trades/aggregated` and render the `Commission` column in `Positions / Trades` so `Gross − Commission = Net` holds visibly on every row (`E2`), with day-scope label (`Today · DD-Mon IST`) on daily P&L tiles (`E1`).
- **Tests:** `tests/diary/test_slice0_ledger_truth.py` (restart idempotency, `mode="live"` vs `"paper"` persistence isolation, historical duplicate collapse, `Gross − Commission = Net` mismatch detection, clock-policy AST test green).

---

### Slice 1 — `/diary` Route, Paper/Live Switcher, Day Summary Strip & IST Calendar Heatmap

**Goal:** Stand up the `/diary` page, strict Paper/Live book switcher, executive day/range KPI strip, and interactive IST Session Calendar Heatmap.

**Deliverables:**
- **Backend (`src/backtest/diary/service.py` & `src/backtest/api/diary.py`):**
  - `GET /api/diary/calendar?mode=paper|live&from=YYYY-MM-DD&to=YYYY-MM-DD&strategy=<optional>`
    - Returns per-IST-session aggregates: `date`, `gross_pnl`, `commission`, `net_pnl`, `trade_count`, `win_count`, `net_win_rate`, `active_strategies`, `active_instances`, `mismatch_count`, `deduped_duplicates_collapsed`, plus `is_holiday` / `holiday_name`.
  - `GET /api/diary/summary?mode=paper|live&from=YYYY-MM-DD&to=YYYY-MM-DD&strategy=<optional>`
    - Returns executive strip metrics: `scope_label`, `day_start_anchor` (when single day = today/snapshot available), `realized_net_pnl`, `running_mtm` (live from `PortfolioManager` for today's IST date only; `null` on past dates), `total_daily_pnl` (`realized_net + running_mtm` for today), `gross_pnl`, `commission` (single sum), `trade_count`, `net_win_rate`, `profit_factor`, `best_day`, `worst_day`, `current_streak`, and `ledger_health` (`verified` | `deduped_legacy` | `mismatch_warning`).
- **Frontend (`src/backtest/web/templates/diary.html`, `static/js/diary.js`, `static/css/diary.css`):**
  - Add `/diary` to `src/backtest/web/app.py`, `base.html` (`Workspace` group + command palette), and `tests/test_workspace_design.py` (`PAGES`).
  - Add `"Open PnL Diary"` quick-action button on `/portfolio/paper` (`/diary?mode=paper`) and `/portfolio/live` (`/diary?mode=live`).
  - **Top Control Bar:** `Book: Paper | Live` dropdown, `Strategy` filter (`All Strategies` or specific `strategy_name`), Month/Quarter navigator + quick range pills (`Today`, `This Week`, `This Month`, `Last 30 Sessions`).
  - **IST Session Calendar Heatmap:** Mon–Fri trading grid (with optional Sat/special-session badge if trades exist), intensity-scaled `--success` / `--danger` cells with explicit `+₹...` / `−₹...` net label + trade count micro-badge, click single day or Shift-click range, plus weekly right-hand rollup column.
- **Tests:** `tests/diary/test_slice1_calendar_and_summary.py` + `tests/test_workspace_design.py`.

---

### Slice 2 — 3-Level Strategy → Spawned Instance → Trade/Structure Attribution Hierarchy

**Goal:** Deliver your primary workflow (#2) — full context on **which strategies and which spawned runner instances** are making or losing money on the selected IST day or date range.

**Deliverables:**
- **Backend (`GET /api/diary/attribution?mode=paper|live&from=YYYY-MM-DD&to=YYYY-MM-DD&strategy=<optional>`):**
  - **Level 1 — Strategy Rollup (`strategy_name`):**
    - `realized_net_pnl`, `running_mtm` (today), `gross_pnl`, `commission` (single sum), `commission_drag_pct` (`commission / abs(gross_pnl)` when `gross_pnl != 0`), `trade_count`, `instance_count`, `net_win_rate`, `profit_factor`, `expectancy_net`, `avg_win_net`, `avg_loss_net`, `max_win_net`, `max_loss_net`.
  - **Level 2 — Spawned Instances under each Strategy (`portfolio_id` / `instance_id`):**
    - Merges persisted `portfolios` rows (`"<name> [<id8>]"`) with live `PortfolioManager` runner metadata when active (`status`: `RUNNING | PAUSED | BLOCKED_BY_RISK | RETIRED`, `symbols`, `timeframe`, `carry_policy`, `allocated_capital`, key `SL/TP/lots` params) + per-instance metrics (`realized_net_pnl`, `running_mtm`, `gross_pnl`, `commission`, `trades`, `net_win_rate`, `profit_factor`).
    - Guarantee: **Sum of Instances == Strategy Row**, and **Sum of Strategies == Day Summary Tile** to the paisa (verified on the 30-Oct 13-trade benchmark in `E3`).
  - **Level 3 — Drilldown Trades & Open Structures (`GET /api/diary/instance/<portfolio_or_instance_id>/trades?...` or inline for single-day `<500` rows):**
    - Deduped closed trades + today's open positions/structures with IST `entry_time`, IST `exit_time`, `hold_duration`, `symbol`, `side`, `qty`, `entry_price`, `exit_price`, `exit_reason`, `gross_pnl`, `commission` (single sum), `net_pnl`, and `reconcile_ok` (`abs((gross - commission - slippage) - net) <= 0.05`).
- **Frontend (`diary.js` Tab 1 — `Strategy & Instances`):**
  - Keyboard-accessible expandable 3-level data grid (`tabular-nums`, column sorting by `Realized Net`, `Commission`, `Trades`, `Win %`, `Profit Factor`), inline PnL contribution bar, instance config badges, and instant filter-to-strategy click sync with the Calendar Heatmap.
- **Tests:** `tests/diary/test_slice2_strategy_instance_attribution.py` (hierarchical sum invariants, retired-instance visibility, active running-MTM attachment on today only, `E3` reconciliation).

---

### Slice 3 — Institutional Edge Diagnostics I: Exit-Reason Attribution & NSE Intraday Regime Matrix

**Goal:** Answer *why* a strategy won or bled on NSE — was it stopped out on noise, eaten alive on tight TPs, flattened at `15:10 IST`, or bleeding in midday chop?

**Deliverables:**
- **Backend (`GET /api/diary/edge/regimes?mode=paper|live&from=YYYY-MM-DD&to=YYYY-MM-DD&strategy=<optional>`):**
  - **Matrix A — `Strategy × Exit Reason` Attribution:**
    - Canonical buckets: `take_profit`, `stop_loss`, `trailing_stop`, `eod_flat`, `time_stop`, `signal`, `risk_limit`, `manual`.
    - Per cell: `trades`, `net_pnl`, `gross_pnl`, `commission`, `avg_net_pnl`, `net_win_rate`.
    - Automated **Diagnostic Callout** rules (deterministic math, zero LLM fluff): e.g., *"TP Net-Negative Alert: `take_profit` exits averaged +₹X gross but −₹Y net after ₹Z commission — widen TP above fee floor"* or *"EOD Bleed: `eod_flat` accounted for X% of session losses"*.
  - **Matrix B — `Strategy × NSE Intraday Session Regime` (IST Entry-Time Buckets, toggleable to Exit-Time):**
    - `09:15–09:45 IST` (*Opening Drive / Volatility*)
    - `09:45–11:30 IST` (*Morning Trend*)
    - `11:30–13:30 IST` (*Midday Chop / Lull*)
    - `13:30–15:10 IST` (*Afternoon / Expiry Gamma*)
    - `15:10–15:30 IST` (*Square-Off / Closing Window*)
    - Per cell: `trades`, `net_pnl`, `net_win_rate`, `expectancy_net`, `commission`.
- **Frontend (`diary.js` Tab 2 — `Exit & Time Regimes`):**
  - Dense institutional 2D heat-tables for `Strategy × Exit Reason` and `Strategy × IST Session Window`, toggleable by `All Strategies` vs single strategy or spawned instance, plus actionable rule callout cards.
- **Tests:** `tests/diary/test_slice3_exit_and_session_regimes.py` (IST bucket boundaries on aware UTC timestamps, exact sum match against day total).

---

### Slice 4 — Institutional Edge Diagnostics II: Hold-Duration / Commission-Drag & Expectancy Quadrant

**Goal:** Quantify overtrading/friction drag and pinpoint whether each spawned runner needs **better entry selectivity** (higher Win %) or **better exit asymmetry** (higher Avg Win / Avg Loss).

**Deliverables:**
- **Backend (`GET /api/diary/edge/payoff?mode=paper|live&from=YYYY-MM-DD&to=YYYY-MM-DD&strategy=<optional>`):**
  - **Hold-Duration & Friction Breakdown:**
    - Buckets computed from exact `(exit_time - entry_time).total_seconds()`: `<1m` (*Churn Watch*), `1–5m` (*Scalp*), `5–15m`, `15–60m`, `60m+`.
    - Per-bucket & per-instance **Commission Drag Ratio** = `commission / max(abs(gross_pnl), 1)` + **Fee Breakeven Hurdle** (`avg_commission_per_trade`).
  - **Expectancy & Payoff Quadrant Coordinates (per Strategy & Spawned Instance):**
    - `net_win_rate` ($W \in [0, 1]$), `payoff_ratio` ($R = \text{Avg Net Win} / |\text{Avg Net Loss}|$), `net_expectancy` ($E = W \cdot \text{AvgWin} - (1-W) \cdot |\text{AvgLoss}|$), `trade_count`, `profit_factor`.
    - Deterministic **Edge Prescription Tag**:
      - `POSITIVE_EDGE` ($E > 0, n \ge 10$)
      - `CHURN_FRICTION_DRAG` ($\text{Gross} > 0 \land \text{Net} \le 0$)
      - `WIDEN_TP_OR_CUT_SL` ($E \le 0, W \ge 50\%, R < \frac{1-W}{W}$)
      - `TIGHTEN_ENTRY_FILTER` ($E \le 0, W < 40\%$)
      - `LOW_SAMPLE` ($n < 10$)
- **Frontend (`diary.js` Tab 3 — `Hold & Payoff Edge`):**
  - Hold-Duration table + Commission-Drag bar meter (`<30%` healthy, `30–50%` warning, `>50%` critical).
  - Interactive local Chart.js / SVG **Win % vs Payoff Ratio Scatter + Hyperbolic Breakeven Curve ($R = \frac{1-W}{W}$)** where hovering/clicking any instance bubble highlights its exact stats and prescription.
- **Tests:** `tests/diary/test_slice4_hold_drag_and_quadrant.py`.

---

### Slice 5 — Paper → Live Graduation Scorecard & Persistent Operator Journal

**Goal:** Close the institutional feedback loop — systematically decide **which Paper instances have earned Live capital** and persist concise human market-regime notes alongside daily/instance P&L.

**Deliverables:**
- **Schema Gate (`BUG-38` Clean Migration Prerequisite):**
  - Unfork duplicate Alembic `020` (`20261013_1200_020_add_upstox_source_to_portfolios.py` → `021`, `down_revision = "020"`) + add single-head invariant check, then add clean migration `022_diary_journal_notes` (+ SQLite/Postgres parity in `src/backtest/db/models.py`).
- **Journal Model (`DiaryNote` in `models.py`):**
  - `note_id` (`UUIDStr` PK), `mode` (`'paper' | 'live'`), `session_date` (`Date`, IST date), `strategy_name` (`String(64)`, nullable for day-level notes), `portfolio_id` (`UUIDStr`, nullable for instance-level notes), `regime_tags` (`JSONVariant`: `["Trend", "Chop", "Expiry", "Gap", "Event", "HighVIX", "RuleBreach"]`), `note_text` (`Text`), `created_at` / `updated_at` (`DateTime(timezone=True)` via `now_utc()`).
- **Backend (`GET /api/diary/graduation`, `GET/POST/DELETE /api/diary/notes`):**
  - **Paper → Live Graduation Scorecard** evaluating each Paper instance over the selected lookback (default 20 trading sessions) against explicit institutional gates:
    1. Sample size $n \ge 20$ closed trades
    2. Net Expectancy $> ₹0$
    3. Net Profit Factor $\ge 1.35$
    4. Commission Drag $\le 35\%$ of Gross Profit
    5. Max Intraday Drawdown $\le$ configured instance/bucket risk cap
    6. Zero unresolved `Gross − Commission ≠ Net` ledger mismatches
    - Outputs verdict: **`🟢 PROMOTE CANDIDATE`**, **`🟡 TUNE EDGE`**, or **`🔴 RETIRE / KILL`** with exact pass/fail checklist per gate.
- **Frontend (`diary.js` Tab 4 — `Graduation & Journal` + Day Header Quick-Note Drawer):**
  - Graduation scorecard table with gate pills + 1-click Day/Instance Regime Tags & Markdown/plain-text journal note editor.
- **Tests:** `tests/diary/test_slice5_graduation_and_journal.py` + Alembic single-head test.

---

## 4. Non-Negotiable Guardrails Across All 6 Slices

1. **Protected Runtime Files Untouched:** Never touch `src/backtest/data/mticker.py`, `tests/test_mticker_ws_fixes.py`, or `entitlement.live` in `config/risk_limits.yaml`.
2. **Clock Policy (`tests/test_timekeeping_policy.py`):** Zero `datetime.utcnow()`, zero bare `datetime.now()`, zero `.replace(tzinfo=None)` — every timestamp boundary goes through `src/backtest/timekeeping.py`.
3. **Database Hygiene:** Never leak connections (`with db.session() as session:`), never touch live `.env` Postgres in pytest (`sqlite:///:memory:` / `tmp_path` SQLite + `PYTEST_CURRENT_TEST` isolation), and ensure identical behavior on PostgreSQL and SQLite.
4. **Reconciliation Invariant:** On every `/diary` response, `sum(strategy.realized_net_pnl) == summary.realized_net_pnl` and `sum(instance.realized_net_pnl) == strategy.realized_net_pnl` to `₹0.01`, asserted in CI tests.

---

## 5. Recommended Execution Order

| Order | Slice | Primary Value Delivered | Est. Scope |
|---:|---|---|---|
| **1st** | **Slice 0** | Fixes `LiveTradePersister` restart duplicates (`B1`) + `mode="live"` persistence tag + visible single `Commission` (`E2`) & IST day bounds (`E1`) | Small, surgical backend + test gate |
| **2nd** | **Slice 1** | `/diary` route + `Paper | Live` switcher + Day Summary Strip + interactive **IST Calendar Heatmap** | New page + `/api/diary/{calendar,summary}` |
| **3rd** | **Slice 2** | **3-Level Strategy → Spawned Instance → Trade Attribution Tree** (including retired runners + live Running MTM on today) | Core attribution table + `/api/diary/attribution` |
| **4th** | **Slice 3** | **`Strategy × Exit Reason`** + **`Strategy × NSE Intraday Time Regime`** matrices & automated rule callouts | Institutional Diagnostics I |
| **5th** | **Slice 4** | **Hold-Duration / Commission-Drag Analyzer** + **Win % vs Payoff Breakeven Quadrant** | Institutional Diagnostics II |
| **6th** | **Slice 5** | **Paper → Live Graduation Scorecard** + persistent **Day/Instance Regime Tags & Journal Notes** (unforks Alembic `020` → `021` + `022`) | Promotion governance + DB journal |
