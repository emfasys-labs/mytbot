# Set the Postgres role password inside the running Docker `db` container to match
# your chosen password (no volume wipe). Use when .env was updated but the DB
# still has an older password.
#
# Usage (from repo root):
#   1. Put your target password in .env as POSTGRES_PASSWORD=...
#   2. .\scripts\apply_postgres_password.ps1
#
# Or:  .\scripts\apply_postgres_password.ps1 -Password 'your-secret'
#
param(
    [string]$Password = "",
    [string]$EnvFile = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not $EnvFile) {
    $EnvFile = Join-Path $Root ".env"
}
if (-not (Test-Path $EnvFile)) {
    Write-Error ('Missing {0} - create it from .env.example' -f $EnvFile)
    exit 1
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
            return $val
        }
    }
    return $null
}

$user = Get-DotEnvValue -Path $EnvFile -Key "POSTGRES_USER"
if (-not $user) { $user = "mytbot" }
$db = Get-DotEnvValue -Path $EnvFile -Key "POSTGRES_DB"
if (-not $db) { $db = "mytbot" }

if (-not $Password) {
    $Password = Get-DotEnvValue -Path $EnvFile -Key "POSTGRES_PASSWORD"
}
if (-not $Password) {
    Write-Error "Set POSTGRES_PASSWORD in .env or pass -Password '...'"
    exit 1
}

if ($user -notmatch '^[a-zA-Z_][a-zA-Z0-9_]*$') {
    Write-Error 'POSTGRES_USER must be a simple identifier.'
    exit 1
}

# SQL string literal escaping for PostgreSQL
$sqlPwd = $Password.Replace("'", "''")
$sql = "ALTER USER $user WITH PASSWORD '$sqlPwd';"

Write-Host ('Applying password for role {0} on database {1}...' -f $user, $db)
docker compose exec -T db psql -v ON_ERROR_STOP=1 -U $user -d $db -c $sql
if ($LASTEXITCODE -ne 0) {
    Write-Error 'psql failed. Is the stack up? Run: docker compose up -d db'
    exit $LASTEXITCODE
}

Write-Host 'Done. Restart anything that connects to Postgres (e.g. uvicorn).'
