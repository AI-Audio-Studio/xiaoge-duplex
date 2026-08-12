# 小歌全双工 R5.2.2 G1/G2 端云执行指南

日期：2026-08-03

适用对象：端侧工程师、云端工程师、协议裁决人、评审组。

本文说明 R5.2.2 交付包如何使用、端云分别要提交什么、G1/G2 如何界定、什么时候放行，以及之后如何同步开发和调试。

## 1. 交付包定位

当前交付包：

`g1_contract_signoff_package_r21_r5_2_2_20260804`

包的定位是：**G1 契约签收确认包**。

R21 不修改 R5.2.2 合同字段；它在 R20 合同基线上加入协议裁决意见和端云二次签收模板。

它不是：

- G2 mock/契约测试编码放行包。
- SDK/Gateway/Agent 真实实现编码放行包。
- 真实工程代码变更包。

端云工程师拿到本包后，第一件事不是改代码，而是确认同一份协议契约是否可接受，并完成三方签收。

本包三方 owner 已预填：

| 角色 | 负责人 | 责任范围 |
| --- | --- | --- |
| Clients owner | 童紫薇 | SDK、GUI、fake SDK、fake executor。 |
| Cloud owner | 王明辉 | Gateway/Auth、sessproto、Agent、voice-cmd、fake server、cloud replay。 |
| Protocol arbiter | 陈强 | manifest hash、字段、枚举、错误码、close code、no-legacy 裁决。 |

## 2. 工程师如何使用本包

1. 先阅读 `README_HANDOFF.md`。
2. 再阅读 `G1_SIGNOFF_FAST_CONFIRM_GUIDE_20260804.md` 和 `05_signoff/PROTOCOL_ARBITER_DECISION_20260804.md`，按自己的角色确认检查项。
3. 在包根目录执行：

```powershell
.\VERIFY_PACKAGE.ps1
```

期望输出：

```text
PACKAGE_FILE_LIST_HASH_CHECK: PASS
CONTRACT_MANIFEST_HASH_CHECK: PASS
SOURCE_RECONCILIATION: PASS
CLOSE_CODE_CASES: 11
PACKAGE_STATUS: G1_SIGNOFF_CONFIRMATION_PACKAGE_NOT_IMPLEMENTATION_RELEASE
```

4. 如果校验失败，先不要签收，也不要基于该包继续评估实现；把失败输出返回评审组。
5. 校验通过后，以以下文件作为契约基准：

`02_contracts/xiaoge-duplex-protocol-r5.2.2.manifest.json`

所有端云确认、后续 mock、契约测试和问题讨论，都必须引用同一个 manifest hash。

## 3. G1 怎么界定

G1 是“接口和契约冻结”，不是编码。

G1 通过条件：

| 条件 | 必须满足 |
| --- | --- |
| 包完整性 | `VERIFY_PACKAGE.ps1` 全部 PASS。 |
| 三方签收 | Clients owner、Cloud owner、Protocol arbiter 都确认同一个 manifest hash。 |
| 字段冻结 | schema、examples、close-code、source-check、workbook、设计文档之间无冲突。 |
| no-legacy | 三方确认 R5.2.2 不兼容此前 clients 协议，不再要求旧 `/ws/audio`、裸 cmd、历史批量字段。 |
| caps | 三方确认 `caps/granted_caps=audio/text/cmd/state`，unknown/duplicate caps 为负例。 |
| 多命令 | 三方确认 `data.reply.multi_command_blocked.ask_split` 可回放，P0 只做 `multi_command_blocked + ask_split`，不生成 `data.cmd/cmd_id/端侧执行副作用`；P1 再设计执行。 |
| registry 边界 | 三方确认 registry schema 是语音意图 seed/registry；端侧可执行只认 `delivery=data.cmd` 或 `data.cmd after confirmation`。 |
| registry 中间 delivery | 三方确认裁决：`ctrl.set/config API` 为 P1/配置链路预留；`data.cmd or ctrl.set by owner` 按具体 action 拆 owner，未冻结为 `data.cmd` 前不进入 G2 正向执行；fake executor 忽略非 `x-client-executable-deliveries`。 |
| G2 语义断言 | 三方确认后续 G2 replayer 必须读取 `payload + context + expect`；不能只用 payload schema 判断多命令样例是否通过。 |
| JSON 8KB | 三方确认 G2 必测 8192 pass、8193 fail。 |

G1 未通过前：

- 不改 SDK/Gateway/Agent/sessproto/voice-cmd 真实实现。
- 不写真实联调代码。
- 不新增与合同不一致的私有字段。

## 4. 端侧工程师要提交什么

端侧 owner：童紫薇。

端侧 owner 范围：

- SDK。
- GUI。
- fake SDK。
- fake executor。

端侧需要提交：

| 提交物 | 文件/形式 | 要求 |
| --- | --- | --- |
| Clients owner 签收 | `05_signoff/xiaoge-duplex-protocol-r5.2.2.signoff.md` | 确认 Clients owner 行为童紫薇，并填写是否签收、日期、manifest hash。 |
| Clients owner 检查项 | `05_signoff/OWNER_SIGNOFF_ACTIONS.md` | 填写 Clients owner 列。 |
| 端侧确认说明 | Markdown 或评审回复 | 说明端侧是否接受契约；如不接受，列出具体字段、枚举、样例或 close code。 |

端侧确认说明至少包含：

1. SDK/GUI 需要发送哪些消息、接收哪些消息。
2. 对 `ctrl.hello`、`ctrl.state`、`ctrl.frontend_state`、`data.cmd`、`data.cmd_ack`、`data.cmd_result`、`data.error` 是否接受。
3. 对 `caps/granted_caps` 四值枚举是否接受。
4. 对 `examples.jsonl` 中端侧相关样例是否接受。
5. 对 `close-codes.jsonl` 中端侧需要处理的 HTTP/WSS close/data.error 行为是否接受。
6. 对 no-legacy 决策是否接受：端侧不要求本版本支持旧 `/ws/audio`、裸 cmd 或历史批量字段。
7. 对 registry 可执行边界是否接受：只消费 `delivery=data.cmd` 或 `data.cmd after confirmation`。
8. G2 阶段 fake SDK / fake executor 的实现边界和负责人。
9. 对 registry 中间 delivery 裁决是否接受：`ctrl.set/config API` 为 P1/配置链路预留；`data.cmd or ctrl.set by owner` 未冻结为 `data.cmd` 前不进入 G2 正向执行。
10. 对多命令 ask_split 是否接受：只展示/播报回复，不触发端侧执行副作用。

## 5. 云端工程师要提交什么

云端 owner：王明辉。

云端 owner 范围：

- Gateway/Auth。
- sessproto/Agent。
- voice-cmd。
- fake server。
- cloud replay。

云端需要提交：

| 提交物 | 文件/形式 | 要求 |
| --- | --- | --- |
| Cloud owner 签收 | `05_signoff/xiaoge-duplex-protocol-r5.2.2.signoff.md` | 确认 Cloud owner 行为王明辉，并填写是否签收、日期、manifest hash。 |
| Cloud owner 检查项 | `05_signoff/OWNER_SIGNOFF_ACTIONS.md` | 填写 Cloud owner 列。 |
| 云端确认说明 | Markdown 或评审回复 | 说明云端是否接受契约；如不接受，列出具体字段、枚举、样例或 close code。 |

云端确认说明至少包含：

1. Gateway/Auth 对 `create_session`、WSS bearer、token TTL/scope、busy/resource_exhausted 的接受情况。
2. sessproto/Agent 对 JSON dispatcher、frame size、`ctrl.state`、`ctrl.frontend_state`、`data.*` 的接受情况。
3. voice-cmd 对 P0 单命令、`multi_command_blocked`、高危确认、no replay、`data.cmd_ack/result` 的接受情况。
4. 对 P0 registry 参数类型仅 `enum/int` 的接受情况。
5. 对 no-legacy 决策的接受情况。
6. 对 `data.reply.multi_command_blocked.ask_split` 只返回 reply、不发 cmd 的机器合同接受情况。
7. G2 阶段 fake server / cloud replay 的实现边界和负责人。
8. 对 G2 replayer 读取 `payload + context + expect` 的接受情况，尤其是 `context.intent_type=control_cmd_multi` 和 `expect.forbidden_types=["data.cmd"]`。
9. 对 registry 中间 delivery 裁决的接受情况；无法接受时应列出具体 action、期望 delivery 和是否需要重新生成 manifest。

## 6. 协议裁决人要提交什么

协议裁决人：陈强。

协议裁决人负责：

- 字段、枚举、错误码、close code。
- manifest hash。
- no-legacy 边界。
- G1/G2 放行解释。

协议裁决人需要提交：

| 提交物 | 文件/形式 | 要求 |
| --- | --- | --- |
| Protocol arbiter 签收 | `05_signoff/xiaoge-duplex-protocol-r5.2.2.signoff.md` | 确认 Protocol arbiter 行为陈强，并填写是否签收、日期、manifest hash。 |
| 裁决说明 | Markdown 或评审回复 | 明确合同版本、hash、未放行内容和后续变更规则。 |

必须裁决清楚：

1. `caps/granted_caps` 是否仅允许 `audio/text/cmd/state`。
2. R5.2.2 close-code 合同是否不含旧 4001。
3. JSON 8192/8193 边界如何测。
4. P0 多命令是否只阻断不执行。
5. `ctrl.status/get/set/ack` 是否仍为 P1/非本轮 P0。
6. P0 registry 参数类型是否仅 `enum/int`。
7. registry schema 的端侧可执行 delivery 边界是否足够明确。
8. registry 中间 delivery 裁决是否接受并冻结。
9. G2 replayer 必须读取 `payload + context + expect` 是否作为后续验收要求冻结。

## 7. G2 主要做什么

G2 是“mock/契约测试准备”，目标是让端云在不改真实实现的前提下，独立验证同一份合同可跑通。

G2 允许做：

- fake server。
- fake SDK。
- fake executor。
- cloud replay。
- JSON schema 校验脚本。
- examples replay 脚本。
- 8192/8193 JSON 边界测试。
- 多命令阻断测试。
- caps 正负例测试。

G2 不允许做：

- 改 SDK/Gateway/Agent/sessproto/voice-cmd 真实实现。
- 真机动作执行。
- 真实云部署上线。
- 绕过 manifest 增私有字段。

## 8. G2 怎么算通过

G2 通过条件：

| 条件 | 必须满足 |
| --- | --- |
| fake 双端可跑 | 端侧 fake SDK/fake executor 和云端 fake server/cloud replay 均能独立跑 examples。 |
| 正例通过 | `examples.jsonl` 中所有 positive examples 被双方接受。 |
| 负例失败正确 | schema fail、semantic fail、transport fail 均按合同失败，不静默吞掉。 |
| caps | `audio/text/cmd/state` 正例通过；unknown/duplicate caps 负例失败。 |
| JSON 边界 | 8192 bytes 通过；8193 bytes 拒绝。 |
| 多命令 | `data.reply.multi_command_blocked.ask_split` 用例不生成 `data.cmd/cmd_id/端侧执行副作用`，只返回 ask_split `data.reply`。 |
| 语义断言 | G2 replayer 同时读取 `payload + context + expect`；能识别 `context.intent_type=control_cmd_multi` 与禁止 `data.cmd` 的期望。 |
| ack/result | accepted/rejected/duplicate、running/succeeded/failed/canceled/timeout、unknown_cmd_id 都可回放。 |
| 报告 | 端云各自产出 mock 运行日志、失败样例、版本 hash 和负责人签名。 |

G2 通过后，才可以申请进入真实实现拆分；真实实现仍需按 G3 契约测试和负责人批准推进。

## 9. G2 后如何同步开发和调试

建议节奏：

1. 端云同时基于同一 manifest hash 创建各自开发分支。
2. 云端先落 `create_session`、WSS bearer、JSON dispatcher、schema 校验和 fake voice-cmd 回放。
3. 端侧先落 SDK 连接、caps hello、frame 分发、cmd_ack/result、fake executor。
4. 每天同步一次 examples replay 结果，失败必须带 `trace_id/session_id/utterance_id/cmd_id`。
5. 第一次联调用 fake server + fake SDK，不接真实机器人动作。
6. 第二次联调用真实 Gateway/Agent + fake executor。
7. 第三次联调用真实 SDK + fake/cloud command，仍不触发高危真动作。
8. 真机动作只在 G3 契约测试通过并经负责人确认后进入。

同步时固定四类材料：

- 当前 manifest hash。
- 已通过/失败 examples 列表。
- 端云日志片段。
- 新发现的字段分歧或需求分歧。

任何字段变化都必须回到 G1 重新生成 manifest，不允许在实现里临时协商。
