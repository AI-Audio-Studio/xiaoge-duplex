# R5.2.2 Owner 签收动作清单

日期：2026-08-03

用途：G1 签收前，端侧 owner、云端 owner、协议裁决人逐项确认。签收基准为：

`02_contracts/xiaoge-duplex-protocol-r5.2.2.manifest.json`

## 0. 责任人

| 角色 | 负责人 | 责任范围 |
| --- | --- | --- |
| Clients owner | 童紫薇 | clients SDK、GUI、fake SDK、fake executor。 |
| Cloud owner | 王明辉 | Gateway/Auth、sessproto、Agent、voice-cmd、fake server、cloud replay。 |
| Protocol arbiter | 陈强 | manifest hash、字段、枚举、错误码、close code、no-legacy 争议裁决。 |

下列表格中的 `Clients owner`、`Cloud owner`、`Protocol arbiter` 分别对应童紫薇、王明辉、陈强。三方签收前不得进入 G2 mock 或真实实现编码。

## 1. 通用检查项

| 检查项 | Clients owner | Cloud owner | Protocol arbiter | 结论/备注 |
| --- | --- | --- | --- | --- |
| 已运行 `VERIFY_PACKAGE.ps1` 且全部 PASS |  | 已确认 |  |  |
| 已确认同一个 manifest hash |  | 已确认：`845F0F4125061FF37A7F4DA20E0C88BC089200A08B319F1035D6522C80B56559` |  |  |
| 已阅读 `G1_SIGNOFF_FAST_CONFIRM_GUIDE_20260804.md` |  | 已确认 |  |  |
| 已阅读并接受 `PROTOCOL_ARBITER_DECISION_20260804.md` |  | 已确认 |  |  |
| 已确认本包不是真实实现放行包 |  | 已确认 |  |  |
| 已确认字段变更必须重新生成 manifest |  | 已确认 |  |  |

## 2. 协议检查项

| 检查项 | Clients owner | Cloud owner | Protocol arbiter | 结论/备注 |
| --- | --- | --- | --- | --- |
| `create_session` request/response 字段可接受 |  | 接受 |  |  |
| WSS token 仅通过 `Authorization: Bearer` 承载 |  | 接受 |  |  |
| `/ws/session` 为 R5.2.2 唯一产品实时入口 |  | 接受 |  |  |
| 不兼容此前 clients 协议的 no-legacy 决策可接受 |  | 接受 |  |  |
| `caps/granted_caps` 仅允许 `audio/text/cmd/state` |  | 接受 |  |  |
| unknown/duplicate caps 负例可接受 |  | 接受 |  |  |
| JSON 8192 pass、8193 fail 作为 G2 必测可接受 |  | 接受 |  |  |
| R5.2.2 close-code 合同不含旧 4001 可接受 |  | 接受 |  |  |

## 3. 命令检查项

| 检查项 | Clients owner | Cloud owner | Protocol arbiter | 结论/备注 |
| --- | --- | --- | --- | --- |
| P0 只自动下发单条 `data.cmd` |  | 接受 |  |  |
| P0 多命令进入 `multi_command_blocked`，只返回 ask_split，不生成 `data.cmd` |  | 接受 |  |  |
| `data.reply.multi_command_blocked.ask_split` 可回放，且禁止 `data.cmd/cmd_id/端侧执行副作用` |  | 接受 |  |  |
| G2 replayer 必须读取 `payload + context + expect`，不能只做 payload schema 校验 |  | 接受 |  |  |
| 多命令样例中 `context.intent_type=control_cmd_multi` 与 `expect.forbidden_types=["data.cmd"]` 可接受 |  | 接受 |  |  |
| P1 再设计低风险有序多命令 group/step |  | 接受 |  |  |
| 高危命令确认前不生成 `data.cmd` |  | 接受 |  |  |
| `data.cmd_ack.status=accepted/rejected/duplicate` 可接受 |  | 接受 |  |  |
| unknown cmd_id 不进入 `data.cmd_ack.status`，走 `data.error/audit` |  | 接受 |  |  |
| `data.cmd_result.status=running/succeeded/failed/canceled/timeout` 可接受 |  | 接受 |  |  |
| P0 不做未 ack 命令重放 |  | 接受 |  |  |

## 4. voice-cmd registry 检查项

| 检查项 | Clients owner | Cloud owner | Protocol arbiter | 结论/备注 |
| --- | --- | --- | --- | --- |
| P0 registry 参数类型仅 `enum/int` |  | 接受 |  |  |
| 控制/配置类 seed 的 action/capability/params/risk 可接受 |  | 接受 |  |  |
| registry schema 被理解为语音意图 seed/registry，不是端侧可执行全集 |  | 接受 |  |  |
| 端侧可执行只认 `delivery=data.cmd` 或 `data.cmd after confirmation` |  | 接受 |  |  |
| fake executor 忽略非 `x-client-executable-deliveries` 的 registry delivery |  | 接受 |  |  |
| `ctrl.set/config API` 按裁决为 P1/配置链路预留，不由 fake executor 自动消费 |  | 接受 |  |  |
| `data.cmd or ctrl.set by owner` 按具体 action 拆 owner，未冻结为 `data.cmd` 前不进入 G2 正向执行 |  | 接受 |  |  |
| 信息查询/知识问答由小歌自处理，返回 `data.reply`，不进入端侧执行器合同 |  | 接受 |  |  |
| P0 seed 只保证 trigger 覆盖，不承诺 200+ 泛化召回 |  | 接受 |  |  |
| embedding/semantic recall 层列入 P1 可接受 |  | 接受 |  |  |

## 5. G2 准备检查项

| 检查项 | Clients owner | Cloud owner | Protocol arbiter | 结论/备注 |
| --- | --- | --- | --- | --- |
| Clients fake SDK 负责人已明确：童紫薇 |  | 已知悉 |  |  |
| Clients fake executor 负责人已明确：童紫薇 |  | 已知悉 |  |  |
| Cloud fake server 负责人已明确：王明辉 |  | 已确认 |  |  |
| Cloud replay 负责人已明确：王明辉 |  | 已确认 |  |  |
| examples 正例/负例回放计划可接受 |  | 接受 |  |  |
| caps 正负例回放计划可接受 |  | 接受 |  |  |
| JSON 8192/8193 回放计划可接受 |  | 接受 |  |  |
| 多命令 ask_split 回放计划可接受 |  | 接受 |  |  |
| 多命令回放计划包含 `context/expect` 语义断言 |  | 接受 |  |  |
| close-code 11 cases 进入 G2 必测，且 R5.2.2 不继承 legacy 4001 |  | 接受 |  |  |
| G2 仅限 fake/mock/contract tests，不改真实 SDK/Gateway/Agent/sessproto/voice-cmd |  | 接受 |  |  |

## 6. 签收结论

| 角色 | 负责人/组织角色 | 是否签收 | manifest hash | 日期 | 备注 |
| --- | --- | --- | --- | --- | --- |
| Clients owner | 童紫薇 | Pending |  |  |  |
| Cloud owner | 王明辉 | 接受 | `845F0F4125061FF37A7F4DA20E0C88BC089200A08B319F1035D6522C80B56559` | 2026-08-04 | 已阅读并接受 R21 协议裁决意见；本签收不代表 G2 mock 编码或真实实现放行。 |
| Protocol arbiter | 陈强 | Pending |  |  |  |
