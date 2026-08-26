# Windows Postgres Quick-Start Guide — Direct Installation (No Docker)

> **Roles:** Windows DevOps Specialist + PostgreSQL DBA
> **OS:** Windows 10/11, PostgreSQL installed directly (e.g. from https://www.postgresql.org/download/windows/)
> **Tools:** `psql`, `createdb`, `pg_dump`, PowerShell / cmd
> **App:** Python Backend, `PYTHONPATH=src`

---

## 1. Verify PostgreSQL Installation on Windows

### Check psql is in PATH

**PowerShell:**
```powershell
psql --version
# Expected: psql (PostgreSQL) 15.x or 16.x

# If not found, add to PATH (adjust version):
$env:Path += ";C:\Program Files\PostgreSQL\15\bin"
# Permanent:
[Environment]::SetEnvironmentVariable("Path", $env:Path + ";C:\Program Files\PostgreSQL\15\bin", "User")
```

**cmd:**
```cmd
psql --version
:: If not found:
set PATH=%PATH%;C:\Program Files\PostgreSQL\15\bin
```

### Check service is running

**PowerShell:**
```powershell
Get-Service -Name postgresql*
# Should be Running

# If not:
net start postgresql-x64-15
```

---

## 2. Log Into Local Postgres Server

**Default superuser is `postgres` with password you set during installation.**

**PowerShell / cmd:**
```powershell
# Connect to default 'postgres' database
psql -U postgres -h localhost -p 5432 -d postgres

# You'll be prompted for password
# If you want to avoid prompt, set PGPASSWORD env (PowerShell):
$env:PGPASSWORD="your_postgres_password"
psql -U postgres -h localhost -d postgres

# Inside psql, you should see:
# postgres=#
```

**If peer auth fails:**
```powershell
# Try with -W to force password prompt
psql -U postgres -h 127.0.0.1 -W -d postgres
```

---

## 3. Create New Database `forward_test`

**Inside psql (`postgres=#`):**
```sql
-- Create database
CREATE DATABASE forward_test OWNER postgres;

-- Optional: create app user (recommended, not using superuser for app)
CREATE USER ft_app WITH PASSWORD 'ChangeMe123!';
GRANT ALL PRIVILEGES ON DATABASE forward_test TO ft_app;

-- Exit psql
\q
```

**Or via cmd (one-liner):**
```cmd
:: Create DB via createdb tool
createdb -U postgres -h localhost -O postgres forward_test

:: Or with custom user
psql -U postgres -h localhost -d postgres -c "CREATE USER ft_app WITH PASSWORD 'ChangeMe123!';"
psql -U postgres -h localhost -d postgres -c "CREATE DATABASE forward_test OWNER ft_app;"
psql -U postgres -h localhost -d postgres -c "GRANT ALL PRIVILEGES ON DATABASE forward_test TO ft_app;"
```

**Verify DB exists:**
```powershell
psql -U postgres -h localhost -d postgres -c "\l" | findstr forward_test
```

---

## 4. Execute SQL Scripts in Correct Order

**You are in `C:\Users\YourName\back-test` or wherever you cloned.**

**PowerShell (recommended):**
```powershell
cd C:\Users\YourName\back-test

# Set password for psql to avoid prompts
$env:PGPASSWORD="your_postgres_password"  # or ft_app password if you created ft_app user

# Step 1: Run main migration (one transaction, correct FK order internally)
psql -U postgres -h localhost -d forward_test -f db/migrations/001_initial_schema.sql

# Expected: BEGIN, CREATE TABLE, CREATE INDEX, INSERT, COMMIT – no errors
# If you created ft_app user, use:
psql -U ft_app -h localhost -d forward_test -f db/migrations/001_initial_schema.sql

# Step 2: Verify schema – expect all PASS
psql -U postgres -h localhost -d forward_test -f db/verify_schema.sql

# Output should show:
# table_count 11 | PASS
# view_count 2 | PASS
# fk_count 14 | PASS
# index_count >=46 | PASS
# etc.
```

**cmd:**
```cmd
cd /d C:\Users\YourName\back-test
set PGPASSWORD=your_postgres_password
psql -U postgres -h localhost -d forward_test -f db\migrations\001_initial_schema.sql
psql -U postgres -h localhost -d forward_test -f db\verify_schema.sql
```

**If you use Alembic instead of manual SQL:**
```powershell
# Alembic uses same ORM models – should produce no diff if SQL file already applied
$env:FORWARD_TEST_DB_URL="postgresql+psycopg2://ft_app:ChangeMe123!@localhost:5432/forward_test"
alembic -c alembic.ini upgrade head
# Should say "Running upgrade 001 -> ..."
```

---

## 5. Configure Python `.env` File

**Location:** Repo root, same folder as `.env.example` – **gitignored**, never pushed.

**PowerShell:**
```powershell
Copy-Item .env.example .env
notepad .env
```

**Exact syntax for `.env` (Windows):**

```ini
# =============================================================================
# mStock TypeA API – Real Broker Credentials (from https://api.mstock.trade)
# =============================================================================
MSTOCK_API_KEY=your_api_key_from_mstock_dashboard
MSTOCK_USERNAME=your_client_code_like_AB1234
MSTOCK_PASSWORD=your_mstock_password
MSTOCK_CHECKSUM=W
MSTOCK_AUTH_MODE=otp
# otp = SMS OTP (set MSTOCK_OTP env when prompted) or totp = authenticator app
MSTOCK_BASE_URL=https://api.mstock.trade

# Optional: if you already have valid token, cache it to skip OTP
# The auth module caches token in .mstock_session_token file (also gitignored)
# You can manually create .mstock_session_token with token string

# =============================================================================
# Telegram Alerts – Preferred (fast, free) – Step 21
# =============================================================================
# 1. Chat @BotFather on Telegram → /newbot → get token like 123456:ABC-DEF...
# 2. Send message to your bot, then visit https://api.telegram.org/bot<token>/getUpdates to get chat_id
TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
TELEGRAM_CHAT_ID=123456789

# Optional: Slack / Discord webhooks
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/1234567890/ABC-DEF...

# Optional: Email alerts (may be delayed)
ALERT_EMAIL_SMTP_HOST=smtp.gmail.com
ALERT_EMAIL_SMTP_PORT=587
ALERT_EMAIL_USER=your_email@gmail.com
ALERT_EMAIL_PASSWORD=your_app_password_from_env
ALERT_EMAIL_FROM=your_email@gmail.com
ALERT_EMAIL_TO=your_email@gmail.com,other@example.com

# =============================================================================
# PostgreSQL – Windows Direct Installation
# =============================================================================
# Format: postgresql+psycopg2://user:password@host:port/dbname
# If password has special chars like @ or :, URL-encode them: @ -> %40, : -> %3A

# Option A: Using superuser postgres (simplest for local dev)
FORWARD_TEST_DB_URL=postgresql+psycopg2://postgres:your_postgres_password@localhost:5432/forward_test

# Option B: Using app user ft_app (recommended)
# FORWARD_TEST_DB_URL=postgresql+psycopg2://ft_app:ChangeMe123!@localhost:5432/forward_test

# Profile: development (SQLite fallback), testing (in-memory), production (requires real URL)
FORWARD_TEST_DB_PROFILE=development

# Optional: log every SQL query (DEBUG level) – useful for Manual Testing Checklist
FORWARD_TEST_DB_LOG_QUERIES=true
FORWARD_TEST_DB_SLOW_QUERY_MS=200
```

**Critical Windows notes:**
- Use `postgresql+psycopg2://` prefix (SQLAlchemy + psycopg2 driver)
- No spaces around `=`
- If path has spaces, wrap entire file path in quotes when using in PowerShell, but `.env` itself should NOT have quotes around values
- Save as UTF-8, not UTF-16 (Notepad default is okay, but ensure no BOM)

**Verify .env is ignored:**
```powershell
git status
# Should NOT show .env as to-be-committed – if it does, check .gitignore has .env
git check-ignore -v .env
# Expected: .gitignore:.env
```

---

## 6. Spin Up Python Virtual Environment on Windows

**PowerShell:**
```powershell
# In repo root
python --version
# Should be 3.9+ (3.11 recommended)

# Create venv
python -m venv .venv

# Activate
.\.venv\Scripts\Activate.ps1
# If execution policy blocks:
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser

# Upgrade pip
python -m pip install --upgrade pip

# Install deps
pip install -r requirements.txt

# Verify
pip list | findstr -i "pandas sqlalchemy psycopg2"
```

**cmd:**
```cmd
python -m venv .venv
.\.venv\Scripts\activate.bat
pip install -r requirements.txt
```

**Expected packages:** `pandas`, `numpy`, `requests`, `python-dotenv`, `SQLAlchemy`, `alembic`, `psycopg2-binary`, `PyYAML`, `Flask`, `matplotlib`, `pyarrow`, `pytest`

---

## 7. Start the App

### Option A – Run Engine (Main Forward Testing Loop – Step 20)

**PowerShell:**
```powershell
$env:PYTHONPATH="src"
$env:FORWARD_TEST_DB_URL="postgresql+psycopg2://postgres:your_password@localhost:5432/forward_test"

# Dry-run (signals but no trades) – safest first run
python -m backtest.forward.engine --config config/forward_testing.yaml --dry-run --symbols INFY

# Backtest replay mode with mock data (no broker needed)
python -m backtest.forward.engine --backtest --symbols INFY --dry-run

# Live papertrade with real mStock (requires .env with MSTOCK_* creds)
python -m backtest.forward.engine --config config/forward_testing.yaml --symbols INFY TCS

# Or via Python script:
python - << 'PY'
from backtest.forward.engine import ForwardTestingEngine
engine = ForwardTestingEngine(config_file="config/forward_testing.yaml")
engine.initialize_system()
print(engine.get_status())
# engine.start()  # blocks – uncomment when ready
PY
```

### Option B – Run Dashboard (Step 19)

**PowerShell:**
```powershell
$env:PYTHONPATH="src"

# Dashboard with mock portfolio demo (no DB needed)
python -m backtest.dashboard.app --host 0.0.0.0 --port 5000

# Or with real engine data
python - << 'PY'
from backtest.forward.engine import ForwardTestingEngine
from backtest.dashboard.app import run_dashboard

engine = ForwardTestingEngine(config_file="config/forward_testing.yaml")
engine.initialize_system()
run_dashboard(host="0.0.0.0", port=5000, portfolio=engine.portfolio, engine=engine, data_handler=engine.data_handler, performance=engine.performance)
PY
```

Open browser: `http://localhost:5000` – you should see Portfolio Overview, Open Positions, Equity Curve, etc., auto-refresh 5s.

### Option C – Run Tests (Verify Everything Works)

```powershell
$env:PYTHONPATH="src"
pytest tests/ -q -k "not live"
# Expected: 1175 passed, 4 skipped

# With live mStock (requires .env):
pytest tests/test_mstock_live_integration.py -s
```

---

## 8. Quick Reference – All Windows Commands in One Block

**Copy-paste for PowerShell (adjust passwords):**

```powershell
cd C:\Users\YourName\back-test
$env:PGPASSWORD="postgres_password"
psql -U postgres -h localhost -d postgres -c "CREATE DATABASE forward_test OWNER postgres;"
psql -U postgres -h localhost -d forward_test -f db/migrations/001_initial_schema.sql
psql -U postgres -h localhost -d forward_test -f db/verify_schema.sql

Copy-Item .env.example .env
notepad .env
# Fill FORWARD_TEST_DB_URL and MSTOCK_* and TELEGRAM_*

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:PYTHONPATH="src"
pytest tests/ -q -k "not live"
python -m backtest.forward.engine --dry-run --symbols INFY
python -m backtest.dashboard.app --port 5000
```

**You’re now ready for Manual Testing Checklist.**

