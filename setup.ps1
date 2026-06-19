<#
.SYNOPSIS
    Rebuild the local Python virtual environment for the duplex voice agent MVP.

.DESCRIPTION
    Creates a fresh .venv with Python 3.13 and installs only the packages the
    MVP needs (editable, from this repo):
      - livekit-agents (core)            [editable]
      - livekit-plugins-openai           [editable]   Qwen LLM via OpenAI gateway
      - livekit-plugins-silero           [editable]   VAD
      - livekit-plugins-turn-detector    [editable]   end-of-turn detection
      - dashscope                        Bailian / Qwen TTS
    Then downloads the turn-detector model files so the agent can run offline.

    Re-run this any time to recreate the environment from scratch.

.EXAMPLE
    .\setup.ps1
.EXAMPLE
    .\setup.ps1 -SkipModelDownload
#>
param(
    [switch]$SkipModelDownload
)

$ErrorActionPreference = "Stop"
$repoRoot = $PSScriptRoot
Set-Location $repoRoot

$venv   = Join-Path $repoRoot ".venv"
$python = Join-Path $venv "Scripts\python.exe"

Write-Host "==> Rebuilding virtual environment at $venv" -ForegroundColor Cyan

# 1. Locate a Python 3.13 interpreter (the venv binaries are version-specific).
$pyExe = $null
if (Get-Command py -ErrorAction SilentlyContinue) {
    $pyExe = @("py", "-3.13")
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $pyExe = @("python")
} else {
    throw "No Python interpreter found. Install Python 3.13 (or adjust this script)."
}

# 2. Remove any existing venv and create a clean one.
if (Test-Path $venv) {
    Write-Host "==> Removing existing .venv" -ForegroundColor Cyan
    Remove-Item -Recurse -Force $venv
}
Write-Host "==> Creating venv" -ForegroundColor Cyan
& $pyExe[0] $pyExe[1..($pyExe.Count-1)] -m venv $venv
if (-not (Test-Path $python)) { throw "venv creation failed: $python not found" }

# 3. Upgrade packaging tooling.
Write-Host "==> Upgrading pip / wheel / setuptools" -ForegroundColor Cyan
& $python -m pip install --upgrade pip wheel setuptools

# 4. Core agent framework (editable) with the extras the MVP uses.
Write-Host "==> Installing livekit-agents (editable)" -ForegroundColor Cyan
& $python -m pip install -e ".\livekit-agents[codecs,images,mcp]"

# 5. Plugins (editable, --no-deps so pip keeps our local livekit-agents
#    instead of pulling the published one from PyPI).
Write-Host "==> Installing plugins (editable, no-deps)" -ForegroundColor Cyan
& $python -m pip install --no-deps `
    -e ".\livekit-plugins\livekit-plugins-openai" `
    -e ".\livekit-plugins\livekit-plugins-silero" `
    -e ".\livekit-plugins\livekit-plugins-turn-detector"

# 6. Runtime deps for the plugins (normally pulled transitively) + TTS SDK
#    + native KWS interrupt (sherpa-onnx + pypinyin).
Write-Host "==> Installing runtime dependencies" -ForegroundColor Cyan
& $python -m pip install `
    "onnxruntime>=1.18" `
    "transformers>=4.47.1,!=4.57.2,!=4.57.3" `
    "jinja2" `
    "dashscope" `
    "sherpa-onnx" `
    "pypinyin"

# 7. Download model files so the turn detector works in offline mode.
if (-not $SkipModelDownload) {
    Write-Host "==> Downloading turn-detector model files (one-time, needs internet)" -ForegroundColor Cyan
    Push-Location (Join-Path $repoRoot "examples\voice_agents")
    try {
        $env:PYTHONUTF8 = "1"
        $env:HF_HUB_OFFLINE = "0"
        $env:TRANSFORMERS_OFFLINE = "0"
        & $python "web_ui_agent.py" download-files
    } finally {
        Pop-Location
    }
}

Write-Host ""
Write-Host "Setup complete. Start the agent with:  .\start.ps1" -ForegroundColor Green
