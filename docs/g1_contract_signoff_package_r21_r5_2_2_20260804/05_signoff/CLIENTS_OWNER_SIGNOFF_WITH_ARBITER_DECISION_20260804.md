# Clients Owner G1 二次签收回执（含裁决意见）

签收对象：`g1_contract_signoff_package_r21_r5_2_2_20260804`

合同版本：`xiaoge-duplex-protocol-r5.2.2`

签收角色：Clients owner

负责人：童紫薇

责任范围：clients SDK、GUI、fake SDK、fake executor

签收日期：2026-08-04

签收基准文件：`02_contracts/xiaoge-duplex-protocol-r5.2.2.manifest.json`

确认的 manifest SHA256：

```text
845F0F4125061FF37A7F4DA20E0C88BC089200A08B319F1035D6522C80B56559
```

裁决依据：`05_signoff/PROTOCOL_ARBITER_DECISION_20260804.md`

## 1. 待签收结论

请在下方三选一填写：

```text
签收结论：接受 / 拒绝 / 有条件接受
```

建议签收口径：

Clients owner 确认已阅读并接受协议裁决意见。端侧 SDK、GUI、fake SDK、fake executor 后续以 R5.2.2 manifest 对应合同为 G1/G2 基准；本签收不代表 G2 mock/契约测试编码已放行，也不代表真实 SDK/Gateway/Agent 实现已放行。

## 2. 已确认的 R5.2.2 基线

| 主题 | Clients owner 确认 |
| --- | --- |
| no-legacy | 本版本不兼容此前 clients 协议；旧 `/ws/audio`、裸 `cmd`、历史批量字段不进入端侧 SDK P0 目标。 |
| 连接与鉴权 | 端侧按 `create_session + /ws/session + ctrl.hello` 实现；WSS token 仅通过 `Authorization: Bearer <access_token>` 承载。 |
| caps | `caps/granted_caps` 仅允许 `audio/text/cmd/state`，非空且去重。 |
| JSON 上限 | WSS JSON 文本帧按 UTF-8 序列化字节数计算，8192 bytes pass，8193 bytes fail。 |
| 多命令 | P0 多命令进入 `multi_command_blocked`，只返回 ask_split `data.reply`，不生成 `data.cmd/cmd_id/端侧执行副作用`。 |
| cmd ack/result | `data.cmd_ack.status=accepted/rejected/duplicate` 只表示投递确认；执行进展和结果由 `data.cmd_result` 表达。 |
| registry 可执行边界 | 端侧 fake executor 只消费 `delivery=data.cmd` 或 `data.cmd after confirmation`。 |
| 信息查询/知识问答 | 由云端小歌自处理并返回 `data.reply`，不进入端侧执行器合同。 |

## 3. 协议裁决意见确认

| 裁决项 | 端侧确认 |
| --- | --- |
| `data.cmd` | 端侧 fake executor 可执行；Cloud 生成 `data.cmd`，Clients 接收并转交 fake executor。 |
| `data.cmd after confirmation` | 高危确认完成后才可执行；确认前不得接收执行类 `data.cmd`。 |
| `cloud_tool + data.reply` | 云端自处理信息查询并返回 `data.reply` + TTS；端侧不执行。 |
| `cloud_knowledge + data.reply` | 云端自处理知识问答并返回 `data.reply` + TTS；端侧不执行。 |
| `ask_split only` | 云端只返回拆分/选择提示；端侧不执行。 |
| `ctrl.set/config API` | R5.2.2 P0 schema 不启用 `ctrl.set`；端侧 fake executor 不消费。 |
| `data.cmd or ctrl.set by owner` | 未裁决为 `data.cmd` 的条目不进入 fake executor 自动执行；若要进入 G2 正向执行，必须先冻结为 `data.cmd` 或 `data.cmd after confirmation` 并重新生成 manifest。 |
| G2 replayer | 后续 G2 必须读取 `payload + context + expect`，不能只做 payload schema 校验。 |
| unknown/late `cmd_id` | unknown 走 `data.error/audit`；late/duplicate 只审计或去重，不污染当前轮。 |

## 4. G2 前置确认

Clients owner 确认：G1 未经三方签收并由评审组确认关闭前，不启动 G2 mock/契约测试编码。

G1 关闭且明确放行 G2 后，端侧仅按以下范围准备：

- fake SDK。
- fake executor。
- examples 回放。
- caps 正负例回放。
- JSON 8192/8193 边界回放。
- 多命令 ask_split 语义断言。
- `data.cmd_ack` / `data.cmd_result` 状态机回放。
- `ctrl.frontend_state` 事件触发、限频和 `trust_level` 行为验证。

G2 仍不得修改真实 SDK 实现，不得接真实机器人动作，不得私下新增字段或兼容旧协议。

## 5. 签收填写区

| 角色 | 负责人 | 签收结论 | manifest SHA256 | 日期 | 备注 |
| --- | --- | --- | --- | --- | --- |
| Clients owner | 童紫薇 | Pending | `845F0F4125061FF37A7F4DA20E0C88BC089200A08B319F1035D6522C80B56559` | 2026-08-04 | 请填写是否接受本裁决。 |

## 6. 如拒绝或有条件接受

请写明：

1. 分歧文件。
2. 具体字段、枚举、样例 ID、delivery 或 close code。
3. 期望改法。
4. 是否需要重新生成 manifest。

没有上述具体分歧时，建议签收为“接受”。
