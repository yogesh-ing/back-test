@echo off
setlocal

rem ============================================================
rem  run.bat - restart the Back-Test app
rem  1) kills any process already listening on :5000 (the stale
rem     server that otherwise shadows a restart)
rem  2) starts the web app fresh from this checkout
rem  Source is NOT pinned: config/data_sources.yaml + .env choose
rem  (backtest/compare = DB, forward/portfolio = mStock).
rem ============================================================

cd /d "%~dp0"
set "PYTHONPATH=%CD%\src"

if exist ".venv\Scripts\python.exe" (
    set "PYTHON_EXE=.venv\Scripts\python.exe"
) else (
    set "PYTHON_EXE=python"
)

echo [run.bat] checking for an existing server on port 5000...
set "KILLED="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":5000 .*LISTENING"') do (
    echo [run.bat] killing PID %%P (was holding :5000)
    taskkill /F /PID %%P >nul 2>&1
    set "KILLED=1"
)
if defined KILLED (
    rem give the OS a beat to release the port
    timeout /t 2 /nobreak >nul
) else (
    echo [run.bat] nothing listening on :5000 - clean start
)

echo [run.bat] starting Back-Test web server at http://127.0.0.1:5000 ...
echo [run.bat] broker login is per-process: after a restart the runners pause
echo [run.bat] until you log in again from the UI.
"%PYTHON_EXE%" -m backtest.web.app --host 0.0.0.0 --port 5000

if errorlevel 1 (
    echo.
    echo [run.bat] server exited with an error - check the output above.
    pause
)

endlocal
