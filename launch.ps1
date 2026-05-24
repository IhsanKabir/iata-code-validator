#!/usr/bin/env pwsh
# Dev launcher for the IATA Code Validator.
#
# Run from anywhere with: `.\launch.ps1` (from repo root)
# or right-click → Run with PowerShell.
#
# Why this script exists:
#   - `src/gui.py` uses relative imports, so it can't be run directly.
#     The correct entry point is `run_app.py` at the repo root.
#   - The repo's `.venv` is currently on Python 3.14, which doesn't have
#     pre-built wheels for matplotlib / a few other deps. We fall back
#     to the system Python 3.13 install where the deps are present.
#
# End users (Roman, Suborna, etc.) should just launch the built .exe
# from dist/ or download the latest release — they don't need this.

$ErrorActionPreference = 'Stop'

# Prefer the project's venv if it has all the needed deps; otherwise
# fall back to the system Python 3.13. We probe by trying `python -c
# "import matplotlib"` against the venv first.
$repo = $PSScriptRoot
$venvPython = Join-Path $repo '.venv\Scripts\python.exe'
$system313 = "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe"

function Test-PythonHasDeps([string]$pythonPath) {
    if (-not (Test-Path $pythonPath)) { return $false }
    # PS 5.1 wraps native-cmd stderr in ErrorRecord objects when you use
    # `2>$null`, leaking the import traceback to the user. Assigning the
    # merged output stream to $null is the clean way to fully suppress.
    $null = & $pythonPath -c "import matplotlib, openpyxl, requests, keyring, rapidfuzz" 2>&1
    return ($LASTEXITCODE -eq 0)
}

$python = $null
if (Test-PythonHasDeps $venvPython) {
    $python = $venvPython
    Write-Host "Using .venv ($python)" -ForegroundColor DarkGray
} elseif (Test-PythonHasDeps $system313) {
    $python = $system313
    Write-Host "Using system Python 3.13 ($python)" -ForegroundColor DarkGray
} else {
    Write-Host "ERROR: No Python install has all the required deps." -ForegroundColor Red
    Write-Host "  Tried: $venvPython"
    Write-Host "  Tried: $system313"
    Write-Host ""
    Write-Host "Install deps with one of:" -ForegroundColor Yellow
    Write-Host "  py -3.13 -m pip install -r requirements.txt"
    Write-Host "  & '$venvPython' -m pip install -r requirements.txt"
    exit 1
}

& $python (Join-Path $repo 'run_app.py') @args
exit $LASTEXITCODE
