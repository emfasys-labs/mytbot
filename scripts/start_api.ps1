# One-command local API startup with password sync.
# - Ensures Docker Postgres is up
# - Applies POSTGRES_PASSWORD from .env to DB role (idempotent)
# - Starts uvicorn API
#
# Usage:
#   .\scripts\start_api.ps1
# Optional:
#   .\scripts\start_api.ps1 -BindHost 0.0.0.0 -Port 8000 -Reload

param(
    [string]$BindHost = "0.0.0.0",
    [int]$Port = 8000,
    [switch]$Reload = $true,
    [switch]$SkipVerify = $false
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Step([string]$msg) {
    Write-Host ("==> {0}" -f $msg)
}

function Warn([string]$msg) {
    Write-Warning $msg
}

if (-not (Test-Path ".env")) {
    throw "Missing .env in repo root. Create it from .env.example first."
}

function Get-DotEnvValue {
    param([string]$Path, [string]$Key)
    $pattern = "^\s*$([regex]::Escape($Key))\s*=\s*(.*)\s*$"
    foreach ($raw in Get-Content -LiteralPath $Path -ErrorAction Stop) {
        $line = $raw.TrimEnd()
        if ($line -match "^\s*#" -or $line -eq "") { continue }
        if ($line -match $pattern) {
            $val = $Matches[1].Trim()
            if ($val.Length -ge 2 -and $val.StartsWith('"') -and $val.EndsWith('"')) {
                $val = $val.Substring(1, $val.Length - 2).Replace('""', '"')
            }
            elseif ($val.Length -ge 2 -and $val.StartsWith("'") -and $val.EndsWith("'")) {
                $val = $val.Substring(1, $val.Length - 2).Replace("''", "'")
            }
            else {
                $val = ($val -replace '\s+#.*$', '').Trim()
            }
            return $val
        }
    }
    return $null
}

Step "Loading POSTGRES_* from .env into process environment"
$envFile = Join-Path $Root ".env"
$keys = @("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD")
foreach ($k in $keys) {
    $v = Get-DotEnvValue -Path $envFile -Key $k
    if ($null -ne $v -and $v -ne "") {
        Set-Item -Path ("Env:\" + $k) -Value $v
    }
}

Step "Checking Docker availability"
$dockerOk = $true
try {
    docker info *> $null
}
catch {
    $dockerOk = $false
}

if ($dockerOk) {
    Step "Starting postgres container (if needed)"
    docker compose up -d db
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to start Docker db container."
    }

    Step "Syncing DB role password from .env"
    & ".\scripts\apply_postgres_password.ps1"
    if ($LASTEXITCODE -ne 0) {
        throw "Password sync failed. Resolve DB auth issue and retry."
    }
}
else {
    Warn "Docker is not available/running. Skipping container start and password sync."
    Warn "If you use local Postgres service, ensure .env POSTGRES_* credentials match your DB."
}

if (-not $SkipVerify) {
    Step "Verifying host can reach Postgres (same creds as API)"
    python ".\scripts\verify_db_connection.py"
    if ($LASTEXITCODE -ne 0) {
        throw @"
Postgres connection from this machine failed.

Most common on Windows: native PostgreSQL already listens on port 5432, so API hits the WRONG server.

Fix:
  1) In .env set POSTGRES_PORT=5433 (or any free port)
  2) docker compose up -d db
  3) .\scripts\apply_postgres_password.ps1
  4) .\scripts\start_api.ps1

Or stop the Windows PostgreSQL service so 5432 is free for Docker.

To skip this check: .\scripts\start_api.ps1 -SkipVerify
"@
    }
}

Step "Starting API server"
$reloadArg = if ($Reload) { "--reload" } else { "" }
$cmd = "uvicorn api.server:app --host $BindHost --port $Port $reloadArg".Trim()
Invoke-Expression $cmd
