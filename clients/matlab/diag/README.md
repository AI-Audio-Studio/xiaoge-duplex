# MATLAB 下行收不到 · 分层诊断(B 方案)

现象:连接/上行/服务端回复都正常,但 MATLAB 收不到下行。用下面两步把问题切到具体某一层。
把**每一步的完整命令行输出**(桥 + MATLAB)以及生成的 `bridge_recv.wav` / `diag_reply.wav` 发回。

前置:`pip install websockets`;两个脚本在 `clients/matlab/diag/`。

---

## 步骤 1 —— 单测「桥 → MATLAB」这段 TCP(不牵扯小歌,最快)

桥(自测模式,只对下行持续发 440Hz 正弦音):
```bash
cd clients/matlab/diag
python bridge_debug.py --selftest --up 5001 --down 5002
```
MATLAB(桥在本机就用 127.0.0.1;在别的主机填其 IP):
```matlab
cd clients/matlab/diag
diag_downlink('127.0.0.1')
```
**判读**
- MATLAB `down_total` 持续增长、生成 `diag_reply.wav`(能听到蜂鸣)→ **MATLAB↔桥 TCP 正常**,问题在上游(小歌↔桥或时序),进入步骤 2。
- MATLAB `down_total=0`,但桥日志 `down_written` 在涨 → **就是 MATLAB↔桥 这段**(常见:桥在另一台机、5002 被防火墙拦;或 `bridgeHost` 填错;或安全软件拦截)。
- 桥日志显示下行**没连上**(`down_connected=False`)→ MATLAB 没连到 5002(端口/主机/防火墙)。

## 步骤 2 —— 全链路(桥连真机小歌)

桥(真机;小歌是 wss 自签):
```bash
python bridge_debug.py 60.205.197.165 10099 --tls --insecure --up 5001 --down 5002
```
MATLAB(发一段 16k/单声道/16bit 语音;没有就用默认静音,靠欢迎语/回声也行):
```matlab
diag_downlink('127.0.0.1','speech16k.wav',25)
```
结束后 Ctrl-C 停桥(会把桥收到的小歌音频存成 `bridge_recv.wav`)。

**判读**(看桥每秒的状态行)
| 桥 `audio_recv` | 桥 `down_written` | MATLAB `down_total` | 结论 |
|---|---|---|---|
| >0 | >0 | >0 | 全链路通(那原 demo 的问题可能是时序/参数,继续给我原 demo 日志) |
| >0 | >0 | **0** | 断在 **MATLAB↔桥(5002)**——同步骤1的网络/防火墙排查 |
| >0 | 0(dropped>0) | 0 | 下行**没连上**时音频就到了(时序):确保 MATLAB 先连 5002 |
| **0** | 0 | 0 | 桥**没从小歌收到**音频(小歌↔桥):把桥完整日志发我 |

---

## 要发回给我的
1. 桥的完整输出(步骤1、步骤2 各一份,含每秒状态行)。
2. MATLAB 的完整输出(含 `MATLAB 版本 | computer` 那几行)。
3. `bridge_recv.wav`、`diag_reply.wav`(有就发)。

> 说明:`diag_downlink.m` 特意**先连下行再连上行**,排除「音频早到、下行未连而被丢」的干扰;原 `demo_file.m` 是先上后下,若步骤2全绿而原 demo 不行,多半就是这个时序差异,我再改 demo。
