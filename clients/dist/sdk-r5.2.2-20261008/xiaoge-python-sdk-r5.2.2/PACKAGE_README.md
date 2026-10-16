# Xiaoge Python SDK R5.2.2 客户交付包

本交付包为 Python 客户端集成 Xiaoge Duplex R5.2.2 `/ws/session` 主链路准备，包含 Python SDK 源码、文件/麦克风 Demo、中文调用说明、协议文档、测试验证脚本、证书以及包内文件校验清单。

## 目录结构

```text
sdk/                              Python SDK 核心源码
examples/                         文件 Demo、实时麦克风 Demo
tests/                            本地 mock 自测、R5.2.2 契约回放
certs/                            云端/测试 Gateway CA PEM
docs/                             中文 Python SDK 调用说明
README.md                         clients SDK 总说明
PROTOCOL.md                       R5.2.2 协议说明
SDK_TEST_VALIDATION.md            SDK 验证说明
requirements.txt                  Python 依赖
PACKAGE_README.md                 本交付包总览
SHA256SUMS.txt                    包内文件校验
```

## 环境要求

- Python 3.10+。
- 核心依赖：`websockets>=12`。
- 契约回放依赖：`jsonschema>=4`。
- `examples/demo_mic.py` 额外需要 `sounddevice>=0.4`、可用麦克风和扬声器。

安装依赖：

```bash
python -m pip install -r requirements.txt
```

## 主链路

当前 SDK 主链路为：

```text
POST create_session
  -> session.created(trace_id, session_id, access_token, ws_url)
  -> WSS /ws/session + Authorization: Bearer <access_token>
  -> ctrl.hello
  -> ctrl.ready / ctrl.state / data.* / PCM
```

旧 `/ws/audio` 不是本 SDK 交付目标。

## 快速自测

```bash
python tests/selftest.py
```

期望输出包含：

```text
records=local-selftest failures=0
```

该测试只启动本地 mock `create_session` 和 mock `/ws/session`，不访问真实云端，也不会触发真实机器人动作。

## 契约回放

如有 R5.2.2 合同目录，可运行：

```bash
python tests/contract_replay.py --contract-dir <path-to-r5.2.2-02_contracts>
```

期望输出：

```text
records=59 failures=0
```

## 文件 Demo

输入 WAV 必须是 16000 Hz、mono、signed 16-bit PCM。

```bash
python examples/demo_file.py "https://gateway.example.com:10097/create_session" \
  robot-x3-001 \
  '{"key_id":"dev","signature":"mock"}' \
  in.wav \
  out_python.wav \
  --api-key "$XIAOGE_CLOUD_API_KEY" \
  --ca-cert certs/cloud-ca.pem \
  --trace-log trace_python.jsonl
```

期望至少看到：

```text
ready 16000
sent=<bytes> bytes received=<bytes> bytes out=out_python.wav
```

## 麦克风 Demo

```bash
python examples/demo_mic.py "https://gateway.example.com:10097/create_session" \
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

## 证书与安全

正式验收不建议使用 `--insecure`。生产/正式测试应使用证书 SAN 匹配的域名访问 Gateway，或使用 `--ca-cert certs/cloud-ca.pem` / 客户自己的 CA bundle。

`XIAOGE_CLOUD_API_KEY` 或 `--api-key` 只用于 `create_session` HTTP 请求的 `x-api-key` header。WebSocket 鉴权使用 `create_session` 返回的 `access_token` 和 `Authorization: Bearer <access_token>`。

## 安全边界

- SDK 收到 `data.cmd` 后只回调业务侧 `on_command(event)`（raw 观察入口为 `on_json(payload)`），不会默认发送 `data.cmd_ack` / `data.cmd_result`。
- 本交付包不连接真实机器人动作模块，不执行真实机器人动作。
- 本交付包不内置明文 API key。
- `--insecure` 仅允许在临时调试或隔离 fake-server 环境使用。

## 更新说明（2026-10-08 刷新）

- 下行事件解析改为宽松模式：服务端 JSON 字段缺失、多余或枚举值未知时，事件不再被丢弃，一律正常回调（缺失字段取类型默认值，多余字段忽略，未知枚举值透传）；每个事件对象新增 `parse_warnings` 字段记录全部偏离项，SDK 同时通过 `logging`（logger `xiaoge.client`）输出 `WARNING` 日志。详见 `docs/PYTHON_SDK_README.md`。
- `on_protocol_error` 语义收窄：仅覆盖无法解析的畸形 JSON 文本帧。
- `session.created` 响应允许多余字段；必填字段与 `ws_url` 路径校验保持不变。
- 出站帧校验（`cmd_ack`/`cmd_result` status 枚举、JSON 8192 字节上限）保持严格，与 R5.2.2 合同一致。

## 进一步说明

- Python SDK 调用说明：见 `docs/PYTHON_SDK_README.md`。
- 协议细节：见 `PROTOCOL.md`。
- 验证步骤：见 `SDK_TEST_VALIDATION.md`。
- 包内文件完整性：见 `SHA256SUMS.txt`。
