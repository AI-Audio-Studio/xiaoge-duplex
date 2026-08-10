# G3 云侧正式签收证据索引

状态值只允许：`PASS`、`FAIL`、`PENDING`、`N/A（附批准人）`。

| Evidence ID | 对应闸口 | 当前状态 | 文件/来源 | 责任人 |
| --- | --- | --- | --- | --- |
| SEC-01 | P0-01 key 轮换/作废 | PENDING | `证据/签收/10097_CREDENTIAL_ROTATION_DIAGNOSTIC_20260810.md`；待 Security owner 签字 | Security owner |
| SEC-02A | P0-01 历史 token 技术失效 | PASS | `证据/签收/10097_CREDENTIAL_ROTATION_DIAGNOSTIC_20260810.md`：610 秒后 4401 | Cloud/test owner |
| SEC-02B | P0-01 历史 token owner 回执 | PENDING | `待签字/DEPLOYMENT_OWNER_TOKEN_INVALIDATION_TEMPLATE.md` | Identity/deployment owner |
| SEC-03 | 包脱敏 | PASS | `证据/G3_CLOUD_V4_SANITIZATION_CHECK_20260810.md` | Cloud owner |
| AUTH-01 | 正式 Bearer-only | PASS | `证据/签收/10097_SMOKE_SUMMARY_FINAL_20260810T1155Z.json` | Cloud owner |
| AUTH-02 | access log 不含 query | PASS | `证据/签收/10097_V4_DEPLOYMENT_AND_ACCEPTANCE_20260810.md`：14 文件 0 命中 | Cloud owner |
| SRC-01 | 完整源码快照 | PASS | `代码/source/`、`代码/SOURCE_SNAPSHOT_MANIFEST_20260810.md` | Cloud owner |
| SRC-02 | 不可变 commit | PASS（技术证据） | `0710254d11d0ee84b0ab09e46d644fd283752461`；`待签字/RELEASE_OWNER_SOURCE_CONFIRMATION_TEMPLATE.md` | Release owner 待签字 |
| REP-01 | clean checkout | PENDING | `待执行/CLEAN_CHECKOUT_REPRO_RUNBOOK.md` 产物 | Release/test owner |
| DEP-01 | 10097 部署快照/哈希 | PASS | `证据/签收/10097_V4_DEPLOYMENT_AND_ACCEPTANCE_20260810.md` | Deployment owner |
| WS-01 | 10097 Bearer/query 矩阵 | PASS | 最终 smoke 12/12 PASS | Cloud/test owner |
| TLS-01 | HTTPS/WSS 证书验证 | PASS | SHA-256 pin；最终 smoke 报告记录验证模式 | Deployment/security owner |
| WS-02 | data.cmd/ack/result | PASS | 10097 smoke + 远端 126 tests | Cloud/端侧 |
| WS-03 | ask_split reply-only | PASS | 10097 smoke + 远端 126 tests | Cloud/端侧 |
| LIFE-01 | cmd lifecycle | PASS（云侧） | 本地/远端 126 tests；unknown 运行态通过 | Cloud owner |
| CAPS-01 | caps 不扩大 | PASS | 10097 smoke `hello_caps_cannot_escalate` | Cloud owner |
| X3-01 | 四 action 独立正例 | PASS | `tests/test_ours_g3_x3_skill_commands.py` 快照 | Cloud owner |
| JOINT-01 | 端云共同签收 | PENDING | `待签字/END_CLOUD_JOINT_SIGNOFF_TEMPLATE.md` | 四方 owner |

## 封包规则

- 每个 PENDING 项完成后，把原始、脱敏后的证据放到 `证据/签收/`，不要覆盖模板。
- 日志文件名必须包含 UTC 时间、commit 前 12 位和环境名。
- 日志内不得出现 API Key、access token、Authorization header 值或 query token。
- 更新本索引状态，并在文件路径列填写实际相对路径。
- 只有所有 P0/P1 为 PASS 后才能生成 `cloud_g3_review_v4_signoff_candidate`。
