@echo off
setlocal
rem Seconds to keep this window open on SUCCESS before auto-closing
rem (press any key during the wait to close immediately). Set 0 to close at once.
set "CLOSE_DELAY=5"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1"
if errorlevel 1 (
  rem Failure: keep the window open so you can read the error above.
  echo.
  echo [FAILED] Stop did not succeed. See the error above.
  echo This window is kept open on purpose; close it manually after checking.
  pause
) else (
  rem Success: brief delay so you can read the output, then auto-close.
  echo.
  echo [OK] Stopped. This window auto-closes in %CLOSE_DELAY%s ^(press a key to close now^).
  timeout /t %CLOSE_DELAY% >nul
)
