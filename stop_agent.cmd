@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1"
rem Success (exit 0): close this window automatically.
rem Failure (exit>=1): keep the window open so you can read the error above.
if errorlevel 1 (
  echo.
  echo [FAILED] Stop did not succeed. See the error above.
  echo This window is kept open on purpose; close it manually after checking.
  pause
)
