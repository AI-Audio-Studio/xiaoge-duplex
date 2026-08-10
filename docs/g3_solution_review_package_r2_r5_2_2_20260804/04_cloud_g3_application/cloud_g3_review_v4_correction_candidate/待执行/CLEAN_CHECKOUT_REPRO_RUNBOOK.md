# Clean Checkout 复现与采证操作单

责任人：Release owner + Test owner。必须在包含本次整改的不可变 commit 产生后执行。

## 前置

- 不在当前脏工作区执行。
- 目标 commit 已推送到评审员可读取的仓库。
- 证据目录位于 clone 目录之外。
- 不导入生产 API Key；本轮 pytest 使用 fake credential。

## PowerShell 流程

```powershell
$commit = '<40位commit>'
$source = '<仓库URL或本机裸仓库路径>'
$checkout = Join-Path $env:TEMP "xiaoge-g3-$($commit.Substring(0,12))"
$evidence = Join-Path (Split-Path $checkout) "g3-clean-evidence-$($commit.Substring(0,12))"

git clone --no-local $source $checkout
Set-Location $checkout
git checkout --detach $commit
New-Item -ItemType Directory -Force -Path $evidence | Out-Null
git status --porcelain=v1 | Tee-Object "$evidence/00_git_status_before.txt"
if ((Get-Content "$evidence/00_git_status_before.txt").Length -ne 0) { throw 'checkout is not clean' }

$collector = Join-Path $checkout `
  'docs/g3_solution_review_package_r2_r5_2_2_20260804/04_cloud_g3_application/cloud_g3_review_v4_correction_candidate/工具/collect_clean_signoff_evidence.ps1'
& $collector -ExpectedCommit $commit -EvidenceDir $evidence
```

## 验收

- `00_git_status_before.txt` 为空。
- `01_commit.txt` 与 release 模板中的 40 位 SHA 一致。
- `10_uv_sync.log`、`20_pytest.log`、`30_ruff.log` 的 exit code 均为 0。
- `40_source_sha256.json` 与包内 source snapshot manifest 可解释地一致。
- `50_sanitization.json` 所有计数为 0。
- 若 `uv sync` 生成未跟踪 `uv.lock`，归档其 SHA-256 和完整文件；不得手工补包。
- 整个 evidence 目录做一次 SHA-256 manifest，并由 Test owner 签字。

完成后把脱敏 evidence 目录复制到本包 `证据/签收/clean_<commit12>_<utc>/`，再更新
`SIGNOFF_EVIDENCE_INDEX.md` 的 `REP-01`。
