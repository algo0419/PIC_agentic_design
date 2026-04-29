$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

$processes = Get-CimInstance Win32_Process |
    Where-Object { $_.Name -like "python*" -and $_.CommandLine -like "*telegram_codex_bridge.py*" }

if (-not $processes) {
    Write-Host "Bridge: stopped"
    exit 1
}

Write-Host "Bridge: running"
$processes |
    Select-Object ProcessId, Name, CreationDate, CommandLine |
    Format-List

Write-Host ""
Write-Host "Latest bridge logs:"
Get-ChildItem .\data\logs\bridge-run-*.log -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 6 Name, Length, LastWriteTime |
    Format-Table -AutoSize

Write-Host ""
Write-Host "Latest task logs:"
Get-ChildItem .\data\logs\*.stdout.log -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -notlike "bridge-run-*" } |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 6 Name, Length, LastWriteTime |
    Format-Table -AutoSize
