-- =============================================================================
-- Parameter Optimization Engine — data attestation columns
-- Migration : 015_optimization_data_attestation
-- Engine    : PostgreSQL 13+
-- Alembic   : revision 015 (hand-applied equivalent of 20260929_1100_015)
--
-- PRD backTest-enhance Part 2 §2.
--
-- A run now carries a record of *what it actually ran on* — which source, how
-- many bars, over what range, fetched when — instead of leaving that to be
-- inferred from whatever the app happened to be started with.
--
-- Strictly additive and strictly unbackfilled. A row written before this
-- migration has no attestation, and that is the honest state of it: a
-- backfilled value would be a claim nobody checked. The UI reads the missing
-- record as "not recorded" and marks the derived fallback `derived: true`.
--
-- data_attestation holds the whole JSON record (including staleness and the
-- synthetic acknowledgement); the flat columns beside it exist so runs can be
-- listed and filtered by source without unpacking JSON in every query. Both
-- are written together by attestation_columns() in the application, which is
-- the only thing that knows the split.
--
-- date_from/date_to are what the candles COVERED, not what was requested. A
-- symbol with gaps reports a narrower range than was asked for, and that gap
-- is exactly what an operator needs to see.
-- =============================================================================

BEGIN;

ALTER TABLE optimization_runs ADD COLUMN IF NOT EXISTS data_source     VARCHAR(30);
ALTER TABLE optimization_runs ADD COLUMN IF NOT EXISTS data_fetch_date DATE;
ALTER TABLE optimization_runs ADD COLUMN IF NOT EXISTS bars_count      INTEGER;
ALTER TABLE optimization_runs ADD COLUMN IF NOT EXISTS symbol          VARCHAR(30);
ALTER TABLE optimization_runs ADD COLUMN IF NOT EXISTS timeframe       VARCHAR(10);
ALTER TABLE optimization_runs ADD COLUMN IF NOT EXISTS date_from       DATE;
ALTER TABLE optimization_runs ADD COLUMN IF NOT EXISTS date_to         DATE;
ALTER TABLE optimization_runs ADD COLUMN IF NOT EXISTS data_attestation JSONB;

-- Partial index: only completed runs are ever listed by source, and the runs
-- that matter are overwhelmingly real-data. Partial so the index stays small
-- on a table that accumulates one row per search.
CREATE INDEX IF NOT EXISTS ix_optimization_runs_data_source
    ON optimization_runs (data_source)
    WHERE status = 'completed';

-- The two reporting views project their column list explicitly, and
-- CREATE OR REPLACE VIEW cannot change one — they are replaced whole.
CREATE OR REPLACE VIEW v_latest_optimization AS
SELECT DISTINCT ON (strategy_id)
    run_id, strategy_id, bucket_id, objective_function, method, status,
    best_score, best_params, baseline_score, overfitted, robustness_score,
    deflated_sharpe, data_source, symbol, timeframe, bars_count,
    completed_at, created_by
FROM optimization_runs
WHERE status = 'completed'
ORDER BY strategy_id, completed_at DESC;

CREATE OR REPLACE VIEW v_optimization_summary AS
SELECT
    o.run_id, o.strategy_id, o.objective_function, o.method, o.status,
    o.total_combinations, o.valid_combinations, o.best_score, o.baseline_score,
    o.overfitted, o.robustness_score, o.deflated_sharpe,
    o.data_source, o.symbol, o.timeframe, o.bars_count,
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
VALUES ('015', 'optimization engine: data attestation columns (PRD Part 2 §2)')
ON CONFLICT (version) DO NOTHING;

COMMIT;

-- =============================================================================
-- END 015_optimization_data_attestation.sql
-- =============================================================================
