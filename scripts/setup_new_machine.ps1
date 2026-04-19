# scripts/setup_new_machine.ps1
# ===============================
# Bootstrap Python venv + pip install on a new Windows PC.
# Does NOT install Docker, Node, Ollama — see docs/NEW_MACHINE_SETUP.md
#
# Usage (PowerShell, repo root):
#   .\scripts\setup_new_machine.ps1

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

Write-Host "=== mytbot: setup_new_machine.ps1 ===" -ForegroundColor Cyan
Write-Host "Repo: $Root"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Error "Python not found. Install Python 3.12+ (64-bit) and add to PATH."
    exit 1
}

$pyExe = "python"
try {
    & $pyExe -c "import sys; assert sys.version_info[:2] >= (3, 12), 'Need Python 3.12+'"
} catch {
    Write-Warning "Recommended: Python 3.12+. Current:"
    & $pyExe --version
}

if (-not (Test-Path ".venv")) {
    Write-Host "Creating .venv ..."
    & $pyExe -m venv .venv
}

$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
$venvPip = Join-Path $Root ".venv\Scripts\pip.exe"

if (-not (Test-Path $venvPy)) {
    Write-Error "Virtualenv python missing at $venvPy"
    exit 1
}

Write-Host "Upgrading pip ..."
& $venvPy -m pip install --upgrade pip

Write-Host "Installing requirements.txt (this may take several minutes) ..."
& $venvPip install -r (Join-Path $Root "requirements.txt")

if (-not (Test-Path ".env")) {
    Copy-Item (Join-Path $Root ".env.example") (Join-Path $Root ".env")
    Write-Host ""
    Write-Host "Created .env from .env.example — edit POSTGRES_* and API keys before running." -ForegroundColor Yellow
} else {
    Write-Host ".env already exists — not overwritten."
}

Write-Host ""
Write-Host "Done (Python deps)." -ForegroundColor Green
Write-Host "Next manual steps:"
Write-Host "  1. Edit .env"
Write-Host "  2. docker compose up -d"
Write-Host "  3. .\.venv\Scripts\alembic.exe upgrade head"
Write-Host "  4. Install Ollama + ollama pull qwen2.5:7b ; ollama pull llama3.1:8b"
Write-Host "  5. cd ui; npm ci; npm run build"
Write-Host "  6. python run.py"
Write-Host ""
Write-Host "Full checklist: docs/NEW_MACHINE_SETUP.md"
