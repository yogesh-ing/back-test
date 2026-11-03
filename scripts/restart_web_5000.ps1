# Restart the Trading Bot web server on port 5000 (127.0.0.1 only)
# Kills ALL listeners on 5000 first — stale servers cause stale templates.
$conns = Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue
$pids = $conns | Select-Object -ExpandProperty OwningProcess -Unique
foreach ($p in $pids) {
    Write-Host "Stopping PID $p"
    Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 2
# B8: a redirected stream is block-buffered, so a server that is not launched
# unbuffered can serve all day while its own log has not moved (30-Oct froze at
# 08:15:38 and the EOD gate could not grep the session-clock sweep line).
# ``-u`` plus the dated pair of files below is the operator-side half;
# ``backtest.web.app.main()`` now sets line buffering itself for every path.
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$root = Split-Path -Parent $PSScriptRoot
Start-Process -WindowStyle Hidden -FilePath '.venv\Scripts\python.exe' `
    -ArgumentList '-u','-m','backtest.web.app','--host','127.0.0.1','--port','5000','--source','synthetic' `
    -WorkingDirectory 'src' `
    -RedirectStandardOutput (Join-Path $root "server_${stamp}.log") `
    -RedirectStandardError  (Join-Path $root "server_${stamp}.err.log")
Start-Sleep -Seconds 6
Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique |
    ForEach-Object { Write-Host "Listening: PID $_" }
