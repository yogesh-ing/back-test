# How to use P&L reports (consolidated statement)

**Page:** `/reporting` · **Use it when…** you need one statement across every
broker and both books: gross → fees → net → estimated tax → net after tax.

Index: [USER-GUIDE.md](../USER-GUIDE.md) · full reference: [WEB-UI.md](../WEB-UI.md) §Reporting

## How to use it

1. Set **From / To** (defaults to the financial year to date).
2. Optionally type broker names into **Brokers** (`All brokers, or comma-separated
   names`) and decide whether to **Include paper trades** (paper is never taxable —
   it never enters a return).
3. `⟳ Generate report`, then read the blocks in order:
   - **Summary ladder** — Gross P&L → itemised fees (brokerage, STT, exchange,
     SEBI, stamp, GST) → Net → Estimated tax → Net after tax → cost+tax drag.
   - **By broker** and **by tax category** (rate, schedule, treatment, loss rule).
   - **Reconciliation**, **caveats / data notes / provenance**, **trade ledger**.
4. **Exports:** `PDF statement`, `ITR annexures (xlsx)`, `Trade ledger (csv)`,
   `Email (dry run)` — the dry run writes a `.eml` to `var/reporting/outbox`;
   sending needs SMTP configured.
5. **Contract-note reconciliation:** per broker, paste the note's net P&L (and
   optional fees) and press `Reconcile` → `PASS / WARNING / FAIL` against the
   tolerance bands in `config/reporting.yaml`.
6. The `demo book` toggle includes a sample book labelled `SIMULATED` — for
   previews only; it is excluded from tax.

## Tax heads (FY 2026-27)

| Head | Treatment |
|------|-----------|
| F&O | Non-speculative business income (slab rate) |
| Intraday equity | Speculative business income (slab rate) |
| Delivery ≤ 12 m | STCG 20 % (s.111A) |
| Delivery > 12 m | LTCG 12.5 % above the ₹1,25,000 exemption (s.112A) |
| Paper trading | Not taxable — never enters a return |
| Unknown instrument | Fails closed as `UNCLASSIFIED` |

Every export carries the *"estimate — verify with a chartered accountant"*
disclaimer.

## Endpoints

`GET /api/reporting/pnl/consolidated`, `GET /api/reporting/config`,
`POST /api/reporting/pnl/export/{pdf,itr,trades}`,
`POST /api/reporting/pnl/reconcile`, `POST /api/reporting/email`.
Rates/tolerances/mailer live in `config/reporting.yaml` (override with
`REPORTING_CONFIG_PATH`). Malformed dates or missing note figures are 400s; a
missing database yields an empty report with a warning, never a 500.
