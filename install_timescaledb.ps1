# Run this as Administrator in PowerShell
# Right-click PowerShell -> "Run as administrator"

$src = "C:\Users\Nirvika\Downloads\New folder\timescaledb"
$pgLib = "C:\Program Files\PostgreSQL\18\lib"
$pgExt = "C:\Program Files\PostgreSQL\18\share\extension"

Write-Host "Copying TimescaleDB DLLs to $pgLib ..." -ForegroundColor Cyan
Copy-Item "$src\timescaledb.dll" $pgLib -Force
Copy-Item "$src\timescaledb-2.29.2.dll" $pgLib -Force
Copy-Item "$src\timescaledb-tsl-2.29.2.dll" $pgLib -Force

Write-Host "Copying control file to $pgExt ..." -ForegroundColor Cyan
Copy-Item "$src\timescaledb.control" $pgExt -Force

Write-Host "Copying SQL extension files to $pgExt ..." -ForegroundColor Cyan
Copy-Item "$src\timescaledb--*.sql" $pgExt -Force

Write-Host ""
Write-Host "Verifying installation..." -ForegroundColor Green
Get-ChildItem "$pgLib\timescale*" | Format-Table Name, Length
Get-ChildItem "$pgExt\timescaledb.control" | Format-Table Name, Length
$sqlCount = (Get-ChildItem "$pgExt\timescaledb--*.sql").Count
Write-Host "SQL files copied: $sqlCount" -ForegroundColor Green
Write-Host ""
Write-Host "Done! Now run this SQL to activate the extension:" -ForegroundColor Yellow
Write-Host "  CREATE EXTENSION IF NOT EXISTS timescaledb;" -ForegroundColor White
