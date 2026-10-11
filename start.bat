@echo off
REM Bhajan Studio launcher for Windows: double-click this file to start the app.
REM It runs start.ps1 (PowerShell), skipping the default script-execution policy for this one run only.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
if errorlevel 1 (
  echo.
  echo Bhajan Studio stopped with an error. See the message above.
  pause
)
