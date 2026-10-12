# How to use Risk management

**Page:** `/risk` · **Use it when…** you need the authoritative risk surface:
exposure, limit usage, configuration and the event history.

Index: [USER-GUIDE.md](../USER-GUIDE.md) · design: [PORTFOLIO-CENTER.md](../PORTFOLIO-CENTER.md),
[PORTFOLIO-INTELLIGENCE.md](../PORTFOLIO-INTELLIGENCE.md)

## Header controls

Bucket selector (`All Buckets / Paper Only / Live Only`), `⟳ Refresh`,
`⬇ Export CSV`.

## Global Risk Dashboard (always at the top)

Six gauges — **Daily Loss Used · Drawdown** ("peak protected") **· Capital
Deployed · Gross Exposure · Open Positions · System Status** — plus
circuit-breaker and warning banners, and per-broker / per-segment cards when more
than one broker is configured.

## Tab 1 — `⚙️ Risk Configuration`

1. Optionally load a preset: `Conservative` / `Balanced` / `Aggressive`.
2. **Global Limits (Supervisor)** — daily loss limit, max drawdown, leverage and
   friends; `💾 Save Global Config`.
3. **Per-Bucket Overrides** — `paper` and `live` are independent: a breach in paper
   does **not** halt live. `💾 Save Bucket Overrides`.
4. **Correlation Groups** — declare which symbols move together; the exposure view
   and concentration warnings use them.
5. `🔴 Live Mode` toggles the live posture — deliberate, not something you hit by
   accident.

Halts are **session-scoped** (latches are written but deliberately not re-armed on
boot) and peak/day anchors re-baseline to the restored book. Updates are validated
with typo detection: a misspelled key is rejected rather than silently ignored.

## Tab 2 — `📜 Audit Timeline`

Every risk event with scope and reason.

1. Filter by **scope** (`All / paper / live`) and **event type**.
2. Search action/reason text, then `↻ Refresh`.
3. `Export CSV` for a record you can hand to someone else.

## Tab 3 — `📊 Exposure Analysis`

- **Exposure by Symbol** (pie) — where the notional sits.
- **Exposure by Correlation Group** (bar) — whether one thesis is really behind
  several "diversified" positions.
- **Drawdown & Daily Loss — last 6 h** (line).
- **Exposure Details** — symbol table (notional, runners) and group table
  (notional, count).

## Tab 4 — `📈 Trade History & Analytics`

Closed-trade risk statistics **grouped by day**.

1. Filter by instrument kind (`All / Equity / Option`).
2. Set rows per page (50–500), then `↻ Refresh`.
3. `Export CSV` to keep the slice.

## Related

The live gauges and the intelligence sections also appear under the Portfolio
command center's **Risk & intelligence** tab — see
[portfolio.md](portfolio.md). Config and audit editing live **only** here (single
authority), so the two never drift apart.
