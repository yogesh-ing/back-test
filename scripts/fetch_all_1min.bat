@echo off
REM ============================================================
REM  Fetch 1min data for all NIFTY 200 instruments
REM  Usage: Double-click this file, or run from terminal
REM ============================================================

REM Load environment variables from .env
for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
    set "%%a=%%b"
)

REM Check if token exists and is fresh
if exist ".mstock_session_token" (
    echo [INFO] Found cached token. Attempting to use it...
    set /p TOKEN=<.mstock_session_token
    echo [INFO] Token length: !TOKEN!
)

REM Run the fetch
echo.
echo ============================================================
echo  Fetching 1min data for all NIFTY 200 instruments
echo  This will take 4-6 hours. Do NOT close this window.
echo ============================================================
echo.

set PYTHONPATH=src
set MSTOCK_AUTH_MODE=totp

.venv\Scripts\python.exe scripts\fetch_nifty500_historical.py ^
    --csv stock-list\nse_ind_nifty200list.csv ^
    --timeframe 1min ^
    --from 2024-01-01 ^
    --skip-existing

echo.
echo ============================================================
echo  DONE! Check DB with:
echo    psql -U postgres -d forward_test -c "SELECT timeframe, count(*) FROM market_data_cache GROUP BY timeframe;"
echo ============================================================
pause
