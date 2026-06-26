@echo off
setlocal
rem 成功后保留窗口的秒数（其间按任意键立即关闭；设 0 = 立即关闭）。
set "CLOSE_DELAY=3"
rem 默认带 -Test（开启测试工具：时间线 + 对齐录音 -> runs\）。
rem 不带测试工具运行：直接用 .\start.ps1（无 -Test）。
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" -Test %*
if errorlevel 1 (
  rem 失败：保留窗口以便查看上方错误。
  echo.
  echo [失败] 启动未成功，请查看上方的错误信息。
  echo 窗口已保留，排查后请手动关闭。
  pause
) else (
  rem 成功：短暂延迟便于查看输出，然后自动关闭。
  echo.
  echo [成功] 已启动。本窗口将在 %CLOSE_DELAY% 秒后自动关闭（按任意键可立即关闭）。
  timeout /t %CLOSE_DELAY% >nul
)
