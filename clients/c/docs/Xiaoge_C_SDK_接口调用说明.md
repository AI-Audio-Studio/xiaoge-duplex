# Xiaoge C SDK R5.2.2 接口调用说明

本文档说明 Xiaoge C SDK R5.2.2 的主要接口、调用流程和 demo 使用方式，适用于端侧设备接入 Xiaoge Duplex `/ws/session` 主链路。

下行业务回调均接收**类型化事件结构体**（`xiaoge_state_event`、`xiaoge_stt_event` 等）；原始下行 JSON 仅通过 `on_json` 观察入口暴露，用于日志与协议审计。

---

## 1. SDK 主链路概览

当前 SDK 使用 **create_session + WebSocket Session** 模式：

```text
1. 设备侧构造 create_session 请求 JSON
2. 业务侧通过 HTTPS POST 调用 create_session
3. 服务端返回 session.created：
   trace_id / session_id / access_token / ws_url
4. SDK 使用 ws_url 建立 WSS /ws/session 连接
5. WebSocket 握手使用：
   Authorization: Bearer <access_token>
6. SDK 自动发送 ctrl.hello
7. 设备侧发送 PCM 音频 / 前端状态 / 双工开关 / 命令执行结果
8. 设备侧接收 TTS PCM / STT 文本 / 回复文本 / 命令 / 状态 / 错误
```

> 注意：旧版 `/ws/audio` 不是当前 SDK 的主链路，新接入请使用 `/ws/session`。

---

## 2. 音频格式要求

SDK 与云端约定的音频格式如下：

| 项目 | 要求 |
|---|---|
| 采样率 | 16000 Hz |
| 声道数 | 单声道 mono |
| 采样格式 | signed 16-bit little-endian |
| WebSocket 音频帧 | binary PCM |
| 推荐发送帧长 | 10–20 ms |
| 推荐帧大小 | 320–640 bytes |
| 最大 binary 帧 | 32768 bytes（`XIAOGE_BINARY_MAX_BYTES`） |

常用 20 ms 音频帧：

```c
#define FRAME_BYTES 640
```

---

## 3. 基本调用流程

### 3.1 配置 SDK 参数

```c
xiaoge_config cfg = {
    "robot-x3-001",                                // device_id
    "{\"key_id\":\"dev\",\"signature\":\"mock\"}", // credential_json
    "audio,text,cmd,state",                        // caps_csv
    "xiaoge-c-sdk-r5.2.2",                         // client_version
    "{}",                                          // prefs_json
    NULL,                                          // api_key，可选
    XIAOGE_AUTO_FAKE_CMD_DEFAULT                   // 命令自动模拟执行
};
```

字段说明：

| 字段 | 说明 |
|---|---|
| `device_id` | 设备 ID，必填 |
| `credential_json` | 设备鉴权凭据，必须是合法 JSON |
| `caps_csv` | 设备能力，默认 `audio,text,cmd,state` |
| `client_version` | 客户端版本，默认 `xiaoge-c-sdk-r5.2.2` |
| `prefs_json` | 偏好配置，可传 `{}` |
| `api_key` | create_session HTTP 的 `x-api-key`，可选 |
| `auto_fake_cmd_executor` | 是否启用 SDK 内置命令模拟执行，见第 7 章 |

---

### 3.2 构造 create_session 请求体

```c
char body[2048];

int n = xiaoge_build_create_session_request(&cfg, body, sizeof(body));
if (n <= 0) {
    // 构造失败：配置非法或 buffer 不足
}
```

生成的 JSON 类似：

```json
{
  "device_id": "robot-x3-001",
  "credential": {
    "key_id": "dev",
    "signature": "mock"
  },
  "caps": ["audio", "text", "cmd", "state"],
  "prefs": {},
  "audio_format": {
    "sample_rate": 16000,
    "channels": 1,
    "sample_format": "int16le"
  },
  "client_version": "xiaoge-c-sdk-r5.2.2"
}
```

> SDK 只负责构造请求体，真实 HTTPS POST 由业务侧完成。demo 中使用 libcurl 作为示例实现。

---

### 3.3 调用 create_session

业务侧向服务端发送：

```http
POST <create_session_url>
Content-Type: application/json
x-api-key: <api_key，可选>
```

请求体为上一步生成的 `body`。

服务端返回字段中，SDK 需要使用以下内容：

```json
{
  "trace_id": "...",
  "session_id": "...",
  "access_token": "...",
  "ws_url": "wss://host/ws/session",
  "config_version": "..."
}
```

---

### 3.4 创建 WebSocket Session 客户端

```c
xiaoge_session sess = {
    trace_id,
    session_id,
    access_token,
    ws_url,
    "audio,text,cmd,state",
    config_version
};
```

注册回调时**必须**设置 `struct_size` 并使用指定初始化器（不要按位置初始化）：

```c
xiaoge_callbacks cb = {
    .struct_size = sizeof(cb),
    .on_ready = on_ready,
    .on_audio = on_audio,
    .on_state = on_state,
    .on_stt = on_stt,
    .on_reply = on_reply,
    .on_duplex_state = on_duplex_state,
    .user = user_data,
};
```

`on_clear`、`on_command`、`on_error`、`on_json`、`on_failure` 按需注册，未注册的回调保持 NULL 即可。回调结构体与各事件说明见第 5 章。

创建客户端：

```c
xiaoge_client *client = xiaoge_create_from_session_with_ca(
    &cfg,
    &sess,
    "certs/cloud-ca.pem", // CA 证书路径，可为 NULL
    0,                    // insecure，正式环境应为 0
    &cb
);

if (!client) {
    // 连接创建失败
}
```

如果测试环境不校验证书，可使用：

```c
xiaoge_client *client = xiaoge_create_from_session(&cfg, &sess, 1, &cb);
```

> `insecure=1` 仅建议在本地测试或隔离测试环境中使用，正式验收和生产环境应使用 CA 证书或系统信任链。

---

### 3.5 驱动 SDK 事件循环

SDK 基于 libwebsockets，需要持续调用：

```c
while (xiaoge_service(client, 20) == 0) {
    // 可在循环中发送音频或状态
}
```

参数 `timeout_ms` 表示单次 service 等待时间，常用 5–50 ms。

连接建立后，`xiaoge_service()` 会自动驱动应用层保活：每 `XIAOGE_KEEPALIVE_PING_INTERVAL_MS`（默认 25 秒）发送一次 `ctrl.ping`，无需业务侧干预（见第 8 章）。

---

### 3.6 发送前端状态

建议在收到 `on_ready` 后发送一次前端状态：

```c
xiaoge_send_frontend_state(
    client,
    "hint",     // trust_level: authoritative / hint / observe
    "awake",    // wake_state
    "speech",   // vad
    1000        // ttl_ms
);
```

当停止说话或进入静音时，可发送：

```c
xiaoge_send_frontend_state(client, "hint", "awake", "silence", 1000);
```

---

### 3.7 发送 PCM 音频

```c
xiaoge_send_pcm(client, pcm_data, pcm_len);
```

推荐每次发送 320 或 640 bytes：

```c
#define FRAME_BYTES 640

xiaoge_send_pcm(client, frame, FRAME_BYTES);
```

返回值：

| 返回值 | 含义 |
|---|---|
| `0` | 成功加入发送队列 |
| `-1` | 参数错误、帧过大或内部失败 |

---

### 3.8 双工开关（ctrl.duplex）

```c
xiaoge_send_duplex(client, 0);  /* 关闭双工；传 1 重新开启 */
```

- 上行发送 `{"type":"ctrl.duplex","enabled":<bool>}`（DEVICE_PROTOCOL §3.3）。
- 服务端成功调度后回执 `ctrl.duplex_state`，SDK 解析为 `xiaoge_duplex_state_event` 并触发 `on_duplex_state`：

```c
static void on_duplex_state(const xiaoge_duplex_state_event *event, void *user) {
    printf("duplex enabled=%d ts_ms=%lld\n", event->enabled, event->ts_ms);
}
```

- `xiaoge_duplex_state_event` 携带 `enabled`、`ts_ms`、`trace_id`、`session_id`、`raw_json`。
- 回执语义是**已调度**，不等于 agent loop 已完成赋值。
- 开关自身不直接修改业务三态：sleeping/dialogue/listening 仍以 `on_state`（`ctrl.state`）为准。
- 关闭双工后上行 PCM 照常发送，服务端对语音与手发文字只显示、不回复。
- 没有 `full_duplex` wire 字段；`ctrl.mic_state` 未支持，不要发送。
- 两个 demo 均提供 `--duplex off|on` 参数，可在会话就绪后切换并观察回执（demo 会打印 `duplex_state: enabled=... ts_ms=...`）。

---

### 3.9 销毁客户端

使用结束后调用：

```c
xiaoge_destroy(client);
```

---

## 4. 常量宏

`xiaoge_client.h` 为协议枚举提供 wire 常量宏，比较事件字段时使用，避免手写字符串（拼错的字面量不会报错，只会永不匹配）：

| 类别 | 宏 |
|---|---|
| 连接状态 | `XIAOGE_LINK_STATE_CONNECTING` / `_CONNECTED` / `_RECONNECTING` / `_CLOSED` |
| 交互三态 | `XIAOGE_INTERACTION_MODE_SLEEPING` / `_DIALOGUE` / `_LISTENING` |
| 引擎门控 | `XIAOGE_ENGINE_GATE_CLOSED` / `_OPEN` / `_KWS_ONLY` |
| 资源状态 | `XIAOGE_RESOURCE_STATE_SLEEPING_HOT` / `_SLEEPING_WARM` / `_ACTIVE_AGENT` / `_RELEASED_IDLE` / `_PENDING_RECONNECT` |
| 命令 ack 状态 | `XIAOGE_CMD_ACK_STATUS_ACCEPTED` / `_REJECTED` / `_DUPLICATE` |
| 命令结果状态 | `XIAOGE_CMD_RESULT_STATUS_RUNNING` / `_SUCCEEDED` / `_FAILED` / `_CANCELED` / `_TIMEOUT` |
| 错误码 | `XIAOGE_ERROR_AUTH_FAILED` / `_PERMISSION_DENIED` / `_BUSY` / `_PROTOCOL_ERROR` / `_CAPABILITY_UNSUPPORTED` / `_TOKEN_EXPIRED` / `_DUPLICATE_CONNECTION` / `_RESOURCE_EXHAUSTED` / `_UNKNOWN_CMD_ID` |

其他常用宏：`XIAOGE_SAMPLE_RATE`（16000）、`XIAOGE_JSON_MAX_BYTES`（8192）、`XIAOGE_BINARY_MAX_BYTES`（32768）、`XIAOGE_KEEPALIVE_PING_INTERVAL_MS`（25000）、`XIAOGE_AUTO_FAKE_CMD_DEFAULT/_DISABLED/_ENABLED`。

---

## 5. 回调函数说明

### 5.1 回调结构体与 ABI

```c
typedef struct {
    size_t struct_size;
    void (*on_ready)(const xiaoge_ready_event *event, void *user);
    void (*on_audio)(const void *pcm, size_t len, void *user);
    void (*on_clear)(const xiaoge_clear_event *event, void *user);
    void (*on_state)(const xiaoge_state_event *event, void *user);
    void (*on_stt)(const xiaoge_stt_event *event, void *user);
    void (*on_reply)(const xiaoge_reply_event *event, void *user);
    void (*on_command)(const xiaoge_command_event *event, void *user);
    void (*on_error)(const xiaoge_error_event *event, void *user);
    void (*on_json)(const char *json, void *user);
    void (*on_failure)(int code, const char *message, void *user);
    void *user;
    void (*on_duplex_state)(const xiaoge_duplex_state_event *event, void *user);
} xiaoge_callbacks;
```

ABI 说明：

- `struct_size` **必须**设置为 `sizeof(xiaoge_callbacks)`；SDK 只拷贝调用方结构体的 `struct_size` 前缀。
- `on_duplex_state` 追加在 `void *user` **之后**：按旧头文件编译的二进制保持兼容（新回调指针为 NULL），新回调必须继续追加在结构体末尾。
- 注册回调请使用指定初始化器，不要按位置初始化。

**指针生命周期（重要）**：所有事件指针及其中的 `const char *` 字段（含 `raw_json`）只在回调函数返回前有效。需要在异步上下文或其它线程长期保存时，必须自行 `strdup`/拷贝。

**入站校验（重要）**：C SDK 对下行帧校验是严格的。必填字段缺失、类型错误或枚举值非法（如未知 `interaction_mode`）时，该帧不会构造事件、不触发业务回调，而是通过 `on_failure` 报告（例如 `invalid ctrl.state payload`），同时仍触发 `on_json` 供排查。这一点与 Python/Android SDK 的宽松透传（`parse_warnings`）策略不同。

---

### 5.2 `on_ready`

云端 Session 就绪时触发（`ctrl.ready`）。

```c
static void on_ready(const xiaoge_ready_event *event, void *user) {
    printf("ready sample_rate=%d granted_caps=%s config_version=%s\n",
           event->sample_rate, event->granted_caps, event->config_version);
}
```

`xiaoge_ready_event` 携带 `sample_rate`、`granted_caps`（逗号分隔）、`config_version`、`trace_id`、`session_id`、`raw_json`。通常在此之后开始发送音频。

---

### 5.3 `on_audio`

收到下行 TTS PCM 音频时触发。

```c
static void on_audio(const void *pcm, size_t len, void *user) {
    // 播放或缓存 PCM；格式为 16k/mono/int16le
}
```

---

### 5.4 `on_clear`

收到清空播放指令时触发（`ctrl.clear`）。

```c
static void on_clear(const xiaoge_clear_event *event, void *user) {
    // 清空本地播放缓冲；event->reason 可为 NULL
}
```

`xiaoge_clear_event` 携带可空的 `reason`（`barge_in`/`user_stop`/`system_cancel`/`sleep`）与 `utterance_id`，以及 `trace_id`、`session_id`、`raw_json`。常见用途：用户打断时，立即停止播放旧回复。

---

### 5.5 `on_state` 与交互状态 interaction_mode 获取方式

服务端状态更新（`ctrl.state`）触发，`xiaoge_state_event` 携带四个状态维度：

```c
typedef struct {
    const char *link_state;
    const char *interaction_mode;
    const char *engine_gate;
    const char *resource_state;
    const char *pending_confirmation_json; /* 可空 */
    long long ts_ms;
    const char *trace_id;
    const char *session_id;
    const char *raw_json;
} xiaoge_state_event;
```

**交互状态 `interaction_mode` 的获取方式：**

1. **唯一入口**是 `on_state` 回调中的 `event->interaction_mode`（`const char *`），与 `link_state`/`engine_gate`/`resource_state` 同帧快照下发。SDK 不缓存当前状态、也没有查询接口——业务侧应在回调中自行保存最新值（跨线程/异步保存需 `strdup`，指针仅回调内有效）。
2. **下发时机**：握手完成后紧跟 `ctrl.ready` 下发一次初始状态，之后每次服务端状态变化重发。
3. **快照语义**：每条 `ctrl.state` 都是全量快照，广播可能重复；按最新一条更新 UI/状态机，不必把每帧当作一次新事件（DEVICE_PROTOCOL §3.1）。
4. **三态含义**：

| `interaction_mode` | 含义 | 典型 `engine_gate` | 典型 `resource_state` |
|---|---|---|---|
| `sleeping` | 业务休眠，等待唤醒/有效输入 | `open` | `SleepingHot` |
| `dialogue` | 正常交互模式 | `open` | `ActiveAgent` |
| `listening` | 收听输入，但被拒轮次不进入正常回复 | `open` | `ActiveAgent` |

5. **比较方式**：使用常量宏，避免手写字符串：

```c
static char g_interaction_mode[32];

static void on_state(const xiaoge_state_event *event, void *user) {
    (void)user;
    snprintf(g_interaction_mode, sizeof(g_interaction_mode), "%s", event->interaction_mode);

    if (strcmp(event->interaction_mode, XIAOGE_INTERACTION_MODE_SLEEPING) == 0) {
        // 休眠态 UI / 逻辑
    } else if (strcmp(event->interaction_mode, XIAOGE_INTERACTION_MODE_DIALOGUE) == 0) {
        // 正常对话态
    } else if (strcmp(event->interaction_mode, XIAOGE_INTERACTION_MODE_LISTENING) == 0) {
        // 收听态
    }
}
```

6. **注意事项**：
   - 实际 wire 字段是 `interaction_mode`，不是 `interaction_state`。
   - 当前三态**全部 `engine_gate="open"`**：不要因为 sleeping/listening 自行停止上传音频。
   - `link_state` 是连接状态，不能表示是否在对话。
   - SDK 本地引擎状态 `agent_state` 不等于业务三态 `interaction_mode`，两者不要混用。
   - `interaction_mode` 非法/缺失时整帧被严格校验拒绝，走 `on_failure`，不会回调 `on_state`（见 5.1 入站校验）。

---

### 5.6 `on_duplex_state`

双工开关回执（`ctrl.duplex_state`），配合 `xiaoge_send_duplex` 使用，见 3.8 节。

---

### 5.7 `on_stt`

收到用户语音识别文本（`data.stt`）。

```c
static void on_stt(const xiaoge_stt_event *event, void *user) {
    printf("stt final=%d text=%s\n", event->final, event->text);
}
```

`xiaoge_stt_event` 携带 `text`、`final`、`utterance_id`、`trace_id`、`session_id`、`ts_ms`、`raw_json`。实时字幕可依据 `final` 区分中间/最终结果。

---

### 5.8 `on_reply`

收到助手文本回复（`data.reply`）。

```c
static void on_reply(const xiaoge_reply_event *event, void *user) {
    printf("reply intent=%s text=%s\n", event->intent_type, event->text);
}
```

`xiaoge_reply_event` 携带 `text`、`final`（SDK 派生，R5.2.2 恒为 1）、`utterance_id`、`intent_type`、可空 `speak_policy`、`trace_id`、`session_id`、`ts_ms`、`raw_json`。

> R5.2.2 协议的 `data.reply` 没有 `final` 字段；事件中的 `final == 1` 是 SDK 为统一文本事件接口派生的值。

---

### 5.9 `on_command`

收到云端下发命令（`data.cmd`）。

```c
static void on_command(const xiaoge_command_event *event, void *user) {
    printf("cmd id=%s action=%s risk=%s\n", event->cmd_id, event->action, event->risk_level);
}
```

`xiaoge_command_event` 携带 `cmd_id`、`capability_id`、`action`、`params_json`（JSON 字符串）、`risk_level`、`ack_timeout_ms`、`result_timeout_ms`、`issued_at_ms`、`utterance_id`、`trace_id`、`session_id`、`raw_json`。

真实执行命令时应基于事件对象回 ack/result，见第 7 章。

---

### 5.10 `on_error`

收到服务端错误事件（`data.error`）。

```c
static void on_error(const xiaoge_error_event *event, void *user) {
    fprintf(stderr, "error code=%s retryable=%d message=%s\n",
            event->code, event->retryable, event->message);
}
```

`xiaoge_error_event` 携带 `code`、`message`、`retryable`、`ts_ms`、`trace_id`、`session_id`、`raw_json`。`code` 与 `XIAOGE_ERROR_*` 宏比较判断重试策略。

---

### 5.11 `on_json`

所有下行 JSON 的 raw 观察入口，用于日志、协议审计或读取尚未建模的字段。**不要**在 `on_json` 与类型化命令回调中重复执行同一命令。

---

### 5.12 `on_failure`

SDK 本地失败通道：入站帧校验失败（见 5.1）、发送失败等。参数为错误码与消息。这不是服务端 `data.error`，两者不要混淆。

---

## 6. 最小接入示例

```c
#include "xiaoge_client.h"

#include <stdio.h>
#include <string.h>

static xiaoge_client *g_client;
static int g_ready = 0;

static void on_ready(const xiaoge_ready_event *event, void *user) {
    (void)user;
    g_ready = 1;
    printf("ready sample_rate=%d\n", event->sample_rate);
}

static void on_audio(const void *pcm, size_t len, void *user) {
    (void)pcm; (void)user;
    printf("received audio len=%zu\n", len);
}

static void on_state(const xiaoge_state_event *event, void *user) {
    (void)user;
    printf("state link=%s mode=%s\n", event->link_state, event->interaction_mode);
}

static void on_stt(const xiaoge_stt_event *event, void *user) {
    (void)user;
    printf("stt final=%d text=%s\n", event->final, event->text);
}

static void on_reply(const xiaoge_reply_event *event, void *user) {
    (void)user;
    printf("reply %s\n", event->text);
}

static void on_duplex_state(const xiaoge_duplex_state_event *event, void *user) {
    (void)user;
    printf("duplex enabled=%d\n", event->enabled);
}

int main(void) {
    xiaoge_config cfg = {
        "robot-x3-001",
        "{\"key_id\":\"dev\",\"signature\":\"mock\"}",
        "audio,text,cmd,state",
        "xiaoge-c-sdk-r5.2.2",
        "{}",
        NULL,
        XIAOGE_AUTO_FAKE_CMD_DEFAULT
    };

    /*
     * 业务侧应先：
     * 1. 调用 xiaoge_build_create_session_request 生成请求体
     * 2. HTTPS POST 到 create_session
     * 3. 从响应中拿到 trace_id/session_id/access_token/ws_url/config_version
     */

    xiaoge_session sess = {
        "trace-id-from-server",
        "session-id-from-server",
        "access-token-from-server",
        "wss://example.com/ws/session",
        "audio,text,cmd,state",
        "config-version-from-server"
    };

    xiaoge_callbacks cb = {
        .struct_size = sizeof(cb),
        .on_ready = on_ready,
        .on_audio = on_audio,
        .on_state = on_state,
        .on_stt = on_stt,
        .on_reply = on_reply,
        .on_duplex_state = on_duplex_state,
        .user = NULL,
    };

    xiaoge_client *client = xiaoge_create_from_session_with_ca(
        &cfg, &sess, "certs/cloud-ca.pem", 0, &cb);

    if (!client) {
        fprintf(stderr, "failed to connect\n");
        return 1;
    }
    g_client = client;

    while (xiaoge_service(client, 20) == 0) {
        if (g_ready) {
            xiaoge_send_frontend_state(client, "hint", "awake", "speech", 1000);

            /*
             * 在这里持续发送 16k/mono/int16le PCM：
             * xiaoge_send_pcm(client, pcm_frame, frame_len);
             */
        }
    }

    xiaoge_destroy(client);
    return 0;
}
```

---

## 7. 命令执行接口

SDK 默认启用内置 fake executor。

也就是说，收到 `data.cmd` 后，SDK 会自动回复：

```text
data.cmd_ack
data.cmd_result running
data.cmd_result succeeded
```

如果业务侧需要真实执行机器人动作，应关闭自动模拟执行：

```c
xiaoge_config cfg = {
    "robot-x3-001",
    "{\"key_id\":\"dev\",\"signature\":\"mock\"}",
    "audio,text,cmd,state",
    "xiaoge-c-sdk-r5.2.2",
    "{}",
    NULL,
    XIAOGE_AUTO_FAKE_CMD_DISABLED
};
```

然后在 `on_command` 中基于事件对象上报命令状态（推荐方式）：

### 7.1 基于事件对象发送 ack / result（推荐）

```c
static void on_command(const xiaoge_command_event *event, void *user) {
    (void)user;

    xiaoge_send_cmd_ack_event(g_client, event,
                              XIAOGE_CMD_ACK_STATUS_ACCEPTED, "ok", "queued");
    xiaoge_send_cmd_result_event(g_client, event,
                                 XIAOGE_CMD_RESULT_STATUS_RUNNING, "ok", "started", 0);

    /* TODO: 在这里执行真实机器人动作 */

    xiaoge_send_cmd_result_event(g_client, event,
                                 XIAOGE_CMD_RESULT_STATUS_SUCCEEDED, "ok", "completed", 0);
}
```

事件版 helper 直接从 `xiaoge_command_event` 复制 `trace_id`/`session_id`/`utterance_id`/`cmd_id`，业务侧只需填写 `status`、业务 `code` 与 `message`，避免手工解析 JSON。

`ack status` 支持（`XIAOGE_CMD_ACK_STATUS_*`）：

| status | 含义 |
|---|---|
| `accepted` | 已接受 |
| `rejected` | 已拒绝 |
| `duplicate` | 重复命令 |

`result status` 支持（`XIAOGE_CMD_RESULT_STATUS_*`）：

| status | 含义 |
|---|---|
| `running` | 执行中 |
| `succeeded` | 执行成功 |
| `failed` | 执行失败 |
| `canceled` | 已取消 |
| `timeout` | 执行超时 |

`retryable`：`0` 不建议重试，`1` 可以重试。

### 7.2 基于原始 cmd JSON 的兼容入口

旧接口保留为兼容入口，`cmd_json` 必须包含 `trace_id`、`session_id`、`utterance_id`、`cmd_id`：

```c
xiaoge_send_cmd_ack(client, cmd_json, "accepted", "ok", "queued");
xiaoge_send_cmd_result(client, cmd_json, "running", "ok", "started", 0);
xiaoge_send_cmd_result_ex(client, cmd_json, "succeeded", "ok", "completed", 0,
                          started_at_ms, finished_at_ms, duration_ms);
```

新代码请优先使用 7.1 的事件版接口。

### 7.3 携带可选 result 字段

`data.cmd_result` 支持可选的业务返回值 `result`（JSON 对象）。事件版和原始 cmd JSON 版分别对应：

```c
xiaoge_send_cmd_result_event_ex(client, event, XIAOGE_CMD_RESULT_STATUS_SUCCEEDED,
                                "ok", "completed", 0,
                                "{\"type\":\"cap\",\"value\":40,\"gauge_id\":1}");
xiaoge_send_cmd_result_ex2(client, cmd_json, XIAOGE_CMD_RESULT_STATUS_SUCCEEDED, "ok", "completed", 0,
                           -1, -1, -1, "{\"type\":\"cap\",\"value\":40,\"gauge_id\":1}");
```

`result_json` 传 `NULL` 时消息中不含 `result` 字段，与旧协议完全兼容；非对象或非法 JSON 会在本地返回 `-1`，不会发送到服务端。

---

## 8. 应用层保活（ctrl.ping）

- 连接建立后，`xiaoge_service()` 自动每 `XIAOGE_KEEPALIVE_PING_INTERVAL_MS`（默认 25 秒）发送一次 `{"type":"ctrl.ping","ts_ms":<epoch ms>}`。25 秒是客户端策略，不是服务端强制期限。
- 业务在自己的主循环里持续调用 `xiaoge_service()` 即自动获得保活。
- 需要手动补发时调用 `xiaoge_send_ping(client)`。
- 保活仅保持传输/会话活性；SDK 不做"连续 pong 未收到则重连"的检测，如需该容错策略请在业务层实现。

---

## 9. Demo 使用说明

### 9.1 构建 SDK 和 demo

在 `xiaoge-c-sdk-r5.2.2` 包根目录执行：

```bash
cmake -S . -B build
cmake --build build
ctest --test-dir build --output-on-failure
```

依赖：

```text
libwebsockets >= 4.0，需启用 TLS
libcurl
CMake
C 编译器
pthread，非 Windows 通常需要
```

---

### 9.2 文件 demo

文件 demo 会读取本地 wav，发送给云端，并将收到的 TTS PCM 写入输出 wav。

```bash
./build/xiaoge_demo_file \
  <create_session_url> \
  <device_id> \
  <credential-json-or-string> \
  in.wav \
  out.wav \
  --ca-cert certs/cloud-ca.pem
```

示例：

```bash
./build/xiaoge_demo_file \
  http://127.0.0.1:10097/create_session \
  robot-x3-001 \
  '{"key_id":"dev","signature":"mock"}' \
  in.wav \
  out.wav \
  --insecure
```

输入 wav 要求：

```text
16000 Hz / mono / signed 16-bit PCM
```

如果测试系统已提前调用 create_session，也可以使用兼容模式：

```bash
./build/xiaoge_demo_file \
  <ws_url> \
  <access_token> \
  <trace_id> \
  <session_id> \
  <device_id> \
  in.wav \
  out.wav \
  --ca-cert certs/cloud-ca.pem
```

---

### 9.3 Windows 麦克风 demo

`xiaoge_demo_mic_win` 是 Windows 实时麦克风 demo：

- 使用 Windows `waveIn` 采集麦克风
- 发送 16 kHz mono int16 PCM
- 打印下行事件
- 默认播放收到的 TTS PCM

默认使用本地代理：

```bash
python examples/local_proxy.py
```

然后运行：

```bash
./build/xiaoge_demo_mic_win --seconds 20
```

显式指定 create_session 参数：

```bash
./build/xiaoge_demo_mic_win \
  http://127.0.0.1:10097/create_session \
  robot-x3-001 \
  '{"key_id":"dev","signature":"mock"}' \
  --seconds 20
```

常用参数：

| 参数 | 说明 |
|---|---|
| `--seconds N` | 采集 N 秒，`0` 表示直到 Ctrl-C |
| `--reply-wait N` | 采集结束后等待回复 N 秒 |
| `--silence-ms N` | 采集结束后补发 N ms 静音 |
| `--no-playback` | 不播放下行 TTS |
| `--duplex off\|on` | 会话就绪后切换双工开关并打印 `ctrl.duplex_state` 回执 |
| `--ca-cert path` | 指定 CA 证书 |
| `--insecure` | 跳过证书校验，仅测试使用 |
| `--api-key key` | 指定 create_session 的 API Key |

---

## 10. 本地代理说明

如果本地环境无法直接访问远端 Gateway，可以使用包内代理：

```bash
python examples/local_proxy.py
```

然后 demo 使用：

```text
http://127.0.0.1:10097/create_session
```

代理负责：

```text
本地 HTTP/WS
  -> 远端 HTTPS/WSS
  -> 改写 create_session 返回的 ws_url
```

可选配置文件：

```text
examples/local_proxy_config.json
```

示例：

```json
{
  "server_host": "60.205.197.165",
  "server_port": 10097,
  "local_host": "127.0.0.1",
  "local_port": 10097,
  "verify_ssl": false
}
```

---

## 11. 错误处理建议

| 场景 | 建议处理 |
|---|---|
| create_session HTTP 401 | 检查 credential 或重新 provision |
| WebSocket 4401 | token 失效，重新 create_session 后重连 |
| HTTP/WSS 403 | 权限不足，提示用户或设备不可用 |
| WSS 4400 | 协议错误，记录 trace 并按策略重连 |
| WSS 4009 | 重复连接，按设备策略保留或重连 |
| HTTP 503 / resource_exhausted | 退避重试 |
| data.error busy/resource_exhausted | 提示繁忙并退避 |
| 连接断开 | 销毁 client，重新 create_session 并建立新连接 |

---

## 12. 接入注意事项

1. **必须使用 `/ws/session`**：SDK 会拒绝 `/ws/audio` 路径。
2. **WebSocket 使用 Bearer Token**：SDK 在 WSS 握手时自动添加 `Authorization: Bearer <access_token>`。
3. **create_session HTTP 由业务侧负责**：SDK 只提供 JSON 构造接口，demo 中的 libcurl 代码仅作为参考。
4. **正式环境不要使用 `--insecure`**：应使用证书 SAN 匹配的域名，或指定 CA bundle。
5. **持续调用 `xiaoge_service`**：否则 WebSocket 收发与自动保活都不会推进。
6. **收到 `ctrl.clear` 应清空本地播放**：避免用户打断后继续播放旧音频。
7. **音频帧不要超过限制**：单个 binary 帧最大 `32768` bytes，推荐 320–640 bytes 小帧连续发送。
8. **真实机器人命令执行时关闭 fake executor**：设置 `cfg.auto_fake_cmd_executor = XIAOGE_AUTO_FAKE_CMD_DISABLED;`，并在 `on_command` 中基于事件回 ack/result。
9. **双工开关回执只代表已调度**：业务三态以 `on_state` 为准，不要用 `ctrl.duplex_state` 推断交互状态。
10. **事件指针仅回调内有效**：跨线程保存必须拷贝字符串。

---

## 13. 常用接口速查

| 接口 | 作用 |
|---|---|
| `xiaoge_build_create_session_request` | 构造 create_session 请求 JSON |
| `xiaoge_create_from_session` | 使用 session 信息创建客户端 |
| `xiaoge_create_from_session_with_ca` | 使用 session 信息和 CA 证书创建客户端 |
| `xiaoge_send_pcm` | 发送上行 PCM 音频 |
| `xiaoge_send_frontend_state` | 发送前端状态 |
| `xiaoge_send_duplex` | 双工开关上行（回执见 `on_duplex_state`） |
| `xiaoge_send_ping` | 手动发送一次 `ctrl.ping` 保活帧 |
| `xiaoge_send_cmd_ack_event` | 基于命令事件发送 ack（推荐） |
| `xiaoge_send_cmd_result_event` | 基于命令事件发送结果（推荐） |
| `xiaoge_send_cmd_ack` | 基于原始 cmd JSON 发送 ack（兼容） |
| `xiaoge_send_cmd_result` | 基于原始 cmd JSON 发送结果（兼容） |
| `xiaoge_send_cmd_result_ex` | 基于原始 cmd JSON 发送带时间信息的结果（兼容） |
| `xiaoge_service` | 驱动 SDK WebSocket 事件循环与自动保活 |
| `xiaoge_destroy` | 释放客户端资源 |
