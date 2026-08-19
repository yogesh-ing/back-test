-- =============================================================================
-- Forward Testing Simulator — Schema Verification
-- Run AFTER applying 001_initial_schema.sql to confirm the migration landed.
--
--   psql -d forward_test -f db/verify_schema.sql
--
-- Every check prints PASS or FAIL in the `result` column. If anything says
-- FAIL, do not proceed to Step 2 — re-read the guide's troubleshooting table.
-- =============================================================================

\echo '=============================================='
\echo ' Forward Testing Schema Verification'
\echo '=============================================='
\echo ''

\echo '--- 1. Tables (expect 10 + schema_migrations = 11) ---'
SELECT
    count(*) AS table_count,
    CASE WHEN count(*) = 11 THEN 'PASS' ELSE 'FAIL — expected 11' END AS result
FROM information_schema.tables
WHERE table_schema = 'public' AND table_type = 'BASE TABLE';

SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
ORDER BY table_name;

\echo ''
\echo '--- 2. Every expected table present ---'
WITH expected(name) AS (
    VALUES ('portfolios'),('positions'),('orders'),('fills'),('trades'),
           ('equity_curve'),('market_data_cache'),('performance_metrics'),
           ('strategy_signals'),('system_logs')
)
SELECT
    e.name AS expected_table,
    CASE WHEN t.table_name IS NULL THEN 'FAIL — MISSING' ELSE 'PASS' END AS result
FROM expected e
LEFT JOIN information_schema.tables t
       ON t.table_name = e.name AND t.table_schema = 'public'
ORDER BY e.name;

\echo ''
\echo '--- 3. Views (expect 2) ---'
SELECT
    count(*) AS view_count,
    CASE WHEN count(*) = 2 THEN 'PASS' ELSE 'FAIL — expected 2' END AS result
FROM information_schema.views
WHERE table_schema = 'public';

\echo ''
\echo '--- 4. Foreign keys (expect 14) ---'
-- positions 1 + orders 2 + fills 2 + trades 4 + equity_curve 1
-- + performance_metrics 1 + strategy_signals 2 + system_logs 1 = 14
SELECT
    count(*) AS fk_count,
    CASE WHEN count(*) = 14 THEN 'PASS' ELSE 'FAIL — expected 14' END AS result
FROM information_schema.table_constraints
WHERE table_schema = 'public' AND constraint_type = 'FOREIGN KEY';

SELECT
    tc.table_name,
    tc.constraint_name,
    ccu.table_name AS references_table,
    rc.delete_rule
FROM information_schema.table_constraints tc
JOIN information_schema.constraint_column_usage ccu
     ON ccu.constraint_name = tc.constraint_name
JOIN information_schema.referential_constraints rc
     ON rc.constraint_name = tc.constraint_name
WHERE tc.table_schema = 'public' AND tc.constraint_type = 'FOREIGN KEY'
ORDER BY tc.table_name, tc.constraint_name;

\echo ''
\echo '--- 5. CHECK constraints per table ---'
SELECT
    rel.relname AS table_name,
    count(*)    AS check_constraints
FROM pg_constraint con
JOIN pg_class rel ON rel.oid = con.conrelid
JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
WHERE con.contype = 'c' AND nsp.nspname = 'public'
GROUP BY rel.relname
ORDER BY rel.relname;

\echo ''
\echo '--- 6. Indexes (expect 46 incl. PK/unique) ---'
SELECT
    count(*) AS index_count,
    CASE WHEN count(*) >= 46 THEN 'PASS' ELSE 'FAIL — expected >= 46' END AS result
FROM pg_indexes WHERE schemaname = 'public';

\echo ''
\echo '--- 7. Critical partial indexes ---'
WITH expected(name) AS (
    VALUES ('uq_positions_one_open_per_symbol'),
           ('uq_orders_client_order_id'),
           ('ix_orders_working'),
           ('ix_signals_unexecuted'),
           ('ix_logs_errors')
)
SELECT
    e.name AS expected_index,
    CASE WHEN i.indexname IS NULL THEN 'FAIL — MISSING' ELSE 'PASS' END AS result
FROM expected e
LEFT JOIN pg_indexes i ON i.indexname = e.name AND i.schemaname = 'public'
ORDER BY e.name;

\echo ''
\echo '--- 8. updated_at triggers (expect 2) ---'
SELECT
    count(*) AS trigger_count,
    CASE WHEN count(*) = 2 THEN 'PASS' ELSE 'FAIL — expected 2' END AS result
FROM pg_trigger
WHERE NOT tgisinternal AND tgname LIKE '%updated_at%';

\echo ''
\echo '--- 9. gen_random_uuid() available ---'
SELECT
    CASE WHEN to_regproc('gen_random_uuid') IS NOT NULL
         THEN 'PASS' ELSE 'FAIL — no UUID generator' END AS result;

\echo ''
\echo '--- 10. Money columns must be NUMERIC, never float ---'
SELECT
    table_name, column_name, data_type,
    'FAIL — must be numeric' AS result
FROM information_schema.columns
WHERE table_schema = 'public'
  AND (column_name LIKE '%price%' OR column_name LIKE '%pnl%'
       OR column_name LIKE '%cash%' OR column_name LIKE '%equity%'
       OR column_name LIKE '%capital%' OR column_name LIKE '%commission%')
  -- Surrogate keys such as equity_id are integers by design, not money.
  AND column_name NOT LIKE '%\_id'
  AND data_type <> 'numeric'
ORDER BY table_name, column_name;
-- An EMPTY result above means every money column is numeric. PASS.

\echo ''
\echo '--- 11. Migration recorded ---'
SELECT version, description, applied_at,
       'PASS' AS result
FROM schema_migrations
ORDER BY version;

\echo ''
\echo '=============================================='
\echo ' Verification complete.'
\echo ' Review any FAIL rows above before continuing.'
\echo '=============================================='
