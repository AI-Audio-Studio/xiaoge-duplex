# 自动化测试子系统 — 设计提案(待评审,先设计后实现)

> 目标:给 Xiaoge Duplex Speech 增加**全自动对话测试**能力——埋点结构化、日志与录音按真实时间对齐、可无人值守跑端到端对话并自动判定。
> 已定方向:**合成音频注入**(走完整 VAD→STT→判停→打断链,最贴近真实)。
> 参考:`duplexMVP2`(xiaoge)成熟的 timeline + 多轨录音 + 声明式断言 + run 目录 + 报告体系。
> 本文是**方案**,不含实现;评审通过后再分阶段落地。

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
| 事件总线 | 自研同步 `EventBus` + `Event`(eventId/atUs/wallTimeUs/turnId/source/payload) | 无;只有文本日志 | **需新增** EventTimeline(可不引入全量 EventBus,先做“订阅+落盘”) |
| 时间线 | `timeline.jsonl`(append+fsync) | 无结构化 | **需新增** |
| 用户输入注入 | 文本注入 `/api/message`(模拟 ASR 输出,不过 STT) | console/麦克风 | **改造**:做**合成音频注入**(比 MVP2 更真,覆盖 STT/打断) |
| 录音 | 多轨 user/assistant/marker/mixed,共享 at_us | `audio_recorder.py` 混音单 wav | **升级**为多轨 + 与 timeline 对齐 |
| 断言 | 8 类声明式(exists/absent/order/latency/text/intent…) | 无 | **需新增** |
| 运行 | HTTP API,轮询,无人值守 | console 单会话 | **新增** headless 注入运行形态 |
| 报告 | report.json + report.md + issues(归类疑似模块) | 无 | **需新增** |

**关键架构差异**:MVP2 是 HTTP 服务 + 自研同步事件总线;本工程是 LiveKit 单事件循环 + 框架事件。因此我们**不照搬 EventBus**,而是**订阅 LiveKit 的 `session.on(...)` 事件 + 在自定义打断/STT 处补埋点**,统一写入 timeline。注入也从“文本”升级为“音频”,因为本工程的价值正是 VAD/三层打断,必须被测到。

---

## 3. 子系统架构(5 个组件)

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

### B. ScriptedAudioInput(合成音频注入)—— 最关键
- 一个实现框架 `io.AudioInput` 的“假麦克风”:不读真实设备,而是按脚本产出帧;在测试模式下**替换** `session.input.audio`(taps 仍叠在其上,KWS/在线打断被真实驱动)。
- **台词→音频解析(wav 优先,离线 TTS 兜底)**:`utterance` 优先用台词库里的**预录 wav**(`audioRef`,最稳/最真、按真人语速录制);无对应 wav 时用**离线 TTS** 合成(normal rate),并**缓存**到 `assets/voice_cache/<hash>.wav` 复用,保证可复现、CI 无网络可跑。台词库 + 缓存是版本化资产。
- **实时节奏注入(关键)**:把音频切 10/20ms 帧,按**墙钟实时速率**推入(3 秒的话就占 3 秒,绝不快进)——否则 VAD/STT/判停/打断的时序全失真。语速取正常值(`speech_rate=1.0` 或录音原速)。静默期注入背景静音帧驱动 VAD 收尾。

### B+. 真实对话节奏模型(按你强调:“正常人机交互节奏、正常语速;真实比这更复杂”)
注入不是“发完一句等一句”,而是一套**可组合的用户行为时间表**,锚定到 agent 事件,默认实时:

- **触发器(可组合)**:
  - `after_agent_idle(gapMs)`:等 `agent_state→listening` 后,再停一个**拟人响应间隔**(默认 300–1200ms,带可控抖动)再开口——常规轮次。
  - `after_event(type, offsetMs)`:如 `playback.started + 1500ms` 让用户**在 agent 播报中开口**(打断/补充场景)。
  - `during_agent_speaking(...)`:在 agent 说话期注入**短附和**(“嗯/对”)测“不抢话”,或**多次连续补充**测软打断。
  - `delay(ms)`:固定延时(可预测,用于回归基线)。
- **拟人时序要素**:轮间响应间隔分布(非固定)、句内停顿/口头语(“嗯…”)测判停与 backchannel、打断的**起始偏移 + 重叠时长**可控、随机抖动(**seeded 可复现**)。
- **节奏画像(pacing profile)**:把上述参数打包成 persona(语速、间隔分布、打断倾向),一套场景可在不同画像下跑,覆盖“急性子抢话 / 慢条斯理 / 边想边说”等真实风格。
- 说明:真实对话还有“说一半改口、对方没听清重复、长静默”等,先把上面这套**可扩展的行为/触发模型**搭好,后续按需加行为类型,不必一次穷尽。

### C. 多轨录音对齐(升级 `audio_recorder.py`)
- 轨道:`user`(注入音频)、`assistant`(TTS 输出)、`marker`(事件标记音,可选)、`mixed`。
- 每段带 `at_us`,与 timeline 同源;输出 `*.wav` + `audio_manifest.json`(段边界 + `eventTimestamps`)。
- 价值:断言失败时可直接定位/回放对应音频片段。

### D. Scenario + Assertion(声明式)
- `Scenario{ id, name, goal, steps[], assertions[], artifactsRequired[] }`。
- `Step{ stepId, trigger, utterance|action, expect[] }`(action 如“静音/切后端”用于测语音控制/面板)。
- 断言类型(初版,对齐 MVP2):`event_exists / event_absent(窗口) / order / latency(A→B ≤ ms) / text_contains / intent_is / no_audio_after_cancel`。
- 场景与断言是**纯数据**,可序列化、可版本化、可作为“需求即测试”。

### E. Runner + Report
- **无人值守驱动**:headless 模式跑完场景 → 跑断言 → 出 `report.json` + `report.md` + `issues`(失败→疑似模块,如 FastInterrupt/Playback/STT)。
- 可被 `pytest` 包一层做 CI 门禁。

---

## 4. run 目录布局(借鉴 MVP2,时间戳)

```
runs/auto/<scenario-id>/run-<YYYYMMDD-HHMMSS>/
├── timeline.jsonl          # 结构化事件(对齐主键 at_us)
├── user.wav assistant.wav marker.wav mixed.wav
├── audio_manifest.json     # 段边界 + eventTimestamps
├── report.json / report.md # 断言结果 + 摘要
└── observed.json           # 末态快照
```
(`runs/` 加入 .gitignore。)

---

## 5. 与现有代码的集成点
- **入口**:`web_ui_agent.py` 增加“测试模式”分支——用 `ScriptedAudioInput` 替换 `session.input.audio`(在 taps 链最内层),并挂 EventTimeline 订阅、升级录音 tap。
- **运行形态(待决策)**:推荐**新增 headless 测试入口**(如 `python -m tools.auto_test --scenario B1-001`),**不启 PortAudio/不开真扬声器**(扬声器输出走“假 AudioOutput”仅录音),CI 友好、无需音频设备;console 注入模式作为可选。
- **埋点**:把现有 `_append_turn_log(...)` 处同时发结构化事件(双写,过渡期兼容旧日志)。

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

> 下面给出具体 schema / 接口草图 / 决策分叉(标 ❓)。仍是设计稿,请直接在此红线。

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
