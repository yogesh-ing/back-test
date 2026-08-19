-- =============================================================================
-- Forward Testing Simulator — Rollback for 001_initial_schema
-- Engine : PostgreSQL 13+
-- =============================================================================
--
-- WARNING: THIS DESTROYS ALL FORWARD TESTING DATA.
-- Take a dump first:
--     pg_dump -Fc -d forward_test -f backup_before_rollback.dump
--
-- Drop order is the reverse of the dependency graph. CASCADE is used on the
-- views only; tables are dropped in an order that satisfies the foreign keys
-- so that an unexpected dependency raises an error instead of being silently
-- destroyed.
-- =============================================================================

BEGIN;

DROP VIEW IF EXISTS v_portfolio_summary;
DROP VIEW IF EXISTS v_open_positions;

-- Leaf tables first (things that reference others).
DROP TABLE IF EXISTS system_logs;
DROP TABLE IF EXISTS strategy_signals;
DROP TABLE IF EXISTS performance_metrics;
DROP TABLE IF EXISTS market_data_cache;
DROP TABLE IF EXISTS equity_curve;
DROP TABLE IF EXISTS trades;
DROP TABLE IF EXISTS fills;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS positions;
DROP TABLE IF EXISTS portfolios;

DROP FUNCTION IF EXISTS set_updated_at();

DELETE FROM schema_migrations WHERE version = '001';

-- schema_migrations itself is intentionally NOT dropped: it is shared
-- bookkeeping across all migrations. Drop it manually if you are tearing the
-- whole database down.

COMMIT;
