# How to use the global chrome

Everything here is on **every** page. Index: [USER-GUIDE.md](../USER-GUIDE.md).

## Sidebar navigation

Three groups, eleven pages:

| Group | Page | URL |
|-------|------|-----|
| **Workspace** | Portfolio | `/portfolio` (also `/portfolio/paper`, `/portfolio/live`) |
| | Analytics | `/analytics` |
| | Risk management | `/risk` |
| | P&L reports | `/reporting` |
| **Research** | Backtest | `/backtest` (`/` redirects here) |
| | Compare strategies | `/compare` |
| | Optimization | `/optimize` |
| | Strategy builder | `/strategy-builder` |
| **Operations** | Engine playground | `/forward` |
| | Market data | `/data` |
| | Settings | `/settings` |

## Top bar

| Control | What it does | How to use it |
|---------|--------------|----------------|
| **Jump to…** (`Ctrl K`) | Page search dialog — navigation only, it never runs or deploys anything | Type a page name, `↑ ↓` to move, `Enter` to open, `Esc` to close |
| **Environment chip** (e.g. `Historical data`) | The *price data source* this deployment runs on — **not** an execution-mode indicator | Read it before trusting a result: synthetic vs real bars change everything |
| **Market status chip** | `NSE OPEN` / `PRE-OPEN` / `NSE CLOSED · WEEKEND` / `NSE CLOSED · <holiday>`; the tooltip carries next open and calendar warnings | Use it to interpret anything time-related (countdowns, "last update") |
| **Halt pill** | Portfolio risk state at a glance | On a Portfolio page it opens the *Risk & intelligence* tab; elsewhere it navigates to `/risk` |
| **Broker chip** | Opens the **Broker Connections** modal: every broker's session, its segment, `Login` / `Logout` / `Reconcile` | Log a broker in here before fetching data or arming live |
| **Data fetch indicator** | Appears only while a historical fetch runs (`n/m`) | Click it to stop the running fetch |
| **Theme toggle** | Light/dark; preference stored as `trading-workspace.theme` | Cosmetic — navigation and theme changes never place an order |

## Risk strip (always visible)

The survival layer: `Feed` state · `Daily Loss (today)` · `DD` (drawdown) ·
`Deployed` · `Pos` (open positions), each with a mini bar.

1. **`Details`** — expands a last-6-hours breakdown chart plus the top losers.
2. **`Flatten all`** — closes every open position across all runners; requires the
   `Yes, flatten everything` confirmation.

On a non-trading day the label reads *"Daily Loss (prev session)"* — it is not
silently showing today's zero.

## Alert widget (bottom right)

Rendered when `PORTFOLIO_INTELLIGENCE_ENABLED` (the default).

1. Read the minimized pill: alert count plus severity mix.
2. Expand it for the list — `View Details` and `Dismiss` per alert.
3. Open the detail modal for *what it means / contributing strategies / typical
   responses / subscribed strategies*. It has **no** position-changing buttons.

Polls `/api/alerts/active` every 3 s (15 s while the page is hidden) and only
re-renders when the alert version changes. See [ALERTS-GUIDE.md](../ALERTS-GUIDE.md).

## The Workspace guide (`?` key)

The `Workspace guide` button (sidebar footer, or `?`) opens the three-step
workflow dialog: **01 Build evidence** (backtest/compare/optimize) → **02 Practice
without exposure** (paper bucket) → **03 Prepare for live trading** (broker
connections, cost models, segment limits).

## Errors and toasts

Everything surfaces as a toast. Every `/api` error includes a request id
(`… [req 979be616]`) — grep that id in `logs/app.log` to find the exact
traceback. Run with `--log-level DEBUG` when you need more. See
[LOGGING.md](../LOGGING.md).

## Keyboard shortcuts

| Key | Action |
|-----|--------|
| `Ctrl K` | Jump to… page search |
| `?` | Workspace guide |
| `↑ ↓` / `Enter` / `Esc` | Move / open / close in either dialog |
