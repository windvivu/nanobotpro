# Make sure venv\ (or .venv\) exists and nanobot is installed in it; set it up on the first run.
# Used by nanobot-launcher.ps1 and nanobot-single.ps1. Docker images do not need it.
$ErrorActionPreference = "Continue"  # pip/python write harmless warnings to stderr
$Root = Split-Path -Parent $PSScriptRoot
$Ready = "import importlib.metadata as m, importlib.util as u; m.distribution('nanobot'); assert all(u.find_spec(x) for x in ('fastapi', 'uvicorn', 'openpyxl', 'docx', 'pypdf'))"

# An existing venv\ (or .venv\) is reused, never recreated.
$VenvPython = $null
foreach ($name in "venv", ".venv") {
    $candidate = Join-Path $Root "$name\Scripts\python.exe"
    if (Test-Path -LiteralPath $candidate) { $VenvPython = $candidate; break }
}
if ($VenvPython) {
    & $VenvPython -c $Ready 2>$null
    if ($LASTEXITCODE -eq 0) { exit 0 }
}

Write-Host "Setting up the Python environment (first run only, may take a few minutes)..." -ForegroundColor Cyan
if (-not $VenvPython) {
    $VenvDir = Join-Path $Root "venv"
    $VenvPython = Join-Path $VenvDir "Scripts\python.exe"
    # Use the first Python 3.11+ found. "py -3" only sees python.org installs, and
    # "python"/"python3" may be the Microsoft Store stub, so try them in turn.
    $py = $null
    foreach ($candidate in "py -3", "py", "python", "python3") {
        $parts = $candidate -split " "
        if (-not (Get-Command $parts[0] -ErrorAction SilentlyContinue)) { continue }
        $pyArgs = @($parts | Select-Object -Skip 1)
        & $parts[0] @pyArgs -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { $py = $parts[0]; break }
    }
    if (-not $py) {
        Write-Host "Python 3.11 or newer is required. Install it from https://www.python.org/downloads/ and run again." -ForegroundColor Red
        exit 1
    }
    & $py @pyArgs -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Could not create $VenvDir." -ForegroundColor Red
        exit 1
    }
}

Push-Location -LiteralPath $Root
try { & $VenvPython -m pip install -e ".[web,office]" } finally { Pop-Location }
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing nanobot failed. Fix the error above and run again." -ForegroundColor Red
    exit 1
}
Write-Host "Python environment ready." -ForegroundColor Green
exit 0
