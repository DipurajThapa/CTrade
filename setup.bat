@echo off
REM Deriv payout census - Windows launcher
REM
REM Double-click this file. It exists because Windows blocks unsigned
REM PowerShell scripts downloaded from the internet, which stops setup.ps1
REM from running directly. This wrapper clears that mark and runs it.

cd /d "%~dp0"
echo Preparing files...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Get-ChildItem -LiteralPath '%~dp0' -Recurse -File | Unblock-File" 2>nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
