# 小歌 MATLAB / Simulink 客户端(R2022b)

对接小歌 `/ws/audio`(服务端需 `WEB_AUDIO=1`)。协议见 [../PROTOCOL.md](../PROTOCOL.md)。
音频格式:16000 Hz、单声道、16-bit。

> ⚠️ **测试状态**:交付环境无 MATLAB,本目录 MATLAB/Java 代码**未运行验证**,按下方步骤在你的 R2022b 上验收;有问题反馈即修。其中 **B 方案的 TCP 桥(`bridge/xiaoge_bridge.py`)已自测通过**。

## 两条路径

| | B 方案(推荐,无 Java) | A 方案(Java-WebSocket 直连) |
|---|---|---|
| 传输 | MATLAB 原生 `tcpclient` ↔ `xiaoge_bridge.py` ↔ 小歌 | MATLAB 内嵌 JVM 直连 WS |
| 依赖 | 主机跑一个 Python 桥 | 编译 Java 适配器 + 下载 jar(见 `lib/`) |
| Simulink 块 | `XiaogeAudioBlock`(MATLAB System) | 用 `+xiaoge/Client.m`(脚本,非块) |
| 状态 | 桥已自测;MATLAB 侧标准 `tcpclient` | 需编译适配器,**未测试** |

**为什么推荐 B**:MATLAB 不能子类化 Java 抽象类 `WebSocketClient`,A 必须借助一个编译好的 Java 适配器(`java/XiaogeWsAdapter.java`),环节多。B 把 WS 收敛到经过测试的 Python 桥,MATLAB 只用原生 TCP。

## B 方案:快速开始
```bash
# 1) 主机起桥(连到小歌);小歌为 wss(HTTPS)时加 --tls,自签再加 --insecure
python clients/matlab/bridge/xiaoge_bridge.py <小歌host> <小歌port> --up 5001 --down 5002
#   例:python .../xiaoge_bridge.py 60.205.197.165 10099 --up 5001 --down 5002 --tls --insecure
```
> **B 链路已对真机验证**:桥连 `wss://60.205.197.165:10099`,经 TCP 上/下行跑通真实语音→TTS(~6.5s)。MATLAB 侧为标准 `tcpclient`,待你在 R2022b 上运行 `demo_file.m` / `build_xiaoge_demo` 确认。
```matlab
% 2a) 无声卡文件验证
addpath(pwd)
demo_file('127.0.0.1', 'in.wav', 'out.wav')   % 连本地桥;小歌地址在上面桥命令里。in.wav 须 16k/单声道/16-bit

% 2b) Simulink 实时(需 Audio Toolbox)
addpath(pwd)
build_xiaoge_demo            % 生成 xiaoge_demo.slx:Mic → XiaogeAudioBlock → Speaker
open_system('xiaoge_demo')   % 运行;BridgeHost/UpPort/DownPort 在块参数里
```

## A 方案:Java-WebSocket
1. 按 [lib/README.md](lib/README.md) 下载 Java-WebSocket(+SLF4J)jar、编译 `XiaogeWsAdapter.jar` 放入 `lib/`。
2. 用 `+xiaoge/Client.m`:
```matlab
c = xiaoge.Client('60.205.197.165', 10099, true);   % 当前部署(wss;自签需在适配器配置信任)
c.OnAudio = @(pcm) sound(double(pcm)/32768, 16000);  % 播放
c.OnClear = @() disp('barge-in');
c.connect();
c.sendPcm(myInt16Frame);     % 上行
```
