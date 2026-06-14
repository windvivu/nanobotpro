Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

param(
    [int]$Port = 8900,
    [string]$HostName = "127.0.0.1"
)

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $ScriptRoot "venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $VenvPython)) {
    Write-Host "Missing local venv Python:" -ForegroundColor Red
    Write-Host "  $VenvPython" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Create and install the environment first:" -ForegroundColor Cyan
    Write-Host "  python -m venv venv"
    Write-Host "  .\venv\Scripts\Activate.ps1"
    Write-Host "  python -m pip install -e "".[web,dev]"""
    exit 1
}

Write-Host ""
Write-Host "=== Nanobot Adminbot Launcher ===" -ForegroundColor Cyan
Write-Host "Starting local multi-bot manager on http://$HostName`:$Port" -ForegroundColor Green
if ($HostName -ne "127.0.0.1" -and $HostName -ne "localhost") {
    Write-Host "Warning: non-local bind exposes Adminbot login to the network. Use a strong password." -ForegroundColor Yellow
}
Write-Host ""
Write-Host "Runtime state is stored in .adminbot/ and is gitignored." -ForegroundColor DarkGray
Write-Host "Press Ctrl+C to stop Adminbot." -ForegroundColor DarkGray
Write-Host ""

Set-Location -LiteralPath $ScriptRoot
& $VenvPython -m adminbot.app.main web --port $Port --host $HostName
