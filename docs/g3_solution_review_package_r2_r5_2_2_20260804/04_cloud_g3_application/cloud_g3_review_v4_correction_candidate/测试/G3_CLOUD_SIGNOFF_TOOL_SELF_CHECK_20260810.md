# G3 正式签收采证工具自检

日期：2026-08-10

| 检查 | 结果 |
| --- | --- |
| PowerShell AST parser | PASS |
| Python bytecode compile | PASS |
| Smoke tool Ruff | PASS |
| Smoke tool `--help` | PASS |
| Smoke tool 64 位证书 SHA-256 pin 参数 | PASS |
| Collector 脏工作区拒绝门禁 | PASS |
| 候选包 private key/JWT/`sk-`/literal Bearer 扫描 | 0 命中 |

脏工作区门禁自检只验证脚本会拒绝当前 working tree，没有执行依赖装配或写出 evidence。
10097 凭据准入和 token TTL 已从开发工作站执行，结果见
`证据/签收/10097_CREDENTIAL_ROTATION_DIAGNOSTIC_20260810.md`。完整 V4 smoke 必须在 deployment
owner 部署当前快照后执行；不得用旧版现网诊断替代。
