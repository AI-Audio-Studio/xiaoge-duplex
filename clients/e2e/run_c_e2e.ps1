param(
    [string]$CreateSessionUrl = "http://127.0.0.1:18082/create_session",
    [string]$WavPath = "E:\Project\Project2026\AIAudioCloudPlatform\xiaogeV2\xiaoge-duplex\xiaoge-duplex\tests\test_realtime\hello_world.wav",
    [string]$OutDir = "",
    [string]$CMakePath = "D:\Android\Android SDK\cmake\4.1.0\bin\cmake.exe",
    [string]$BuildDir = "",
    [string]$ApiKey = $env:XIAOGE_CLOUD_API_KEY
)

$ErrorActionPreference = "Stop"
$clients = Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")
function Invoke-Native {
    param(
        [string]$FilePath,
        [string[]]$Arguments
    )
    $stdout = [System.IO.Path]::GetTempFileName()
    $stderr = [System.IO.Path]::GetTempFileName()
    try {
        $process = Start-Process -FilePath $FilePath -ArgumentList $Arguments -NoNewWindow -Wait -PassThru `
            -RedirectStandardOutput $stdout -RedirectStandardError $stderr
        Get-Content -LiteralPath $stdout -ErrorAction SilentlyContinue | ForEach-Object { Write-Output $_ }
        Get-Content -LiteralPath $stderr -ErrorAction SilentlyContinue | ForEach-Object { Write-Output $_ }
        if ($process.ExitCode -ne 0) {
            throw "$FilePath failed with exit code $($process.ExitCode)"
        }
    } finally {
        Remove-Item -LiteralPath $stdout,$stderr -Force -ErrorAction SilentlyContinue
    }
}
function Invoke-Adb {
    param([string[]]$Arguments)
    Invoke-Native "adb" @("wait-for-device")
    Invoke-Native "adb" $Arguments
}
function Invoke-AdbShellRaw {
    param([string]$Command)
    Invoke-Native "adb" @("wait-for-device")
    $stdout = [System.IO.Path]::GetTempFileName()
    $stderr = [System.IO.Path]::GetTempFileName()
    try {
        $process = Start-Process -FilePath "cmd.exe" -ArgumentList @("/d", "/c", "adb shell $Command 2>&1") `
            -NoNewWindow -Wait -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
        Get-Content -LiteralPath $stdout -ErrorAction SilentlyContinue | ForEach-Object { Write-Output $_ }
        Get-Content -LiteralPath $stderr -ErrorAction SilentlyContinue | ForEach-Object { Write-Output $_ }
        $code = $process.ExitCode
    } finally {
        Remove-Item -LiteralPath $stdout,$stderr -Force -ErrorAction SilentlyContinue
    }
    if ($code -ne 0) {
        throw "adb shell failed with exit code $code"
    }
}

if (-not $OutDir) {
    $OutDir = Join-Path $PSScriptRoot "evidence\c"
}
if (-not $BuildDir) {
    $BuildDir = Join-Path $env:TEMP "xiaoge_c_deps\xiaoge-c-build-android-arm64"
}
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$preparedWav = Join-Path $OutDir "input_16k.wav"
python -W ignore::DeprecationWarning (Join-Path $PSScriptRoot "prepare_wav_16k.py") $WavPath $preparedWav

Invoke-Native $CMakePath @("--build", $BuildDir, "--parallel", "4")

$payload = @{
    device_id = "c-e2e-001"
    credential = @{ key_id = "dev-key"; signature = "hmac-signature" }
    caps = @("audio", "text", "cmd", "state")
    prefs = @{ locale = "zh-CN" }
    audio_format = @{ sample_rate = 16000; channels = 1; sample_format = "int16le" }
    client_version = "xiaoge-c-e2e-r5.2.2"
} | ConvertTo-Json -Depth 6 -Compress
$headers = @{}
if ($ApiKey) { $headers["x-api-key"] = $ApiKey }
$session = Invoke-RestMethod -Method Post -Uri $CreateSessionUrl -ContentType "application/json" -Headers $headers -Body $payload

Invoke-Adb @("reverse", "tcp:18082", "tcp:18082")
Invoke-Adb @("push", (Join-Path $BuildDir "xiaoge_demo_file"), "/data/local/tmp/xiaoge_demo_file")
Invoke-Adb @("push", $preparedWav, "/data/local/tmp/xiaoge_e2e.wav")
Invoke-Adb @("shell", "chmod", "755", "/data/local/tmp/xiaoge_demo_file")

$remoteOut = "/data/local/tmp/xiaoge_c_reply.wav"
$cmd = "/data/local/tmp/xiaoge_demo_file '$($session.ws_url)' '$($session.access_token)' '$($session.trace_id)' '$($session.session_id)' 'c-e2e-001' /data/local/tmp/xiaoge_e2e.wav $remoteOut"
Invoke-AdbShellRaw $cmd
Invoke-Adb @("pull", $remoteOut, (Join-Path $OutDir "c_reply.wav"))
