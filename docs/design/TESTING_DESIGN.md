# 自动化测试子系统 — 设计 + 落地现状(已实现 vs 规划)

> 目标:给 Xiaoge Duplex Speech 增加**全自动对话测试**能力——埋点结构化、日志与录音按真实时间对齐、可无人值守跑端到端对话并自动判定。
> 已定方向:**合成音频注入**(走完整 VAD→STT→判停→打断链,最贴近真实)。
> 参考:`duplexMVP2`(xiaoge)成熟的 timeline + 多轨录音 + 声明式断言 + run 目录 + 报告体系。
>
> **本文已分清「已实现 vs 规划」**:P0 数据基座(结构化时间线 + 判停 KPI + 多轨录音)与
> 阶段1 的录音回放注入(`ScriptedAudioInput`)**已落地**;声明式场景/断言/报告/headless runner
> 等仍为**规划,未实现**,正文相应段落已标注。已实现部分以 `examples/voice_agents/` 下的实际
> 代码为准:`event_timeline.py`、`turn_metrics.py`、`test_recorder.py`、`scripted_audio.py`,
> 入口分支在 `web_ui_agent.py`。

---

## 1. 目标与范围

- **埋点(instrumentation)**:把目前分散的文本日志(`qwen_voice_turn_metrics.log` 的 TURN_USER/TURN_ASSISTANT、`STOP_KWS_EARLY`/`OVERLAP_ONLINE_INTERRUPT` 等)升级为**结构化事件流**,带 turnId/责任模块/payload。
- **时间对齐**:日志事件与录音音频共享同一时间基(microsecond,monotonic 排序 + wall 关联),做到“拿一条日志就能定位到录音的那一帧”。
- **合成音频注入**:用脚本驱动“用户台词”→ TTS 合成 → 按节奏推入音频输入,**经过真实 VAD/STT/判停/打断链**。
- **全自动对话测试**:无人值守跑完一组场景,**声明式断言**判 pass/fail,产出 JSON+MD 报告与失败归类。
- **不在本期**:UI 自动化、压测/并发、真麦克风众测(可作为后续“手动 replay”模式保留)。

---

## 2. 与 duplexMVP2 的对照(可借鉴 / 必须改造)

| 维度 | duplexMVP2(参考) | 本工程现状 | 结论 |
|---|---|---|---|
| 事件总线 | 自研同步 `EventBus` + `Event`(eventId/atUs/wallTimeUs/turnId/source/payload) | 无;只有文本日志 | **已实现** `EventTimeline`(`event_timeline.py`,订阅 session 事件 + emit + 后台线程写盘) |
| 时间线 | `timeline.jsonl`(append+fsync) | 无结构化 | **已实现**:`timeline.jsonl`(append + flush) |
| 用户输入注入 | 文本注入 `/api/message`(模拟 ASR 输出,不过 STT) | console/麦克风 | **已实现(阶段1)**:**录音回放注入**(`ScriptedAudioInput`,wav 按真实节奏注入,走完整 STT/打断链)。合成 TTS 注入仍为规划 |
| 录音 | 多轨 user/assistant/marker/mixed,共享 at_us | `audio_recorder.py` 混音单 wav | **已实现**:**新增** `test_recorder.py`(`TestRecorder`)录 user/assistant/duplex,与 timeline 同源时钟。`audio_recorder.py` 维持正常模式单文件混音不变 |
| 断言 | 8 类声明式(exists/absent/order/latency/text/intent…) | 无 | **(规划,未实现)** |
| 运行 | HTTP API,轮询,无人值守 | console 单会话 | **(规划,未实现)**;当前靠环境变量在 `web_ui_agent.py` 内启用 |
| 报告 | report.json + report.md + issues(归类疑似模块) | 无 | **(规划,未实现)**;当前产物为 `turn_kpis.json`(判停 KPI) |

**关键架构差异**:MVP2 是 HTTP 服务 + 自研同步事件总线;本工程是 LiveKit 单事件循环 + 框架事件。因此我们**不照搬 EventBus**,而是**订阅 LiveKit 的 `session.on(...)` 事件 + 在自定义打断/STT 处补埋点**,统一写入 timeline。注入也从“文本”升级为“音频”,因为本工程的价值正是 VAD/三层打断,必须被测到。

---

## 3. 子系统架构(5 个组件)

> **落地现状**:组件 A(EventTimeline)、C(多轨录音)**已实现**;组件 B 的**录音回放注入**
> (`ScriptedAudioInput`)**已实现**,但其「TTS 合成 + 台词库 + 缓存」部分及组件 B+(persona/
> 拟人节奏)、D(Scenario/Assertion)、E(Runner/Report)**均为规划,未实现**。下图含未实现部分。

```
                        ┌────────────── Scenario(声明式场景) ──────────────┐
                        │ steps[ {trigger, utterance, expect[]} ]            │
                        │ assertions[ {type, ...} ]                          │
                        └───────────────┬───────────────────────────────────┘
                                        ▼
   ┌─────────────┐   合成音频    ┌──────────────────┐   帧(实时节奏)   ┌──────────────┐
   │ 用户嗓音 TTS │ ───────────► │ ScriptedAudioInput│ ───────────────► │ AgentSession │
   └─────────────┘              │ (替换 input.audio)│                  │ (真实全链路) │
                                └──────────────────┘                  └──────┬───────┘
                                        ▲ 触发器:delay/after_event/on_idle         │ 事件
   ┌──────────────────────────┐         │                                          ▼
   │ EventTimeline(JSONL)     │ ◄───────┴─────── 订阅 session 事件 + 自定义埋点 ───┐
   │ at_us(mono)+wall+turnId  │                                                    │
   └──────────┬───────────────┘        ┌──────────────────────────┐              │
              │ 共享 at_us              │ 多轨录音 user/assistant/  │ ◄────────────┘
              ▼                         │ marker/mixed + manifest  │
   ┌──────────────────────────┐        └──────────────────────────┘
   │ AssertionRunner + Report │ ──► report.json / report.md / issues(疑似模块)
   └──────────────────────────┘
```

### A. EventTimeline(结构化埋点 + 落盘)
- 统一事件模型:`{eventId, type, atUs(monotonic µs), wallUs, turnId, source, payload}`。
- **来源**:① 订阅 LiveKit `session.on("conversation_item_added"/"agent_state_changed"/"user_state_changed"/"user_input_transcribed"/"metrics_collected")`;② 在自定义点补发事件:KWS 命中、在线打断、停止词 StopResponse、FunASR final、后端切换。
- 事件类型(初版):`asr.final / turn.user / turn.assistant / agent_state.changed / interrupt.kws / interrupt.online / interrupt.stopword / tts.first_byte / playback.started/finished / eou.decision`。
- 写 `timeline.jsonl`(append + flush)。这是日志与录音的**对齐主键**。

### B. ScriptedAudioInput(音频注入)—— 最关键
- **已实现(录音回放注入)**:`scripted_audio.py` 的 `ScriptedAudioInput` 是实现 `io.AudioInput`
  的“假麦克风”:不读真实设备,而是按脚本产出帧;`AGENT_SCENARIO` 设了即**替换**
  `session.input.audio`(taps 仍叠在其上,KWS/在线打断被真实驱动)。
- **已实现**:**实时节奏注入**——把 wav 切 `frame_ms`(默认 10ms)帧,按**绝对时刻对齐**实时推入
  (防累计漂移,3 秒就占 3 秒,绝不快进);开头注入 `lead_silence_s`(默认 4.0s)静音让开场白先放完,
  wav 放完后**持续吐静音**驱动 VAD 收尾(不结束迭代,以免误判输入关闭)。
- **(规划,未实现)**:**台词→音频解析的 TTS 链路**。当前只支持直接给定 wav(`AGENT_SCENARIO` 指向
  wav,或 json 里 `wav` 字段);**预录台词库 `assets/lines/`、TTS 缓存 `assets/voice_cache/`、
  离线 TTS 合成兜底均未实现**,故 `utteranceId`/`audioRef`/`<hash>.wav` 等概念尚不存在。

### B+. 真实对话节奏模型(**规划,未实现**)
> 以下 persona / 触发器 / 拟人时序模型为**规划,未实现**。当前注入仅为「单段 wav 实时回放」(见 §B),
> 尚无可组合的用户行为时间表。

注入不是“发完一句等一句”,而是一套**可组合的用户行为时间表**,锚定到 agent 事件,默认实时:

- **触发器(可组合)**:
  - `after_agent_idle(gapMs)`:等 `agent_state→listening` 后,再停一个**拟人响应间隔**(默认 300–1200ms,带可控抖动)再开口——常规轮次。
  - `after_event(type, offsetMs)`:如 `playback.started + 1500ms` 让用户**在 agent 播报中开口**(打断/补充场景)。
  - `during_agent_speaking(...)`:在 agent 说话期注入**短附和**(“嗯/对”)测“不抢话”,或**多次连续补充**测软打断。
  - `delay(ms)`:固定延时(可预测,用于回归基线)。
- **拟人时序要素**:轮间响应间隔分布(非固定)、句内停顿/口头语(“嗯…”)测判停与 backchannel、打断的**起始偏移 + 重叠时长**可控、随机抖动(**seeded 可复现**)。
- **节奏画像(pacing profile)**:把上述参数打包成 persona(语速、间隔分布、打断倾向),一套场景可在不同画像下跑,覆盖“急性子抢话 / 慢条斯理 / 边想边说”等真实风格。
- 说明:真实对话还有“说一半改口、对方没听清重复、长静默”等,先把上面这套**可扩展的行为/触发模型**搭好,后续按需加行为类型,不必一次穷尽。

### C. 多轨录音对齐(**已实现**:`test_recorder.py` / `TestRecorder`,非升级 `audio_recorder.py`)
- **实际**:多轨录音是**新增独立模块** `test_recorder.py` 的 `TestRecorder`;`audio_recorder.py`
  保持不变,仍是正常模式的单文件混音录音(`recordings/<ts>/conversation.wav`)。
- **实际轨道**:`user`(麦克风/注入音频)、`assistant`(TTS 输出)、`duplex`(立体声:左=user,
  右=assistant)。`marker`(事件标记音)与独立 `mixed` 轨**未实现**——立体声 `duplex.wav` 即承担
  混听用途;`marker` 标注为「(规划,未实现)」。
- 每段带 `at_us`,与 timeline 同源时钟(monotonic µs);输出 `user.wav / assistant.wav /
  duplex.wav` + `audio_manifest.json`(含 `baseAtUs / sampleRate / tracks(段数、帧数、时长)/
  duplex`)。`eventTimestamps` 字段**未实现**,标注「(规划,未实现)」。
- 价值:断言失败时可直接定位/回放对应音频片段。

### D. Scenario + Assertion(声明式)(**规划,未实现**)
> 声明式场景与断言框架**未实现**。当前 `AGENT_SCENARIO` 的 json 仅承载注入参数
> (`wav/expect/lead_silence_s/frame_ms`),其中 `expect` 用于 `turn_metrics.py` 的覆盖率 KPI,
> 并非完整断言体系。下文 schema 为目标设计。

- `Scenario{ id, name, goal, steps[], assertions[], artifactsRequired[] }`。
- `Step{ stepId, trigger, utterance|action, expect[] }`(action 如“静音/切后端”用于测语音控制/面板)。
- 断言类型(初版,对齐 MVP2):`event_exists / event_absent(窗口) / order / latency(A→B ≤ ms) / text_contains / intent_is / no_audio_after_cancel`。
- 场景与断言是**纯数据**,可序列化、可版本化、可作为“需求即测试”。

### E. Runner + Report(**规划,未实现**)
> headless runner、断言执行、`report.json`/`report.md`/`issues` 归类、pytest/CI 门禁**均未实现**。
> 当前唯一的自动产物是 `turn_kpis.json`(判停 KPI,见 `turn_metrics.py`)。

- **无人值守驱动**:headless 模式跑完场景 → 跑断言 → 出 `report.json` + `report.md` + `issues`(失败→疑似模块,如 FastInterrupt/Playback/STT)。
- 可被 `pytest` 包一层做 CI 门禁。

---

## 4. run 目录布局(**已实现**,时间戳)

实际产物(由 `AGENT_TIMELINE=1` 触发,写到仓库根 `runs/<时间戳>/`):

```
runs/<YYYYmmdd_HHMMSS>/
├── timeline.jsonl          # 结构化事件(对齐主键 atUs,monotonic µs)
├── user.wav                # 用户(麦克风/注入)单声道
├── assistant.wav           # 小歌(TTS)单声道
├── duplex.wav              # 立体声:左=user / 右=assistant(替代 mixed)
├── audio_manifest.json     # baseAtUs / sampleRate / tracks / duplex
├── turn_kpis.json          # 判停 KPI 汇总(turn_metrics.py)
└── debug.log               # 全量 DEBUG 日志(install_debug_log)
```

(`runs/` 已加入 .gitignore。)

**(规划,未实现)** 以下产物尚未生成,留作 P2/P3:
- `marker.wav`、独立 `mixed.wav`(目前用 `duplex.wav` 立体声承载混听);
- `report.json` / `report.md`(断言结果 + 摘要,P2);
- `observed.json`(末态快照,P3);
- `runs/auto/<scenario-id>/run-.../` 这种按场景分层的路径(当前为扁平 `runs/<时间戳>/`)。

---

## 5. 与现有代码的集成点
- **入口(已实现)**:`web_ui_agent.py` 已有“测试模式”分支,靠**环境变量**启用,默认零开销:
  - `AGENT_TIMELINE=1`:创建并 attach `EventTimeline`(timeline.jsonl)+ `TurnMetrics`
    (turn_kpis.json)+ `install_debug_log`(debug.log),并安装 `TestRecorder` 录多轨音频。
  - `AGENT_SCENARIO=<wav 或 json>`:在 session 启动后用 `ScriptedAudioInput` 替换
    `session.input.audio`(在 recorder/KWS/在线打断 tap 包裹之前),注入音频如实流经全链路。
    json 支持字段:`wav` / `expect`(可选,喂给覆盖率 KPI)/ `lead_silence_s`(默认 4.0)/
    `frame_ms`(默认 10)。
- **(规划,未实现)** headless 测试入口 `python -m tools.auto_test --scenario <id>`、`tools/` 目录、
  “假 AudioOutput 仅录音”的扬声器旁路:**均未实现**。当前注入运行在常规 agent 进程内(console/dev/
  start 任一形态 + 上述环境变量),扬声器仍按正常输出链路播放并由 `TestRecorder` 旁路录音。
- **埋点**:`EventTimeline` 旁路订阅 session 框架事件;过渡期与现有 `_append_turn_log(...)` 文本日志并存。

---

## 6. 决策(已定)与待定项

**已定(本次评审)**
- **运行形态**:**实时节奏注入**为第一原则(墙钟实时、正常语速)。默认 **headless 入口**(不开真麦克风/扬声器,扬声器走“假 AudioOutput”仅录音),CI 友好;console 注入作为人工旁听的可选模式。两者共用同一套实时注入与节奏模型。
- **用户嗓音**:**预录 wav 台词库优先**;无对应 wav 时**离线 TTS 兜底并缓存**为 wav。台词库 + `assets/voice_cache/` 纳入版本管理。
- **回合节奏**:采用 **B+ 可组合行为/触发模型**(常规轮 `after_agent_idle` + 打断轮 `after_event`/`during_agent_speaking`),拟人间隔 + seeded 抖动;承认真实更复杂,模型保持可扩展。
- **首批场景集**:**对齐 MVP2 较全集**(见 §6.1)。

**待定(请拍板)**
- **A. pass/fail 与延迟阈值来源**:沿用本应用 `turn_handling` 的值,还是单列“测试延迟预算表”(如 felt e2e ≤ 1.5s、KWS 打断 ≤ 0.8s、在线打断 ≤ 1.2s)?
- **B. 拟人间隔/抖动的具体分布与默认 persona**(响应间隔区间、打断起始偏移、是否默认开抖动)。
- **C. 离线 TTS 具体选型**(edge-tts 需联网首取再缓存 / 纯本地模型);以及台词库的组织方式(按 utteranceId 还是按文本 hash)。
- **D. 是否保留真麦克风手动 replay 模式**作为补充。

### 6.1 首批场景集(对齐 MVP2 B1/FD 系列,P0 先跑通)
| ID | 名称 | 重点 | 优先级 |
|---|---|---|---|
| B1-001 | 普通自由问答 | 能听能答、端到端延迟 | **P0** |
| FD-004 | 强打断立即停播 | KWS/能量打断及时性 | **P0** |
| FD-001 | 语音填充词过滤·不抢话 | backchannel 不误打断 | **P0** |
| FD-002 | 播放中补充并合并上下文 | 软打断→等说完→重规划 | P1 |
| FD-003 | 暂停与继续 | 暂停/恢复 | P1 |
| FD-005 | 连续多次打断 | 持续输入稳定性 | P1 |
| B1-008 | 澄清问题 | 追问/澄清 | P1 |
| B1-009 | 结合第一轮上下文 | 上下文延续 | P1 |
| B1-011 | 上下文隔离 | 多会话/清场 | P1 |
| B1-012 | 长对话连贯性 | 长时稳定(配模拟用户) | P2 |
| (新增) | 停止词沉默 / 数字逐位读 | 本工程特有(StopResponse/数字归一化) | P1 |

---

## 8. 详细设计 v2(按评审意见,四块全部细化)

> **(本节大部分为规划,未实现)** 下面给出具体 schema / 接口草图 / 决策分叉(标 ❓),仍是设计稿。
> 已落地的只有:§8.1 的 `EventTimeline` / `timeline.jsonl` / 多轨录音 + `audio_manifest.json`
> (但**无 `marker` 轨、无 `eventTimestamps`**);§8.2 的 `ScriptedAudioInput`(**仅录音回放,
> 无 `resolve(utterance)` 的台词库/TTS 缓存链路,无 `RecordingOnlyAudioOutput` 假扬声器**)。
> §8.3(persona)、§8.4(Scenario/Assertion/Evaluator/Report/headless Runner `python -m
> tools.auto_test`)**均未实现**。

### 8.1 数据基座(时钟 · 事件 · 时间线 · 录音)
**单一时钟 + ID 工厂**(全子系统共用,保证日志与录音同源):
```
Clock.now_us()  -> monotonic µs(排序/对齐)     Clock.wall_us() -> epoch µs(人读/关联)
IdFactory: evt_000001 / turn_000001 / resp_000001
```
**Event 模型**(JSONL 一行一个):
```
{ eventId, type, atUs, wallUs, turnId?, responseId?, source, payload }
```
**规范事件目录(LiveKit / 自定义 → 统一 type)**:
| 来源 | canonical type | 关键 payload |
|---|---|---|
| session `user_input_transcribed`(interim/final) | `asr.interim` / `asr.final` | text, provider |
| session `conversation_item_added`(user/assistant) | `turn.user` / `turn.assistant` | text, metrics(各 delay/ttft/ttfb) |
| session `agent_state_changed` | `agent_state.changed` | old,new |
| session `user_state_changed` | `user_state.changed` | old,new |
| 自定义埋点(KWS/在线/停止词) | `interrupt.kws` / `interrupt.online` / `interrupt.stopword` | keyword/chars |
| 自定义(TTS 首包/播放) | `tts.first_byte` / `playback.started` / `playback.finished` | turnId |
| 判停 | `eou.decision` | prob, delay_used |
**TimelineRecorder**:`attach(session)` 挂 `session.on(...)`;对外暴露 `emit(type,payload,...)` 供自定义点调用;写 `timeline.jsonl`(append+flush)。过渡期与现有 `_append_turn_log` 双写。
**录音(多轨,共享 Clock)**:`RecordedSegment{at_us, pcm, track}`;轨 `user`(注入旁路)/`assistant`(TTS output tap)/`marker`;输出 `*.wav` + `audio_manifest.json{sampleRate, tracks:{name:{segments:[{at_us,frames}]}}, eventTimestamps}`。
❓**8.1-a** 事件目录是否够用/要加哪些?❓**8.1-b** 是否要 `marker` 轨(把关键事件渲染成可听标记)?

### 8.2 注入机制(假麦克风 + 台词解析 + 实时帧)
**ScriptedAudioInput(io.AudioInput)**:
```
schedule: list[(start_at_us, frames[])]      # 由 acts 编译得到
_pump():  按 20ms 帧、用 monotonic 节拍实时 push;空档 push 静音帧驱动 VAD
```
- 安装点:测试模式下 `session.input.audio = ScriptedAudioInput(...)` 作为**最内层 source**,KWS/在线/录音 tap 仍包在外 → 注入音频真实流经三层打断。
- **假扬声器** `RecordingOnlyAudioOutput(io.AudioOutput)`:headless 下不出声,只计时+落 assistant 轨(保留 `clear_buffer/flush` 转发以维持打断语义)。
**台词解析 `resolve(utterance) -> pcm`(wav 优先)**:
```
1) assets/lines/<utteranceId>.wav        # 预录库,最真/最稳(真人语速)
2) assets/voice_cache/<sha1(text+voice+rate)>.wav   # 命中缓存
3) 离线 TTS 合成 → 写入缓存 → 用之       # 兜底,可复现
```
❓**8.2-a** 台词键用 `utteranceId`(显式、可读)还是 `text hash`(零维护)?建议 **id 优先、缺则 hash**。
❓**8.2-b** 假扬声器是否也提供“实时播放可旁听”的 console 变体?

### 8.3 真实节奏 / persona(你强调的重点)
**用户行为 = 可组合的 UserAct 时间表**:
```
UserAct{ id, trigger, content:(line|backchannel|behavior), concurrentWithAgent?:bool }
Trigger = after_agent_idle(gap_ms_range)        # 常规轮:等 listening + 拟人间隔
        | after_event(type, offset_ms_range)    # 播放中开口(打断/补充)
        | during_agent_speaking(at_ratio|ms)    # agent 说话期插入(附和/抢话)
        | delay(ms)                             # 固定(回归基线)
```
**PacingProfile(persona)**:
```
{ response_gap_ms:{min,max}, jitter_seed,
  barge_in:{prob, anchor_event, offset_ms_range, overlap_ms},
  backchannel:{prob, tokens:["嗯","对","好"]},
  intra_pause:{prob, ms_range} }            # 句内停顿/边想边说
```
示例 persona:`急性子`(gap 150–400、barge_in.prob 0.6)、`常规`(gap 400–900、prob 0.15)、`慢条斯理`(gap 900–1800、intra_pause 多)。
**更复杂的真实行为(分期纳入)**:`self_correct`(说一半改口=两段拼接+中途停)、`repeat_on_misunderstanding`(检测到 agent 澄清→重复/换说法)、`long_silence`、`overlap`(双方重叠)。先把上面**可扩展的 Act/Trigger/Profile 框架**搭好,行为类型按期追加。
❓**8.3-a** 三个 persona 的默认数值认可吗?❓**8.3-b** 默认是否开 jitter(更真但需 seed 固定以复现)?❓**8.3-c** 首期要支持哪几种复杂行为(self_correct / repeat / overlap)?

### 8.4 测试上层(场景 · 断言 · 报告 · runner)
```
Scenario{ id, name, goal, persona?, acts:[UserAct], assertions:[Assertion], artifactsRequired:[] }
Assertion = event_exists(type[,window]) | event_absent(type, after,until)
          | order(typeA, typeB, ...) | latency(from,to,maxMs)
          | text_contains(type, *subs) | intent_is(*vals)        # intent 待 #5 落地
          | no_audio_after_cancel
```
**Evaluator**:读 `timeline.jsonl + manifest + observed` → 逐条 `{status, evidence, suspectedModules}`。
**Report**:`report.json`(schema 版本 + checks + issues + summary)+ `report.md`(人读);`issues` 由失败断言映射到疑似模块(FastInterrupt/Playback/STT/EOU…)。
**Runner(headless)**:`python -m tools.auto_test --scenario <id> [--persona <p>] [--seed N]`
```
启 test-mode session(ScriptedAudioInput + RecordingOnlyOutput + TimelineRecorder)
→ 编译 acts→schedule → 实时驱动 → 末 act 完成后等 N 秒静默判终止/或全局 timeout
→ 跑 assertions → 出 report → 退出码 = pass/fail(可被 pytest/CI 包一层)
```
❓**8.4-a** pass/fail 延迟阈值:沿用 `turn_handling` 值,还是单列“测试延迟预算表”?
❓**8.4-b** 终止条件:末 act 后“静默 N 秒”合适?N 取多少?❓**8.4-c** 报告里是否需要 issues→疑似模块的映射表(需要我先定义模块归类规则)?

---

## 7. 分阶段落地建议(评审后)
- **P0**:EventTimeline + 多轨录音对齐(先让“跑一轮真人对话”能产出对齐的 timeline+wav)。
- **P1**:ScriptedAudioInput + 用户嗓音 TTS + 单场景跑通(端到端无人值守一条)。
- **P2**:Scenario/Assertion/Report 框架 + 首批 P0 场景。
- **P3**:模拟用户 LLM 自对话(长对话连贯性/压力)、issues 归类、pytest/CI 门禁。

> 落地后若架构图需要补一张“测试子系统”视图,我再加。
