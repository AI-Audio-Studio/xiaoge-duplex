param(
    [string]$CreateSessionUrl = "http://127.0.0.1:18082/create_session",
    [string]$WavPath = "E:\Project\Project2026\AIAudioCloudPlatform\xiaogeV2\xiaoge-duplex\xiaoge-duplex\tests\test_realtime\hello_world.wav",
    [string]$OutDir = ""
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

if (-not $OutDir) {
    $OutDir = Join-Path $PSScriptRoot "evidence\android"
}
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$preparedWav = Join-Path $OutDir "input_16k.wav"
python -W ignore::DeprecationWarning (Join-Path $PSScriptRoot "prepare_wav_16k.py") $WavPath $preparedWav
$reportPath = Join-Path $OutDir "android_e2e_result.txt"

Push-Location (Join-Path $clients "android")
try {
    Invoke-Native (Join-Path (Get-Location) "gradlew.bat") @(
        ":xiaoge-sdk-core:testDebugUnitTest",
        "--tests", "com.xiaoge.client.AndroidFileE2eTest",
        "-Dxiaoge.e2e.enabled=true",
        "-Dxiaoge.e2e.createSessionUrl=$CreateSessionUrl",
        "-Dxiaoge.e2e.wavPath=$preparedWav",
        "-Dxiaoge.e2e.reportPath=$reportPath"
    )
    Get-Content -LiteralPath $reportPath -Encoding UTF8
} finally {
    Pop-Location
}
