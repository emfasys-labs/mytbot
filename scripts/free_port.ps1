param([int]$Port = 8000)
$conns = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue
$pids = $conns | Select-Object -ExpandProperty OwningProcess -Unique | Where-Object { $_ -gt 0 }
foreach ($p in $pids) {
    Write-Host "Killing PID $p..."
    Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 3
$remaining = (Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue | Measure-Object).Count
if ($remaining -eq 0) {
    Write-Host "Port $Port is free."
} else {
    Write-Host "Port $Port still has $remaining connections (will clear after browser tabs close)."
}
