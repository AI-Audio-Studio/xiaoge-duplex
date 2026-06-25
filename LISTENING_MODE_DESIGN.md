# 聆听模式(Listening Mode)设计文档 · v3

| 项 | 内容 |
|---|---|
| 功能 | 聆听模式 —— 小歌"只听不插":ASR 照常工作,但临时缓冲、不进上下文、不回复 |
| 参考 | sibling 工程 `duplexMVP2` 的 standby(**仅参考,非标杆**;按"更好体验"取舍) |
| 状态 | **v3 待复评**(纳入第一、二轮评审 + 体验优化,逐条经源码核实) |
| 范围 | 仅 `examples/voice_agents/`;新增独立模块 `listening_mode.py` + host 接线;**不改判停/STT/上游** |

> **v3 相对 v2 的变更**(三处"比照搬 duplexMVP2 更好"的体验优化):
> 1. **命令词回声处理:内容感知**(§5.5)——不再无条件吞掉命令词后那条 final(duplexMVP2 做法,会把"小歌干活了,顺便整理一下"整条丢掉);改为**剥掉命令词、用剩余**:空=纯回声吞掉、非空=当内容/回答。连说一句就能用。
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
          │   ⓪ 命令词回声(内容感知,§5.5):剥词→空则吞、非空则用剩余继续                 │
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
- `from_environment()`;属性 `enabled/active/awaiting_organize_answer/pending_command_echo/keywords/command_keyword/wake_keyword/temp_ttl_s/enter_notice/min_organize_chars/temp_transcript`。
- `observe_keyword(kw) -> Event{NONE,ENTERED,EXITED}`(命中即置 `pending_command_echo=kw`)。
- `observe_turn(text, interrupted_agent) -> Auto{NONE,ENTER}`(仅未聆听时计数)。
- `capture(text)`;`force_exit() -> bool`(只在 agent 循环调,§5.7)。
- `strip_command_echo(text) -> str|None`:若 `pending_command_echo` 置位则 disarm 并剥词,返回剩余(可能空串);否则返回 None(非回声轮)。
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
- 进入 `小歌聆听模式`、退出 `小歌干活了`。`on_hit` 回传原词串,`observe_keyword` 按归一化匹配。
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
- **只在有实质内容时才走"主动问"**:`temp_has_substance()`(`≥ min_organize_chars`)为真 → 启动定时器 t、置 `awaiting_organize_answer`、`session.say("刚才听的我先存着了,要整理一下吗?", add_to_chat_ctx=False)`;否则 → **静默退出 + `drop_temp()`**(不问、不留)。
- **主动问的回答(承内容感知 §5.5)**:只看问话之后第一轮(命令词回声已被 ⓪ 处理)。`is_affirmative(text)` 为真 → `take_temp()` → `turn_ctx.add_message(role="user", content="[聆听记录] 我刚才在聆听模式期间说了:…")` → **不抛 StopResponse**(LLM 本轮据此整理)→ `_cancel` 定时器、`clear_awaiting`;否则 → `clear_awaiting`、落正常逻辑,temp 留到 t 丢。
- **为什么 `turn_ctx.add_message` 而非 `update_chat_ctx`**:`turn_ctx` 是一次性副本 `chat_ctx.copy()`(证据 `agent_activity.py:2062`,注释"changes will not be kept"),只喂本轮 `_generate_reply`(2131)→ **原始内容只本轮可见、不持久**,历史只留"整理一下"+摘要,**正好 G4**。`chat_ctx` 是只读视图(`agent.py:156`)。
- **定时丢弃**:t 内无整理 → `drop_temp()`(留日志)。**再次进入(t 未到)**:**立即 `drop_temp()` + 取消定时器**,开新缓冲。

### 5.5 命令词回声处理(内容感知,跨进入/退出)—— v3 体验优化
**问题**(评审第二轮,经源码核实):KWS 是旁路 tap(`kws_interrupt.py:257-260` `__anext__` 每帧原样返回下游)→ 说"小歌干活了"既触发 KWS、又被 STT 转成一个正常用户轮;命令词不在任何现有过滤里,其 ASR 终稿会进 `on_user_turn_completed`。退出侧会被分支②当成"整理回答"消费掉 → 整理哑火(真 bug)。

**做法(内容感知,优于 duplexMVP2 的盲吞)**:
- `observe_keyword`→ENTERED/EXITED 时置一次性 `pending_command_echo = 该命令词`。
- `on_user_turn_completed` **最顶部(分支①②③之前)**:`rem = ctrl.strip_command_echo(text)`(置位则 disarm + 归一化剥掉命令词,返回剩余;否则返回 None)。
  - `rem is None`(非回声轮)→ 照常往下。
  - `rem == ""`(纯回声"小歌干活了")→ **吞掉、`return`**,**不动 active/awaiting**(状态留给真答)。
  - `rem != ""`(连说"小歌干活了 顺便整理一下")→ **用 `rem` 替换本轮文本继续往下**:active→capture(rem);awaiting→把 rem 当回答(`is_affirmative("顺便整理一下")`=真→整理)。
- 匹配用**归一化(去标点/空白)+ 包含/前缀**,容忍 STT 把命令词转得略有出入。
- 自动进入无命令语,不置标志。
- **固有边界(接受)**:剩余非空即按内容走,**不再细分"这句是不是说给小歌的"**。极端如"小歌干活了我们继续开会吧"→剩余"我们继续开会吧"按内容/回答处理——此时用户已退出聆听、小歌恢复应答属预期,可接受。
- **鲁棒性**:`pending_command_echo` 只 arm 一轮;若回声轮没出现、下一轮直接是真答"好啊",剥词无匹配则原样返回(非空)→ 仍正常进②判定,不卡死(比盲吞稳)。
- 效果:退出哑火修复;连说一句即生效;进入侧命令词不污染缓冲。

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
| `XIAOGE_LISTEN_COMMAND` | 小歌聆听模式 | 进入命令词(KWS) |
| `XIAOGE_LISTEN_WAKE` | 小歌干活了 | 退出命令词(KWS) |
| `XIAOGE_LISTEN_AUTO_ENABLE` | 1 | 自动进入(产品定默认开) |
| `XIAOGE_LISTEN_AUTO_TURNS` | 3 | 自动进入连续轮数 N |
| `XIAOGE_LISTEN_AUTO_MINCHARS` | 20 | 自动进入单轮最小字数 L |
| `XIAOGE_LISTEN_TEMP_TTL` | 120 | 临时内容定时丢弃 t(秒) |
| `XIAOGE_LISTEN_MIN_ORGANIZE_CHARS` | 15 | 退出后"主动问"的最小内容量 M(低于则静默丢) |
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
| `on_user_turn_completed` | **最前**:⓪ `strip_command_echo`(空吞/非空用剩余);①active→capture+StopResponse;②awaiting→答案判定/注入;③observe_turn==ENTER→say(False)+进入+StopResponse |
| `_on_stt` | `if _listen_ctrl and _listen_ctrl.active: 跳过 _live.feed_*` |
| `transcription_node` | `if active: 不广播 assistant 气泡` |
| `_handle_mic` | `call_soon_threadsafe(_listen_force_exit_on_agent_loop)` |
| UI | 进入/退出 broadcast `listening`;前端全屏横幅(host gate) |
| `.env.example` | 登记 9 个 key |

> **G5 评估**:host 改动是"一个模块 global + 接线 + 定时器协程",与本仓 `_mute_gate`/`_switchable_stt`/`_speaker_gate` 同款;**判定逻辑全在 `listening_mode.py`**,未揉进其他功能文件 → G5(逻辑解耦)满足。

---

## 8. 边界与异常
- 总开关关:`enabled=False`,所有 `observe_*`/`strip_command_echo` 返回 NONE,host 全旁路,零影响。
- KWS 降级:命令词进入失效;自动进入+通话键退出仍可用。
- 聆听期收到停止词:聆听分支在 `on_user_turn_completed` **最前**,先于停止词链(否则"停/好了"被抢先 `interrupt`+StopResponse,绕过 capture)。
- 连说"小歌干活了+整理":§5.5 剥词后用剩余 → 直接整理(v3 已解决,不丢请求)。
- 整理时 LLM 失败:回退正常,temp 按 t 处理。

## 9. 已核实结论
**§9.1 `StopResponse` 足以让该轮不入 chat_ctx —— 已验证。** 证据:抛 `StopResponse`→`agent_activity.py:2068-2069` 直接 `return`,此前 `user_message` 未 append 进 chat_ctx(仅 `_closing` 分支 2055)。仓内先例 `_is_backchannel/_should_ignore_user_turn`→`raise StopResponse()`(`web_ui_agent.py:1001-1015`),同构、已在生产路径验证。

## 10. 验证计划
- **单测(控制器纯函数)**:observe_keyword 进/出/NONE+置回声;observe_turn 连续到 N/清零;capture;force_exit;`strip_command_echo`(空/非空/非回声);`is_affirmative`;`temp_has_substance`;take/drop_temp;再入丢弃。
- **集成自测**:env 旁路;`replace` 词表合并;StopResponse 吞轮不回复;`turn_ctx.add_message` 只本轮可见;active 期不广播 `_live`/assistant 气泡。
- **手测**:命令词进出(含连说退出+整理)、自动进入、通话键退出+挂起、整理流程(有实质内容→问→肯定→注入摘要;内容少→静默丢)、定时丢弃、再入丢弃、UI 横幅、误进逃生。

## 11. 回退
`XIAOGE_LISTEN_ENABLE=0`(默认)= 功能完全旁路,与上线前一致。

## 12. 决策记录
| # | 决策 |
|---|---|
| 命令词检测 | KWS(`replace` 追加词表;保留强打断) |
| 命令词回声 | **内容感知**:剥词,空吞/非空用剩余(优于盲吞,连说可用) |
| 整理触发 | 方案A 主动问;**仅 temp ≥ M 时问**,否则静默丢;只认下一轮(回声已 ⓪ 处理) |
| 整理注入 | `turn_ctx.add_message` 本轮临时,禁 `update_chat_ctx`,只留摘要(G4) |
| 自动进入 | N=3/L=20;**默认开、不自动退出**;借一次 `say(...,False)` 提示;触发轮不入缓冲;前 N-1 轮不回收(已知代价) |
| 通话键 | 聆听期=退出+挂起;再按=恢复;复用 mic;退出补问在解除静音回正常时 |
| 并发 | force_exit `call_soon_threadsafe` 回 agent 循环,单线程串行 |
| 不进上下文 | StopResponse(已核实充分) |
| UI 冻结 | **host 侧 gate**:停 `_live`、不广播 assistant 气泡、发 `listening` 横幅(屏幕干净) |
| 解耦 | 逻辑全在 `listening_mode.py`;host 模块 global + 接线 + 定时器(本仓范式,G5 满足) |
