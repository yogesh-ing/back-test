@echo off
cd /d C:\learning\back-test
set PYTHONPATH=src
set MSTOCK_ACCESS_TOKEN=
for /f "delims=" %%i in (.mstock_session_token) do set MSTOCK_ACCESS_TOKEN=%%i
.venv\Scripts\python.exe scripts\fetch_nifty500_historical.py --csv stock-list\nse_ind_nifty200list.csv --from 2020-01-01 --skip-existing
pause
