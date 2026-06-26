# 聆听模式(Listening Mode)设计文档 · v3

| 项 | 内容 |
|---|---|
| 功能 | 聆听模式 —— 小歌"只听不插":ASR 照常工作,但临时缓冲、不进上下文、不回复 |
| 参考 | sibling 工程 `duplexMVP2` 的 standby(**仅参考,非标杆**;按"更好体验"取舍) |
| 状态 | **v3 待复评**(纳入第一、二轮评审 + 体验优化,逐条经源码核实) |
| 范围 | 仅 `examples/voice_agents/`;新增独立模块 `listening_mode.py` + host 接线;**不改判停/STT/上游** |

> **v3 相对 v2 的变更**(三处"比照搬 duplexMVP2 更好"的体验优化):
> 1. **退出尾巴处理:尾巴窗 + 按唤醒词切分**(§5.5,几经迭代:内容感知剥词→整窗全吞→尾巴窗切分)——退出后开一次性尾巴窗,定位唤醒词(精确+模糊),**丢唤醒词及之前的监听内容、留之后接着说的真话并正常回复**;对 STT 听岔/聚合鲁棒。
> 2. **主动问只在有实质内容时**(§5.4)——temp 内容量 < M 时退出**不问、静默丢弃**,避免无意义打扰。
> 3. **UI 冻结走 host 侧 gate**(§5.3)——聆听期 host **停发** `_live` 气泡、进入提示作为**专门 `listening` 横幅**(非 assistant 气泡),屏幕干净;不再靠"纯前端遮罩盖住底层闪动"。
>
> v2 已确立(经源码核实,保留):§9.1 StopResponse 足够;`say(...,add_to_chat_ctx=False)`;force_exit `call_soon_threadsafe` 串行;`turn_ctx.add_message` 不用 `update_chat_ctx`;KWS `replace` 追加词表;聆听分支插 `on_user_turn_completed` 最前;默认开+不自动退出。

---

## 1. 背景与目标

### 背景
小歌全双工、随时应答。但用户**并非总在跟小歌对话**(开会、和旁人说话、自言自语、长口述),此时小歌频繁应答/被打断既打扰、又把无关内容塞进上下文。本功能让小歌在这些场景"只听不插",并能在结束后按需整理所听内容。

### 目标(G)
- **G1 只听不插**:聆听期不回复、不被带跑;ASR 仍工作。
- **G2 两种进入**:命令词 / 自动检测。
- **G3 两种退出**:命令词 / 通话键。**无自动退出**。
- **G4 不污染上下文**:聆听期文本进临时缓冲,用户明确要求前不进上下文;整理时**只把摘要留进历史**,原始自说自话不持久。
- **G5 解耦**:逻辑全在独立模块、可单测;host 仅持一个引用 + 接线(本仓既有范式)。
- **G6 可配置**:整功能一键开关,默认关(opt-in)。

### 非目标
常驻待机唤醒词;改判停/STT/VAD/关麦/上游;自动退出。

---

## 2. 设计原则
1. **纯状态机 + host 喂事件**:控制器 `listening_mode.py` 只持状态/缓冲/决策,**纯同步、无 asyncio、无 I/O**,不 import 工程模块。
2. **"纯状态机 ≠ 线程安全"**:线程安全靠 host 把所有变更串行到 agent 循环(§5.7)。
3. **最小落点 + 复用**:既有钩子(`_on_kws_hit`/`on_user_turn_completed`/`_handle_mic`/`broadcast`)接线 + 一个定时器协程 + 一个模块 global;命令词复用 KWS,通话键退出复用 `/api/mic`,进入提示复用 `session.say`。
4. **默认关、可配置**;关麦(挂起)与聆听是两个独立状态。

---

## 3. 架构

```
        ┌──────────── host: web_ui_agent.py(模块 global _listen_ctrl + 接线)────────────┐
KWS命中─►│ _on_kws_hit(agent循环): interrupt(force); evt=observe_keyword(kw)              │
          │   ENTERED/EXITED → 进入/退出收尾 + UI横幅 + (退出)定时器/主动问 + 置回声标志   │
用户轮 ──►│ on_user_turn_completed(agent循环,分支插最前):                                │
          │   ① 聆听期吞;①b 退出尾巴窗(§5.5):切分,丢唤醒词及之前、留之后正常回复       │
          │   ① active: capture; StopResponse()         # 不回复/不入ctx;UI不广播气泡    │
          │   ② awaiting: 答案(可为剥词剩余)→is_affirmative→turn_ctx 注入摘要; else 清    │
          │   ③ else: observe_turn==ENTER→say(notice,False)+进入+StopResponse             │
通话键 ──►│ _handle_mic(web循环): gate.muted^=1; call_soon_threadsafe(_listen_force_exit) │
ASR interim►│ _on_stt: if active: 不喂 _live(host gate,§5.3)                             │
定时器t ─►│ host asyncio(agent循环): _arm/_cancel/_expired                               │
          └────────────────────────────────┬─────────────────────────────────────────────┘
                                            │ 纯同步调用(全部在 agent 循环线程)
                                  ┌─────────▼─────────┐
                                  │ listening_mode.py │ ListeningController(纯状态机,可单测)
                                  └───────────────────┘
```

控制器 API(`@dataclass`,纯同步):
- `from_environment()`;属性 `enabled/active/awaiting_organize_answer/keywords/command_keyword/wake_keyword/temp_ttl_s/drain_s/enter_notice/min_organize_chars/organize_enabled/temp_transcript`。
- `observe_keyword(kw) -> Event{NONE,ENTERED,EXITED}`。
- `observe_turn(text, interrupted_agent) -> Auto{NONE,ENTER}`(仅未聆听时计数)。
- `capture(text)`;`force_exit() -> bool`(只在 agent 循环调,§5.7)。
- `split_after_command(text, keyword) -> str|None`:定位唤醒词(精确+归一化滑窗模糊),返回其后内容(""=纯退出指令,None=定位不到);供退出尾巴切分(见 §5.5)。host 侧另持尾巴窗 `drain_s`。
- `is_affirmative(text) -> bool`;`take_temp()`;`drop_temp()`;`clear_awaiting()`;`temp_has_substance() -> bool`(≥ `min_organize_chars`)。

---

## 4. 状态机
```
        命令词"小歌聆听模式" / 自动进入(连续N轮自说自话)
   正常 ─────────────────────────────────────────────►  聆听
    ▲                                                      │ 每轮 ASR→临时缓冲;不回复、不入ctx、UI冻结
    └──────────── 命令词"小歌干活了" / 通话键 ──────────────┘
```
无自动退出。

---

## 5. 详细设计

### 5.1 命令词(KWS)
- 进入命令词**可多个**(默认 `小歌聆听模式` / `小歌进入聆听模式` / `聆听模式`,`XIAOGE_LISTEN_COMMAND` 用 `|` 配,任一命中即进入;`enter_keywords` 属性汇总),退出 `小歌干活了`。`on_hit` 回传原词串,`observe_keyword` 按归一化匹配(进入侧 `hit in {归一化的各进入词}`)。注:`聆听模式`(4 字)较短,KWS 灵敏度高时偶有误触概率,作为便利项接受、可在 env 去掉。
- **词表合并(必做)**:`KwsConfig.from_env()` 设了 `XIAOGE_KWS_KEYWORDS` 时**整体覆盖**默认(证据 `kws_interrupt.py:92-99`)。故用 `dataclasses.replace` 把命令词**追加到已解析的 `_kws_config.keywords` 元组**。
- `_on_kws_hit`:**保留**顶部 `session.interrupt(force=True)`(证据 `web_ui_agent.py:1299`)——进入时希望小歌立刻停、退出时无妨;随后 `evt=observe_keyword(kw)`,ENTERED/EXITED 按聆听处理(并置回声标志,§5.5),NONE 落原打断逻辑。

### 5.2 自动进入
**判据**:连续 **N** 轮"自说自话";一轮需**同时** `interrupted_agent==True`(`_overlap_turn_state["user_spoke_over_agent"]`,证据 `web_ui_agent.py:1167-1168`)且 `len(text) >= L`。满足 +1,不满足清零,≥N → `observe_turn` 返回 `ENTER`(仅未聆听时计)。

**进入动作(就这一轮,之后全静默)**:置聆听态 → `notice=ctrl.enter_notice`,非空则 `session.say(notice, add_to_chat_ctx=False)`(默认 True 会写上下文,证据 `agent_session.py:1112`)→ `raise StopResponse()` 抑制本轮回复(**触发轮长文本不回复、不 capture,静默吞**)→ broadcast `listening` 横幅。

**默认开、不自动退出**(产品定)。逃生路径:进入提示话术 + UI 横幅 + 命令词 + 通话键。N=3/L≈20 起点。
- **已知固有代价(评审小注,接受)**:要连续 N 轮才判定,**前 N-1 轮的自说自话已被正常回复、已进上下文**,进入只静默第 N 轮;不做复杂的上下文回收(过度工程),靠提示让用户知晓。

### 5.3 聆听期行为 + UI 冻结(host 侧 gate)
- 每个**成轮**的 ASR final → `capture(text)` → `StopResponse()`(不回复、不入 ctx;StopResponse 足够,§9.1)。
  - 注:`_on_stt` 的 overlap-ack 早清(`clear_user_turn()`)在聆听期被跳过(`not _listening` 条件),让该轮流到 `on_user_turn_completed` 由 ① `capture`,避免聆听内容被早清丢掉。
- **UI 干净(host gate,优于纯前端遮罩)**:
  - 聆听 `active` 期,`_on_stt` 里**跳过 `_live.feed_*`**(不再广播 `user_partial` 闪动气泡;证据现状 `web_ui_agent.py:1174-1183`)。
  - `transcription_node` 在 `active` 时**只跳过 broadcast(`web_ui_agent.py:988`)**,不广播 assistant 气泡——故进入提示**不会变成气泡**。
  - **必守边界**:只 gate 上述**广播 + `_live.feed_*`**;**绝不 gate `tts_node`(`web_ui_agent.py:994`,独立路径)**——否则进入提示就不出声了。
  - 进入/退出广播 `{type:"listening", on, hint}`,前端据此显示/撤销一层**纱状遮罩**(`backdrop-filter` 半透明)**完整覆盖会话显示区**,顶部居中大字"聆听中"+退出方式提示;**底部 dock 的通话键 `z-index` 浮于遮罩之上仍可点**(退出路径不被挡)。
- **打断抑制(关键:聆听语句不应能打断小歌)**:聆听期用户的话不进显示/上下文,**也不该打断小歌的受控播报**(进入提示 / "要整理吗")。实测发现仅 gate 显示/`on_user_turn_completed` 不够——KWS 强打断、在线2pass 文本抢断、停止词早断、框架 VAD barge-in 仍会切掉小歌的提示。
  - 统一守卫 `_listen_interrupt_blocked()` = `active`(聆听期)**或**退出后保护窗内(`_LISTEN_GUARD_S`,默认 6s;退出"要整理吗"播报期 + 刚说的退出指令/前一句残留 STT 到达窗)。
  - 该守卫为真时,**全部直接打断路径短路**:`_on_kws_hit`、`_on_online_text`、`_on_stt` 停止词早断、`on_user_turn_completed` 停止词。进入提示与"要整理吗"用 `session.say(..., allow_interruptions=False)` 挡掉框架 VAD barge-in。
  - **进入瞬间**(尚未 `active`、保护窗未开)KWS 仍正常强打断小歌当前话(要立刻停下来听)。用户答完整理问题(②)即 `_listen_clear_guard()`,摘要/正常回复恢复可打断。关闭或正常态守卫恒 False → 主链路零影响。

### 5.4 退出 + 临时内容生命周期(G4 核心)
退出(命令词/通话键)时:控制器把工作缓冲转入 `temp_transcript`。

> **整理总开关 `organize_enabled`(`XIAOGE_LISTEN_ORGANIZE`,当前默认关)**:整理体验尚需打磨,先把**"问是否整理 + 整理动作"整体关掉**——退出不问、不注入、不整理;临时内容只按 **TTL 定时删除**(独立保留)。下面"主动问/整理回答"两段仅在该开关为 1 时生效。先让聆听**进入/退出**开关跑顺,整理后续再优化。
- **只在有实质内容时才走"主动问"**:`temp_has_substance()`(`≥ min_organize_chars`)为真 → 启动定时器 t、置 `awaiting_organize_answer`、`session.say("刚才听的我先存着了,要整理一下吗?", add_to_chat_ctx=False)`;否则 → **静默退出 + `drop_temp()`**(不问、不留)。
- **主动问的回答**:退出尾巴已被尾巴窗(§5.5)先处理,问话之后(关窗后)第一轮即回答。`is_affirmative(text)` 为真 → `take_temp()` → `turn_ctx.add_message(role="user", content="[聆听记录] 我刚才在聆听模式期间说了:…")` → **不抛 StopResponse**(LLM 本轮据此整理)→ `_cancel` 定时器、`clear_awaiting`;否则 → `clear_awaiting`、落正常逻辑,temp 留到 t 丢。
- **为什么 `turn_ctx.add_message` 而非 `update_chat_ctx`**:`turn_ctx` 是一次性副本 `chat_ctx.copy()`(证据 `agent_activity.py:2062`,注释"changes will not be kept"),只喂本轮 `_generate_reply`(2131)→ **原始内容只本轮可见、不持久**,历史只留"整理一下"+摘要,**正好 G4**。`chat_ctx` 是只读视图(`agent.py:156`)。
- **定时丢弃**:t 内无整理 → `drop_temp()`(留日志)。**再次进入(t 未到)**:**立即 `drop_temp()` + 取消定时器**,开新缓冲。

### 5.5 退出尾巴处理 —— 尾巴窗 + 按唤醒词切分(丢之前、留之后)
**根因(手测 run 20260625_210354 实证)**:KWS 是旁路 tap(`kws_interrupt.py:257-260` `__anext__` 每帧原样返回下游),且 **KWS 是声学命中、几乎即时**,而**主 STT 的 final 滞后 ~1.5s**(`transcription_delay≈1.5s`)。于是用户说"…[监听内容]小歌干活了"时:KWS 在唤醒词处即 `active=False`、撤横幅;但这句话(funasr-stream 连说会**聚合成一条** final)的 STT final 在 `active` 翻假**之后**才到 → 显示闸(`_on_stt`)与吞轮逻辑都以 `active` 为准、此刻已放行 → **整条(含唤醒词+前面的监听内容)被显示,前半段还被当成退出后正常话回复+进上下文**。

**需求升级**:不仅要丢"唤醒词及之前的聆听内容",还要**保留用户说完"小歌干活了"紧接着说的真话**并正常回复。

**做法(尾巴窗 + 切分;放弃整窗全吞 / 易漏的剥词)**:
- 退出瞬间照常 `active=False`、撤横幅(UI 跟手);同时开一个**一次性"尾巴窗"**:`_listen_exit_pending=True` + 安全时限 `drain_s`(默认 2.5s,`XIAOGE_LISTEN_DRAIN`),见 `_listen_arm_tail()` / `_listen_tail_pending()`。
- **显示**:`_on_stt` 的 `_listening = active or _listen_tail_pending()` → 尾巴窗内抑制 live 气泡(唤醒词及之前内容不闪出)。
- **吞轮 + 切分**(`on_user_turn_completed` ①b,窗内):用 `ctrl.split_after_command(text, wake)`(精确 + **归一化滑窗模糊**定位,容忍"小郭/小哥干活了")定位唤醒词:
  - **定位不到**(`None`)→ 整条吞(`StopResponse`),**窗保持**(等真正含唤醒词那条;也兜住滞后到达的监听 final)。
  - **定位到** → **关窗**(`_listen_consume_tail()`),丢唤醒词及之前;
    - 之后为空 → 吞(纯退出指令);
    - 之后非空("今天天气")→ `new_message.content=[after]`、**不** `StopResponse` → 作为正常用户轮:干净气泡(`conversation_item_added`,live 已抑制故 `addMsg`)+ 回复 + 进上下文。
- **时序保证**:唤醒词那条一旦定位即关窗,故"说完唤醒词紧接另起一句(独立 final,哪怕只隔 ~0.5s)"也能留住——关窗后它走正常。每次退出(命令词/通话键)都开窗;进入侧回声由 `active` 的 ① 直接吞。
- **权衡(接受、可调)**:唤醒词被 STT 听得太离谱(模糊也定位不到)时,尾巴窗会持续吞到时限 `drain_s`,这段时间内的真话可能被一起吞(极少;不比之前差)。窗长由 `XIAOGE_LISTEN_DRAIN` 调。
- 与 organize 的关系:尾巴窗先处理退出尾巴,真正的"要整理吗"回答(关窗后)再进②;organize 当前默认关,其交互后续随整理一起优化。

### 5.6 通话键交互(最小改动,复用 mic)
- 聆听期按通话键 = 退出聆听 **+** 复用原 `/api/mic` 静音切换 → **退出聆听 + 挂起(静音)**,UI"挂起中";再按 = 解除静音 = 正常。
- 通话键退出**也补问**:用户**再按通话键解除静音回正常**时,若 `temp_transcript` 仍在且有实质内容(t 未到)→ 这时触发"主动问"。(通话键路径静默,无命令词回声问题。)

### 5.7 并发模型(必做)
- 线程归属(已核实):`_on_kws_hit` 经 `call_soon_threadsafe` 在 **agent 循环**(`kws_interrupt.py:217`);`on_user_turn_completed` 在 **agent 循环**;`_handle_mic` aiohttp handler 在 **web 循环/另一线程**(`web_ui_agent.py:361-362` 两个独立 loop)。
- `force_exit()` 从 web 线程改多字段状态机,与 agent 线程 `observe_turn`/`capture` 竞争(`gate.muted` 是 GIL 原子布尔才安全,控制器不享受)。
- **做法**:`_handle_mic` 把"退出聆听+善后"打包,`loop.call_soon_threadsafe(...)` 投回 **agent 循环**;`gate.muted` 仍在 web 线程翻转。→ 控制器所有变更**单线程串行**。

### 5.8 与现有功能的关系
| 现有 | 影响 |
|---|---|
| 判停/funasr-stream/GAP/STT/VAD | 不动 |
| 关麦/MuteGate/挂起中 | 独立;通话键退出聆听时复用其静音 |
| KWS 打断 | 复用词表+回调;命令词保留强打断,其余落原逻辑 |
| 上下文/LLM | 聆听期不写入;整理用 `turn_ctx` 本轮注入,只留摘要 |
| 显示 | 聆听期 host gate:停 `_live`、不广播 assistant 气泡,改发 `listening` 横幅 |

---

## 6. 配置项(env,默认关)
| key | 默认 | 说明 |
|---|---|---|
| `XIAOGE_LISTEN_ENABLE` | 0 | 总开关 |
| `XIAOGE_LISTEN_COMMAND` | 小歌聆听模式\|小歌进入聆听模式\|聆听模式 | 进入命令词(KWS);`\|` 配多个,任一命中即进入 |
| `XIAOGE_LISTEN_WAKE` | 小歌干活了 | 退出命令词(KWS) |
| `XIAOGE_LISTEN_AUTO_ENABLE` | 1 | 自动进入(产品定默认开) |
| `XIAOGE_LISTEN_AUTO_TURNS` | 3 | 自动进入连续轮数 N |
| `XIAOGE_LISTEN_AUTO_MINCHARS` | 20 | 自动进入单轮最小字数 L |
| `XIAOGE_LISTEN_TEMP_TTL` | 120 | 临时内容定时丢弃 t(秒) |
| `XIAOGE_LISTEN_MIN_ORGANIZE_CHARS` | 15 | 退出后"主动问"的最小内容量 M(低于则静默丢) |
| `XIAOGE_LISTEN_ORGANIZE` | 0 | "问是否整理 + 整理动作"总开关;先关(定时删除独立保留),后续优化 |
| `XIAOGE_LISTEN_DRAIN` | 2.5 | 退出尾巴窗(秒):窗内定位唤醒词并切分(丢之前、留之后);见 §5.5 |
| `XIAOGE_LISTEN_ENTER_NOTICE` | 好,我先听着。需要我就说『小歌干活了』。 | 进入提示(空串=不出声) |

> **灰度观测(评审产品视角)**:默认开 + 4 个数值旋钮(N/L/M/t)意味着"误进 / 该问没问 / 不该问却问"的体感都依赖现场表现。灰度阶段把 **N/L/M/t 当作要观测、回收再定的对象**,不一次定死。注意 `L`(自动进入单轮长度=20)与 `M`(退出后值得问的 temp 总量=15)用途不同、别混。

---

## 7. host 接线落点清单(如实描述)
| 落点 | 改动 |
|---|---|
| 模块级 | `_listen_ctrl` global、`_listen_ttl_handle` global、`_listen_force_exit_on_agent_loop()`、`_arm/_cancel/_on_temp_ttl_expired()` |
| import | `from listening_mode import ListeningController, ListeningEvent`;`from dataclasses import replace` |
| 启动 | 构造 `_listen_ctrl`;`replace(_kws_config, keywords=_kws_config.keywords + _listen_ctrl.keywords)` |
| `_on_kws_hit` | 保留 `interrupt(force)`;`observe_keyword`→ENTERED(进入+say+UI+置回声)/EXITED(退出收尾+定时器/主动问+置回声)/NONE 原逻辑 |
| `on_user_turn_completed` | **最前**:①active→capture+吞;①b `_listen_tail_pending()`→`split_after_command` 切分(丢之前/留之后/兜底吞);②awaiting→答案判定/注入(organize 开);④observe_turn==ENTER→say(False)+进入+StopResponse |
| `_on_stt` | `if _listen_ctrl and _listen_ctrl.active: 跳过 _live.feed_*` |
| `transcription_node` | `if active: 不广播 assistant 气泡` |
| `_handle_mic` | `call_soon_threadsafe(_listen_force_exit_on_agent_loop)` |
| UI | 进入/退出 broadcast `listening`;前端全屏横幅(host gate) |
| `.env.example` | 登记 9 个 key |

> **G5 评估**:host 改动是"一个模块 global + 接线 + 定时器协程",与本仓 `_mute_gate`/`_switchable_stt`/`_speaker_gate` 同款;**判定逻辑全在 `listening_mode.py`**,未揉进其他功能文件 → G5(逻辑解耦)满足。

---

## 8. 边界与异常
- 总开关关:`enabled=False`,所有 `observe_*` 返回 NONE、尾巴窗永不开,host 全旁路,零影响。
- KWS 降级:命令词进入失效;自动进入+通话键退出仍可用。
- 聆听期收到停止词:聆听分支在 `on_user_turn_completed` **最前**,先于停止词链(否则"停/好了"被抢先 `interrupt`+StopResponse,绕过 capture)。
- 连说"小歌干活了+整理":§5.5 剥词后用剩余 → 直接整理(v3 已解决,不丢请求)。
- 整理时 LLM 失败:回退正常,temp 按 t 处理。

## 9. 已核实结论
**§9.1 `StopResponse` 足以让该轮不入 chat_ctx —— 已验证。** 证据:抛 `StopResponse`→`agent_activity.py:2068-2069` 直接 `return`,此前 `user_message` 未 append 进 chat_ctx(仅 `_closing` 分支 2055)。仓内先例 `_is_backchannel/_should_ignore_user_turn`→`raise StopResponse()`(`web_ui_agent.py:1001-1015`),同构、已在生产路径验证。

## 10. 验证计划
- **单测(控制器纯函数)**:observe_keyword 进/出/NONE;observe_turn 连续到 N/短噪声不重置/长未打断重置;capture;force_exit;`is_affirmative`;`temp_has_substance`;take/drop_temp;再入丢弃;`drain_s` 解析;`split_after_command`(纯退出/聚合后话/前缀/连说请求/夹标点/听岔小郭小哥/定位不到→None)。
- **集成自测**:尾巴窗内未定位唤醒词→吞且窗保持;定位到→切分关窗,之后真话正常显示+回复。
- **集成自测**:env 旁路;`replace` 词表合并;StopResponse 吞轮不回复;`turn_ctx.add_message` 只本轮可见;active 期不广播 `_live`/assistant 气泡。
- **手测**:命令词进出(含连说退出+整理)、自动进入、通话键退出+挂起、整理流程(有实质内容→问→肯定→注入摘要;内容少→静默丢)、定时丢弃、再入丢弃、UI 横幅、误进逃生。

## 11. 回退
`XIAOGE_LISTEN_ENABLE=0`(默认)= 功能完全旁路,与上线前一致。

## 12. 决策记录
| # | 决策 |
|---|---|
| 命令词检测 | KWS(`replace` 追加词表;保留强打断) |
| 退出尾巴 | **尾巴窗 + 按唤醒词切分**(`drain_s` 默认 2.5s + `split_after_command` 精确/模糊定位):丢唤醒词及之前的监听内容、**留之后接着说的真话并正常回复**;定位到即关窗(故快接的独立后话也留得住);定位不到则吞、窗保持。对 STT 听岔/聚合/前缀鲁棒 |
| 整理触发 | 方案A 主动问;**仅 temp ≥ M 时问**;受 `organize_enabled` 总开关(当前默认关) |
| 整理注入 | `turn_ctx.add_message` 本轮临时,禁 `update_chat_ctx`,只留摘要(G4) |
| 自动进入 | N=3/L=20;**默认开、不自动退出**;借一次 `say(...,False)` 提示;触发轮不入缓冲;前 N-1 轮不回收(已知代价) |
| 通话键 | 聆听期=退出+挂起;再按=恢复;复用 mic;退出补问在解除静音回正常时 |
| 并发 | force_exit `call_soon_threadsafe` 回 agent 循环,单线程串行 |
| 不进上下文 | StopResponse(已核实充分) |
| UI 冻结 | **host 侧 gate**:停 `_live`、不广播 assistant 气泡、发 `listening` 横幅(屏幕干净) |
| 解耦 | 逻辑全在 `listening_mode.py`;host 模块 global + 接线 + 定时器(本仓范式,G5 满足) |
