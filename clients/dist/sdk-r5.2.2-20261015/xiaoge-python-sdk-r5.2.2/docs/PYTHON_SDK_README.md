# Xiaoge Python SDK R5.2.2 中文调用说明

本文说明如何在 Python 应用中集成 Xiaoge Duplex R5.2.2 客户端 SDK，完成建连、上行语音、下行播放、文本/状态/命令回调，以及本地 Demo 和自测验证。

## 1. SDK 范围

Python SDK 当前实现的是 R5.2.2 主链路：

```text
POST create_session
  -> session.created(trace_id, session_id, access_token, ws_url)
  -> WSS /ws/session + Authorization: Bearer <access_token>
  -> ctrl.hello
  -> ctrl.ready / ctrl.state / data.* / PCM
```

历史 `/ws/audio` 不是本 SDK 的交付目标。收到服务端下发的 `data.cmd` 后，SDK 通过 `on_command(CommandEvent)` 交给业务侧处理。SDK 不会默认发送 `cmd_ack` / `cmd_result`，也不会执行真实机器人动作。业务侧确认要执行或拒绝命令时，应显式上报命令状态。所有 raw 下行 JSON 统一通过 `on_json(payload)` 观察。

## 2. 环境要求

- Python 3.10+。
- 核心 SDK 依赖：`websockets>=12`。
- 契约回放依赖：`jsonschema>=4`。
- 实时麦克风 Demo 依赖：`sounddevice>=0.4`，并需要可用的麦克风和扬声器设备。

安装依赖：

```bash
python -m pip install -r requirements.txt
```

> `sounddevice` 只被 `examples/demo_mic.py` 使用。如客户只集成 SDK 或只运行文件 Demo，可按需移除该依赖。

## 3. 目录结构

交付包推荐目录如下：

```text
sdk/xiaoge_client.py              核心 Python SDK
examples/demo_file.py             16 kHz mono int16 WAV 文件 Demo
examples/demo_mic.py              实时麦克风/扬声器 Demo
tests/selftest.py                 本地 mock create_session + /ws/session 自测
tests/contract_replay.py          R5.2.2 契约回放工具
certs/cloud-ca.pem                云端/测试 Gateway CA PEM
docs/PYTHON_SDK_README.md         本中文调用说明
README.md                         clients SDK 总说明
PROTOCOL.md                       R5.2.2 协议说明
SDK_TEST_VALIDATION.md            验证说明
requirements.txt                  Python 依赖
PACKAGE_README.md                 交付包总览
SHA256SUMS.txt                    包内文件校验
```

运行 Demo 或测试时，交付包脚本会在 `examples/` 和 `tests/` 下生成轻量启动文件，使 `from xiaoge_client import ...` 能加载 `sdk/xiaoge_client.py`。

## 4. API Key 与鉴权

`create_session` HTTP 请求如需云端 API key，可以通过两种方式传入：

1. 设置环境变量：

   ```bash
   export XIAOGE_CLOUD_API_KEY=your-cloud-api-key
   ```

2. 创建 `XiaogeClient` 时显式传 `api_key`，或运行 Demo 时传 `--api-key`。

SDK 只在 `create_session` HTTP 请求中携带非空 `x-api-key`。WebSocket 建连使用 `create_session` 返回的 `access_token`，请求头为：

```text
Authorization: Bearer <access_token>
```

SDK 不会把 API key、token 放进 `ctrl.hello` 或 WebSocket URL query 中。

## 5. TLS 与证书

推荐正式测试/生产使用证书 SAN 匹配的域名访问 Gateway，并保留证书校验：

```text
https://60.205.197.165:10099/create_session
wss://gateway.example.com:10097/ws/session
```

如服务端使用内部 CA，可传入 CA PEM：

```python
from xiaoge_client import default_ssl_context

ssl_ctx = default_ssl_context(ca_cert="certs/cloud-ca.pem")
```

Demo 对应参数：

```bash
--ca-cert certs/cloud-ca.pem
```

`--insecure` / `default_ssl_context(insecure=True)` 会跳过证书和主机名校验，仅允许在临时调试或隔离 fake-server 环境使用，不应用于正式验收和生产。

## 6. 核心 SDK 调用

```python
import asyncio
import os

from xiaoge_client import XiaogeClient, default_ssl_context


async def main() -> None:
    client = XiaogeClient(
        "https://60.205.197.165:10099/create_session",
        "robot-x3-001",
        {"key_id": "dev", "signature": "mock"},
        api_key=os.environ.get("XIAOGE_CLOUD_API_KEY", ""),
        ssl=default_ssl_context(ca_cert="certs/cloud-ca.pem"),
        trace_log_path="client_trace.jsonl",
    )

    client.on_ready = lambda sample_rate: print("ready", sample_rate)
    client.on_audio = lambda pcm: print("audio bytes", len(pcm))
    client.on_clear = lambda event: print("clear", event.reason)
    client.on_state = lambda event: print("state", event.link_state, event.interaction_mode)
    client.on_stt = lambda event: print("stt", event.text, event.is_final)
    client.on_reply = lambda event: print("reply", event.text, event.intent_type)
    client.on_command = lambda event: print("cmd", event.cmd_id, event.action)
    client.on_error = lambda event: print("error", event.code, event.message)
    client.on_json = lambda payload: print("raw", payload["type"])

    runner = asyncio.create_task(client.run())
    while client.frontend_state is None:
        await asyncio.sleep(0.01)

    await client.send_frontend_state(trust_level="hint", wake_state="awake", vad="speech")
    await client.send_pcm(b"\x00" * 320)
    await client.close()
    await runner


asyncio.run(main())
```

### 常用事件回调

| 回调 | 说明 |
| --- | --- |
| `on_ready(sample_rate)` | 简洁 ready 回调，服务端 `ctrl.ready` 到达，语音链路可用。 |
| `on_ready_event(event)` | 完整 ready 事件，包含 `sample_rate`、`granted_caps`、`config_version` 和 trace/session。 |
| `on_audio(pcm)` | 服务端下行 PCM，可写入播放器或 WAV。 |
| `on_clear(event)` | 服务端清理/打断事件，常用 `event.reason`。 |
| `on_state(event)` | 服务端状态事件，常用 `event.interaction_mode`、`event.resource_state`。 |
| `on_stt(event)` | 用户语音识别文本事件，常用 `event.text`、`event.is_final`。 |
| `on_reply(event)` | 助手回复文本事件，常用 `event.text`、`event.intent_type`。 |
| `on_command(event)` | 服务端下发命令事件，常用 `event.cmd_id`、`event.action`、`event.params`。 |
| `on_error(event)` | 服务端 `data.error` 事件，常用 `event.code`、`event.retryable`。 |
| `on_stt_text(text, is_final)` | 便捷 STT 文本回调。 |
| `on_reply_text(text)` | 便捷回复文本回调。 |
| `on_json(payload)` | 所有下行 JSON raw observer，适合日志/协议审计。 |
| `on_protocol_error(event)` | SDK 本地入站错误，目前仅覆盖无法解析的畸形 JSON 文本帧；不是服务端 `data.error`。 |
| `on_failure(error)` | SDK 本地 failure/异常通道。 |

`on_clear`、`on_state`、`on_stt`、`on_reply`、`on_command`、`on_error` 均接收类型化事件对象。`on_json(payload)` 是唯一 raw observer，适合日志、协议审计或读取尚未建模的字段；业务处理不要同时在 `on_json` 和类型化命令回调里执行同一命令。

事件字段均从原始 payload 拷贝而来，`event.raw` 保留原始协议内容供调试。`ReplyEvent.is_final` 是 SDK 为统一文本事件接口派生的值，R5.2.2 `data.reply` 协议 payload 没有 `final` 字段，SDK 不会把它写入 raw payload。

### 状态枚举常量

`ctrl.state` 的四个合同枚举提供 `str` 枚举常量，用于替代手写字符串比较（拼错的字符串不报错，只会永不匹配）：

```python
from xiaoge_client import InteractionMode

def on_state(event):
    if event.interaction_mode == InteractionMode.SLEEPING:
        ...  # 休眠态 UI/逻辑

client.on_state = on_state
```

| 枚举 | 取值 |
| --- | --- |
| `LinkState` | `CONNECTING` / `CONNECTED` / `RECONNECTING` / `CLOSED` |
| `InteractionMode` | `SLEEPING` / `DIALOGUE` / `LISTENING` |
| `EngineGate` | `CLOSED` / `OPEN` / `KWS_ONLY` |
| `ResourceState` | `SLEEPING_HOT` / `SLEEPING_WARM` / `ACTIVE_AGENT` / `RELEASED_IDLE` / `PENDING_RECONNECT` |

枚举混入 `str`，可直接与 `event.link_state` 等字符串字段比较（`InteractionMode.SLEEPING == "sleeping"` 为 `True`）。字段类型保持字符串：未知协议值仍按宽松解析原样透传并记入 `parse_warnings`。注意业务三态 `interaction_mode` 与引擎 `agent_state` 是两个概念，不要混用。

### 交互状态 interaction_mode 获取方式

`interaction_mode` 只能从 `on_state` 回调的事件对象获取；SDK 不缓存当前状态、也不提供查询接口，业务侧自行保存最新值：

```python
latest_mode: str | None = None

def on_state(event):
    global latest_mode
    latest_mode = event.interaction_mode
    if event.interaction_mode == InteractionMode.SLEEPING:
        ...  # 休眠态 UI/逻辑

client.on_state = on_state
```

- **下发时机**：握手完成后紧跟 `ctrl.ready` 下发一次初始状态，之后服务端每次状态变化重发。
- **快照语义**：每条 `ctrl.state` 都是四字段的**全量快照**，广播可能重复；按最新一条更新 UI/状态机，不必把每帧当作新事件（DEVICE_PROTOCOL §3.1）。
- **三态含义**：

| `interaction_mode` | 含义 | 典型 `engine_gate` | 典型 `resource_state` |
| --- | --- | --- | --- |
| `sleeping` | 业务休眠，等待唤醒/有效输入 | `open` | `SleepingHot` |
| `dialogue` | 正常交互模式 | `open` | `ActiveAgent` |
| `listening` | 收听输入，但被拒轮次不进入正常回复 | `open` | `ActiveAgent` |

- **注意**：实际 wire 字段是 `interaction_mode`（不是 `interaction_state`）；当前三态全部 `engine_gate="open"`，不要因 sleeping/listening 自行停止上传音频；`link_state` 是连接状态，不代表是否在对话；SDK 引擎状态 `agent_state` 不等于业务三态。比较方式见上文「状态枚举常量」。

### 应用层保活（ctrl.ping）

`run()` 在发送 `ctrl.hello` 后自动启动应用层保活：每 25 秒发送一次
`{"type":"ctrl.ping","ts_ms":<epoch ms>}`（`send_json` 会照常写入 trace，`message_type=ctrl.ping`）。
25 秒是客户端策略（对齐 DEVICE_PROTOCOL §4.2 的浏览器前端），不是服务端强制期限。

- 调整周期：在 `run()` 之前给 `client.ping_interval_s` 赋值；`<= 0` 关闭自动保活。
- 手动补发：`await client.send_ping()`。
- 收到 `ctrl.pong` 时 SDK 按 `client_ts_ms` 回显用客户端时钟计算 RTT 并输出 `DEBUG` 日志，
  随后仍交给 `on_json(payload)`；pong 不会唤醒 dialogue，也不延长业务 listening/idle 超时。
- 保活仅保持传输/会话活性，SDK 不做"连续 pong 未收到则重连"的检测；如需该容错策略请在业务层实现。

### 双工开关（ctrl.duplex）

```python
await client.send_duplex(False)   # 关闭双工；True 重新开启

client.on_duplex_state = lambda event: print("duplex enabled =", event.enabled)
```

- `send_duplex(enabled)` 发送 `{"type":"ctrl.duplex","enabled":<bool>}`（DEVICE_PROTOCOL §3.3）；
  入参必须是 bool，传字符串/数字会抛 `XiaogeProtocolError`。
- 服务端成功调度后回 `ctrl.duplex_state`，SDK 解析为 `DuplexStateEvent`（`enabled`、`ts_ms`、
  `trace_id`、`session_id`、`raw`、`parse_warnings`）并触发 `on_duplex_state`，同时照常走 `on_json`。
- 回执语义是**已调度**，不等于 agent loop 已完成赋值；开关自身不直接修改业务三态
  （sleeping/dialogue/listening 仍以 `ctrl.state` 为准）。
- 没有 `full_duplex` wire 字段；`ctrl.mic_state` 未支持，不要发送。

### 下行事件宽松解析

下行事件不会因服务端协议字段漂移而丢失：字段缺失时以类型默认值交付（字符串 `""`、数字 `0`、布尔 `False`、对象/数组为空），多余字段忽略（仍可在 `event.raw` 中访问），未知枚举值（如新增 `intent_type`/错误码）原样透传；Python 端不做数字/布尔字符串转型，类型不符时直接给默认值并告警。每个事件对象携带 `parse_warnings`（`tuple[str, ...]`，完全合规时为空）记录全部偏离项，SDK 会通过 `logging`（logger `xiaoge.client`）以 `WARNING` 级别输出，便于发现服务端协议漂移。业务侧如需严格判断，可检查 `not event.parse_warnings` 或 `event.text` 是否为空。

## 7. 音频格式

SDK 默认协议音频格式为：

```text
sample_rate: 16000
channels: 1
sample_format: int16le
```

也就是 16 kHz、mono、signed 16-bit little-endian PCM。单个二进制 PCM frame 最大 32768 bytes。文件 Demo 按 20 ms 一帧发送，即每帧 640 bytes。

输入 WAV 必须是 16000 Hz、mono、signed 16-bit PCM，否则 `demo_file.py` 会拒绝运行。

## 8. 命令处理

SDK 收到下行 `data.cmd` 后，推荐调用 `on_command(CommandEvent)` 交给业务侧处理：

- SDK 不会默认发送 `data.cmd_ack`。
- SDK 不会默认发送 `data.cmd_result`。
- SDK 不会调用真实机器人动作模块。
- 业务侧如果要执行或拒绝命令，推荐使用 `send_command_ack(event, ...)` / `send_command_result(event, ...)`，由 SDK 从事件中复制 `trace_id/session_id/utterance_id/cmd_id`。

```python
from xiaoge_client import CmdAckStatus, CmdResultStatus, CommandEvent


async def handle_command(client: XiaogeClient, event: CommandEvent) -> None:
    await client.send_command_ack(event, CmdAckStatus.ACCEPTED, "ok", "queued")
    await client.send_command_result(event, CmdResultStatus.RUNNING, "ok", "started")
    await client.send_command_result(event, CmdResultStatus.SUCCEEDED, "ok", "completed")


client.on_command = lambda event: asyncio.create_task(handle_command(client, event))
```

如需观察原始协议帧，请注册 `on_json(payload)`；命令执行仍应只绑定 `on_command(event)`，避免重复 ack/result 或重复执行动作。

业务侧自行接管真实命令执行时，建议使用 SDK 枚举或常量，避免手写非法状态：

```python
from xiaoge_client import CmdAckStatus, CmdResultStatus, ProtocolCodec

ack = ProtocolCodec.cmd_ack(command, CmdAckStatus.ACCEPTED, "ok", "queued")
running = ProtocolCodec.cmd_result(command, CmdResultStatus.RUNNING, "ok", "started")
succeeded = ProtocolCodec.cmd_result(command, CmdResultStatus.SUCCEEDED, "ok", "completed")
# 可选:在 cmd_result 中携带业务返回值
succeeded = ProtocolCodec.cmd_result(
    command, CmdResultStatus.SUCCEEDED, "ok", "completed",
    result={"type": "cap", "value": 40, "gauge_id": 1},
)
```

合法 `cmd_ack` status 为 `accepted`、`rejected`、`duplicate`；合法 `cmd_result` status 为 `running`、`succeeded`、`failed`、`canceled`、`timeout`。SDK 仍兼容合法原始字符串，但非法 status 会在本地抛出 `XiaogeProtocolError`，不会发送到服务端。`result` 为可选关键字参数,不传时消息中不含该字段,与旧协议兼容。

## 9. 本地自测

```bash
python tests/selftest.py
```

期望输出包含：

```text
records=local-selftest failures=0
```

该测试会启动本地 mock `create_session` 和 mock `/ws/session`，不访问真实云端，也不会触发真实机器人动作。

## 10. 契约回放

如有 R5.2.2 合同目录，可运行：

```bash
python tests/contract_replay.py --contract-dir <path-to-r5.2.2-02_contracts>
```

期望输出：

```text
records=59 failures=0
```

## 11. 文件 Demo

```bash
python examples/demo_file.py \
  "https://60.205.197.165:10099/create_session" \
  robot-x3-001 \
  '{"key_id":"dev","signature":"mock"}' \
  in.wav \
  out_python.wav \
  --api-key "$XIAOGE_CLOUD_API_KEY" \
  --ca-cert certs/cloud-ca.pem \
  --trace-log trace_python.jsonl
```

临时测试 IP HTTPS 或自签证书不完整环境可加 `--insecure`，但正式验收不应使用。

期望至少看到：

```text
ready 16000
sent=<bytes> bytes received=<bytes> bytes out=out_python.wav
```

判断：

- `ready 16000`：`create_session`、WSS Bearer、`ctrl.hello`、`ctrl.ready` 已通过。
- `sent > 0`：PCM 上行已发送。
- `received > 0`：服务端有 TTS PCM 下行。
- `received = 0`：客户端链路可能仍成功，但服务端/agent 没有下发音频，需要查服务端 ASR/TTS/agent。

双工开关验证：加 `--duplex off`（或 `--duplex on`），连接后 demo 发送 `ctrl.duplex` 并在收到
`ctrl.duplex_state` 回执时打印 `duplex_state: enabled= ...`。`--duplex off` 时语音仍会上传，
但服务端不再回复（转写仍可见、无 `reply:`/下行音频），可用于对照验证开关生效。

## 12. 麦克风 Demo

```bash
python examples/demo_mic.py \
  "https://60.205.197.165:10099/create_session" \
  robot-x3-001 \
  '{"key_id":"dev","signature":"mock"}' \
  --api-key "$XIAOGE_CLOUD_API_KEY" \
  --ca-cert certs/cloud-ca.pem \
  --trace-log trace_mic.jsonl
```

期望输出：

```text
ready 16000
talking; press Ctrl-C to exit
```

麦克风 Demo 同样支持 `--duplex off|on`：连接后发送 `ctrl.duplex`，收到回执打印
`duplex_state: enabled= ...`；关闭双工时持续说话可观察"stt final: 仍打印、reply: 不再出现"。

如果服务端返回最终识别结果，demo 会打印：

```text
stt final: <用户最终识别文本>
```

如果服务端返回回复文本，demo 会打印：

```text
reply: <回复文本>
```

Ctrl-C 退出。

## 13. 常见问题

### `create_session failed HTTP 401/403`

检查 `XIAOGE_CLOUD_API_KEY` 或 `--api-key` 是否配置、是否与目标 Gateway 环境匹配。

### 连接到了 `/ws/audio` 或 URL 带 query token

确认 Gateway 返回的 `ws_url` 是精确的 `/ws/session`。SDK 会拒绝历史 `/ws/audio`、带 query token 或 fragment 的旧链路。

### TLS 证书失败

正式环境优先使用证书 SAN 匹配的域名和 CA bundle。如果证书 SAN 与 IP/域名不匹配，需要使用证书匹配的访问地址或更新证书配置。不要用 `--insecure` 作为正式解决方案。

### 麦克风 Demo 无声音或无法打开设备

确认系统麦克风权限、默认输入/输出设备和 PortAudio/sounddevice 安装均正常。服务器没有下发 TTS 时，扬声器侧也可能保持静音。
