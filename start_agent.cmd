@echo off
setlocal
rem Default to -Test (enable test tooling: timeline + aligned recording -> runs\).
rem To run WITHOUT the test tooling, use:  .\start.ps1   (no -Test)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" -Test %*
echo.
pause
