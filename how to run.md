# How to Run the Application

## Prerequisites

- Python 3.10+
- Project dependencies installed
- A fresh checkout needs two files the repo expects but does not ship:
  - `pyproject.toml` — pins `[tool.pytest.ini_options] pythonpath = ["src"]`
    (tests run without a manual PYTHONPATH) plus the core dependencies.
  - `.env` — copy from `.env.example`. The app auto-loads it
    (`src/backtest/__init__.py` → `load_dotenv`); real-data runs need
    `FORWARD_TEST_DB_URL` pointing at PostgreSQL
    (`postgresql+psycopg2://...`), not a YAML file — `config/*.yaml` only
    carries engine/reporting/alert settings.

## 1) Open a terminal in the project root

```powershell
cd C:\learning\back-test
```

## 2) Install dependencies

```powershell
pip install -r requirements.txt
```

## 3) Start the web app

This repo's actual Flask entry point is `backtest.web.app`:

```powershell
$env:PYTHONPATH = "C:\learning\back-test\src"
python -m backtest.web.app --host 0.0.0.0 --port 5000
```

Alternative if you want the project helper scripts:

```powershell
.\run.bat                # kills whatever holds :5000, then starts fresh (real sources)
.\start_dashboard.bat    # same, but forces --source synthetic
```

## 4) Open the UI

Once the server is running, open any of these in a browser:

- http://127.0.0.1:5000
- http://localhost:5000

Common pages:

- http://127.0.0.1:5000/  (home)
- http://127.0.0.1:5000/portfolio
- http://127.0.0.1:5000/forward

## 5) Health check

```powershell
Invoke-WebRequest -Uri http://127.0.0.1:5000/health -UseBasicParsing
```

Expected response:

```json
{"source":"synthetic","status":"ok"}
```

## Production (Linux server)

```bash
gunicorn -c gunicorn.conf.py backtest.web.wsgi:app
```

- One worker, N threads — trading state lives inside the process (runners,
  broker session, alert broker); see `gunicorn.conf.py` before changing.
- Overrides: `GUNICORN_BIND` (default `127.0.0.1:5000`), `GUNICORN_THREADS` (8),
  `GUNICORN_TIMEOUT` (600 s), `BACKTEST_SOURCE`.
- The app has no login of its own: keep it bound to localhost or put an
  authenticated reverse proxy in front before exposing it.

## Notes

- The app binds to `0.0.0.0`, which is intended for preview access.
- The broker (mStock/Dhan) session is **per-process**: after every restart,
  log in again from the UI (broker popup) before live feeds and runners
  resume. `run.bat` prints the same reminder at boot.
- Market data is served from the PostgreSQL cache (`FORWARD_TEST_DB_URL`),
  filled via the Data tab; nothing is read from YAML at runtime.
- If port 5000 is already in use, run a different port:

```powershell
python -m backtest.web.app --host 0.0.0.0 --port 5001
```
