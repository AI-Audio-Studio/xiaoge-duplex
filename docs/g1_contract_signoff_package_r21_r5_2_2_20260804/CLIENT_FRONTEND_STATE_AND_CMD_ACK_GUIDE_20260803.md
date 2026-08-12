# 端侧说明：ctrl.frontend_state 与 data.cmd_ack 使用规则

日期：2026-08-03

适用对象：端侧 SDK、GUI、端侧执行器、fake SDK、fake executor 开发同事。

本文说明两个端侧关心的问题：

1. `ctrl.frontend_state` 什么时候上传，`trust_level` 如何理解和设置。
2. `data.cmd_ack.status` 中 `accepted/rejected/duplicate` 如何理解，什么场景发送。

注意：协议字段名是 `ctrl.frontend_state`。端侧文档或代码注释中如出现缺少 `end_` 的误拼，应统一修正为 `ctrl.frontend_state`。

## 1. ctrl.frontend_state 是什么

`ctrl.frontend_state` 是端侧向小歌上报“机器人本地前端状态”的 P0 控制消息。

它主要用于：

- 唤醒/休眠门控。
- 端侧 KWS、按钮、GUI 唤醒事件上报。
- VAD 辅助。
- DOA 声源方向辅助。
- lock 状态上报。
- GUI 状态展示。
- 端云日志和 trace 对账。

它不是登录鉴权消息。登录鉴权仍然使用：

```text
create_session + WSS Authorization: Bearer <access_token>
```

## 2. ctrl.frontend_state 什么时候上传

建议端侧采用“事件触发 + 必要限频”，不要高频刷状态。

| 场景 | 是否上传 | 建议字段 |
| --- | --- | --- |
| 本地 KWS 唤醒成功 | 必须上传 | `wake_event=local_kws`, `wake_state=awake`, `trust_level=authoritative` |
| 物理按钮唤醒 | 必须上传 | `wake_event=button`, `wake_state=awake`, `trust_level=authoritative` |
| GUI 点击唤醒 | 必须上传 | `wake_event=gui`, `wake_state=awake`, `trust_level=authoritative` |
| 进入本地休眠/退出唤醒态 | 应上传 | `wake_state=sleeping`, `trust_level=authoritative` |
| VAD 检测到语音/静音变化 | 可上传，建议限频 | `vad=speech/silence`, `trust_level=hint` |
| DOA 声源方向变化 | 可上传，建议限频 | `doa=15`, `trust_level=observe` 或 `hint` |
| lock_mode 改变 | 应上传 | `lock_mode=true/false`, `trust_level=authoritative` 或 `hint` |
| 只是周期性状态镜像 | 不建议高频上传 | P0 不做高频状态镜像，避免噪声 |

## 3. ctrl.frontend_state 字段示例

本地 KWS 已确认唤醒：

```json
{
  "type": "ctrl.frontend_state",
  "trace_id": "trace-001",
  "session_id": "sess-001",
  "seq": 17,
  "ts_ms": 1789000000456,
  "ttl_ms": 1000,
  "trust_level": "authoritative",
  "wake_event": "local_kws",
  "wake_state": "awake",
  "vad": "speech"
}
```

只是 VAD 检测到有人声：

```json
{
  "type": "ctrl.frontend_state",
  "trace_id": "trace-001",
  "session_id": "sess-001",
  "seq": 18,
  "ts_ms": 1789000001456,
  "ttl_ms": 1000,
  "trust_level": "hint",
  "wake_event": "none",
  "wake_state": "unknown",
  "vad": "speech"
}
```

只是上报声源方向：

```json
{
  "type": "ctrl.frontend_state",
  "trace_id": "trace-001",
  "session_id": "sess-001",
  "seq": 19,
  "ts_ms": 1789000002456,
  "ttl_ms": 1000,
  "trust_level": "observe",
  "wake_event": "none",
  "wake_state": "unknown",
  "doa": 15
}
```

## 4. trust_level 怎么理解

`trust_level` 是端侧告诉小歌：“这条端侧状态小歌可以信到什么程度，能不能据此改变交互模式。”

它有三个取值：

| 值 | 中文理解 | 小歌可以怎么用 | 小歌不能怎么用 |
| --- | --- | --- | --- |
| `observe` | 观测 | 用于日志、GUI 展示、调试、审计 | 不能触发唤醒，不能打开 `engine_gate`，不能改变 `interaction_mode` |
| `hint` | 提示 | 可辅助 ASR/VAD/端点检测/方向显示/策略判断 | 不能单独触发唤醒，不能单独让 sleeping 进入 dialogue/listening |
| `authoritative` | 授权/权威 | 端侧已完成可靠判断，小歌可以据此做模式迁移 | 不能绕过鉴权、能力授权、高危确认、命令安全策略 |

最重要规则：

```text
只有 trust_level=authoritative 的有效 wake_event，才可以触发小歌从 sleeping 进入 listening/dialogue，并打开 engine_gate。
```

也就是说，如果只是 `vad=speech`，即使检测到有人说话，只要 `trust_level=hint`，小歌也不能仅凭它唤醒。

## 5. trust_level 如何设置

端侧建议按以下规则设置：

| 端侧判断来源 | 建议 trust_level | 原因 |
| --- | --- | --- |
| 本地 KWS 已确认唤醒词 | `authoritative` | 端侧主唤醒已完成可靠判断。 |
| 物理按钮唤醒 | `authoritative` | 用户明确操作。 |
| GUI 唤醒 | `authoritative` | 用户明确操作。 |
| 明确进入休眠 | `authoritative` | 端侧明确状态变化，可用于模式迁移。 |
| 明确 lock/unlock | `authoritative` 或 `hint` | 如果端侧要求小歌强制门控，用 authoritative；如果只是提示状态，用 hint。 |
| VAD speech/silence | `hint` | 可辅助端点判断，但不应单独唤醒。 |
| 疑似唤醒、低置信唤醒 | `hint` | 不应直接打开后级门控。 |
| DOA 声源方向 | `observe` 或 `hint` | 通常只用于显示/辅助，不直接触发模式迁移。 |
| 调试/展示状态 | `observe` | 只记录，不影响状态机。 |

## 6. 小歌如何使用 authoritative wake

小歌只有在满足类似以下条件时，才应基于 `ctrl.frontend_state` 做模式迁移：

```text
trust_level = authoritative
wake_event = local_kws / button / gui
wake_state = awake
ttl_ms 未过期
seq 未乱序/重复
当前 WSS 已鉴权
能力/caps 允许
```

通过后，小歌可以把状态从：

```text
interaction_mode = sleeping
engine_gate = closed
```

迁移到：

```text
interaction_mode = dialogue 或 listening
engine_gate = open
```

端侧要注意：`authoritative` 不表示“端侧说什么云端都无条件信”。云端仍要检查 token、session、caps、ttl、seq、当前状态和安全策略。

## 7. data.cmd_ack 是什么

`data.cmd_ack` 是端侧 SDK 收到云端 `data.cmd` 后返回的“投递确认”。

它只说明：

```text
端侧是否接收/拒绝/判重了这条命令
```

它不表示命令已经执行完成。

命令执行进展或最终结果必须使用：

```text
data.cmd_result
```

因此，正常命令闭环是：

```text
云端 -> 端侧: data.cmd
端侧 -> 云端: data.cmd_ack
端侧 -> 云端: data.cmd_result
```

## 8. data.cmd_ack.status 三个值

| status | 含义 | 什么时候发送 | 是否继续执行 |
| --- | --- | --- | --- |
| `accepted` | 已接收并已交给端侧执行流程 | 命令合法、capability 支持、当前状态允许执行、已进入 executor 队列 | 是，后续发送 `data.cmd_result` |
| `rejected` | 拒绝投递，不会执行 | capability 不支持、权限不足、参数非法、当前锁定/状态不允许、风险策略不允许 | 否，不应再执行 |
| `duplicate` | 重复命令，不再次执行 | 收到相同 `cmd_id`，且仍在去重窗口内 | 否，不重复执行 |

## 9. accepted 什么时候发送

端侧收到 `data.cmd` 后，如果满足以下条件，应返回 `accepted`：

- `cmd_id` 没见过，或不在重复窗口内。
- `capability_id` 是端侧支持的能力。
- `action` 是端侧可识别动作。
- 参数结构和范围可接受。
- 当前状态允许执行。
- 已成功放入 executor 队列或即将调用 executor。

示例：

```json
{
  "type": "data.cmd_ack",
  "trace_id": "trace-001",
  "session_id": "sess-001",
  "utterance_id": "utt-001",
  "cmd_id": "cmd-0001",
  "status": "accepted",
  "code": "sdk_received",
  "message": "accepted by SDK",
  "received_at_ms": 1789000001120
}
```

后续执行中或完成时，再发送 `data.cmd_result`。

## 10. rejected 什么时候发送

端侧收到 `data.cmd` 后，如果决定不执行，应返回 `rejected`。

典型原因：

- `capability_id` 不支持。
- `action` 不支持。
- 参数非法或越界。
- 当前机器人处于 lock 状态。
- 当前模式不允许执行。
- 权限不足。
- 高危策略未满足。
- executor 不可用且无法排队。

示例：能力不支持。

```json
{
  "type": "data.cmd_ack",
  "trace_id": "trace-001",
  "session_id": "sess-001",
  "utterance_id": "utt-001",
  "cmd_id": "cmd-0002",
  "status": "rejected",
  "code": "capability_unsupported",
  "message": "capability motion.jump is unsupported",
  "received_at_ms": 1789000001200
}
```

`rejected` 后端侧不执行，云端也不应继续等待该命令的 `data.cmd_result`。

## 11. duplicate 什么时候发送

端侧收到 `data.cmd` 后，如果发现相同 `cmd_id` 已经处理过，并且仍在去重窗口内，应返回 `duplicate`。

典型场景：

- 弱网导致同一条 `data.cmd` 重复到达。
- 客户端重连过程中重复收到相同 `cmd_id`。
- 云端或中间层错误重发了同一条命令。

示例：

```json
{
  "type": "data.cmd_ack",
  "trace_id": "trace-001",
  "session_id": "sess-001",
  "utterance_id": "utt-001",
  "cmd_id": "cmd-0001",
  "status": "duplicate",
  "code": "duplicate_cmd_id",
  "message": "duplicate cmd_id ignored",
  "received_at_ms": 1789000001300
}
```

端侧必须保证：`duplicate` 不触发重复执行。

## 12. data.cmd_ack 的几个边界

1. `accepted` 不代表执行成功，只代表端侧已接收并准备执行。
2. 执行成功、失败、取消、超时都必须通过 `data.cmd_result` 表达。
3. `rejected` 后端侧不执行，通常也不再发送 `data.cmd_result`。
4. `duplicate` 后端侧不重复执行。
5. `status` 不包含 `unknown`。未知 `cmd_id` 的 ack/result 不应混入 `data.cmd_ack.status`，应走 `data.error` 或审计。
6. `capability_missing` 不是 P0 合法枚举，能力不支持统一使用 `capability_unsupported`。

## 13. 端侧实现建议

端侧收到 `data.cmd` 后，建议处理顺序如下：

```text
1. 校验 schema 和必填字段
2. 检查 cmd_id 是否重复
3. 检查 capability_id/action 是否支持
4. 检查参数是否合法
5. 检查当前状态是否允许执行
6. 返回 data.cmd_ack
7. 如 accepted，转交 executor
8. executor 返回 running/succeeded/failed/canceled/timeout
9. 发送 data.cmd_result
```

伪代码：

```text
if cmd_id in dedupe_window:
    ack(status="duplicate", code="duplicate_cmd_id")
elif capability_id not supported:
    ack(status="rejected", code="capability_unsupported")
elif params invalid:
    ack(status="rejected", code="invalid_params")
elif robot locked or state not allowed:
    ack(status="rejected", code="permission_denied")
else:
    ack(status="accepted", code="sdk_received")
    executor.execute(cmd)
    send data.cmd_result
```

## 14. 一句话总结

`ctrl.frontend_state.trust_level` 决定小歌能不能把端侧状态当作模式迁移依据：`observe` 只看，`hint` 辅助，`authoritative` 才能授权唤醒/休眠等状态迁移。

`data.cmd_ack.status` 决定端侧是否接收这条命令：`accepted` 表示接收并准备执行，`rejected` 表示拒绝且不执行，`duplicate` 表示重复命令且不重复执行。
