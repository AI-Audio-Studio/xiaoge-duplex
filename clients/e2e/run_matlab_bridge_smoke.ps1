param(
    [string]$CreateSessionUrl = "http://127.0.0.1:18082/create_session",
    [string]$WavPath = "E:\Project\Project2026\AIAudioCloudPlatform\xiaogeV2\xiaoge-duplex\xiaoge-duplex\tests\test_realtime\hello_world.wav",
    [string]$OutDir = "",
    [int]$UpPort = 5501,
    [int]$DownPort = 5502,
    [int]$EventsPort = 5503,
    [string]$ApiKey = $env:XIAOGE_CLOUD_API_KEY
)

$ErrorActionPreference = "Stop"
$clients = Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")
if (-not $OutDir) {
    $OutDir = Join-Path $PSScriptRoot "evidence\matlab_bridge"
}
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$preparedWav = Join-Path $OutDir "input_16k.wav"
$trace = Join-Path $OutDir "matlab_bridge_trace.jsonl"
$bridgeOut = Join-Path $OutDir "matlab_bridge.stdout.log"
$bridgeErr = Join-Path $OutDir "matlab_bridge.stderr.log"
python -W ignore::DeprecationWarning (Join-Path $PSScriptRoot "prepare_wav_16k.py") $WavPath $preparedWav

$credential = '{"key_id":"dev-key","signature":"hmac-signature"}'
$bridgeArgs = @(
    "-u",
    (Join-Path $clients "matlab\bridge\xiaoge_bridge.py"),
    $CreateSessionUrl,
    "matlab-bridge-smoke-001",
    $credential,
    "--up", "$UpPort",
    "--down", "$DownPort",
    "--events", "$EventsPort",
    "--api-key", $ApiKey,
    "--trace-log", $trace,
    "--wait-events-client"
)
$bridge = Start-Process -FilePath "python" `
    -ArgumentList $bridgeArgs `
    -WindowStyle Hidden `
    -RedirectStandardOutput $bridgeOut `
    -RedirectStandardError $bridgeErr `
    -PassThru

try {
    Start-Sleep -Seconds 1
    python (Join-Path $PSScriptRoot "matlab_bridge_smoke.py") `
        --host "127.0.0.1" `
        --up $UpPort `
        --down $DownPort `
        --events $EventsPort `
        --wav $preparedWav `
        --trace-log $trace
} finally {
    Stop-Process -Id $bridge.Id -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 300
    Write-Output "--- bridge stdout ---"
    Get-Content -LiteralPath $bridgeOut -Encoding UTF8 -ErrorAction SilentlyContinue
    Write-Output "--- bridge stderr ---"
    Get-Content -LiteralPath $bridgeErr -Encoding UTF8 -ErrorAction SilentlyContinue
}
