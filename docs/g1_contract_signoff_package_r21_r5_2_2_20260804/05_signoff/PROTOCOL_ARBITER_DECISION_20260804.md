# R5.2.2 G1 协议裁决意见

日期：2026-08-04

适用包：`g1_contract_signoff_package_r21_r5_2_2_20260804`

合同版本：`xiaoge-duplex-protocol-r5.2.2`

签收基准文件：`02_contracts/xiaoge-duplex-protocol-r5.2.2.manifest.json`

manifest 文件 SHA256：

```text
845F0F4125061FF37A7F4DA20E0C88BC089200A08B319F1035D6522C80B56559
```

## 1. 裁决结论

端侧 owner 和云侧 owner 对 R20/R5.2.2 的签收意见均为有条件接受。两方意见合理，不构成新的 P0/P1 技术阻塞。

本裁决确认：

1. R5.2.2 合同基线不变，不修改 schema、examples、close-codes、source-check、manifest、工作簿或 SVG。
2. registry 中间 delivery 不修改 enum，不进入端侧 fake executor 自动执行集合；通过本裁决冻结 owner 和消费路径。
3. G2 replayer 必须读取 `payload + context + expect`，不能只做 payload schema 校验。
4. 多命令 `data.reply.multi_command_blocked.ask_split` 只能输出 `data.reply`，不得生成 `data.cmd/cmd_id/端侧执行副作用`。
5. JSON 8192/8193、caps `audio/text/cmd/state`、close-code 11 cases、unknown/late `cmd_id` 行为均进入 G2 必测。
6. G1 未经端侧 owner、云侧 owner、协议裁决人按本裁决意见重新签收并由评审组复核关闭前，仍不放行 G2 mock/契约测试编码。
7. SDK/Gateway/Agent/sessproto/voice-cmd 真实实现仍不放行。

## 2. registry delivery 裁决表

| delivery | G1 裁决口径 | G2/G3 验收影响 |
| --- | --- | --- |
| `data.cmd` | 端侧 fake executor 可执行；Cloud 生成 `data.cmd`，Clients 接收并转交 fake executor。 | G2 必测单命令 `data.cmd -> data.cmd_ack -> data.cmd_result`。 |
| `data.cmd after confirmation` | 高危确认完成后才可下发；确认前不得生成 `data.cmd`。 | G2/G3 必测高危确认前无 `data.cmd`，确认后才进入执行链。 |
| `cloud_tool + data.reply` | 云端小歌自处理信息查询并返回 `data.reply` + TTS；端侧不执行。 | G2 不要求端侧 fake executor 消费。 |
| `cloud_knowledge + data.reply` | 云端小歌自处理知识问答并返回 `data.reply` + TTS；端侧不执行。 | G2 不要求端侧 fake executor 消费。 |
| `ask_split only` | 云端只返回拆分/选择提示；端侧不执行。 | G2 必测多命令不产生 `data.cmd/cmd_id/端侧执行副作用`。 |
| `ctrl.set/config API` | R5.2.2 P0 schema 不启用 `ctrl.set`；作为 P1/配置链路预留。端侧 fake executor 不消费。 | G2 只确认不被 fake executor 自动消费；真实配置写入后续 P1/G3 另行验收。 |
| `data.cmd or ctrl.set by owner` | 按具体 action 拆分 owner：设备/本地播放器类配置可走 `data.cmd` 由端侧执行；小歌会话配置走配置 API/后续 `ctrl.set` 由云侧配置链路处理。R5.2.2 G2 只测试“不被 fake executor 自动消费未裁决路径”。 | 若某 action 要进入 G2 正向执行，必须先在 registry 中冻结为 `data.cmd` 或 `data.cmd after confirmation` 并重新生成 manifest；否则仅作为 owner 待拆分配置项。 |

## 3. 对端侧有条件意见的裁决

| 端侧条件 | 裁决 |
| --- | --- |
| `ctrl.set/config API`、`data.cmd or ctrl.set by owner` 需明确 owner 和配置/控制链路消费路径 | 接受，按第 2 节裁决表冻结。端侧 fake executor 只消费 `data.cmd` 与 `data.cmd after confirmation`。 |
| G2 replayer 必须读取 `payload + context + expect` | 接受，列为 G2 必测前置条件。 |
| 多命令 ask_split 只输出 `data.reply`，禁止 `data.cmd/cmd_id/端侧执行副作用` | 接受，维持 R5.2.2 合同样例。 |
| JSON 8192/8193 按 UTF-8 序列化字节数计算 | 接受，维持 R5.2.2 合同。 |
| unknown/late `cmd_id` 不混入 `data.cmd_ack.status` | 接受。unknown 走 `data.error/audit`；late/duplicate 只审计或去重，不污染当前轮。 |

## 4. 对云侧有条件意见的裁决

| 云侧条件 | 裁决 |
| --- | --- |
| G2 仅实现 fake server/cloud replay/contract tests，不改真实 Gateway/Auth/sessproto/Agent/voice-cmd | 接受，作为 G2 放行前置边界。 |
| G2 replayer 必须读取 `payload + context + expect` | 接受，列为 G2 必测前置条件。 |
| 多命令只 ask_split，不生成 `data.cmd/cmd_id/端侧副作用` | 接受，维持 R5.2.2 合同样例。 |
| `/ws/session` 与旧 `/ws/audio` 隔离；R5.2.2 不继承 legacy 4001 | 接受，维持 no-legacy 和 close-code 合同。 |
| JSON 8192/8193、caps `audio/text/cmd/state`、close-code 11 cases 进入 G2 必测 | 接受，作为 G2 必测集合。 |
| registry 中间 delivery 的 owner/消费路径由三方备注冻结，否则退回协议裁决 | 接受，按第 2 节裁决表冻结。 |

## 5. G1 关闭条件

G1 关闭必须同时满足：

1. R21 包内 `VERIFY_PACKAGE.ps1` 全部 PASS。
2. Clients owner 童紫薇确认本裁决并签同一个 manifest SHA256。
3. Cloud owner 王明辉确认本裁决并签同一个 manifest SHA256。
4. Protocol arbiter 陈强确认本裁决并签同一个 manifest SHA256。
5. 评审组复核三方签收材料后明确 G1 关闭。

任一条件不满足，G1 不关闭；G2 mock/契约测试编码和真实实现继续阻塞。
