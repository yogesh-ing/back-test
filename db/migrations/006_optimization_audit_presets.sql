-- =============================================================================
-- Parameter Optimization Engine — presets + audit
-- Migration : 006_optimization_audit_presets
-- Engine    : PostgreSQL 13+
-- Alembic   : revision 006
-- =============================================================================
--
-- WHAT THIS DOES
--   parameter_presets   named, reusable parameter sets per strategy. Sources:
--                       optimization | manual | default | import | snapshot
--                       ('snapshot' = automatic "params before apply" copy,
--                       the rollback plan's safety net).
--   optimization_audit  append-only log of runs started/completed/cancelled,
--                       parameters applied / rolled back, presets saved.
--
-- ROLLBACK
--   db/migrations/005_009_optimization_rollback.sql
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS parameter_presets (
    preset_id            UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_id          VARCHAR(100)  NOT NULL,
    name                 VARCHAR(100)  NOT NULL,
    description          TEXT,
    params               JSONB         NOT NULL,

    source               VARCHAR(50)   NOT NULL,
    optimization_run_id  UUID,
    backtest_metrics     JSONB,

    is_active            BOOLEAN       NOT NULL DEFAULT true,
    applied_count        INTEGER       NOT NULL DEFAULT 0,
    last_applied_at      TIMESTAMPTZ,

    created_by           VARCHAR(100),
    created_at           TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ   NOT NULL DEFAULT now(),

    CONSTRAINT fk_presets_opt_run FOREIGN KEY (optimization_run_id)
        REFERENCES optimization_runs (run_id) ON DELETE SET NULL,
    CONSTRAINT uq_strategy_preset_name UNIQUE (strategy_id, name),
    CONSTRAINT ck_presets_source CHECK
        (source IN ('optimization','manual','default','import','snapshot')),
    CONSTRAINT ck_presets_applied_nonneg CHECK (applied_count >= 0)
);

COMMENT ON TABLE parameter_presets IS 'Saved parameter presets for strategies';

DROP TRIGGER IF EXISTS trg_parameter_presets_updated_at ON parameter_presets;
CREATE TRIGGER trg_parameter_presets_updated_at
    BEFORE UPDATE ON parameter_presets
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE IF NOT EXISTS optimization_audit (
    audit_id           UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id             UUID,
    strategy_id        VARCHAR(100)  NOT NULL,

    action             VARCHAR(50)   NOT NULL,
    action_details     JSONB,

    old_params         JSONB,
    new_params         JSONB,
    params_diff        JSONB,

    applied_to_bucket  VARCHAR(100),
    applied_to_mode    VARCHAR(20),
    runner_restarted   BOOLEAN,

    expected_impact    JSONB,
    actual_impact      JSONB,

    requires_approval  BOOLEAN       NOT NULL DEFAULT false,
    approved_by        VARCHAR(100),
    approved_at        TIMESTAMPTZ,

    user_id            VARCHAR(100),
    "timestamp"        TIMESTAMPTZ   NOT NULL DEFAULT now(),
    ip_address         VARCHAR(45),
    user_agent         VARCHAR(255),

    CONSTRAINT fk_audit_opt_run FOREIGN KEY (run_id)
        REFERENCES optimization_runs (run_id) ON DELETE SET NULL,
    CONSTRAINT ck_audit_mode CHECK
        (applied_to_mode IS NULL OR applied_to_mode IN ('paper', 'live'))
);

COMMENT ON TABLE optimization_audit IS
    'Audit log for all optimization and parameter change actions';

INSERT INTO schema_migrations (version, description)
VALUES ('006', 'optimization engine: parameter_presets + optimization_audit')
ON CONFLICT (version) DO NOTHING;

COMMIT;

-- =============================================================================
-- END 006_optimization_audit_presets.sql
-- =============================================================================
