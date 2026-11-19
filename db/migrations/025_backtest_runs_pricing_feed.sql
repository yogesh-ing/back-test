-- Migration 025: Options model backtest provenance (Plan V3 Phase 3)
-- backtest_runs gains pricing_feed (model_bs for quarantined model runs,
-- NULL for legacy equity runs) and model_params JSONB (feed configuration).
-- Numbering: 024 was taken by Sprint D's hand-applied SQL; alembic chains
-- 023 -> 025 to keep both numbering lines collision-free.

BEGIN;

ALTER TABLE backtest_runs ADD COLUMN IF NOT EXISTS pricing_feed VARCHAR(32);
ALTER TABLE backtest_runs ADD COLUMN IF NOT EXISTS model_params JSONB;

COMMIT;