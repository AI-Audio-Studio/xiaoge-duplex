param(
    [switch]$Text = $true
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot ".venv\\Scripts\\python.exe"
$script = Join-Path $repoRoot "examples\\voice_agents\\qwen_gateway_console_agent.py"

if (!(Test-Path $python)) {
    throw "Missing virtual environment at $python"
}

chcp 65001 > $null
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

if (-not $env:QWEN_BASE_URL) {
    $env:QWEN_BASE_URL = "https://60.205.197.165:10092/llm/v1"
}
if (-not $env:QWEN_API_KEY) {
    $env:QWEN_API_KEY = "EMPTY"
}
if (-not $env:QWEN_MODEL) {
    $env:QWEN_MODEL = "Qwen3-4B"
}
if (-not $env:QWEN_VERIFY_SSL) {
    $env:QWEN_VERIFY_SSL = "false"
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
