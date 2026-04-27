param(
    [string]$TaskName = "mytbot-auto-training",
    [string]$StartTime = "03:20"
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    $Python = "python"
}

$Action = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument "scripts\auto_train_models.py" `
    -WorkingDirectory $RepoRoot

$Trigger = New-ScheduledTaskTrigger -Daily -At $StartTime
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "mytbot research/paper-only automatic model training" `
    -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName' at $StartTime from $RepoRoot"
