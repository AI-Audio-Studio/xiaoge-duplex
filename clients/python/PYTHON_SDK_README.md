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
| `on_protocol_error(event)` | SDK 本地入站 JSON/字段校验失败；不是服务端 `data.error`。 |
| `on_failure(error)` | SDK 本地 failure/异常通道。 |

`on_clear`、`on_state`、`on_stt`、`on_reply`、`on_command`、`on_error` 均接收类型化事件对象。`on_json(payload)` 是唯一 raw observer，适合日志、协议审计或读取尚未建模的字段；业务处理不要同时在 `on_json` 和类型化命令回调里执行同一命令。

事件字段均从原始 payload 拷贝而来，`event.raw` 保留原始协议内容供调试。`ReplyEvent.is_final` 是 SDK 为统一文本事件接口派生的值，R5.2.2 `data.reply` 协议 payload 没有 `final` 字段，SDK 不会把它写入 raw payload。

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
```

合法 `cmd_ack` status 为 `accepted`、`rejected`、`duplicate`；合法 `cmd_result` status 为 `running`、`succeeded`、`failed`、`canceled`、`timeout`。SDK 仍兼容合法原始字符串，但非法 status 会在本地抛出 `XiaogeProtocolError`，不会发送到服务端。

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
records=56 failures=0
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
