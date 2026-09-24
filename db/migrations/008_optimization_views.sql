-- =============================================================================
-- Parameter Optimization Engine — analytics views
-- Migration : 008_optimization_views
-- Engine    : PostgreSQL 13+ (DISTINCT ON — no SQLite mirror)
-- Alembic   : revision 008
-- =============================================================================

BEGIN;

-- Latest completed optimization per strategy
CREATE OR REPLACE VIEW v_latest_optimization AS
SELECT DISTINCT ON (strategy_id)
    run_id, strategy_id, bucket_id, objective_function, method, status,
    best_score, best_params, baseline_score, overfitted, robustness_score,
    completed_at, created_by
FROM optimization_runs
WHERE status = 'completed'
ORDER BY strategy_id, completed_at DESC;

-- Top 10 constraint-passing results per run
CREATE OR REPLACE VIEW v_top_results AS
SELECT
    r.result_id, r.run_id, o.strategy_id, o.objective_function,
    r.params, r.objective_score, r.sharpe, r.total_return, r.max_drawdown,
    r.profit_factor, r.win_rate, r.total_trades, r.rank
FROM optimization_results r
JOIN optimization_runs o ON r.run_id = o.run_id
WHERE r.rank IS NOT NULL AND r.rank <= 10;

-- Score distribution + runtime per completed run
CREATE OR REPLACE VIEW v_optimization_summary AS
SELECT
    o.run_id, o.strategy_id, o.objective_function, o.method, o.status,
    o.total_combinations, o.valid_combinations, o.best_score, o.baseline_score,
    o.overfitted, o.robustness_score,
    COUNT(r.result_id)          AS results_count,
    AVG(r.objective_score)      AS avg_score,
    STDDEV(r.objective_score)   AS score_stddev,
    MIN(r.objective_score)      AS min_score,
    MAX(r.objective_score)      AS max_score,
    EXTRACT(EPOCH FROM (o.completed_at - o.started_at)) / 60 AS runtime_minutes
FROM optimization_runs o
LEFT JOIN optimization_results r ON o.run_id = r.run_id
WHERE o.status = 'completed'
GROUP BY o.run_id;

-- Parameter change history (applies, rollbacks, manual overrides)
CREATE OR REPLACE VIEW v_parameter_history AS
SELECT
    a.audit_id, a.strategy_id, a.action, a.old_params, a.new_params,
    a.params_diff, a.applied_to_bucket, a.applied_to_mode, a.user_id,
    a."timestamp", o.best_score AS optimization_score,
    o.method AS optimization_method
FROM optimization_audit a
LEFT JOIN optimization_runs o ON a.run_id = o.run_id
WHERE a.action IN ('params_applied', 'params_rolled_back', 'manual_override');

-- Active presets with the score of the run they came from
CREATE OR REPLACE VIEW v_active_presets AS
SELECT
    p.preset_id, p.strategy_id, p.name, p.description, p.params, p.source,
    p.backtest_metrics, p.applied_count, p.last_applied_at, p.created_at,
    o.best_score AS optimization_score, o.completed_at AS optimization_date
FROM parameter_presets p
LEFT JOIN optimization_runs o ON p.optimization_run_id = o.run_id
WHERE p.is_active = true;

INSERT INTO schema_migrations (version, description)
VALUES ('008', 'optimization engine: 5 analytics views')
ON CONFLICT (version) DO NOTHING;

COMMIT;

-- =============================================================================
-- END 008_optimization_views.sql
-- =============================================================================
