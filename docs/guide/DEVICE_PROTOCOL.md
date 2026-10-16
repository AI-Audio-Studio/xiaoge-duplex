# 小歌 -97 · 端侧交互协议

> 整理日期：2026-10-14。面向硬件、嵌入式、APP 等自研协议客户端。
>
> **核验边界**：本文依据 `_remote_edit/` 中较新的网关、worker、V2/r11 应用实现，结合本地音频与指令生命周期模块整理。本次未对全部协议文件做实时远端哈希核验；不能据此保证每个运行中 worker 与这些文件逐字一致。2026-10-14 门控评测核验了生产磁盘 r11 与配置，但不是整个协议栈的线上验收。
>
> - -97 部署入口：`https://60.205.197.165:10097`，WS 使用 `wss`。
> - 源码中的网关默认端口仍可能是 `10099`，不是 -97 部署端口。
> - 本文以 **`POST /create_session` + `/ws/session` 单连接模式**为主。
> - [CLIENT_INTEGRATION.md](CLIENT_INTEGRATION.md) 主要描述旧 cookie 双通道模式，不应将旧文中的鉴权、欢迎语和 12 秒重连规则直接套用到本文。
> - 所有令牌、ID、API Key 与时间戳示例均为占位值，不是真实凭据。

## 1. 总体约定

```text
端侧 ── HTTPS 创建会话 / WSS 双向连接 ── 网关 :10097
                                         │
                                         └── poolmgr 分配的 worker（内部端口）
```

端侧只连接网关，不直接连接 worker 或 poolmgr。一条 `/ws/session` 同时承载：

| 帧类型 | 内容 |
| --- | --- |
| WebSocket Text | JSON 控制消息、转写、回复、设备指令及回执 |
| WebSocket Binary | 双向裸 PCM 音频 |
| WebSocket Ping/Pong | WebSocket 库处理的传输层保活，不是 JSON |

基本约定：

- JSON 消息由 `type` 分流；不要自行发明当前未支持的消息类型。
- `trace_id`、`session_id` 使用创建会话返回的值；指令回执还须回传原 `utterance_id` 和 `cmd_id`。
- 毫秒字段使用非负整数，不能使用 `true/false`；`ts_ms` 等时间戳为 epoch 毫秒。
- 常规握手字段是 `device_id/caps`，不是 `client_id/project_id/requested_tools`。
- merged 路径中的非法 P0 消息会导致连接以 `4400` 关闭。

## 2. 创建会话与握手

### 2.1 创建会话

```http
POST /create_session HTTP/1.1
Host: 60.205.197.165:10097
X-API-Key: <项目API_KEY>
Content-Type: application/json
```

```json
{
  "device_id": "device-example-001",
  "client_version": "1.0.0",
  "caps": ["audio", "text", "cmd", "state"],
  "audio_format": {
    "sample_rate": 16000,
    "channels": 1,
    "sample_format": "int16le"
  }
}
```

| 字段 | 要求 |
| --- | --- |
| `device_id` | 非空字符串 |
| `client_version` | 非空字符串 |
| `caps` | 非空、不重复；取值限于 `audio/text/cmd/state` |
| `audio_format` | 必须是 16000 Hz、单声道、`int16le` |

当前 worker 要求 `caps` 包含 `cmd`，否则返回 HTTP 403 `permission_denied`。
API Key 推荐用 `X-API-Key`；网关也接受 `Authorization: ApiKey <KEY>` 等兼容入口。
API Key 用于准入，**不是后续 WS 的会话令牌**。

成功响应：

```json
{
  "type": "session.created",
  "trace_id": "trace-example-001",
  "session_id": "sess-example-001",
  "access_token": "<短期会话令牌>",
  "expires_in_ms": 600000,
  "ws_url": "wss://60.205.197.165:10097/ws/session",
  "granted_caps": ["audio", "text", "cmd", "state"],
  "config_snapshot": {
    "config_version": "cfg-r5.2.2-webpanel"
  }
}
```

常见失败：HTTP 400 `protocol_error`、401 `auth_failed`、403 `permission_denied`、503 `resource_exhausted`。

**两个期限不同**：worker 短期令牌 TTL 为 600000ms；网关对创建后尚未连接的 pending 会话另有约 30 秒清理期限。因此创建后应及时连接，不能理解为一定可等待 10 分钟再连接。

### 2.2 建立 WebSocket

使用响应中的 `ws_url`，握手请求头为：

```http
Authorization: Bearer <access_token>
```

正式 `/ws/session` 不使用查询参数 `access_token` 鉴权。不要将 token 放入 `ctrl.hello`。
浏览器原生 WebSocket 的自定义请求头限制，应使用项目的专门前端适配路径处理，不能直接照搬为硬件协议。

连接后发送：

```json
{
  "type": "ctrl.hello",
  "proto": 2,
  "role": "device",
  "trace_id": "trace-example-001",
  "session_id": "sess-example-001",
  "device_id": "device-example-001",
  "caps": ["audio", "text", "cmd", "state"]
}
```

- `role` 允许 `device` 或 `panel`。
- `trace_id/session_id/device_id` 必须为非空字符串。
- `caps` 非空、不重复且属于支持集合；最终能力取 hello 请求与创建时授予能力的交集。
- hello 应使用创建响应的关联 ID，不要新生成一套 ID。

服务端返回 **`ctrl.ready`，不是 `hello`**：

```json
{
  "type": "ctrl.ready",
  "trace_id": "trace-example-001",
  "session_id": "sess-example-001",
  "sample_rate": 16000,
  "granted_caps": ["audio", "text", "cmd", "state"],
  "config_version": "cfg-r5.2.2-webpanel"
}
```

协商到 `state` 后，再发送初始状态。V2 初始为 sleeping，**无欢迎语**：

```json
{
  "type": "ctrl.state",
  "trace_id": "trace-example-001",
  "session_id": "sess-example-001",
  "link_state": "connected",
  "interaction_mode": "sleeping",
  "engine_gate": "open",
  "resource_state": "SleepingHot",
  "ts_ms": 1790000000000
}
```

收到 ready 后再开始正常业务交换。hello 会调度 attach 重置；不要周期性重发 hello 充当心跳。

## 3. 状态字段：连接、交互、双工不能混为一谈

### 3.1 `ctrl.state` 的业务三态

实际 wire 字段是 **`interaction_mode`，不是 `interaction_state`**。

| `interaction_mode` | 含义 | `engine_gate` | `resource_state` |
| --- | --- | --- | --- |
| `sleeping` | 业务休眠，等待唤醒/有效输入 | `open` | `SleepingHot` |
| `dialogue` | 正常交互模式 | `open` | `ActiveAgent` |
| `listening` | 收听输入，但被拒轮次不进入正常回复 | `open` | `ActiveAgent` |

注意：

- `link_state:"connected"` 是连接状态，不能表示是否对话。
- 当前三态 **全部 `engine_gate:"open"`**；不要因 sleeping/listening 自行停止上传音频。
- sleeping 的 open 是按键唤醒死锁修复后的实现，不要照旧设计改成 `kws_only`。
- 状态广播可能重复。端侧按最新 `ctrl.state` 更新 UI，不必把每帧都当作一次新事件。
- SDK 的 `agent_state=listening` 是引擎状态，不等于业务 `interaction_mode=listening`。
- merged 状态桥将旧 `state` 转为 `ctrl.state`，不承诺携带 SDK `agent_state`；`user_state` 当前用于内部语音活动记录，不是设备 wire 字段。

### 3.2 状态变化表（V2 + r11 semantic gate 开启）

| 当前状态 | 事件 | 结果 |
| --- | --- | --- |
| 任意 | 新会话 hello/attach | sleeping；清理交互定时器及 gate 授权，无欢迎语 |
| sleeping | 语音完成轮 | 先 dialogue，再进行门控；若拒绝则转 listening |
| sleeping | 合格按钮、本地 KWS、GUI 唤醒帧 | dialogue |
| sleeping | 云端 KWS 命中 | dialogue，并进入相应打断处理 |
| dialogue/listening | 语音门控拒绝 | listening；发送 final 转写，停止该轮正常回复 |
| listening | 语音门控放行 | dialogue，再进入正常回复 |
| dialogue/listening | 主模型调用 `not_addressed` | listening；打断残留播报，终止该轮 |
| sleeping/listening | 手发 `data.text` | 先切 dialogue，再检查退出语、双工开关及门控 |
| dialogue/listening | 命中整句语义退出规则 | sleeping，保留该句转写，静默退出 |
| listening | 最后语音活动后聆听超时 | sleeping，原因 `listen_timeout`；默认 30 秒 |
| dialogue | 空闲超时且守卫条件允许 | sleeping；默认 30 秒 |

空闲退出会受生成、TTS、播放在途及双工状态等条件约束，不是从最近一个网络包起严格 30 秒。
门控失败/超时也走拒绝路径，所以 listening 不一定代表“成功认出旁聊”。
睡眠中的旁聊可能产生 `sleeping → dialogue → listening`，端侧不应将短暂 dialogue 当作一定会有回复。
r11 gate 开启时，listening 的恢复由 **门控放行**触发，不是等待主聊天 LLM 的首个文本 chunk。

### 3.3 双工开关

端侧发送：

```json
{"type":"ctrl.duplex","enabled":false}
```

成功调度后返回：

```json
{
  "type":"ctrl.duplex_state",
  "trace_id":"trace-example-001",
  "session_id":"sess-example-001",
  "enabled":false,
  "ts_ms":1790000001000
}
```

这个回执表示已成功调度更新，不等待 agent loop 真正完成赋值。
**没有名为 `full_duplex` 的上述 wire 字段**。

- 开关自身不直接修改业务三态。
- r11 关闭双工时，语音与手发文字都只显示、不回复，文字不绕过开关。
- 手发文字可先退出 listening；语音在 listening 中可能保持 listening，因为在门控前已被双工检查终止。
- 麦克风静音是独立输入门，不等同 sleeping。旧 `/api/mic` 的静音广播属于 legacy 控制路径，不要发未支持的 `ctrl.mic_state` 到 merged 通道。

### 3.4 控制消息支持边界

| 消息 | 当前处理 / 回执 |
| --- | --- |
| `ctrl.duplex` | 调度开关更新，返回 `ctrl.duplex_state` |
| `ctrl.frontend_state` | 限定的真实唤醒事件；无独立 ACK，观察实际 `ctrl.state` |
| `ctrl.state` | 服务端输出，不是端侧设置状态的入站控制命令 |
| `ctrl.mic_state` / `ctrl.interrupt` | 当前 merged 校验器不接受，发送会导致 `4400`；不要作为设备控制命令 |
| `ctrl.session_config` | 人设/声音临时配置；reset 不等于重新建立会话 |

## 4. 保活与端侧唤醒

### 4.1 传输层保活：WebSocket Ping/Pong

网关与 worker 的 aiohttp WebSocket 配置 `heartbeat=30`，标准 Ping/Pong 交由库处理。
端侧 WebSocket 库应正常响应服务端 Ping。

**这不是每 30 秒发送 JSON `ctrl.ping` 的约定**；也不是收到任意业务包就重置业务休眠状态。
不要把 heartbeat 参数直接解释成固定 30 秒业务断线期限。

### 4.2 应用层保活：`ctrl.ping` / `ctrl.pong`

端侧可以发送：

```json
{"type":"ctrl.ping","ts_ms":1790000000000}
```

服务端回复：

```json
{
  "type":"ctrl.pong",
  "trace_id":"trace-example-001",
  "session_id":"sess-example-001",
  "ts_ms":1790000000010,
  "client_ts_ms":1790000000000
}
```

| 字段 | 语义 |
| --- | --- |
| 请求 `ts_ms` | 可省略或 null；非 null 时必须为非负整数 |
| 回复 `ts_ms` | 服务端当前 epoch 毫秒 |
| 回复 `client_ts_ms` | 仅在请求有合法时间戳时回显 |
| 回复关联 ID | merged 路径包含 trace/session；旧音频路径字段条件不同 |

最小请求 `{"type":"ctrl.ping"}` 也合法，但回复不会包含 `client_ts_ms`。
RTT 应用端侧同一计时基准计算，不要直接用服务端时钟减客户端时钟推断网络延迟。

现有浏览器前端每 **25 秒**发送一次应用 ping；hello 等待 **10 秒**。
这两个值是前端策略，不是服务端强制协议期限。代码未发现基于 JSON pong 的独立服务端 watchdog。
硬件端可采用 25 秒周期；如果自行增加“连续若干次 pong 未收到则重连”，应明确属于客户端容错策略。

应用心跳不会唤醒 dialogue，也不用于延长业务 listening/idle 超时。

### 4.3 `ctrl.frontend_state`：窄唤醒，不是心跳或全状态覆盖

真实按钮唤醒示例：

```json
{
  "type":"ctrl.frontend_state",
  "seq":2,
  "ttl_ms":1000,
  "trust_level":"authoritative",
  "wake_state":"awake",
  "wake_event":"button"
}
```

当前实现只在 V2 中同时满足以下条件才调度唤醒：

```text
trust_level == authoritative
wake_state == awake
wake_event ∈ {button, local_kws, gui}
```

- 该入口只处理 **sleeping → dialogue**；在 listening 中发送按钮帧不保证退出 listening。
- 周期 observe/hint 镜像、VAD/DOA 提示、单独 `wake_state:"awake"` 不激活唤醒。
- 端侧 authoritative 不代表端侧整套状态始终覆盖云端；不能用此帧直接设置 sleeping/listening。
- merged 校验要求 `seq/ttl_ms` 为非负整数；当前没有利用它们执行序号去重或 TTL 续期。
- 当前谓词不额外排除 `seq=1`；是否唤醒取决于上述三个字段，而不是序号。
- `source/awake/full_duplex/device` 不是当前窄唤醒分支读取的权威字段。
- 不应期待专门 frontend-state ACK；以随后实际收到的 `ctrl.state` 为状态依据。
- 旧测试/注释中“整帧退役”的表述落后于恢复后的窄唤醒实现，以实际处理分支为准。

## 5. 音频、转写、回复及清播放

### 5.1 二进制音频

上下行格式均为：**16000 Hz、单声道、signed PCM int16 little-endian 裸字节**。

- 不附加 WAV 头、seq、时间戳或自定义包头，不使用 JSON/base64 包装。
- 每个采样 2 字节；建议帧长度保持偶数字节。
- worker 将输入重切为 320 字节（160 采样，即 10ms）处理。
- 20ms=640 字节、40ms=1280 字节，均可作为端侧发送分片；20–40ms 是建议，不是必须精确匹配的协议要求。
- 只在已就绪、协商音频能力后正常发送。下行 Binary 按相同格式播放。

### 5.2 文字输入

```json
{"type":"data.text","text":"讲一个故事"}
```

`text` 必须为字符串，去除两端空白后的长度为 1–2000。
手发文字明确表达受话对象，但仍须判定当前音乐动作，不能绕过音乐授权。

### 5.3 转写与回复

转写字段样例：

```json
{
  "type":"data.stt",
  "trace_id":"trace-example-001",
  "session_id":"sess-example-001",
  "utterance_id":"utt-example-001",
  "text":"讲一个故事",
  "final":true,
  "ts_ms":1790000001000
}
```

`final=false` 为中间转写，`final=true` 为最终转写。收到 final 不代表门控一定放行，也不保证随后有回复。

`data.reply` 的字段集合为：

```text
 type, trace_id, session_id, utterance_id,
 intent_type, text, ts_ms, speak_policy
```

端侧按 `utterance_id` 关联显示回复；本文件不将 `intent_type/speak_policy` 猜写为固定枚举。
下行音频独立通过 Binary 帧提供，不要因收到回复文本再无条件自行合成一遍 TTS。

### 5.4 打断后的清播放

```json
{
  "type":"ctrl.clear",
  "trace_id":"trace-example-001",
  "session_id":"sess-example-001",
  "reason":"barge_in"
}
```

收到后，端侧立即停止当前下行语音播放并清空未播放缓存，随后接收的新音频重新排队。
`reason` 默认是 `barge_in`；clear 没有音频包序号边界，不要自行假设可以按 utterance 音频头过滤。
这不是“停止所有设备音乐”的指令。业务进入 listening 也不会无条件停止此前合法音乐。
主语音 TTS 让位后的已有音乐恢复与新的 `play_music` 执行请求不同。

## 6. 设备指令与 ACK/RESULT

服务端 `data.cmd` 字段：

```text
 type, trace_id, session_id, utterance_id, cmd_id,
 capability_id, action, params, risk_level,
 ack_timeout_ms, result_timeout_ms, issued_at_ms
```

`capability_id/action/params/risk_level` 使用真实下发值，不在端侧重写。
`issued_at_ms` 为实际下发时间，没有额外必需的 `ts_ms`。
默认 ACK 和 RESULT 超时均为 **3000ms**，以实际下发字段为准。

收到合法指令后回 ACK（关联字段来自原指令）：

```json
{
  "type":"data.cmd_ack",
  "trace_id":"trace-example-001",
  "session_id":"sess-example-001",
  "utterance_id":"utt-example-002",
  "cmd_id":"cmd-example-002",
  "status":"accepted",
  "code":"ok",
  "received_at_ms":1790000002000
}
```

完成后回 RESULT：

```json
{
  "type":"data.cmd_result",
  "trace_id":"trace-example-001",
  "session_id":"sess-example-001",
  "utterance_id":"utt-example-002",
  "cmd_id":"cmd-example-002",
  "status":"succeeded",
  "code":"ok"
}
```

| 回执 | 要求 |
| --- | --- |
| ACK | `status` 为 `accepted/rejected/duplicate`；`code` 非空；`received_at_ms` 非负整数 |
| RESULT | `status` 为 `running/succeeded/failed/canceled/timeout`；`code` 非空 |
| 关联 | 四个 ID 必填；trace/session/utterance 必须匹配原指令 |

- `ok` 是示例非空 code，不代表唯一合法 code。
- RESULT 不强制 `started_at_ms/finished_at_ms`。
- **ACK 和 RESULT 截止都从同一次实际下发时间计算，不是 ACK 后再计 3 秒。**
- 服务端约每 100ms 扫描超时；未实现超时自动重下发。
- ACK 的 rejected/duplicate 当前都进入失败处理，不是“duplicate 等同成功”。
- 未知 cmd 或关联不匹配会返回 `data.error`，`code=unknown_cmd_id`、`retryable=false`。
- 端侧应按 `cmd_id` 幂等处理，防止重复动作；晚到/重复结果不会再次选定执行结局。
- 一般成功不追加口播，失败有口播；电量查询成功有专门数值回复处理。

## 7. 断开、重连及限制

### 7.1 merged 通道重新创建会话

`/ws/session` 结束时，网关删除 token 映射、结束会话并释放 worker。

```text
连接丢失 → 停止旧连接收发/清理旧播放与会话关联
         → 客户端退避
         → POST /create_session 获取新 token / ID
         → 新 WSS 连接 → ctrl.hello → ctrl.ready / ctrl.state
```

不承诺旧 token 复连、音频续接、历史恢复或设备指令重放。重连策略不得自动重执行旧指令。
旧 cookie `/ws/audio` 的约 12 秒宽限 **不适用于 merged**。

### 7.2 关闭码

| 码 | 适用场景 |
| --- | --- |
| `4401` | merged 会话 token 无效/过期，或旧无 cookie 音频 API Key 拒绝 |
| `4009` | merged 同会话重复连接 |
| `4400` | merged 非法消息、坏 JSON；worker JSON 超过 8192 字节 |
| `1008` | merged 网关代理速率超限 |
| `1009` | merged Binary 超过网关帧大小上限 |
| `1011` | 上游 worker 不可达或代理错误 |
| `1013` | 旧音频路径资源满或 worker busy |
| `4001` | 旧 cookie 会话无效，或旧 `/ws` 无有效 cookie |
| `4002` | 旧 cookie 音频连接重复 |

源码默认网关限制为 200 条/秒、单帧 32768 字节，实际部署可配置。
worker 对 merged JSON 另有 8192 字节限制。超过代理文本大小限制/坏 JSON 也可能以 `4400` 结束。
不要无限快速重连；鉴权或协议错误先修复参数，容量/网络错误再采用退避。

## 8. 完整交互示例

### 8.1 新会话、按钮唤醒、正常请求

```text
端 → HTTPS POST /create_session，项目 API Key
云 → session.created（token、trace_id、session_id、ws_url）
端 → WSS /ws/session，Authorization: Bearer token
端 → ctrl.hello
云 → ctrl.ready
云 → ctrl.state：sleeping / open / SleepingHot
端 → ctrl.frontend_state：authoritative + awake + button
云 → ctrl.state：dialogue / open / ActiveAgent
端 → Binary PCM（“讲一个故事”）
云 → data.stt（partial/final，实际发布可能交错）
云 → data.reply + Binary PCM 回复
端 → ctrl.ping（与业务独立）
云 → ctrl.pong
```

具体转写、状态、回复帧的先后可能受异步发布影响；除 ready/初始 state 等明确发送顺序外，不要将示意图当作全链路严格排序承诺。

### 8.2 旁聊进入 listening，再恢复正常交互

```text
当前：dialogue
端 → 音频：“王哥仓库这边三个人那边四个人”
云 → final 转写；门控若拒绝则 ctrl.state：listening
云 → 不进入该轮正常回复，无新音乐授权
端 → 音频：“小歌讲个故事”
云 → 门控若放行则 ctrl.state：dialogue
云 → 正常回复
```

这是目标分支示例，不是语义正确率保证。r11 实测仍有正常请求误拒，不能仅凭协议文档认为语义验收已通过。

### 8.3 listening 超时与心跳共存

```text
当前：listening
端 → 每 25 秒 ctrl.ping
云 → ctrl.pong（连接仍正常）
若无新的语音活动且聆听定时器到期：
云 → ctrl.state：sleeping / open / SleepingHot
```

收到 pong 不意味着仍是 dialogue；业务 sleeping 也不意味着 WS 已断开。

### 8.4 指令执行

```text
云 → data.cmd（cmd_id=C，ack_timeout_ms=3000，result_timeout_ms=3000）
端 → data.cmd_ack（C，accepted）
端 → 实际执行（按 cmd_id 幂等）
端 → data.cmd_result（C，succeeded/failed 等）
```

两个截止从原下发时间开始，不能在 ACK 之后再等待完整 result_timeout_ms。

## 9. legacy 兼容与 reset 边界

| 项目 | 当前 merged `/ws/session` | 旧 `/ws/audio` / cookie 双通道 |
| --- | --- | --- |
| 准入 | 创建时 API Key，WS Bearer token | 无 cookie 音频已有 API Key 准入；cookie 路径另有会话准入 |
| ready | `ctrl.ready` | `ready` |
| 文本事件 | `data.stt/data.reply` | 可能仍为 `user_partial/message` |
| V2 初态 | sleeping，无欢迎语 | 当前 V2 音频 attach 也推 sleeping；初始字段不完全相同 |
| JSON pong | trace/session 上下文 | 不保证 trace；有注入 session 时才带 session |
| 断线恢复 | 新建会话 | 旧 cookie 音频可有约 12 秒宽限；无 cookie 不是同一规则 |

旧 cookie 音频宽限期间，下行在客户端不在线时丢弃，不是缓存后补播。

`ctrl.session_config {"reset":true}` 只清当前 persona/voice 临时覆盖，**不是整个会话重置**：不重置三态、双工、聊天历史或 gate。
hello/attach 另有 gate.reset，清 recent/background/grants 并使旧 epoch 失效，但不能据此声称清空了所有 SDK 聊天历史。

## 10. 实现依据与接入检查

本次主要依据（路径相对仓库根目录）：

| 主题 | 实现位置 |
| --- | --- |
| 网关鉴权、创建、token 路由与释放 | `_remote_edit/gateway/main.py:137–174,286–355` |
| worker 创建、hello、ready、初态 | `_remote_edit/webpanel/server.py:135–189,290–337,608–726` |
| ping/pong 与 frontend 唤醒 | `_remote_edit/webpanel/server.py:630–684,812–844` |
| 状态映射、转写、回复、clear | `_remote_edit/webpanel/bridge.py:180–210,234–342` |
| V2 唤醒、聆听超时与会话起点 | `_remote_edit/app/interaction_host.py:106–173` |
| r11 语音、文字、attach 接线 | `_remote_edit/web_ui_agent.py:1431–1560` |
| 空闲退出 | `_remote_edit/app/standby_host.py:108–189` |
| SDK 状态与内部语音活动 | `_remote_edit/app/setup_taps.py:217–289` |
| 代理限流、大小及关闭传播 | `_remote_edit/gateway/proxy.py:236–296` |
| PCM 输入重切与输出 | `examples/voice_agents/app/web_audio.py:19–55,123–153` |
| 指令字段与默认期限 | `examples/voice_agents/common/g3_intent.py:134–135,618–630` |
| 指令生命周期 | `examples/voice_agents/webpanel/command_lifecycle.py:52–207` |
| 前端应用心跳策略 | `_remote_edit/index.html:352–375` |

交付端侧前建议逐项检查：

- [ ] 创建请求、Bearer 握手、hello 字段正确且关联一致。
- [ ] 能处理 Text/Binary 混合帧，并正常响应标准 WS Ping。
- [ ] 三态显示使用 `interaction_mode`，不混用 SDK listening。
- [ ] sleeping 不自行关音频；按钮按窄唤醒报文发送。
- [ ] 应用 ping 不被用作业务唤醒或续期。
- [ ] clear 立即清播放缓存；不重复用文本合成云端 TTS。
- [ ] 指令 ACK/RESULT 四个 ID 回传准确，执行按 cmd_id 幂等。
- [ ] merged 断线重新创建会话，不复用旧 token 或重放旧指令。
- [ ] 正式对接时对目标版本重新做握手、状态、保活与回执实测。
