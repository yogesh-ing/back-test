-- =============================================================================
-- Parameter Optimization Engine — core tables
-- Migration : 005_optimization_core
-- Engine    : PostgreSQL 13+ (gen_random_uuid() is core since 13)
-- Alembic   : revision 005 (db/alembic/versions/*_005_optimization_core.py)
-- =============================================================================
--
-- WHAT THIS DOES
--   optimization_runs     one row per optimization job: config (param space,
--                         objective, constraints, backtest window), lifecycle,
--                         progress counters, best result, walk-forward verdict,
--                         original-vs-optimized comparison and analytics.
--   optimization_results  one row per tested parameter combination with its
--                         standardized metrics, constraint verdict and rank.
--
--   Units: total_return / max_drawdown / cagr are decimal fractions
--   (0.124 = 12.4 %, max_drawdown is negative); win_rate is a percentage.
--   rank is assigned to constraint-passing rows only (1 = best).
--
-- DEPENDS ON
--   set_updated_at() from 001 (re-declared below with CREATE OR REPLACE so the
--   file is self-sufficient on databases that were stamped).
--
-- IDEMPOTENCY
--   CREATE ... IF NOT EXISTS / DROP TRIGGER IF EXISTS / ON CONFLICT DO NOTHING.
--
-- ROLLBACK
--   db/migrations/005_009_optimization_rollback.sql (drops 005-009 objects).
-- =============================================================================

BEGIN;

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- -----------------------------------------------------------------------------
-- optimization_runs
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS optimization_runs (
    run_id               UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_id          VARCHAR(100)  NOT NULL,
    bucket_id            VARCHAR(100),

    -- Configuration
    objective_function   VARCHAR(50)   NOT NULL,
    method               VARCHAR(50)   NOT NULL,
    param_space          JSONB         NOT NULL,
    constraints          JSONB,
    backtest_config      JSONB         NOT NULL,

    -- Status tracking
    status               VARCHAR(20)   NOT NULL DEFAULT 'pending',
    started_at           TIMESTAMPTZ,
    completed_at         TIMESTAMPTZ,
    error_message        TEXT,

    -- Progress
    total_combinations   INTEGER,
    tested_combinations  INTEGER       NOT NULL DEFAULT 0,
    valid_combinations   INTEGER       NOT NULL DEFAULT 0,

    -- Results summary
    best_params          JSONB,
    best_score           NUMERIC(10,4),
    best_metrics         JSONB,

    -- Original-vs-optimized comparison (extension)
    baseline_params      JSONB,
    baseline_metrics     JSONB,
    baseline_score       NUMERIC(10,4),

    -- Walk-forward validation
    walk_forward_enabled BOOLEAN       NOT NULL DEFAULT false,
    walk_forward_config  JSONB,
    walk_forward_results JSONB,
    overfitted           BOOLEAN,
    avg_train_score      NUMERIC(10,4),
    avg_test_score       NUMERIC(10,4),

    -- Analytics (extension)
    analysis             JSONB,
    robustness_score     NUMERIC(4,2),

    -- Metadata
    created_by           VARCHAR(100),
    created_at           TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ   NOT NULL DEFAULT now(),
    task_id              VARCHAR(100),

    CONSTRAINT ck_opt_runs_status CHECK (status IN
        ('draft','pending','running','paused','completed','failed','cancelled')),
    CONSTRAINT ck_opt_runs_objective CHECK (objective_function IN
        ('sharpe','sortino','calmar','total_return','profit_factor','expectancy')),
    CONSTRAINT ck_opt_runs_method CHECK (method IN ('grid','random','bayesian','genetic')),
    CONSTRAINT ck_opt_runs_tested_nonneg CHECK (tested_combinations >= 0),
    CONSTRAINT ck_opt_runs_valid_nonneg CHECK (valid_combinations >= 0)
);

COMMENT ON TABLE optimization_runs IS 'Main table for optimization runs';

DROP TRIGGER IF EXISTS trg_optimization_runs_updated_at ON optimization_runs;
CREATE TRIGGER trg_optimization_runs_updated_at
    BEFORE UPDATE ON optimization_runs
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- -----------------------------------------------------------------------------
-- optimization_results
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS optimization_results (
    result_id                UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id                   UUID          NOT NULL,
    params                   JSONB         NOT NULL,

    -- Core metrics
    sharpe                   NUMERIC(10,4),
    sortino                  NUMERIC(10,4),
    calmar                   NUMERIC(10,4),
    total_return             NUMERIC(10,4),
    cagr                     NUMERIC(10,4),
    max_drawdown             NUMERIC(10,4),
    drawdown_duration_days   INTEGER,

    -- Trade statistics
    profit_factor            NUMERIC(10,4),
    win_rate                 NUMERIC(5,2),
    total_trades             INTEGER,
    winning_trades           INTEGER,
    losing_trades            INTEGER,
    expectancy               NUMERIC(10,2),
    avg_win                  NUMERIC(10,2),
    avg_loss                 NUMERIC(10,2),
    largest_win              NUMERIC(10,2),
    largest_loss             NUMERIC(10,2),

    -- Risk metrics
    volatility               NUMERIC(10,4),
    downside_deviation       NUMERIC(10,4),

    -- Execution quality
    avg_holding_time_minutes INTEGER,
    avg_slippage             NUMERIC(10,4),

    -- Constraint validation
    constraints_met          BOOLEAN       NOT NULL DEFAULT true,
    constraint_violations    JSONB,

    -- Scoring
    objective_score          NUMERIC(10,4) NOT NULL,
    rank                     INTEGER,

    full_result              JSONB,
    created_at               TIMESTAMPTZ   NOT NULL DEFAULT now(),

    CONSTRAINT fk_opt_results_run FOREIGN KEY (run_id)
        REFERENCES optimization_runs (run_id) ON DELETE CASCADE,
    CONSTRAINT ck_opt_results_rank_pos CHECK (rank IS NULL OR rank >= 1),
    CONSTRAINT ck_opt_results_win_rate CHECK
        (win_rate IS NULL OR (win_rate >= 0 AND win_rate <= 100))
);

COMMENT ON TABLE optimization_results IS 'Individual parameter combination results';

INSERT INTO schema_migrations (version, description)
VALUES ('005', 'optimization engine: optimization_runs + optimization_results')
ON CONFLICT (version) DO NOTHING;

COMMIT;

-- =============================================================================
-- END 005_optimization_core.sql
-- =============================================================================
