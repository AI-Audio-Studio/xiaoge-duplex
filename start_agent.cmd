@echo off
setlocal
rem Default to -Test (enable test tooling: timeline + aligned recording -> runs\).
rem To run WITHOUT the test tooling, use:  .\start.ps1   (no -Test)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" -Test %*
rem Success (exit 0): close this launcher window automatically.
rem Failure (exit>=1): keep the window open so you can read the error above.
if errorlevel 1 (
  echo.
  echo [FAILED] Start did not succeed. See the error above.
  echo This window is kept open on purpose; close it manually after checking.
  pause
)
