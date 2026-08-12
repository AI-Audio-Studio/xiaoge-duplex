# JOINT-01 当前 Web-only 范围签收边界说明

日期：2026-08-11  
目标包：`cloud_g3_review_v4_correction_candidate`  
目标环境：`60.205.197.165:10097`

## 0. 结论

当前 G3 V4 候选包只有 Web 端测试条件；本文件确认：

- 可接受 Web 端 + 10097 smoke 作为 G3 云侧技术整改候选包的协议行为证据。
- 不把 Web-only 测试冒充为真实机器人端侧联合验收。
- 不授权生产上线、灰度发布或真实机器人动作。
- `ROBOT_ACTION_ENABLED=0` 仍为真实动作 gate。

## 1. 当前可由 Web 端论证的事项

| 能力 | 证据 | 结论 |
| --- | --- | --- |
| Demo 不携带默认 API Key | `10097_SMOKE_SUMMARY_FINAL_20260810T1155Z.json` `demo_requires_user_key` | PASS |
| 正式 `/ws/session` Bearer-only | query-only 和 Authorization+query 均 `4401`，Bearer 正例通过 | PASS |
| debug query route 默认关闭 | `debug_query_route_disabled` 返回 404 | PASS |
| access token 历史失效 | `10097_CREDENTIAL_ROTATION_DIAGNOSTIC_20260810.md` 610 秒后 Bearer 返回 `4401` | PASS |
| 单命令云侧 dry-run | `single_command_dry_run` 返回 `data.cmd action=navigation.move` | PASS |
| fake ack/running/succeeded | `fake_ack_running_succeeded` | PASS（云侧/fake） |
| unknown cmd_id | `unknown_cmd_id` 返回 `data.error code=unknown_cmd_id` | PASS |
| 多命令 ask_split | `multi_command_ask_split` 返回 reply 且无 `cmd_id` | PASS |
| caps 不扩大 | `hello_caps_cannot_escalate` | PASS |
| 真实动作 gate | `10097_V4_DEPLOYMENT_AND_ACCEPTANCE_20260810.md` 记录 `ROBOT_ACTION_ENABLED=0` | PASS |

## 2. 当前不能由 Web 端单独论证的事项

| 事项 | 当前处理 |
| --- | --- |
| 真实机器人端侧收到 `data.cmd` | 未覆盖；当前仅 Web/gateway/poolmgr/agent/fake executor 范围 |
| 真实端侧 ack/running/succeeded/result | 未覆盖；当前 fake ack/running/succeeded 只证明云侧协议处理能力 |
| 真实动作执行安全 | 未覆盖；真实动作 gate 保持关闭 |
| 生产 TLS 可信 CA 链 | 未覆盖；当前为 G3 内部 certificate SHA-256 pin |

## 3. Owner 范围声明

当前 owner 声明：G3 V4 当前只有 Web 端测试条件，接受 Web 端 + 10097 smoke 作为“云侧技术整改候选包”的验收依据；最终端云联合签收如需覆盖真实端侧设备，应在端侧环境可用后另行采证。

| 签收方 | 责任人 | 结论 | 时间（UTC） | 备注/例外批准 |
| --- | --- | --- | --- | --- |
| 云侧 owner | allen.wangmh | PASS（Web-only/G3 candidate） | 2026-08-11 | 仅覆盖云侧 + Web 端 + fake executor 技术整改候选包 |
| 端侧 owner | N/A（当前无真实端侧） | N/A（Web-only 例外） | 2026-08-11 | 不冒充真实端侧联合验收 |
| 测试 owner | allen.wangmh | PASS（Web-only/G3 candidate） | 2026-08-11 | 依据 10097 smoke、126 tests、Ruff、日志扫描 |
| 安全 owner | allen.wangmh | PASS（G3 candidate） | 2026-08-11 | 旧 API Key 和历史 token 回执另见 SEC-01/SEC-02B |

## 4. 可关闭与不可关闭口径

可关闭为 `PASS（Web-only/G3 candidate）`：

- Web 端 Demo 安全准入。
- Bearer-only 正式通道。
- query token 正式路径拒绝。
- 云侧 dry-run `data.cmd` 生成。
- fake executor ack/running/succeeded 云侧协议处理。
- caps 不扩大。
- 运行日志敏感值扫描。

仍不能声明：

- 真实端侧设备联合验收完成。
- 生产发布完成。
- 真实机器人动作放行。
- 生产 TLS 可信 CA 链验收完成。

最终结论：`G3_CLOUD_WEB_ONLY_CANDIDATE_APPROVED`
