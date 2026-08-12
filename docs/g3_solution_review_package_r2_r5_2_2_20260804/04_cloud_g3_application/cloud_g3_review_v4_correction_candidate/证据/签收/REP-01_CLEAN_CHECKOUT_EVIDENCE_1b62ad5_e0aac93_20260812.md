# REP-01 clean checkout 复现证据

采证时间：2026-08-12T05:52:34Z  
采证环境：`60.205.197.165`，`/data/home/allen.wangmh/software/xiaoge/xiaoge-duplex-commit-test`  
证据目录：`/data/home/allen.wangmh/software/xiaoge/g3_rep01_evidence_e0aac93`

本文件只记录 clean checkout 复现摘要、文件哈希和状态，不记录 API Key、access token、Authorization 值、带 token 的 URL 或服务器口令。

## 不可变提交

| 字段 | 值 |
| --- | --- |
| clean checkout commit | `e0aac93802afc017eaefc01adf5290ab7d44cdf9` |
| 分支 | `g3-cloud-v4-signoff-20260810` |
| bundle 来源 | 本地分支 bundle `/tmp/xiaoge-duplex-g3-cloud-v4-signoff-20260810.bundle` |
| checkout 模式 | detached HEAD |
| checkout 前状态 | clean |
| checkout 后状态 | clean（`70_git_status_after.txt` 为空） |

`e0aac93802afc017eaefc01adf5290ab7d44cdf9` 在 `35734e9ff15b9b5c0fc5536240ae7c5b3dcf9722` 的候选包整改基础上补充了测试必需的 G1 R5.2.2 合同包，并在 `1b62ad503810c73cc331740def2cf5b3264deac7` 基础上修正 REP-01 采证工具对 Python 生成产物的清理逻辑。该提交不改变 V4 运行态 Bearer-only/WS/cmd 主逻辑。

## 工具链

| 字段 | 值 |
| --- | --- |
| Python | `Python 3.10.12` |
| uv | `uv 0.11.28 (x86_64-unknown-linux-gnu)` |
| `uv.lock` SHA-256 | `bfba9a46a771882fc14f0e365c9272279b795114c0d5f6c0c1f4de7d8bf73da6` |

## 复现结果

| 检查 | 结果 | 原始证据文件 |
| --- | --- | --- |
| `uv sync --all-extras --dev` | PASS | `10_uv_sync.log` |
| 8 个指定测试文件 | PASS，`126 passed in 2.14s` | `20_pytest.log` |
| Ruff 指定目标 | PASS，`All checks passed!` | `30_ruff.log` |
| source snapshot SHA-256 | PASS，30 个文件 | `40_source_sha256.json` |
| 凭据脱敏扫描 | PASS，全部 0 命中 | `50_sanitization.json` |
| 生成产物扫描 | PASS，`generated_artifacts=0` | `50_sanitization.json` |
| clean checkout 状态 | PASS，空输出 | `70_git_status_after.txt` |
| evidence manifest | PASS，10 个文件 | `99_evidence_sha256.json` |

## 脱敏与生成产物扫描摘要

`50_sanitization.json` 结果：

```json
{
  "private_key": 0,
  "jwt": 0,
  "openai_style": 0,
  "literal_bearer": 0,
  "query_token": 0,
  "removed_demo_default": 0,
  "api_key_persistence": 0,
  "generated_artifacts": 0
}
```

## 证据 manifest 锚点

| 文件 | SHA-256 |
| --- | --- |
| `99_evidence_sha256.json` | `98a0af4f856b01666e7cfd1b149b50ec9843ec12e478e433e13c8211daa85970` |

最终结论：`REP-01_CLEAN_CHECKOUT_PASS`。

本回执关闭 `REP-01 clean checkout` 复现闸口；不扩大为正式生产上线、真实机器人动作授权或生产 TLS 验收。
