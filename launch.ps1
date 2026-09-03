#!/usr/bin/env pwsh
# Dev launcher for Travel Ops Console.
#
# Run from the repo root with: `.\launch.ps1`
# or right-click -> Run with PowerShell.
#
# Why this script exists:
#   - `src/gui.py` uses relative imports, so it cannot be run directly.
#     The correct entry point is `run_app.py` at the repo root.
#   - More than one Python on this machine can OPEN the app and only some can
#     FINISH a run. The probe below rejects an interpreter that would fail
#     later, rather than launching into it and wasting the run.

$ErrorActionPreference = 'Stop'

$repo = $PSScriptRoot
$venvPython = Join-Path $repo '.venv\Scripts\python.exe'
$system313 = "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe"

# Everything the app needs to COMPLETE a run, not merely to draw a window.
# duckdb, pandas and pyarrow read the local sales warehouse: without them the
# counter gap sheet, the PNR check's cross-month lookup and the visit impact
# analysis each fail partway, after the user has waited for the parse.
# patchright drives the Zenith sign-in; sv_ttk and matplotlib draw the UI.
# usba_reporting is deliberately absent from this list -- it is a private wheel
# that only the Zenith > Reports sub-tab needs, and that tab explains itself
# when it is missing.
$required = @(
    'matplotlib', 'openpyxl', 'requests', 'keyring', 'rapidfuzz',
    'duckdb', 'pandas', 'pyarrow', 'patchright', 'sv_ttk', 'msal'
)

# find_spec does not execute the module, so this cannot print a traceback and
# needs no stderr suppression. It also carries no quote characters: Windows
# PowerShell strips those out of a native command's arguments, which turned
# ",".join into ,.join and made every interpreter look broken.
$probe = @'
import importlib.util, sys
for m in sys.argv[1:]:
    try:
        ok = importlib.util.find_spec(m) is not None
    except Exception:
        ok = False
    if not ok:
        print(m)
'@

$python = $null
$report = @()
foreach ($candidate in @($venvPython, $system313)) {
    if (-not (Test-Path $candidate)) {
        $report += "  not installed    $candidate"
        continue
    }
    $missing = @(& $candidate -c $probe @required)
    if ($LASTEXITCODE -ne 0) {
        $report += "  will not run     $candidate"
        continue
    }
    if ($missing.Count -eq 0) {
        $python = $candidate
        break
    }
    $report += ("  missing " + ($missing -join ', ') + "    $candidate")
}

if (-not $python) {
    Write-Host "ERROR: no Python here can finish a run." -ForegroundColor Red
    $report | ForEach-Object { Write-Host $_ }
    Write-Host ""
    Write-Host "Install the dependencies with:" -ForegroundColor Yellow
    Write-Host "  & '$venvPython' -m pip install -r requirements.txt"
    exit 1
}

$ver = & $python -c "import sys;print(sys.version.split()[0])"
Write-Host "Python $ver  $python" -ForegroundColor DarkGray
if ($report.Count -gt 0) {
    $report | ForEach-Object { Write-Host "  (skipped)$_" -ForegroundColor DarkGray }
}

# Say which code is about to run. A released .exe and this working tree can
# differ by a whole feature -- testing the wrong one costs a Zenith sign-in and
# a full run before anyone notices the tick box is not there.
try {
    $branch = (& git -C $repo rev-parse --abbrev-ref HEAD 2>$null)
    $sha = (& git -C $repo rev-parse --short HEAD 2>$null)
    if ($branch) {
        $mark = ''
        if (& git -C $repo status --porcelain 2>$null) {
            $mark = ' + uncommitted changes'
        }
        Write-Host "Source   $branch @ $sha$mark" -ForegroundColor DarkGray
    }
} catch {
    # git missing, or not a checkout: the app runs the same either way
}

& $python (Join-Path $repo 'run_app.py') @args
exit $LASTEXITCODE
