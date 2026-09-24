-- =============================================================================
-- Parameter Optimization Engine — indexes
-- Migration : 007_optimization_indexes
-- Engine    : PostgreSQL 13+
-- Alembic   : revision 007
-- =============================================================================
--
-- The GIN index on optimization_results.params uses the default jsonb_ops
-- operator class — no btree_gin / pg_trgm extension is required. It serves
-- containment lookups such as:
--     SELECT * FROM optimization_results WHERE params @> '{"fast": 10}';
--
-- On a large, live optimization_results table build the GIN index with
-- CREATE INDEX CONCURRENTLY outside a transaction instead.
-- =============================================================================

BEGIN;

-- optimization_runs
CREATE INDEX IF NOT EXISTS idx_opt_runs_strategy        ON optimization_runs (strategy_id);
CREATE INDEX IF NOT EXISTS idx_opt_runs_status          ON optimization_runs (status);
CREATE INDEX IF NOT EXISTS idx_opt_runs_created         ON optimization_runs (created_at);
CREATE INDEX IF NOT EXISTS idx_opt_runs_strategy_status ON optimization_runs (strategy_id, status);
CREATE INDEX IF NOT EXISTS idx_opt_runs_bucket          ON optimization_runs (bucket_id);

-- optimization_results
CREATE INDEX IF NOT EXISTS idx_opt_results_run          ON optimization_results (run_id);
CREATE INDEX IF NOT EXISTS idx_opt_results_rank         ON optimization_results (run_id, rank)
    WHERE rank IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_opt_results_score        ON optimization_results (run_id, objective_score);
CREATE INDEX IF NOT EXISTS idx_opt_results_constraints  ON optimization_results (run_id, constraints_met);
CREATE INDEX IF NOT EXISTS idx_opt_results_params_gin   ON optimization_results USING gin (params);

-- parameter_presets
CREATE INDEX IF NOT EXISTS idx_presets_strategy         ON parameter_presets (strategy_id);
CREATE INDEX IF NOT EXISTS idx_presets_active           ON parameter_presets (strategy_id, is_active)
    WHERE is_active = true;
CREATE INDEX IF NOT EXISTS idx_presets_source           ON parameter_presets (source);

-- optimization_audit
CREATE INDEX IF NOT EXISTS idx_audit_strategy           ON optimization_audit (strategy_id);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp          ON optimization_audit ("timestamp");
CREATE INDEX IF NOT EXISTS idx_audit_action             ON optimization_audit (action);
CREATE INDEX IF NOT EXISTS idx_audit_user               ON optimization_audit (user_id);
CREATE INDEX IF NOT EXISTS idx_audit_bucket             ON optimization_audit (applied_to_bucket);
CREATE INDEX IF NOT EXISTS idx_audit_run                ON optimization_audit (run_id);

INSERT INTO schema_migrations (version, description)
VALUES ('007', 'optimization engine: secondary + partial + GIN indexes')
ON CONFLICT (version) DO NOTHING;

COMMIT;

-- =============================================================================
-- END 007_optimization_indexes.sql
-- =============================================================================
