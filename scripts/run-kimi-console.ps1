param(
    [switch]$Text = $true
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot ".venv\\Scripts\\python.exe"
$script = Join-Path $repoRoot "examples\\voice_agents\\kimi_console_agent.py"

if (!(Test-Path $python)) {
    throw "Missing virtual environment at $python"
}

chcp 65001 > $null
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

if (-not $env:ANTHROPIC_API_KEY) {
    if ($env:ANTHROPIC_AUTH_TOKEN) {
        $env:ANTHROPIC_API_KEY = $env:ANTHROPIC_AUTH_TOKEN
    } else {
        throw "Missing ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN"
    }
}

if (-not $env:ANTHROPIC_BASE_URL) {
    throw "Missing ANTHROPIC_BASE_URL"
}

if (-not $env:ANTHROPIC_MODEL) {
    $env:ANTHROPIC_MODEL = "kimi-k2.6"
}

if (-not $env:LIVEKIT_URL) {
    $env:LIVEKIT_URL = "ws://127.0.0.1:7880"
}
if (-not $env:LIVEKIT_API_KEY) {
    $env:LIVEKIT_API_KEY = "devkeydevkeydevkeydevkeydevkey12"
}
if (-not $env:LIVEKIT_API_SECRET) {
    $env:LIVEKIT_API_SECRET = "devsecretdevsecretdevsecretdevse"
}

$argsList = @($script, "console")
if ($Text) {
    $argsList += "--text"
}

& $python @argsList
