-- =============================================================================
-- Forward Testing Simulator — Market Holidays Schema & 2026 Seed (SQLite)
-- Migration : 017_market_holidays
-- Engine    : SQLite 3.35+
-- =============================================================================

CREATE TABLE IF NOT EXISTS market_holidays (
    holiday_date        DATE PRIMARY KEY,
    segment             TEXT NOT NULL DEFAULT 'equity',
    description         TEXT NOT NULL,
    is_trading_holiday  BOOLEAN NOT NULL DEFAULT 1,
    source              TEXT NOT NULL DEFAULT 'nse',
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_market_holidays_date ON market_holidays (holiday_date);

INSERT OR IGNORE INTO market_holidays (holiday_date, segment, description, is_trading_holiday, source)
VALUES
    ('2026-01-15', 'equity', 'Municipal Corporation Election - Maharashtra', 1, 'nse'),
    ('2026-01-26', 'equity', 'Republic Day', 1, 'nse'),
    ('2026-03-03', 'equity', 'Holi', 1, 'nse'),
    ('2026-03-26', 'equity', 'Shri Ram Navami', 1, 'nse'),
    ('2026-03-31', 'equity', 'Shri Mahavir Jayanti', 1, 'nse'),
    ('2026-04-03', 'equity', 'Good Friday', 1, 'nse'),
    ('2026-04-14', 'equity', 'Dr. Baba Saheb Ambedkar Jayanti', 1, 'nse'),
    ('2026-05-01', 'equity', 'Maharashtra Day', 1, 'nse'),
    ('2026-05-28', 'equity', 'Bakri Id', 1, 'nse'),
    ('2026-06-26', 'equity', 'Muharram', 1, 'nse'),
    ('2026-09-14', 'equity', 'Ganesh Chaturthi', 1, 'nse'),
    ('2026-10-02', 'equity', 'Mahatma Gandhi Jayanti', 1, 'nse'),
    ('2026-10-20', 'equity', 'Dussehra', 1, 'nse'),
    ('2026-11-08', 'equity', 'Diwali Laxmi Pujan', 1, 'nse'),
    ('2026-11-24', 'equity', 'Prakash Gurpurb Sri Guru Nanak Dev', 1, 'nse'),
    ('2026-12-25', 'equity', 'Christmas', 1, 'nse');

INSERT OR IGNORE INTO schema_migrations (version, description)
VALUES ('017', 'market_holidays: calendar schema and 2026 NSE holiday seed');
