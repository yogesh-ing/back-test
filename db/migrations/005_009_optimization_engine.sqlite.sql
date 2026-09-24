-- =============================================================================
-- Parameter Optimization Engine — SQLite mirror of migrations 005, 006, 007, 009
-- Engine : SQLite 3.24+  (local development only; production is PostgreSQL)
-- =============================================================================
--
-- Generated from the ORM models (backtest.db.models) and verified against them
-- by tests/db/test_migrations_005_009.py. 008 (analytics views) is PostgreSQL
-- only. UUIDs are TEXT and JSONB is TEXT here; the application supplies uuid4
-- primary keys, so no server-side UUID default is needed.
--
--     sqlite3 forward_test.db < db/migrations/005_009_optimization_engine.sqlite.sql
-- =============================================================================

PRAGMA foreign_keys = ON;

BEGIN;

CREATE TABLE IF NOT EXISTS optimization_runs (
	run_id VARCHAR(36) NOT NULL, 
	strategy_id VARCHAR(100) NOT NULL, 
	bucket_id VARCHAR(100), 
	objective_function VARCHAR(50) NOT NULL, 
	method VARCHAR(50) NOT NULL, 
	param_space JSON NOT NULL, 
	constraints JSON, 
	backtest_config JSON NOT NULL, 
	status VARCHAR(20) DEFAULT 'pending' NOT NULL, 
	started_at DATETIME, 
	completed_at DATETIME, 
	error_message TEXT, 
	total_combinations INTEGER, 
	tested_combinations INTEGER DEFAULT 0 NOT NULL, 
	valid_combinations INTEGER DEFAULT 0 NOT NULL, 
	best_params JSON, 
	best_score NUMERIC(10, 4), 
	best_metrics JSON, 
	baseline_params JSON, 
	baseline_metrics JSON, 
	baseline_score NUMERIC(10, 4), 
	walk_forward_enabled BOOLEAN DEFAULT false NOT NULL, 
	walk_forward_config JSON, 
	walk_forward_results JSON, 
	overfitted BOOLEAN, 
	avg_train_score NUMERIC(10, 4), 
	avg_test_score NUMERIC(10, 4), 
	analysis JSON, 
	robustness_score NUMERIC(4, 2), 
	created_by VARCHAR(100), 
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	task_id VARCHAR(100), 
	PRIMARY KEY (run_id), 
	CONSTRAINT ck_opt_runs_status CHECK (status IN ('draft','pending','running','paused','completed','failed','cancelled')), 
	CONSTRAINT ck_opt_runs_objective CHECK (objective_function IN ('sharpe','sortino','calmar','total_return','profit_factor','expectancy')), 
	CONSTRAINT ck_opt_runs_method CHECK (method IN ('grid','random','bayesian','genetic')), 
	CONSTRAINT ck_opt_runs_tested_nonneg CHECK (tested_combinations >= 0), 
	CONSTRAINT ck_opt_runs_valid_nonneg CHECK (valid_combinations >= 0)
);

CREATE INDEX IF NOT EXISTS idx_opt_runs_bucket ON optimization_runs (bucket_id);
CREATE INDEX IF NOT EXISTS idx_opt_runs_created ON optimization_runs (created_at);
CREATE INDEX IF NOT EXISTS idx_opt_runs_status ON optimization_runs (status);
CREATE INDEX IF NOT EXISTS idx_opt_runs_strategy ON optimization_runs (strategy_id);
CREATE INDEX IF NOT EXISTS idx_opt_runs_strategy_status ON optimization_runs (strategy_id, status);

CREATE TABLE IF NOT EXISTS optimization_results (
	result_id VARCHAR(36) NOT NULL, 
	run_id VARCHAR(36) NOT NULL, 
	params JSON NOT NULL, 
	sharpe NUMERIC(10, 4), 
	sortino NUMERIC(10, 4), 
	calmar NUMERIC(10, 4), 
	total_return NUMERIC(10, 4), 
	cagr NUMERIC(10, 4), 
	max_drawdown NUMERIC(10, 4), 
	drawdown_duration_days INTEGER, 
	profit_factor NUMERIC(10, 4), 
	win_rate NUMERIC(5, 2), 
	total_trades INTEGER, 
	winning_trades INTEGER, 
	losing_trades INTEGER, 
	expectancy NUMERIC(10, 2), 
	avg_win NUMERIC(10, 2), 
	avg_loss NUMERIC(10, 2), 
	largest_win NUMERIC(10, 2), 
	largest_loss NUMERIC(10, 2), 
	volatility NUMERIC(10, 4), 
	downside_deviation NUMERIC(10, 4), 
	avg_holding_time_minutes INTEGER, 
	avg_slippage NUMERIC(10, 4), 
	constraints_met BOOLEAN DEFAULT true NOT NULL, 
	constraint_violations JSON, 
	objective_score NUMERIC(10, 4) NOT NULL, 
	rank INTEGER, 
	full_result JSON, 
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	PRIMARY KEY (result_id), 
	CONSTRAINT ck_opt_results_rank_pos CHECK (rank IS NULL OR rank >= 1), 
	CONSTRAINT ck_opt_results_win_rate CHECK (win_rate IS NULL OR (win_rate >= 0 AND win_rate <= 100)), 
	CONSTRAINT fk_opt_results_run FOREIGN KEY(run_id) REFERENCES optimization_runs (run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_opt_results_constraints ON optimization_results (run_id, constraints_met);
CREATE INDEX IF NOT EXISTS idx_opt_results_rank ON optimization_results (run_id, rank) WHERE rank IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_opt_results_run ON optimization_results (run_id);
CREATE INDEX IF NOT EXISTS idx_opt_results_score ON optimization_results (run_id, objective_score);

CREATE TABLE IF NOT EXISTS parameter_presets (
	preset_id VARCHAR(36) NOT NULL, 
	strategy_id VARCHAR(100) NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	description TEXT, 
	params JSON NOT NULL, 
	source VARCHAR(50) NOT NULL, 
	optimization_run_id VARCHAR(36), 
	backtest_metrics JSON, 
	is_active BOOLEAN DEFAULT true NOT NULL, 
	applied_count INTEGER DEFAULT 0 NOT NULL, 
	last_applied_at DATETIME, 
	created_by VARCHAR(100), 
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	PRIMARY KEY (preset_id), 
	CONSTRAINT uq_strategy_preset_name UNIQUE (strategy_id, name), 
	CONSTRAINT ck_presets_source CHECK (source IN ('optimization','manual','default','import','snapshot')), 
	CONSTRAINT ck_presets_applied_nonneg CHECK (applied_count >= 0), 
	CONSTRAINT fk_presets_opt_run FOREIGN KEY(optimization_run_id) REFERENCES optimization_runs (run_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_presets_active ON parameter_presets (strategy_id, is_active) WHERE is_active = 1;
CREATE INDEX IF NOT EXISTS idx_presets_source ON parameter_presets (source);
CREATE INDEX IF NOT EXISTS idx_presets_strategy ON parameter_presets (strategy_id);

CREATE TABLE IF NOT EXISTS optimization_audit (
	audit_id VARCHAR(36) NOT NULL, 
	run_id VARCHAR(36), 
	strategy_id VARCHAR(100) NOT NULL, 
	action VARCHAR(50) NOT NULL, 
	action_details JSON, 
	old_params JSON, 
	new_params JSON, 
	params_diff JSON, 
	applied_to_bucket VARCHAR(100), 
	applied_to_mode VARCHAR(20), 
	runner_restarted BOOLEAN, 
	expected_impact JSON, 
	actual_impact JSON, 
	requires_approval BOOLEAN DEFAULT false NOT NULL, 
	approved_by VARCHAR(100), 
	approved_at DATETIME, 
	user_id VARCHAR(100), 
	timestamp DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	ip_address VARCHAR(45), 
	user_agent VARCHAR(255), 
	PRIMARY KEY (audit_id), 
	CONSTRAINT ck_audit_mode CHECK (applied_to_mode IS NULL OR applied_to_mode IN ('paper', 'live')), 
	CONSTRAINT fk_audit_opt_run FOREIGN KEY(run_id) REFERENCES optimization_runs (run_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_action ON optimization_audit (action);
CREATE INDEX IF NOT EXISTS idx_audit_bucket ON optimization_audit (applied_to_bucket);
CREATE INDEX IF NOT EXISTS idx_audit_run ON optimization_audit (run_id);
CREATE INDEX IF NOT EXISTS idx_audit_strategy ON optimization_audit (strategy_id);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON optimization_audit (timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_user ON optimization_audit (user_id);

-- Seed presets (009)
INSERT OR IGNORE INTO parameter_presets
    (preset_id, strategy_id, name, description, params, source, is_active, created_by)
VALUES
    ('00000000-0000-4000-8000-000000000001', 'default', 'Conservative',
     'Low risk preset with tight stop-loss and moderate targets',
     '{"stop_loss_pct": 10, "target_pct": 15, "position_size_pct": 2, "max_positions": 3}',
     'default', 1, 'system'),
    ('00000000-0000-4000-8000-000000000002', 'default', 'Moderate',
     'Balanced risk/reward preset',
     '{"stop_loss_pct": 15, "target_pct": 25, "position_size_pct": 5, "max_positions": 5}',
     'default', 1, 'system'),
    ('00000000-0000-4000-8000-000000000003', 'default', 'Aggressive',
     'Higher risk preset with wider stops and bigger targets',
     '{"stop_loss_pct": 20, "target_pct": 40, "position_size_pct": 10, "max_positions": 8}',
     'default', 1, 'system');

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     VARCHAR(64) PRIMARY KEY,
    description TEXT        NOT NULL,
    applied_at  TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT OR IGNORE INTO schema_migrations (version, description) VALUES
    ('005', 'optimization engine: optimization_runs + optimization_results'),
    ('006', 'optimization engine: parameter_presets + optimization_audit'),
    ('007', 'optimization engine: secondary + partial indexes'),
    ('009', 'optimization engine: default Conservative/Moderate/Aggressive presets');

COMMIT;
