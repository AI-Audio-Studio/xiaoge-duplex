# G3 云侧 V4 候选包脱敏检查

日期：2026-08-10

最近复跑：2026-08-10T11:25:59Z（加入 10097 凭据轮换诊断和证书 pin 工具后）

扫描范围：`cloud_g3_review_v4_correction_candidate/` 全部文件，包括 `代码/source/`。

## 结果

| 模式 | 命中数 |
| --- | ---: |
| PEM/OpenSSH private key header | 0 |
| JWT 三段长 token | 0 |
| `sk-` 长凭据形态 | 0 |
| 长字面量 `Bearer` token | 0 |
| `access_token=tok-...` / `token=tok-...` query 值 | 0 |
| Demo 默认 key、API Key URL/localStorage 持久化标记 | 0 |

扫描只证明当前候选包没有上述完整凭据形态。新 Key 准入、缺失/错误 Key 拒绝结果见
`签收/10097_CREDENTIAL_ROTATION_DIAGNOSTIC_20260810.md`；旧 Key 平台作废和历史 token
失效仍须对应 owner 签字。
