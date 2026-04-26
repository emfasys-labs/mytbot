param(
    [switch]$InstallUiDeps
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$uiRoot = Join-Path $repoRoot "ui"
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"

if (Test-Path -LiteralPath $venvPython) {
    $pythonExe = $venvPython
}
else {
    $pythonExe = "python"
}

if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    throw "npm is not available in PATH. Install Node.js or open from a shell where npm works."
}

if (-not (Test-Path -LiteralPath $uiRoot)) {
    throw "UI folder not found at $uiRoot"
}

$nodeModulesPath = Join-Path $uiRoot "node_modules"
if ($InstallUiDeps -or -not (Test-Path -LiteralPath $nodeModulesPath)) {
    Write-Host "Installing UI dependencies..."
    & npm --prefix $uiRoot install
    if ($LASTEXITCODE -ne 0) {
        throw "npm install failed in $uiRoot"
    }
}

$serverCommand = "Set-Location -LiteralPath '$repoRoot'; & '$pythonExe' 'run.py'"
$uiCommand = "Set-Location -LiteralPath '$uiRoot'; npm run dev"

Start-Process powershell -ArgumentList @(
    "-NoExit",
    "-ExecutionPolicy", "Bypass",
    "-Command", $serverCommand
) -WindowStyle Normal

Start-Process powershell -ArgumentList @(
    "-NoExit",
    "-ExecutionPolicy", "Bypass",
    "-Command", $uiCommand
) -WindowStyle Normal

Write-Host "mytbot server and UI launched in separate windows."
