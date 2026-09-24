-- =============================================================================
-- Parameter Optimization Engine — seed default presets
-- Migration : 009_optimization_seed_presets
-- Engine    : PostgreSQL 13+
-- Alembic   : revision 009
-- =============================================================================
--
-- Strategy-agnostic templates (strategy_id = 'default'). Fixed UUIDs make
-- the seed identical across the Alembic and hand-applied paths, and
-- ON CONFLICT keeps the file re-runnable.
-- =============================================================================

BEGIN;

INSERT INTO parameter_presets
    (preset_id, strategy_id, name, description, params, source, is_active, created_by)
VALUES
    ('00000000-0000-4000-8000-000000000001', 'default', 'Conservative',
     'Low risk preset with tight stop-loss and moderate targets',
     '{"stop_loss_pct": 10, "target_pct": 15, "position_size_pct": 2, "max_positions": 3}'::jsonb,
     'default', true, 'system'),
    ('00000000-0000-4000-8000-000000000002', 'default', 'Moderate',
     'Balanced risk/reward preset',
     '{"stop_loss_pct": 15, "target_pct": 25, "position_size_pct": 5, "max_positions": 5}'::jsonb,
     'default', true, 'system'),
    ('00000000-0000-4000-8000-000000000003', 'default', 'Aggressive',
     'Higher risk preset with wider stops and bigger targets',
     '{"stop_loss_pct": 20, "target_pct": 40, "position_size_pct": 10, "max_positions": 8}'::jsonb,
     'default', true, 'system')
ON CONFLICT (strategy_id, name) DO NOTHING;

INSERT INTO schema_migrations (version, description)
VALUES ('009', 'optimization engine: default Conservative/Moderate/Aggressive presets')
ON CONFLICT (version) DO NOTHING;

COMMIT;

-- =============================================================================
-- END 009_optimization_seed_presets.sql
-- =============================================================================
