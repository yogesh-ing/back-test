-- =============================================================================
-- Parameter Optimization Engine — ROLLBACK of migrations 005-009
-- Engine : PostgreSQL 13+
-- =============================================================================
--
-- DESTROYS every optimization run, result, preset and audit row.
-- Take a backup first:
--     pg_dump -t 'optimization_*' -t parameter_presets forward_test > opt_backup.sql
--
-- Alembic equivalent:  alembic downgrade 004
-- set_updated_at() belongs to 001 and is intentionally left in place.
-- =============================================================================

BEGIN;

-- Views first (they depend on the tables)
DROP VIEW IF EXISTS v_active_presets;
DROP VIEW IF EXISTS v_parameter_history;
DROP VIEW IF EXISTS v_optimization_summary;
DROP VIEW IF EXISTS v_top_results;
DROP VIEW IF EXISTS v_latest_optimization;

-- Tables in reverse dependency order (indexes/triggers go with them)
DROP TABLE IF EXISTS optimization_audit;
DROP TABLE IF EXISTS parameter_presets;
DROP TABLE IF EXISTS optimization_results;
DROP TABLE IF EXISTS optimization_runs;

DELETE FROM schema_migrations WHERE version IN ('005', '006', '007', '008', '009');

COMMIT;

-- =============================================================================
-- END 005_009_optimization_rollback.sql
-- =============================================================================
