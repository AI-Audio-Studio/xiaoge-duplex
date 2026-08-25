param(
    [string]$CreateSessionUrl = "http://127.0.0.1:18082/create_session",
    [string]$WavPath = "E:\Project\Project2026\AIAudioCloudPlatform\xiaogeV2\xiaoge-duplex\xiaoge-duplex\tests\test_realtime\hello_world.wav",
    [string]$OutDir = "",
    [string]$ApiKey = $env:XIAOGE_CLOUD_API_KEY
)

$ErrorActionPreference = "Stop"
$clients = Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")
if (-not $OutDir) {
    $OutDir = Join-Path $PSScriptRoot "evidence\python"
}
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$preparedWav = Join-Path $OutDir "input_16k.wav"
python -W ignore::DeprecationWarning (Join-Path $PSScriptRoot "prepare_wav_16k.py") $WavPath $preparedWav

$outWav = Join-Path $OutDir "python_reply.wav"
$trace = Join-Path $OutDir "python_trace.jsonl"
$credential = '{"key_id":"dev-key","signature":"hmac-signature"}'

Push-Location (Join-Path $clients "python")
try {
    python demo_file.py $CreateSessionUrl "python-e2e-001" $credential $preparedWav $outWav --api-key $ApiKey --trace-log $trace
} finally {
    Pop-Location
}
