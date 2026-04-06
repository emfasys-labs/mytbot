# Run test_ibkr.py with the repo virtualenv (avoids "wrong" global Python).
# Usage:
#   .\test-ibkr.ps1          # paper (default)
#   .\test-ibkr.ps1 -Live    # live
[CmdletBinding()]
param(
    [switch]$Live
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Error @"
Missing .venv. From repo root run:
  python -m venv .venv
  .\.venv\Scripts\pip install -r requirements.txt
"@
}
if ($Live) {
    if (-not $env:APP_ENV) { $env:APP_ENV = "live" }
    if (-not $env:IBKR_PORT) { $env:IBKR_PORT = "7496" }
    Write-Host "IBKR mode: LIVE (APP_ENV=$env:APP_ENV, IBKR_PORT=$env:IBKR_PORT)"
    & $py (Join-Path $PSScriptRoot "test_ibkr.py") --live @args
} else {
    if (-not $env:APP_ENV) { $env:APP_ENV = "paper" }
    if (-not $env:IBKR_PORT) { $env:IBKR_PORT = "7497" }
    Write-Host "IBKR mode: PAPER (APP_ENV=$env:APP_ENV, IBKR_PORT=$env:IBKR_PORT)"
    & $py (Join-Path $PSScriptRoot "test_ibkr.py") --paper @args
}
