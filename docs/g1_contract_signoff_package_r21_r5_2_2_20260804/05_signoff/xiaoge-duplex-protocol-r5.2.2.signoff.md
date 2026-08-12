# 小歌全双工协议 R5.2.2 G1 签收表

状态：草稿，尚未签收

合同版本：`xiaoge-duplex-protocol-r5.2.2`

签收基准：`02_contracts/xiaoge-duplex-protocol-r5.2.2.manifest.json`

本签收表是 G1 契约签收材料。它不授权 SDK/Gateway/Agent 真实实现改动，也不授权在评审同意前进入 G2 mock/契约测试编码。

R21 追加协议裁决意见：`05_signoff/PROTOCOL_ARBITER_DECISION_20260804.md`。三方签收时必须同时确认该裁决意见。

## 1. 合同文件

| 文件 | 用途 |
| --- | --- |
| `xiaoge-duplex-protocol-r5.2.2.schema.json` | P0 JSON 报文 schema |
| `xiaoge-duplex-protocol-r5.2.2.examples.jsonl` | P0 正/负样例 |
| `xiaoge-duplex-protocol-r5.2.2.close-codes.jsonl` | close/error code 回放用例 |
| `xiaoge-duplex-voicecmd-registry-r5.2.2.schema.json` | P0 语音意图 seed/registry schema；端侧可执行边界由 delivery 决定 |
| `xiaoge-duplex-protocol-r5.2.2.source-check.json` | 源表一致性报告 |
| `xiaoge-duplex-protocol-r5.2.2.manifest.json` | hash 签收基准 |
| `PROTOCOL_ARBITER_DECISION_20260804.md` | R21 协议裁决意见 |
| `CLIENTS_OWNER_SIGNOFF_WITH_ARBITER_DECISION_20260804.md` | 端侧 owner 二次签收模板 |
| `CLOUD_OWNER_SIGNOFF_WITH_ARBITER_DECISION_20260804.md` | 云侧 owner 二次签收模板 |

## 2. 需签收的核心口径

| 主题 | R5.2.2 口径 | 是否接受 |
| --- | --- | --- |
| no-legacy | 本版本不兼容此前 clients 协议；旧 `/ws/audio`、裸 cmd、历史批量字段不进入合同和 G2 mock |  |
| caps | `caps/granted_caps` 仅允许 `audio/text/cmd/state`，非空且去重 |  |
| JSON 上限 | WSS JSON 文本帧 8192 bytes 通过、8193 bytes 拒绝 |  |
| 多命令 | P0 只识别并阻断多控制动作；合同样例 `data.reply.multi_command_blocked.ask_split` 只返回 ask_split `data.reply`，不生成 `data.cmd/cmd_id/端侧执行副作用` |  |
| registry 类型 | registry schema 是语音意图 seed/registry；端侧只把 `delivery=data.cmd` 或 `data.cmd after confirmation` 当作可执行契约；参数类型仅 `enum/int` |  |
| registry 中间 delivery | `ctrl.set/config API`、`data.cmd or ctrl.set by owner` 不由端侧 fake executor 自动消费；三方需确认 owner 与配置/控制链路消费路径 |  |
| registry 中间 delivery 裁决 | `ctrl.set/config API` 为 P1/配置链路预留；`data.cmd or ctrl.set by owner` 按具体 action 拆 owner，未冻结为 `data.cmd` 或 `data.cmd after confirmation` 前不进入 G2 正向执行 |  |
| G2 语义断言 | 后续 G2 replayer 必须读取 `payload + context + expect`，不能只做 payload schema 校验；多命令样例必须断言不生成 `data.cmd` |  |
| G2 放行边界 | G1 未由三方按 R21 裁决签收并经评审组关闭前，不启动 G2 mock/契约测试编码；真实实现仍不放行 |  |
| close code | R5.2.2 close-code 合同不含旧 4001 |  |

## 3. 责任 owner

| 角色 | 负责人或唯一可追责组织角色 | 范围 | 签收结论 | 日期 |
| --- | --- | --- | --- | --- |
| Clients owner | 童紫薇 | SDK、GUI、fake SDK、fake executor；确认 R21 裁决意见 | Pending |  |
| Cloud owner | 王明辉 | Gateway/Auth、sessproto、Agent、voice-cmd、fake server、cloud replay；确认 R21 裁决意见 | 接受 | 2026-08-04 |
| Protocol arbiter | 陈强 | manifest hash、字段、枚举、错误码、close code、no-legacy 决策；确认 R21 裁决意见 | Pending |  |

G1 关闭前，上表三方必须确认同一个 manifest hash，并由评审组确认无 P0/P1 阻塞。

## 4. G2 mock 责任拆分

| 项目 | Owner | 负责人/备注 |
| --- | --- | --- |
| fake server / cloud replay | Cloud owner | 王明辉 |
| fake SDK / fake executor | Clients owner | 童紫薇 |
| shared examples / manifest / replay report 签收 | Clients owner + Cloud owner + Protocol arbiter | 童紫薇 + 王明辉 + 陈强 |

## 5. Gate 声明

- G1 只能在三方 owner 确认同一个 manifest hash 后签收。
- G2 只能在 G1 签收并经评审确认关闭后编写 mock/测试代码。
- 真实 SDK/Gateway/Agent 实现仍需 G1/G2/G3 和负责人明确批准。

## 6. 签收备注

| 角色 | 备注 |
| --- | --- |
| Clients owner | 童紫薇：待确认同一个 manifest hash。 |
| Cloud owner | 王明辉：已确认 manifest SHA256 `845F0F4125061FF37A7F4DA20E0C88BC089200A08B319F1035D6522C80B56559`，已阅读并接受 R21 协议裁决意见。本签收不代表 G2 mock 编码或真实 Gateway/Auth/sessproto/Agent/voice-cmd 实现放行。 |
| Protocol arbiter | 陈强：待裁决并确认同一个 manifest hash。 |
