# Run test_ibkr.py with the repo virtualenv (avoids "wrong" global Python).
# Usage: .\test-ibkr.ps1
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
& $py (Join-Path $PSScriptRoot "test_ibkr.py") @args
