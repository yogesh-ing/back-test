# Data Coverage Query Optimization

**Priority**: High (user-facing performance issue)
**Estimated Effort**: 35 minutes
**Status**: Backlog

## Problem

The `/api/data/coverage` endpoint scans all 46M rows in `market_data_cache` on every page load and poll to compute per-symbol coverage statistics. This causes:

- **14+ second page loads** without proper indexing
- **High database CPU** during fetch jobs (competing with INSERT operations)
- **Blocking other operations** due to long-running GROUP BY queries
- **Poor user experience** - "CHECKING" chip stays gray for too long

Current implementation in `data_manager.py`:
```python
def _probe_covered_days(engine, symbols, timeframe, from_date, to_date):
    # Runs: SELECT symbol, MAX(ts)::date FROM market_data_cache
    #       WHERE symbol IN (...) AND timeframe = ? GROUP BY symbol
    # This scans the entire hypertable every time
```

## Root Cause

TimescaleDB hypertables don't support efficient `GROUP BY` across chunks without a covering index. The query must scan every chunk to find the MAX(ts) per symbol.

Migration 019 added `ix_mdc_tf_ts` index, but it only helps when fetching is idle (Timescale rejects CONCURRENTLY builds during writes).

## Proposed Solutions

### Option 1: Summary Table (Recommended)

Create a lightweight denormalized table updated incrementally:

```sql
CREATE TABLE data_coverage_summary (
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    bar_count INTEGER NOT NULL DEFAULT 0,
    from_date DATE,
    to_date DATE,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
    PRIMARY KEY (symbol, timeframe)
);

CREATE INDEX idx_coverage_lookup ON data_coverage_summary (symbol, timeframe);
```

**Update Strategy**: After each symbol's fetch completes in `_run_fetch_job_inner()`:
```python
# In the per-symbol loop, after bars are persisted:
with engine.begin() as conn:
    conn.execute(
        text("""
            INSERT INTO data_coverage_summary (symbol, timeframe, bar_count, from_date, to_date, updated_at)
            VALUES (:symbol, :timeframe, :count, :from_d, :to_d, now())
            ON CONFLICT (symbol, timeframe) DO UPDATE
                SET bar_count = EXCLUDED.bar_count,
                    from_date = LEAST(COALESCE(data_coverage_summary.from_date, EXCLUDED.from_date), EXCLUDED.from_date),
                    to_date = GREATEST(COALESCE(data_coverage_summary.to_date, EXCLUDED.to_date), EXCLUDED.to_date),
                    updated_at = now()
        """),
        {"symbol": symbol, "timeframe": timeframe, "count": len(bars), ...}
    )
```

**Query becomes instant**:
```python
def _probe_covered_days(...):
    result = engine.execute(
        text("SELECT symbol, from_date, to_date, bar_count FROM data_coverage_summary WHERE timeframe = :tf"),
        {"tf": timeframe}
    )
    # Returns in <10ms instead of 14s
```

**Pros**:
- Reads go from 14s → <100ms
- Incremental updates (only touched symbols)
- No blocking during fetches
- Permanent fix

**Cons**:
- Extra write overhead per symbol (~5ms)
- Must handle migration for existing data
- Slight risk of drift if update fails (mitigated by recomputing from source)

**Implementation Steps**:
1. Create migration script `migrations/020_coverage_summary_table.sql`
2. Add backfill logic: `INSERT INTO data_coverage_summary SELECT ... GROUP BY ...` from existing data
3. Modify `_persist_bars()` or add hook after fetch to update summary
4. Update `_probe_covered_days()` to read from summary table
5. Add fallback: if summary table missing, fall back to old query + log warning
6. Test: verify coverage matches before/after

---

### Option 2: Increase Cache TTL (Quick Relief)

In `data_manager.py`, change:
```python
_FRESHNESS_TTL_SECONDS = 1800.0  # Was 300.0 (5 min → 30 min)
```

**Pros**: 1 line change, immediate relief
**Cons**: Stale data for 30 min, doesn't fix root cause

---

### Option 3: Materialized View

```sql
CREATE MATERIALIZED VIEW mv_coverage AS
SELECT symbol, timeframe, COUNT(*) as bar_count,
       MIN(ts)::date as from_date, MAX(ts)::date as to_date
FROM market_data_cache
GROUP BY symbol, timeframe;

CREATE UNIQUE INDEX idx_mv_cov ON mv_coverage (symbol, timeframe);
```

Refresh strategy: Call `REFRESH MATERIALIZED VIEW CONCURRENTLY mv_coverage` after fetch job completes.

**Pros**: No schema changes, faster than live query
**Cons**: Still needs full refresh (takes minutes), can't refresh during writes

---

### Option 4: Lazy Per-Symbol Check

Don't pre-compute coverage on page load. Only check when user ticks a symbol:
```javascript
// In data_manager.js
async function dmCheckCoverage(symbol) {
    const resp = await fetch(`/api/data/symbol-coverage?symbol=${symbol}`);
    // Update just that row
}
```

**Pros**: Instant page load, targeted queries
**Cons**: First tick slower, still hits DB for each symbol

---

## Recommended Approach

**Do Option 1 (Summary Table) + Option 2 (Increase TTL temporarily)**:

1. **Immediate** (5 min): Increase TTL to 1800s for quick relief
2. **This session** (30 min): Build summary table with migration + backfill
3. **Deploy**: Switch `_probe_covered_days()` to read summary table
4. **Verify**: Compare coverage output before/after, ensure fetch updates work
5. **Cleanup**: Remove old slow query code once verified

**Total effort**: ~35 minutes
**Expected result**: Coverage checks go from 14s → <100ms permanently

## Migration Script Template

```sql
-- migrations/020_coverage_summary_table.sql
BEGIN;

CREATE TABLE IF NOT EXISTS data_coverage_summary (
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    bar_count INTEGER NOT NULL DEFAULT 0,
    from_date DATE,
    to_date DATE,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
    PRIMARY KEY (symbol, timeframe)
);

CREATE INDEX IF NOT EXISTS idx_coverage_lookup
    ON data_coverage_summary (symbol, timeframe);

-- Backfill from existing data (may take several minutes on 46M rows)
INSERT INTO data_coverage_summary (symbol, timeframe, bar_count, from_date, to_date, updated_at)
SELECT symbol,
       timeframe,
       COUNT(*)::int as bar_count,
       MIN(ts)::date as from_date,
       MAX(ts)::date as to_date,
       now() as updated_at
FROM market_data_cache
GROUP BY symbol, timeframe
ON CONFLICT (symbol, timeframe) DO NOTHING;

COMMIT;
```

## Code Changes Required

1. **`data_manager.py`**:
   - Line ~1700: Change `_FRESHNESS_TTL_SECONDS = 1800.0`
   - Add `_update_coverage_summary(engine, symbol, timeframe, bars)` helper
   - Call it in `_run_fetch_job_inner()` after `_persist_bars()` succeeds
   - Rewrite `_probe_covered_days()` to query summary table
   - Add fallback to old query if summary table doesn't exist

2. **`migrations/020_coverage_summary_table.sql`**: New file

3. **Test**: Run fetch job, verify summary table updates match actual bars stored

## Acceptance Criteria

- [ ] Page load with coverage check completes in <2 seconds
- [ ] Coverage data matches actual bars in database
- [ ] Fetch job updates summary table incrementally
- [ ] No regression in fetch throughput (bars/sec)
- [ ] Graceful fallback if summary table missing
- [ ] Migration script tested on production-like data volume

## Dependencies

- PostgreSQL access (forward_test database)
- Existing `market_data_cache` hypertable with data
- Migration system (check how other migrations are applied)

## Related Issues

- Memory note: [[db-connection-exhaustion]] - heavy queries contribute to connection pool pressure
- Memory note: [[data-refresh-reminder-initiative]] - freshness chip relies on this query
- PRD: Backtest data-source routing uses coverage to determine what's available for backtesting
