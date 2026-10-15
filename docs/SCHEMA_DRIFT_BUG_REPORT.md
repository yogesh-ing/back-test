# CRITICAL BUG: Database Schema Drift Blocking Trade Persistence

**Severity:** HIGH (data loss — trades not persisting to DB)  
**Discovered:** 2026-10-14 during PnL validation testing  
**Impact:** Zero trades persisted since migration 008 was added to ORM  

---

## Root Cause

The SQLAlchemy ORM model (`src/backtest/db/models.py`) expects the `portfolios` table to have these columns:
- `segment` (String(64)) - capital partition label
- `execution_broker` (String(32)) - broker routing target

But the actual SQLite database only has migrations up to **005**, missing migrations **008-020**.

### Evidence

```sql
-- What the ORM model expects (from models.py lines 269-273):
segment: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text("''"))
execution_broker: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("''"))

-- What the DB actually has (from schema_migrations table):
002: portfolios: add mode/source columns
003: market_data_cache: canonical timeframe naming
004: trade_structures: option structure book
005: portfolio intelligence: alerts, greeks + regime history
[MISSING: 006, 007, 008, 009, 010, 011, 012, 013, 014, 015, 016, 017, 018, 019, 020]
```

### Error Log

```
sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) no such column: portfolios.segment
[SQL: SELECT portfolios.portfolio_id, ..., portfolios.segment, portfolios.execution_broker 
       FROM portfolios WHERE portfolios.name = ?]
```

This error occurs every time `LiveTradePersister.flush_runner()` tries to persist a closed trade (every 5 ticks).

---

## Consequences

1. **No trades in DB** → Can't run audit reports, tax calculations, or performance attribution
2. **PnL reconciliation impossible** → No ground truth to validate against API
3. **Silent failure** → Errors logged but trading continues normally (fail-soft design masks the bug)
4. **State persistence works** → JSON file keeps runners alive, but trade history is lost on restart

---

## Fix Required

Apply pending migrations to bring DB schema in sync with ORM model:

```bash
cd /c/learning/back-test
PYTHONPATH=src python -m alembic -c db/alembic.ini upgrade head
```

Or manually apply migration 008:

```sql
ALTER TABLE portfolios ADD COLUMN segment VARCHAR(64) NOT NULL DEFAULT '';
ALTER TABLE portfolios ADD COLUMN execution_broker VARCHAR(32) NOT NULL DEFAULT '';
```

Then verify:

```bash
python scripts/export_trades.py  # Should show >0 trades if any closed today
python scripts/validate_pnl_reconciliation.py  # Should reconcile DB vs API
```

---

## Prevention

1. **Add migration check to startup**: Fail fast if DB schema lags behind ORM model
2. **CI gate**: Block PRs that modify ORM models without corresponding migration files
3. **Integration test**: Run `export_trades.py` as part of nightly smoke tests

---

## Related Files

- `src/backtest/db/models.py` — ORM model (expects migration 008+)
- `src/backtest/forward/trade_persistence.py` — Fails on line 122 (_ensure_portfolio_row)
- `db/alembic/versions/20260928_1200_008_multi_broker_segments.py` — Missing migration
- `forward_test.db` — Stuck at migration 005

---

**Status:** IDENTIFIED, FIX PENDING  
**Next Action:** Apply migration 008 and re-run trade export
