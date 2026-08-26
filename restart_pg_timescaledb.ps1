# Run in Administrator PowerShell
Write-Host "Restarting PostgreSQL..." -ForegroundColor Cyan
Restart-Service postgresql-x64-18 -Force
Start-Sleep -Seconds 3

Write-Host "Verifying TimescaleDB loaded..." -ForegroundColor Cyan
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -h localhost -d forward_test -c "SELECT default_version, installed_version FROM pg_available_extensions WHERE name = 'timescaledb';"

Write-Host "Creating extension..." -ForegroundColor Cyan
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -h localhost -d forward_test -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"

Write-Host "Version check..." -ForegroundColor Green
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -h localhost -d forward_test -c "SELECT * FROM timescaledb_version;"

Write-Host "Done!" -ForegroundColor Green
