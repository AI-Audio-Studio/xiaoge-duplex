param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-fA-F]{40}$')]
    [string]$ExpectedCommit,

    [Parameter(Mandatory = $true)]
    [string]$EvidenceDir
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Get-Location).Path
$actualCommit = (git rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $actualCommit -ne $ExpectedCommit) {
    throw "HEAD does not match ExpectedCommit"
}

$statusBefore = @(git status --porcelain=v1)
if ($LASTEXITCODE -ne 0 -or $statusBefore.Count -ne 0) {
    throw 'clean checkout required before evidence collection'
}

$resolvedEvidence = [System.IO.Path]::GetFullPath($EvidenceDir)
$repoPrefix = $repoRoot.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
if (
    $resolvedEvidence -eq $repoRoot -or
    $resolvedEvidence.StartsWith($repoPrefix, [System.StringComparison]::OrdinalIgnoreCase)
) {
    throw 'EvidenceDir must be outside the checkout'
}
New-Item -ItemType Directory -Force -Path $resolvedEvidence | Out-Null

# uv writes normal interpreter/progress diagnostics to stderr. PowerShell 5 surfaces those as
# NativeCommandError records under Stop, so native tools below are judged by LASTEXITCODE instead.
$ErrorActionPreference = 'Continue'

Set-Content -Encoding UTF8 -LiteralPath (Join-Path $resolvedEvidence '01_commit.txt') `
    -Value $actualCommit
@(
    "utc=$([DateTime]::UtcNow.ToString('o'))"
    "python=$(python --version 2>&1)"
    "uv=$(uv --version 2>&1)"
) | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $resolvedEvidence '02_toolchain.txt')

uv sync --offline --all-extras --dev 2>&1 |
    Tee-Object -FilePath (Join-Path $resolvedEvidence '10_uv_sync.log')
if ($LASTEXITCODE -ne 0) { throw 'uv sync failed' }

$tests = @(
    'tests/test_ours_live_transcript.py',
    'tests/test_ours_g2_r5_2_2_cloud_contract.py',
    'tests/test_ours_g3_intent_command_rag.py',
    'tests/test_ours_g3_ws_session_protocol.py',
    'tests/test_ours_g3_x3_skill_commands.py',
    'tests/test_ours_concurrency_m5_admin_routes.py',
    'tests/test_ours_knowledge.py',
    'tests/test_ours_music_player.py'
)
uv run pytest @tests -q 2>&1 |
    Tee-Object -FilePath (Join-Path $resolvedEvidence '20_pytest.log')
if ($LASTEXITCODE -ne 0) { throw 'pytest failed' }

$ruffTargets = @(
    'examples/voice_agents/common/g3_intent.py',
    'examples/voice_agents/app/knowledge_index.py',
    'examples/voice_agents/app/music_player.py',
    'examples/voice_agents/app/music_tools.py',
    'examples/voice_agents/app/online_interrupt_host.py',
    'examples/voice_agents/app/session_state.py',
    'examples/voice_agents/app/setup_taps.py',
    'examples/voice_agents/app/web_audio.py',
    'examples/voice_agents/gateway/config.py',
    'examples/voice_agents/gateway/main.py',
    'examples/voice_agents/gateway/proxy.py',
    'examples/voice_agents/live_transcript.py',
    'examples/voice_agents/web_ui_agent.py',
    'examples/voice_agents/webpanel/command_lifecycle.py',
    'examples/voice_agents/webpanel/state.py',
    'examples/voice_agents/webpanel/server.py',
    'examples/voice_agents/webpanel/bridge.py',
    'tests/_g2_contract_r5_2_2.py',
    'tests/test_ours_concurrency_m5_admin_routes.py',
    'tests/test_ours_g2_r5_2_2_cloud_contract.py',
    'tests/test_ours_g3_intent_command_rag.py',
    'tests/test_ours_g3_ws_session_protocol.py',
    'tests/test_ours_g3_x3_skill_commands.py',
    'tests/test_ours_knowledge.py',
    'tests/test_ours_live_transcript.py',
    'tests/test_ours_music_player.py'
)
uv run ruff check @ruffTargets 2>&1 |
    Tee-Object -FilePath (Join-Path $resolvedEvidence '30_ruff.log')
if ($LASTEXITCODE -ne 0) { throw 'ruff failed' }

$snapshotFiles = @($ruffTargets) + @(
    'examples/voice_agents/data/knowledge/product_manual.md',
    'examples/voice_agents/webpanel/static/index.html',
    'uv.lock',
    'xg.sh'
)
$productionFiles = @($ruffTargets | Where-Object { $_ -notlike 'tests/*' }) + @(
    'examples/voice_agents/data/knowledge/product_manual.md',
    'examples/voice_agents/webpanel/static/index.html',
    'xg.sh'
)
$sourceHashes = foreach ($path in $snapshotFiles) {
    $item = Get-Item -LiteralPath $path
    [ordered]@{
        path = $path.Replace('\', '/')
        bytes = $item.Length
        sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLower()
    }
}
$sourceHashes | ConvertTo-Json -Depth 3 |
    Set-Content -Encoding UTF8 -LiteralPath (Join-Path $resolvedEvidence '40_source_sha256.json')

$patterns = [ordered]@{
    private_key = '-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----'
    jwt = 'eyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}'
    openai_style = '(?i)sk-[A-Za-z0-9_-]{20,}'
    literal_bearer = '(?i)Bearer\s+[A-Za-z0-9._-]{24,}'
    removed_demo_default = 'DEFAULT_RUOYI_API_KEY'
}
$scanText = ($productionFiles | ForEach-Object {
    Get-Content -Raw -Encoding UTF8 -LiteralPath $_
}) -join "`n"
$scanResult = [ordered]@{}
$scanFailed = $false
foreach ($entry in $patterns.GetEnumerator()) {
    $count = [regex]::Matches($scanText, $entry.Value).Count
    $scanResult[$entry.Key] = $count
    if ($count -ne 0) { $scanFailed = $true }
}
$scanResult | ConvertTo-Json |
    Set-Content -Encoding UTF8 -LiteralPath (Join-Path $resolvedEvidence '50_sanitization.json')
if ($scanFailed) { throw 'sensitive material scan failed; inspect locally without copying matches' }

if (Test-Path -LiteralPath 'uv.lock') {
    Copy-Item -LiteralPath 'uv.lock' -Destination (Join-Path $resolvedEvidence 'uv.lock')
    (Get-FileHash -Algorithm SHA256 -LiteralPath 'uv.lock').Hash.ToLower() |
        Set-Content -Encoding UTF8 -LiteralPath (Join-Path $resolvedEvidence '60_uv_lock_sha256.txt')
}

$statusAfter = @(git status --porcelain=v1)
$statusAfter | Set-Content -Encoding UTF8 `
    -LiteralPath (Join-Path $resolvedEvidence '70_git_status_after.txt')
$unexpectedStatus = @($statusAfter | Where-Object { $_ -ne '?? uv.lock' })
if ($unexpectedStatus.Count -ne 0) {
    throw 'test run changed tracked files or created unexpected untracked files'
}

Get-ChildItem -File -Recurse -LiteralPath $resolvedEvidence | ForEach-Object {
    [ordered]@{
        path = $_.FullName.Substring($resolvedEvidence.Length + 1).Replace('\', '/')
        sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash.ToLower()
    }
} | ConvertTo-Json -Depth 3 |
    Set-Content -Encoding UTF8 -LiteralPath (Join-Path $resolvedEvidence '99_evidence_sha256.json')

Write-Output "Evidence collected at $resolvedEvidence"
