@echo off
setlocal
rem 成功后保留窗口的秒数（其间按任意键立即关闭；设 0 = 立即关闭）。
set "CLOSE_DELAY=3"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1"
if errorlevel 1 (
  rem 失败：保留窗口以便查看上方错误。
  echo.
  echo [失败] 停止未成功，请查看上方的错误信息。
  echo 窗口已保留，排查后请手动关闭。
  pause
) else (
  rem 成功：短暂延迟便于查看输出，然后自动关闭。
  echo.
  echo [成功] 已停止。本窗口将在 %CLOSE_DELAY% 秒后自动关闭（按任意键可立即关闭）。
  timeout /t %CLOSE_DELAY% >nul
)
