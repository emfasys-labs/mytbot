# Recreate local Docker Postgres (and Redis) from your current .env.
# Prefer .\apply_postgres_password.ps1 first if you only need to change the role password (no data loss).
# Use this when Python says "password authentication failed" but you know POSTGRES_PASSWORD
# in .env is correct — the DB volume was almost certainly created with an older password.
#
# Usage (from repo root):
#   .\scripts\reset_postgres_volume.ps1 -Force
#
param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not $Force) {
    Write-Host @"

This DELETES the Docker volumes for this project (local Postgres + Redis data)
and starts fresh db/redis using POSTGRES_* from .env in:

  $Root\.env

Then runs: alembic upgrade head

To proceed, run:

  .\scripts\reset_postgres_volume.ps1 -Force

"@
    exit 1
}

Write-Host "Stopping stack and removing volumes..."
docker compose down -v
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Starting db + redis..."
docker compose up -d db redis
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Waiting for Postgres to become healthy..."
$deadline = (Get-Date).AddSeconds(90)
$ok = $false
while ((Get-Date) -lt $deadline) {
    $status = docker inspect -f "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}" mytbot_db 2>$null
    if ($status -eq "healthy") {
        $ok = $true
        break
    }
    Start-Sleep -Seconds 2
}
if (-not $ok) {
    Write-Error "Postgres did not become healthy in time. Check: docker compose logs db"
    exit 1
}

$alembic = Join-Path $Root ".venv\Scripts\alembic.exe"
if (Test-Path $alembic) {
    Write-Host "Running alembic upgrade head (venv)..."
    & $alembic upgrade head
} else {
    Write-Host "Running alembic upgrade head..."
    alembic upgrade head
}
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host @"

Done. Postgres user/password now match POSTGRES_* in .env.

Restart your API (uvicorn) so it reconnects.

"@
