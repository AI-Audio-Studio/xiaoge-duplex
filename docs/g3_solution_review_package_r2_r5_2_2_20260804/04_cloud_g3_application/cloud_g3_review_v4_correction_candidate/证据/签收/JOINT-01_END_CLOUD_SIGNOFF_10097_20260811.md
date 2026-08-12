# JOINT-01 端云联合签收回执

日期：2026-08-11  
目标包：`cloud_g3_review_v4_correction_candidate`  
目标环境：`60.205.197.165:10097`  
源码锚点：`0710254d11d0ee84b0ab09e46d644fd283752461`

## 0. 签收结论

端云 owner 确认：当前 G3 V4 候选包已完成测试阶段端云联合签收，可作为 `JOINT-01` 的关闭证据。

当前处于测试阶段，本次签收只覆盖 G3 云侧 Web-only 技术整改候选包评审、10097 测试环境、Web 端入口与端云协议闭环：不申请正式生产上线，不申请生产 TLS 验收，不授权真实机器人动作。

## 1. 联合签收范围

| 范围 | 结论 | 说明 |
| --- | --- | --- |
| 云侧 `data.cmd` 下发 | PASS | 10097 smoke 已见证 `data.cmd action=navigation.move` |
| 端侧协议接收/回执链路 | PASS | 端云 owner 确认当前测试阶段端侧协议链路已完成联合签收 |
| ack/running/succeeded 生命周期 | PASS | 10097 smoke 已见证 fake executor ack/running/succeeded；owner 接受其作为当前测试阶段端云协议闭环证据 |
| unknown/duplicate/late/timeout 边界 | PASS | 本地/远端 126 tests 覆盖 lifecycle 边界，unknown 运行态 smoke 通过 |
| 真实动作 gate | PASS | `ROBOT_ACTION_ENABLED=0` 保持关闭 |
| 生产 TLS | N/A | 当前仅 G3 内部 certificate SHA-256 pin；生产 TLS 另行验收 |

## 2. 关键证据引用

| 证据 | 路径/结果 |
| --- | --- |
| 最终 10097 smoke | `证据/签收/10097_SMOKE_SUMMARY_FINAL_20260810T1155Z.json`，`overall=PASS`，12/12 PASS |
| 单命令 dry-run | `single_command_dry_run`：`type=data.cmd action=navigation.move` |
| ack/running/succeeded | `fake_ack_running_succeeded`：`frames_sent=true` |
| unknown cmd_id | `unknown_cmd_id`：`type=data.error code=unknown_cmd_id` |
| 远端回归 | `证据/签收/10097_V4_DEPLOYMENT_AND_ACCEPTANCE_20260810.md`：126 tests PASS，Ruff PASS |
| 动作 gate | `ROBOT_ACTION_ENABLED=0` |
| TLS 模式 | certificate SHA-256 pin：`460e09d5d59b91df...` |

## 3. Owner 签收表

| 签收方 | 责任人 | 结论 | 时间（UTC） | 备注/例外批准 |
| --- | --- | --- | --- | --- |
| 云侧 owner | allen.wangmh | PASS | 2026-08-11 | 10097 smoke、远端 126 tests、Ruff 和日志扫描通过 |
| 端侧 owner | allen.wangmh | PASS（测试阶段端云协议签收） | 2026-08-11 | 确认当前测试阶段端云联合签收已完成；不授权真实机器人动作 |
| 测试 owner | allen.wangmh | PASS | 2026-08-11 | 接受当前 G3 测试阶段端云协议闭环证据 |
| Release owner | allen.wangmh | PASS（source traceability only） | 2026-08-11 | 源码锚点已确认；`REP-01 clean checkout` 单独 pending |
| Security owner | allen.wangmh | PASS | 2026-08-11 | 旧 API Key 平台侧作废，历史 token 已失效 |

## 4. 不扩大声明

本回执关闭当前 G3 V4 测试阶段 `JOINT-01` 端云联合签收，不表示：

- 最终 `SIGNOFF` 已完成。
- `REP-01 clean checkout` 已完成。
- 生产上线、灰度发布或合入 `main` 已获批准。
- 真实机器人动作已放行。
- 生产 TLS 可信 CA/hostname 验收已完成。

最终结论：`JOINT-01_END_CLOUD_TEST_STAGE_APPROVED`
