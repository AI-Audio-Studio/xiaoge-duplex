param(
    [string]$WavPath = "E:\Project\Project2026\AIAudioCloudPlatform\xiaogeV2\xiaoge-duplex\xiaoge-duplex\tests\test_realtime\hello_world.wav",
    [string]$GatewayDir = "E:\Project\Project2026\AIAudioCloudPlatform\xiaogeV2\g3_solution_review_package_r2_r5_2_2_20260804\04_cloud_g3_application\evidence",
    [int]$Port = 18082,
    [string]$OutDir = ""
)

$ErrorActionPreference = "Stop"
if (-not $OutDir) {
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $OutDir = Join-Path $PSScriptRoot "evidence\$stamp"
}
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$gatewayOut = Join-Path $OutDir "fake_gateway.stdout.log"
$gatewayErr = Join-Path $OutDir "fake_gateway.stderr.log"
$gateway = Start-Process -FilePath "python" `
    -ArgumentList @("-u", "run_fake_gateway.py", "--host", "127.0.0.1", "--port", "$Port") `
    -WorkingDirectory $GatewayDir `
    -WindowStyle Hidden `
    -RedirectStandardOutput $gatewayOut `
    -RedirectStandardError $gatewayErr `
    -PassThru

$results = @()
$url = "http://127.0.0.1:$Port/create_session"
try {
    Start-Sleep -Seconds 3
    $steps = @(
        @{ Name = "c"; Script = "run_c_e2e.ps1" },
        @{ Name = "python"; Script = "run_python_e2e.ps1" },
        @{ Name = "android"; Script = "run_android_e2e.ps1" },
        @{ Name = "matlab_bridge"; Script = "run_matlab_bridge_smoke.ps1" }
    )
    foreach ($step in $steps) {
        $log = Join-Path $OutDir "$($step.Name).log"
        $stepOut = Join-Path $OutDir $step.Name
        New-Item -ItemType Directory -Force -Path $stepOut | Out-Null
        & (Join-Path $PSScriptRoot $step.Script) -CreateSessionUrl $url -WavPath $WavPath -OutDir $stepOut *> $log
        $code = $LASTEXITCODE
        if ($null -eq $code) { $code = 0 }
        $results += [PSCustomObject]@{ Name = $step.Name; ExitCode = $code; Log = $log }
        if ($code -ne 0) {
            throw "$($step.Name) e2e failed with exit code $code"
        }
    }
} finally {
    Stop-Process -Id $gateway.Id -Force -ErrorAction SilentlyContinue
}

$summary = Join-Path $OutDir "E2E_SUMMARY.md"
$generatedAt = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$lines = @(
    "# Clients R5.2.2 Cross-Language E2E Summary",
    "",
    "generated_at: $generatedAt",
    "",
    "fake Gateway: $url",
    "wav: $WavPath",
    "",
    "| Target | ExitCode | Log |",
    "| --- | ---: | --- |"
)
foreach ($result in $results) {
    $lines += "| $($result.Name) | $($result.ExitCode) | $($result.Log) |"
}
$lines | Set-Content -LiteralPath $summary -Encoding UTF8
Get-Content -LiteralPath $summary -Encoding UTF8
