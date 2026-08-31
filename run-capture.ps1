# Starts the 14-day capture and keeps this PC awake for the duration.
#
# To run: right-click -> "Run with PowerShell"

$ErrorActionPreference = "Stop"
$venvPython = Join-Path (Resolve-Path ".venv") "Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    Write-Host "Run setup.ps1 first." -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit 1
}

Write-Host ""
Write-Host "Stopping this PC from sleeping for the next 14 days..."
# Requires admin. Without it the capture dies the first time the PC sleeps.
try {
    powercfg /change standby-timeout-ac 0
    powercfg /change hibernate-timeout-ac 0
    powercfg /change monitor-timeout-ac 20
    Write-Host "  OK  Sleep and hibernate disabled while plugged in." -ForegroundColor Green
} catch {
    Write-Host "  !!  Could not change power settings." -ForegroundColor Yellow
    Write-Host "      Re-run this as Administrator, or set Settings ->"
    Write-Host "      System -> Power -> Sleep to 'Never' by hand."
    Write-Host "      If the PC sleeps, the capture stops."
}

Write-Host ""
Write-Host "Also turn OFF automatic Windows restarts for the next fortnight:"
Write-Host "  Settings -> Windows Update -> Advanced -> Active hours"
Write-Host "A reboot at 3am is the most likely way this dies."

Write-Host ""
Write-Host "Starting the capture. Leave this window open." -ForegroundColor Cyan
Write-Host "Watch it at http://127.0.0.1:8765 (open that in your browser)."
Write-Host "Press Ctrl+C to stop early - your data is kept either way."
Write-Host ""

# Dashboard in the background, capture in the foreground.
Start-Process -WindowStyle Hidden -FilePath $venvPython `
    -ArgumentList "-m", "deriv_census.cli", "serve"

& $venvPython -m deriv_census.cli run
