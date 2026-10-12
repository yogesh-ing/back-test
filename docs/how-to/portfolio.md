# How to use Portfolio (the command center)

**Pages:** `/portfolio` · `/portfolio/paper` · `/portfolio/live`

**Use it when…** you want to see what is held, steer it, or put a strategy to work.
This is a trading console, not just a monitor: automation keeps trading and you can
intervene at any moment.

Index: [USER-GUIDE.md](../USER-GUIDE.md) · endpoints: [PORTFOLIO-CENTER.md](../PORTFOLIO-CENTER.md)

## The three views

| Page | Shows |
|------|-------|
| `/portfolio` | Both buckets side by side: PAPER and LIVE cards (equity, allocated, daily/realised P&L, instances, running, open positions, mini loss-limit/drawdown gauges), a collapsible **Global risk** header, the full command center embedded for LIVE, and a PAPER summary table |
| `/portfolio/paper` | Paper bucket only — simulated fills, live instances never render here |
| `/portfolio/live` | Live bucket only — real-money instances; paper never renders here |

Each bucket page carries the banner *"Scoping this command center to the PAPER/LIVE
bucket only — other buckets never appear here"*, and the metric cards, bulk actions
and positions list are all filtered to that bucket.

## Command center header

- Metric cards: **Total Capital · Total Equity · Deployed · Daily P&L · Realized
  P&L · Open Positions · Daily Loss Limit** (progress bar).
- **`+ Add instance`** — opens the spawn modal (below).
- **`⏸ Pause all` / `▶ Resume all`** — bulk control, scoped to the current bucket.
- **`⬇ Export charts`** — writes a PnL-vs-spot PNG + CSV into `charts/` for every
  live option runner (also runs automatically at 15:30 IST).
- **`⏹ Emergency flatten`** — closes **both** books (paper and live) after
  confirmation; hidden on the paper-bucket page so a sandbox click cannot masquerade
  as a real-money action.
- **Circuit-breaker banner** (`CIRCUIT BREAKER HALTED` + `Reset & Resume`) appears
  when a global breaker has tripped; concentration warnings appear above the matrix.

## Strategy instances (the matrix)

Search box, status filter (`Running / Paused / Stopped / Error`) and sort
(Daily P&L, Open P&L, Capital Allocation, Name, Status). Row actions:

| Button | Action |
|--------|--------|
| `⏸` / `▶` | Pause / resume this runner |
| `⏹` | Stop it |
| `🔍` | Deep dive into the instance |
| `🗑` | Remove the instance — **flattens its book first**, then deletes the row |
| `⟳` | Refresh marks (option rows) |

An empty bucket offers `Add your first instance`.

## Add instance (spawn) modal

Routing only — trading logic lives in **Playbooks**, never here. Fields (some are
conditional on the strategy's instrument kind):

1. **Instance name** (optional; auto-generates `strategy·instrument·timeframe`)
2. **Strategy** — with a hint line describing it
3. **Playbook** (optional) — pick a declarative option config to spawn from
4. **Target type**, **Timeframe**, **Bucket mode** (`paper` / `live`), **Data source**
5. **Segment** and **Broker** — shown only when segments are configured
6. **Instrument** (SymbolPicker: `All / Equity / Index / F&O` tabs + search),
   **Lots per leg** (options), **Universe/Pool** + **Max pool positions** (pooled equity)
7. **Allocated capital**, then the strategy's own parameters

`Deploy Instance` creates it. The instrument is derived from the strategy's signal
kind — the form never asks you to invent one.

## The seven tabs

### Tab 1 — `Positions` (default)

*What is held right now, with the manual controls.* One flat row per open position;
an option structure shows as a single net row with its legs underneath.

1. Read `Unrealized P&L`, then `Target` / `Stop` — `—` means no level is armed.
2. Narrow with `Filter symbol / instance…`, or tick `SL/Target only` to hide rows
   with nothing armed.
3. Act from the **Actions** column — each opens a modal that names the row it will
   affect and posts to `/api/portfolio/position/action`:
   - `🛑 Modify SL` / `🎯 Modify Target` — enter a **price** (share price, or net
     premium per unit for options). The *server* validates it against the live mark;
     a refusal is shown verbatim and the modal stays open.
   - `◐ Close 50%` — equity positions only.
   - `✕ Close All` — flatten that position.
4. Manual levels are checked **on every bar, every mark move and every stress
   markdown**, so a manual stop fires even if the strategy never trades again.
   Whichever exit fires first — manual or strategy — wins.
5. `↻ Refresh` re-renders from the latest SSE snapshot.

On a **live** runner a close returns `status: "placed"`: the UI tells you the order
was sent rather than pretending the position is already flat.

### Tab 2 — `Equity`

On-demand equity snapshot of the session.

1. Press `⟳ Refresh snapshot` (it fetches `/api/portfolio/equity/snapshot`, scoped
   to the bucket on paper/live pages).
2. Read the session stats row and the equity chart.
3. Use the runner table — `Equity · Realized P&L · Today · Win rate ·
   Trades (today/all)` — to see which instance is carrying the book.

### Tab 3 — `Orders`

The engine's own order ledger: every order's fate plus the slippage actually paid.
The tab badge counts working + rejected orders from the SSE snapshot; the panel
polls every 3 s while visible.

1. Filter with the status dropdown (`All / Pending / Filled / Rejected / Cancelled`),
   the `Filter symbol / instance…` box, and the row limit (50–500).
2. **Pending** rows carry their **Age** and the app's only `Cancel` button.
3. **Filled** rows show `Ordered @` vs `Filled @` with adverse-positive **Slippage**
   per unit — that column is where cost surprises show up.
4. Tick `⏰ Aging only` to see only orders past the thresholds: **60 s → warn**,
   **5 min → alert**, one audit entry per band.
5. `✎ Amend` (on an order resting **at a venue**) edits quantity / limit price —
   the venue is asked first, and a refusal leaves local state untouched.
   Simulated orders fill or reject inside the same call, so for those use Cancel
   and let the strategy re-arm.

Option structures execute as a unit on the strategy's own option book; their
entries/exits are on the runner signals and the **Trade history** tab.

### Tab 4 — `Risk & intelligence`

Live risk gauges plus the **Portfolio Intelligence** board — *information only: the
platform calculates and alerts, strategies decide, you override. Nothing here
closes a position.*

**Header controls:** bucket filter (`All Buckets / Paper Only / Live Only`),
`↻ Refresh Risk`, `💥 Stress Test (-25%)`, `Reset Breaker`.

| Section | What you get | Refresh |
|---------|--------------|---------|
| 📊 Portfolio Greeks | Delta/Gamma/Vega/Theta in ₹, per-runner breakdown, scenario revaluation table, warnings | every 1 s |
| 🎯 Concentration | By underlying and by strike cluster (positions, types, holders, exposure) | every 1 s |
| 🔗 Strategy Correlation | Heatmap, group by `runner` or `strategy`, `↻ Recalculate` | every 30 s |
| 🌡️ Market Regime | Current regime, VIX input (`Set India VIX` → `Apply`), strategy regime-fit table | every 30 s |
| 📈 Market Activity | OI / liquidity activity | every 30 s |

Fast data refreshes every 1 s, slow every 30 s — **only while the tab and the page
are visible**; expanded state is stored per section and deep links
`…?tab=risk#pi-<section>` jump straight to one. Editing config, per-bucket limits
and the audit timeline is done on the **Risk page** (single authority); this tab
links to it. See [risk-management.md](risk-management.md).

### Tab 5 — `Trade history`

**Aggregated closed trades** across every runner — the post-trade view.

1. Choose the kind: `All Kinds / Equity Only / Option Only`, and a row limit.
2. `↻ Refresh` loads them; read `Entry → Exit`, `PnL` and **`Exit Reason / Time`**
   (`auto_square_off`, `expiry_settlement`, `strategy_signal`, …) — the reason is
   what turns a losing trade into a lesson.
3. Summaries sit above the table.

### Tab 6 — `Playbooks`

Declarative, reusable option-strategy configs: structure, strikes policy, exits,
sizing, risk envelope — **no code**. One concept, embedded here rather than on a
page of its own.

1. `＋ New Playbook` — or open one of the three seeded defaults (NIFTY ATM Bull
   Spread Conservative, NIFTY ATM Long Call/Put Directional, BANKNIFTY Delta 35
   Spread; they cannot be deleted).
2. Each card shows name, underlying · structure · strike · qty, the exit bits, and
   the risk cap with an **`estimated`** badge (V1 moneyness model) plus its version.
3. **`Deploy`** spawns a runner from it (the same `Add instance` path), `Edit`
   bumps the version, `Delete` removes it.
4. The engine owns HOW (live/paper check, margin, lot size from the instrument
   master — never stored, NSE revises lot sizes); the strategy owns WHAT (signal).

Full spec: [ARCHITECTURE-UNIFIED-TRADING.md](../ARCHITECTURE-UNIFIED-TRADING.md).

### Tab 7 — `Activity log`

The audit trail: spawns, bulk actions (tagged with the bucket they applied to),
breaker events, order-aging entries, alert lifecycle. Newest first — use it to
answer *"who/what changed this, and when"* after any surprise.
