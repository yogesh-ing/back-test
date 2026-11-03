# Institutional Edge & Strategy Performance Playbook

**In-App UI Access:** Click **`Edge playbook`** in the top-right header of **`/diary`** (PnL Diary) or **`/analytics`** (Strategy Performance Analytics), use the contextual `Tuning guide` / `Prescription decoder` / `Gate parameters` buttons inside `/diary`, or press **`?`** anywhere in the workspace and select **Step 04 (`Diagnose & graduate edge`)**.

---

## 1. How Strategy Analytics (`/analytics`) & PnL Diary (`/diary`) Work Hand-in-Hand

`/analytics` (**Performance / Strategy Analytics**) and `/diary` (**PnL Diary & Institutional Edge Suite**) are **complementary, non-overlapping layers** of a quantitative trading desk review:

| Dimension | Strategy Analytics (`/analytics`) — **Macro & Venue Radar** | PnL Diary (`/diary`) — **Micro-Attribution & Graduation Gatekeeper** | How They Work Hand-in-Hand |
| :--- | :--- | :--- | :--- |
| **Primary Question** | *"Is my portfolio compounding smoothly, is rolling Sharpe decaying, and is my broker filling cleanly?"* | *"Which exact spawned instance, exit rule, IST clock window, or hold duration caused today's P&L — and what lever fixes it?"* | Spot macro degradation or venue drag on `/analytics`, then open `/diary` to isolate the exact parameter/regime culprit. |
| **Aggregation Lens** | Continuous MTM equity curve, 10-trade rolling Sharpe trend, monthly table, and **Cross-Broker venue** (`mStock`, `Dhan`, `Upstox`). | Strict **`Paper \| Live` segregated book**, **IST Session Calendar** heatmap, and **Strategy → Spawned Instance** (`symbol · TF · SL/TP/trail · capital`). | `/analytics` evaluates multi-month return stability & broker venues; `/diary` compares spawned parameter configs without mixing simulated and real capital. |
| **Core Metrics** | Total Return %, Annualized Sharpe, Sortino, Calmar, Equity Drawdown %, Adverse Slippage (bps), Fill Rate %, Rejections. | `Gross − Commission = Net` (`2dp`), Net Profit Factor (`PF`), Net Expectancy (`₹/trade`), Payoff Ratio `R` vs `R_BE`, Commission Drag %, Hold-Duration hurdle. | High Sharpe with thin sample or hidden fee churn is caught by `/diary` gates; `6/6` passed `/diary` gates still check `/analytics` for rolling stability & broker fill quality. |
| **Governance Output** | Rolling **Edge Degradation Warning** + **Broker Compare / Migration / Recommendation** modals. | Deterministic **6-Gate Graduation Verdict** (`PROMOTE_CANDIDATE`, `TUNE_EDGE`, `RETIRE_OR_KILL`) + persisted **IST Operator Session Journal**. | Require clean `/analytics` rolling health **and** `6/6` `/diary` graduation gates before deploying or scaling Live lots *(use `/reporting` for consolidated fiscal/ITR statements)*. |

### The 5-Stage Prop-Desk Lifecycle Flywheel

1. **`/backtest` & `/compare`** — Build out-of-sample evidence and compare parameter sensitivity.
2. **`/portfolio/paper`** — Spawn configured Paper instances under live NSE ticks and realistic cost models.
3. **`/analytics` (Macro Check)** — Track Equity Curve, Sharpe/Sortino/Calmar, 10-trade Rolling Sharpe stability, and Broker Execution Quality (slippage in bps, fill rate, rejections).
4. **`/diary` (Micro Surgery & Gatekeeping)** — Drill into Spawned Instances, Exit-Reason Matrix, NSE IST Intraday Regimes, Hold-Duration Friction, Payoff Quadrant, and the 6-Gate Graduation Scorecard.
5. **`/portfolio/live` (Controlled Promotion)** — Promote `6/6` green `PROMOTE_CANDIDATE` instances at **1× minimum lot**, respecting `entitlement.live` (`long_call`, `long_put`), and log observations in the IST Session Journal.

---

## 2. Institutional Parameters & Gate Thresholds

### A. PnL Diary Engine Gates (`src/backtest/diary/`)

| Gate / Parameter | Engine Pass / Fail Threshold | Prop-Desk Stretch Target | Why It Matters |
| :--- | :--- | :--- | :--- |
| **1. Closed Sample Size (`N`)** | **`≥ 10`** for Edge Prescription · **`≥ 20`** for Live Graduation | **`≥ 50`** across ≥ 2 vol regimes | Blocks promoting lucky 4-trade streaks (`LOW_SAMPLE`). Early kill triggers at `N ≥ 10` if bleeding. |
| **2. Net Expectancy (`E[net]`)** | **`> +₹0.00 / trade`** *(after combined Commission)* | **`≥ 2.0 × Avg Commission / trade`** | Proves rupee edge per round-trip comfortably clears transaction costs. |
| **3. Net Profit Factor (`PF`)** | **`≥ 1.35×`** ($\sum \text{Net Wins} / |\sum \text{Net Losses}|$) | **`1.60× – 2.20×`** | At `1.05×–1.20×`, live bid-ask spread and one opening gap wipe out weeks of paper gains. |
| **4. Payoff Ratio (`R` vs `R_BE`)** | **`R > (1 − W) / W`** *(above hyperbolic curve)* | **`R ≥ 1.30 × R_BE`** | Links Net Win Rate ($W$) directly to Avg Win / Avg Loss asymmetry ($R$). |
| **5. Commission Drag (`Comm / |Gross|`)** | **`< 30%`** Healthy · **`≤ 35%`** Graduation Gate<br>**`30–50%`** Warning · **`> 50%`** Critical · **`≥ 75%`** Kill | **`< 20%`** of Gross Profit | Measures what share of gross alpha is eaten by combined fees. Above `50%`, turnover is too high for the move size. |
| **6. Realized Max Drawdown (`MDD`)** | **`≤ 10.0%`** of allocated capital *(or **`≤ ₹25,000`** if unallocated)* & not `BLOCKED_BY_RISK` | **`≤ 6.0%`** per runner · **`Net / MDD ≥ 2.0`** | Caps peak-to-trough realized tail damage; any risk-halted runner automatically fails graduation. |
| **7. Ledger Integrity (`E2` Truth)** | **`0` anomalies** (`|Gross − Commission − Net| ≤ ₹0.02`) | **`0.00` paisa drift** | Every trade row, cell, and rollup must reconcile to exact `2dp` (`tabular-nums`). |
| **8. Live Option Entitlement** | `config/risk_limits.yaml` → **`[long_call, long_put]`** | Defined-risk long premium first | Paper short/multi-leg option spawns receive an explicit `entitlement_advisory` badge prior to Live review. |

### B. Strategy Analytics (`/analytics`) Companion Benchmarks

- **Annualized Sharpe Ratio**: `≥ 1.50` solid · `≥ 2.00` institutional grade · `< 0.75` fragile.
- **Sortino Ratio**: `≥ 2.00` (penalizes downside deviation only — primary ratio for convex long-option strategies).
- **Calmar Ratio (`Annualized Return / Max DD`)**: `≥ 1.50` (`≥ 2.50` strong intraday book).
- **Order Fill Rate**: `≥ 98.0%` on market / marketable-limit orders; `0` stale orders (`> 60s`).
- **Adverse Slippage**: `< 5–8 bps` on liquid index / NIFTY F&O; `> 15 bps` materially degrades scalp expectancy.

---

## 3. The 5-Step Strategy Tuning Workflow

Run this sequence after every **10–20 closed Paper trades** (changing **one parameter lever at a time** on a newly spawned Paper instance):

1. **Step 1 — Separate Strategy Family vs Spawned Instance (`/diary` → `Attribution`)**
   - Expand each strategy into its **Spawned Instances** (`symbol · timeframe · SL/TP/trail · capital`).
   - Retire bad symbol/timeframe spawns (`PF < 1.0`) instead of rewriting strategy code that already works (`PF > 1.6`) on liquid instruments.

2. **Step 2 — Exit-Reason Surgery (`/diary` → `Regimes` → `Exit-Reason Matrix`)**
   - **`signal` (Signal Flip) bleeding while `take_profit` is green**: Indicator flips in sideways chop. Disable `signal_flip` exit (or require confirmation bars) and let `stop_loss` / `take_profit` / `trailing_stop` manage exits.
   - **`stop_loss` > 65% of trades with short hold time**: Stop is inside normal 1-bar ATR noise. Widen SL to `1.5×–2.0× ATR` and scale down quantity so rupee risk stays constant.
   - **`trailing_stop` Avg Win < `|Avg Loss|`**: Trail arms too tight and chokes trend winners. Increase trail activation threshold.
   - **`eod_flat` (`15:15–15:30`) consistently red**: Late entries run out of session time. Block new intraday entries after `14:30 IST`.

3. **Step 3 — NSE Intraday IST Regime Filtering (`/diary` → `Regimes` → `NSE Session Matrix`, `Entry Clock`)**
   - **`09:15–09:45` Opening Drive**: Wide spreads and overnight auction discovery — delay mean-reversion entries until `09:30–09:45 IST`.
   - **`09:45–11:30` Morning Trend**: Cleanest institutional directional flow — primary window for trend/momentum setups.
   - **`11:30–13:30` Midday Chop**: Volume trough; if `Entry Clock` Net P&L is negative here, add a midday entry blackout (`11:30–13:30 IST`).
   - **`13:30–15:10` Afternoon / Gamma & `15:10–15:30` Square-Off**: European overlap + expiry gamma acceleration; avoid fresh intraday entries inside `15:10–15:30 IST`.

4. **Step 4 — Cut Sub-Minute Friction & Clear the Breakeven Curve (`/diary` → `Payoff & Drag`)**
   - If `< 1m` (**Churn Watch**) or `1–5m` (**Scalp**) shows `Drag > 50%`, step up timeframe (`1m → 5m/15m`) or add a cooldown.
   - Ensure expected gross move per trade is at least `3×–4× Avg Commission / Trade` (**Fee Hurdle**).

5. **Step 5 — Cross-Check `/analytics` & Graduate (`/diary` → `Graduation & Journal`)**
   - Verify `6/6` gates green (`PROMOTE_CANDIDATE`), check `/analytics` rolling Sharpe & broker slippage, promote at 1× minimum lot, and document changes in the **Operator Session Journal**.

---

## 4. Deterministic Prescription Decoder

| Badge / Verdict | Trigger | Root Cause | Actionable Parameter Fix |
| :--- | :--- | :--- | :--- |
| **`CHURN_FRICTION_DRAG`** | `Gross > ₹0` & `Net ≤ ₹0` | Positive directional alpha eaten `>100%` by combined Commission. | **Do NOT invert signals.** Cut turnover 40–60%: higher timeframe, cooldown bars, or wider target. |
| **`TIGHTEN_ENTRY_FILTER`** | `N ≥ 10`, `W < 40%`, `E[net] ≤ 0` | Whipsaw/chop entries forcing `R_BE > 1.50×`. | Add regime confirmation (ADX, VWAP slope, VIX band, or `11:30–13:30 IST` blackout). |
| **`WIDEN_TP_OR_CUT_SL`** | `N ≥ 10`, `W ≥ 40%`, `R < R_BE` | Negative skew (`|Avg Loss| >> Avg Win`). | Cap max SL, add `time_stop` on stagnant losers, or trail winners instead of capping TP early. |
| **`POSITIVE_EDGE`** | `N ≥ 10`, `E[net] > 0`, `R > R_BE` | Net win rate & payoff clear fee hurdle. | Freeze parameters; accumulate `N ≥ 20` trades for graduation. |
| **`LOW_SAMPLE`** | `N < 10` | Insufficient statistical sample. | Hold sizing steady in Paper until `N ≥ 10–20`. |

---

## 5. Core Equations & Governance Rules

- **Single-Sum Commission Rule (`E2`)**:
  $$\text{Net P\&L} = \text{Gross P\&L} - \text{Commission}$$
  `Commission` is always **one single combined sum** (brokerage + statutory taxes), formatted to `2dp` (`tabular-nums`) with zero sub-component fee split.
- **Net Expectancy & Hyperbolic Breakeven Curve**:
  $$E_{\text{net}} = W \cdot \overline{W}_{\text{net}} - (1 - W) \cdot |\overline{L}_{\text{net}}|, \qquad R = \frac{\overline{W}_{\text{net}}}{|\overline{L}_{\text{net}}|}, \qquad R_{\text{BE}} = \frac{1 - W}{W}$$
- **Strict Book & Clock Discipline**: `Paper` and `Live` books are strictly segregated; all session dates and intraday windows use **`Asia/Kolkata` (`IST`, `+05:30`)**.

---

## 6. Constrained Live Roster Selection Tournament (When 15–20 Paper Runners Pass)

> Full architectural reference, formulas, decision codes, and API contract: **[`docs/GRADUATION-AND-LIVE-ROSTER-GUIDE.md`](./GRADUATION-AND-LIVE-ROSTER-GUIDE.md)**.

When 15–20 Paper instances run across 30–40 sessions and multiple pass all 6 Graduation Gates, `/diary` → **`Graduation & Journal`** (`GET /api/diary/roster-plan`) runs a 5-stage portfolio construction tournament without touching any Paper/Live execution code:

1. **Stage 1 — Gate, Entitlement, Temporal Breadth, H1→H2 Trajectory & Backtest Haircut Filter**:
   - Requires `6/6` Graduation Gates (`PROMOTE_CANDIDATE`) and Live option entitlement clearance (`long_call` / `long_put` baseline).
   - **Temporal Session Breadth (`min_session_days = 5`)**: Holds single-day scalper clusters (`HOLD_SESSION_BREADTH`) until edge is proven across `≥ 5` distinct IST sessions.
   - **Chronological Half-Split Decay Guard (`block_decaying_edge = true`)**: Splits closed trades chronologically into `H1` (first half) vs `H2` (recent half) and holds front-loaded luck runners where `H1 > ₹0` collapsed to `E[H2] ≤ ₹0` (`HOLD_RECENT_DECAY`).
   - **Sim-to-Real Haircut Filter**: Rejects severe curve-fit decay (`Backtest PF ≥ 1.50×` with `< 45%` Paper PF retention -> `DISQUALIFIED_HAIRCUT`).
2. **Stage 2 — Composite Capital-Efficiency Ranking (`0–100`)**: `40 pts` Return-over-Drawdown (`Net P&L / Max DD`) + `25 pts` Net PF cushion + `15 pts` Friction efficiency (`1 − Drag`) + `20 pts` Backtest-to-Paper PF retention & sample depth, minus `H1→H2` trajectory penalty (`−6 pts` cooling / `−15 pts` decaying).
3. **Stage 3 — Multi-Timeframe Family Deduplication**: Winner-Take-All per `(Strategy, Canonical Underlying)` across timeframes unless fast scalp (`≤ 5m`) vs swing (`≥ 1h/1day`) **and** daily IST P&L correlation `ρ < 0.50`.
4. **Stage 4 — Daily IST Net P&L De-Correlation (`ρ`) & Worst-Red-Day Tail Co-Crash Guard**:
   - Skips hidden clones whose daily IST Net P&L Pearson correlation `ρ > max_correlation` (default `0.65` across `≥ 5` active IST session days -> `PRUNED_CORRELATED_CLONE`).
   - **Tail Co-Crash Overlap (`max_tail_overlap = 3`)**: Even when normal-day `ρ ≤ 0.65`, prunes candidates sharing `3/3` worst losing IST session days with an already-selected Live winner (`PRUNED_TAIL_CRASH_OVERLAP`).
5. **Stage 5 — Reserve, Underlying Concentration, Slot Knapsack & Journal Snapshot**:
   - Holds `20%` margin reserve, caps single-underlying allocation at `40%` of total Live capital (catching `NIFTY` spot + `NIFTY` CE option overlap), splits winners into **Wave 1 (Slots #1–#2)**, **Wave 2 (Slots #3+)**, and **Warm Paper Bench**, and persists an immutable promotion audit note via **`Log Roster to Journal`** (`POST /api/diary/notes`).


