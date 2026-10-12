# Restart the Trading Bot web server on port 5000 (127.0.0.1 only)
# Kills ALL listeners on 5000 first — stale servers cause stale templates.
$conns = Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue
$pids = $conns | Select-Object -ExpandProperty OwningProcess -Unique
foreach ($p in $pids) {
    Write-Host "Stopping PID $p"
    Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 2
Start-Process -WindowStyle Hidden -FilePath '.venv\Scripts\python.exe' `
    -ArgumentList '-m','backtest.web.app','--host','127.0.0.1','--port','5000','--source','synthetic' `
    -WorkingDirectory 'src'
Start-Sleep -Seconds 6
Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique |
    ForEach-Object { Write-Host "Listening: PID $_" }
