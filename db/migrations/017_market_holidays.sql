-- =============================================================================
-- Forward Testing Simulator — Market Holidays Schema & 2026 Seed
-- Migration : 017_market_holidays
-- Engine    : PostgreSQL 13+
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS market_holidays (
    holiday_date        DATE PRIMARY KEY,
    segment             TEXT NOT NULL DEFAULT 'equity',
    description         TEXT NOT NULL,
    is_trading_holiday  BOOLEAN NOT NULL DEFAULT TRUE,
    source              TEXT NOT NULL DEFAULT 'nse',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_market_holidays_year ON market_holidays (EXTRACT(year FROM holiday_date));

-- Seed 2026 NSE equity trading holidays
INSERT INTO market_holidays (holiday_date, segment, description, is_trading_holiday, source)
VALUES
    ('2026-01-15', 'equity', 'Municipal Corporation Election - Maharashtra', TRUE, 'nse'),
    ('2026-01-26', 'equity', 'Republic Day', TRUE, 'nse'),
    ('2026-03-03', 'equity', 'Holi', TRUE, 'nse'),
    ('2026-03-26', 'equity', 'Shri Ram Navami', TRUE, 'nse'),
    ('2026-03-31', 'equity', 'Shri Mahavir Jayanti', TRUE, 'nse'),
    ('2026-04-03', 'equity', 'Good Friday', TRUE, 'nse'),
    ('2026-04-14', 'equity', 'Dr. Baba Saheb Ambedkar Jayanti', TRUE, 'nse'),
    ('2026-05-01', 'equity', 'Maharashtra Day', TRUE, 'nse'),
    ('2026-05-28', 'equity', 'Bakri Id', TRUE, 'nse'),
    ('2026-06-26', 'equity', 'Muharram', TRUE, 'nse'),
    ('2026-09-14', 'equity', 'Ganesh Chaturthi', TRUE, 'nse'),
    ('2026-10-02', 'equity', 'Mahatma Gandhi Jayanti', TRUE, 'nse'),
    ('2026-10-20', 'equity', 'Dussehra', TRUE, 'nse'),
    ('2026-11-08', 'equity', 'Diwali Laxmi Pujan', TRUE, 'nse'),
    ('2026-11-24', 'equity', 'Prakash Gurpurb Sri Guru Nanak Dev', TRUE, 'nse'),
    ('2026-12-25', 'equity', 'Christmas', TRUE, 'nse')
ON CONFLICT (holiday_date) DO NOTHING;

-- Record migration
INSERT INTO schema_migrations (version, description)
VALUES ('017', 'market_holidays: calendar schema and 2026 NSE holiday seed')
ON CONFLICT (version) DO NOTHING;

COMMIT;
