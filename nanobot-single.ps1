# Run one nanobot bot with its web dashboard, without Adminbot.
#   .\nanobot-single.ps1                                  default bot (~\.nanobot\config.json)
#   .\nanobot-single.ps1 --config "path\to\config.json"   another bot, e.g. one created in Adminbot
# If scripts are blocked: powershell -ExecutionPolicy Bypass -File .\nanobot-single.ps1
# Refuses to start while Adminbot or a bot it started is running (the account would answer twice).
$adminbot = Get-CimInstance Win32_Process |
    Where-Object { $_.Name -like 'python*' -and $_.CommandLine -match 'adminbot[.]app[.]main|[.]adminbot[\\/]instances' }
if ($adminbot) {
    Write-Host "Adminbot or one of its bots is running (PID $($adminbot.ProcessId -join ', ')). Stop it in Adminbot first." -ForegroundColor Red
    exit 1
}
# Reuses venv\ or .venv\; creates venv\ and installs nanobot on the first run.
& "$PSScriptRoot\scripts\ensure-venv.ps1"
if ($LASTEXITCODE -ne 0) { exit 1 }
$py = "$PSScriptRoot\venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $py)) { $py = "$PSScriptRoot\.venv\Scripts\python.exe" }

# Opens the dashboard in the browser once it answers (NANOBOT_NO_BROWSER=1 to skip).
# Its address comes from the config the gateway reads (default port 8899).
$cfg = Join-Path $HOME ".nanobot\config.json"; $webHost = $null; $port = 8899
for ($i = 0; $i -lt $args.Count; $i++) {
    if ($args[$i] -in "--config", "-c" -and $i + 1 -lt $args.Count) { $cfg = $args[$i + 1] }
    elseif ($args[$i] -like "--config=*") { $cfg = $args[$i].Substring(9) }
    elseif ($args[$i] -eq "--host" -and $i + 1 -lt $args.Count) { $webHost = $args[$i + 1] }
}
try {
    $web = (Get-Content -Raw -Encoding UTF8 -LiteralPath $cfg -ErrorAction Stop | ConvertFrom-Json).gateway.web
    if ($web.port) { $port = [int]$web.port }
    if (-not $webHost) { $webHost = $web.host }
} catch {}
if (-not $webHost -or $webHost -in "0.0.0.0", "::") { $webHost = "127.0.0.1" }
if ($args -notcontains "--help") {
    Start-Process powershell -WindowStyle Hidden -ArgumentList @(
        "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", "`"$PSScriptRoot\scripts\open-browser.ps1`"", "-Url", "http://${webHost}:$port")
}

& $py -m nanobot gateway --web @args
exit $LASTEXITCODE
