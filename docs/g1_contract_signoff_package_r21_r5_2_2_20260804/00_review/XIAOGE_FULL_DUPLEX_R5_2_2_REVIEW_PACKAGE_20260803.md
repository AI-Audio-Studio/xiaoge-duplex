# 小歌全双工语音交互 R5.2.2 评审包

日期：2026-08-03

本轮性质：R5.2.2 合同修订。只修改需求工作簿、协议/命令设计文档、JSON 合同、SVG 图和交付包，不修改 SDK、Gateway、Agent、sessproto、voice-cmd 实现代码、配置默认行为或部署脚本。

## 1. 本轮输入

用户已确认：

1. 当前 clients 使用量少，本版本不需要兼容此前 clients 协议。
2. 开工前先解决云侧提出的 6 个合同问题。
3. 多命令不能完全忽略：P0 要识别并给出拆分/选择提示，但不自动执行；P1 再设计低风险有序多命令。

云侧开工前问题：

| 编号 | 问题 | R5.2.2 处理 |
| --- | --- | --- |
| C1 | `VOICE_CMD_DESIGN.md` 4.2 多命令示例与 P0 单命令规则冲突 | 已拆为单命令正例和多命令阻断负例；多命令进入 `multi_command_blocked`，只返回 `data.reply` ask_split，不生成 `data.cmd`。 |
| C2 | `caps/granted_caps` 缺少机器枚举约束 | 冻结为 `audio/text/cmd/state`，数组非空且去重；unknown/duplicate caps 是 schema/协议负例。 |
| C3 | voice-cmd 参数类型口径不一致 | 选择收窄 schema：P0 registry 参数类型仅 `enum/int`；`string/object/array/date_expr` 等启动拒载。 |
| C4 | gate 漏报策略安全但泛化召回不足 | 明确 P0 seed 只保证 trigger 覆盖，不承诺 200+ 泛化召回；embedding/semantic recall 层列入 P1。 |
| C5 | JSON 8KB 依赖应用层补校验，G2 容易漏测 | 合同 examples 增加 8192 pass、8193 fail；G2 mock 必测 SDK 发送前限制 + agent/sessproto 二次校验。 |
| C6 | 本版本不要兼容之前版本客户端 | 协议改为 no-legacy：只接受 `create_session + /ws/session + ctrl.hello`；旧 `/ws/audio`、裸 cmd、历史批量字段不进入合同和 G2 mock。 |

## 2. R5.2.2 交付物

| 产物 | 路径/文件 | 用途 |
| --- | --- | --- |
| 工作簿 | `xiaoge_full_duplex_requirements_design_20260731_r5_2_2_review.xlsx` | 主评审入口，包含需求矩阵、权威 schema、样例帧、端云分工、P0 registry、Gate 和问题追踪。 |
| 工作簿 inspect | `xiaoge_full_duplex_requirements_design_20260731_r5_2_2_review.xlsx.inspect.ndjson` | 合同生成脚本反向校验依据。 |
| 协议 schema | `xiaoge-duplex-protocol-r5.2.2.schema.json` | P0 JSON 报文机器 schema。 |
| 协议 examples | `xiaoge-duplex-protocol-r5.2.2.examples.jsonl` | P0 正/负样例，含 caps enum、JSON 8192/8193、多命令阻断。 |
| close-code 用例 | `xiaoge-duplex-protocol-r5.2.2.close-codes.jsonl` | close/error code 回放用例。R5.2.2 不含旧 4001。 |
| voice-cmd registry schema | `xiaoge-duplex-voicecmd-registry-r5.2.2.schema.json` | P0 seed 控制/配置命令注册表 schema，参数类型仅 `enum/int`。 |
| source-check | `xiaoge-duplex-protocol-r5.2.2.source-check.json` | 工作簿、样例、schema、close-code 的一致性证明。 |
| manifest | `xiaoge-duplex-protocol-r5.2.2.manifest.json` | G1 签收 hash 基准。 |
| 设计文档 | `PROTOCOL_V2_DESIGN.md`、`VOICE_CMD_DESIGN.md` | 协议语义和命令语义权威说明。 |
| SVG 图 | `xiaoge_r5_2_2_*.svg` | 端云时序、命令状态、唤醒休眠、Gateway 资源状态图。 |

## 3. 机器校验结果

已运行：

```text
node --check outputs/xiaoge_full_duplex_20260731/build_workbook.mjs
node --check outputs/xiaoge_full_duplex_20260731/build_diagrams.mjs
node --check docs/design/protocol-v2/contracts/build_contracts.mjs
node outputs/xiaoge_full_duplex_20260731/build_workbook.mjs
node docs/design/protocol-v2/contracts/build_contracts.mjs
node outputs/xiaoge_full_duplex_20260731/build_diagrams.mjs
```

合同生成输出：

```text
CONTRACT_PACKAGE: xiaoge-duplex-protocol-r5.2.2
SCHEMA_SAMPLE_FIELD_CHECK: PASS
P1_CONTROL_NOT_IN_P0_SCHEMA: PASS
POSITIVE_EXAMPLES: 16
NEGATIVE_SCHEMA_FAIL_EXAMPLES: 12
NEGATIVE_SEMANTIC_OR_TRANSPORT_EXAMPLES: 3
SOURCE_RECONCILIATION: PASS
CLOSE_CODE_CASES: 11
```

工作簿公式错误扫描：`#REF!/#DIV/0!/#VALUE!/#NAME?/#N/A` 命中 0 条。

## 4. R5.2.2 冻结口径

| 主题 | 冻结口径 |
| --- | --- |
| 连接 | HTTPS `create_session` 短连接拿权限，WSS `/ws/session` 单长连接承载实时语音和 JSON。 |
| 鉴权 | WSS token 仅在 `Authorization: Bearer <access_token>`；`ctrl.hello` 不带 token。 |
| no-legacy | 不兼容此前 clients 协议；旧入口和旧裸帧不进入产品合同、examples、G2 mock。 |
| caps | `caps/granted_caps` 只能是 `audio/text/cmd/state`，非空、去重。 |
| JSON 大小 | WSS JSON 文本帧 UTF-8 序列化上限 8192 bytes；8192 pass、8193 fail。 |
| 多命令 | P0 识别多控制动作并阻断执行，返回拆分/选择提示；P1 再设计低风险有序多命令。 |
| 命令注册表 | P0 registry 参数类型仅 `enum/int`。信息查询/知识问答由小歌自处理，不进入控制命令 registry。 |
| cmd ack/result | `data.cmd_ack.status=accepted/rejected/duplicate` 只表示 SDK 投递确认；`data.cmd_result.status=running/succeeded/failed/canceled/timeout` 表示执行进展/结果。 |

## 5. 请评审组重点复核

1. 工作簿 `全双工语音交互需求` 中 FR-CONN-005、FR-CMD-003、FR-DEV-001、NFR-COMP-001 是否已体现 no-legacy 和多命令分阶段。
2. 工作簿 `权威JSON Schema`、`Schema样例帧`、合同 schema/examples 是否一致。
3. `caps/granted_caps` 是否只有 `audio/text/cmd/state`，且 unknown/duplicate 负例进入 G2。
4. P0 registry 是否只剩 `enum/int` 参数类型，且非控制类信息查询/知识问答未混入控制命令 registry。
5. `close-codes.jsonl` 是否不再包含旧 4001。
6. G2 mock 是否明确覆盖 8192/8193 JSON 边界和 `multi_command_blocked` 不下发 `data.cmd`。
7. 包内 README、签收表和 owner checklist 是否足以让端侧/云侧按同一 manifest hash 签收。

## 6. 当前 Gate 结论

| Gate | 状态 | 说明 |
| --- | --- | --- |
| G1 技术材料 | 可发起复核 | 机器合同已通过 source reconciliation。 |
| G1 三方签收 | 未关闭 | 端侧 owner、云端 owner、协议裁决人仍需确认同一个 manifest hash。 |
| G2 mock/契约测试 | 未放行 | 需 G1 签收关闭后再进入 mock/测试编码。 |
| SDK/Gateway/Agent 真实实现 | 未放行 | 仍需后续 G2/G3 和负责人明确批准。 |

## 7. R5.2.2 事实复核意见（评审组追加，2026-08-03）

### 7.1 复核范围与方法

本轮按“不看设计者自述，只看实际表、图、设计方案、机器合同和工程现状”的口径复核。已核验：

1. `xiaoge_full_duplex_requirements_design_20260731_r5_2_2_review.xlsx.inspect.ndjson` 中的需求矩阵、权威 schema、样例帧、协议与接口、P0 seed 命令表、命令状态机、实施路线与验收、图表索引。
2. `docs/design/protocol-v2/contracts/` 下 R5.2.2 schema、examples、close-codes、registry schema、source-check、manifest、signoff。
3. `docs/design/protocol-v2/PROTOCOL_V2_DESIGN.md` v1.9 与 `docs/design/voice-cmd/VOICE_CMD_DESIGN.md` v4.5。
4. R5.2.2 四张 SVG 图及签收包 `g1_contract_signoff_package_r18_r5_2_2_20260803`。
5. 当前未做实现编码；现有工程中的旧 `/ws/audio` 等只能作为历史/现状背景，不能替代或否定 R5.2.2 no-legacy 合同。

### 7.2 复核结论表

| 复核项 | 事实证据 | 结论 | 处理要求 |
| --- | --- | --- | --- |
| no-legacy | `PROTOCOL_V2_DESIGN.md` v1.9、工作簿 FR-CONN-005、协议与接口、manifest/signoff 均指向 `create_session + /ws/session + ctrl.hello`；合同 close-codes 未包含旧 4001；旧 `/ws/audio` 未进入 R5.2.2 examples。 | 通过本轮事实复核。用户已确认不用兼容旧 SDK，此方向可以成立。 | G1 签收时必须继续明确旧入口、裸 cmd、历史批量字段不作为本版开发目标。 |
| `caps/granted_caps` | schema 中 `createSessionRequest.caps`、`createSessionResponse.granted_caps`、`ctrlHello.caps`、`ctrlReady.granted_caps` 均为数组，枚举仅 `audio/text/cmd/state`，非空且去重；examples 有 unknown/duplicate 负例。 | 通过。 | G2 mock 必须保留 unknown/duplicate caps 反例。 |
| JSON 8192/8193 | examples 有 8192 pass 与 8193 fail；close/error 用例和 Gate 表要求 G2 覆盖 SDK 发送前限制与云端二次校验。 | 通过。 | G2 不得只测应用层，必须测 SDK 发送前和 agent/sessproto 二次校验。 |
| cmd_ack/result | schema、样例帧、协议正文、命令状态机均区分 `data.cmd_ack.status=accepted/rejected/duplicate` 与 `data.cmd_result.status=running/succeeded/failed/canceled/timeout`；unknown cmd 走 `data.error/audit`。 | 通过。 | G2/G3 用例要覆盖 unknown、duplicate、late result、ack timeout、result timeout。 |
| close/error code | manifest/source-check/close-codes 覆盖 1013、4009、4400、4401、4403，以及 401/403/503、`busy/protocol_error/resource_exhausted`；未见旧 4001 进入 R5.2.2 机器合同。 | 通过。 | 保持 4001 不进入 R5.2.2 合同和验收包。 |
| P0 registry 参数类型 | `xiaoge-duplex-voicecmd-registry-r5.2.2.schema.json` 的 param type 仅 `enum/int`。 | 通过参数类型收敛。 | 见 7.3 的 registry 命名边界问题，签收前需避免端侧误读。 |
| 多命令 P0 策略 | 工作簿 FR-CMD-003、P0 seed SEED-017、命令状态机、协议与接口、两张图都写明 `multi_command_blocked/ask_split` 且不生成 `data.cmd`。 | 设计说明方向正确，但机器合同证据不完整。 | 见 7.3 阻塞问题 R5.2.2-REV-P0-1。 |
| 图 | 四张 SVG 均生成，图表索引有需求映射；主时序图和命令状态图覆盖 no-legacy、多命令、ack/result、休眠门控和资源状态。 | 主体清晰，可用于技术评审；仍有非阻塞可读性问题。 | 见 7.3 图可读性问题。 |
| 签收包 | `VERIFY_PACKAGE.ps1` 输出 `PACKAGE_FILE_LIST_HASH_CHECK: PASS`、`CONTRACT_MANIFEST_HASH_CHECK: PASS`、`SOURCE_RECONCILIATION: PASS`；signoff 状态为 `DRAFT - not signed`，Clients/Cloud/Protocol owner 均为 `UNASSIGNED`。 | 包完整性通过；G1 签收未关闭。 | 不能以当前包作为最终签收闭环包。 |

### 7.3 必须处理的问题

| 问题ID | 严重级别 | 问题 | 事实依据 | 修改要求 | Gate |
| --- | --- | --- | --- | --- | --- |
| R5.2.2-REV-P0-1 | P0 阻塞 | `multi_command_blocked/ask_split` 缺少机器可回放合同样例，导致“多命令不下发 data.cmd”无法被 G2 mock 按合同自动验收。 | `xiaoge-duplex-protocol-r5.2.2.examples.jsonl` 中检索 `multi_command`、`multi_cmd`、`ask_split`、`往前走一米再挥手`、`control_cmd_multi` 均无命中；但工作簿 SEED-017、命令状态机、协议与接口和 SVG 均声称已覆盖。评审包第 2 节也声称 examples 包含“多命令阻断”，该声明与实际 examples 不一致。 | 在机器合同中补一条可回放的多命令阻断语义用例，至少包含输入话术“往前走一米再挥手”、期望 `intent_type/control_cmd_multi` 或 `multi_command_blocked`、期望只返回 `data.reply` ask_split、明确禁止 `data.cmd/cmd_id/端侧执行副作用`。同时更新 source-check，使 FR-CMD-003/SEED-017 与该用例建立正反向追踪。 | G1 技术材料补齐后再复核；未补前不得进入 G2 mock/实现。 |
| R5.2.2-REV-P1-1 | P1 签收阻塞 | owner 分工框架有了，但签收人仍未落到具体可负责的人或唯一组织角色。端和云分两人开发，小歌 clients 一人维护，其他云端部分另一人维护，这个边界必须在 G1 包里明确。 | `xiaoge-duplex-protocol-r5.2.2.signoff.md` 中 Clients owner、Cloud owner、Protocol arbiter 均为 `*_UNASSIGNED`；包内只给出 fake server/cloud replay 与 fake SDK/fake executor 的分工项。 | 将 Clients owner 明确为 clients SDK/GUI/fake SDK/fake executor 的唯一负责人；将 Cloud owner 明确为 Gateway/Auth/sessproto/Agent/voice-cmd/fake server/cloud replay 的唯一负责人；Protocol arbiter 明确负责 manifest hash、字段、枚举、错误码、no-legacy 争议裁决。三方签同一个 manifest hash 后，G1 才能关闭。 | G1 签收前必须完成。 |
| R5.2.2-REV-P1-2 | P1 澄清 | `voicecmd-registry` 机器 schema 允许 `info_query/knowledge_qa`，虽然 delivery 标为 `cloud_tool + data.reply` / `cloud_knowledge + data.reply`，但如果端侧把该文件理解为“可执行控制命令注册表”，会产生协同歧义。 | registry schema 的 `intent_type` enum 包含 `info_query`、`knowledge_qa`；工作簿中又要求信息查询/知识问答由小歌自处理，不进入控制命令 registry。 | 签收包中把该文件命名/说明为“语音意图 seed/registry schema”，或拆出“端侧可执行 control registry”视图；明确只有 `delivery=data.cmd` 或 `data.cmd after confirmation` 的条目可被 clients/fake executor 当作端侧执行契约。 | G1 签收前澄清；不阻塞 no-legacy 方向。 |
| R5.2.2-REV-P2-1 | P2 可读性 | SVG 主体可读，但存在英文 token 被硬拆行，影响传给端云同事时的观感和局部理解。 | `xiaoge_r5_2_2_end_cloud_sequence.svg` 中 `ack/r` + `esult`、`slee` + `ping`；`xiaoge_r5_2_2_wake_sleep_gate_state.svg` 中 `fr` + `ontend_state`。主时序图 M11 将 `multi_cmd ask_split / high-risk confirm` 放在同一箭头说明中，也容易被读成同一路径。 | 重排这些说明框或改短文案；建议把多命令 ask_split 与高危确认拆成两个分支标签。 | 不阻塞机器合同修正；正式对外签收包建议修。 |
| R5.2.2-REV-P2-2 | P2 包一致性 | 当前签收包内 `00_review/` 是本次评审追加前的拷贝。后续如修合同/examples/图，manifest 和 zip 必然变化。 | 主评审文件被本节追加后，既有 zip/目录内 review copy 不会自动包含本节。 | 设计者完成 P0/P1 修改后重新生成工作簿、contracts、SVG、signoff package、manifest hash 和 zip，并把本节评审意见纳入新版 review copy。 | G1 再送审前完成。 |

### 7.4 Gate 结论

| Gate | 评审结论 | 说明 |
| --- | --- | --- |
| G1 技术材料 | 不同意按当前 R5.2.2 包直接关闭 | no-legacy、caps、JSON 8KB、close/error、ack/result 已基本通过；但多命令阻断缺少机器可回放合同样例，且评审包对 examples 的覆盖声明与事实不一致。 |
| G1 三方签收 | 不同意关闭 | owner 仍未指定，signoff 仍是 draft。 |
| 是否可发端云同事 | 可作为“带已知阻塞问题的草稿”预览，不建议作为正式 G1 签收包传递 | 如果要让端云同事同步确认，应先让设计者补齐 R5.2.2-REV-P0-1 并重新打包；否则端云会拿到一个声称覆盖多命令、但机器用例缺失的包。 |
| G2 mock/契约测试 | 不放行 | 必须等 G1 技术材料修正并由 Clients owner、Cloud owner、Protocol arbiter 签同一 manifest hash。 |
| SDK/Gateway/Agent 实现 | 不放行 | 本轮仍只评设计与合同，不进入实现编码。 |

### 7.5 对设计者的明确要求

1. 先修 R5.2.2-REV-P0-1：补多命令阻断的机器可回放用例和 source-check 追踪，确保 `examples.jsonl` 或等价合同用例中能检索到 `multi_command_blocked/ask_split`，并明确“不生成 data.cmd”可验收。
2. 补齐三方 owner：clients owner、cloud owner、protocol arbiter 必须是具体人或唯一可问责角色，且在签收文件中写清各自负责的 fake/mock、真实实现边界和 manifest hash 签收责任。
3. 澄清 registry 消费边界：端侧只消费可执行控制命令视图，信息查询/知识问答只能由云端小歌自处理并返回 `data.reply`。
4. 修正 SVG 小可读性问题并重新打包；新版包需要重新跑 `VERIFY_PACKAGE.ps1`，并给出新的 manifest hash。

## 8. 设计方对第 7 节复审的响应与修正记录（2026-08-03）

### 8.1 总体结论

接受第 7 节复审意见。原 R18/R5.2.2 包不能作为 G1 关闭包，原因成立：多命令阻断缺少机器可回放合同样例、三方 owner 未落具体人、registry 消费边界容易被端侧误读、SVG 可读性需修正，且包内 `00_review/` 不是最新评审文件。

本次只修设计材料、合同、工作簿、SVG 和签收包，不修改 SDK/Gateway/Agent/sessproto/voice-cmd 等工程实现代码。G2 mock 与真实实现仍未放行。

### 8.2 逐条响应

| 问题ID | 处理结论 | 修正内容 | 证据/产物 |
| --- | --- | --- | --- |
| R5.2.2-REV-P0-1 | 接受，已补齐 | 在 `examples.jsonl` 增加 `data.reply.multi_command_blocked.ask_split`：输入话术 `往前走一米再挥手`，context 标明 `control_cmd_multi`、`multi_command_blocked`、`ask_split`、`SEED-017`、`FR-CMD-003`；expect 标明输出类型只有 `data.reply`，禁止 `data.cmd/cmd_id/executor_side_effect`。 | `02_contracts/xiaoge-duplex-protocol-r5.2.2.examples.jsonl`；`02_contracts/xiaoge-duplex-protocol-r5.2.2.source-check.json` 的 `multi_command_blocked_contract`。 |
| R5.2.2-REV-P1-1 | 接受，owner 已落具体人 | 根据用户补充，Clients owner=童紫薇，负责 clients SDK/GUI/fake SDK/fake executor；Cloud owner=王明辉，负责 Gateway/Auth/sessproto/Agent/voice-cmd/fake server/cloud replay；Protocol arbiter=陈强，负责 manifest hash、字段、枚举、错误码、close code、no-legacy 争议裁决。签收状态仍为 Pending。 | `02_contracts/xiaoge-duplex-protocol-r5.2.2.signoff.md`；manifest `accountable_owners`；`05_signoff/OWNER_SIGNOFF_ACTIONS.md`。 |
| R5.2.2-REV-P1-2 | 接受，已澄清 | 保留文件名但把 schema title/description 改为“Voice Intent Seed/Registry”，并增加 `x-client-executable-deliveries=["data.cmd","data.cmd after confirmation"]` 与 `x-cloud-reply-only-deliveries`。明确 info_query/knowledge_qa/ask_split only 只由云端处理并返回 `data.reply`，端侧不得当作执行器契约。 | `02_contracts/xiaoge-duplex-voicecmd-registry-r5.2.2.schema.json`；工作簿 `P0 registry schema`；`VOICE_CMD_DESIGN.md` §5.1.1。 |
| R5.2.2-REV-P2-1 | 接受，已修正 | 主时序图将 M11 拆为 `M11a 多命令 ask_split` 与 `M11b 高危确认` 两个分支；图中缩短 `frontend_state`、`sleeping`、`ack/result` 等展示标签，避免英文 token 硬拆行。 | `04_diagrams/xiaoge_r5_2_2_end_cloud_sequence.svg`；`xiaoge_r5_2_2_command_delivery_state.svg`；`xiaoge_r5_2_2_wake_sleep_gate_state.svg`；`xiaoge_r5_2_2_gateway_resource_state.svg`。 |
| R5.2.2-REV-P2-2 | 接受，重新打包 | 重新生成工作簿、contracts、SVG、signoff package、manifest hash 和 zip；新版包内 `00_review/` 包含第 7 节复审和本第 8 节响应。 | 新包：`g1_contract_signoff_package_r19_r5_2_2_20260803`；新 zip：`g1_contract_signoff_package_r19_r5_2_2_20260803.zip`。 |

### 8.3 Gate 口径

| Gate | 当前状态 | 放行条件 |
| --- | --- | --- |
| G1 技术材料 | 可重新送审 | 评审组复核新版 R19 包的合同、source-check、工作簿和 SVG 后确认无 P0/P1 阻塞。 |
| G1 三方签收 | 未关闭 | 童紫薇、王明辉、陈强三方确认并签同一个 manifest hash。 |
| G2 mock/契约测试 | 仍阻塞 | G1 技术材料复核通过且三方签收关闭后，才允许写 fake SDK/fake executor/fake server/cloud replay。 |
| SDK/Gateway/Agent 真实实现 | 仍阻塞 | G2 mock 通过并完成 G3 契约测试方案确认后，才可申请真实实现。 |

### 8.4 复核重点

1. `examples.jsonl` 可检索到 `multi_command_blocked`、`ask_split`、`往前走一米再挥手`、`control_cmd_multi`、`SEED-017`、`FR-CMD-003`。
2. `source-check.json` 中 `multi_command_blocked_contract.result=PASS`，并明确 `forbidden_outputs=["data.cmd","cmd_id","executor_side_effect"]`。
3. `signoff.md` 与 manifest 中三方 owner 均为：童紫薇、王明辉、陈强，状态为 Pending 而非 Signed。
4. registry schema title/description 与扩展字段明确“语音意图 seed/registry”和端侧可执行 delivery 边界。
5. 新包运行 `VERIFY_PACKAGE.ps1` 后必须输出 `PACKAGE_FILE_LIST_HASH_CHECK: PASS`、`CONTRACT_MANIFEST_HASH_CHECK: PASS`、`SOURCE_RECONCILIATION: PASS`。

## 9. 对第 8 节响应的事实复核意见（评审组追加，2026-08-03）

### 9.1 本轮复核结论

第 8 节响应中的 R19/R5.2.2 修正基本属实。第 7 节提出的 P0 阻塞项 `R5.2.2-REV-P0-1` 已通过实际合同补齐；owner、registry 边界、SVG 可读性和 R19 打包也已按要求处理到可进入 G1 三方签收确认的程度。

结论：**同意 R19 作为 G1 技术材料提交童紫薇、王明辉、陈强做三方签收确认；不同意直接关闭 G1；不同意放行 G2 mock/契约测试编码；不同意放行 SDK/Gateway/Agent 真实实现。**

### 9.2 机器复核记录

| 复核项 | 实际结果 | 结论 |
| --- | --- | --- |
| R19 包校验 | 运行 `g1_contract_signoff_package_r19_r5_2_2_20260803/VERIFY_PACKAGE.ps1`，输出 `PACKAGE_FILE_LIST_HASH_CHECK: PASS`、`CONTRACT_MANIFEST_HASH_CHECK: PASS`、`SOURCE_RECONCILIATION: PASS`、`CLOSE_CODE_CASES: 11`、`PACKAGE_STATUS: G1_SIGNOFF_CONFIRMATION_PACKAGE_NOT_IMPLEMENTATION_RELEASE`。 | 通过。 |
| examples 分类 | 独立解析 `xiaoge-duplex-protocol-r5.2.2.examples.jsonl`：positive=17，negative schema fail=12，negative semantic=2，negative transport=1；与 manifest 计数一致。 | 通过。 |
| 多命令样例 | `examples.jsonl` 已有 `data.reply.multi_command_blocked.ask_split`；包含输入 `往前走一米再挥手`、`context.intent_type=control_cmd_multi`、`state=multi_command_blocked`、`reply_style=ask_split`、`source_seed=SEED-017`、`requirements=FR-CMD-003`。 | 通过。 |
| 多命令禁止输出 | 同一样例 `expect.output_types=["data.reply"]`，`expect.forbidden_types=["data.cmd"]`，`no_cmd_id=true`，`no_side_effects=true`；`context.forbidden_outputs` 包含 `data.cmd/cmd_id/executor_side_effect`。 | 通过。 |
| source-check 追踪 | `source-check.json` 中 `multi_command_blocked_contract.result=PASS`，并关联 `example_id=data.reply.multi_command_blocked.ask_split`、`source_seed=SEED-017`、`requirement_id=FR-CMD-003`。 | 通过。 |
| owner | `signoff.md` 和 manifest 已写入 Clients owner=童紫薇、Cloud owner=王明辉、Protocol arbiter=陈强，均为 Pending。包内 README、OWNER_SIGNOFF_ACTIONS、G1/G2 指南也同步。 | 通过；签收未完成。 |
| registry 边界 | `xiaoge-duplex-voicecmd-registry-r5.2.2.schema.json` title/description 已改为 Voice Intent Seed/Registry；扩展字段含 `x-client-executable-deliveries=["data.cmd","data.cmd after confirmation"]` 和 `x-cloud-reply-only-deliveries`，明确 info/knowledge/ask_split 不能被端侧 fake executor 当执行契约。 | 条件通过，见 9.3 注意项。 |
| SVG | 主时序图已拆出 `M11a 多命令 ask_split` 与 `M11b 高危确认`；原先 `ack/r + esult`、`fr + ontend_state` 等硬拆问题在文本结构核验中未再发现。 | 通过文本/结构核验；本机无 SVG 渲染器，未声明渲染目视通过。 |
| R19 包内 review copy | `00_review/XIAOGE_FULL_DUPLEX_R5_2_2_REVIEW_PACKAGE_20260803.md` 已包含第 7 节复审意见和第 8 节设计方响应。 | 通过；本第 9 节是包生成后的新追加。 |

### 9.3 保留注意项

| 项 | 严重级别 | 说明 | 要求 |
| --- | --- | --- | --- |
| registry 中间 delivery | P2/签收注意 | schema 已明确端侧 fake executor 只消费 `data.cmd` 和 `data.cmd after confirmation`。但 enum 中仍存在 `ctrl.set/config API` 与 `data.cmd or ctrl.set by owner`，它们既不在 `x-client-executable-deliveries`，也不在 `x-cloud-reply-only-deliveries`。这不再阻塞 G1 技术材料，但三方签收时必须确认这些中间 delivery 的 owner 和消费路径，避免实现阶段端侧/云端各自理解。 | 在 G1 签收会议或签收表备注中确认：fake executor 忽略非 `x-client-executable-deliveries`；config/control 由 clients/config/cloud config 按 owner 另行实现和验收。 |
| `data.reply.intent_type` | P2/验收注意 | 新增多命令样例的真实 payload 是 `data.reply.intent_type=control_cmd`，而 `control_cmd_multi` 放在 `context.intent_type`；这是因为 `data.reply` schema 枚举不包含 `control_cmd_multi`。 | G2 回放器不能只做 payload schema 校验，还必须读取 `context/expect` 进行语义断言：multi-command 只能输出 `data.reply`，不得生成 `data.cmd/cmd_id/端侧副作用`。 |
| 包内 review copy | P2/分发注意 | R19 zip 是第 8 节响应后生成的，本第 9 节追加后，zip 内 `00_review` 不会自动包含第 9 节。 | 若 R19 zip 作为唯一材料发端云，请同步附带本最新版评审文档；若要求包内 review copy 也包含第 9 节，则设计者需重新打包但不必修改合同字段。 |

### 9.4 Gate 结论

| Gate | 本轮结论 | 说明 |
| --- | --- | --- |
| G1 技术材料 | 同意进入三方签收确认 | 第 7 节 P0/P1 技术阻塞已实质修复，无新的 P0/P1 阻塞。 |
| G1 三方签收 | 未关闭 | 童紫薇、王明辉、陈强仍需确认并签同一个 manifest hash。 |
| G2 mock/契约测试 | 暂不放行 | 只有 G1 三方签收完成并经评审确认后，才允许进入 mock/契约测试编码。 |
| SDK/Gateway/Agent 真实实现 | 不放行 | 真实实现仍需 G2/G3 和负责人明确批准。 |

### 9.5 可传递意见

可以把 R19 作为 **G1 契约签收确认包** 传给端侧 owner 童紫薇、云端 owner 王明辉、协议裁决人陈强确认；传递时必须明确三点：

1. 他们签的是同一个 R5.2.2 manifest hash，不是签“可以开始实现”。
2. G1 未签收关闭前，不得写 G2 mock/契约测试代码。
3. 真实 SDK/Gateway/Agent 实现仍未放行。

## 10. 设计方对第 9 节复审的响应与 R20 签收包处理记录（2026-08-03）

### 10.1 总体结论

接受第 9 节复审意见。R19/R5.2.2 的合同修正已经可以作为 G1 技术材料进入三方签收确认，但不能直接关闭 G1，也不能放行 G2 mock/契约测试编码，更不能放行 SDK/Gateway/Agent 真实实现。

本次处理不再修改 R5.2.2 schema、examples、close-codes、source-check、manifest、工作簿或 SVG 的合同内容，只做签收分发层面的补强：重新生成一个 R20 签收包，使包内 `00_review/` 包含第 9 节复审意见和本第 10 节响应，并在签收包中增加详细使用说明，便于童紫薇、王明辉、陈强快速确认同一个 manifest hash。

R20 的定位是 **G1 契约签收确认包**，不是实现放行包。

### 10.2 对第 9.3 节保留注意项的逐条响应

| 保留项 | 处理结论 | R20 处理方式 | 对签收/G2 的约束 |
| --- | --- | --- | --- |
| registry 中间 delivery | 接受，作为 G1 签收确认项处理，不改合同枚举 | 在 R20 的快速签收说明、README、G1/G2 执行指南和 owner checklist 中明确：端侧 fake executor 只消费 `x-client-executable-deliveries` 中的 `data.cmd` 与 `data.cmd after confirmation`；`ctrl.set/config API`、`data.cmd or ctrl.set by owner` 不由 fake executor 自动消费，需由对应 owner 在配置/控制链路中另行确认实现与验收路径。 | 三方签收时必须在备注中确认中间 delivery 的 owner 与消费路径；如三方有分歧，G1 不关闭，回到协议裁决。 |
| `data.reply.intent_type` | 接受，作为 G2 回放器语义断言要求处理，不扩展 `data.reply` schema | 在 R20 使用说明中明确：多命令阻断样例的 payload 仍是合法 `data.reply.intent_type=control_cmd`，`control_cmd_multi` 放在合同样例的 `context.intent_type`；G2 replayer 必须同时读取 `payload + context + expect`，不能只做 payload schema 校验。 | G2 mock/契约测试通过条件必须包含：`data.reply.multi_command_blocked.ask_split` 只输出 `data.reply`，不得生成 `data.cmd/cmd_id/端侧执行副作用`。 |
| 包内 review copy | 接受，通过重新打包解决 | 生成 R20 签收包，包内 `00_review/XIAOGE_FULL_DUPLEX_R5_2_2_REVIEW_PACKAGE_20260803.md` 包含第 9 节和第 10 节；合同字段不变。 | 对外转发时优先使用 R20 包；不再把 R19 zip 作为唯一材料发给端云确认。 |

### 10.3 R20 签收包新增使用说明

R20 包新增 `G1_SIGNOFF_FAST_CONFIRM_GUIDE_20260803.md`，用于让端云同事在较短时间内完成确认。该说明覆盖：

1. 收到包后的 5 分钟确认路径：解压、运行 `VERIFY_PACKAGE.ps1`、核对 manifest hash、按角色查看检查项、填写签收材料。
2. 三方共同基线：no-legacy、caps 枚举、JSON 8192/8193、多命令 ask_split、registry 可执行边界、close-code 口径。
3. 童紫薇、王明辉、陈强各自需要重点确认的文件、字段和结论。
4. 第 9.3 节两个容易误读的点：registry 中间 delivery 和 `context/expect` 语义断言。
5. 签收返回物：`05_signoff/xiaoge-duplex-protocol-r5.2.2.signoff.md`、`05_signoff/OWNER_SIGNOFF_ACTIONS.md`、角色确认说明。
6. 分歧处理：任何字段、枚举、错误码、delivery 或样例分歧都不得在实现里私下消化，必须回到 G1 协议裁决并重新生成 manifest。

### 10.4 Gate 口径

| Gate | 当前结论 | 说明 |
| --- | --- | --- |
| G1 技术材料 | 同意使用 R20 发起三方签收确认 | R20 仅增强签收说明和 review copy，合同基线沿用第 9 节已经复核通过的 R5.2.2 内容。 |
| G1 三方签收 | 未关闭 | 童紫薇、王明辉、陈强必须确认并签同一个 R5.2.2 manifest hash，评审组复核签收材料后才算关闭。 |
| G2 mock/契约测试 | 暂不放行 | G1 未关闭前不得写 fake SDK、fake executor、fake server、cloud replay 或契约测试代码。 |
| SDK/Gateway/Agent 真实实现 | 不放行 | 真实实现仍需 G2/G3 通过并由负责人明确批准。 |

### 10.5 给评审组的复核建议

请重点复核 R20 包的以下点：

1. `VERIFY_PACKAGE.ps1` 是否仍输出 `PACKAGE_FILE_LIST_HASH_CHECK: PASS`、`CONTRACT_MANIFEST_HASH_CHECK: PASS`、`SOURCE_RECONCILIATION: PASS`。
2. 包内 `00_review/` 是否包含第 9 节复审意见和本第 10 节响应。
3. `G1_SIGNOFF_FAST_CONFIRM_GUIDE_20260803.md` 是否足够支持端侧 owner、云端 owner、协议裁决人快速确认。
4. owner checklist 是否已把 registry 中间 delivery 和 G2 `context/expect` 断言列为明确签收/后续验收项。
5. R20 是否仍清楚标注：这是 G1 契约签收确认包，不是 G2 或真实实现放行包。

### 10.6 R20 产物与校验记录

R20 实际产物：

- 签收包目录：`outputs/xiaoge_full_duplex_20260731/g1_contract_signoff_package_r20_r5_2_2_20260803`
- 签收包 zip：`outputs/xiaoge_full_duplex_20260731/g1_contract_signoff_package_r20_r5_2_2_20260803.zip`
- R5.2.2 manifest 文件 SHA256：`845F0F4125061FF37A7F4DA20E0C88BC089200A08B319F1035D6522C80B56559`
- R20 zip SHA256：由最终压缩产物生成后在交付回复中提供，不写入包内文件，避免 zip 哈希自引用失效。

R20 包内校验输出：

```text
PACKAGE_FILE_LIST_HASH_CHECK: PASS
CONTRACT_MANIFEST_HASH_CHECK: PASS
SOURCE_RECONCILIATION: PASS
CLOSE_CODE_CASES: 11
PACKAGE_STATUS: G1_SIGNOFF_CONFIRMATION_PACKAGE_NOT_IMPLEMENTATION_RELEASE
```

## 11. 协议裁决者对端云 G1 有条件签收意见的裁决与 R21 处理记录（2026-08-04）

### 11.1 输入材料

本轮输入为端侧与云侧对 R20/R5.2.2 G1 契约签收包的回应：

- 端侧：`outputs/xiaoge_full_duplex_20260731/端云回应/CLIENTS_OWNER_SIGNOFF_20260804.md`
- 云侧：`outputs/xiaoge_full_duplex_20260731/端云回应/G1签收-Cloud.md`

端侧 owner 童紫薇与云侧 owner 王明辉均为 **有条件接受**。两方确认的 manifest SHA256 或签收对象均指向 R5.2.2 G1 契约基线，且未要求修改 schema、examples、close-codes、source-check 或 manifest。

### 11.2 裁决结论

接受端云两方的有条件签收意见。两方意见合理，不构成新的 P0/P1 技术阻塞；G1 不需要重开 R5.2.2 合同字段。

裁决如下：

1. R5.2.2 合同基线不变，manifest 文件 SHA256 仍为 `845F0F4125061FF37A7F4DA20E0C88BC089200A08B319F1035D6522C80B56559`。
2. registry 中间 delivery 不修改 enum，不进入端侧 fake executor 自动执行集合；通过 G1 裁决备注冻结 owner 和消费路径。
3. G2 replayer 必须读取 `payload + context + expect`，不能只做 payload schema 校验。
4. 多命令 `data.reply.multi_command_blocked.ask_split` 只能输出 `data.reply`，不得生成 `data.cmd/cmd_id/端侧执行副作用`。
5. JSON 8192/8193、caps `audio/text/cmd/state`、close-code 11 cases、unknown/late `cmd_id` 行为均进入 G2 必测。
6. G1 未经端侧 owner、云侧 owner、协议裁决人按本裁决意见重新签收并由评审组复核关闭前，仍不放行 G2 mock/契约测试编码。
7. SDK/Gateway/Agent/sessproto/voice-cmd 真实实现仍不放行。

### 11.3 registry delivery 裁决表

| delivery | G1 裁决口径 | G2/G3 验收影响 |
| --- | --- | --- |
| `data.cmd` | 端侧 fake executor 可执行；Cloud 生成 `data.cmd`，Clients 接收并转交 fake executor。 | G2 必测单命令 `data.cmd -> data.cmd_ack -> data.cmd_result`。 |
| `data.cmd after confirmation` | 高危确认完成后才可下发；确认前不得生成 `data.cmd`。 | G2/G3 必测高危确认前无 `data.cmd`，确认后才进入执行链。 |
| `cloud_tool + data.reply` | 云端小歌自处理信息查询并返回 `data.reply` + TTS；端侧不执行。 | G2 不要求端侧 fake executor 消费。 |
| `cloud_knowledge + data.reply` | 云端小歌自处理知识问答并返回 `data.reply` + TTS；端侧不执行。 | G2 不要求端侧 fake executor 消费。 |
| `ask_split only` | 云端只返回拆分/选择提示；端侧不执行。 | G2 必测多命令不产生 `data.cmd/cmd_id/端侧执行副作用`。 |
| `ctrl.set/config API` | R5.2.2 P0 schema 不启用 `ctrl.set`；作为 P1/配置链路预留。端侧 fake executor 不消费。 | G2 只确认不被 fake executor 自动消费；真实配置写入后续 P1/G3 另行验收。 |
| `data.cmd or ctrl.set by owner` | 按具体 action 拆分 owner：设备/本地播放器类配置可走 `data.cmd` 由端侧执行；小歌会话配置走配置 API/后续 `ctrl.set` 由云侧配置链路处理。R5.2.2 G2 只测试“不被 fake executor 自动消费未裁决路径”。 | 若某 action 要进入 G2 正向执行，必须先在 registry 中冻结为 `data.cmd` 或 `data.cmd after confirmation` 并重新生成 manifest；否则仅作为 owner 待拆分配置项。 |

### 11.4 对端云条件的处理

| 条件 | 裁决 | 是否需要改合同 |
| --- | --- | --- |
| registry 中间 delivery owner/消费路径需冻结 | 接受，按 11.3 表冻结。 | 不改 schema/manifest；写入 R21 签收包与端云二次签收模板。 |
| G2 replayer 读取 `payload + context + expect` | 接受，作为 G2 必测前置条件。 | 不改合同；已有 examples/source-check 支撑。 |
| 多命令 ask_split 不生成 `data.cmd/cmd_id/端侧副作用` | 接受，维持 R20/R5.2.2 合同。 | 不改合同；已有机器样例支撑。 |
| JSON 8192/8193 按 UTF-8 bytes | 接受，维持 R20/R5.2.2 合同。 | 不改合同；已有样例支撑。 |
| unknown/late `cmd_id` 不污染 `data.cmd_ack.status` | 接受，unknown 走 `data.error/audit`，late/duplicate 只审计或去重。 | 不改合同；已有设计和样例支撑。 |
| G2 只做 fake/mock/test，不改真实实现 | 接受，继续作为 Gate 约束。 | 不改合同；写入 R21 签收模板。 |
| `/ws/session` 与旧 `/ws/audio` 隔离，R5.2.2 不继承 legacy 4001 | 接受，维持 no-legacy。 | 不改合同；已有设计和 close-code 合同支撑。 |

### 11.5 R21 签收包处理

生成 R21 G1 签收包，目的不是修改合同，而是把裁决意见带入端云二次签收内容。

R21 应包含：

1. `05_signoff/PROTOCOL_ARBITER_DECISION_20260804.md`：协议裁决意见。
2. `05_signoff/CLIENTS_OWNER_SIGNOFF_WITH_ARBITER_DECISION_20260804.md`：端侧二次签收模板，内含裁决意见。
3. `05_signoff/CLOUD_OWNER_SIGNOFF_WITH_ARBITER_DECISION_20260804.md`：云侧二次签收模板，内含裁决意见。
4. 更新后的 `README_HANDOFF.md`、快速签收说明和 owner checklist。
5. 包内 `00_review/` 包含本第 11 节。

### 11.6 Gate 口径

| Gate | 当前结论 | 放行条件 |
| --- | --- | --- |
| G1 技术材料 | 可用 R21 发起端云二次签收 | R21 只增加裁决意见与签收模板，合同基线不变。 |
| G1 三方签收 | 未关闭 | 童紫薇、王明辉、陈强确认本裁决并签同一个 manifest hash 后，提交评审组复核关闭。 |
| G2 mock/契约测试 | 暂不放行 | G1 签收关闭后，再由裁决者/评审组明确放行。 |
| SDK/Gateway/Agent 真实实现 | 不放行 | 仍需 G2/G3 通过和负责人批准。 |
