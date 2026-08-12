# 小歌全双工语音交互 G1 契约签收确认包 R21 / R5.2.2

日期：2026-08-04

包用途：提交给端侧 owner、云端 owner、协议裁决人做 R5.2.2 G1 契约确认与签收。

R21 相比 R20 不修改合同字段、schema、examples、close-codes、source-check、manifest、工作簿或 SVG；只加入协议裁决意见、端云二次签收模板和最新 review copy，方便端云在同一裁决口径下重新签收。

本包不是 G2 mock/契约测试编码放行包，也不是 SDK/Gateway/Agent 真实实现编码放行包。

## 当前 Gate 状态

| Gate | 状态 | 说明 |
| --- | --- | --- |
| G1 技术材料 | 可进入端云二次签收 | 裁决者已同意端云有条件签收意见；R21 带裁决意见供端云重新确认。 |
| G1 三方签收 | 未关闭 | 童紫薇、王明辉、陈强需要确认同一个 manifest hash 和本裁决意见。 |
| G2 mock/契约测试编码 | 仍阻塞 | 只能在 G1 签收经评审确认关闭后再申请。 |
| SDK/Gateway/Agent 真实实现 | 仍阻塞 | 需要后续 G2/G3 Gate 和负责人明确批准。 |

## 三方 owner

| 角色 | 负责人 | 责任范围 |
| --- | --- | --- |
| Clients owner | 童紫薇 | clients SDK、GUI、fake SDK、fake executor。 |
| Cloud owner | 王明辉 | Gateway/Auth、sessproto、Agent、voice-cmd、fake server、cloud replay。 |
| Protocol arbiter | 陈强 | manifest hash、字段、枚举、错误码、close code、no-legacy 争议裁决。 |

## R5.2.2 核心变化

| 主题 | 口径 |
| --- | --- |
| no-legacy | 本版本不兼容此前 clients 协议；只接受 `create_session + /ws/session + ctrl.hello`。 |
| caps | `caps/granted_caps` 只能为 `audio/text/cmd/state`，非空且去重。 |
| 多命令 | P0 识别多控制动作并阻断执行，合同样例 `data.reply.multi_command_blocked.ask_split` 只返回 ask_split `data.reply`，不生成 `data.cmd/cmd_id/端侧执行副作用`；P1 再设计有序多命令执行。 |
| registry 类型 | `xiaoge-duplex-voicecmd-registry-r5.2.2.schema.json` 是语音意图 seed/registry；端侧可执行只认 `delivery=data.cmd` 或 `data.cmd after confirmation`；参数类型仅 `enum/int`。 |
| registry 中间 delivery 裁决 | `ctrl.set/config API` 为 P1/配置链路预留；`data.cmd or ctrl.set by owner` 按 action 拆 owner，未冻结为 `data.cmd` 前不进入 fake executor 自动执行。 |
| G2 replayer | 必须读取 `payload + context + expect`，不能只做 payload schema 校验。 |
| JSON 上限 | WSS JSON 文本帧 UTF-8 序列化上限 8192 bytes；G2 必测 8192 pass、8193 fail。 |
| close code | R5.2.2 close-code 合同不含旧 4001。 |

## 包目录说明

| 目录/文件 | 内容 | 用途 |
| --- | --- | --- |
| `00_review/` | R5.2.2 评审入口文档 | 已包含第 7-11 节；第 11 节记录端云签收意见裁决和 R21 处理。 |
| `01_workbook/` | R5.2.2 工作簿和 inspect NDJSON | 确认需求矩阵、schema、样例帧、端云分工、P0 registry、路线与验收。 |
| `02_contracts/` | 协议 schema、examples、close-codes、source-check、manifest、signoff、生成脚本 | G1 机器契约与签收依据。manifest hash 是签收基准。 |
| `03_design_docs/` | `PROTOCOL_V2_DESIGN.md`、`VOICE_CMD_DESIGN.md` | 协议语义、命令语义、no-legacy、多命令和 Gate 依据。 |
| `04_diagrams/` | R5.2.2 SVG 图 | 用于端云对齐时序、状态、命令投递和资源状态；最终以 schema/manifest 为准。 |
| `05_signoff/` | 签收工作副本、裁决意见、端云二次签收模板和 owner checklist | 端云填写 owner、确认 manifest hash、确认裁决意见、返回签收材料。 |
| `ENGINEER_EXECUTION_GUIDE_G1_G2_20260803.md` | 端云 G1/G2 执行说明 | 说明 G1/G2 怎么界定、分别提交什么、何时放行。 |
| `G1_SIGNOFF_FAST_CONFIRM_GUIDE_20260804.md` | G1 三方快速签收说明 | 说明收到包后如何快速校验、按角色确认、填写签收材料，以及裁决意见。 |
| `CLIENT_FRONTEND_STATE_AND_CMD_ACK_GUIDE_20260803.md` | 端侧字段说明 | 说明 `ctrl.frontend_state` 和 `data.cmd_ack.status` 使用规则。 |
| `VERIFY_PACKAGE.ps1` | 包内哈希校验脚本 | 解压后校验包内文件哈希和 contracts manifest。 |

## 端云确认任务

1. 在包根目录运行 `.\VERIFY_PACKAGE.ps1`。
2. 确认 `02_contracts/xiaoge-duplex-protocol-r5.2.2.manifest.json` 中的 hash 与收到的契约文件一致。
3. 确认自己消费或产生的字段、枚举、错误码、close code、能力行为和 no-legacy 边界是否可接受。
4. 确认 `05_signoff/xiaoge-duplex-protocol-r5.2.2.signoff.md` 中三方 owner 已预填为童紫薇、王明辉、陈强；如不一致，先退回评审组。
5. 阅读 `05_signoff/PROTOCOL_ARBITER_DECISION_20260804.md`。
6. 端侧填写 `05_signoff/CLIENTS_OWNER_SIGNOFF_WITH_ARBITER_DECISION_20260804.md`，云侧填写 `05_signoff/CLOUD_OWNER_SIGNOFF_WITH_ARBITER_DECISION_20260804.md`。
7. 在 `05_signoff/xiaoge-duplex-protocol-r5.2.2.signoff.md` 和 `05_signoff/OWNER_SIGNOFF_ACTIONS.md` 勾选各自 owner 检查项。
8. 三方确认签收的是同一个 manifest hash 和同一份裁决意见。
9. 将填写后的签收材料返回给评审组做 G1 关闭复核。

## 包内校验

解压后在本包根目录执行：

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

## 禁止事项

- 不得基于本包启动 SDK/Gateway/Agent 真实实现编码。
- 不得把旧 `/ws/audio`、裸 cmd、历史批量字段当作 R5.2.2 P0 主协议。
- 不得跳过 G1/G2 去做真实联调。
- 不得修改契约文件后继续沿用旧 manifest hash。
- 转发时不要混入 `node_modules/`、临时预览文件、无关 zip 或本地工程实现 diff。
