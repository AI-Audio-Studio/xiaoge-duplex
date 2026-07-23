# 云侧对接指南 · 自研协议客户端

> 面向对象:自研**协议客户端**(硬件 / 嵌入式 / APP,直接走 WebSocket,不使用浏览器)。
> 相关文档:架构总览 [ARCHITECTURE.md](ARCHITECTURE.md) · 运行部署 [RUN.md](RUN.md) · 并发网关设计 [../design/concurrency/](../design/concurrency/README.md)
>
> **认证说明**:本文只描述**当前已实现**的准入机制(共享口令)。若依(RuoYi)token 接入是后续独立事项,预留位见 [§8](#8-认证现状与若依接入预留)。
>
> 可以参考web客户端：https://60.205.197.165:10099/

---

## 1. 连接拓扑

客户端只与**网关**打交道,网关对外暴露**单一端口**(默认 `10099`,可 TLS 终结),内部反代到池管理器分配的 agent 进程:

```
自研客户端 ──(wss/ws 单端口)──▶ 网关(gateway) ──反代──▶ poolmgr 分配的 agent 进程(127.0.0.1)
                                    │
                                    └── 池满 → 繁忙(WS 1013) / 无空闲进程 → 1011
```

- agent 进程绑 `127.0.0.1`,**不对外**;客户端**永远不直连** agent 端口。
- 一条会话 = 网关分配的一个 agent 进程;进程一对一服务单个客户端。

---

## 2. 两种对接模式(先选一种)

无 cookie 的协议客户端与带 cookie 的浏览器路径,能力不同。**这是对接前必须理解的核心差异**:

| | 模式 A · 纯音频直连 | 模式 B · 完整会话(仿浏览器) |
| --- | --- | --- |
| 建连方式 | 直接 `GET /ws/audio`(**不带 cookie**) | 先 `GET /` 拿亲和 cookie,再带 cookie 连各通道 |
| 音频上下行 `/ws/audio` | ✅ | ✅ |
| 状态通道 `/ws`(转写 / 状态) | ❌ 无 cookie 一律 4001 拒绝 | ✅ |
| 控制接口 `/api/mic`(静音等) | ❌ 无 cookie 一律 409 | ✅ |
| 准入口令(`XG_ACCESS_CODE`) | **当前不校验**(见 §8) | 在 `GET /` 前置校验 |
| 断线重连 | 无宽限窗:断开即结束会话,重连=**全新会话**(重放欢迎语,可能换到别的 agent 进程) | 有宽限窗(默认 12s):同 cookie 重连**接回同一进程**,音频帧续接、agent 无感 |
| 适用场景 | 纯语音对讲、无需实时转写 UI、最简实现 | 需要转写字幕 / 静音控制 / 抗抖动重连 |

> 决策建议:若客户端只做「说话—听回复」的语音对讲,选**模式 A**,实现最省。若需要实时转写、静音开关、或弱网下的会话保持,选**模式 B**。

---

## 3. 云侧前置条件(部署方需确认)

对接方需要云侧**确认以下配置已就绪**:

**网关(环境变量前缀 `XG_`)**

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `XG_LISTEN_HOST` | `60.205.197.165` | 对外监听地址 |
| `XG_LISTEN_PORT` | `10099` | 对外端口 |
| `XG_SSL_CERT` / `XG_SSL_KEY` | 空 | **两者都设** → 启用 TLS,客户端用 `wss://` / `https://`;否则明文 `ws://` |
| `XG_ACCESS_CODE` | 空 | 共享准入口令。空=不启用准入(见 §8) |
| `XG_HMAC_SECRET` | 进程内随机 | 亲和 / 准入 cookie 的签名密钥;**留空则重启失效**(所有会话回初始态) |
| `XG_MSG_RATE` | `200` | 单连接消息速率上限(条/秒),超限断开该连接 |
| `XG_MAX_FRAME_BYTES` | `32768` | 单帧字节上限,超限断开该连接 |
| `XG_GRACE_SECONDS` | `12.0` | 模式 B 断线宽限窗 |

**agent(音频通道开关)**

- 云侧 agent 必须以 `WEB_AUDIO=1` 启动,`/ws/audio` 才存在(池管理器在服务器形态下注入)。否则协议客户端无法建立音频通道。

---

## 4. 音频参数(上下行一致)

| 项 | 值 |
| --- | --- |
| 采样率 | **16000 Hz** |
| 位深 / 编码 | PCM **有符号 16 位小端**(Int16 LE) |
| 声道 | 单声道(mono) |
| 传输 | WebSocket **二进制帧**,裸 PCM,无容器 / 无头 |
| 建议分片 | 20~40ms/帧(320~640 采样 = 640~1280 字节);内部按 10ms(160 采样)重切,客户端分片大小可不同 |
| 帧上限 | ≤ `XG_MAX_FRAME_BYTES`(默认 32768 字节);速率 ≤ `XG_MSG_RATE`(默认 200 条/秒) |

> 注意:**不要发过小过密的帧**(如每 1ms 一帧),会触发速率上限被断开。20~40ms 分片既满足实时性又远低于限流阈值。

---

## 5. 音频通道 `/ws/audio` 协议

同一条 WebSocket 上,**二进制帧 = 音频 PCM,文本帧 = JSON 控制消息**,客户端必须按帧类型区分。

### 5.1 上行(客户端 → 云侧,麦克风)
- 发送**二进制帧**:16kHz / 单声道 / Int16LE 裸 PCM。
- 静音期可不发(模式 A 无 `/api/mic`;不发即等价静音)。

### 5.2 下行(云侧 → 客户端)

| 帧类型 | 内容 | 客户端处理 |
| --- | --- | --- |
| 二进制 | TTS 回复音频(16kHz / mono / Int16LE) | 顺序播放 |
| 文本 `{"type":"ready","sample_rate":16000}` | 建连后立即下发,确认 agent 就绪 | 可用于确认采样率 |
| 文本 `{"type":"clear"}` | **打断信号**(用户插话,barge-in):清空未播放缓冲 | 立即停止并丢弃已缓冲的待播音频 |
| 文本 `{"type":"busy","message":"..."}` | 服务器繁忙,随后关闭连接 | 提示并按 §7 退避重连 |

> 连接建立后云侧会**主动播报欢迎语**(下行音频随即到达),客户端连上后应立即准备好播放。

---

## 6. 状态通道 `/ws`(仅模式 B)

纯文本 JSON,**云侧 → 客户端**单向推送(客户端发的消息被忽略,仅保活)。连上先收一条 `state` 快照。

| 消息 | 字段 | 含义 |
| --- | --- | --- |
| `state` | `muted`,`stt_backend`,`tts_backend`,`audio_mode`,`agent_state?` | 状态快照 / 增量;仅出现的字段更新 |
| `user_speaking` | `state:"start"` | 检测到用户开口(实时转写起始) |
| `user_partial` | `text` | 实时转写中间结果(逐字增长) |
| `message` | `role:"user"\|"assistant"`,`text`,`ts` | 一句话定稿(用户或助手) |
| `listening` | `on:bool`,`hint` | 聆听模式开关提示 |
| `clear` | — | 打断:清屏 / 停播 |
| `busy` | `message` | 繁忙,随后关闭 |

> 模式 A 客户端拿不到本通道(无 cookie → 4001)。若需实时转写字幕,必须用模式 B 。

---

## 7. 关闭码与重连策略

| WS 关闭码 | 触发 | 客户端应对 |
| --- | --- | --- |
| `4001` affinity-lost | cookie 失效 / agent 进程已亡(模式 B);或无 cookie 连 `/ws`·`/ws/audio` 被拒 | 模式 B:从 `GET /` 重新走一遍(重分配);模式 A:视为会话结束,重连 `/ws/audio` |
| `4002` another-window | 同 cookie 已有活跃音频(双开,模式 B) | 关闭其一;单客户端不应触发 |
| `1013` TRY_AGAIN_LATER | 池满 / 座位已满 | **退避重连**(建议 3~5s 起,指数退避) |
| `1011` | 上游 agent 进程不可达 | 短暂退避后重连 |
| `1000`/`1006` | 正常 / 异常断开 | 按需重连 |

**重连要点**
- 模式 A 无宽限窗:每次重连都是新会话,会重放欢迎语。
- 模式 B 宽限窗内(默认 12s)带**同一 cookie** 重连 `/ws/audio`,可接回原进程、帧续接。
- WS 心跳:服务端 30s ping,标准 WS 库自动回 pong;自研实现需保证响应 ping,否则被判定掉线。

---

## 8. 认证:现状与若依接入预留

### 8.1 当前已实现(共享口令,D-18)
- 机制:`XG_ACCESS_CODE` 非空时启用。浏览器在 `GET /` 入口页被拦,`POST /access` 提交口令 → 校验通过下发 HMAC 签名的准入 cookie `xg_access` → 之后放行。
- **作用范围仅限模式 B 的 `GET /` 入口**。

### 8.2 重要:协议客户端当前无准入门
- 模式 A 直连 `GET /ws/audio`(无 cookie)的路径**不经过口令校验** —— 当前任何能访问网关端口的客户端都可直接分配会话。
- 若云侧对协议客户端有准入要求,**当前需靠网络层管控**(内网 / 防火墙 / mTLS / 反向代理鉴权),网关应用层暂无 token 校验。

### 8.3 若依(RuoYi)token 接入(后续事项,尚未实现)
目标形态:客户端在建连时携带若依登录 token(如 `Authorization` 头或 `?token=` 查询参数),网关在 `/ws/audio` 分配前校验:
- 校验方式二选一:① 直连若依 Redis 查 `login_tokens:{uuid}`(需解 JWT 取 uuid);② 调若依接口 `/system/user/getInfo` 验证。
- 校验失败 → WS 拒绝(建议 `4401` 自定义码或 `1008`)。
- 落点:网关 `ws_audio` 处理器分配前(即本文档 §5 建连的最前置);模式 B 则在 `GET /` 准入处并入。

> 该项落地后本文档 §8 将更新为正式协议。在此之前,协议客户端的准入以 §8.2 的网络层管控为准。

---

## 9. 最小客户端伪代码(模式 A · Python)

```python
import asyncio, json, aiohttp

GATEWAY = "wss://60.205.197.165:10099/ws/audio"   # TLS 时用 wss://
SR = 16000  # 16kHz / mono / Int16LE

async def run():
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(GATEWAY, heartbeat=30) as ws:
            recv = asyncio.create_task(receiver(ws))
            # 上行:每 20~40ms 推一帧麦克风 PCM(此处示意)
            async for pcm_frame in mic_stream():        # bytes, Int16LE 16k mono
                await ws.send_bytes(pcm_frame)
            await recv

async def receiver(ws):
    async for msg in ws:
        if msg.type == aiohttp.WSMsgType.BINARY:
            play(msg.data)                              # 顺序播放 TTS PCM
        elif msg.type == aiohttp.WSMsgType.TEXT:
            m = json.loads(msg.data)
            if m["type"] == "ready":   pass             # 已就绪
            elif m["type"] == "clear": stop_playback()  # 打断:清空待播缓冲
            elif m["type"] == "busy":  handle_busy(m.get("message"))
        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
            break   # 依关闭码按 §7 决定是否重连
```

**对接自检清单**
- [ ] 采样率严格 16000、Int16LE、单声道,上下行一致
- [ ] 二进制=音频 / 文本=JSON 控制,分开处理
- [ ] 收到 `clear` 立即停播并清缓冲(打断体验)
- [ ] 分片 20~40ms,不触发 200 条/秒 与 32KB/帧 限制
- [ ] 按关闭码区分重连策略(`1013` 退避、`4001` 重建会话)
- [ ] 响应服务端 WS ping(保活)
- [ ] 准入:确认走网络层管控还是等 §8.3 token 接入
```
