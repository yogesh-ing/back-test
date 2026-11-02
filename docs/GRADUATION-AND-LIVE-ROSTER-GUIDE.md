# Graduation Scorecard & Constrained Live Roster Optimizer — Quant Architect Guide

**UI Surface**: `/diary` → Tab 4 (`Graduation & Journal`) · In-App Manual: `Institutional Edge Playbook` (`Roster Tournament` tab)  
**Backend Modules**: `src/backtest/diary/graduation.py`, `src/backtest/diary/roster_selector.py`, `src/backtest/diary/service.py`, `src/backtest/api/diary.py`  
**Frontend Modules**: `src/backtest/web/static/js/diary.js`, `src/backtest/web/templates/diary.html`, `src/backtest/web/templates/components/edge_playbook_modal.html`

---

## 1. Architectural Quarantine & Zero-Touch Execution Guarantee

The Graduation Scorecard and Constrained Live Roster Optimizer operate **100% read-only against historical Backtest, Paper, and Live trade ledgers** (with optional write-only operator audit notes in `diary_notes`):

- **Zero-Touch Execution Invariant**: No Paper/Live runner loop (`forward_test.py`, `portfolio_manager.py`), broker execution adapter (`zerodha.py`, `mstock.py`), strategy base/plugin (`strategy/*.py`, `plugins/strategies/*.py`), or trading execution API (`api/forward.py`, `api/portfolio.py`, `api/live.py`) is modified or coupled to the optimizer.
- **Strict Book Segregation**: Paper (`?mode=paper`) and Live (`?mode=live`) diaries are evaluated independently and never blended.
- **Single-Sum Commission Rule (`E2`)**: Every ledger row, scorecard cell, correlation vector, and journal snapshot enforces `Gross P&L − Commission = Net P&L` (exact `2dp`, `±₹0.02` reconciliation tolerance) with `Commission` presented as one combined sum.
- **Exchange Clock Authority**: All session dates (`active_session_days`, daily P&L vectors, worst-red-day tail sets) are anchored strictly to **`Asia/Kolkata` (`IST`, `+05:30`)** via `src/backtest/timekeeping.py`.

---

## 2. Layer 1 — Individual 6-Gate Graduation Scorecard (`graduation.py`)

Before any Paper instance is considered for Live capital, it must prove **standalone statistical viability** across 6 deterministic gates:

| Gate # | Gate ID | Threshold | Purpose |
| :---: | :--- | :--- | :--- |
| **G1** | `min_trades` | `N ≥ 20` closed trades | Prevents small-sample coin-flip promotions (`5/5` lucky starts). |
| **G2** | `net_expectancy` | `E[net] > ₹0.00 / trade` | Confirms positive mathematical expectation **after** full brokerage + statutory drag. |
| **G3** | `profit_factor` | `PF_net ≥ 1.35×` (or unbounded `Losses = ₹0`) | Ensures cushion against live fill slippage (`1.02×` Paper edges go negative in Live). |
| **G4** | `commission_drag` | `Gross > ₹0` and `Commission / Gross ≤ 35.0%` | Blocks high-turnover broker-feeding scalpers whose edge evaporates on wider spreads. |
| **G5** | `max_drawdown` | `MDD ≤ 10.0%` of initial capital (or `≤ ₹25,000` fallback) | Caps peak-to-trough realized equity pain before live deployment. |
| **G6** | `clean_execution` | `0` ledger anomalies & status `≠ BLOCKED_BY_RISK` | Confirms accounting integrity and clean circuit-breaker history. |

### Non-Breaking Temporal & Trajectory Telemetry on Every Graduation Row
Alongside the 6 gates (`gates_total == 6`), `evaluate_instance_graduation()` attaches two structural diagnostics surfaced as inline pills on both the 6-Gate Scorecard and the Live Roster table:

1. **Active IST Session Breadth (`count_active_ist_sessions`)**:
   - Counts distinct `Asia/Kolkata` (`IST`) trading dates (`exit_date_ist` / `entry_date_ist`) across the instance's closed trades.
   - Surfaces `Sessions: Xd` (highlighted amber when `N ≥ 20` trades occurred in `< 5` IST days).
2. **Chronological Half-Split Edge Trajectory (`evaluate_edge_trajectory`)**:
   - Sorts trades chronologically by `exit_ts_utc` (`entry_ts_utc` fallback) and splits at `midpoint = N // 2` into **First Half (`H1`)** vs **Recent Half (`H2`)** once `N ≥ 10`:
     - **`STEADY_OR_EXPANDING` (`H2 Steady`)**: `E[H2] > ₹0` and `E[H2] ≥ 40%` of `E[H1]`.
     - **`COOLING_RECENT_EDGE` (`H2 Cooling`)**: `H1 > ₹0`, `E[H2] > ₹0`, and `N ≥ 12`, but `E[H2] < 40% × E[H1]` (edge compressing as regime shifts).
     - **`DECAYING_RECENT_EDGE` (`H2 Decay`)**: `H1 > ₹0`, `N ≥ 12`, and **`E[H2] ≤ ₹0`** — classic *"front-loaded luck"* where week-1 gains mask a losing strategy in recent sessions.
     - **`NEGATIVE_BOTH_HALVES`**: `H1 ≤ ₹0` and `H2 ≤ ₹0`.

---

## 3. Layer 2 — 5-Stage Constrained Live Roster Tournament (`roster_selector.py`)

When 15–20 Paper instances run for 30–40 sessions and 8–12 pass all `6/6` gates, promoting all of them simultaneously causes **margin starvation, correlated index drawdowns, and multi-timeframe leverage stacking**.

`build_live_roster_plan()` (`GET /api/diary/roster-plan`) runs a deterministic 5-stage tournament to select the optimal `K` Live runners and assign every remaining runner an explicit tournament decision code:

### Stage 1 — Qualification, Entitlement, Session Breadth, H2 Decay & Sim-to-Real Haircut
Candidates failing any hard structural safeguard are filtered before capital allocation:
1. **`DISQUALIFIED_GATES`**: Fewer than `6/6` Graduation Gates passed (`verdict != PROMOTE_CANDIDATE`).
2. **`HOLD_ENTITLEMENT_REVIEW`**: Uses option structures outside baseline `config/risk_limits.yaml` `entitlement.live` (`[long_call, long_put]`).
3. **`HOLD_SESSION_BREADTH`** *(Safeguard #1)*: `active_session_days < min_session_days` (default **`5` IST days**). Prevents a 1-minute scalper that fired 25 trades on one trendy session from claiming Live capital before surviving chop/gap/expiry sessions.
4. **`HOLD_RECENT_DECAY`** *(Safeguard #2)*: When `block_decaying_edge=True` (default **ON**), holds candidates flagged `DECAYING_RECENT_EDGE` (`H1 > ₹0` followed by `E[H2] ≤ ₹0` across `N ≥ 12` trades).
5. **`DISQUALIFIED_HAIRCUT`**: Matched against historical `backtest_runs` (`(strategy_name, canonical_underlying, timeframe)` fallback to `(strategy_name, canonical_underlying)`). If `BT PF ≥ 1.50×` collapses to `< 45%` PF retention in Paper (`SEVERE_DECAY`), the instance is flagged as curve-fit.

### Stage 2 — Composite Capital-Efficiency Score (`0–100`)
All runners are ranked by **risk-adjusted, friction-adjusted, trajectory-adjusted efficiency** rather than raw rupee P&L:

$$\text{Score} = \text{clamp}_{[0,100]}\!\Big(S_{\text{R/DD}} + S_{\text{PF}} + S_{\text{Drag}} + S_{\text{Sim}} + P_{\text{H2}}\Big)$$

- **$S_{\text{R/DD}}$ (`0–40 pts`)**: `min(40, (Net P&L / max(MDD, 2% × Capital)) / 4.0 × 40)` — rewards smooth return-over-drawdown (`4.0×` R/DD earns full `40` pts).
- **$S_{\text{PF}}$ (`0–25 pts`)**: `min(25, max(0, PF_net − 1.0) / 1.5 × 25)` — full `25` pts at `PF ≥ 2.50×`.
- **$S_{\text{Drag}}$ (`0–15 pts`)**: `max(0, (1 − Drag% / 50%) × 15)` — rewards wide gross edge over brokerage/taxes.
- **$S_{\text{Sim}}$ (`0–20 pts`)**: Up to `12` pts for Backtest-to-Paper PF retention (`≥ 75%` full credit; neutral `7.5` pts when Paper-Only) + up to `8` pts for trade sample depth (`min(8, N / 50 × 8)`).
- **$P_{\text{H2}}$ (`0`, `−6`, or `−15 pts`)**: Trajectory adjustment (`−6.0` pts for `COOLING_RECENT_EDGE`, `−15.0` pts for `DECAYING_RECENT_EDGE`).

### Stage 3 — Multi-Timeframe Family Deduplication (`dedupe_timeframes = True`)
- Groups candidates by `(strategy_name, canonical_underlying)` (where `NIFTY26NOV24500CE` and `NIFTY` both canonicalize to **`NIFTY`**).
- Enforces **Winner-Take-All** unless one runner is a fast intraday scalper (`1m` / `3m` / `5m`), the other is a higher-timeframe swing runner (`≥ 1h` / `1day`), **and** their daily IST Net P&L correlation is `< 0.50`. Redundant timeframes are marked **`PRUNED_TIMEFRAME_DUPLICATE`**.

### Stage 4 — Daily IST P&L De-Correlation (`ρ`) & Worst-Red-Day Tail Co-Crash Guard
Iterating down the ranked candidates against already-selected Live Roster winners:
1. **Normal-Regime Linear Correlation (`PRUNED_CORRELATED_CLONE`)**: Computes pairwise Pearson `ρ` across the union of active IST dates (requiring `≥ 5` active days). Prunes any candidate with `ρ > max_correlation` (default **`0.65`**).
2. **Tail Co-Crash Overlap (`PRUNED_TAIL_CRASH_OVERLAP`)** *(Safeguard #3)*: Linear correlation often looks benign (`ρ ≈ 0.30–0.45`) on quiet days while spiking to `1.0` during gap/VIX shocks. `compute_tail_crash_overlap()` extracts each runner's **3 worst losing IST session days** (`daily_pnl < ₹0`, requiring `≥ 3` red days on both runners) and prunes any candidate sharing **`≥ max_tail_overlap` (default `3 / 3`)** worst crash dates with an already-selected winner.

### Stage 5 — Cash Reserve, Underlying Concentration & Slot Knapsack
- **Margin Buffer**: Locks `reserve_pct` (default **`20%`**) of `live_capital` in unallocated cash (`₹3,00,000 → ₹2,40,000` deployable).
- **Single-Underlying Cap (`DEFERRED_UNDERLYING_CAP`)**: Caps cumulative capital on any single canonical underlying (`NIFTY`, `BANKNIFTY`, `RELIANCE`, etc.) at `max_underlying_pct` (default **`40%`** of total Live capital).
- **Slot & Capital Knapsack (`DEFERRED_SLOT_CAP`, `DEFERRED_CAPITAL_CAP`)**: Caps concurrent Live runners at `max_live_runners` (default **`5`**) and deployable budget. Qualified runners that do not fit are placed on the **Warm Paper Bench** for instant rotation if a Live runner degrades.
- **Staged Rollout Waves**: Selected slots `#1–#2` are tagged **`Wave 1`** (deploy first for 5 Live sessions at `1×` lot); slots `#3–#K` are tagged **`Wave 2`**.

---

## 4. Complete Tournament Decision Code Reference (12 Codes)

| Decision Code | UI Badge | Bucket | Meaning & Operator Action |
| :--- | :--- | :--- | :--- |
| `SELECTED_LIVE_ROSTER` | `#k Live Roster · Wave 1/2` | Selected | Passed all 6 gates + 5 tournament stages. Deploy in Wave 1 (`#1–#2`) or Wave 2 (`#3+`) at `1×` minimum lot. |
| `DEFERRED_SLOT_CAP` | `Warm Bench · Slot Cap` | Warm Bench | Qualified & uncorrelated, but `max_live_runners` slots are full. Keep running in Paper as #1 backup. |
| `DEFERRED_UNDERLYING_CAP` | `Warm Bench · Underlying Cap` | Warm Bench | Qualified, but adding it breaches `max_underlying_pct` on that index/stock. |
| `DEFERRED_CAPITAL_CAP` | `Warm Bench · Capital Cap` | Warm Bench | Qualified, but required capital exceeds remaining deployable budget after `reserve_pct`. |
| `PRUNED_TIMEFRAME_DUPLICATE` | `Pruned · Same Strat+Symbol TF` | Pruned | Outscored by a stronger timeframe of the same `(Strategy, Underlying)` family. |
| `PRUNED_CORRELATED_CLONE` | `Pruned · Corr ρ > Cap` | Pruned | Daily IST Net P&L Pearson `ρ > max_correlation` against a higher-ranked winner. |
| `PRUNED_TAIL_CRASH_OVERLAP` | `Pruned · Tail Co-Crash` | Pruned | Shares `3/3` worst losing IST crash days with a higher-ranked winner despite acceptable linear `ρ`. |
| `HOLD_SESSION_BREADTH` | `Hold · Few IST Sessions` | Held | Passed 6 gates on trade count, but across `< min_session_days` IST sessions. Accumulate more multi-day history. |
| `HOLD_RECENT_DECAY` | `Hold · Recent Half Decay` | Held | Positive overall solely from `H1` luck while recent half `E[H2] ≤ ₹0`. Keep in Paper until `H2` recovers. |
| `HOLD_ENTITLEMENT_REVIEW` | `Hold · Live Entitlement` | Held | Uses multi-leg/short option structures requiring explicit live entitlement sign-off. |
| `DISQUALIFIED_HAIRCUT` | `Disqualified · BT Decay` | Disqualified | Paper PF collapsed `< 45%` of Backtest PF (`≥ 1.50×`). Re-examine slippage/overfitting in `/backtest`. |
| `DISQUALIFIED_GATES` | `Not Graduated` | Disqualified | Has not yet cleared all `6/6` individual Graduation Gates. |

---

## 5. API & Operator Journal Audit Trail Reference

### `GET /api/diary/roster-plan`

| Query Parameter | Default | Clamped Bounds | Description |
| :--- | :---: | :---: | :--- |
| `mode` | `paper` | `paper \| live` | Segregated diary book to evaluate. |
| `from`, `to` | Current IST month | `≤ 400` days | Inclusive `YYYY-MM-DD` IST session window. |
| `strategy` | *(all)* | string | Optional strategy family filter. |
| `live_capital` | `300000` | `₹25,000 – ₹10,00,00,000` | Total Live trading budget in INR. |
| `reserve_pct` | `20` | `0% – 80%` | Cash & intraday margin buffer held out of allocation. |
| `max_underlying_pct` | `40` | `10% – 100%` | Max share of total Live capital on one canonical underlying. |
| `max_correlation` | `0.65` | `0.10 – 1.00` | Max pairwise daily IST Net P&L Pearson `ρ`. |
| `max_live_runners` | `5` | `1 – 25` | Max concurrent Live runners selected. |
| `min_session_days` | `5` | `1 – 60` | Minimum distinct active IST sessions required for Live selection. |
| `dedupe_timeframes` | `true` | boolean | Winner-Take-All across timeframes on same `(Strategy, Underlying)`. |
| `block_decaying_edge` | `true` | boolean | Hold `DECAYING_RECENT_EDGE` (`E[H2] ≤ ₹0`) candidates out of Live selection. |
| `max_tail_overlap` | `3` | `1 – 3` | Max shared worst losing IST days (out of 3) allowed with a selected winner. |

### Safeguard #4 — One-Click Promotion Audit Trail (`Log Roster to Journal`)
Clicking **`Log Roster to Journal`** (`#diary-roster-log-journal`) calls `PnLDiary.buildRosterJournalNotePayload(rosterData, sessionDate)` and posts to `POST /api/diary/notes` with tags `["RosterPlan", "Graduation"]`. This records an immutable timestamped snapshot of:
1. Window dates, Live capital budget, Reserve %, Max Underlying %, Max `ρ`, Min IST Days, and `Block H2 Decay` state.
2. Every selected Wave 1 / Wave 2 runner with its Composite Score, Sim-to-Real retention status, Session Breadth, and H2 Trajectory.
3. Warm Bench runners and counts of Session-Breadth Holds, H2-Decay Holds, and Correlation/Tail-Crash Prunes — creating a complete institutional promotion record before you manually start any Live runner.
