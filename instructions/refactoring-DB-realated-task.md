# DB-Related Task List (extracted)

Here are all the DB-related tasks pulled out of the implementation plan/tickets, grouped by phase. Each maps to the original ticket so you can track it back.

---

## GROUP 1 — Schema Changes (additive, safe)

### DB-T1 — Add `mode`/`source` columns to `portfolios`
- **From:** Ticket P1.1
- **Files:**
  - `db/migrations/002_add_mode_source.sql` (create)
  - `db/models.py` (edit — add `mode`, `source` fields to `Portfolio` model)
- **What:** 
  ```sql
  ALTER TABLE portfolios
    ADD COLUMN mode   TEXT NOT NULL DEFAULT 'paper'
      CHECK (mode IN ('paper','live')),
    ADD COLUMN source TEXT NOT NULL DEFAULT 'synthetic'
      CHECK (source IN ('synthetic','replay','mstock'));
  ```
- **Test:** `tests/db/test_migrations_002.py` — apply on fresh + existing DB, verify defaults & CHECK constraints reject bad values.
- **Acceptance:** additive, existing rows backfilled, no breakage.

---

## GROUP 2 — Keep As-Is (already correct, do NOT touch)

### DB-T2 — Verify core tables stay production-shaped
- **From:** design doc §9
- **Files:** `db/models.py`
- **Tables to keep as-is (only verify):** `positions`, `orders`, `fills`, `trades`, `equity_curve`, `market_data_cache`, `performance_metrics`, `strategy_signals`, `system_logs`
- **Note:** the `orders` table already has `broker_order_id` (currently always `NULL`). It gets **populated** in Phase 3 (live), but the schema stays.

---

## GROUP 3 — Live-Order Support (Phase 3 wiring)

### DB-T3 — Populate `orders.broker_order_id` in the live path
- **From:** Ticket P3.3 / P3.4
- **Files:**
  - `db/models.py` — confirm `broker_order_id` column present (it is).
  - `simulator/fill_providers.py` (create) — `BrokerFillProvider.get_fill()` sets `broker_order_id`.
  - `simulator/order.py` — ensure `Order` row persistence writes `broker_order_id`.
- **What:** In live mode, the real broker returns an order id → stored into `orders.broker_order_id`. In paper mode it stays `NULL`.
- **Acceptance:** After a live fill, the `orders` row has the broker's id.

---

## GROUP 4 — Delete Phantom/Dead Tables & References

### DB-T4 — Remove phantom `forward_test_*` tables & death references
- **From:** design doc §8/§9, Ticket P3.4
- **Files:**
  - `forward/live_engine.py` (delete, or strip all `forward_test_state` / `forward_test_trades` / `forward_test_equity` writes)
  - Any DDL/doctest referencing them
- **What:** These tables have **no DDL anywhere** — pure dead code. Either add real DDL if you actually need forward-test state, or delete all references. **Recommendation: delete the references; the spec'd tables (`equity_curve`, `trades`) already hold this data with proper schema.**
- **Acceptance:** `grep -r "forward_test_state\|forward_test_trades\|forward_test_equity" src/ db/` returns nothing.

---

## GROUP 5 — Cleanup / Unification (Phase 4)

### DB-T5 — Single authority for DB-URL resolution
- **From:** Ticket P4.3
- **Files:**
  - `db/config.py` (edit — **the** single source of DB URL)
  - `data/db_source.py` (edit — call `db/config.py`, don't resolve itself)
  - remove inline resolution in `live_engine.py` (deleted in T4 anyway)
- **What:** Today the DB URL is resolved in 3 places (`db/config.py`, `DbSource`, `live_engine`). Collapse to **one** (pick `db/config.py`).
- **Acceptance:** exactly one file resolves the DB connection URL.

### DB-T6 — Fix timeframe-naming drift against schema CHECK constraints
- **From:** Ticket P4.3
- **Files:**
  - `db/models.py` (CHECK constraints on timeframe columns)
  - config files + engine code that use `1H`/`4hour`/`minute`
- **What:** Pick **one canonical naming** (e.g. `1m,1h,1d`) and make config, code, and schema agree. The schema CHECK values are the source of truth — align code to them, not the other way.
- **Acceptance:** no timeframe string in code that violates the schema CHECK constraint.

---

## Summary Board — DB Tasks Only

| ID | Task | Phase | Files | Test | Depends on |
|---|---|---|---|---|---|
| DB-T1 | add `mode`/`source` cols | 1 | `002_add_mode_source.sql`, `db/models.py` | `test_migrations_002` | — |
| DB-T2 | verify core tables | 1 | (read-only) `db/models.py` | — | — |
| DB-T3 | populate `broker_order_id` | 3 | `fill_providers.py`, `order.py` | live-fill test | DB-T1 |
| DB-T4 | drop phantom `forward_test_*` | 3 | `live_engine.py` (del) | grep check | — |
| DB-T5 | unify DB-URL resolution | 4 | `db/config.py`, `db_source.py` | — | DB-T4 |
| DB-T6 | fix timeframe naming | 4 | `db/models.py` + config/code | — | — |

---

**Suggested execution order:** DB-T1 → DB-T2 → DB-T4 → DB-T3 → DB-T5 → DB-T6

(DB-T1 is the foundation; DB-T2 is a quick verify; DB-T4 is safe cleanup that unblocks live-path work; DB-T3 depends on T1; T5/T6 are tail cleanup.)

Want me to also generate the **SQL migration file content** for DB-T1 and the **DB model changes** ready to paste into `db/models.py`?