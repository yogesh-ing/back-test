@echo off
setlocal

cd /d "%~dp0"
set "PYTHONPATH=%CD%\src"

if exist ".venv\Scripts\python.exe" (
    set "PYTHON_EXE=.venv\Scripts\python.exe"
) else (
    set "PYTHON_EXE=python"
)

echo Starting Back-Test web server at http://127.0.0.1:5000 ...
"%PYTHON_EXE%" -m backtest.web.app --host 0.0.0.0 --port 5000 --source synthetic

if errorlevel 1 (
    echo.
    echo Server exited with an error.
    pause
)

endlocal
