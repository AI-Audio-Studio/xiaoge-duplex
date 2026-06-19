<#
.SYNOPSIS
    Stop the duplex voice agent MVP started by .\start.ps1.

.DESCRIPTION
    Reads .run\web_ui_agent.pid and terminates that process together with any
    child processes the agent spawned. If the pid file is missing or stale, it
    falls back to matching python processes whose command line points at THIS
    repo's web_ui_agent.py (it never touches agents from other projects).
#>
$ErrorActionPreference = "Stop"
$repoRoot = $PSScriptRoot
$runDir  = Join-Path $repoRoot ".run"
$pidFile = Join-Path $runDir "web_ui_agent.pid"

function Stop-Tree([int]$rootPid) {
    # Kill children first, then the root.
    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$rootPid" -ErrorAction SilentlyContinue
    foreach ($c in $children) { Stop-Tree ([int]$c.ProcessId) }
    $p = Get-Process -Id $rootPid -ErrorAction SilentlyContinue
    if ($p) {
        Write-Host "Stopping PID $rootPid ($($p.ProcessName))" -ForegroundColor Cyan
        Stop-Process -Id $rootPid -Force -ErrorAction SilentlyContinue
    }
}

$stopped = $false

# --- Primary path: the recorded PID -----------------------------------------
if (Test-Path $pidFile) {
    $rec = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($rec -and (Get-Process -Id $rec -ErrorAction SilentlyContinue)) {
        Stop-Tree ([int]$rec)
        $stopped = $true
    } else {
        Write-Host "Recorded PID $rec is not running (stale pid file)." -ForegroundColor DarkGray
    }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    Remove-Item (Join-Path $runDir "web_ui_agent.port") -Force -ErrorAction SilentlyContinue
}

# --- Fallback: match this repo's web_ui_agent.py only ------------------------
if (-not $stopped) {
    $needle = (Join-Path $repoRoot "examples\voice_agents\web_ui_agent.py")
    $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*web_ui_agent.py*" -and $_.CommandLine -like "*$repoRoot*" }
    if ($procs) {
        foreach ($p in $procs) { Stop-Tree ([int]$p.ProcessId) }
        $stopped = $true
    }
}

if ($stopped) {
    Write-Host "Agent stopped." -ForegroundColor Green
} else {
    Write-Host "No running agent found for this project." -ForegroundColor Yellow
}
