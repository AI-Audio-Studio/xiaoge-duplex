<#
.SYNOPSIS
    Start the duplex voice agent MVP (web_ui_agent.py, console mode + web UI).

.DESCRIPTION
    - Loads configuration from the repo-root .env file.
    - Picks a free port for the web test panel (default 8787, auto-bumps if busy).
    - Launches the agent in a new window so you can talk to it (mic) and see logs.
    - Opens the browser test panel at http://localhost:<port>.
    - Records the process id in .run\web_ui_agent.pid so .\stop.ps1 can stop it.

.PARAMETER Port
    Preferred web UI port. Defaults to WEB_UI_PORT from .env (8787), else 8787.

.PARAMETER Text
    Use text input instead of the microphone (console --text).

.EXAMPLE
    .\start.ps1
.EXAMPLE
    .\start.ps1 -Port 8770 -Text
#>
param(
    [int]$Port = 0,
    [switch]$Text,
    [switch]$Background
)

$ErrorActionPreference = "Stop"
$repoRoot = $PSScriptRoot
Set-Location $repoRoot

$python  = Join-Path $repoRoot ".venv\Scripts\python.exe"
$agentDir = Join-Path $repoRoot "examples\voice_agents"
$runDir   = Join-Path $repoRoot ".run"
$pidFile  = Join-Path $runDir "web_ui_agent.pid"

if (-not (Test-Path $python)) {
    throw "Virtual environment missing. Run .\setup.ps1 first."
}

# --- Refuse to start a second instance ---------------------------------------
if (Test-Path $pidFile) {
    $oldPid = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($oldPid -and (Get-Process -Id $oldPid -ErrorAction SilentlyContinue)) {
        throw "Agent already running (PID $oldPid). Stop it first with .\stop.ps1"
    }
}

# --- Load .env into this process so the values are inherited by the agent -----
$envFile = Join-Path $repoRoot ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
            $idx = $line.IndexOf("=")
            $name = $line.Substring(0, $idx).Trim()
            $val  = $line.Substring($idx + 1).Trim()
            if ($name) { Set-Item -Path "Env:$name" -Value $val }
        }
    }
    Write-Host "Loaded .env" -ForegroundColor DarkGray
} else {
    Write-Warning "No .env found at $envFile - the agent may fail to reach its services."
}

# --- Console mode still wants LiveKit dev creds present (fake local job) ------
if (-not $env:LIVEKIT_URL)        { $env:LIVEKIT_URL = "ws://127.0.0.1:7880" }
if (-not $env:LIVEKIT_API_KEY)    { $env:LIVEKIT_API_KEY = "devkeydevkeydevkeydevkeydevkey12" }
if (-not $env:LIVEKIT_API_SECRET) { $env:LIVEKIT_API_SECRET = "devsecretdevsecretdevsecretdevse" }

# --- UTF-8 so the Chinese console output doesn't crash on Windows ------------
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
# Turn-detector / FunASR judge models are cached locally; stay offline for fast start.
if (-not $env:HF_HUB_OFFLINE)     { $env:HF_HUB_OFFLINE = "1" }
if (-not $env:TRANSFORMERS_OFFLINE) { $env:TRANSFORMERS_OFFLINE = "1" }

# --- Choose a free web UI port ----------------------------------------------
function Test-PortFree([int]$p) {
    -not (Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue)
}
$preferred = if ($Port -gt 0) { $Port }
             elseif ($env:WEB_UI_PORT) { [int]$env:WEB_UI_PORT }
             else { 8787 }
$chosen = $preferred
while (-not (Test-PortFree $chosen) -and $chosen -lt ($preferred + 20)) { $chosen++ }
if (-not (Test-PortFree $chosen)) { throw "No free port near $preferred for the web UI." }
if ($chosen -ne $preferred) {
    Write-Warning "Port $preferred is in use; using $chosen instead."
}
$env:WEB_UI_PORT = "$chosen"

# --- Launch ------------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $runDir | Out-Null
$logFile = Join-Path $runDir "web_ui_agent.log"

Write-Host "Starting voice agent (web UI on http://localhost:$chosen) ..." -ForegroundColor Cyan

# Pass the bare script name (WorkingDirectory is the agent dir) so the space in
# the repo path can't split the argument.
$scriptName = "web_ui_agent.py"
$mode = if ($Text) { @($scriptName, "console", "--text") } else { @($scriptName, "console") }

if ($Background) {
    # Headless: no window, logs streamed to .run\web_ui_agent.log.
    # NOTE: the console UI (mic-level visualizer, live transcript) is not visible.
    $proc = Start-Process -FilePath $python -ArgumentList $mode `
                -WorkingDirectory $agentDir -PassThru -WindowStyle Hidden `
                -RedirectStandardOutput "$logFile" -RedirectStandardError "$logFile.err"
    Write-Host "Running headless. Logs: $logFile" -ForegroundColor DarkGray
} else {
    # Default: a visible console window so you can SEE the microphone level
    # visualizer and the live transcript, and interact with the agent.
    $proc = Start-Process -FilePath $python -ArgumentList $mode `
                -WorkingDirectory $agentDir -PassThru
    Write-Host "A console window opened - watch the mic level bars while you speak." -ForegroundColor DarkGray
}

$proc.Id | Out-File -FilePath $pidFile -Encoding ascii
"$chosen" | Out-File -FilePath (Join-Path $runDir "web_ui_agent.port") -Encoding ascii

Write-Host ""
Write-Host "Started. PID $($proc.Id)  ->  $pidFile" -ForegroundColor Green
Write-Host "Web test panel: http://localhost:$chosen" -ForegroundColor Green
Write-Host "Talk to the agent through your microphone." -ForegroundColor Green
Write-Host "Stop it with:   .\stop.ps1" -ForegroundColor Green
