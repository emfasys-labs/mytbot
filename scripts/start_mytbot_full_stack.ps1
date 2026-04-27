param(
    [switch]$InstallUiDeps,
    [string]$UiUrl = "http://127.0.0.1:8000/",
    [int]$BrowserDelaySec = 5,
    [switch]$RunViteDev
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

Start-Process powershell -ArgumentList @(
    "-NoExit",
    "-ExecutionPolicy", "Bypass",
    "-Command", $serverCommand
) -WindowStyle Normal

if ($RunViteDev) {
    $uiCommand = "Set-Location -LiteralPath '$uiRoot'; npm run dev"
    Start-Process powershell -ArgumentList @(
        "-NoExit",
        "-ExecutionPolicy", "Bypass",
        "-Command", $uiCommand
    ) -WindowStyle Normal
}

Write-Host "Opening UI in the default browser after ${BrowserDelaySec}s: $UiUrl"
Start-Sleep -Seconds $BrowserDelaySec
Start-Process $UiUrl

if ($RunViteDev) {
    Write-Host "mytbot server, Vite dev, and browser URL ready."
}
else {
    Write-Host "mytbot server started. UI is served by run.py — run 'cd ui; npm run build' if dist is stale."
}
