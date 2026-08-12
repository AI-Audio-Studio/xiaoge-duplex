$ErrorActionPreference = "Stop"

$packageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$fileListPath = Join-Path $packageRoot "PACKAGE_FILE_LIST.sha256"
$contractManifestPath = Join-Path $packageRoot "02_contracts\xiaoge-duplex-protocol-r5.2.2.manifest.json"

function Assert-FileHash {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][string]$ExpectedHash,
    [Parameter(Mandatory = $true)][string]$Label
  )

  if (-not (Test-Path -LiteralPath $Path)) {
    throw "Missing file: $Label ($Path)"
  }

  $actualHash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToUpperInvariant()
  if ($actualHash -ne $ExpectedHash.ToUpperInvariant()) {
    throw "Hash mismatch: $Label expected=$ExpectedHash actual=$actualHash"
  }
}

if (-not (Test-Path -LiteralPath $fileListPath)) {
  throw "Missing PACKAGE_FILE_LIST.sha256"
}

foreach ($line in Get-Content -LiteralPath $fileListPath -Encoding UTF8) {
  if ([string]::IsNullOrWhiteSpace($line)) {
    continue
  }

  $parts = $line -split "\s+", 2
  if ($parts.Count -ne 2) {
    throw "Invalid PACKAGE_FILE_LIST.sha256 line: $line"
  }

  $relativePath = $parts[1].Replace("/", [IO.Path]::DirectorySeparatorChar)
  Assert-FileHash `
    -Path (Join-Path $packageRoot $relativePath) `
    -ExpectedHash $parts[0] `
    -Label $parts[1]
}

$manifest = Get-Content -LiteralPath $contractManifestPath -Encoding UTF8 -Raw | ConvertFrom-Json

$contractGeneratedMappings = @{
  "protocol_schema" = "02_contracts\xiaoge-duplex-protocol-r5.2.2.schema.json"
  "examples_jsonl" = "02_contracts\xiaoge-duplex-protocol-r5.2.2.examples.jsonl"
  "close_code_cases_jsonl" = "02_contracts\xiaoge-duplex-protocol-r5.2.2.close-codes.jsonl"
  "voicecmd_registry_schema" = "02_contracts\xiaoge-duplex-voicecmd-registry-r5.2.2.schema.json"
  "source_reconciliation_report" = "02_contracts\xiaoge-duplex-protocol-r5.2.2.source-check.json"
  "signoff" = "02_contracts\xiaoge-duplex-protocol-r5.2.2.signoff.md"
}

foreach ($property in $manifest.generated_files.PSObject.Properties) {
  if (-not $contractGeneratedMappings.ContainsKey($property.Name)) {
    throw "Unknown generated file in contract manifest: $($property.Name)"
  }
  Assert-FileHash `
    -Path (Join-Path $packageRoot $contractGeneratedMappings[$property.Name]) `
    -ExpectedHash $property.Value.sha256 `
    -Label $property.Name
}

$sourceMappings = @{
  "workbook" = "01_workbook\xiaoge_full_duplex_requirements_design_20260731_r5_2_2_review.xlsx"
  "workbook_inspect" = "01_workbook\xiaoge_full_duplex_requirements_design_20260731_r5_2_2_review.xlsx.inspect.ndjson"
  "protocol_v2" = "03_design_docs\PROTOCOL_V2_DESIGN.md"
  "voice_cmd" = "03_design_docs\VOICE_CMD_DESIGN.md"
  "generator" = "02_contracts\build_contracts.mjs"
}

foreach ($property in $manifest.sources.PSObject.Properties) {
  if (-not $sourceMappings.ContainsKey($property.Name)) {
    throw "Unknown source file in contract manifest: $($property.Name)"
  }
  Assert-FileHash `
    -Path (Join-Path $packageRoot $sourceMappings[$property.Name]) `
    -ExpectedHash $property.Value.sha256 `
    -Label $property.Name
}

if ($manifest.validation.result -ne "PASS") {
  throw "Contract manifest validation is not PASS"
}

if ($manifest.validation.source_reconciliation.result -ne "PASS") {
  throw "Source reconciliation is not PASS"
}

Write-Output "PACKAGE_FILE_LIST_HASH_CHECK: PASS"
Write-Output "CONTRACT_MANIFEST_HASH_CHECK: PASS"
Write-Output "SOURCE_RECONCILIATION: $($manifest.validation.source_reconciliation.result)"
Write-Output "CLOSE_CODE_CASES: $($manifest.validation.source_reconciliation.close_code_cases_checked)"
Write-Output "PACKAGE_STATUS: G1_SIGNOFF_CONFIRMATION_PACKAGE_NOT_IMPLEMENTATION_RELEASE"
