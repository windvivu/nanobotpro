param(
    [int]$Port = 8900,
    [string]$HostName = "127.0.0.1"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

# Reuses venv\ or .venv\; creates venv\ and installs nanobot on the first run.
& (Join-Path $ScriptRoot "scripts\ensure-venv.ps1")
if ($LASTEXITCODE -ne 0) { exit 1 }
$VenvPython = Join-Path $ScriptRoot "venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $VenvPython)) { $VenvPython = Join-Path $ScriptRoot ".venv\Scripts\python.exe" }

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
# Opens the dashboard in the browser once it answers (NANOBOT_NO_BROWSER=1 to skip).
$BrowserHost = if ($HostName -in "0.0.0.0", "::") { "127.0.0.1" } else { $HostName }
Start-Process powershell -WindowStyle Hidden -ArgumentList @(
    "-NoProfile", "-ExecutionPolicy", "Bypass",
    "-File", "`"$(Join-Path $ScriptRoot 'scripts\open-browser.ps1')`"", "-Url", "http://${BrowserHost}:$Port")
& $VenvPython -m adminbot.app.main web --port $Port --host $HostName
