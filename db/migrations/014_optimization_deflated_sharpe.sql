-- =============================================================================
-- Parameter Optimization Engine — deflated Sharpe column
-- Migration : 014_optimization_deflated_sharpe
-- Engine    : PostgreSQL 13+
-- Alembic   : revision 014 (hand-applied equivalent of 20260929_1000_014)
--
-- PRD backTest-enhance Part 2 §3.
--
-- Stores SR₀ — the Sharpe the best-of-N had to beat — on the same scale as
-- best_metrics.sharpe, which is what makes it sortable and comparable in a
-- list. The full statistic (DSR probability, trial count, observations) lives
-- in analysis->'deflated_sharpe'.
--
-- NUMERIC(6, 3) rather than matching robustness_score's (4, 2): the bar rises
-- with the size of the search, so a wide search over a narrow distribution of
-- trial Sharpes produces values well past 99.99. Clamping into four digits
-- would be wrong rather than merely lossy.
-- =============================================================================

BEGIN;

ALTER TABLE optimization_runs ADD COLUMN IF NOT EXISTS deflated_sharpe NUMERIC(6, 3);

-- The two reporting views project their column list explicitly, and
-- CREATE OR REPLACE VIEW cannot change one — they are replaced whole.
CREATE OR REPLACE VIEW v_latest_optimization AS
SELECT DISTINCT ON (strategy_id)
    run_id, strategy_id, bucket_id, objective_function, method, status,
    best_score, best_params, baseline_score, overfitted, robustness_score,
    deflated_sharpe, completed_at, created_by
FROM optimization_runs
WHERE status = 'completed'
ORDER BY strategy_id, completed_at DESC;

CREATE OR REPLACE VIEW v_optimization_summary AS
SELECT
    o.run_id, o.strategy_id, o.objective_function, o.method, o.status,
    o.total_combinations, o.valid_combinations, o.best_score, o.baseline_score,
    o.overfitted, o.robustness_score, o.deflated_sharpe,
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

INSERT INTO schema_migrations (version, description)
VALUES ('014', 'optimization engine: deflated Sharpe column (PRD Part 2 §3)')
ON CONFLICT (version) DO NOTHING;

COMMIT;

-- =============================================================================
-- END 014_optimization_deflated_sharpe.sql
-- =============================================================================
