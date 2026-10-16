# Xiaoge C SDK R5.2.2 接口调用说明

本文档说明 Xiaoge C SDK R5.2.2 的主要接口、调用流程和 demo 使用方式，适用于端侧设备接入 Xiaoge Duplex `/ws/session` 主链路。

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
7. 设备侧发送 PCM 音频 / 前端状态 / 命令执行结果
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
| 最大 binary 帧 | 32768 bytes |

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
    "audio,text,cmd,state",                       // caps_csv
    "xiaoge-c-sdk-r5.2.2",                        // client_version
    "{}",                                         // prefs_json
    NULL,                                         // api_key，可选
    XIAOGE_AUTO_FAKE_CMD_DEFAULT                  // 命令自动模拟执行
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
| `auto_fake_cmd_executor` | 是否启用 SDK 内置命令模拟执行 |

`caps_csv` 当前支持：

```text
audio,text,cmd,state
```

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

注册回调：

```c
xiaoge_callbacks cb = {
    on_ready,
    on_audio,
    on_clear,
    on_state,
    on_stt,
    on_reply,
    on_cmd,
    on_error,
    user_data
};
```

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

### 3.8 销毁客户端

使用结束后调用：

```c
xiaoge_destroy(client);
```

---

## 4. 回调函数说明

### 4.1 回调结构体

```c
typedef struct {
    void (*on_ready)(int sample_rate, void *user);
    void (*on_audio)(const void *pcm, size_t len, void *user);
    void (*on_clear)(const char *json, void *user);
    void (*on_state)(const char *json, void *user);
    void (*on_stt)(const char *json, void *user);
    void (*on_reply)(const char *json, void *user);
    void (*on_cmd)(const char *json, void *user);
    void (*on_error)(const char *json, void *user);
    void *user;
} xiaoge_callbacks;
```

---

### 4.2 `on_ready`

云端 Session 就绪时触发。

```c
static void on_ready(int sample_rate, void *user) {
    printf("ready sample_rate=%d\n", sample_rate);
}
```

通常在此之后开始发送音频。

---

### 4.3 `on_audio`

收到下行 TTS PCM 音频时触发。

```c
static void on_audio(const void *pcm, size_t len, void *user) {
    // 播放或缓存 PCM
}
```

音频格式同样为：

```text
16000 Hz / mono / signed 16-bit little-endian PCM
```

---

### 4.4 `on_clear`

收到清空播放指令时触发。

```c
static void on_clear(const char *json, void *user) {
    // 清空本地播放缓冲
}
```

常见用途：用户打断时，立即停止播放旧回复。

---

### 4.5 `on_state`

收到服务端状态事件时触发。

```c
static void on_state(const char *json, void *user) {
    printf("state: %s\n", json);
}
```

---

### 4.6 `on_stt`

收到语音识别文本时触发。

```c
static void on_stt(const char *json, void *user) {
    printf("stt: %s\n", json);
}
```

---

### 4.7 `on_reply`

收到助手文本回复时触发。

```c
static void on_reply(const char *json, void *user) {
    printf("reply: %s\n", json);
}
```

---

### 4.8 `on_cmd`

收到云端下发命令时触发。

```c
static void on_cmd(const char *cmd_json, void *user) {
    printf("cmd: %s\n", cmd_json);
}
```

如果使用真实设备执行命令，应在此回调中解析命令，并发送 ack/result。

---

### 4.9 `on_error`

收到错误事件时触发。

```c
static void on_error(const char *json, void *user) {
    fprintf(stderr, "error: %s\n", json);
}
```

---

## 5. 命令执行接口

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

然后在 `on_cmd` 中手动上报命令状态。

---

### 5.1 发送命令 ack

```c
xiaoge_send_cmd_ack(
    client,
    cmd_json,
    "accepted",
    "ok",
    "queued"
);
```

`status` 支持：

| status | 含义 |
|---|---|
| `accepted` | 已接受 |
| `rejected` | 已拒绝 |
| `duplicate` | 重复命令 |

---

### 5.2 发送命令结果

```c
xiaoge_send_cmd_result(
    client,
    cmd_json,
    "running",
    "ok",
    "started",
    0
);
```

`status` 支持：

| status | 含义 |
|---|---|
| `running` | 执行中 |
| `succeeded` | 执行成功 |
| `failed` | 执行失败 |
| `canceled` | 已取消 |
| `timeout` | 执行超时 |

`retryable`：

| 值 | 含义 |
|---|---|
| `0` | 不建议重试 |
| `1` | 可以重试 |

---

### 5.3 发送带时间信息的命令结果

```c
xiaoge_send_cmd_result_ex(
    client,
    cmd_json,
    "succeeded",
    "ok",
    "completed",
    0,
    started_at_ms,
    finished_at_ms,
    duration_ms
);
```

---

### 5.4 真实命令执行示例

```c
static xiaoge_client *g_client;

static void on_cmd(const char *cmd_json, void *user) {
    (void)user;

    xiaoge_send_cmd_ack(g_client, cmd_json, "accepted", "ok", "queued");
    xiaoge_send_cmd_result(g_client, cmd_json, "running", "ok", "started", 0);

    // TODO: 在这里执行真实机器人动作

    xiaoge_send_cmd_result(g_client, cmd_json, "succeeded", "ok", "completed", 0);
}
```

---

## 6. 最小接入示例

```c
#include "xiaoge_client.h"

#include <stdio.h>

static int g_ready = 0;

static void on_ready(int sample_rate, void *user) {
    (void)user;
    g_ready = 1;
    printf("ready sample_rate=%d\n", sample_rate);
}

static void on_audio(const void *pcm, size_t len, void *user) {
    (void)pcm;
    (void)user;
    printf("received audio len=%zu\n", len);
}

static void on_text(const char *json, void *user) {
    (void)user;
    printf("%s\n", json);
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
        on_ready,
        on_audio,
        on_text,
        on_text,
        on_text,
        on_text,
        on_text,
        on_text,
        NULL
    };

    xiaoge_client *client = xiaoge_create_from_session_with_ca(
        &cfg,
        &sess,
        "certs/cloud-ca.pem",
        0,
        &cb
    );

    if (!client) {
        fprintf(stderr, "failed to connect\n");
        return 1;
    }

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

## 7. Demo 使用说明

### 7.1 构建 SDK 和 demo

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

### 7.2 文件 demo

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

### 7.3 Windows 麦克风 demo

`xiaoge_demo_mic_win` 是 Windows 实时麦克风 demo：

- 使用 Windows `waveIn` 采集麦克风
- 发送 16 kHz mono int16 PCM
- 打印下行 JSON
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
| `--ca-cert path` | 指定 CA 证书 |
| `--insecure` | 跳过证书校验，仅测试使用 |
| `--api-key key` | 指定 create_session 的 API Key |

---

## 8. 本地代理说明

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

## 9. 错误处理建议

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

## 10. 接入注意事项

1. **必须使用 `/ws/session`**：SDK 会拒绝 `/ws/audio` 路径。
2. **WebSocket 使用 Bearer Token**：SDK 在 WSS 握手时自动添加 `Authorization: Bearer <access_token>`。
3. **create_session HTTP 由业务侧负责**：SDK 只提供 JSON 构造接口，demo 中的 libcurl 代码仅作为参考。
4. **正式环境不要使用 `--insecure`**：应使用证书 SAN 匹配的域名，或指定 CA bundle。
5. **持续调用 `xiaoge_service`**：否则 WebSocket 收发不会推进。
6. **收到 `ctrl.clear` 应清空本地播放**：避免用户打断后继续播放旧音频。
7. **音频帧不要超过限制**：单个 binary 帧最大 `32768` bytes，推荐 320–640 bytes 小帧连续发送。
8. **真实机器人命令执行时关闭 fake executor**：设置 `cfg.auto_fake_cmd_executor = XIAOGE_AUTO_FAKE_CMD_DISABLED;`。

---

## 11. 常用接口速查

| 接口 | 作用 |
|---|---|
| `xiaoge_build_create_session_request` | 构造 create_session 请求 JSON |
| `xiaoge_create_from_session` | 使用 session 信息创建客户端 |
| `xiaoge_create_from_session_with_ca` | 使用 session 信息和 CA 证书创建客户端 |
| `xiaoge_send_pcm` | 发送上行 PCM 音频 |
| `xiaoge_send_frontend_state` | 发送前端状态 |
| `xiaoge_send_cmd_ack` | 发送命令 ack |
| `xiaoge_send_cmd_result` | 发送命令执行结果 |
| `xiaoge_send_cmd_result_ex` | 发送带时间信息的命令执行结果 |
| `xiaoge_service` | 驱动 SDK WebSocket 事件循环 |
| `xiaoge_destroy` | 释放客户端资源 |
