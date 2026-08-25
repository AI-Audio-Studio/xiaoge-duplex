# 关键机制设计图集

> 覆盖：打断 / 判停 / 拒识 / 气泡四大机制
> 代码基准：`examples/voice_agents/` + `livekit-agents/livekit/agents/`

---

## 目录

1. [系统整体架构](#1-系统整体架构)
2. [音频处理 Tap 链](#2-音频处理-tap-链)
3. [判停（End-of-Turn）流程](#3-判停-end-of-turn-流程)
4. [打断三条路径数据流](#4-打断三条路径数据流)
5. [拒识决策树](#5-拒识决策树)
6. [气泡数据流](#6-气泡数据流)
7. [完整轮次生命周期时序](#7-完整轮次生命周期时序)
8. [状态机：聆听模式](#8-状态机聆听模式)
9. [关键参数速查表](#9-关键参数速查表)

---

## 1. 系统整体架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                          Browser (WebPanel)                         │
│                                                                     │
│  ┌──────────┐  ┌──────────┐  ┌────────────┐  ┌───────────────────┐ │
│  │ 麦克风   │  │ 用户气泡 │  │ 助手气泡   │  │  状态栏/控制面板  │ │
│  │ PCM流    │  │ (live)   │  │ (final)    │  │  ASR/TTS切换      │ │
│  └────┬─────┘  └────▲─────┘  └─────▲──────┘  └────────▲──────────┘ │
└───────┼─────────────┼───────────────┼──────────────────┼────────────┘
        │ /ws/audio   │ broadcast     │ broadcast        │ broadcast
        │ (PCM binary)│ (message/user)│ (message/asst)   │ (state)
┌───────┼─────────────┼───────────────┼──────────────────┼────────────┐
│       │         Gateway Layer       │                  │            │
│  ┌────▼─────────────────────────────────────────────────▼────────┐  │
│  │                    proxy.py  ←→  affinity.py                  │  │
│  │   handle_ws_audio()    handle_ws_state()    handle_http()      │  │
│  │   (PCM流双向泵)         (状态通道泵)          (HTTP反代)         │  │
│  └────────────────────────────┬──────────────────────────────────┘  │
│                               │                                      │
│  ┌────────────────────────────▼──────────────────────────────────┐  │
│  │                       Pool Manager                            │  │
│  │   manager.py  →  default_spawn()  →  web_ui_agent.py console  │  │
│  │   (进程池)         (子进程启动)        (Agent 进程)             │  │
│  └───────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
        │ PCM binary frames        ▲ PCM binary frames
        │                         │
┌───────▼─────────────────────────┴────────────────────────────────────┐
│                        Agent Process                                  │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────────┐ │
│  │                     entrypoint()                                │ │
│  │  WebSocketAudioInput → tap链 → AgentSession → STT/VAD/LLM/TTS  │ │
│  └─────────────────────────────────────────────────────────────────┘ │
│                                                                       │
│  ┌──────────────┐  ┌─────────────┐  ┌────────────┐  ┌────────────┐  │
│  │  webpanel/   │  │  online_    │  │  kws_      │  │  listening │  │
│  │  bridge.py   │  │  interrupt  │  │  interrupt │  │  mode      │  │
│  │  (broadcast) │  │  (2pass旁路)│  │  (KWS唤醒) │  │  (聆听态)  │  │
│  └──────────────┘  └─────────────┘  └────────────┘  └────────────┘  │
└───────────────────────────────────────────────────────────────────────┘
```

---

## 2. 音频处理 Tap 链

> 装配顺序决定数据流向。越晚装配的 tap 越靠外层（先收到数据）。

### 2a. 装配顺序（entrypoint 中从上到下）

```
entrypoint()
  │
  ├─①─ setup_scenario_injection()
  │       session.input.audio = ScriptedAudioInput   [仅 AGENT_SCENARIO 有效]
  │
  ├─②─ setup_web_audio()
  │       session.input.audio = WebSocketAudioInput  [浏览器 PCM；WEB_AUDIO=1]
  │       session.output.audio = WebSocketAudioOutput(本地输出)
  │
  ├─③─ setup_mute_gate()
  │       session.input.audio = MuteGate(②)
  │
  ├─④─ setup_recording()
  │       recorder.install(session)                  [挂事件监听，不包裹 audio]
  │
  ├─⑤─ setup_kws()
  │       session.input.audio = KwsTapAudioInput(③)
  │
  └─⑥─ setup_online_interrupt()
          session.input.audio = OnlineTapAudioInput(⑤)
```

### 2b. 运行时音频数据流（从源头到消费者）

```
  Browser /ws/audio
  (PCM binary, 16kHz, 10ms帧)
         │
         ▼
  ┌─────────────────────────┐
  │  WebSocketAudioInput ②  │  Queue(maxsize=400) 帧级缓冲
  │  _sync_push → __anext__ │  超时50ms → 补静音帧
  └────────────┬────────────┘
               │  AudioFrame
               ▼
  ┌─────────────────────────┐
  │       MuteGate ③        │  静音=输出全零帧（真关麦）
  │   MuteGate(AudioInput)  │  下游：不转写、不打断、无人声
  └────────────┬────────────┘
               │
         ┌─────┴────────────────────────────┐
         │  (tee: 同一帧送往多个下游)        │
         ▼                                  ▼
  ┌────────────────────┐      ┌──────────────────────────┐
  │  KwsTapAudioInput⑤│      │  OnlineTapAudioInput ⑥   │
  │  (喂 KWS 声学模型) │      │  (喂 FunASR 2pass 在线流) │
  │  唤醒词 → KWS事件  │      │  增量文本 → _online_text  │
  └────────────────────┘      │  _fanout()               │
               │              └──────────────────────────┘
               │                           │
               └──────────┬────────────────┘
                          │  (passthrough: 原帧继续传)
                          ▼
               ┌───────────────────────┐
               │   AgentSession.input  │
               │   .audio              │
               └───────────┬───────────┘
                          │
               ┌───────────┴───────────┐
               │                       │
               ▼                       ▼
    ┌──────────────────┐    ┌─────────────────────┐
    │  VAD (Silero)    │    │  STT                │
    │  min_silence=    │    │  funasr-stream /    │
    │  0.35s           │    │  qwen3 / iflytek    │
    │  → 开口/停说事件  │    │  → interim / final  │
    └──────────────────┘    └─────────────────────┘
```

### 2c. 音频输出链

```
  AgentSession.output.audio
         │  AudioFrame (TTS合成)
         ▼
  ┌────────────────────────────────────────┐
  │       WebSocketAudioOutput             │
  │  ┌────────────────────────────────┐   │
  │  │ next_in_chain (本地扬声器输出)  │   │  headless=None
  │  └────────────────────────────────┘   │
  │  capture_frame() →                    │
  │    broadcast_audio(pcm)      ─────────┼──→ /ws/audio 客户端
  │    next.capture_frame(frame) ─────────┼──→ 本地扬声器
  └────────────────────────────────────────┘
  clear_buffer() → broadcast_audio_ctrl({type:clear})
                   next.clear_buffer()
```

---

## 3. 判停（End-of-Turn）流程

```
用户开口说话
     │
     ▼
┌────────────────────────────────────────────────────────┐
│  VAD Layer（Silero）                                    │
│                                                        │
│  音频帧 → 语音概率 → 状态机                              │
│                                                        │
│    speech_prob > threshold → "SPEAKING"                │
│    speech_prob < threshold                             │
│    AND 静音持续 >= min_silence_duration(0.35s)          │
│                    ↓                                   │
│             触发 "END_OF_SPEECH" 事件                  │
└────────────────────────┬───────────────────────────────┘
                         │  VAD EndOfSpeech
                         ▼
┌────────────────────────────────────────────────────────┐
│  Turn Detection Layer（AgentSession 内部）              │
│                                                        │
│  ┌──────────────────────────────────────────────────┐  │
│  │  EOU Model（MultilingualModel）                  │  │
│  │                                                  │  │
│  │  输入: 当前已转写文本（in-flight transcript）     │  │
│  │  输出: EOU 概率（0~1，该轮是否真的"说完了"）      │  │
│  │                                                  │  │
│  │  EOU prob >= unlikely_threshold(默认=模型内置)    │  │
│  │    → 判定"说完"                                  │  │
│  │    → 等 min_delay(0.3s) → 提交轮次              │  │
│  │                                                  │  │
│  │  EOU prob < unlikely_threshold                   │  │
│  │    → 判定"没说完，继续等"                         │  │
│  │    → 等到 max_delay(0.6s) → 强制提交            │  │
│  └──────────────────────────────────────────────────┘  │
└────────────────────────┬───────────────────────────────┘
                         │  Endpointing 完成
                         ▼
┌────────────────────────────────────────────────────────┐
│  preemptive_generation（TURN_PREEMPTIVE_TTS=true）      │
│                                                        │
│  STT final 到达 前：已开始 LLM 推理（用当前 best文本）  │
│  STT final 到达 后：若内容不同则重推；相同则直接播      │
│                                                        │
│  收益：减少首响延迟                                     │
│  风险：续话打断后残片回复（用户未说完就推了）            │
└────────────────────────┬───────────────────────────────┘
                         │
                         ▼
                  on_user_turn_completed()
                  → 拒识 → LLM → TTS

─────────────────────────────────────────────────────────
时间轴示意：

  用户:  [开口────────说话──────停─────────]
  VAD:   [speech─────────────────]silence─0.35s─→ END
  EOU:   (文本到了就算)→ P=0.8 > th → min_delay=0.3s → ✓提交
                      → P=0.2 < th → ...等...max_delay=0.6s → ✓提交

  preemptive=true:
  LLM:              [→推理开始(最佳文本)──────]
  STT final:                        [final到达→若相同复用]
  TTS:                              [────合成播放────]
```

---

## 4. 打断三条路径数据流

```
时间轴（用户说话覆盖 AI 播报）：

  AI 播报: ████████████████████████████████████████████
  用户开口:         │
                   ▼

  音频帧 → OnlineTapAudioInput
               │
               ├──→ FunASR 2pass 在线流（最快，~0.3s）
               │         │
               │    [路径A] online_interrupt_host.py:59
               │    ┌─────────────────────────────────────────┐
               │    │ _accumulate_online_text()                │
               │    │   ① 聆听期保护 → 不判（清累积）          │
               │    │   ② segment_end → 不判                  │
               │    │   ③ AI未在speaking → 不判               │
               │    │   ④ 打断后1s防抖                        │
               │    └──────────────┬──────────────────────────┘
               │                   │ accum 文本
               │    ┌──────────────▼──────────────────────────┐
               │    │ _judge_online_interrupt()                │
               │    │                                         │
               │    │  should_ignore_user_turn(accum)?        │
               │    │  ├─ YES → session.interrupt(force=True) │ ← 最早，~0.3s
               │    │  │        broadcast({type:clear})       │
               │    │  └─ NO                                  │
               │    │       meaningful_chars >= min_chars?    │
               │    │       AND vad_ok(说话中 or 刚停0.6s内)? │
               │    │       ├─ YES → session.interrupt()      │ ← 软打断
               │    │       └─ NO  → 累积继续等               │
               │    └─────────────────────────────────────────┘
               │
  音频帧 → AgentSession.input → VAD → STT
                                          │
                                     STT final（~0.5-1.5s）
                                          │
               [路径B] setup_taps.py:225  │
               ┌──────────────────────────▼──────────────────┐
               │ _handle_stt_event()                         │
               │                                             │
               │  should_ignore_user_turn(transcript)?       │
               │  ├─ YES AND NOT listen_interrupt_blocked()  │
               │  │    → session.interrupt(force=True)       │ ← 早打断兜底
               │  └─ NO                                      │
               │       overlap AND is_overlap_ack()?         │
               │       └─ YES → session.clear_user_turn()   │ ← 清轮，不入LLM
               └─────────────────────────────────────────────┘
                                          │
                                    轮次进入处理
                                          │
               [路径C] web_ui_agent.py:253│
               ┌──────────────────────────▼──────────────────┐
               │ on_user_turn_completed()                    │
               │                                             │
               │  _handle_listening_turn()  ← 聆听期逻辑     │
               │  _maybe_auto_enter_listening()              │
               │                                             │
               │  _apply_turn_filters()                      │
               │  ├─ should_ignore_user_turn()               │
               │  │    → NOT listen_interrupt_blocked():     │
               │  │         session.interrupt(force=True)    │ ← 最晚兜底
               │  │    → raise StopResponse()               │
               │  ├─ is_backchannel()                        │
               │  │    → raise StopResponse()               │ ← 只跳回复
               │  └─ overlap AND is_overlap_ack()            │
               │       → raise StopResponse()               │
               └─────────────────────────────────────────────┘
                                          │ 通过所有过滤
                                          ▼
                                      LLM 推理

─────────────────────────────────────────────────────────────
三条路径对比：

  路径    触发时机           延迟       触发条件
  ─────────────────────────────────────────────
  A       在线2pass文本到达  ~0.3s      字数达标 + VAD佐证
  B       STT final         ~0.5~1.5s  停止词(force) / 附和词(clear)
  C       on_user_turn      ~1.5~2s    停止词 / 背调 / 压话附和

  三条路径对同一次打断可能全部触发，框架层需幂等（重复interrupt无害）。
```

---

## 5. 拒识决策树

```
on_user_turn_completed(text)
          │
          ▼
  ┌───────────────────────────┐
  │ 聆听期处理                │  _handle_listening_turn()
  │                           │
  │  ctrl.active == True?     │
  │  ├─ YES → ctrl.capture()  │  ← 吞入缓冲，不回复，不进上下文
  │  │         raise StopResponse
  │  │
  │  └─ NO                    │
  │      listen_tail_pending? │  ← 退出后尾巴窗（~1.5s）
  │      ├─ YES, 无唤醒词     │    → 整条吞，StopResponse
  │      ├─ YES, 有唤醒词     │    → 切分：丢唤醒词前，留后半
  │      └─ NO → 继续         │
  └───────────────────────────┘
          │
          ▼
  ┌───────────────────────────┐
  │ 自动进入聆听              │  _maybe_auto_enter_listening()
  │                           │
  │  连续 N 轮触发计数?       │
  │  ├─ 达到阈值 → 进入聆听   │  raise StopResponse（触发轮不回复）
  │  └─ 未达到 → 继续         │
  └───────────────────────────┘
          │
          ▼
  ┌──────────────────────────────────────────────────────────┐
  │  _apply_turn_filters(text)     common/text_rules.py      │
  │                                                          │
  │  Step 1: 停止词判定                                       │
  │  should_ignore_user_turn(text)                           │
  │                                                          │
  │    按标点分段 → 每段逐一判：                               │
  │    ┌──────────────────────────────────────────────────┐  │
  │    │ for seg in segments:                             │  │
  │    │   STOP_REPLY_PATTERNS.fullmatch(seg)? ──→ 停止词 │  │
  │    │   all(ch in OVERLAP_ACK_CHARS)? ─────→ 附和字   │  │
  │    │   else ───────────────────────────────→ 实义段   │  │
  │    │                                         (return False) │
  │    │ 全段通过 AND 至少一段是停止词 ──→ True             │  │
  │    └──────────────────────────────────────────────────┘  │
  │                                                          │
  │    TRUE →  NOT listen_interrupt_blocked()?               │
  │              YES → session.interrupt(force=True)  ←强打断 │
  │            raise StopResponse()  ←──────────────────跳回复│
  │                                                          │
  │  Step 2: 背调词判定（整句）                               │
  │  is_backchannel(text)                                    │
  │    BACKCHANNEL_RE: ^[嗯哦噢喔啊...][同类字+标点]*$       │
  │    TRUE → raise StopResponse()  ←────────────────只跳回复 │
  │                                                          │
  │  Step 3: 压话附和判定                                    │
  │  spoke_over_agent AND is_overlap_ack(text)               │
  │    is_overlap_ack: 剥标点后全是 OVERLAP_ACK_CHARS         │
  │    TRUE → raise StopResponse()  ←────────────────只跳回复 │
  └──────────────────────────────────────────────────────────┘
          │  三步全部通过
          ▼
  ┌─────────────────────────┐
  │  数字序列归一化          │
  │  "12345" → "1、2、3、4、5"│
  │  (纯2-16位数字串)        │
  └─────────────────────────┘
          │
          ▼
        LLM 推理

─────────────────────────────────────────────────────────
特殊词表速查：

  STOP_WORDS（28个）：停/停下/停一下/暂停/好了/行了/别说/别说了/
    别讲/别讲了/别念了/等一下/等等/等下/稍等/知道了/我知道了/
    闭嘴/安静/不听了/不用了/不要了/休庭(FunASR误识兜底)…

  STOP_LEAD_IN（引导词前缀）：那/你/就/请/先/那你/那就
    → "那别说了" 也能命中 "别说了"

  BACKCHANNEL_CHARS：嗯哦噢喔啊呃唉唔诶哼呢

  OVERLAP_ACK_CHARS：嗯哦噢喔啊呃唉唔诶哼呢 + 对好是行的呀嘛
```

---

## 6. 气泡数据流

> 气泡显示与 LLM 上下文是两条独立流，刻意解耦。

```
┌──────────────────────────────────────────────────────────────────────┐
│                        用户气泡数据流                                  │
│                                                                      │
│  音频 → STT                                                          │
│           │                                                          │
│           ├─ interim（流式STT）                                       │
│           │    │                                                     │
│           │    ├─ live_from_main=True（funasr-stream/iflytek）:       │
│           │    │    live.feed_full(transcript)  ← 全量置换            │
│           │    │                                                     │
│           │    └─ live_from_main=False（离线STT）:                    │
│           │         online_interrupt_host._online_text_fanout()      │
│           │         live.feed_online(piece, segment_end)  ← 增量追加  │
│           │                                                          │
│           │    └─→ LiveTranscript                                    │
│           │           _maybe_open() → 新气泡 or 并入当前             │
│           │           broadcast({type:"transcript",text:accum})      │
│           │                   ↓                                      │
│           │              Browser 气泡实时更新（边说边长）              │
│           │                                                          │
│           └─ final                                                   │
│                │  (STT定稿)                                          │
│                ▼                                                     │
│         live.feed_commit(transcript)  ← 中途final追加（防气泡缩水）   │
│                │                                                     │
│                └─→ _log_user_item():                                 │
│                    LEADING_PUNCT_RE.sub("",text)  ← 去句首游离标点   │
│                    broadcast({type:"message",role:"user",text:净化后})│
│                    LLM上下文用原文（不净化）                           │
└──────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────┐
│                        助手气泡数据流                                  │
│                                                                      │
│  AgentSession → LLM 推理 → token流                                   │
│                                │                                     │
│                    ┌───────────▼────────────────────┐               │
│                    │  transcription_node()           │               │
│                    │  (web_ui_agent.py:155)          │               │
│                    │                                 │               │
│                    │  chunk → collected.append(chunk)│               │
│                    │  yield chunk（不阻断TTS流）      │               │
│                    │                                 │               │
│                    │  收集完整文本后:                 │               │
│                    │  strip_markdown(full_text)      │               │
│                    │  NOT listen_ctrl.active:        │               │
│                    │    broadcast({type:"message",   │               │
│                    │              role:"assistant",  │               │
│                    │              text:净化后})       │               │
│                    └───────────┬────────────────────┘               │
│                                │ token流（原始，含markdown）          │
│                    ┌───────────▼────────────────────┐               │
│                    │  tts_node()                     │               │
│                    │  sanitize_stream(text)          │               │
│                    │  → 去markdown/符号/括号注释等    │               │
│                    │  → Agent.default.tts_node()     │               │
│                    │  → TTS合成 → 音频输出            │               │
│                    └─────────────────────────────────┘               │
└──────────────────────────────────────────────────────────────────────┘

净化层分工：
  LEADING_PUNCT_RE  → 用户气泡：去句首游离标点（FunASR遗留）
  strip_markdown()  → 助手气泡：去md格式（**bold** ## 等）
  sanitize_stream() → TTS流：去所有不适合朗读的符号

上下文原则：
  用户原文  不净化  进 ChatContext（保证LLM理解正确）
  助手原文  不净化  进 ChatContext（add_to_chat_ctx=True）
  add_to_chat_ctx=False 的 say() → 只播放，不进上下文
    使用场景：聆听期提示 / "要整理吗" / 开场白等
```

---

## 7. 完整轮次生命周期时序

```
时间 →

用户:    静默  │开口──────────说话──────────停顿──续说──停────│
               │
VAD:           │[SPEAKING]─────────────────────────────[SILENCE 0.35s]→EndOfSpeech
               │
2pass在线:     │   [增量文本t1]─[t2]──[t3]──────────────────
               │         │       │     │
               │    [路径A]     [路径A] [路径A]─→ 实义字数达标+VAD佐证
               │                              │
               │     (AI正在speaking时才判断)  └──→ session.interrupt()
               │
STT:           │                    [interim─────][final]
               │                                   │
               │                             [路径B] _handle_stt_event()
               │                                   └─→ 停止词 interrupt
               │                                       附和词 clear_turn
               │
EndOfSpeech    │─────────────────────────────────────────►│
               │                           │
EOU Model:     │                           ├ P>th → wait min_delay(0.3s)
               │                           └ P<th → wait max_delay(0.6s)
               │
preemptive:    │               [LLM推理开始(最佳interim)───────────────]
(=true时)      │                           │
               │                     STT final到达 → 检查是否一致
               │
on_user_turn   │─────────────────────────────────────────────────────►│
_completed:    │                                                       │
               │    ┌──────────────────────────────────────────────┐  │
               │    │ ① 聆听期处理（吞入/切分）                     │  │
               │    │ ② 自动进入聆听检测                            │  │
               │    │ ③ 停止词/背调/附和 → StopResponse            │  │
               │    │ ④ 数字归一化                                  │  │
               │    └──────────────────────────────────────────────┘  │
               │                                                       │
LLM:           │                                           [推理────────────────]
               │                                                       │
transcription  │                                               [收集─────────]
_node:         │                                                   │
               │                                           strip_markdown
               │                                           broadcast(assistant气泡)
               │
tts_node:      │                                                   [合成──]
               │                                                        │
               │                                              sanitize_stream
               │                                              → TTS引擎
               │
TTS播放:        │                                                        [████████████]
               │                                                        │
               │              用户再次开口打断 ──────────────────────────►│
               │                                                      interrupt()
               │                                                      clear_buffer()
               │                                              WebSocketAudioOutput
               │                                              .clear_buffer()
               │                                              broadcast_audio_ctrl(clear)
               │
Browser:       │[用户interim气泡─────────────────][用户final气泡]       [助手气泡]
               │                                                        [音频播放████]
```

---

## 8. 状态机：聆听模式

```
                     ┌─────────────────┐
                     │   NORMAL 态     │
                     │  (正常对话)      │
                     └────────┬────────┘
                              │
           ┌──────────────────┼──────────────────┐
           │                  │                  │
    KWS唤醒到聆听词     连续N轮触发         手动通话键
    setup_kws()        auto_count达标      listen_on_mic_toggle()
           │                  │                  │
           └──────────────────▼──────────────────┘
                              │
                    listen_enter_aftermath()
                       ① 取消旧TTL
                       ② session.say(enter_notice,
                            add_to_chat_ctx=False,
                            allow_interruptions=False)
                       ③ broadcast({type:listening,on:True})
                              │
                              ▼
                     ┌─────────────────┐
                     │   ACTIVE 态     │◄────────────────────┐
                     │  (聆听期)        │                     │
                     │                 │   用户说话 → 吞入缓冲│
                     │  ctrl.active=T  │   不显示气泡         │
                     │  用户语音不打断  │   不进上下文         │
                     │  小歌受控播报    │   not force_interrupt│
                     └────────┬────────┘                     │
                              │                              │
           ┌──────────────────┼──────────────────┐          │
           │                  │                  │          │
     KWS听到退出词       手动通话键              TTL超时      │
     ctrl.split_after_  listen_on_mic_toggle()  （无实质内容）
     command()                │                  │          │
           │                  │                  ▼          │
           │                  │           temp_ttl到期       │
           └──────────────────▼──────────────────┘          │
                              │                              │
                    listen_exit_aftermath()                  │
                       ① listen_arm_tail()（尾巴窗~1.5s）    │
                       ② listen_arm_ttl()（内容TTL定时器）   │
                       ③ broadcast({type:listening,on:False})│
                       ④ 有实质内容 AND organize_enabled?    │
                              │                              │
                    ┌─────────┴──────────┐                  │
                  YES                   NO                  │
                    │                   │                   │
             listen_ask_organize()      ▼                   │
             "要整理吗?"          NORMAL 态                  │
             allow_interruptions=F                          │
                    │                                       │
                    ▼                                       │
           AWAITING_ORGANIZE 态                             │
             （等用户回答）                                  │
                    │                                       │
           ┌────────┴────────┐                             │
         肯定回答           否定回答                         │
           │                 │                             │
    整理摘要(进上下文)      NORMAL态                        │
    LLM生成summary                                         │
           │                                               │
           ▼                                               │
         NORMAL 态                                         │
                                                           │
注：ACTIVE 态内若用户一直在说话 → 累积 temp_transcript →──┘
    ctrl.auto_turns 计数不在此期间增加（吞入不算轮次）
```

---

## 9. 关键参数速查表

### 判停参数

| 环境变量 | 默认值 | 说明 | 代码位置 |
|---|---|---|---|
| `TURN_VAD_MIN_SILENCE` | 0.35s | VAD 静音判停阈值 | `turn_config.py:25` |
| `TURN_ENDPOINT_MIN_DELAY` | 0.3s | EOU 通过后最短等待 | `turn_config.py:27` |
| `TURN_ENDPOINT_MAX_DELAY` | 0.6s | 超时强制提交 | `turn_config.py:28` |
| `TURN_PREEMPTIVE_TTS` | true | VAD后立即推LLM（加速首响） | `turn_config.py:30` |
| `TURN_UNLIKELY_THRESHOLD` | 模型默认 | EOU概率<此值=未说完 | `turn_config.py:37` |

### 打断参数

| 环境变量 | 默认值 | 说明 | 代码位置 |
|---|---|---|---|
| `TURN_INTR_MIN_WORDS` | 3 | 软打断最少词数 | `turn_config.py:32` |
| `TURN_INTR_MIN_DURATION` | 2.0s | 软打断最短说话时长 | `turn_config.py:33` |
| `TURN_INTR_BACKCHANNEL` | (1.8,3.5) | 背调边界窗 | `turn_config.py:34` |
| `XIAOGE_ONLINE_VAD_GRACE` | 0.6s | VAD佐证宽限（文本滞后容忍） | `online_interrupt_host.py:35` |

### 在线打断参数

| 环境变量 | 默认值 | 说明 | 代码位置 |
|---|---|---|---|
| `XIAOGE_ONLINE_MIN_CHARS` | (OnlineInterruptConfig) | 在线2pass触发打断最少实义字 | `online_interrupt.py` |

### 气泡参数

| 环境变量 | 默认值 | 说明 | 代码位置 |
|---|---|---|---|
| `LIVE_TRANSCRIPT` | true | live 气泡开关 | `live_transcript.py:41` |
| `LIVE_TRANSCRIPT_NEW_TURN_GAP` | 1.5s | 停顿超此秒数=新气泡 | `live_transcript.py:44` |

### STT 路由参数

| 环境变量 | 可选值 | 说明 | 代码位置 |
|---|---|---|---|
| `XIAOGE_STACK` | `optimized`/`upstream` | optimized默认funasr-stream | `setup_taps.py:80` |
| `STT_BACKEND` | `funasr`/`funasr-stream`/`iflytek`/`qwen3`/`qwen3-stream` | 显式覆盖STT | `setup_taps.py:82` |

---

*生成时间: 2026-07-13 · 基于代码二次验证，与实际实现一致*
