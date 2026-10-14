# SDK 下行 JSON 回调事件化重构详细计划

日期：2026-08-19  
适用范围：`clients/` 下 Python、Android、C、Matlab bridge SDK/示例/文档  
协议基线：`xiaoge-duplex-protocol-r5.2.2`  
参考归档包：`E:\Project\Project2026\AIAudioCloudPlatform\xiaogeV2\g2_archive_package_r2_r5_2_2_20260804`

## 1. 背景与目标

当前 SDK 的下行 JSON 回调主要以协议 payload 透传为主：

- Python：`on_clear(payload)` / `on_state(payload)` / `on_stt(payload)` / `on_reply(payload)` / `on_cmd(payload)` / `on_error(payload)`，payload 是 `dict`。
- Android：`onClear(JSONObject payload)` / `onStateUpdate(JSONObject payload)` / `onStt(JSONObject payload)` / `onReply(JSONObject payload)` / `onCmd(JSONObject payload)` / `onError(JSONObject payload)`。
- C：`on_clear(const char *json)` / `on_state(const char *json)` / `on_stt(const char *json)` / `on_reply(const char *json)` / `on_cmd(const char *json)` / `on_error(const char *json)`，直接返回 JSON 字符串。
- Matlab bridge：复用 Python SDK，将事件作为 JSON 行转发。

这种设计对协议调试很直接，但对 SDK 调用者不够友好。调用者最常见需求是显示文本、处理状态、清理播放、执行命令或响应错误，却必须理解协议字段、手动解析 JSON、处理字段缺省和类型。对 `on_cmd` 和 `on_error` 来说，裸 JSON 还会增加执行动作、ack/result、重试策略等业务错误风险。

本计划目标是：

1. 在不修改 R5.2.2 协议合同的前提下，将高层 SDK 下行 JSON 回调改为稳定、语义化、易调用的事件对象。
2. 优先将 `data.stt`、`data.reply`、`data.cmd`、`data.error` 建模为 `SttEvent`、`ReplyEvent`、`CommandEvent`、`ErrorEvent`。
3. 同步规划 `ctrl.clear`、`ctrl.state` 的 `ClearEvent`、`StateEvent`，并将 `ctrl.ready` 的 `ReadyEvent` 作为可选增强。
4. 保留原始 JSON payload/字符串作为高级调试、审计和协议兼容入口。
5. 分阶段兼容迁移，避免一次性破坏现有调用方。
6. 将 G2 归档包中的协议边界、字段枚举、禁止项带入 SDK 真实实现计划。

## 2. 参考协议与 G2 归档约束

### 2.1 归档包状态

参考包 `g2_archive_package_r2_r5_2_2_20260804` 的 `README_HANDOFF.md` 明确：

- G2 结论是 `G2_PASS_WITH_ACTIONS`。
- 可进入 G3 申请与方案准备。
- 该包不是 SDK/Gateway/Agent/sessproto/voice-cmd 真实实现编码放行包。
- 不得把本包当作真实实现编码放行包，不得绕过 G3 方案评审。

因此本文档是 SDK 修改方案计划，不等同于直接开工放行。真实编码仍需按项目流程获得批准。

### 2.2 合同版本与 manifest

归档合同信息：

- 合同版本：`xiaoge-duplex-protocol-r5.2.2`
- manifest SHA256：`845F0F4125061FF37A7F4DA20E0C88BC089200A08B319F1035D6522C80B56559`
- JSON text frame 上限：`8192` bytes
- binary PCM frame 上限：`32768` bytes
- P0 下行消息：`ctrl.ready`、`ctrl.state`、`ctrl.clear`、`data.stt`、`data.reply`、`data.cmd`、`data.error`
- P0 上行消息：`ctrl.hello`、`ctrl.frontend_state`、`data.cmd_ack`、`data.cmd_result`、必要的 `data.error/audit`

本计划只改 SDK 本地 API 形态，不新增协议字段、不新增协议枚举、不改变线上 WebSocket JSON 帧格式。

### 2.3 R5.2.2 `data.stt` 字段

根据归档包 `02_contracts/xiaoge-duplex-protocol-r5.2.2.schema.json`，`data.stt` 必填字段为：

| 字段 | 类型/约束 | 说明 | SDK 高层暴露建议 |
| --- | --- | --- | --- |
| `type` | const `data.stt` | 协议消息类型 | 不作为业务主字段；放在 `raw` 或 `message_type` |
| `trace_id` | string, non-empty | 链路追踪 | 暴露为可选/必有元数据 |
| `session_id` | string, non-empty | 会话 ID | 暴露为可选/必有元数据 |
| `utterance_id` | string, non-empty | 一轮语音/文本归属 | 暴露，高层重要 |
| `text` | string, non-empty | 用户 STT 文本 | 暴露，高层核心 |
| `final` | boolean | 是否最终识别结果 | 暴露，高层核心 |
| `ts_ms` | integer >= 0 | 服务端时间戳 | 暴露为元数据 |

`data.stt` 禁止额外字段（schema `additionalProperties: false`）。SDK 不能假设存在 `confidence`、`language`、`segment_id` 等未入合同字段。

### 2.4 R5.2.2 `data.reply` 字段

根据 schema，`data.reply` 必填字段为：

| 字段 | 类型/约束 | 说明 | SDK 高层暴露建议 |
| --- | --- | --- | --- |
| `type` | const `data.reply` | 协议消息类型 | 不作为业务主字段；放在 `raw` 或 `message_type` |
| `trace_id` | string, non-empty | 链路追踪 | 暴露为可选/必有元数据 |
| `session_id` | string, non-empty | 会话 ID | 暴露为可选/必有元数据 |
| `utterance_id` | string, non-empty | 与用户输入/命令关联 | 暴露，高层重要 |
| `intent_type` | enum | 意图类型 | 暴露，高层有业务价值 |
| `text` | string, non-empty | 助手回复文本 | 暴露，高层核心 |
| `ts_ms` | integer >= 0 | 服务端时间戳 | 暴露为元数据 |

`data.reply` 可选字段：

| 字段 | 枚举 | 说明 | SDK 高层暴露建议 |
| --- | --- | --- | --- |
| `speak_policy` | `silent` / `ack` / `ack_then_result` / `final_only` | 语音播报策略 | 暴露为可选元数据 |

注意：R5.2.2 `data.reply` **没有协议字段 `final`**。如果 SDK 为统一高层接口提供 `ReplyEvent.is_final`，它必须是 SDK 派生语义，默认 `true`，不能写回协议 payload，也不能误导为服务端字段。

### 2.5 `intent_type` 合同枚举

`data.reply.intent_type` 合同枚举为：

- `control_cmd`
- `info_query`
- `knowledge_qa`
- `chat`
- `config`
- `system`

SDK 可提供语言内枚举/常量，但必须与合同保持一致，未知值应按协议错误或原始 payload 路径处理，不能静默扩展新枚举。

### 2.6 `speak_policy` 合同枚举

`data.reply.speak_policy` 合同枚举为：

- `silent`
- `ack`
- `ack_then_result`
- `final_only`

这是助手回复的业务元信息，普通 UI 可以忽略，高级调用方可用于决定播报/展示策略。

### 2.7 G2 fake SDK 边界要求

归档包 `03_client_g2_evidence/CLIENT_G2_FAKE_SDK_DESIGN.md` 中强调：

- JSON 接收覆盖 `ctrl.ready`、`ctrl.state`、`ctrl.clear`、`data.stt`、`data.reply`、`data.cmd`、`data.error`。
- JSON 发送覆盖 `ctrl.frontend_state`、`data.cmd_ack`、`data.cmd_result`、必要的 `data.error/audit`。
- trace/id 只透传 `trace_id/session_id/utterance_id/cmd_id`，不生成历史批量字段。
- 不支持旧 `/ws/audio`。
- 不支持历史批量字段兼容。
- 不支持 `ctrl.status`、`ctrl.get`、`ctrl.set`、`ctrl.ack` 进入 G2 正向验收。
- 不新增字段、枚举、delivery、错误码或 close code。

SDK 重构必须继承这些边界。

## 3. 当前设计问题

### 3.1 调用者成本高

当前 README 和 demo 中的典型使用方式是：

```python
client.on_stt = lambda payload: print(payload.get("text"))
client.on_reply = lambda payload: print(payload.get("text"))
```

Android 中也是：

```java
String text = payload.optString("text", "");
```

这说明普通调用者最关心的是 `text`，以及 STT 的 `final`。直接暴露 payload 让调用方重复写解析逻辑。

### 3.2 SDK 层和协议层边界混淆

高层 SDK 应该提供稳定业务语义，协议 JSON 应该是底层传输细节。当前 `on_clear` / `on_state` / `on_stt` / `on_reply` / `on_cmd` / `on_error` 直接回调 JSON，使业务层需要知道协议字段、枚举、缺省值和兼容规则。

### 3.3 类型安全不足

- C 调用者需要手动解析 JSON 字符串。
- Android 调用者拿到 `JSONObject`，没有编译期字段检查。
- Python 调用者拿到 `dict[str, Any]`，IDE 补全和类型检查都弱。

### 3.4 兼容性边界不清

如果高层 API 直接暴露完整 JSON，调用者可能依赖任意字段。后续协议内部调整、字段收敛或 SDK 增加派生字段时，很容易变成破坏性变更。

## 4. 设计原则

1. **协议不变**：不改 WebSocket JSON 帧，不改 schema，不新增字段/枚举。
2. **高层易用**：普通用户通过 `event.text` / `event.is_final` 完成主要工作。
3. **原始可达**：高级用户仍可拿到 `raw` payload 或 raw JSON 字符串。
4. **强类型优先**：各语言尽量使用 dataclass/class/struct 表达 SDK 事件，包括文本、命令、错误、清理和状态事件。
5. **派生字段明确标注**：例如 `ReplyEvent.is_final=true` 是 SDK 派生，不是协议字段。
6. **生命周期清楚**：C 结构体内字符串有效期必须文档化。
7. **向后兼容迁移**：新增接口优先，旧接口保留并标记 deprecated。
8. **命令执行单入口**：`data.cmd` 不能因为新旧回调共存而被重复执行；raw 回调只作为观察/审计入口，推荐执行入口是 `CommandEvent`。
9. **raw 不可污染事件**：事件字段必须先从原始 payload 拷贝/冻结后再派发回调；任何 raw observer 或旧回调都不能影响已经构造的事件。
10. **本地错误与服务端错误分流**：服务端 `data.error` 才进入 `ErrorEvent`；JSON 解析失败、字段校验失败、网络或 callback 异常进入 SDK 本地 failure/protocol-error 通道。
11. **合同驱动测试**：事件对象构建必须由 R5.2.2 examples/schema 驱动测试。

## 5. 目标高层事件模型

### 5.1 事件化范围与优先级

本计划不只覆盖文本回调，而是覆盖所有下行 JSON 回调。按业务风险和易用性，推荐优先级如下：

| 回调 | 协议消息 | 当前高层问题 | 是否事件化 | 优先级 | 目标事件 |
| --- | --- | --- | --- | --- | --- |
| `on_stt` | `data.stt` | 调用者反复解析 `text/final` | 是 | P0 | `SttEvent` |
| `on_reply` | `data.reply` | 调用者反复解析 `text/intent_type/speak_policy` | 是 | P0 | `ReplyEvent` |
| `on_cmd` | `data.cmd` | 涉及动作执行、ack/result、风险等级，裸 JSON 容易误用 | 是 | P0 | `CommandEvent` |
| `on_error` | `data.error` | 涉及错误码、可重试策略，裸 JSON 容易忽略关键字段 | 是 | P0 | `ErrorEvent` |
| `on_clear` | `ctrl.clear` | 清理原因有业务语义，裸 JSON 不利于播放器/状态处理 | 是 | P1 | `ClearEvent` |
| `on_state` | `ctrl.state` | 状态枚举多，UI/状态机解析成本高 | 是 | P1 | `StateEvent` |
| `on_ready` | `ctrl.ready` | 当前 `sample_rate` 已好用，但缺少 caps/config 元数据 | 可选增强 | P2 | `ReadyEvent` |
| `on_audio` | binary PCM | PCM bytes 本身就是业务数据 | 否 | - | 保持 bytes/pcm |

### 5.2 通用事件元数据

建议所有 JSON 派生事件都携带公共元数据：

| 字段 | 类型 | 来源 | 说明 |
| --- | --- | --- | --- |
| `trace_id` | string | 协议字段 | 链路追踪 ID |
| `session_id` | string | 协议字段 | 会话 ID |
| `raw` | dict/JSONObject/JSON string | 原始 payload | 高级调试与审计 |

其中带轮次归属的事件还应携带 `utterance_id`，带时间戳的事件还应携带 `ts_ms`。

### 5.3 通用文本元数据

建议抽象出公共字段：

| 字段 | 类型 | 来源 | 说明 |
| --- | --- | --- | --- |
| `text` | string | 协议字段 | 文本内容 |
| `is_final` | boolean | STT 来自 `final`；Reply SDK 派生 | 是否最终文本 |
| `utterance_id` | string | 协议字段 | 一轮输入/回复关联 ID |
| `trace_id` | string | 协议字段 | 链路追踪 ID |
| `session_id` | string | 协议字段 | 会话 ID |
| `ts_ms` | integer | 协议字段 | 服务端毫秒时间戳 |
| `raw` | dict/JSONObject/JSON string | 原始 payload | 高级调试与审计 |

### 5.4 `SttEvent`

语义：用户语音识别文本事件。

字段：

| 字段 | 必需 | 来源 | 说明 |
| --- | --- | --- | --- |
| `text` | 是 | `data.stt.text` | 用户识别文本 |
| `is_final` | 是 | `data.stt.final` | 是否最终识别结果 |
| `utterance_id` | 是 | `data.stt.utterance_id` | 语音轮次 ID |
| `trace_id` | 是 | `data.stt.trace_id` | trace |
| `session_id` | 是 | `data.stt.session_id` | session |
| `ts_ms` | 是 | `data.stt.ts_ms` | 时间戳 |
| `raw` | 是/可选 | 原 payload | 原始协议 payload |

兼容别名：Python 可提供 `final` 属性映射到 `is_final`，贴合协议字段；Android/C 推荐命名 `isFinal`/`final` 视语言习惯。

### 5.5 `ReplyEvent`

语义：助手回复文本事件。

字段：

| 字段 | 必需 | 来源 | 说明 |
| --- | --- | --- | --- |
| `text` | 是 | `data.reply.text` | 助手回复文本 |
| `is_final` | 是 | SDK 派生，R5.2.2 恒为 `true` | 统一文本事件接口，不代表协议字段 |
| `utterance_id` | 是 | `data.reply.utterance_id` | 回复关联轮次 ID |
| `intent_type` | 是 | `data.reply.intent_type` | 意图类型 |
| `speak_policy` | 否 | `data.reply.speak_policy` | 播报策略 |
| `trace_id` | 是 | `data.reply.trace_id` | trace |
| `session_id` | 是 | `data.reply.session_id` | session |
| `ts_ms` | 是 | `data.reply.ts_ms` | 时间戳 |
| `raw` | 是/可选 | 原 payload | 原始协议 payload |

`ReplyEvent.is_final` 的文档必须写清楚：R5.2.2 reply 是整条回复事件，协议没有 `final` 字段；SDK 为便于 UI 统一处理文本事件而置为 `true`。

### 5.6 `CommandEvent`

语义：云侧下发给端侧执行的命令事件，对应 `data.cmd`。

字段：

| 字段 | 必需 | 来源 | 说明 |
| --- | --- | --- | --- |
| `cmd_id` | 是 | `data.cmd.cmd_id` | 命令唯一 ID，ack/result 必须引用 |
| `capability_id` | 是 | `data.cmd.capability_id` | 能力 ID |
| `action` | 是 | `data.cmd.action` | 动作名 |
| `params` | 是 | `data.cmd.params` | 动作参数对象 |
| `risk_level` | 是 | `data.cmd.risk_level` | `low` / `medium` / `high` |
| `ack_timeout_ms` | 是 | `data.cmd.ack_timeout_ms` | ack 超时时间 |
| `result_timeout_ms` | 是 | `data.cmd.result_timeout_ms` | result 超时时间 |
| `issued_at_ms` | 是 | `data.cmd.issued_at_ms` | 命令下发时间 |
| `utterance_id` | 是 | `data.cmd.utterance_id` | 关联用户轮次 |
| `trace_id` | 是 | `data.cmd.trace_id` | trace |
| `session_id` | 是 | `data.cmd.session_id` | session |
| `raw` | 是/可选 | 原 payload | 原始协议 payload |

`CommandEvent` 是除文本事件外最高优先级的事件化对象，因为它涉及真实动作执行、安全风险、重复命令处理和 ack/result 协议正确性。SDK 应配套提供基于 `CommandEvent` 的 ack/result helper，避免调用者手动从 JSON 复制 `trace_id/session_id/utterance_id/cmd_id`。

### 5.7 `ErrorEvent`

语义：服务端或协议层错误事件，对应 `data.error`。

字段：

| 字段 | 必需 | 来源 | 说明 |
| --- | --- | --- | --- |
| `code` | 是 | `data.error.code` | 合同错误码枚举 |
| `message` | 是 | `data.error.message` | 可展示/可记录错误信息 |
| `retryable` | 是 | `data.error.retryable` | 是否建议重试 |
| `ts_ms` | 是 | `data.error.ts_ms` | 时间戳 |
| `trace_id` | 是 | `data.error.trace_id` | trace |
| `session_id` | 是 | `data.error.session_id` | session |
| `raw` | 是/可选 | 原 payload | 原始协议 payload |

`data.error.code` 合同枚举为：`auth_failed`、`permission_denied`、`busy`、`protocol_error`、`capability_unsupported`、`token_expired`、`duplicate_connection`、`resource_exhausted`、`unknown_cmd_id`。SDK 应提供语言内常量/枚举，调用者不应手写字符串判断重试策略。

### 5.8 `ClearEvent`

语义：服务端要求端侧清理播放/状态，对应 `ctrl.clear`。

字段：

| 字段 | 必需 | 来源 | 说明 |
| --- | --- | --- | --- |
| `reason` | 否 | `ctrl.clear.reason` | `barge_in` / `user_stop` / `system_cancel` / `sleep` |
| `utterance_id` | 否 | `ctrl.clear.utterance_id` | 相关轮次 |
| `trace_id` | 是 | `ctrl.clear.trace_id` | trace |
| `session_id` | 是 | `ctrl.clear.session_id` | session |
| `raw` | 是/可选 | 原 payload | 原始协议 payload |

`ClearEvent` 对播放器清空、打断 UI、睡眠状态同步有意义。普通调用者可以只执行 `player.clear()`，高级调用者可以根据 `reason` 决定 UI 文案或统计口径。

### 5.9 `StateEvent`

语义：服务端状态更新，对应 `ctrl.state`。

字段：

| 字段 | 必需 | 来源 | 说明 |
| --- | --- | --- | --- |
| `link_state` | 是 | `ctrl.state.link_state` | `connecting` / `connected` / `reconnecting` / `closed` |
| `interaction_mode` | 是 | `ctrl.state.interaction_mode` | `sleeping` / `dialogue` / `listening` |
| `engine_gate` | 是 | `ctrl.state.engine_gate` | `closed` / `open` / `kws_only` |
| `resource_state` | 是 | `ctrl.state.resource_state` | `SleepingHot` / `SleepingWarm` / `ActiveAgent` / `ReleasedIdle` / `PendingReconnect` |
| `pending_confirmation` | 否 | `ctrl.state.pending_confirmation` | 等待确认上下文 |
| `ts_ms` | 是 | `ctrl.state.ts_ms` | 时间戳 |
| `trace_id` | 是 | `ctrl.state.trace_id` | trace |
| `session_id` | 是 | `ctrl.state.session_id` | session |
| `raw` | 是/可选 | 原 payload | 原始协议 payload |

`StateEvent` 对 UI 状态展示、睡眠/唤醒状态展示、重连提示和资源状态监控有价值。Android 侧需避免与已有本地 `SessionState` 命名混淆，可命名为 `ServerStateEvent` 或 `StateUpdateEvent`。

### 5.10 `ReadyEvent` 可选增强

`ctrl.ready` 当前在高层通常只暴露 `sample_rate`，这对普通音频链路已足够。若需要更完整元数据，可新增可选事件：

| 字段 | 必需 | 来源 | 说明 |
| --- | --- | --- | --- |
| `sample_rate` | 是 | `ctrl.ready.sample_rate` | 协议音频采样率 |
| `granted_caps` | 是 | `ctrl.ready.granted_caps` | 服务端授予能力 |
| `config_version` | 是 | `ctrl.ready.config_version` | 配置版本 |
| `trace_id` | 是 | `ctrl.ready.trace_id` | trace |
| `session_id` | 是 | `ctrl.ready.session_id` | session |
| `raw` | 是/可选 | 原 payload | 原始协议 payload |

建议保留现有 `on_ready(sample_rate)`，只新增 `on_ready_event(ReadyEvent)`，避免破坏简洁主路径。

### 5.11 原始协议事件

保留原始协议事件入口：

| 事件 | 用途 |
| --- | --- |
| `on_json(payload)` / `on_raw_json(json)` | 所有下行 JSON 统一观察，用于日志/调试；不得作为命令执行入口 |
| `on_clear_raw(payload/json)` | 仅观察原始 `ctrl.clear` |
| `on_state_raw(payload/json)` | 仅观察原始 `ctrl.state` |
| `on_stt_raw(payload/json)` | 仅观察原始 `data.stt` |
| `on_reply_raw(payload/json)` | 仅观察原始 `data.reply` |
| `on_cmd_raw(payload/json)` | 仅观察原始 `data.cmd` |
| `on_error_raw(payload/json)` | 仅观察原始 `data.error` |

原始接口不作为 README 的主推荐用法，但必须文档化为高级用途。命令类 raw 回调尤其只能推荐用于日志、审计和协议调试；真实动作执行应只绑定 `CommandEvent`/`on_command` 或旧 `on_cmd` 二者之一，SDK demo 不得同时在两个入口执行命令。

### 5.12 raw payload 不可变性与派发隔离

事件构造必须在调用任何用户回调之前完成，且事件字段应从原始 payload 中拷贝为语言内不可变/只读字段。原因是 Python `dict` 和 Android `JSONObject` 都是可变对象，如果先调用 `on_json` 或旧 raw 回调，再构造事件，会让用户代码有机会污染后续 `event.raw` 或业务字段。

推荐策略：

- Python：事件字段是 frozen dataclass 字段；`event.raw` 默认保存 deep copy 或只读 mapping。若未来为了内存提供 `retain_raw_payload=False`，所有事件的 `raw` 规则必须一致。
- Android：事件类字段均为 `final`；`raw` 使用 defensive copy，例如从原始 `JSONObject` 复制生成，避免 listener 修改原对象影响事件。
- C：事件字段指针和 `raw_json` 只在回调返回前有效；调用方长期保存必须自行复制。

推荐派发模板为：

1. 解析 JSON。
2. 对已知 `type` 做本地轻量校验。
3. 构造事件对象或本地错误对象。
4. 调用全局 raw observer。
5. 调用兼容旧 raw 回调。
6. 调用新事件回调。
7. 调用便捷文本回调。

这样 raw observer 可观察协议 payload，但不能改变本次事件对象。

## 6. Python SDK 修改计划

### 6.1 目标 API

新增 dataclass：

```python
@dataclass(frozen=True, slots=True)
class SttEvent:
    text: str
    is_final: bool
    utterance_id: str
    trace_id: str
    session_id: str
    ts_ms: int
    raw: JsonObject

    @property
    def final(self) -> bool:
        return self.is_final


@dataclass(frozen=True, slots=True)
class ReplyEvent:
    text: str
    is_final: bool
    utterance_id: str
    intent_type: str
    trace_id: str
    session_id: str
    ts_ms: int
    raw: JsonObject
    speak_policy: str | None = None
```

建议回调字段：

```python
client.on_ready_event: Callable[[ReadyEvent], None] | None
client.on_clear_event: Callable[[ClearEvent], None] | None
client.on_state_event: Callable[[StateEvent], None] | None
client.on_stt_event: Callable[[SttEvent], None] | None
client.on_reply_event: Callable[[ReplyEvent], None] | None
client.on_command: Callable[[CommandEvent], None] | None
client.on_error_event: Callable[[ErrorEvent], None] | None
client.on_stt_text: Callable[[str, bool], None] | None
client.on_reply_text: Callable[[str], None] | None
client.on_json: Callable[[JsonObject], None] | None
client.on_protocol_error: Callable[[ProtocolErrorEvent], None] | None
client.on_failure: Callable[[Exception], None] | None
```

旧接口保留：

```python
client.on_clear: Callable[[JsonObject], None] | None     # deprecated/raw
client.on_state: Callable[[JsonObject], None] | None     # deprecated/raw
client.on_stt: Callable[[JsonObject], None] | None       # deprecated/raw
client.on_reply: Callable[[JsonObject], None] | None     # deprecated/raw
client.on_cmd: Callable[[JsonObject], None] | None       # deprecated/raw
client.on_error: Callable[[JsonObject], None] | None     # deprecated/raw
```

### 6.2 派发顺序

收到 `data.stt`：

1. 解析 JSON。
2. 可选：运行本地轻量字段校验，确认必填字段存在。
3. 构造 `SttEvent`。
4. 调用 `on_json(payload)`。
5. 调用 `on_stt_raw(payload)`，如提供。
6. 调用旧 `on_stt(payload)`，如提供。
7. 调用新 `on_stt_event(event)`，如提供。
8. 调用便捷 `on_stt_text(event.text, event.is_final)`，如提供。

收到 `data.reply`：

1. 解析 JSON。
2. 构造 `ReplyEvent(is_final=True, ...)`。
3. 调用 `on_json(payload)`。
4. 调用 `on_reply_raw(payload)`，如提供。
5. 调用旧 `on_reply(payload)`，如提供。
6. 调用新 `on_reply_event(event)`，如提供。
7. 调用便捷 `on_reply_text(event.text)`，如提供。

说明：为了兼容旧行为，阶段 1 中旧文本接口仍然照常回调。若担心重复副作用，文档应提醒调用者不要同时注册旧接口和新便捷接口处理同一业务。

对 `data.cmd` 需要更严格：

- `on_json` / `on_cmd_raw` / 旧 `on_cmd(payload)` 可保留为兼容观察入口。
- 新代码推荐只在 `on_command(CommandEvent)` 中执行动作。
- SDK demo 和文档不得同时在旧 `on_cmd` 与新 `on_command` 中执行同一命令。
- 如果 SDK 增加 `prefer_event_callbacks` 或类似配置，则当 `on_command` 已注册时，旧 `on_cmd` 应降级为 raw observer 或不再作为执行入口。

阶段 1 还必须提供基于 `CommandEvent` 的 ack/result helper，避免业务方继续把 raw JSON 当作命令上下文传来传去：

```python
await client.send_command_ack(event, status="accepted", code="ok", message="queued")
await client.send_command_result(
    event,
    status="succeeded",
    code="ok",
    message="done",
    retryable=False,
)
```

helper 只从 `CommandEvent.trace_id/session_id/utterance_id/cmd_id` 派生上行关联字段，不允许调用方手写这些字段。

### 6.3 代码落点

主要文件：

- `clients/python/xiaoge_client.py`
- `clients/python/PYTHON_SDK_README.md`
- `clients/python/demo_file.py`
- `clients/python/demo_mic.py`
- `clients/python/selftest.py`
- 相关 package/rebuild 脚本中内嵌 README 模板，如 `clients/package_python_sdk.py`

### 6.4 Python 兼容策略

阶段 1：

- 新增 `SttEvent` / `ReplyEvent` / `CommandEvent` / `ErrorEvent`。
- 新增 `ProtocolErrorEvent` 或等价本地错误通道，用于 JSON/字段校验失败，不与服务端 `data.error` 混用。
- 新增 `on_stt_event` / `on_reply_event` / `on_command` / `on_error_event`。
- 新增 `on_stt_text` / `on_reply_text` 便捷文本回调。
- 新增 `send_command_ack(CommandEvent, ...)` / `send_command_result(CommandEvent, ...)`。
- 保留 `on_stt(payload)` / `on_reply(payload)` / `on_cmd(payload)` / `on_error(payload)`。
- README 主示例改用新接口，但保留旧接口小节。

阶段 2：

- 新增 `ClearEvent` / `StateEvent`，可选新增 `ReadyEvent`。
- 新增 `on_clear_event` / `on_state_event` / `on_ready_event`。
- 旧 JSON payload 接口文档标记 deprecated/raw。
- 在 debug log 中提示旧接口未来会迁移，但不在运行时频繁 warning，避免污染实时场景日志。

阶段 3：

- 下一个主版本才考虑把 `on_stt` / `on_reply` / `on_cmd` / `on_error` 等主名称切换为事件对象，或继续永久保留 payload 版本但命名为 raw API。

## 7. Android SDK 修改计划

### 7.1 目标 API

Android 真实代码中当前存在两套 listener 形态：`XiaogeClient.Listener` 内嵌接口和独立 `XiaogeSessionListener`。进入编码前必须先冻结主 API：

- 如果 `XiaogeClient.Listener` 仍是实际主路径，则新事件方法必须加到该接口，并由 `XiaogeClient` dispatch 调用。
- 如果希望统一到 `XiaogeSessionListener`，必须提供 adapter 或迁移层，避免文档更新但主 client 仍只调用旧内嵌 listener。
- `onState(JSONObject)` 与 `onStateUpdate(JSONObject)` 的命名差异必须收敛；建议新接口统一为 `onStateEvent(StateEvent event)`，旧接口按所在 listener 保持兼容。
- `onReady(int sampleRate)` 与 `onReady(int sampleRate, boolean reconnected)` 的差异也必须收敛；`reconnected` 若作为 SDK 派生语义，应放入 `ReadyEvent` 并标注不是 R5.2.2 协议字段。

以下 API 示例以统一后的主 listener 为准。

新增 Java 类：

```java
public final class SttEvent {
    public final String text;
    public final boolean isFinal;
    public final String utteranceId;
    public final String traceId;
    public final String sessionId;
    public final long tsMs;
    public final JSONObject raw; // defensive copy
}

public final class ReplyEvent {
    public final String text;
    public final boolean isFinal; // R5.2.2 SDK-derived, always true
    public final String utteranceId;
    public final String intentType;
    public final String speakPolicy; // nullable
    public final String traceId;
    public final String sessionId;
    public final long tsMs;
    public final JSONObject raw;
}
```

`XiaogeSessionListener` 新增：

```java
default void onReadyEvent(ReadyEvent event) {}
default void onClearEvent(ClearEvent event) {}
default void onStateEvent(StateEvent event) {}
default void onSttEvent(SttEvent event) {}
default void onReplyEvent(ReplyEvent event) {}
default void onCommand(CommandEvent event) {}
default void onErrorEvent(ErrorEvent event) {}
default void onTextEvent(TextEvent event) {} // 可选统一抽象，非必须
default void onJson(JSONObject payload) {}
default void onProtocolError(ProtocolErrorEvent event) {}
```

旧接口保留：

```java
default void onClear(JSONObject payload) {}
default void onStateUpdate(JSONObject payload) {}
default void onStt(JSONObject payload) {}
default void onReply(JSONObject payload) {}
default void onCmd(JSONObject payload) {}
default void onError(JSONObject payload) {}
```

如果希望 API 更自然，也可以在下一个大版本将 `onStt(SttEvent event)` 作为主接口，把旧接口改名为 `onSttPayload(JSONObject payload)`；但阶段 1 不建议直接破坏。

### 7.2 代码落点

主要文件：

- `clients/android/xiaoge-sdk-core/src/main/java/com/xiaoge/client/XiaogeSessionListener.java`
- `clients/android/xiaoge-sdk-core/src/main/java/com/xiaoge/client/XiaogeClient.java`
- 新增：`SttEvent.java`、`ReplyEvent.java`、`CommandEvent.java`、`ErrorEvent.java`、`ClearEvent.java`、`StateEvent.java`，可选 `ReadyEvent.java`、`TextEvent.java`
- `clients/android/demo/src/main/java/com/xiaoge/client/demo/MainActivity.java`
- `clients/android/docs/ANDROID_SDK_README.md`
- `clients/android/xiaoge-sdk-core/src/test/java/com/xiaoge/client/...`

### 7.3 Android 派发顺序

收到 `data.stt`：

1. `JSONObject payload = new JSONObject(text)`。
2. 构造 `SttEvent.fromJson(payload)`。
3. `listener.onJson(payload)`。
4. `listener.onStt(payload)` 旧接口。
5. `listener.onSttEvent(event)` 新接口。

收到 `data.reply`：

1. 构造 `ReplyEvent.fromJson(payload)`。
2. `listener.onJson(payload)`。
3. `listener.onReply(payload)` 旧接口。
4. `listener.onReplyEvent(event)` 新接口。

### 7.4 Android 线程要求

现有文档说明回调运行在 SDK 内部线程，UI 更新必须切主线程。新事件接口沿用同一线程模型，不能引入额外线程切换语义变化。

事件对象必须在线程切换前完成字段拷贝：`SttEvent.fromJson(payload)` / `ReplyEvent.fromJson(payload)` 等应复制字符串、数字、布尔值，并为 `raw` 创建 defensive copy。listener 若修改旧 `JSONObject payload`，不得影响已构造事件。

### 7.5 Android 命令 helper

新增基于事件的命令回执接口，旧 `sendCmdAck(JSONObject, ...)` / `sendCmdResult(JSONObject, ...)` 保留为 raw 兼容入口：

```java
client.sendCommandAck(event, CmdAckStatus.ACCEPTED, "ok", "queued");
client.sendCommandResult(event, CmdResultStatus.SUCCEEDED, "ok", "done", false);
```

这些 helper 只从 `CommandEvent` 复制 `traceId/sessionId/utteranceId/cmdId`。Android demo 中真实执行动作只应绑定 `onCommand(CommandEvent)` 或旧 `onCmd(JSONObject)` 之一。

## 8. C SDK 修改计划

### 8.1 目标 API

C SDK 当前最需要改善，因为调用者拿到的是 JSON 字符串。建议新增结构体回调，同时保留 raw JSON 回调。

```c
typedef struct {
    const char *text;
    int final;
    const char *utterance_id;
    const char *trace_id;
    const char *session_id;
    long long ts_ms;
    const char *raw_json;
} xiaoge_stt_event;

typedef struct {
    const char *text;
    int final;                 /* SDK-derived for R5.2.2 data.reply, always 1 */
    const char *utterance_id;
    const char *intent_type;
    const char *speak_policy;   /* nullable */
    const char *trace_id;
    const char *session_id;
    long long ts_ms;
    const char *raw_json;
} xiaoge_reply_event;
```

`CommandEvent` 必须同时带上命令回执所需的全部关联字段：

```c
typedef struct {
    const char *cmd_id;
    const char *capability_id;
    const char *action;
    const char *params_json;
    const char *risk_level;
    long long ack_timeout_ms;
    long long result_timeout_ms;
    long long issued_at_ms;
    const char *utterance_id;
    const char *trace_id;
    const char *session_id;
    const char *raw_json;
} xiaoge_command_event;
```

### 8.2 C ABI 策略

现有 `xiaoge_callbacks` 结构体已经被 demo 和下游以位置初始化方式使用，且 `void *user` 位于末尾。不得在原结构体中间插入新函数指针，也不建议依赖“追加字段”作为二进制兼容方案。

推荐新增 `xiaoge_callbacks_v2`，并使用 `struct_size` 支持未来扩展：

```c
typedef struct {
    size_t struct_size;

    void (*on_ready)(int sample_rate, void *user);
    void (*on_audio)(const void *pcm, size_t len, void *user);

    /* Deprecated/raw protocol callbacks. */
    void (*on_clear)(const char *json, void *user);
    void (*on_state)(const char *json, void *user);
    void (*on_stt)(const char *json, void *user);
    void (*on_reply)(const char *json, void *user);
    void (*on_cmd)(const char *json, void *user);
    void (*on_error)(const char *json, void *user);

    /* New high-level callbacks. */
    void (*on_ready_event)(const xiaoge_ready_event *event, void *user);
    void (*on_clear_event)(const xiaoge_clear_event *event, void *user);
    void (*on_state_event)(const xiaoge_state_event *event, void *user);
    void (*on_stt_event)(const xiaoge_stt_event *event, void *user);
    void (*on_reply_event)(const xiaoge_reply_event *event, void *user);
    void (*on_command)(const xiaoge_command_event *event, void *user);
    void (*on_error_event)(const xiaoge_error_event *event, void *user);

    /* SDK-local failure/protocol-error callback; not a server data.error. */
    void (*on_failure)(int code, const char *message, void *user);

    void *user;
} xiaoge_callbacks_v2;
```

并新增 v2 create API：

```c
xiaoge_client *xiaoge_create_from_session_v2(const xiaoge_config *cfg,
                                             const xiaoge_session *session,
                                             int insecure,
                                             const xiaoge_callbacks_v2 *cb);
```

旧 `xiaoge_create_from_session(..., const xiaoge_callbacks *cb)` 保持 ABI/source 兼容，只分发 raw 回调。新 demo 应改用指定初始化器，避免未来结构体扩展再次产生位置初始化错位。

### 8.2.1 C 命令回执 helper

新增基于 `xiaoge_command_event` 的 helper，避免调用者继续传 raw JSON：

```c
int xiaoge_send_cmd_ack_event(xiaoge_client *c,
                              const xiaoge_command_event *event,
                              const char *status,
                              const char *code,
                              const char *message);

int xiaoge_send_cmd_result_event(xiaoge_client *c,
                                 const xiaoge_command_event *event,
                                 const char *status,
                                 const char *code,
                                 const char *message,
                                 int retryable);
```

旧 `xiaoge_send_cmd_ack(..., const char *cmd_json, ...)` / `xiaoge_send_cmd_result(..., const char *cmd_json, ...)` 保留为 raw 兼容入口。

`auto_fake_cmd_executor` 与新 `on_command` 必须互斥定义清楚：如果用户注册 `on_command`，demo 和默认配置不得再自动发送 fake ack/result，除非调用方显式启用 fake executor。

### 8.3 C 字符串生命周期

必须文档化：

- `xiaoge_stt_event*`、`xiaoge_reply_event*`、`xiaoge_command_event*`、`xiaoge_error_event*`、`xiaoge_clear_event*`、`xiaoge_state_event*`、`xiaoge_ready_event*` 只在回调函数返回前有效。
- `event->text` 等字符串指针指向 SDK 内部临时解析缓冲区或 raw JSON buffer。
- 调用者若需异步保存，必须自行 `strdup`。

### 8.4 C 解析策略

当前 C SDK 已经有 `json_type_is` 这类轻量判断。新增事件对象需要解析字段。可选方案：

- 短期：实现最小 JSON 字段提取器，只支持合同字段字符串/布尔/整数，避免引入重依赖。
- 长期：引入受控 JSON parser，例如 cJSON/jsmn，但需要评估交付包体积、许可证、Windows/Android NDK 构建影响。

建议短期采用合同驱动最小提取器，字段缺失时仍调用 raw 旧接口，并通过 `on_error` 或日志报告协议错误。

## 9. Matlab bridge 修改计划

Matlab bridge 当前通过 Python SDK 接收事件并转发。建议：

1. Python bridge 注册新的 `on_stt_event` / `on_reply_event` / `on_command` / `on_error_event`，并逐步接入 `on_clear_event` / `on_state_event`。
2. 对 Matlab event socket 输出更友好的 JSON 行，例如：

```json
{"event":"stt","text":"往前走一米","final":true,"utterance_id":"utt-0001","ts_ms":1789000000100}
```

```json
{"event":"reply","text":"好的，正在执行。","final":true,"intent_type":"control_cmd","speak_policy":"ack_then_result","utterance_id":"utt-0001","ts_ms":1789000000700}
```

3. 同时保留 `raw` 字段或另开 raw event 模式，供调试使用。
4. Matlab demo 优先展示 `event.text`，不再要求用户理解 R5.2.2 payload。

## 10. 协议校验与错误处理

### 10.1 入站字段校验

SDK 应至少校验事件构造所需字段。P0 阶段优先覆盖 `data.stt`、`data.reply`、`data.cmd`、`data.error`，P1 阶段覆盖 `ctrl.clear`、`ctrl.state`，P2 阶段可选覆盖 `ctrl.ready`。

`data.stt`：

- `type == "data.stt"`
- `text` 是非空字符串
- `final` 是 boolean
- `utterance_id` / `trace_id` / `session_id` 是非空字符串
- `ts_ms` 是非负整数

`data.reply`：

- `type == "data.reply"`
- `text` 是非空字符串
- `intent_type` 属于合同枚举
- `speak_policy` 如存在，属于合同枚举
- `utterance_id` / `trace_id` / `session_id` 是非空字符串
- `ts_ms` 是非负整数

`data.cmd`：

- `type == "data.cmd"`
- `cmd_id` / `capability_id` / `action` 是非空字符串
- `params` 是 object
- `risk_level` 属于 `low/medium/high`
- `ack_timeout_ms` / `result_timeout_ms` 是正整数
- `issued_at_ms` 是非负整数
- `utterance_id` / `trace_id` / `session_id` 是非空字符串

`data.error`：

- `type == "data.error"`
- `code` 属于合同错误码枚举
- `message` 是非空字符串
- `retryable` 是 boolean
- `ts_ms` 是非负整数
- `trace_id` / `session_id` 是非空字符串

`ctrl.clear`：

- `type == "ctrl.clear"`
- `trace_id` / `session_id` 是非空字符串
- `utterance_id` 如存在是非空字符串
- `reason` 如存在属于 `barge_in/user_stop/system_cancel/sleep`

`ctrl.state`：

- `type == "ctrl.state"`
- `link_state` / `interaction_mode` / `engine_gate` / `resource_state` 属于合同枚举
- `pending_confirmation` 如存在是 object
- `ts_ms` 是非负整数
- `trace_id` / `session_id` 是非空字符串

### 10.2 校验失败策略

建议分层处理：

1. JSON 无法解析：调用 SDK 本地 failure/protocol-error 通道，不调用任何协议事件回调。
2. `type` 未知：忽略或 debug log，保持现有行为；可调用 raw observer 供调试。
3. 已知 `type` 但字段缺失/类型错误：
   - 调用 raw JSON 观察回调，便于排查。
   - 调用 SDK 本地 `on_protocol_error` / `onFailure` / `on_failure` 报告协议错误。
   - 不构造高层事件，避免给业务层错误对象。
4. 服务端正常下发的 `data.error`：构造 `ErrorEvent` 并调用 `on_error_event`，这是业务/协议错误，不等同于 SDK 本地异常。
5. `data.reply` 没有 `speak_policy`：合法，事件字段为 `None/null`。

不得把本地校验失败伪造成服务端 `data.error`，否则调用方会误判重试策略、错误来源和 trace 归属。

### 10.3 JSON frame size

SDK 不应因为引入事件对象而改变 R5.2.2 transport 限制：

- 下行 JSON text frame 最大 `8192` bytes。
- 上行 JSON 发送前仍需按 compact UTF-8 bytes 校验。
- 事件对象不参与网络序列化，不应被误用为协议 payload。

## 11. 文档修改计划

### 11.1 Python README

主示例改为：

```python
client.on_stt_event = lambda e: print("stt", e.text, e.is_final)
client.on_reply_event = lambda e: print("reply", e.text, e.intent_type)
```

增加章节：

- 文本事件对象字段表。
- 命令、错误、清理、状态事件对象字段表。
- `ReplyEvent.is_final` 是 SDK 派生字段，R5.2.2 协议没有 `data.reply.final`。
- raw payload 接口适用于调试和协议审计。
- 旧 `on_clear(payload)` / `on_state(payload)` / `on_stt(payload)` / `on_reply(payload)` / `on_cmd(payload)` / `on_error(payload)` 已不推荐。

### 11.2 Android README

主示例改为：

```java
@Override
public void onSttEvent(SttEvent event) {
    String text = event.text;
}

@Override
public void onReplyEvent(ReplyEvent event) {
    String text = event.text;
}
```

并说明 UI 线程切换要求不变。

### 11.3 C README

增加：

- `xiaoge_stt_event` / `xiaoge_reply_event` / `xiaoge_command_event` / `xiaoge_error_event` / `xiaoge_clear_event` / `xiaoge_state_event` 字段说明。
- 字符串生命周期。
- raw JSON 旧接口和新事件接口的关系。
- ABI/版本迁移说明。

### 11.4 协议文档引用

`clients/PROTOCOL.md` 可补充一个 SDK API 注释小节，说明：

- 协议 payload 仍是 R5.2.2 schema。
- SDK 高层事件是本地语言 binding，不属于协议字段扩展。

## 12. 测试计划

### 12.1 合同样例驱动测试

使用归档包 examples：

- `data.stt.valid`
- `data.reply.valid`
- `data.reply.multi_command_blocked.ask_split`
- `data.reply.max_json_transport`
- `data.cmd.valid`
- `data.error.unknown_cmd_id.valid`
- `ctrl.clear.valid`
- `ctrl.state.valid`
- `ctrl.ready.valid`

验证：

- 能从 `data.stt.valid` 构造 `SttEvent`。
- `SttEvent.text == "往前走一米"`。
- `SttEvent.is_final == true`。
- `SttEvent.utterance_id == "utt-0001"`。
- 能从 `data.reply.valid` 构造 `ReplyEvent`。
- `ReplyEvent.intent_type == "control_cmd"`。
- `ReplyEvent.speak_policy == "ack_then_result"`。
- 对没有 `speak_policy` 的 reply，字段为 null/None。
- `ReplyEvent.is_final == true` 且不写入 raw payload。
- 能从 `data.cmd.valid` 构造 `CommandEvent`，并保留 `cmd_id/action/risk_level/timeout`。
- 能从 `data.error.unknown_cmd_id.valid` 构造 `ErrorEvent`，并保留 `code/retryable/message`。
- 能从 `ctrl.clear.valid` 构造 `ClearEvent`，并保留 `reason/utterance_id`。
- 能从 `ctrl.state.valid` 构造 `StateEvent`，并保留四类状态枚举。
- 可选：能从 `ctrl.ready.valid` 构造 `ReadyEvent`，并保留 `sample_rate/granted_caps/config_version`。

### 12.2 负例测试

从 examples/schema 中补充或构造：

- `data.stt` 缺少 `final`：不得构造 `SttEvent`。
- `data.stt.final` 是字符串：不得构造 `SttEvent`。
- `data.reply.intent_type` 非枚举：不得构造 `ReplyEvent`。
- `data.reply.speak_policy` 非枚举：不得构造 `ReplyEvent`。
- `data.cmd` 缺少 `cmd_id`：不得构造 `CommandEvent`。
- `data.cmd.risk_level` 非枚举：不得构造 `CommandEvent`。
- `data.error.code` 非合同错误码：不得构造 `ErrorEvent`。
- `ctrl.clear.reason` 非枚举：不得构造 `ClearEvent`。
- `ctrl.state.link_state` 等状态字段非枚举：不得构造 `StateEvent`。

### 12.3 兼容测试

- 只注册旧 `on_stt(payload)` / `on_cmd(payload)` / `on_error(payload)`：行为与修改前一致。
- 只注册新 `on_stt_event(event)` / `on_command(event)` / `on_error_event(event)`：正常收到事件。
- 同时注册旧接口和新接口：两者按文档顺序收到回调。
- raw payload 与 event.raw 内容一致，但 raw observer 修改 payload 不影响 event 字段。
- `ReplyEvent.is_final` 不写入 raw payload。
- callback 抛异常时 SDK 通过本地 failure 通道报告，连接/dispatch 策略符合现有行为。
- 命令同时注册 raw 和 event 回调时，demo/fake executor 不会重复 ack/result。
- demo 使用新接口仍能运行。

### 12.4 各语言测试落点

Python：

- 更新 `clients/python/selftest.py`。
- 增加 dataclass 构造单测。
- 使用 `uv run pytest` 或项目现有 client selftest 命令。

Android：

- 更新 `ProtocolCodecTest` 或新增 `TextEventTest`。
- 验证 `SttEvent.fromJson` / `ReplyEvent.fromJson` / `CommandEvent.fromJson` / `ErrorEvent.fromJson`，P1 再验证 `ClearEvent.fromJson` / `StateEvent.fromJson`。
- demo 编译通过。

C：

- 增加最小 JSON 字段提取测试。
- 增加 callback dispatch 测试。
- 覆盖字符串生命周期文档示例。

Matlab bridge：

- smoke 测试 event socket 输出新字段。
- 保留 raw 模式兼容测试。

## 13. 分阶段实施计划

### 阶段 0：评审确认

产出：

- 本计划文档。
- SDK API 命名确认：`on_stt_event` / `on_reply_event` / `on_command` / `on_error_event` 是否作为阶段 1 新接口。
- G3/真实编码许可确认。

退出条件：

- Clients owner 确认 API 命名。
- Protocol arbiter 确认未修改 R5.2.2 协议。
- 若涉及 C ABI，确认版本策略。

### 阶段 1：Python SDK 先行

原因：Python 实现成本最低，且 Matlab bridge 可复用。

任务：

1. 新增 `SttEvent` / `ReplyEvent` / `CommandEvent` / `ErrorEvent` dataclass。
2. 新增本地 `ProtocolErrorEvent` 或等价 failure 通道。
3. 新增事件构造 helper，例如 `_stt_event_from_payload()` / `_reply_event_from_payload()` / `_command_event_from_payload()` / `_error_event_from_payload()`。
4. 新增 `send_command_ack(CommandEvent, ...)` / `send_command_result(CommandEvent, ...)`。
5. 在 dispatch 中先构造不可变/隔离事件，再调用 raw/旧/新回调。
6. 保留旧 payload 回调。
7. 更新 Python README、demo、selftest。

验收：

- Python selftest 通过。
- 旧 demo 兼容，新 demo 使用事件对象。
- `data.reply` raw payload 不出现 SDK 派生 `final` 字段。

### 阶段 2：Android SDK

任务：

1. 冻结并收敛 Android 主 listener：明确 `XiaogeClient.Listener` 与 `XiaogeSessionListener` 的关系，提供 adapter 或统一主入口。
2. 新增 `SttEvent.java` / `ReplyEvent.java` / `CommandEvent.java` / `ErrorEvent.java`，P1 可继续新增 `ClearEvent.java` / `StateEvent.java`。
3. 主 listener 增加新默认方法。
4. 新增 `sendCommandAck(CommandEvent, ...)` / `sendCommandResult(CommandEvent, ...)`。
5. `XiaogeClient` dispatch 中先构造 defensive-copy 事件并回调。
6. 更新 Android demo 和 README。
7. 增加 Java 单测。

验收：

- Android core tests 通过。
- Demo UI 不再直接解析 `JSONObject` 的 `text`，命令和错误处理也优先使用事件对象。
- 旧 `onStt(JSONObject)` / `onReply(JSONObject)` / `onCmd(JSONObject)` / `onError(JSONObject)` 仍可用。

### 阶段 3：C SDK

任务：

1. 采用 `xiaoge_callbacks_v2` + `struct_size`，旧 `xiaoge_callbacks` 保持 ABI/source 兼容。
2. 新增 `xiaoge_stt_event` / `xiaoge_reply_event` / `xiaoge_command_event` / `xiaoge_error_event`，P1 可继续新增 `xiaoge_clear_event` / `xiaoge_state_event`。
3. 新增 `xiaoge_send_cmd_ack_event` / `xiaoge_send_cmd_result_event`。
4. 增加合同字段解析。
5. Dispatch 中同时支持 raw JSON 和 event 回调，并定义 `auto_fake_cmd_executor` 与 `on_command` 的互斥关系。
6. 更新 C README 和示例；新示例使用指定初始化器。

验收：

- C e2e/smoke 通过。
- 旧 raw JSON 回调仍可工作。
- 新 event 回调无需业务侧 JSON 解析即可拿到 text/final/intent/cmd/error。

### 阶段 4：P1/P2 事件补齐与 Matlab bridge / 交付包脚本

任务：

1. 补齐 `ClearEvent` / `StateEvent`，可选补齐 `ReadyEvent`。
2. Bridge 使用 Python 新事件接口。
3. Matlab event socket 输出高层文本、命令、错误、清理、状态事件。
4. 更新 Matlab README/demo。
5. 更新 package/rebuild 脚本内嵌文档。

验收：

- Matlab bridge smoke 通过。
- 下游仍可选择 raw event 模式。

### 阶段 5：弃用旧接口与版本化

任务：

1. 文档明确旧接口 deprecated。
2. CHANGELOG 记录迁移方式。
3. 下个主版本再决定是否重命名主接口。

建议版本：

- 当前兼容新增：`r5.2.2+sdk-event-api` 或 SDK 自身 minor bump。
- 破坏性切换：留到下一个 major/minor 协议外 SDK 版本，不与 R5.2.2 协议版本混淆。

## 14. 风险与缓解

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 调用者同时注册新旧接口导致重复处理 | UI 重复显示文本或命令重复处理 | 文档说明派发顺序；demo 只用一种接口；命令执行建议只绑定 `on_command` 或旧 `on_cmd` 之一 |
| `on_cmd` 仍由调用者手写 JSON 解析 | 动作误执行、ack/result 关联错误 | P0 引入 `CommandEvent` 和基于 event 的 ack/result helper |
| `on_error` 仍由调用者手写 JSON 解析 | 忽略 `retryable` 或错误码导致错误重试策略 | P0 引入 `ErrorEvent` 和错误码常量 |
| `ReplyEvent.is_final` 被误认为协议字段 | 错误依赖/回写协议 | 文档和注释明确 SDK 派生，raw 不含该字段 |
| SDK 事件模型和 schema 漂移 | 字段错误 | 测试直接引用 R5.2.2 examples/schema |
| 过度封装导致高级能力丢失 | 调试困难 | 保留 raw JSON 回调和 event.raw |
| 引入 JSON parser 增加 C 包复杂度 | 构建/许可证风险 | 先做最小合同字段提取或评估轻量 parser |
| 误把 G2 包当实现放行 | 流程风险 | 本文明确仅为计划，实施需 G3/owner 批准 |

## 15. 推荐最终 API 形态摘要

### Python

```python
client.on_stt_event = lambda e: print(e.text, e.is_final)
client.on_reply_event = lambda e: print(e.text, e.intent_type)
client.on_command = lambda e: execute(e.action, e.params)
client.on_error_event = lambda e: maybe_retry(e.code, e.retryable)
client.on_clear_event = lambda e: player.clear(reason=e.reason)
client.on_state_event = lambda e: update_status(e.interaction_mode, e.resource_state)

# advanced/debug
client.on_stt = lambda payload: ...       # deprecated raw payload
client.on_reply = lambda payload: ...     # deprecated raw payload
client.on_cmd = lambda payload: ...       # deprecated raw payload
client.on_error = lambda payload: ...     # deprecated raw payload
client.on_json = lambda payload: ...
```

### Android

```java
@Override
public void onSttEvent(SttEvent event) {
    showUserText(event.text, event.isFinal);
}

@Override
public void onReplyEvent(ReplyEvent event) {
    showAssistantText(event.text);
}

@Override
public void onCommand(CommandEvent event) {
    executeCommand(event.action, event.params);
}

@Override
public void onErrorEvent(ErrorEvent event) {
    showOrRetry(event.code, event.retryable);
}
```

### C

```c
static void on_stt_event(const xiaoge_stt_event *event, void *user) {
    printf("stt final=%d text=%s\n", event->final, event->text);
}

static void on_reply_event(const xiaoge_reply_event *event, void *user) {
    printf("reply intent=%s text=%s\n", event->intent_type, event->text);
}

static void on_command(const xiaoge_command_event *event, void *user) {
    printf("cmd id=%s action=%s risk=%s\n", event->cmd_id, event->action, event->risk_level);
}

static void on_error_event(const xiaoge_error_event *event, void *user) {
    printf("error code=%s retryable=%d message=%s\n", event->code, event->retryable, event->message);
}
```

## 16. 结论

当前 `on_clear` / `on_state` / `on_stt` / `on_reply` / `on_cmd` / `on_error` 直接返回 JSON payload/字符串适合作为协议调试层，但不适合作为高层 SDK 的主推荐 API。

基于 R5.2.2 合同，`data.stt` 的稳定业务核心字段是 `text` 与 `final`，同时 `utterance_id/trace_id/session_id/ts_ms` 有明确追踪与关联价值；`data.reply` 的稳定业务核心字段是 `text`、`intent_type`，可选 `speak_policy` 有播报策略价值，但协议中没有 `final`。除此之外，`data.cmd` 的 `cmd_id/action/params/risk_level/timeout` 直接影响动作执行安全和 ack/result 正确性，`data.error` 的 `code/message/retryable` 直接影响错误恢复策略，`ctrl.clear` 与 `ctrl.state` 也有明确 UI 和运行时状态语义。

推荐将 SDK 高层 API 改为事件对象体系：P0 包括 `SttEvent` / `ReplyEvent` / `CommandEvent` / `ErrorEvent`，P1 包括 `ClearEvent` / `StateEvent`，P2 可选增强 `ReadyEvent`。

- 普通调用者直接使用 `event.text`。
- 实时字幕使用 `SttEvent.is_final`。
- 高级调用者使用 `utterance_id`、`intent_type`、`speak_policy`、`cmd_id`、`risk_level`、`retryable`、状态枚举和 trace/session 元数据。
- 调试和协议审计使用 `event.raw` 或 raw JSON 回调。

这样可以在不改变协议合同的情况下，提高 SDK 易用性、类型安全、长期兼容性和文档清晰度。
