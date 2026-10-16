# 小歌语音 Agent：代码阅读与延迟分析指导

> 面向要读懂当前实现、定位一轮对话延迟或开始改代码的开发者。按“入口装配 → 一轮音频 → 轮次决策 → 回复输出 → 指标定位”的顺序阅读。
>
> 本文以当前仓库符号为准，行号易漂移，优先按文件与函数名定位。架构全景见[架构说明](ARCHITECTURE.md)；框架内核的逐符号深读见[源码级框架导读](../../examples/voice_agents/qwen_voice_agent_code_guide.md)。

## 先建立三个心智模型

1. **这是应用层对 LiveKit Agents 编排内核的扩展**：`AgentSession` 管会话，`AgentActivity` 管当前轮次/打断/抢先生成，应用层负责选择模型、装配 tap 和业务策略。不要把框架默认行为与本应用覆盖配置混为一谈。
2. **音频主路与旁路并存**：主路必须逐帧向下游传递；KWS、在线 ASR、录音等 tap 旁路观察或复制音频。一个 input tap 丢帧/不返回，就可能让 VAD/STT 断粮。输出 tap 同样要保留 `flush`、`clear_buffer` 等控制语义，才能正确打断。
3. **低延迟来自并行和隐藏等待，不是把每个阶段都变快**：VAD/STT/EOU/LLM/TTS 的部分工作重叠，预热把建连搬到用户说话或上一轮播放期间；分析时要看 wall-clock 时间线，不能简单把所有组件耗时相加。

## 推荐阅读顺序

| 顺序 | 阅读位置 | 要回答的问题 |
| --- | --- | --- |
| 1. 应用入口 | `examples/voice_agents/web_ui_agent.py`：`VoiceAgent`、`entrypoint` | 会话在哪创建？业务钩子做了什么？装配顺序是什么？ |
| 2. 后端选择 | `examples/voice_agents/app/setup_taps.py`：`setup_stt`；`examples/voice_agents/app/backends.py`：`STT_BACKENDS`、`TTS_BACKENDS`、`build_stt`、`build_tts`、`build_llm` | 当前选了什么 STT/TTS/LLM？哪些能热切换？ |
| 3. 会话装配 | `examples/voice_agents/app/setup_taps.py`：`SessionWiring`、`register_session_handlers`、`setup_test_instrumentation` 及各 `setup_*` | 事件处理器、观测工具、音频 tap 以什么顺序接入？ |
| 4. 轮次参数 | `examples/voice_agents/turn_config.py`：`TurnConfig.from_env`、`turn_handling`；入口处 `prewarm` | 本应用的静音阈值、endpointing、打断与抢先生成值从哪里来？ |
| 5. 框架主干 | `livekit-agents/livekit/agents/voice/agent_session.py` → `voice/agent_activity.py` → `voice/audio_recognition.py` | 音频如何进入活动状态机？何时形成用户轮、何时提交回复？ |
| 6. STT 路径 | `livekit-agents/livekit/agents/stt/stream_adapter.py` 与 `examples/voice_agents/providers/stt/` | 主 STT 是整段识别还是原生流式？VAD/分段由谁负责？ |
| 7. 回复与音频输出 | `web_ui_agent.py` 的 `VoiceAgent` 节点重写；`examples/voice_agents/providers/tts/`；`AgentActivity` 回复生成路径 | LLM 首 token 后文本如何变成 TTS 首包和输出音频？打断如何取消/清空播放？ |
| 8. 指标和测试 | `examples/voice_agents/app/setup_taps.py`：`_log_user_item`、`_log_assistant_item`；`examples/voice_agents/common/runtime.py`；`turn_metrics.py`、`event_timeline.py` | 看到的延迟数值测量了什么？怎样获得细粒度事件？ |

### 运行入口与调度边界

本地调试通常从 `python examples/voice_agents/web_ui_agent.py console`（或项目运行脚本）进入；框架调度/作业上下文可从 `livekit-agents/livekit/agents/worker.py`、`job.py` 和 `ipc/proc_pool.py` 继续追。调度到 `entrypoint` 之后才进入本文关注的一轮实时对话链路；不要把 job 启动/模型冷启动耗时与稳定会话内每轮时延混算。

## 一轮对话：按数据流追源码

```text
麦克风/浏览器音频输入
  → session.input.audio（可被 MuteGate、KWS/在线识别/录音 tap 包装）
  → AgentSession 音频转发
  → AgentActivity.push_audio / AudioRecognition
       ├─ VAD：用户说话状态、语音起止与判停时钟
       └─ STT：转写事件 ───────────────┐
                                        ├→ EOU / endpointing → 用户轮提交
                          preemptive ───┘       → on_user_turn_completed 业务过滤/归一化
                                                   → LLM 流式生成
                                                   → 文本处理 / transcription 广播
                                                   → TTS 流式合成 → output.audio → 播放/推流
```

同时存在不等主链路结束的**打断旁路**：本地 KWS 命中可快速强打断；在线 2-pass ASR 可在播报期间基于增量识别触发打断（host 策略装配在 `app/online_interrupt_host.py`，音频/识别传输在 `online_interrupt.py`）；主 STT final 的停止词处理是另一层策略。旁路的结果与主 STT 转写不可混淆：例如在线打断用的文本不一定进入对话上下文。

### STT 分支是理解延迟的关键

`setup_stt()` 根据 `XIAOGE_STACK`/`STT_BACKEND` 装配路径：

- **默认 `upstream` + `funasr`**：离线 STT 经框架 `StreamAdapter`，由 VAD 切出整段后调用识别；`StreamAdapter` 这段等待是结构性延迟，持久 WebSocket 复用主要省连接握手，不会消除等整段的成本。
- **`optimized` 默认 `funasr-stream`，或显式流式后端**：原生流式 STT 自己维护 VAD/聚合，绕过 `StreamAdapter`；切分与结果节奏不同，不能照搬离线路径的耗时解释。
- 非流式与流式后端的 partial/final 行为不同，live 转写显示源也可能不同。切换后先确认运行时日志里的 `STT_START provider=... mode=... stack=...`，再按对应实现分析。

## 全链路延迟：阶段、口径与重叠

分析前先定起止点。常见目标是**用户停说 → agent 首个可听/可推送音频帧**；另有用户开口到首帧、用户停说到整段回复播完等不同口径。它们不能用同一个“延迟”字段替代。

| 阶段 | 主要代码/事件锚点 | 可能的耗时来源 | 读数注意事项 |
| --- | --- | --- | --- |
| 静音确认/切段 | `TurnConfig.vad_min_silence_s`；VAD 与 `StreamAdapter` | 静音门限；流式路径自己的端点/VAD 聚合策略 | 仅看用户停说后的等待；该时间可能已与 STT 流式处理重叠。 |
| 主 STT 转写 | `AudioRecognition`、`StreamAdapter`、具体 Provider 的 recognize/stream | 离线整段上传、服务端排队/推理、网络 RTT、连接冷启动；流式后端的聚合/最终化 | WebSocket 复用/预热减少建连，不等于识别计算时间为零。 |
| EOU/endpointing | `audio_recognition.py` 的 EOU 与延迟收尾逻辑；`TurnConfig.endpoint_*` | 模型判断及 min/max 等待策略 | EOU 推理可与等待窗并行；按最后语音时间锚定的等待可能扣除已流逝静默，别把完整阈值重复加一遍。 |
| LLM 首 token | `VoiceAgent`/LLM 节点；`llm_node_ttft` | 网关 RTT、排队、首 token 推理、上下文长度 | preemptive generation 可在轮次最终确认前启动，已重叠部分不应再次全额加到 E2E。若输入最终被改写/忽略，抢先结果可能取消。 |
| TTS 首包 | TTS 节点与 Provider；`tts_node_ttfb` | 首句文本到达、TTS 建连/会话更新、服务端首包、重采样 | 按句/片段流式可降低首包等待；预热连接隐藏的是握手，冷启动与热连接要分别比较。 |
| 首帧输出/播放 | `session.output.audio` 或 WebSocket 输出首帧；播放事件 | 本地缓冲、编码/传输、播放器缓冲/设备启动 | `tts_node_ttfb` 不是物理扬声器出声时间；优先找实际首帧/播放锚点。 |

概念上可写作：

```text
停说→首帧 ≈ max(主 STT 可用时间, VAD/EOU/endpointing 可提交时间, 抢先回复准备时间)
          + 轮次提交后仍未完成的 LLM/TTS/输出等待
```

这只是解释并行关系的示意，不是精确计算公式。实际链路受 provider、配置、服务端负载、音频输出形态影响；文档中的历史实测数值（例如约 1.1s）仅是特定环境/口径的观测，不构成 SLA 或当前性能保证。稳定比较应固定场景、后端、配置和起止点，报告样本数及中位数/p90。

### 优化点应映射到等待段

- 离线 STT：复用连接减少每轮握手；快速上传降低“说完信号”后的额外等待；检查是否仍被 VAD 整段切分限制。
- 流式 STT：关注首个 interim、finalize 和 gap/VAD 门控，不要只看请求总耗时。
- LLM：预热/连接池、禁用不需要的推理模式、控制上下文和输出长度，比较 `llm_node_ttft`。
- TTS：连接预热、按句增量合成，比较 TTFB 与首帧间隔；区别合成首包和真正播放启动。
- 判停：用 `TURN_*` 配置评估更快 endpointing 与误切/残片回复之间的权衡；不是越短越好。

## 指标怎么读、怎么定位

### 逐轮摘要

`app/setup_taps.py` 的 `_log_user_item()` 输出 `TURN_USER`，含转写、语音窗、`transcription_delay`、`end_of_turn_delay`、`on_user_turn_completed_delay`；`_log_assistant_item()` 输出 `TURN_ASSISTANT`，含 `llm_ttft`、`tts_ttfb`、`playback_latency`、`e2e_latency`、`wall_clock_e2e` 和 `felt`，并输出 `WALL_CLOCK_E2E`。项目其他历史日志可能仍出现 `FELT_LATENCY` 名称，当前此处日志标签以源码为准。字段来自框架 item metrics 或应用侧时间锚点，读前要回到计算处核对。

`common/runtime.py` 的 `append_turn_log()` 将指标行排队交后台线程写入 `TURN_METRICS_LOG`（默认 `qwen_voice_turn_metrics.log`），避免同步文件写拖住 agent 事件循环；与 `timeline.jsonl` 的事件时间线是两类不同产物。


- `conversation_item_added(assistant)` 在播放完成附近才提交；应用代码明确说明 `event.created_at + tts_node_ttfb` 是播放结束附近的估算，而不是首个音频帧。
- WebSocket 音频输出可能不发框架依赖的 `playback_started`，因此框架 `e2e_latency` 可能为空；应用会尝试用 `burst_start_ts` 回填首帧口径，缺失时退回近似值。以当前运行形态和输出实现为准。
- `FELT_LATENCY`/KPI 的 felt 估算存在事件锚点兜底路径；`turn_metrics.py` 注释也记录 web/headless 下 `agent_state_changed → speaking` 不一定派发。将指标当作定位线索，不要把近似值误称为硬件播放实测。

### 逐步排查

1. **先固定口径与配置**：记录 `XIAOGE_STACK`、`STT_BACKEND`、`TTS_BACKEND`、`TURN_*`、输入/输出形态及场景；确认 `STT_START` 选择结果。
2. **看用户轮指标**：`transcription_delay` 或 EOT 异常大时，分别检查识别最终化、StreamAdapter/VAD 切段、EOU/endpointing；`audio_recognition.py` 中 `transcription_delay` 是最后 final 转写相对最后语音时间的差值，并非纯 ASR 推理耗时；`end_of_turn_delay` 是到轮次收尾计算点的耗时。读取对应代码核对当前版本的计时锚点。
3. **看 assistant 指标**：`llm_ttft` 大查 LLM/gateway；`tts_ttfb` 大查首段文本、TTS 连接与 provider；两者都正常但体感仍慢，追踪输出首帧、播放器缓冲和计时锚点。
4. **开测试时间线复核先后关系**：设置 `AGENT_TIMELINE=1`（或使用项目测试运行方式），查看 `runs/<时间戳>/timeline.jsonl`、`debug.log`、`turn_kpis.json`。时间线事件可对齐并行发生的阶段；debug 文件在测试模式安装，正常运行默认不挂文件日志处理器。
5. **做受控对比**：同一录音/注入场景下每次只改一个参数或后端，比较多轮样本的中位数/p90、识别覆盖率、过度切分/残片和误打断，而非只比较一次最快值。

## 按问题继续读

- **改会话/轮次/打断**：`livekit-agents/livekit/agents/voice/agent_session.py`、`agent_activity.py`、`audio_recognition.py`；再读 `turn_config.py`、`app/setup_taps.py` 的策略钩子。
- **改 STT**：`app/setup_taps.py:setup_stt`（按符号搜索）、`livekit-agents/livekit/agents/stt/stream_adapter.py`、`examples/voice_agents/providers/stt/` 与 `app/backends.py` 注册表。
- **改 TTS/输出**：`examples/voice_agents/providers/tts/`、`web_ui_agent.py` 的节点重写、`livekit-agents/livekit/agents/voice/` 的生成与 playout 路径。
- **查延迟观测**：`app/setup_taps.py`、`common/runtime.py`、`turn_metrics.py`、`event_timeline.py`。
- **查完整系统结构、线程/事件循环和风险**：回到[架构说明](ARCHITECTURE.md)；需要深挖框架状态机时读[源码级框架导读](../../examples/voice_agents/qwen_voice_agent_code_guide.md)。
