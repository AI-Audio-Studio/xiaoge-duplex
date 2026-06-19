# 小歌 Xiaoge Duplex Speech — 代码导读

> 全双工中文语音交互引擎（基于 LiveKit）的源码导读。

> 适用版本：本仓库当前代码。涉及文件：
> - 应用层（我们写的）：`examples/voice_agents/qwen_funasr_bailian_voice_agent.py`、`custom_audio_providers.py`、`kws_interrupt.py`
> - 框架层（LiveKit Agents）：`livekit-agents/livekit/agents/voice/agent_session.py`、`agent_activity.py`、`audio_recognition.py`、`stt/stream_adapter.py`、`cli/cli.py`
>
> 文中所有 `文件:行号` 均已对照源码核实。行号会随代码演进漂移，漂了就按旁边给出的函数名/关键字搜索。

---

## 0.  Python 速查表

读这套代码前，先把下面几个 Python 概念映射到你熟悉的 Java 概念，后文不再解释：

| Python 写法 | 等价的 Java 概念 | 说明 |
|---|---|---|
| `async def foo(): ... await bar()` | 协程。类似 Netty EventLoop 上的回调链 / `CompletableFuture.thenCompose` | **单线程事件循环**：所有 `async` 函数在同一个线程上协作式调度，`await` 是让出执行权的点。没有锁竞争，但一个协程里跑阻塞代码会卡死整个循环 |
| `asyncio.create_task(coro())` | `executor.submit(runnable)` | 把协程丢给事件循环并发跑，不等结果（fire-and-forget 时用） |
| `asyncio.to_thread(blocking_fn)` | `CompletableFuture.supplyAsync(fn, threadPool)` | 把**阻塞**调用（如同步网络库）丢到线程池，避免卡事件循环。TTS 的 DashScope SDK 是同步的，所以到处都是它 |
| `@decorator` | 注解 + 动态代理 | `@server.rtc_session()` ≈ Spring 的 `@RequestMapping`：把函数注册到框架 |
| `@session.on("event_name")` | `addEventListener` | 事件监听器注册，回调是普通同步函数 |
| `raise StopResponse()` | 抛一个受检异常做控制流 | 框架在外层 catch 它，含义是"这轮不要生成回复" |
| `with lock: ...` / `async with ...` | try-with-resources / synchronized 块 | 自动获取/释放资源 |
| `def __anext__(self)` | `Iterator.next()` 的异步版 | `async for x in stream` 会反复调它 |
| `@property def model(self)` | getter | `obj.model` 实际调方法 |
| 没有 interface，靠基类 + `capabilities` 对象 | 接口 + 能力探测 | STT/TTS 都继承抽象基类，用 `STTCapabilities(streaming=..., interim_results=...)` 声明自己支持什么，**框架据此切换行为分支**（这点极其重要，见 §4.2） |
| `queue.Queue` + `threading.Thread` | `BlockingQueue` + `Thread` | 真线程。KWS 解码、TTS 回调都跑在独立真线程上，与事件循环之间靠队列+`call_soon_threadsafe`（≈ `SwingUtilities.invokeLater`）通信 |

**一句话心智模型**：主流程全在一个事件循环线程上（像 Node.js / Netty），凡是阻塞的第三方 SDK 都被 `to_thread` 推到线程池，凡是要从别的线程往主流程报事件的都走 `call_soon_threadsafe`。

---

## 1. 总体架构

### 1.1 架构图

```
┌──────────────────────────── console 进程（单事件循环 + 若干工作线程）────────────────────────────┐
│                                                                                                │
│  麦克风 ─► sounddevice ─► AudioProcessingModule(AEC+NS) ─► ConsoleAudioInput                    │
│  扬声器 ◄─ 播放缓冲 ◄──────────┐        ▲ 播放参考信号(process_reverse_stream)                    │
│                               │        │  [cli.py:325 WebRTC APM，回声消除]                     │
│                        ┌──────┴────────┴──┐                                                     │
│                        │   AgentSession    │  ≈ 一次会话的 Spring Context                        │
│                        └──────┬───────────┘                                                     │
│                               │ session.input.audio（音频帧流的唯一入口）                         │
│                    ┌──────────▼───────────┐                                                     │
│                    │  KwsTapAudioInput     │ 装饰器模式：原帧透传 + 旁路拷贝                       │
│                    └──────────┬───────────┘──► NativeKwsSpotter（sherpa-onnx，独立线程）         │
│                               │                   命中"停/别说了" ─► interrupt(force=True) ──┐   │
│              ┌────────────────┼────────────────────┐                                       │   │
│              ▼                ▼                    ▼                                       │   │
│      ┌──────────────┐ ┌────────────────┐ ┌───────────────────┐                             │   │
│      │ Silero VAD   │ │ StreamAdapter   │ │ MultilingualModel │ EOU 判停模型（本地 ONNX）     │   │
│      │ (说话起止    │ │  内含同一 VAD    │ │ 输入=转写文本      │                             │   │
│      │  时间源)     │ │  分段→触发STT    │ │ 输出=说完的概率    │                             │   │
│      └──────┬───────┘ └───────┬────────┘ └─────────▲─────────┘                             │   │
│             │                 │ 整段音频            │ final 文本                            │   │
│             │                 ▼                    │                                       │   │
│             │         FunASROfflineSTT ────────────┤                                       │   │
│             │         (持久 WS 复用)                │                                       │   │
│             │                                      │                                       │   │
│             └────────────► AgentActivity / AudioRecognition ◄──────────────────────────────┘   │
│                            （判停状态机 + 打断闸门 + 轮次提交）                                   │
│                                      │ 用户轮落定                                              │
│                                      ▼                                                         │
│                         Agent.on_user_turn_completed（拒识：停止词/背调词/数字归一）              │
│                                      │ 放行                                                    │
│                                      ▼                                                         │
│                         LLM 流式生成 ──token──► QwenStreamingTTS ──PCM──► 播放                  │
│                                                 (每轮一连接+预热池)                              │
└────────────────────────────────────────────────────────────────────────────────────────────────┘
              │                          │                          │
      外部服务 ▼                          ▼                          ▼
   FunASR WS 服务器               Qwen3-4B (vLLM 自建)        DashScope qwen-tts-realtime
   wss://60.205.197.165:10090     https://...:10092/llm/v1    (阿里云百炼，WebSocket)
```

### 1.2 组件解释（类比 Java 生态）

| 组件 | 角色 | Java 类比 |
|---|---|---|
| **AgentServer / Worker** | 进程级调度器，接任务、起会话 | Tomcat / 一个接收任务的容器 |
| **JobContext** | 一次任务的上下文（房间、进程数据） | `ServletContext` + request 上下文 |
| **AgentSession** | 一次语音会话的总容器，挂载 stt/vad/tts/llm/判停配置，管理生命周期和事件总线 | Spring `ApplicationContext` + 事件总线 |
| **Agent (VoiceAgent)** | 业务逻辑：人设 prompt、工具、轮次钩子 | 你写的 `@Service`，框架回调它的钩子方法 |
| **AgentActivity** | session 内部的"当前活动"状态机：打断决策、轮次提交、投机生成 | 一个复杂的状态机 Service（框架内部类，不直接 new） |
| **AudioRecognition** | 判停管线：聚合 VAD 事件 + STT 转写 + EOU 推理，决定"用户这轮何时结束" | 事件聚合器 / CEP 引擎 |
| **Silero VAD** | 本地神经网络，逐帧判断"有人在说话吗" | 一个本地推理库 |
| **StreamAdapter** | 适配器：把"非流式 STT"包装成框架要的"流式 STT"接口，用 VAD 切段 | 经典 Adapter 模式 |
| **FunASROfflineSTT** | 自定义 STT 实现，整段音频→远端 FunASR→文本 | 实现 SPI 接口的自定义 Provider |
| **MultilingualModel** | EOU（End of Utterance）判停模型：读转写文本，输出"说完了"的概率 | 本地 ONNX 推理封装 |
| **QwenStreamingTTS** | 自定义 TTS 实现，文本→DashScope→PCM 音频 | 同上，另一个 SPI 实现 |
| **NativeKwsSpotter / KwsTapAudioInput** | 关键词检出（"停"），音频旁路 + 装饰器 | 装饰器模式 + 独立工作线程 |

**三个本地模型**（不走网络）：Silero VAD、MultilingualModel（判停）、sherpa-onnx KWS。
**三个远程服务**：FunASR（STT）、Qwen LLM、DashScope TTS——全链路延迟大头都在这三个的网络与冷启动上，所以代码里有大量"持久连接/预热/投机"手段（§6）。

---

## 2. 文件与类地图

```
examples/voice_agents/
├── qwen_funasr_bailian_voice_agent.py   ← 主文件：装配 + 业务钩子 + 埋点（≈ Application + Controller）
│   ├── VoiceAgent(Agent)                  业务 Agent：人设、on_enter、on_user_turn_completed
│   ├── entrypoint(ctx)                    :254  会话装配（≈ @Configuration），必读
│   ├── prewarm(proc)                      :197  进程预热：加载 VAD
│   ├── _STOP_WORDS / _STOP_REPLY_PATTERNS :51   停止词表 + 正则
│   ├── _BACKCHANNEL_RE                    :69   背调词（语气词）正则
│   └── 5 个 @session.on(...) 事件钩子      :303-381  指标埋点 + 停止词早期打断 + TTS 预热触发
│
├── custom_audio_providers.py            ← 自定义 STT/TTS Provider（≈ SPI 实现包）
│   ├── FunASROfflineSTT(stt.STT)          :51   离线 STT，持久 WS（当前在用）
│   │   ├── capabilities 声明              :61-67  streaming=False ← 决定框架走哪套打断逻辑！
│   │   ├── _ensure_ws / _reset_ws         :117-138 持久连接管理
│   │   ├── _recognize_once                :140  单段识别协议（init→上传→is_speaking:false→等is_final）
│   │   └── _recognize_impl                :192  入口：锁串行 + 失败重连重试一次
│   ├── FunASRStreamingSTT(stt.STT)        :234  流式 2pass 版（当前未用，留作 A/B 对照）
│   ├── QwenStreamingTTS(tts.TTS)          :589  流式 TTS，每轮一连接 + size=1 预热池
│   │   ├── _build_connection              :630  connect+update_session（~1s 握手就在这）
│   │   ├── take_connection                :644  取连接：命中预热零握手，否则现建
│   │   └── prewarm_connection             :658  后台预建（线程安全，TTL 防服务端闲置关）
│   ├── _QwenSynthesizeStream              :732  一轮 TTS 的生命周期（必读，见 §7）
│   └── _QwenStreamCallback                :550  DashScope 回调→asyncio 的桥（bind 延迟绑定防串台）
│
├── kws_interrupt.py                     ← 本地关键词强打断（可选，缺依赖自动降级）
│   ├── KwsConfig.from_env                 :73   全部从环境变量读（XIAOGE_KWS_*）
│   ├── NativeKwsSpotter                   :92   解码线程 + 队列 + debounce
│   │   ├── try_create                     :118  依赖/模型齐全才创建，否则返回 None（优雅降级）
│   │   └── _decode                        :199  喂波形→解码→命中判定
│   └── KwsTapAudioInput(io.AudioInput)    :226  装饰器：透传 + 旁路（仅 8 行核心逻辑）
│
livekit-agents/livekit/agents/
├── voice/agent_session.py               ← 会话容器
│   ├── start()                            :576  会话启动序列
│   ├── input.audio 包装先例               :745  框架自己也这么包（录音功能）
│   ├── _forward_audio_task                :1413 唯一音频读取者（只捕获一次 input.audio）
│   └── _on_audio_input_changed            :1624 setter 钩子：_started 后重启读取任务
│
├── voice/agent_activity.py              ← 打断/轮次/投机 状态机（核心中的核心）
│   ├── _interrupt_by_audio_activity       :1605 打断决策中枢（唯一入口，三处触发）
│   │   └── min_words 闸门                 :1627-1636 读 current_transcript 数词，不够直接 return
│   ├── on_vad_inference_done              :1743 触发点①：VAD 说话时长 ≥ min_duration
│   ├── on_interim_transcript              :1792 触发点②：interim 到达（本项目无 interim，死路）
│   ├── on_final_transcript                :1821 触发点③：final 到达（本项目唯一活路）
│   ├── on_preemptive_generation           :1857 投机生成启动
│   ├── _cancel_preemptive_generation      :1242 投机丢弃（搜调用点看哪些情况会翻盘）
│   └── on_user_turn_completed 调用处      :2059-2075 钩子调用 + catch StopResponse + 计时
│
├── voice/audio_recognition.py           ← 判停管线
│   ├── _run_eou_detection                 :1124 判停入口
│   ├── _bounce_eou_task                   :1138-1219 EOU 概率 vs 阈值 → min/max delay（精读！）
│   │   └── extra_sleep 算式               :1213-1219 等待时间扣除已流逝静音
│   ├── transcription_delay 定义           :1231-1243 指标口径（≠ STT 推理耗时）
│   └── current_transcript property        :827  min_words 闸门的数据源
│
├── stt/stream_adapter.py                ← 非流式→流式适配器
│   └── StreamAdapterWrapper._run          :97-143 VAD 事件驱动分段，END_OF_SPEECH 时整段交 STT
│
└── cli/cli.py                           ← console 模式
    └── AudioProcessingModule              :325  WebRTC AEC+NS（KWS 防自触发的保障）
```

---

## 3. 端到端主线：一轮对话的完整时序

**这是全文最重要的一节。** 跟着下面的编号在源码里走一遍，主线就通了。

```
T0  用户开口
 │   Silero VAD 检出语音 → AgentActivity.on_start_of_speech (agent_activity.py:1682)
 │   ├─ session 状态机：user_state → "speaking"
 │   └─ 应用层钩子 user_state_changed (主文件:345-353)：
 │        后台线程预热 TTS 连接（prewarm_connection），赌 1~2s 后要用
 │
 │   同时：每一帧音频都流经 KwsTapAudioInput.__anext__ (kws_interrupt.py:237)
 │        → 旁路进 KWS 解码线程。若此刻 AI 在说话且用户喊"停"，
 │        ~360ms 后命中 → interrupt(force=True)，不等下面任何步骤。
 │
T1  用户停口
 │   VAD 持续观察静音。这期间什么都不会发生——离线 STT 还没被调用。
 │
T1+0.35s  VAD 静音确认（min_silence_duration=0.35，主文件:200）
 │   StreamAdapterWrapper._run (stream_adapter.py:97-143)：
 │   收到 VAD END_OF_SPEECH → merge_frames 把整段音频合并 (:122)
 │   → 调 FunASROfflineSTT._recognize_impl (custom_audio_providers.py:192)
 │
T1+0.35s ~ +0.6s  FunASR 识别（~200ms 推理 + 持久 WS 几乎零连接开销）
 │   _recognize_once (custom_audio_providers.py:140)：
 │   init payload → 全速分片上传 → {"is_speaking": false} → 等 is_final → 文本
 │
T2  final 转写到手 —— 一齐发生四件事：
 │   ① 框架发 user_input_transcribed 事件
 │      → 应用层钩子 (主文件:365-376)：记 STT_FINAL 日志；
 │        若命中停止词正则 → session.interrupt(force=True)【停止词早期打断】
 │   ② AgentActivity.on_final_transcript (agent_activity.py:1821)
 │      → 调 _interrupt_by_audio_activity (:1605)【普通打断闸门，见 §5】
 │   ③ AudioRecognition._run_eou_detection (audio_recognition.py:1124)
 │      → EOU 模型推理 + endpointing 等待【判停，见 §4】
 │   ④ 投机生成 on_preemptive_generation (agent_activity.py:1857)
 │      → 不等判停落定，LLM 先跑；preemptive_tts=True 时 TTS 也先合成
 │
T2+~0s（快路径）或 T2+0~0.25s（慢路径）  判停落定，用户轮提交
 │   ★ min_delay=0.3 / max_delay=0.6（主文件:294）的锚点是 T1（最后一帧语音），
 │     不是 T2。框架算式 (audio_recognition.py:1213-1219)：
 │       extra_sleep = (0.3 或 0.6) + last_speaking_time − now
 │     到 T2 时已流逝 ~0.55s（VAD 0.35 + STT 0.2），所以：
 │       快路径 0.3−0.55<0 → 零等待，final 一到立即提交；
 │       慢路径 0.6−0.55 → 只再等 ~0.05s（STT 快时最多 ~0.25s）。
 │   AgentActivity 调 Agent.on_user_turn_completed (调用处 agent_activity.py:2059)
 │   → 我们的实现 (主文件:170-191)【拒识，见 §5.3】：
 │      停止词 → 兜底 interrupt + raise StopResponse（不回复）
 │      背调词("嗯。") → raise StopResponse（不回复，已播语音不动）
 │      纯数字 → 重写成 "1、2、3" 再交给 LLM
 │      其他 → 放行
 │
T3  LLM 流式生成（若投机命中，此刻已经在跑甚至跑完了一半）
 │   build_llm (主文件:108-141)：OpenAI 兼容客户端，keepalive 连接池，
 │   enable_thinking=False。token 流式吐出。
 │
T4  TTS 合成（_QwenSynthesizeStream._run, custom_audio_providers.py:748）
 │   take_connection：命中预热 → 零握手；否则现建 ~1s
 │   LLM token 攒到句末标点（。！？!?；;\n）→ append_text 一句 (:792)
 │   全部文本进完 → commit() 触发收尾 → 服务端持续推 audio.delta
 │   → 回调线程解 base64 → queue → drain 协程 → output_emitter.push → 播放
 │
T5  AI 出声。用户随时可以打断，回到 T0 的 KWS / 打断闸门逻辑。
```

**延迟账单**（对应上面的 T 点，实测数据）：

| 段 | 耗时 | 谁决定的 |
|---|---|---|
| T1→T1+0.35s VAD 静音确认 | 350ms | `min_silence_duration=0.35`（主文件:200） |
| T1+0.35→T2 STT | ~200ms 推理（持久 WS 省掉 ~190ms 连接） | FunASR 服务端，地板 |
| T2→轮次落定 判停等待 | 快路径 ~0.3s / 慢路径 ≤0.6s | `endpointing` 配置（主文件:294） |
| LLM 首 token | 稳态 ~150-250ms（冷启动 3.6s+，已用 warmup 压掉） | 远端 vLLM |
| TTS 首包 | 预热命中≈首句合成时间；未命中 ~1s 握手 | 预热池 + 句切分 |
| 投机生成 | 把判停等待和 LLM/TTS 启动**并行**掉 | `preemptive_generation`（主文件:299） |

---

## 4. 专题一：判停（什么时候算用户说完了）

### 4.1 两级判停：VAD 物理静音 + EOU 语义判断

判停不是一个开关，是两级串联：

1. **物理级（VAD）**：静音满 `min_silence_duration=0.35s` → 才触发 STT、才有后续一切。这 350ms 是所有轮次的固定地板。
2. **语义级（EOU 模型）**：拿到转写文本后，MultilingualModel 结合聊天上下文输出"这句话说完了"的概率，走二元分流。

### 4.2 EOU 二元分流（精读 audio_recognition.py:1138-1219）

```
final 文本 → EOU 模型推理 → 概率 P
   │
   ├─ P ≥ unlikely_threshold（模型按语言内置的阈值，:1160-1165）
   │     → 快路径：等到"距最后一帧语音满 min_delay(0.3s)"就提交轮次
   │
   └─ P < unlikely_threshold —— 模型认为"可能还没说完"
         → 慢路径：观察期延长到"距最后一帧语音满 max_delay(0.6s)"
   两档共用一个算式 (:1213-1219)：
       extra_sleep = (min_delay 或 max_delay) + last_speaking_time - now
   锚点是最后一帧语音（T1），不是 final 到手时刻。到 final 到手时
   已流逝 ~0.55s（VAD hold 0.35 + STT ~0.2），代入：
       快路径 0.3 − 0.55 < 0  → 零等待，final 一到立即提交
       慢路径 0.6 − 0.55      → 实际只再等 0~0.25s
   （min_delay/max_delay 配置在主文件:294 的 "endpointing" 里）
```

**关键认知**：
- 这是**二元**的，没有按概率渐变的中间档。短答（"行/好/对"）模型常给低概率 → 系统性落慢路径，这是短答延迟偏高的结构性原因。
- 观察期内用户真的接着说了 → 轮次不提交，新语音拼进来重新判。所以慢路径不是浪费，是容错。
- `transcription_delay`（:1231-1243）= final 到手时刻 − VAD 最后语音帧。**它不是 STT 推理耗时**——里面混着 VAD hold 0.35s + 连接 + 上传。FunASR 真实推理只 ~200ms。看延迟别冤枉 STT。

### 4.3 判停可调参数

| 参数 | 位置 | 当前值 | 调小 | 调大 |
|---|---|---|---|---|
| `min_silence_duration` | 主文件:200（VAD 加载） | 0.35 | 全轮次提速，但句中小停顿易被误判说完（碎句） | 全轮次变慢 |
| `endpointing.min_delay` | 主文件:294 | 0.3 | 快路径提速，碎句风险升 | 全局变慢 |
| `endpointing.max_delay` | 主文件:294 | 0.6（从 1.5 逐步砍下来） | 慢路径提速；句中 >0.6s 停顿的长句易被切碎，太碎回调 0.8~1.0 | 慢路径变慢 |
| `unlikely_threshold` | `MultilingualModel(unlikely_threshold=...)`（主文件:277 可加参数） | 模型内置 | 更多轮判"说完"走快路径，**高风险**，框架注释明确不推荐 | 更多轮走慢路径 |

---

## 5. 专题二：打断与拒识

### 5.1 先分清两个概念

- **打断（interrupt）**：让正在播的 AI 语音停下。管"嘴"。
- **拒识（StopResponse）**：这轮用户输入不生成回复。管"脑"。
- 停止词 = 打断 + 拒识；背调词（"嗯/哦"）= 只拒识不打断。两者解耦。

### 5.2 打断决策中枢：`_interrupt_by_audio_activity`（agent_activity.py:1605）

所有"普通打断"（非 force）都汇到这一个函数，内部闸门：

```python
# agent_activity.py:1627-1636（示意）
if stt 存在 and min_words > 0:
    text = self._audio_recognition.current_transcript   # 当前轮已转写的文本
    if 词数(text) < min_words:                            # split_words 把句尾"。"也算一个词！
        return                                            # 不打断，直接放弃
# 通过闸门 → 暂停播放 + 启动误打断恢复计时器（false_interruption_timer）
```

三个触发点（都在 agent_activity.py）：

| 触发点 | 行号 | 时机 | 在本项目的命运 |
|---|---|---|---|
| `on_vad_inference_done` | :1743-1758 | 用户连续发声 ≥ `min_duration`(2.0s) | **被闸死**：离线 STT 说话中途 `current_transcript` 为空，0 词 < 3，每次都 return |
| `on_interim_transcript` | :1792-1810 | 实时转写片段到达 | **死路**：FunASR 离线无 interim |
| `on_final_transcript` | :1821-1842 | final 到达 | **唯一活路**。注意这条路**不检查 min_duration**，只过 min_words 闸门 |

**由此推出本项目的真实打断行为**（与直觉相反，务必理解）：

> 用户压着 AI 连说 10 秒，AI **不会**被打断——不是"还没说完"，而是句中没有转写文本、min_words 闸门拦掉了 VAD 触发路径。打断真正发生在用户**停顿 0.35s + STT 出 final** 之后。

两个例外随时能切：**KWS 命中**和**停止词 final**都走 `session.interrupt(force=True)`，force 绕过全部闸门。

### 5.3 拒识：`on_user_turn_completed`（主文件:170-191）

框架在轮次落定后调用这个钩子（调用处 agent_activity.py:2059-2075，框架 catch `StopResponse`）：

```
final 文本
  ├─ 全匹配停止词正则(_STOP_REPLY_PATTERNS, 主文件:61)
  │    → interrupt(force=True) 兜底（通常早期路径已停播，这里是 no-op）
  │    → raise StopResponse  （不回复）
  ├─ 全匹配背调词正则(_BACKCHANNEL_RE, 主文件:70)   # 纯语气字"嗯哦啊…"+标点
  │    → raise StopResponse  （只不回复；不打断，故事继续播）
  ├─ 纯数字串 → 重写为"1、2、3"逐位形式（防 LLM 当整数念）
  └─ 其他 → 正常生成回复
```

**min_words=3 的真实含义**（重要，防止误调）：FunASR 的 final 永远带句尾标点，而框架的 `split_words(split_character=True)` 把"。"算一个独立词 → `嗯。`=2 词、`换个故事。`=5 词。min_words=3 的作用是**拦"单字+句号"**（背调词），放行真指令。`停。`=2 也会被拦，但停止词走 force 不经此闸门。**别调回 2**——那是修过的 bug（"嗯一声 agent 卡一下"）。

### 5.4 三条打断路径赛跑（话音落 → 停播）

| 路径 | 延迟 | 代码入口 | 角色 |
|---|---|---|---|
| ① KWS | ~460ms | kws_interrupt.py `_decode`:199 → 主文件 `_on_kws_hit`:404 | 最快 + **纠错**（FunASR 把"停"听成"零"时只有它能兜）；召回不稳会漏 |
| ② 停止词早期 | ~600-800ms（350 VAD + 200 STT + 调度） | 主文件 `user_input_transcribed` 钩子:365-376 | KWS 漏了的兜底；与普通轮共用 STT 链路，只是在 final 事件就动手 |
| ③ 普通闸门 | 再晚 0.3-0.6s+ | agent_activity.py:1821→1605 | 真插话（用户说长句）走这里 |

三处 `interrupt` 幂等，谁先到谁生效。

### 5.5 打断/拒识可调参数

| 参数 | 位置 | 当前值 | 说明 |
|---|---|---|---|
| `min_words` | 主文件:281 | 3 | 见 §5.3，是"单字+句号"算术防线，别降 |
| `min_duration` | 主文件:284 | 2.0 | **当前实为空转**（VAD 路径被 min_words 闸死）。换流式 STT 后才复活 |
| `backchannel_boundary` | 主文件:285 | (1.8, 3.5) | AI 刚开口 1.8s 内不被切 |
| `_STOP_WORDS` | 主文件:51 | 元组 | 加新停止词/误识兜底词（如"休庭"）改这里 |
| `_BACKCHANNEL_CHARS` | 主文件:69 | "嗯哦噢喔啊呃唉唔诶哼呢" | 背调字符集 |
| KWS `keywords_threshold` | 环境变量 `XIAOGE_KWS_KEYWORDS_THRESHOLD` | 0.18 | 调低（如 0.12）提召回、增误触风险。当前零误触发，有下调空间 |
| KWS 关键词 | 环境变量 `XIAOGE_KWS_KEYWORDS`（`\|` 分隔） | DEFAULT_KEYWORDS（kws_interrupt.py:44） | |
| KWS 开关 | `XIAOGE_KWS_ENABLE_NATIVE=1` + `XIAOGE_KWS_MODEL_DIR` | 默认关 | 缺依赖/模型自动降级，日志见 KWS_ACTIVE/KWS_DISABLED |

---

## 6. 专题三：KWS 旁路（怎么在不动框架的前提下"窃听"音频）

### 6.1 接入原理（装饰器模式 + setter 钩子）

```
session.input.audio  ←  这是框架读音频的唯一入口（一个异步迭代器）
        │
KwsTapAudioInput(原 input, spotter)        ← kws_interrupt.py:226
   async def __anext__(self):
       frame = await super().__anext__()    # 从被包装的原 input 拿帧
       self._spotter.push(frame)            # 旁路拷贝（非阻塞入队）
       return frame                         # 原样透传给框架
```

**为什么必须在 `session.start()` 之后包**（主文件:400-403 注释引用的证据链）：
- `_forward_audio_task`（agent_session.py:1413）启动时**只捕获一次** `input.audio` 引用；
- `input.audio` 是个 setter，赋值触发 `_on_audio_input_changed`（:1624）：`_started=True` 时会 cancel 旧的 forward task 并重启，重新捕获到包装层；`_started=False` 时是 no-op——start 前包了等于白包。
- 框架自己在 :745 用同款模式包录音功能，是官方认可的扩展点。

### 6.2 线程模型

```
事件循环线程                          KWS 解码线程（真线程）
__anext__ → push() 非阻塞入队 ──────► queue.get() → sherpa-onnx 解码
（队列满丢最旧帧，绝不阻塞音频管线）        命中 → loop.call_soon_threadsafe(on_hit)
                                              │
事件循环线程 ◄────────────────────────────────┘
_on_kws_hit → session.interrupt(force=True)
```

### 6.3 防自触发：console 的 AEC

cli.py:325 创建 WebRTC `AudioProcessingModule(echo_cancellation=True, noise_suppression=True)`：扬声器要播的音频先作为"参考信号"喂 `process_reverse_stream`，麦克风帧过 `process_stream` 时把参考信号里的声音减掉 → KWS 拿到的是 post-AEC 帧，AI 自己说"停"不会触发自己。**生产 room 模式没有这层免费保障，需另配 AEC。**

---

## 7. 专题四：TTS 流式合成、预热与打断语义

### 7.1 一轮 TTS 的生命周期（精读 `_QwenSynthesizeStream._run`，custom_audio_providers.py:748-825）

```
take_connection()            ← :757  取连接：预热命中=零握手；否则现建 ~1s
callback.bind(queue, event)  ← :758  把当轮队列绑到连接的回调上（延迟绑定，见 7.3）
循环：LLM token 进来 → 攒到句末标点（。！？!?；;\n）→ append_text 一句   ← :787-795
     （首句的合成与 LLM 后续生成重叠 —— 这就是"流式"的价值）
输入结束 → 残余文本 append → commit() 触发收尾 → 等 response.done       ← :797-807
finish() + close()           ← :821-824  每轮用完即关
─────────────────────────────────────────────────
except BaseException:        ← :811-819  ★打断语义本体★
    框架打断 = cancel 这个协程 = 在这里 close() 连接
    → 服务端确定性中止合成、在途音频全丢
    → 下一轮全新连接，物理上不可能串台
```

音频回程：DashScope 回调线程收 `audio.delta` → base64 解码 → `queue.Queue` → `_drain` 协程（:768）→ `output_emitter.push` → 播放。

### 7.2 为什么"每轮一连接"而不是持久复用

踩过的死结：DashScope realtime **没有可靠中止在途 response 的手段**（`cancel_response()` 实测不生效），持久连接下上一轮被打断的残留音频会顺着连接漏进下一轮（实测出现过"下一轮拿到上一轮整段音频"）。唯一确定性清理是 close 连接。所以定论：**连接每轮独占用完即关，握手成本用预热补**。

### 7.3 预热池（QwenStreamingTTS:622-677）

- size=1 的池：`prewarm_connection`（:658）后台线程预建 `connect+update_session`，挂起待用；`take_connection`（:644）优先取它。
- TTL（`BAILIAN_TTS_WARM_TTL` 默认 20s）：超龄连接可能被服务端闲置关闭，宁可丢弃现建也不用半死连接。
- **延迟绑定防串台**：`_QwenStreamCallback`（:550）构造时不持有任何轮次的队列，`bind()` 在取用时才挂当轮 queue。预热到 bind 之间不 commit，服务端不吐 delta；万一有悬空 delta，回调里 `q is None` 直接丢（:570-571）。
- 触发时机：用户**一开口**就预热（主文件:352-353），到 TTS 真正开跑有 1~2s 提前量，握手刚好藏进用户说话+判停期。

### 7.4 TTS 可调参数

| 参数 | 位置 | 当前值 | 说明 |
|---|---|---|---|
| 句切分标点集 | custom_audio_providers.py:792 `"。！？!?；;\n"` | — | 首段若改成遇"，"也切，首包可再提前 200-500ms，韵律需耳测 |
| `BAILIAN_TTS_WARM_TTL` | 环境变量 | 20s | 预热连接保鲜期 |
| `BAILIAN_TTS_MODEL` / `BAILIAN_TTS_VOICE` | 环境变量 | qwen-tts-realtime / Ethan | |
| `speech_rate` / `sample_rate` | QwenStreamingTTS 构造参数 | 1.0 / 24000 | |
| `preemptive_tts` | 主文件:299 | True | 投机合成：判停翻盘会白烧一次 TTS，背调词轮次是主要浪费源 |

---

## 8. 投机生成（preemptive generation）

- 入口：`on_preemptive_generation`（agent_activity.py:1857-1906）。STT final 一到就启动 LLM（`enabled` 默认 True），`preemptive_tts: True` 让 TTS 也提前合成——把"判停等待"和"LLM ttft + TTS 首包"从串行变并行。
- 翻盘丢弃：`_cancel_preemptive_generation`（:1242），调用点散布在 :830/:999/:1268/:1941/:1971——新语音到达、转写变化、被打断等都会丢弃重做。
- 落定校验：轮次提交后还要核对 `on_user_turn_completed` 有没有改写消息（:2094 起），改了照样丢弃。
- 代价：被 `StopResponse` 拦掉的轮次（背调词）已经白烧了一次 DashScope 合成。误判率低，当前认为可接受。

---

## 9. 指标埋点与日志解读

日志文件：仓库根目录 `qwen_voice_turn_metrics.log`（带毫秒戳）。各标记的产地都在主文件 :303-381 的事件钩子里：

| 标记 | 含义 | 怎么用 |
|---|---|---|
| `STT_FINAL text=...` | FunASR 最终转写（含被拒识的轮次） | 核对误识；与 TURN_ASSISTANT 时间戳相减估打断延迟 |
| `TURN_USER ... transcription_delay=... end_of_turn_delay=...` | 用户轮指标 | transcription_delay 含 VAD hold（§4.2 提醒）；end_of_turn_delay 是判停总等待 |
| `TURN_ASSISTANT ... llm_ttft=... tts_ttfb=... e2e_latency=...` | 助手轮指标 | tts_ttfb 看预热是否命中；e2e_latency 离线 STT 下时有时无 |
| `FELT_LATENCY felt=...` | 用户停口→AI 开口 | **当前埋点是坏的（恒为 -），待修**；先手动比时间戳 |
| `STOP_PHRASE_EARLY` / `STOP_PHRASE` | 停止词早期打断 / 兜底 | 两条都出现说明早期路径生效 |
| `STOP_KWS_EARLY keyword=...` | KWS 命中 | 与 STT_FINAL 比时间戳即 KWS 领先量（实测 ~330ms） |
| `BACKCHANNEL ... skip_reply` | 背调词被拒识 | 出现它而故事没被切 = min_words=3 防线在工作 |
| `FALSE_INTERRUPTION resumed=...` | 误打断及是否恢复 | |
| `KWS_ACTIVE` / `KWS_DISABLED reason=...` | KWS 激活自报告 | 每次启动先看这条确认 KWS 状态 |

---

## 10. 三条精读路线（读完以上后做的"习题"）

**路线 A：「嗯。」的一生（拒识线）**
stream_adapter.py:122 整段 flush → custom_audio_providers.py:140 识别出 `嗯。` → audio_recognition.py:1165 短词低概率落慢路径等 0.6s → agent_activity.py:1842→1635 `嗯。`=2 词 <3 被闸门拦下（不打断）→ :2065 进 on_user_turn_completed → 主文件:180-184 背调正则命中 `StopResponse` → 投机 TTS 被 cancel（白烧一次）。**结果：故事不停、不回话。**

**路线 B：「停」的三场赛跑（打断线）**
kws_interrupt.py:199 KWS ~460ms 命中 → 主文件:404 force 打断（第一名）∥ 主文件:374 STT_FINAL 正则 ~700ms force 打断（第二名，幂等 no-op）∥ 主文件:175-178 turn completed 兜底（第三名）+ StopResponse 拦回复。**三处 interrupt 幂等，职责分层：①②管停得快，③管不接话。**

**路线 C：一次正常问答的延迟账单（性能线）**
主文件:200 VAD 0.35 → custom_audio_providers.py:140 STT ~200ms → audio_recognition.py:1213 extra_sleep 算式 → agent_activity.py:1857 投机并行 → custom_audio_providers.py:644 take_connection 预热命中 → 日志 TURN_ASSISTANT 对账。**目标：能指着日志里每个数字说出它产生在哪一行。**

---

## 11. 已知陷阱速查（改代码前必看）

1. **别把 `vad=` 从 AgentSession 上摘掉**（主文件:274）——判停和打断闸门会失去时间源，延迟飘高。
2. **别降 `min_words` 回 2**——"单字+句号"=2 词会重新放行背调词打断（修过的 bug）。
3. **别尝试 DashScope TTS 跨轮持久连接**——cancel 不生效，残留音频串台，是验证过的死结（§7.2）。
4. **别给离线 STT 上传加限速**——服务端攒齐才识别，限速纯拖慢。
5. **`HF_HUB_OFFLINE=1`（主文件:43）别删**——删了每次启动多等 ~30s 连 huggingface 超时重试。更新判停模型时临时设 0。
6. **transcription_delay 高 ≠ STT 慢**——口径含 VAD hold 0.35s（§4.2）。
7. **console 交互式切 text/audio 模式会剥掉 KWS 包装层**（cli.py 切模式时重赋 input.audio）——音频测试别中途切模式。
8. **生产 room 模式没有 console 的免费 AEC**——上线前必须另配，否则 KWS 会被 AI 自己的声音触发。
