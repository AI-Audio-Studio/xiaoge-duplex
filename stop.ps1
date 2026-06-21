<#
.SYNOPSIS
    Stop the duplex voice agent MVP started by .\start.ps1.

.DESCRIPTION
    1) Signals the test recorder to FLUSH (writes .run\recorder.flush, waits for
       .run\recorder.flushed) so the recording tail isn't dropped.
    2) Force-stops the agent process tree. Targets = the recorded PID (.run\pid) plus
       any python process whose command line points at THIS repo's web_ui_agent.py
       (covers the inference subprocess too). Never touches other projects' agents.
       Re-sweeps once to be certain nothing survives.
#>
$ErrorActionPreference = "Stop"
$repoRoot = $PSScriptRoot
$runDir  = Join-Path $repoRoot ".run"
$pidFile = Join-Path $runDir "web_ui_agent.pid"

function Get-AgentPids {
    $pids = @()
    if (Test-Path $pidFile) {
        $r = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($r -and ($r -match '^\d+$')) { $pids += [int]$r }
    }
    $match = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*web_ui_agent.py*" -and $_.CommandLine -like "*$repoRoot*" }
    foreach ($m in $match) { $pids += [int]$m.ProcessId }
    return ($pids | Where-Object { $_ -gt 0 } | Select-Object -Unique)
}

function Flush-Recording([int]$timeoutSec = 5) {
    if (-not (Test-Path $runDir)) { return }
    $flag = Join-Path $runDir "recorder.flush"
    $done = Join-Path $runDir "recorder.flushed"
    if (Test-Path $done) { Remove-Item $done -Force -ErrorAction SilentlyContinue }
    Set-Content -Path $flag -Value "flush" -Encoding ASCII -ErrorAction SilentlyContinue
    Write-Host "Flushing recording before stop..." -ForegroundColor Cyan
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    $ok = $false
    while ((Get-Date) -lt $deadline) {
        if (Test-Path $done) { $ok = $true; break }
        Start-Sleep -Milliseconds 150
    }
    if (Test-Path $flag) { Remove-Item $flag -Force -ErrorAction SilentlyContinue }
    if (Test-Path $done) { Remove-Item $done -Force -ErrorAction SilentlyContinue }
    if ($ok) { Write-Host "Recording flushed." -ForegroundColor Green }
}

function Stop-Tree([int]$rootPid) {
    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$rootPid" -ErrorAction SilentlyContinue
    foreach ($c in $children) { Stop-Tree ([int]$c.ProcessId) }
    $p = Get-Process -Id $rootPid -ErrorAction SilentlyContinue
    if ($p) {
        Write-Host "Stopping PID $rootPid ($($p.ProcessName))" -ForegroundColor Cyan
        Stop-Process -Id $rootPid -Force -ErrorAction SilentlyContinue
    }
}

$targets = @(Get-AgentPids)
if ($targets.Count -gt 0) {
    Write-Host "Agent PIDs: $($targets -join ', ')" -ForegroundColor DarkGray
    Flush-Recording                              # 先刷盘(不掉音),再杀进程
    foreach ($t in $targets) { Stop-Tree $t }
    Start-Sleep -Milliseconds 300                # 再扫一遍,确保子进程(推理进程等)清干净
    foreach ($t in @(Get-AgentPids)) { Stop-Tree $t }
    Write-Host "Agent stopped." -ForegroundColor Green
} else {
    Write-Host "No running agent found for this project." -ForegroundColor Yellow
}

Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $runDir "web_ui_agent.port") -Force -ErrorAction SilentlyContinue
