# How to use Strategy analytics

**Page:** `/analytics` · **Use it when…** you want to know whether an edge still
exists, over the forward/portfolio books (not a backtest).

Index: [USER-GUIDE.md](../USER-GUIDE.md) · detail: [STRATEGY-PERFORMANCE-ANALYTICS.md](../STRATEGY-PERFORMANCE-ANALYTICS.md)

## Header controls

- `Compare backtests` — jumps to `/compare` with your current selection in mind.
- `⟳ Refresh` — re-reads `/api/analytics`.

**Shared filters** (apply to both tabs): `Period` (7d / 30d / 90d / 1y / All time)
and `Mode` (All Modes / Paper Only / Live Only).

## Tab 1 — `Overview`

1. Read **Portfolio Overview**: total return, portfolio Sharpe (with a grade), win
   rate (`5 trades 2W/3L` style detail), max drawdown, and the active-strategy count.
2. **Cross-Broker Portfolio** breaks the same numbers down per broker — useful the
   moment two venues are in play.
3. **Running Strategies** — one row per instance; click a row to open its
   **Strategy Detail** tab.
4. **🚨 Performance Alerts & Edge Degradation** — the suite's own verdict on when
   performance has drifted from what the backtest promised.

## Tab 2 — `Strategy Detail`

Appears once a strategy is selected (also reachable as `/analytics?strategy=<id>`).
Read in this order:

| Section | Why it matters |
|---------|----------------|
| Strategy Description | What the thing is supposed to do |
| Key Performance Ratios | The headline numbers, graded |
| Equity Curve & Drawdown | Shape of the ride, not just the destination |
| Rolling Performance Trend (edge stability) | A dying edge shows here first |
| Monthly Performance Breakdown | Whether one month is carrying everything |
| Trade P&L Distribution & Insights | Win/loss shape and outliers |
| Recent Executed Trades | What it actually did lately |

Endpoints: `GET /api/analytics`, `GET /api/analytics/<instance_id>`.
