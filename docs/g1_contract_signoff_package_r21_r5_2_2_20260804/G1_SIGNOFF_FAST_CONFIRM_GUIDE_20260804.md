# 小歌 R5.2.2 G1 三方快速签收说明（含裁决意见）

日期：2026-08-04

适用包：`g1_contract_signoff_package_r21_r5_2_2_20260804`

合同版本：`xiaoge-duplex-protocol-r5.2.2`

签收基准文件：`02_contracts/xiaoge-duplex-protocol-r5.2.2.manifest.json`

manifest 文件 SHA256：

```text
845F0F4125061FF37A7F4DA20E0C88BC089200A08B319F1035D6522C80B56559
```

## 0. 本包定位

R21 是 G1 契约二次签收包。它在 R20 合同基线不变的前提下，加入协议裁决意见和端云二次签收模板。

本包不授权：

- G2 mock/契约测试编码。
- SDK/Gateway/Agent/sessproto/voice-cmd 真实实现改动。
- 真实机器人动作联调。
- 私下新增字段、枚举、delivery、错误码或 close code。

## 1. 收到包后先做什么

1. 解压包并进入包根目录。

```powershell
cd .\g1_contract_signoff_package_r21_r5_2_2_20260804
```

2. 运行校验。

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

3. 核对 manifest 文件 SHA256。

```powershell
(Get-FileHash .\02_contracts\xiaoge-duplex-protocol-r5.2.2.manifest.json -Algorithm SHA256).Hash
```

必须等于：

```text
845F0F4125061FF37A7F4DA20E0C88BC089200A08B319F1035D6522C80B56559
```

4. 阅读裁决文件：

```text
05_signoff/PROTOCOL_ARBITER_DECISION_20260804.md
```

5. 按角色填写签收文件：

```text
05_signoff/CLIENTS_OWNER_SIGNOFF_WITH_ARBITER_DECISION_20260804.md
05_signoff/CLOUD_OWNER_SIGNOFF_WITH_ARBITER_DECISION_20260804.md
05_signoff/xiaoge-duplex-protocol-r5.2.2.signoff.md
05_signoff/OWNER_SIGNOFF_ACTIONS.md
```

## 2. 裁决后冻结的核心口径

| 主题 | 口径 |
| --- | --- |
| 合同基线 | R5.2.2 schema、examples、close-codes、source-check、manifest 不变。 |
| no-legacy | `/ws/session` 是产品实时入口；旧 `/ws/audio`、裸 cmd、历史批量字段不进入本版合同、mock 或验收。 |
| caps | 只允许 `audio/text/cmd/state`，unknown/duplicate caps 为负例。 |
| JSON 上限 | 8192 bytes pass，8193 bytes fail；按 UTF-8 序列化后的 JSON 文本帧字节数计算。 |
| 多命令 | P0 只返回 `multi_command_blocked + ask_split` 的 `data.reply`，不得生成 `data.cmd/cmd_id/端侧执行副作用`。 |
| G2 replayer | 必须读取 `payload + context + expect`，不能只做 payload schema 校验。 |
| unknown/late `cmd_id` | unknown 走 `data.error/audit`；late/duplicate 只审计或去重，不污染当前轮。 |
| G2 边界 | G2 只允许 fake SDK/fake executor/fake server/cloud replay/contract tests，不改真实实现。 |

## 3. registry delivery 裁决表

| delivery | 裁决口径 |
| --- | --- |
| `data.cmd` | 端侧 fake executor 可执行；Cloud 生成，Clients 接收并转交 fake executor。 |
| `data.cmd after confirmation` | 高危确认完成后才可下发；确认前不得生成 `data.cmd`。 |
| `cloud_tool + data.reply` | 云端自处理信息查询并返回 `data.reply` + TTS；端侧不执行。 |
| `cloud_knowledge + data.reply` | 云端自处理知识问答并返回 `data.reply` + TTS；端侧不执行。 |
| `ask_split only` | 云端只返回拆分/选择提示；端侧不执行。 |
| `ctrl.set/config API` | R5.2.2 P0 schema 不启用 `ctrl.set`；作为 P1/配置链路预留；端侧 fake executor 不消费。 |
| `data.cmd or ctrl.set by owner` | 按具体 action 拆 owner；若要进入 G2 正向执行，必须先冻结为 `data.cmd` 或 `data.cmd after confirmation` 并重新生成 manifest。 |

## 4. 端侧 owner 要返回什么

负责人：童紫薇

请返回：

1. 已填写的 `05_signoff/CLIENTS_OWNER_SIGNOFF_WITH_ARBITER_DECISION_20260804.md`。
2. 已填写的 `05_signoff/xiaoge-duplex-protocol-r5.2.2.signoff.md` 中 Clients owner 行。
3. 已填写的 `05_signoff/OWNER_SIGNOFF_ACTIONS.md` 中 Clients owner 列。

端侧重点确认：

- fake executor 只消费 `data.cmd` 与 `data.cmd after confirmation`。
- registry 中间 delivery 不自动执行。
- 多命令 ask_split 只展示/播报，不触发执行。
- `data.cmd_ack` 不是执行结果。
- G1 未关闭前不写 G2 或真实 SDK 实现。

## 5. 云侧 owner 要返回什么

负责人：王明辉

请返回：

1. 已填写的 `05_signoff/CLOUD_OWNER_SIGNOFF_WITH_ARBITER_DECISION_20260804.md`。
2. 已填写的 `05_signoff/xiaoge-duplex-protocol-r5.2.2.signoff.md` 中 Cloud owner 行。
3. 已填写的 `05_signoff/OWNER_SIGNOFF_ACTIONS.md` 中 Cloud owner 列。

云侧重点确认：

- G2 只做 fake server/cloud replay/contract tests。
- G2 replayer 读取 `payload + context + expect`。
- 多命令不产生 `data.cmd/cmd_id/端侧副作用`。
- `/ws/session` 与旧 `/ws/audio` 隔离，不继承 legacy 4001。
- JSON 8192/8193、caps、close-code 11 cases 进入 G2 必测。

## 6. 协议裁决人要返回什么

负责人：陈强

请返回：

1. 已填写的 `05_signoff/xiaoge-duplex-protocol-r5.2.2.signoff.md` 中 Protocol arbiter 行。
2. 已填写的 `05_signoff/OWNER_SIGNOFF_ACTIONS.md` 中 Protocol arbiter 列。
3. 是否确认本裁决已解决端云有条件签收项。

## 7. G1 关闭条件

G1 关闭必须同时满足：

| 条件 | 要求 |
| --- | --- |
| 包校验 | `VERIFY_PACKAGE.ps1` 全部 PASS。 |
| 端侧签收 | 童紫薇确认本裁决并签同一个 manifest SHA256。 |
| 云侧签收 | 王明辉确认本裁决并签同一个 manifest SHA256。 |
| 协议裁决 | 陈强确认本裁决并签同一个 manifest SHA256。 |
| 评审复核 | 评审组复核签收材料后明确 G1 关闭。 |

任一条件不满足，G1 不关闭。

## 8. 分歧处理

如仍有分歧，请不要修改包内合同文件后继续沿用旧 hash。请返回：

1. 分歧文件。
2. 具体字段、枚举、样例 ID、delivery 或 close code。
3. 期望改法。
4. 是否需要重新生成 manifest。
