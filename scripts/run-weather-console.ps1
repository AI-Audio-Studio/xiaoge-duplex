param(
    [switch]$Text,
    [switch]$ListDevices,
    [switch]$KeepOpenAIBaseUrl,
    [string]$InputDevice,
    [string]$OutputDevice
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot ".venv\\Scripts\\python.exe"
$script = Join-Path $repoRoot "examples\\voice_agents\\weather_agent.py"

if (!(Test-Path $python)) {
    throw "Missing virtual environment at $python"
}

# Avoid UnicodeEncodeError in the console CLI on Windows.
chcp 65001 > $null
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

# Console mode still expects these values to exist even when running a fake local job.
if (-not $env:LIVEKIT_URL) {
    $env:LIVEKIT_URL = "ws://127.0.0.1:7880"
}
if (-not $env:LIVEKIT_API_KEY) {
    $env:LIVEKIT_API_KEY = "devkeydevkeydevkeydevkeydevkey12"
}
if (-not $env:LIVEKIT_API_SECRET) {
    $env:LIVEKIT_API_SECRET = "devsecretdevsecretdevsecretdevse"
}

if (-not $KeepOpenAIBaseUrl) {
    Remove-Item Env:OPENAI_BASE_URL -ErrorAction SilentlyContinue
    Remove-Item Env:OPENAI_API_BASE -ErrorAction SilentlyContinue
}

$argsList = @($script, "console")
if ($Text) {
    $argsList += "--text"
}
if ($ListDevices) {
    $argsList += "--list-devices"
}
if ($InputDevice) {
    $argsList += @("--input-device", $InputDevice)
}
if ($OutputDevice) {
    $argsList += @("--output-device", $OutputDevice)
}

& $python @argsList
