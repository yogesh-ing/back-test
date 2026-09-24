# How to Run the Application

## Prerequisites

- Python 3.10+
- Project dependencies installed

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

Alternative if you want the project helper script:

```powershell
.\start_dashboard.bat
```

## 4) Open the UI

Once the server is running, open any of these in a browser:

- http://127.0.0.1:5000
- http://localhost:5000

Common pages:

- http://127.0.0.1:5000/  (home)
- http://127.0.0.1:5000/dashboard
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

## Notes

- The app binds to `0.0.0.0`, which is intended for preview access.
- If port 5000 is already in use, run a different port:

```powershell
python -m backtest.web.app --host 0.0.0.0 --port 5001
```
