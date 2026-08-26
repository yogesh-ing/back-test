# ============================================================
#  Fetch 1min data for all NIFTY 200 instruments
#  Usage: Right-click -> Run with PowerShell
# ============================================================

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot\..

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " Fetching 1min data for all NIFTY 200 instruments" -ForegroundColor Cyan
Write-Host " This will take 4-6 hours. Do NOT close this window." -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# Load .env
$envFile = Get-Content ".env" | Where-Object { $_ -notmatch '^\s*#' -and $_ -match '=' }
foreach ($line in $envFile) {
    $parts = $line -split '=', 2
    $key = $parts[0].Trim()
    $val = $parts[1].Trim()
    [Environment]::SetEnvironmentVariable($key, $val, "Process")
}

# Step 1: Login
Write-Host "[1/3] Logging in..." -ForegroundColor Yellow
$loginBody = @{
    Username = $env:MSTOCK_USERNAME
    Password = $env:MSTOCK_PASSWORD
}
$loginHeaders = @{
    "X-Mirae-Version"   = "1"
    "Content-Type"      = "application/x-www-form-urlencoded"
}
$loginResp = Invoke-RestMethod -Uri "$($env:MSTOCK_BASE_URL)/openapi/typea/connect/login" -Method Post -Body $loginBody -Headers $loginHeaders
if ($loginResp.status -ne "success") {
    Write-Host "Login failed: $($loginResp | ConvertTo-Json)" -ForegroundColor Red
    exit 1
}
Write-Host "  Login OK" -ForegroundColor Green

# Step 2: Get TOTP from user
Write-Host ""
Write-Host "[2/3] Enter your TOTP code from authenticator app:" -ForegroundColor Yellow
$totp = Read-Host "TOTP"

# Step 3: Verify TOTP
Write-Host "  Verifying TOTP..." -ForegroundColor Yellow
$verifyBody = @{
    api_key = $env:MSTOCK_API_KEY
    totp    = $totp
}
$verifyResp = Invoke-RestMethod -Uri "$($env:MSTOCK_BASE_URL)/openapi/typea/session/verifytotp" -Method Post -Body $verifyBody -Headers $loginHeaders

# Extract token
if ($verifyResp.access_token) {
    $token = $verifyResp.access_token
} elseif ($verifyResp.data.access_token) {
    $token = $verifyResp.data.access_token
} else {
    Write-Host "No token in response: $($verifyResp | ConvertTo-Json)" -ForegroundColor Red
    exit 1
}
$token | Out-File -FilePath ".mstock_session_token" -Encoding utf8 -NoNewline
Write-Host "  Token saved" -ForegroundColor Green

# Step 4: Fetch 1min data
Write-Host ""
Write-Host "[3/3] Starting fetch (this will take a while)..." -ForegroundColor Yellow
Write-Host ""

$env:PYTHONPATH = "src"
$env:MSTOCK_ACCESS_TOKEN = $token
$env:MSTOCK_AUTH_MODE = "totp"

& ".venv\Scripts\python.exe" scripts\fetch_nifty500_historical.py `
    --csv "stock-list\nse_ind_nifty200list.csv" `
    --timeframe 1min `
    --from "2024-01-01" `
    --skip-existing

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host " DONE!" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "Check DB:" -ForegroundColor Cyan
Write-Host '  psql -U postgres -d forward_test -c "SELECT timeframe, count(*) FROM market_data_cache GROUP BY timeframe;"'
Write-Host ""
