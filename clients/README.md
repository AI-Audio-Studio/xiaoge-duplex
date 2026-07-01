# 小歌客户端 SDK

把第三方设备/程序接入小歌全双工语音引擎。三个端共用一套协议:[PROTOCOL.md](PROTOCOL.md)。

## 前置
服务端以 `WEB_AUDIO=1` 启动,暴露 `/ws/audio`。
**当前部署**:`wss://60.205.197.165:10099/ws/audio`(HTTPS,自签证书;连接用 `--tls --insecure`)。

## 三个客户端
| 目录 | 适用 | 依赖 | 自测状态 |
|---|---|---|---|
| [python/](python/) | Python 应用、快速验证 | `websockets`(+`sounddevice` 仅实时 demo) | ✅ 本机自测通过(SDK + 文件 demo + 冒烟) |
| [c/](c/) | 嵌入式/原生程序 | libwebsockets ≥4.0 | ⚠️ 代码完整,需在你方工具链 build+验证 |
| [matlab/](matlab/) | MATLAB / Simulink(R2022b) | B:Python 桥;A:Java-WebSocket jar | ⚠️ B 的桥已自测;MATLAB 侧待你方运行验证 |

## 共同音频格式
16000 Hz、单声道、16-bit 有符号小端、裸 PCM。详见 [PROTOCOL.md](PROTOCOL.md)。

## 各端快速入口
- Python:`cd python && pip install -r requirements.txt && python selftest.py`
- C:见 [c/README.md](c/README.md)(cmake + libwebsockets)
- MATLAB/Simulink:见 [matlab/README.md](matlab/README.md)(**推荐 B 方案 TCP 桥**)

> 自测边界(诚实说明):打包环境无 C 工具链、无 MATLAB,故 C 与 MATLAB 为"代码完整 + 验证步骤",由你方在对应工具链上验收;Python 全链路与 MATLAB 的 TCP 桥已在本机自测通过。
