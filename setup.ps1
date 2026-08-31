# Deriv payout census - Windows setup
#
# Run this once. It installs everything, checks it works, then measures
# Deriv's cut and tells you in plain English whether this is worth building.
#
# To run: right-click this file -> "Run with PowerShell"
#   or in PowerShell:  .\setup.ps1

$ErrorActionPreference = "Stop"

function Say($text)  { Write-Host "`n$text" -ForegroundColor Cyan }
function Good($text) { Write-Host "  OK  $text" -ForegroundColor Green }
function Bad($text)  { Write-Host "  !!  $text" -ForegroundColor Red }

Write-Host ""
Write-Host "===================================================================="
Write-Host " Deriv payout census - setup"
Write-Host "===================================================================="

# --- 1. Python -------------------------------------------------------------
Say "Step 1 of 4: checking Python"

$python = $null
foreach ($candidate in @("py -3", "python", "python3")) {
    try {
        $parts = $candidate.Split(" ")
        $version = & $parts[0] $parts[1..($parts.Length-1)] --version 2>&1
        if ($version -match "Python 3\.(\d+)") {
            if ([int]$Matches[1] -ge 11) { $python = $candidate; break }
            Bad "Found $version but this needs Python 3.11 or newer."
        }
    } catch { }
}

if (-not $python) {
    Bad "Python 3.11+ not found."
    Write-Host ""
    Write-Host "  Install it with this command, then run this script again:"
    Write-Host ""
    Write-Host "      winget install Python.Python.3.12" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  (Or download from https://www.python.org/downloads/ and TICK"
    Write-Host "   the box that says 'Add python.exe to PATH' during install.)"
    Write-Host ""
    Read-Host "Press Enter to close"
    exit 1
}
Good "Python found ($python)"

# --- 2. Virtual environment and dependencies -------------------------------
Say "Step 2 of 4: installing (takes 1-2 minutes, downloads about 100 MB)"

$parts = $python.Split(" ")
if (-not (Test-Path ".venv")) {
    & $parts[0] $parts[1..($parts.Length-1)] -m venv .venv
}
$venvPython = Join-Path (Resolve-Path ".venv") "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Bad "Could not create the virtual environment. Is the folder read-only?"
    Read-Host "Press Enter to close"
    exit 1
}

& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet -e ".[dev]"
if ($LASTEXITCODE -ne 0) {
    Bad "Install failed. Check your internet connection and try again."
    Read-Host "Press Enter to close"
    exit 1
}
Good "Installed"

# --- 3. Self-test ----------------------------------------------------------
Say "Step 3 of 4: checking the tool works (about 2 minutes)"
Write-Host "  Running its own tests. Nothing touches Deriv yet."

& $venvPython -m pytest -q 2>&1 | Select-Object -Last 3
if ($LASTEXITCODE -ne 0) {
    Bad "Self-test failed. Do not trust any number this produces."
    Write-Host "  Send the output above back and I will fix it."
    Read-Host "Press Enter to close"
    exit 1
}
Good "All tests passed"

# --- 4. The measurement ----------------------------------------------------
Say "Step 4 of 4: measuring Deriv's cut"
Write-Host "  Connecting to Deriv and asking for real prices."
Write-Host "  This only READS prices. It cannot place a trade or touch money."
Write-Host ""

& $venvPython -m deriv_census.cli preflight --dump-raw capture.json
$preflight = $LASTEXITCODE

Write-Host ""
Write-Host "===================================================================="
if ($preflight -eq 0) {
    Good "Done. Read the 'WHAT THIS MEANS' box above - that is your answer."
    Write-Host ""
    Write-Host "  A file called capture.json was saved in this folder."
    Write-Host "  Send it back to Claude so the readings can be double-checked."
    Write-Host ""
    Write-Host "  If the verdict said the 14-day capture is worth running,"
    Write-Host "  start it with:" -NoNewline
    Write-Host "  .\run-capture.ps1" -ForegroundColor Yellow
} else {
    Bad "Preflight did not pass."
    Write-Host ""
    Write-Host "  Most likely reasons:"
    Write-Host "    - The currency market is closed (it shuts Friday evening"
    Write-Host "      to Sunday evening). Try again on a weekday."
    Write-Host "    - No internet, or a firewall is blocking the connection."
    Write-Host ""
    Write-Host "  Send capture.json back and the exact messages above."
}
Write-Host "===================================================================="
Write-Host ""
Read-Host "Press Enter to close"
