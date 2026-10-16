# Xiaoge Clients SDK 测试验证说明

本文档说明 `clients` 目录内 Python、C、MATLAB SDK 如何验证可用性，重点覆盖 demo 运行方式，以及三种 TLS 场景：

- 临时跑通：使用 `--insecure` 跳过证书校验，只允许在隔离测试环境使用。
- 正式正确做法：使用证书 SAN 匹配的域名访问 HTTPS/WSS。
- 自签 CA：使用 `--ca-cert` 指定 PEM CA 文件，保留证书校验。

当前 R5.2.2 主链路必须是：

```text
POST create_session
  -> session.created(trace_id, session_id, access_token, ws_url)
  -> WSS /ws/session + Authorization: Bearer <access_token>
  -> ctrl.hello
  -> ctrl.ready / ctrl.state / data.* / PCM
```

旧 `/ws/audio` 不是本批 SDK demo 的验证目标。

## 0. 通用准备

### 0.1 测试 Gateway

测试 Gateway 需要提供：

```text
https://<host>:<port>/create_session
wss://<host>:<port>/ws/session
```

示例联调地址：

```text
https://60.205.197.165:10099/create_session
```

如果直接使用 IP HTTPS，服务端证书通常不会包含该 IP 的 SAN，会出现：

```text
SSL: CERTIFICATE_VERIFY_FAILED
IP address mismatch
```

这种情况下临时测试可以加 `--insecure`；正式验收应改用证书中的域名，或重新签发包含目标域名/IP 的证书。

### 0.2 通用变量

PowerShell 示例：

```powershell
$CLIENTS = "E:\Project\Project2026\AIAudioCloudPlatform\xiaogeV2\xiaoge-duplex\xiaoge-duplex\clients"
$CREATE_SESSION_URL = "https://60.205.197.165:10099/create_session"
$DEVICE_ID = "robot-x3-001"
$CREDENTIAL = '{"key_id":"dev","signature":"mock"}'
$API_KEY = $env:XIAOGE_CLOUD_API_KEY  # 或直接填入测试 key："<your-api-key>"
```

`create_session` HTTP 如需 `x-api-key`，由环境变量 `XIAOGE_CLOUD_API_KEY` 或命令行 `--api-key <key>` 显式注入；SDK/评审包不内置明文 key。以下所有真实 Gateway 运行命令均显式写出 `--api-key $API_KEY`，没有 key 的纯本地 fake Gateway 可把该参数省略或令 `$API_KEY` 为空。

### 0.3 本地代理模式

如果防火墙拦截 C demo 直连公网，但允许 Python 出站，可以先启动本地代理，让 C demo 只连接 `127.0.0.1`：

```powershell
cd $CLIENTS
python .\c\local_proxy.py
```

代理默认提供：

```text
http://127.0.0.1:10097/create_session -> https://60.205.197.165:10099/create_session
ws://127.0.0.1:10097/ws/session       -> wss://60.205.197.165:10097/ws/session
```

`local_proxy.py` 默认关闭上游 TLS 证书校验，仅用于本地/隔离联调。启动后保持该窗口运行，再在另一个 PowerShell 窗口运行 C demo。

### 0.4 准备输入音频

`demo_file` 输入必须是 16000 Hz、mono、signed 16-bit PCM WAV。

可生成一个 2 秒测试音频：

```powershell
cd $CLIENTS
@'
import wave, math, struct
sr = 16000
with wave.open("in.wav", "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(sr)
    for i in range(sr * 2):
        v = int(12000 * math.sin(2 * math.pi * 440 * i / sr))
        w.writeframes(struct.pack("<h", v))
'@ | python -
```

验收时建议同时准备一段真实语音 WAV。纯 tone 可以验证传输链路，但不一定触发 ASR/TTS。

### 0.5 TLS 三种运行方式

#### 临时跑通：`--insecure`

适用于 IP 地址、自签证书、证书未配置完整的测试环境：

```powershell
--insecure
```

风险：跳过服务器身份校验，不能作为正式验收结论。

#### 正式正确做法：使用证书匹配的域名

如果证书签给 `gateway.example.com`，命令中应使用：

```text
https://60.205.197.165:10099/create_session
```

不要使用：

```text
https://60.205.197.165:10099/create_session
```

如测试环境 DNS 未配置，可临时 hosts 解析：

```text
60.205.197.165 gateway.example.com
```

然后用域名运行 demo，不加 `--insecure`。

#### 自签 CA：`--ca-cert`

如果服务端证书由内部 CA 签发，把 CA PEM 文件放到：

```text
clients/certs/cloud-ca.pem
```

Python demo、MATLAB bridge 默认会尝试使用该文件；也可以显式指定：

```powershell
--ca-cert ..\certs\cloud-ca.pem
```

C demo 也支持：

```powershell
--ca-cert ..\certs\cloud-ca.pem
```

注意：自签 CA 仍要求访问 URL 的 host 与证书 SAN 匹配。CA 只解决“信任链”，不解决“IP/域名不匹配”。

## 1. Python SDK 验证

目录：

```powershell
cd E:\Project\Project2026\AIAudioCloudPlatform\xiaogeV2\xiaoge-duplex\xiaoge-duplex\clients\python
```

### 1.1 安装依赖

```powershell
python -m pip install -r requirements.txt
```

`demo_mic.py` 需要麦克风/扬声器设备，并依赖 `sounddevice`。

### 1.2 离线自测

```powershell
python selftest.py
```

期望输出：

```text
records=local-selftest failures=0
```

该测试会启动本地 mock `create_session` 和 mock `/ws/session`，不访问真实云端，也不会触发真实机器人动作。

### 1.3 契约回放

```powershell
python contract_replay.py --contract-dir E:\Project\Project2026\AIAudioCloudPlatform\xiaogeV2\g1_contract_signoff_package_r21_r5_2_2_20260804\02_contracts
```

期望输出：

```text
records=59 failures=0
```

### 1.4 文件 demo：临时跑通

使用 IP HTTPS 时加 `--insecure`：

```powershell
python demo_file.py `
  "https://60.205.197.165:10099/create_session" `
  robot-x3-001 `
  '{"key_id":"dev","signature":"mock"}' `
  ..\in.wav `
  ..\out_python.wav `
  --api-key $API_KEY `
  --insecure `
  --trace-log ..\trace_python.jsonl
```

期望至少看到：

```text
ready 16000
sent=<bytes> bytes received=<bytes> bytes out=..\out_python.wav
```

判断：

- 出现 `ready 16000`：`create_session`、WSS Bearer、`ctrl.hello`、`ctrl.ready` 已通过。
- `sent > 0`：PCM 上行已发送。
- `received > 0`：服务端有 TTS PCM 下行。
- `received = 0`：客户端链路可能仍成功，但服务端/agent 没有下发音频，需要查服务端 ASR/TTS/agent。

### 1.5 文件 demo：正式域名证书

```powershell
python demo_file.py `
  "https://60.205.197.165:10099/create_session" `
  robot-x3-001 `
  '{"key_id":"dev","signature":"mock"}' `
  ..\in.wav `
  ..\out_python.wav `
  --api-key $API_KEY `
  --trace-log ..\trace_python.jsonl
```

正式验收不应使用 `--insecure`。

### 1.6 文件 demo：自签 CA

```powershell
python demo_file.py `
  "https://60.205.197.165:10099/create_session" `
  robot-x3-001 `
  '{"key_id":"dev","signature":"mock"}' `
  ..\in.wav `
  ..\out_python.wav `
  --api-key $API_KEY `
  --ca-cert ..\certs\cloud-ca.pem `
  --trace-log ..\trace_python.jsonl
```

### 1.7 麦克风 demo：临时跑通

```powershell
python demo_mic.py `
  "https://60.205.197.165:10099/create_session" `
  robot-x3-001 `
  '{"key_id":"dev","signature":"mock"}' `
  --api-key $API_KEY `
  --insecure `
  --trace-log ..\trace_mic.jsonl
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

### 1.8 麦克风 demo：正式域名证书

```powershell
python demo_mic.py `
  "https://60.205.197.165:10099/create_session" `
  robot-x3-001 `
  '{"key_id":"dev","signature":"mock"}' `
  --api-key $API_KEY `
  --trace-log ..\trace_mic.jsonl
```

### 1.9 麦克风 demo：自签 CA

```powershell
python demo_mic.py `
  "https://60.205.197.165:10099/create_session" `
  robot-x3-001 `
  '{"key_id":"dev","signature":"mock"}' `
  --api-key $API_KEY `
  --ca-cert ..\certs\cloud-ca.pem `
  --trace-log ..\trace_mic.jsonl
```

## 2. C SDK 验证

目录：

```powershell
cd E:\Project\Project2026\AIAudioCloudPlatform\xiaogeV2\xiaoge-duplex\xiaoge-duplex\clients\c
```

C SDK 核心库仍只负责 WSS session 连接和协议帧；为了便于端侧联调，`xiaoge_demo_file` 已内置 libcurl HTTP POST，会先调用 `create_session`，再连接返回的 `ws_url`。

### 2.1 环境要求

需要：

- CMake
- C 编译器
- libwebsockets，且带 TLS 支持
- libcurl，供 `xiaoge_demo_file` 内置 HTTP `create_session` 使用
- Linux/类 Linux 构建通常还需要 pthread

Linux 示例：

```bash
sudo apt-get update
sudo apt-get install -y cmake gcc libwebsockets-dev libcurl4-openssl-dev
```

Windows 建议使用 MSYS2/MinGW 或目标嵌入式 Linux 工具链。当前本机已可用：

```powershell
$env:PATH = "D:\msys64\mingw64\bin;D:\Android\Android SDK\cmake\4.1.0\bin;$env:PATH"
```

### 2.2 构建和本地 codec 测试

```powershell
cmake -S . -B build-mingw64 -G Ninja
cmake --build build-mingw64
ctest --test-dir build-mingw64 --output-on-failure
```

期望输出包含：

```text
records=c-codec-smoke failures=0
```

### 2.3 C 文件 demo：内置 HTTP create_session

临时跑通 IP HTTPS 版本，跳过证书校验：

```powershell
cd E:\Project\Project2026\AIAudioCloudPlatform\xiaogeV2\xiaoge-duplex\xiaoge-duplex\clients\c
.\build-mingw64\xiaoge_demo_file.exe `
  "https://60.205.197.165:10099/create_session" `
  robot-x3-001 `
  '{"key_id":"dev","signature":"mock"}' `
  ..\in.wav `
  ..\out_c.wav `
  --api-key $API_KEY `
  --insecure
```

正式域名证书版本：把 URL 改为证书 SAN 匹配的域名，并去掉 `--insecure`。

```powershell
.\build-mingw64\xiaoge_demo_file.exe `
  "https://60.205.197.165:10099/create_session" `
  robot-x3-001 `
  '{"key_id":"dev","signature":"mock"}' `
  ..\in.wav `
  ..\out_c.wav `
  --api-key $API_KEY
```

自签 CA 版本：URL 仍必须使用证书 SAN 匹配的域名，通过 `--ca-cert` 指定 CA 文件。

```powershell
.\build-mingw64\xiaoge_demo_file.exe `
  "https://60.205.197.165:10099/create_session" `
  robot-x3-001 `
  '{"key_id":"dev","signature":"mock"}' `
  ..\in.wav `
  ..\out_c.wav `
  --api-key $API_KEY `
  --ca-cert ..\certs\cloud-ca.pem
```

API key 已在上述命令中通过 `--api-key $API_KEY` 显式传入；如果云侧不要求 create_session key，可省略该参数或令 `$API_KEY` 为空。

期望输出包含：

```text
session trace_id=<trace_id> session_id=<session_id> ws_url=<ws_url>
ready sample_rate=16000
sent=<bytes> received=<bytes> out=..\out_c.wav
```

- `ready sample_rate=16000`：WSS Bearer、`ctrl.hello`、`ctrl.ready` 已通过。
- `sent > 0`：PCM 上行已发送。
- demo 返回码为 `0` 且 `sent` 等于输入 wav 的 PCM data 字节数：整段文件已发完。
- `received > 0`：服务端有 TTS PCM 下行。

### 2.4 C 麦克风 demo：本地代理模式

`xiaoge_demo_mic_win` 是 Windows 实时麦克风 demo，使用 Windows `waveIn` 采集 16 kHz、mono、int16 PCM，通过 C SDK 发送到 `/ws/session`。该 demo 默认连接本地代理：

```text
http://127.0.0.1:10097/create_session
```

先在一个 PowerShell 窗口启动代理：

```powershell
cd E:\Project\Project2026\AIAudioCloudPlatform\xiaogeV2\xiaoge-duplex\xiaoge-duplex\clients
python .\c\local_proxy.py
```

再在另一个 PowerShell 窗口运行麦克风 demo：

```powershell
cd E:\Project\Project2026\AIAudioCloudPlatform\xiaogeV2\xiaoge-duplex\xiaoge-duplex\clients
chcp 65001
$env:PATH = "D:\msys64\mingw64\bin;$env:PATH"
.\c\build-mingw64\xiaoge_demo_mic_win.exe --api-key $API_KEY --seconds 8 --reply-wait 15 --silence-ms 2000 --no-playback
```

`chcp 65001` 用于确保 PowerShell 正常显示 `data.stt` / `data.reply` 中的中文。`--no-playback` 只关闭本地扬声器播放，不影响下行 TTS PCM 接收统计；如需播放服务端返回音频，去掉该参数。

可选参数：

- `--seconds N`：采集 N 秒麦克风；`0` 表示直到 Ctrl-C。
- `--reply-wait N`：停止采集并发送静音后继续等待服务端回复 N 秒。
- `--silence-ms N`：停止采集后补发 N 毫秒静音，辅助服务端 VAD 收尾。
- `--no-playback`：不播放下行 PCM，只统计 `received`。
- `--api-key key`：显式设置 create_session HTTP `x-api-key`；默认不发送。

期望 demo 输出至少包含：

```text
session trace_id=<trace_id> session_id=<session_id> ws_url=ws://127.0.0.1:10097/ws/session
ready sample_rate=16000
talking; press Ctrl-C to exit
capture stopped; discarded <n> stale queued mic frames, sent vad=silence, then sending <ms> ms silence and waiting <n> seconds for reply
captured=<bytes> captured_frames=<n> sent=<bytes> received=<bytes> dropped=0 pending=0
```

如果链路完全跑通，还会看到服务端下行 JSON：

```text
{"type":"data.stt", ...}
{"type":"data.reply", ...}
```

一次已验证的成功现象：`data.stt` 和 `data.reply` 正常显示中文，且统计类似：

```text
captured=252800 captured_frames=395 sent=316800 received=143040 dropped=0 pending=0
```

判断：

- `ws_url=ws://127.0.0.1:10097/ws/session`：`local_proxy.py` 已正确改写 `create_session` 返回值。
- `ready sample_rate=16000`：代理、WS、Bearer、`ctrl.hello`、`ctrl.ready` 已通过。
- `captured > 0`：Windows 麦克风采集正常。
- `sent > captured`：麦克风 PCM 加上尾部静音已送入 C SDK。
- `received > 0`：服务端已下发 TTS PCM。
- `dropped=0 pending=0`：本地麦克风队列无丢帧、退出时无积压。

代理窗口期望看到：

```text
HTTP POST /create_session -> 200
WS client connected 127.0.0.1 /ws/session -> wss://60.205.197.165:10097/ws/session
```

### 2.5 C 文件 demo：兼容旧参数

如果宿主应用或外部脚手架已经先完成 `create_session`，也可以继续把返回字段直接传入 demo：

```powershell
.\build-mingw64\xiaoge_demo_file.exe `
  "<ws_url>" `
  "<access_token>" `
  "<trace_id>" `
  "<session_id>" `
  robot-x3-001 `
  ..\in.wav `
  ..\out_c.wav `
  --api-key $API_KEY `
  --ca-cert ..\certs\cloud-ca.pem
```

临时跑 IP 时可把 `--ca-cert ..\certs\cloud-ca.pem` 改为 `--insecure`。

### 2.6 C 验收注意点

- `ws_url` 必须指向 `/ws/session`，旧 `/ws/audio` 应被拒绝。
- `xiaoge_demo_file` 的 create_session HTTP 层仅在显式传入 `--api-key` 或设置 `XIAOGE_CLOUD_API_KEY` 时发送 `x-api-key`。
- C SDK 的 WebSocket 会话鉴权只使用 `Authorization: Bearer <access_token>`。create_session 的可选 HTTP key 不进入 WebSocket 合同。
- `data.cmd` 只走内置 fake executor，返回 `data.cmd_ack` 和 `data.cmd_result`，不会调用真实机器人动作。

## 3. MATLAB / Simulink SDK 验证

目录：

```powershell
cd E:\Project\Project2026\AIAudioCloudPlatform\xiaogeV2\xiaoge-duplex\xiaoge-duplex\clients\matlab
```

MATLAB SDK 通过 Python TCP bridge 连接云端：

```text
MATLAB demo_file.m
  -> tcpclient 5001/5002/5003
  -> bridge/xiaoge_bridge.py
  -> Python XiaogeClient
  -> create_session + WSS /ws/session
```

端口：

| Port | Direction |
| --- | --- |
| 5001 | MATLAB -> bridge PCM |
| 5002 | bridge -> MATLAB TTS PCM |
| 5003 | bridge -> MATLAB JSONL events |

### 3.1 安装 Python bridge 依赖

```powershell
cd E:\Project\Project2026\AIAudioCloudPlatform\xiaogeV2\xiaoge-duplex\xiaoge-duplex\clients\python
python -m pip install -r requirements.txt
```

### 3.2 启动 bridge：临时跑通

```powershell
cd E:\Project\Project2026\AIAudioCloudPlatform\xiaogeV2\xiaoge-duplex\xiaoge-duplex\clients\matlab
python bridge\xiaoge_bridge.py `
  "https://60.205.197.165:10099/create_session" `
  robot-x3-001 `
  '{"key_id":"dev","signature":"mock"}' `
  --up 5001 `
  --down 5002 `
  --events 5003 `
  --api-key $API_KEY `
  --insecure `
  --trace-log ..\trace_matlab_bridge.jsonl
```

期望日志：

```text
bridge ready up=:5001 down=:5002 events=:5003
```

### 3.3 启动 bridge：正式域名证书

```powershell
python bridge\xiaoge_bridge.py `
  "https://60.205.197.165:10099/create_session" `
  robot-x3-001 `
  '{"key_id":"dev","signature":"mock"}' `
  --up 5001 `
  --down 5002 `
  --events 5003 `
  --api-key $API_KEY `
  --trace-log ..\trace_matlab_bridge.jsonl
```

### 3.4 启动 bridge：自签 CA

```powershell
python bridge\xiaoge_bridge.py `
  "https://60.205.197.165:10099/create_session" `
  robot-x3-001 `
  '{"key_id":"dev","signature":"mock"}' `
  --up 5001 `
  --down 5002 `
  --events 5003 `
  --api-key $API_KEY `
  --ca-cert ..\certs\cloud-ca.pem `
  --trace-log ..\trace_matlab_bridge.jsonl
```

### 3.5 MATLAB 文件 demo

保持 bridge 运行，在 MATLAB 中执行：

```matlab
cd('E:\Project\Project2026\AIAudioCloudPlatform\xiaogeV2\xiaoge-duplex\xiaoge-duplex\clients\matlab')
addpath(pwd)
demo_file('127.0.0.1', '..\in.wav', 'out_matlab.wav')
```

期望：

- MATLAB 控制台打印 bridge 透传的 JSONL events。
- 生成 `out_matlab.wav`。
- 如果服务端下发 TTS PCM，`out_matlab.wav` 会包含音频；如果没有下行 PCM，输出可能为空音频。

### 3.6 MATLAB Programmatic Client

```matlab
cd('E:\Project\Project2026\AIAudioCloudPlatform\xiaogeV2\xiaoge-duplex\xiaoge-duplex\clients\matlab')
addpath(pwd)

c = xiaoge.Client('127.0.0.1', 5001, 5002, 5003);
c.OnEvent = @(ev) disp(ev.type);
c.sendPcm(int16Frame);
tts = c.readAudio(320);
events = c.readEvents();
c.close();
```

### 3.7 Simulink demo

前提：MATLAB/Simulink R2022b 或兼容版本。

```matlab
cd('E:\Project\Project2026\AIAudioCloudPlatform\xiaogeV2\xiaoge-duplex\xiaoge-duplex\clients\matlab')
addpath(pwd)
build_xiaoge_demo
open_system('xiaoge_demo')
```

运行前仍需先启动 Python bridge。

## 4. 最终验收标准

每个 SDK 至少满足：

1. 不使用旧 `/ws/audio`。
2. `create_session` 返回 `session.created`，字段包含 `trace_id`、`session_id`、`access_token`、`ws_url`。
3. `ws_url` 指向 `/ws/session`。
4. WSS 连接使用 `Authorization: Bearer <access_token>`。
5. 首个上行 JSON 控制帧是 `ctrl.hello`。
6. 收到 `ctrl.ready`，`sample_rate = 16000`。
7. 能发送 16k/mono/int16 PCM。
8. 能处理 `ctrl.clear`，清空本地播放缓存。
9. 能处理 `data.stt`、`data.reply`、`data.error`。
10. 能处理 `data.cmd`；Python/Android SDK 只回调业务侧，C SDK 默认 fake executor 不触发真实机器人动作。

## 5. 常见问题

### 5.1 `CERTIFICATE_VERIFY_FAILED: IP address mismatch`

原因：用 IP 访问 HTTPS/WSS，但证书 SAN 不包含该 IP。

临时处理：

```powershell
--insecure
```

正式处理：

- 使用证书 SAN 中的域名访问。
- 配置 DNS 或 hosts。
- 或重新签发包含目标域名/IP 的证书。

### 5.2 `4401 auth_failed`

排查：

- 如果云侧要求 create_session HTTP key，确认 `--api-key` 或 `XIAOGE_CLOUD_API_KEY` 是否正确。
- WSS upgrade 是否带 `Authorization: Bearer <access_token>`。
- `access_token` 是否来自同一次 `create_session`。
- 是否先发送了 `ctrl.hello`，再发送其他控制帧。

### 5.3 `ready 16000` 出现但 `received=0`

说明协议连接和 PCM 上行已成功，但服务端没有返回 TTS PCM。继续查：

- 输入音频是否是真实语音。
- 服务端 ASR 是否产出 `data.stt final=true`。
- agent 是否生成 `data.reply`。
- TTS 是否启用并向 `/ws/session` 下发二进制 PCM。

### 5.4 MATLAB demo 连不上 bridge

排查：

- bridge 是否已启动。
- 5001/5002/5003 是否被占用或被防火墙拦截。
- MATLAB 当前目录是否在 `clients/matlab`。
- 是否先 `addpath(pwd)`。
