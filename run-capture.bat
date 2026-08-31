@echo off
REM Starts the 14-day capture. Double-click this file.
REM
REM For the sleep settings to apply, right-click this file and choose
REM "Run as administrator". Without that the capture still runs, but the
REM PC may sleep and stop it.

cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Get-ChildItem -LiteralPath '%~dp0' -Recurse -File | Unblock-File" 2>nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-capture.ps1"
