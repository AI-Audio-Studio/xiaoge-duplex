# 聆听模式设计评审意见

> 注:本文为三轮评审过程快照。部分早期结论/符号(如 strip_command_echo、pending_command_echo、"纯前端遮罩"方案)已被最终实现取代——退出尾巴改为"尾巴窗 + split_after_command 切分",UI 改为 host 侧 gate。**最终以 LISTENING_MODE_DESIGN.md(§5.5/§5.3)与代码为准。**

> 评审对象:`LISTENING_MODE_DESIGN.md`
> 评审方式:逐条核对 `examples/voice_agents/web_ui_agent.py` 与框架 `livekit-agents` 源码,所有结论附 `文件:行号` 证据。
> 评审范围:仅评审,不改任何现有文件。

---

## 总评

设计整体**成熟、可落地**:解耦思路(纯状态机 + host 薄接线)与现有 `MuteGate`/`KWS`/`online_interrupt` 等旁路模块的风格一致,默认关、可回退,符合 [testing-hard-constraint] 的"opt-in、不影响正常流程"原则。核心机制有**仓内现成先例背书**,不是空想。

但有 **5 处需要在评审阶段就纠正/补强**,其中 2 处是设计文档里写错的事实判断,1 处是被设计忽略的真实并发风险。下面按严重度排序。

---

## 一、好消息:§9.1 标称的"唯一不确定点"其实已被证伪——StopResponse 足够

设计 §9 把"`StopResponse` 是否足以让该用户轮不写入 chat_ctx"列为**唯一不确定点**并请评审组关注。**实际追源码可确认:足够,不需要额外移除消息。**

证据链(`livekit-agents/livekit/agents/voice/agent_activity.py`):
- `user_message` 在 [agent_activity.py:2005](livekit-agents/livekit/agents/voice/agent_activity.py:2005) 现场新建,此时**尚未进入** chat_ctx。
- `on_user_turn_completed` 在 [agent_activity.py:2065](livekit-agents/livekit/agents/voice/agent_activity.py:2065) 被调用;若抛 `StopResponse`,[agent_activity.py:2068-2069](livekit-agents/livekit/agents/voice/agent_activity.py:2068) 直接 `return`。
- 此后 `user_message` 既不会被 append 到 `self._agent._chat_ctx`,也不会传入 `_generate_reply`。唯一会写入 chat_ctx 的分支都受 `self._session._closing` / `info.skip_reply` 守卫([agent_activity.py:2054-2056](livekit-agents/livekit/agents/voice/agent_activity.py:2054)、[2088-2090](livekit-agents/livekit/agents/voice/agent_activity.py:2088)),正常聆听轮不会命中。

更强的佐证:**同一文件里已有现成先例**。`on_user_turn_completed` 里 `_is_backchannel` / `_should_ignore_user_turn` 命中后正是 `raise StopResponse()`,目的就是"附和/停止词不进上下文、不回复"([web_ui_agent.py:1001-1015](examples/voice_agents/web_ui_agent.py:1001))。聆听期的 `capture + StopResponse` 与之同构,机制已在生产路径验证。

**建议**:把 §9.1 从"唯一不确定点"降级为"已验证可行",删掉"可能需在回调内移除消息"的兜底设想——它不必要,反而会引入复杂度。

---

## 二、事实性错误:`session.say(NOTICE)` 并非"不写上下文",开场白也不是

设计在 §3、§5.2 第 2 步反复声称进入提示走 `session.say(NOTICE)`,"**不经 LLM、不写上下文(同开场白机制)**"。**后半句是错的。**

证据:
- `AgentSession.say` 签名默认 `add_to_chat_ctx: bool = True`([agent_session.py:1106-1113](livekit-agents/livekit/agents/voice/agent_session.py:1106))。
- 现有开场白调用 [web_ui_agent.py:1383](examples/voice_agents/web_ui_agent.py:1383) `session.say("你好呀…")` **没有传** `add_to_chat_ctx`,因此开场白**确实写进了 chat_ctx**。设计引用的"先例"本身就和它的论断相反。

影响:
1. 若按文档直接 `session.say(NOTICE)`,提示话术会**污染上下文**——恰好违背本功能 G4("内容不污染上下文")的初衷。
2. 修复很简单:**显式传 `session.say(NOTICE, add_to_chat_ctx=False)`**。但设计的推理依据("同开场白机制")是错的,需在文档更正,避免实现者照抄默认。
3. 附带问题:`say()` 的文本会经 `transcription_node` 广播成**assistant 气泡**([web_ui_agent.py:979-990](examples/voice_agents/web_ui_agent.py:979))。聆听期 UI 标称"冻结/变灰",但这条提示仍会作为正常助手气泡冒出来,需前端或 host 单独处理,否则视觉与"冻结"语义冲突。

---

## 三、被忽略的真实并发风险:控制器被两个事件循环/线程同时改

设计反复强调"纯状态机、可单测、零依赖",但**没有讨论控制器状态的跨线程访问**。实际上 host 的三个接线点不在同一线程:

- `_handle_mic`(通话键 → `ctrl.force_exit()`)是 **aiohttp handler,跑在 web server 线程/`_web_loop`** 上(server 在独立线程启动,[web_ui_agent.py:810-811](examples/voice_agents/web_ui_agent.py:810)、[790-792](examples/voice_agents/web_ui_agent.py:790))。
- `on_user_turn_completed`(capture/observe_turn)跑在 **agent 事件循环**。
- `_on_kws_hit` 经 `call_soon_threadsafe` 投递到 **agent 循环**([kws_interrupt.py:217](examples/voice_agents/kws_interrupt.py:217)),与 turn 回调同线程,这条没问题。

问题在 `force_exit()`:它从 **web 线程**改写控制器状态(state 枚举、temp 缓冲、awaiting 标志),而 `observe_turn`/`capture` 同时从 **agent 线程**读写同一批状态——**数据竞争**。现有 `_handle_mic` 之所以能从 web 线程改 `gate.muted` 不出事,是因为那只是一个 GIL 原子布尔([web_ui_agent.py:718-720](examples/voice_agents/web_ui_agent.py:718));聆听控制器是**多字段非原子状态机**,不享受同等保证。

**建议**:`force_exit()` 也用 `call_soon_threadsafe` 投递到 agent 循环执行(把 `_agent_loop` 暴露给 `_handle_mic`,仓里已有此 global,[web_ui_agent.py:362](examples/voice_agents/web_ui_agent.py:362)),让控制器的所有变更都在单一线程串行。"纯状态机"不等于"线程安全",这一点设计必须明确,否则会偶发状态错乱(例如退出与 capture 交叉)。

---

## 四、"host 薄接线"低估了 ctrl 的接入成本(影响 G5)

设计称 host 仅"几行薄接线"。但控制器实例 `ctrl` 需要同时被三个不同作用域访问:

- `_handle_mic`——**模块级函数**;
- `_on_kws_hit`——**entrypoint 内的闭包**;
- `on_user_turn_completed`——**`VoiceAgent` 的实例方法**,而 `VoiceAgent()` 在 [web_ui_agent.py:1240](examples/voice_agents/web_ui_agent.py:1240) 无参构造,拿不到 entrypoint 的局部变量。

因此 `ctrl` 实际上**必须做成模块级 global**(与 `_mute_gate`/`_switchable_stt` 同款),定时器句柄、`awaiting_organize_answer` 等可变状态也会横跨"Agent 方法"和"entrypoint 的 asyncio 定时器"两处。这能实现,但**不是"几行",也不像设计暗示的那样零散**——它会在本就偏长(1404 行)的 host 里再加一组全局可变状态。

**建议**:文档如实写成"新增一个模块级 `ctrl` global + host 定时器协程",别用"薄接线/几行"淡化;否则与 G5"不揉进其他功能文件"的承诺有出入。这是表述问题,不阻塞落地。

---

## 五、自动进入:默认开 + 触发轮被丢弃 + 信号易误判

§5.2 的自动进入判据(连续 N 轮 `user_spoke_over_agent==True` 且 `len>=L`)在工程上可行——`_overlap_turn_state["user_spoke_over_agent"]` 确实存在且语义正确(用户开口瞬间 agent 是否在说话,[web_ui_agent.py:1167-1168](examples/voice_agents/web_ui_agent.py:1167))。但有三点产品/逻辑隐患:

1. **信号本身区分不了"自言自语"与"正当打断"**。用户长句压过小歌,既可能是对旁人说话,也可能是嫌小歌啰嗦、在纠正/追加需求——后者恰恰是在跟小歌对话。N=3 能缓解,但在小歌话多的场景仍可能误进。
2. **误进的失败模式很差**:本功能**无自动退出**,一旦误进,用户(语音产品、未必看屏)对着小歌说话却只换来沉默,需知道命令词或按通话键才能逃出。进入提示话术 + UI 横幅能缓解,但**默认把 auto-enter 设为开**([config 表 `XIAOGE_LISTEN_AUTO_ENABLE=1`])对一个"主打不打扰"的功能偏激进。
3. **触发轮的内容被丢弃**:进入动作"就这一轮、之后全静默",该轮长文本既不回复也不 capture,静默吞掉。多为自说自话尚可接受,但应在文档显式写明"触发轮不入缓冲",免得实现者纠结。

**建议**:auto-enter 默认改 `0`(命令词/通话键作为一期主路径,自动进入作为灰度开关),或落实 §9.2 的"时间窗约束"再默认开。

---

## 六、若干较小但应在文档澄清的点

- **KWS 词表合并可行**,但要注意 `KwsConfig.from_env()` 在设了 `XIAOGE_KWS_KEYWORDS` 时会**整体覆盖**默认词表([kws_interrupt.py:92-99](examples/voice_agents/kws_interrupt.py:92))。聆听命令词必须**追加到已解析的 `_kws_config.keywords` 元组**,而不是只塞 env,否则用户一旦自定义 KWS 词表,聆听命令词就丢了。中文短语转拼音由 `_phrase_to_keyword_line` 自动完成([kws_interrupt.py:300-318](examples/voice_agents/kws_interrupt.py:300)),`on_hit` 回传的是原词串,`observe_keyword(kw)` 按原词精确匹配可行。✓
- **"每轮 ASR final → capture" 不完全成立**。早于 `on_user_turn_completed` 的 `_on_stt` 回调会对 overlap-ack 调 `session.clear_user_turn()`([web_ui_agent.py:1189-1190](examples/voice_agents/web_ui_agent.py:1189)),被清掉的轮不会到达 `on_user_turn_completed`,也就不会被 capture。聆听期的纯附和("嗯")会因此漏记——无伤大雅,但与文档措辞不符,建议注明。
- **聆听期 live 气泡仍在后端持续广播**(`_live`/`user_partial`,[web_ui_agent.py:1174-1183](examples/voice_agents/web_ui_agent.py:1174))。§5.3 说"不弹正常气泡"只能靠前端冻结遮罩遮住,底层 broadcast 不停。要么 host 在聆听期 gate 掉 `_live`,要么明确这是纯前端遮挡。
- **`standby.py` 无法在本仓核对**。设计引用 sibling 工程 `duplexMVP2/src/xiaoge/session_control/standby.py`,该文件不在本仓(已全仓搜索,无匹配),"sherpa KWS 命中稳定 / 实测过 `小歌干活了`"等结论无法在此验证,属外部依据,评审仅能采信文档自述。

---

## 结论与优先级

设计**方向正确、可落地**,核心顾虑(不进上下文)经源码验证反而比文档更乐观。落地前**必须处理**:

| 优先级 | 事项 |
|---|---|
| 必改 | §三 跨线程并发:`force_exit()` 投递回 agent 循环串行化 |
| 必改 | §二 `say(NOTICE, add_to_chat_ctx=False)`,并修正文档对"开场白机制"的错误引用 |
| 建议 | §五 auto-enter 默认关 / 加时间窗;触发轮丢弃写明 |
| 文档 | §一 §9.1 降级为已验证;§四 如实描述 ctrl global;§六 各点澄清 |

> 本评审仅核对了 host 接线面与框架契约,未对控制器内部算法做单测级推演——§10 的单测计划(纯函数控制器)合理,建议照执行。

---

# 附:实现级补充(应评审要求展开)

> 下列代码均为**示意骨架**,符号(`_agent_loop` / `_kws_config` / `StopResponse` / `broadcast` / `turn_ctx` 等)对齐现有 `web_ui_agent.py`,可直接据此细化。控制器保持纯同步、无 I/O;所有 asyncio/线程编排都在 host。

## A. 线程模型:把"谁在哪个线程改控制器"钉死(对应 §三,必改)

现状下三个接线点的线程归属(已核对):

| 接线点 | 运行线程 | 入口证据 |
|---|---|---|
| `_on_kws_hit` | **agent 循环**(经 `call_soon_threadsafe`) | [kws_interrupt.py:217](examples/voice_agents/kws_interrupt.py:217) |
| `on_user_turn_completed` | **agent 循环** | 框架回调 |
| `_handle_mic` | **web 循环(另一线程)** | aiohttp handler,server 独立线程 [web_ui_agent.py:810](examples/voice_agents/web_ui_agent.py:810) |

**目标:控制器状态只在 agent 循环这一个线程被读写。** KWS 和 turn 回调天然满足;唯一越界的是通话键。做法——`_handle_mic` 不直接碰 `ctrl`,而是把"退出聆听 + 善后"打包成一个 host 函数,投递回 agent 循环:

```python
# 模块级:把"退出聆听"的完整 host 副作用收口成一个函数,只在 agent 循环跑
def _listen_force_exit_on_agent_loop() -> None:
    if _listen_ctrl is None or not _listen_ctrl.active:
        return
    _listen_ctrl.force_exit()                 # 纯状态机:active->False,缓冲转 temp
    _arm_temp_ttl_timer()                      # asyncio 定时器(见 §E),此刻在 agent 循环,安全
    broadcast({"type": "listening", "on": False})
    # 通话键退出的"补问"按 §5.4 在解除静音回正常时再触发,这里不问

async def _handle_mic(request):
    ...
    gate.muted = not gate.muted               # 原有逻辑,web 线程改原子布尔,保持不动
    loop = _agent_loop
    if loop is not None and loop.is_running():
        loop.call_soon_threadsafe(_listen_force_exit_on_agent_loop)   # ← 唯一新增
    ...
```

要点:`gate.muted` 仍在 web 线程翻转(原子布尔,沿用现状无害);**控制器的多字段状态机改动全部 marshal 到 agent 循环**。force_exit 是 fire-and-forget,mic 切换不必等它,二者是独立状态,顺序无关。文档须把"纯状态机 ⇒ 可单测"和"线程安全"分开陈述——前者成立,后者靠 host 串行化保证。

## B. `ctrl` 全局接入 + 构造 + KWS 词表合并(对应 §四)

`ctrl` 必须是模块 global(与 `_mute_gate`/`_switchable_stt` 同款),否则 `VoiceAgent.on_user_turn_completed`(无参构造 [web_ui_agent.py:1240](examples/voice_agents/web_ui_agent.py:1240))拿不到它。

```python
# 模块级
_listen_ctrl: "ListeningController | None" = None
_listen_ttl_handle: asyncio.TimerHandle | None = None

# entrypoint 内,KWS 构造之前:
global _listen_ctrl
_listen_ctrl = ListeningController.from_environment()

# 词表合并:注意 from_env() 在设了 XIAOGE_KWS_KEYWORDS 时会"整体覆盖"默认词表,
# 所以要追加到"已解析"的元组上,而不是只设 env(见正文 §六)
_kws_config = KwsConfig.from_env()
if _listen_ctrl.enabled:
    _kws_config = replace(                      # dataclasses.replace
        _kws_config,
        keywords=_kws_config.keywords + _listen_ctrl.keywords,   # 去重可选
    )
```

`on_hit` 回传的是原词串(`get_result` 取 `@原词`,[kws_interrupt.py:230](examples/voice_agents/kws_interrupt.py:230)),故 `observe_keyword(kw)` 按原词精确匹配可行;debounce 800ms 对命令词足够。

## C. `on_user_turn_completed` 的插入顺序(关键,易踩坑)

现有链路顺序是:停止词 → 附和 → overlap-ack → 数字归一([web_ui_agent.py:997-1021](examples/voice_agents/web_ui_agent.py:997))。聆听相关分支**必须插在最前**,否则聆听期说"停/好了"会被既有停止词逻辑抢先 `interrupt(force=True)`+StopResponse,虽不致命但语义混乱、且绕过 capture。

```python
async def on_user_turn_completed(self, turn_ctx, new_message):
    ctrl = _listen_ctrl
    text = new_message.text_content

    # ① 聆听期:吞缓冲、不回复、不进上下文(StopResponse 已验证足够,见正文 §一)
    if ctrl is not None and ctrl.active:
        ctrl.capture(text)
        raise StopResponse()

    # ② 退出后"等整理回答":只认问话之后的下一轮
    if ctrl is not None and ctrl.awaiting_organize_answer:
        if ctrl.is_affirmative(text):                 # 小词集判定,纯函数
            transcript = ctrl.take_temp()             # 取出并清空 temp
            _cancel_temp_ttl_timer()                  # §E
            ctrl.clear_awaiting()
            # ★ 注入方式见 §D —— 编辑 turn_ctx(本轮临时),不要 update_chat_ctx
            turn_ctx.add_message(
                role="user",
                content=f"[聆听记录] 我刚才在聆听模式期间说了:{' '.join(transcript)}",
            )
            return            # 不抛 StopResponse → 正常生成"整理"回复
        else:
            ctrl.clear_awaiting()                     # 新话题:走正常,不动 temp(留 TTL 丢)
            # 落到下面原有逻辑

    # ③ 原有逻辑(停止词/附和/overlap-ack/数字归一)保持不动
    if _should_ignore_user_turn(text):
        ...
```

注意 §正文 §六提到的边界:早于本回调的 `_on_stt` 会对 overlap-ack 调 `clear_user_turn()`([web_ui_agent.py:1189](examples/voice_agents/web_ui_agent.py:1189)),那种轮根本不会进到①,所以"纯附和"在聆听期不会被 capture——可接受,但文档"每轮 final 都 capture"措辞要改成"每个**成轮**的 final"。

## D. 整理注入:用 `turn_ctx` 临时注入,**不要** `update_chat_ctx`(核对所得,重要)

框架把传给 `on_user_turn_completed` 的 `turn_ctx` 设计成**一次性可变副本**:`temp_mutable_chat_ctx = self._agent.chat_ctx.copy()`([agent_activity.py:2062](livekit-agents/livekit/agents/voice/agent_activity.py:2062)),注释明确"changes will not be kept inside Agent.chat_ctx"([agent_activity.py:2059-2061](livekit-agents/livekit/agents/voice/agent_activity.py:2059)),它只喂给本轮 `_generate_reply(chat_ctx=temp_mutable_chat_ctx)`([agent_activity.py:2131](livekit-agents/livekit/agents/voice/agent_activity.py:2131))。

这对本功能恰好是**理想行为**,值得在设计里写清:
- 往 `turn_ctx` 注入 `[聆听记录]…` → LLM **本轮**看得到、据此整理;
- 但这段原始自说自话**不会持久进历史**,持久进 chat_ctx 的只有用户那句"整理一下" + 小歌生成的**摘要**——正好实现 G4"原始内容不污染上下文,只留整理结果"。

因此**不应**调 `await self.update_chat_ctx(...)`(那是持久替换、async、还会和本轮框架的 ctx 管理交叉),也不需要手动把 transcript 写进 `self._chat_ctx`。`chat_ctx` 属性本身是只读视图(`_ReadOnlyChatContext`,[agent.py:156](livekit-agents/livekit/agents/voice/agent.py:156)),想硬写也写不进——这反过来印证"编辑 turn_ctx 副本"是框架给的正道。
唯一要接受的小瑕疵:摘要回复引用了历史里不存在的原文,但摘要自洽,可接受。

## E. 临时内容定时器生命周期(host asyncio,全在 agent 循环)

控制器不持有 asyncio;TTL 句柄放模块级,所有操作都在 agent 循环(§A 已保证 force_exit 也被 marshal 进来),无需加锁:

```python
def _arm_temp_ttl_timer() -> None:                 # 退出聆听时
    _cancel_temp_ttl_timer()
    loop = _agent_loop
    globals()['_listen_ttl_handle'] = loop.call_later(
        _listen_ctrl.temp_ttl_s, _on_temp_ttl_expired
    )

def _cancel_temp_ttl_timer() -> None:              # 整理成功 / 再次进入聆听 时
    h = _listen_ttl_handle
    if h is not None:
        h.cancel()
        globals()['_listen_ttl_handle'] = None

def _on_temp_ttl_expired() -> None:                # 到点
    if _listen_ctrl is not None:
        _listen_ctrl.drop_temp()                   # 留日志
    globals()['_listen_ttl_handle'] = None
```

四条状态转移对应:退出→`_arm`;整理肯定→`_cancel`+`take_temp`;再次进入聆听(t 未到)→`_cancel`+`drop_temp`(§5.4 第 6 点);到点→`_on_temp_ttl_expired`。`call_later` 必须在 agent 循环线程注册——§A 已确保 force_exit 路径也在该线程,故 KWS 退出与通话键退出走同一套,无分叉。

## F. 进入提示与 UI 一致性(对应 §二)

```python
notice = _listen_ctrl.enter_notice            # XIAOGE_LISTEN_ENTER_NOTICE,空串=不出声
if notice:
    session.say(notice, add_to_chat_ctx=False)   # ← 必须显式 False,默认 True 会写历史
broadcast({"type": "listening", "on": True, "hint": "说『小歌干活了』或点通话键退出"})
raise StopResponse()                             # 抑制触发轮回复(该轮文本不 capture,见正文 §五)
```

附带:`say()` 文本会经 `transcription_node` 广播成 **assistant 气泡**([web_ui_agent.py:987-990](examples/voice_agents/web_ui_agent.py:987)),且聆听期 `_live`/`user_partial` 仍在持续广播([web_ui_agent.py:1174-1183](examples/voice_agents/web_ui_agent.py:1174))。前端"冻结遮罩"是覆盖层,底层 broadcast 不停。两种收口任选其一,需在设计里点名:
1. **纯前端**:收到 `listening:on` 后,遮罩层之上仍渲染但整体置灰/锁滚动(改动小,底层消息照走);
2. **host 侧 gate**:聆听期不再调 `_live.feed_*`、并把进入提示用单独 `{type:"listening_notice"}` 而非 assistant 气泡(更干净,但要动 host 与前端两处)。

建议一期用方案 1,与"最小改动"基调一致。

## G. 控制器字段建议(便于 §10 单测,纯函数)

```python
@dataclass
class ListeningController:
    enabled: bool
    active: bool = False
    awaiting_organize_answer: bool = False
    _buffer: list[str] = field(default_factory=list)   # 聆听期工作缓冲
    temp_transcript: list[str] = field(default_factory=list)  # 退出后待整理
    _auto_count: int = 0                               # 连续自说自话计数
    command_keyword: str = "小歌聆听模式"
    wake_keyword: str = "小歌干活了"
    temp_ttl_s: float = 120.0
    auto_turns: int = 3
    auto_min_chars: int = 20
    enter_notice: str = "好,我先听着。需要我就说『小歌干活了』。"
```

单测可全程不碰 asyncio:`observe_keyword`/`observe_turn`(计数到 N、清零)/`capture`/`force_exit`/`take_temp`/`drop_temp`/`is_affirmative`/再入丢弃,均为纯状态转移断言——这也正是把 asyncio、`say`、定时器、broadcast 全部留在 host 的回报。

---

## 补充优先级小结

| 优先级 | 事项 | 对应节 |
|---|---|---|
| 必改 | force_exit 经 `call_soon_threadsafe` 回 agent 循环,控制器单线程串行 | A |
| 必改 | 整理注入用 `turn_ctx.add_message`(本轮临时),禁用 `update_chat_ctx` | D |
| 必改 | 聆听分支插在 `on_user_turn_completed` **最前**,先于停止词链 | C |
| 必改 | `say(notice, add_to_chat_ctx=False)` | F |
| 建议 | KWS 词表用 `replace()` 追加到已解析元组 | B |
| 建议 | TTL 定时器四态转移收口为 `_arm/_cancel/_expired` | E |
| 建议 | 聆听期 UI 一期走"纯前端遮罩" | F |

---

# 第二轮复评(对 `LISTENING_MODE_DESIGN.md v2`)

## 总评:v2 可以进入实现,第一轮意见已逐条吸收且引用属实

把 v1 的四条必改、三条建议对照 v2 复核,全部落实到位,且新增的源码引用我都验过——`say` 默认写上下文([agent_session.py:1112](livekit-agents/livekit/agents/voice/agent_session.py:1112))、`say` 经 `transcription_node` 变 assistant 气泡([agent_activity.py:2397](livekit-agents/livekit/agents/voice/agent_activity.py:2397))、`turn_ctx` 一次性副本([agent_activity.py:2062](livekit-agents/livekit/agents/voice/agent_activity.py:2062))、只读 chat_ctx([agent.py:156](livekit-agents/livekit/agents/voice/agent.py:156))、KWS 词表覆盖语义([kws_interrupt.py:92-99](examples/voice_agents/kws_interrupt.py:92))、并发线程归属——**均无虚标**。G4/G5 的论证现在是站得住的。文档质量明显提升,可以作为实现依据。

但复评时发现**一个双方此前都漏掉的新问题**,属逻辑正确性,建议下一轮先解决再开工。

## 新发现(必须带给研发):命令词的 ASR 回声会冲掉聆听分支

**根因**:KWS 是**旁路 tap**(`KwsTapAudioInput` 并行喂,音频照样流向 STT),所以用户说"小歌聆听模式 / 小歌干活了"时,**KWS 命中**与**STT 把这句转成一个正常用户轮**是**两条都会发生**的路径。而 v2 这两个命令词是聆听控制器独有的新词,既不在 `_STOP_WORDS` 也不在任何现有过滤里——它们的 ASR 文本会原样进 `on_user_turn_completed`。

**后果分两处,退出侧是真 bug:**

1. **进入侧(轻微)**:`observe_keyword`→ENTERED 置 `active=True` 后,"小歌聆听模式"这句的 ASR final 随即到达,命中分支①被 `capture` 进缓冲——缓冲里平白多出一条命令词。仅脏数据,可接受。

2. **退出侧(逻辑错误)**:`observe_keyword`→EXITED 置 `active=False` 并设 `awaiting_organize_answer=True`、抛出"主动问"。紧接着"小歌干活了"这句的 ASR final 到达,此时 `active=False`、`awaiting=True` → 命中分支②,被当成**"问话之后第一轮回答"**。`is_affirmative("小歌干活了")` 为假 → `clear_awaiting`,整理问被这句命令词回声**自己消费掉**。结果:用户真正的回答("好啊,整理一下")到来时 `awaiting` 已清,被当普通轮处理,**temp 不注入、摘要不发生**,整理流程哑火。设计 §5.4"只认下一轮回答"的前提("下一轮"是用户的真实回答)在 KWS 退出路径上不成立。

**注**:通话键退出路径**没有**这个问题(通话键是静默的,`awaiting` 在解除静音后才置,下一轮自然是用户真实回答)。所以是 KWS-退出 特有。

**修法方向**(留给研发,不替其定方案):host 需吞掉命令词自身的那一个 ASR 回声。最小做法是 `observe_keyword` 返回 ENTERED/EXITED 时,host 记一个"忽略下一条等于该命令词文本的 final"的一次性标志,在 `on_user_turn_completed` 最前(分支①之前)消费;或在分支② 先判定 `text == wake_keyword` 则跳过、不算作整理回答。这与现有 KWS 停止词"双路径"是同一类问题(停止词在 STT 侧由 `_should_ignore_user_turn` 兜),只是新命令词缺了对应的 STT 侧兜底。

## 两个小注(非阻塞,知会即可)

- **自动进入的前 N-1 轮不会被追溯清理**:要连续 N 轮才判定进入,意味着第 1…N-1 轮的自说自话此前已正常回复、已进上下文。进入只对第 N 轮起静默。这是"需要连续 N 轮才能检测"的固有代价,产品默认开时应知晓——历史里仍会留下前两轮的自说自话与小歌的应答。
- **`_on_kws_hit` 顶部现有 `session.interrupt(force=True)`**([web_ui_agent.py:1299](examples/voice_agents/web_ui_agent.py:1299)):v2 §7 写"NONE 落原打断逻辑",但进入/退出命令词是否也要保留这次强打断(打断小歌正在说的话)需明确——通常进入聆听时希望打断,退出时也无妨,建议显式写清而非留白。

## 复评结论

| 项 | 结论 |
|---|---|
| v1 意见吸收 | 全部到位,引用属实 |
| 可否实现 | 可以;建议先消化下方一条 |
| 新增必解 | **命令词 ASR 回声**:退出侧会哑掉整理流程,需 host 吞掉命令词自身那一轮 |
| 小注 | 前 N-1 轮不追溯清理;命令词是否保留强打断需写清 |

> 仍未做控制器内部算法的单测级推演;§10 计划合理。这条新问题本质在 host 接线面(命令词双路径),不影响"控制器纯状态机可单测"的结论。

---

# 第三轮复评(对 `LISTENING_MODE_DESIGN.md v3`)

## 总评:可以开工实现。三轮收敛到位,无遗留阻塞问题

第二轮提的"命令词 ASR 回声"已用 §5.5 的**内容感知剥词**正确闭环;并新增三处体验优化(回声内容感知 / 主动问设内容门槛 / UI 改 host 侧 gate)。我把 v3 的新机制按源码逐条推演,**未发现新的正确性问题**。这一版可作为实现基线。

## 对 v3 三处新机制的核验

**1. §5.5 命令词剥词(`strip_command_echo`)——逻辑自洽,且对"回声轮是否真出现"都鲁棒。** 我把三条路径都走了一遍:
- 纯回声"小歌干活了" → `rem==""` → 吞掉、不动 `awaiting`,真答留给下一轮 ✓
- 连说"小歌干活了 顺便整理一下" → `rem="顺便整理一下"` → 进②被判定为整理回答 ✓
- **关键鲁棒性**:若 STT 没把命令词单独成轮、下一轮直接是真答"好啊" → 一次性标志被 disarm,剥词无匹配则原样返回(`rem` 非空)→ 仍进②正常判定 ✓。即"回声轮没来"也不会卡死整理流程——这是比 duplexMVP2 盲吞更稳的地方,设计抓对了。

**2. §5.3 UI 用 host gate——方向更干净,但实现时要守住一条边界。** `transcription_node` 在 `active` 时跳过广播是对的(进入提示不再变 assistant 气泡)。**务必只 gate 广播、不要 gate `tts_node`**:进入提示的"出声"走的是独立的 `tts_node`([web_ui_agent.py:992-994](examples/voice_agents/web_ui_agent.py:992)),与 `transcription_node` 的广播是两条路;只要不误伤 `tts_node`,提示照样能说出来、只是不显示气泡。同理 §5.3 只 gate `_on_stt` 里的 `_live.feed_*`,不要动同函数内的停止词/`clear_user_turn` 早处理——v3 §7 表述已是这个精度,保持即可。

**3. §5.4 主动问设 `min_organize_chars` 门槛——合理。** 注意现在有两个相近阈值各司其职:`L`(=20,自动进入的单轮长度)与 `M`(=15,退出后是否值得问的 temp 总量),用途不同不冲突,但实现/调参时别混。

## 三个小注(非阻塞,知会即可)

- **剥词的一处固有边界(可接受,建议在 §5.5 加一句话点明)**:`pending_command_echo` 在 KWS 命中后**只 arm 一轮**,用"归一化 + 包含/前缀"剥词。绝大多数情况下一轮就是回声,没问题。唯一会"显意外"的是命令词嵌在一句更长、且面向旁人的话里——如"小歌干活了我们继续开会吧":剥词后 `rem="我们继续开会吧"` 会被当作退出后的内容/回答处理。鉴于用户此刻确已退出聆听、小歌恢复应答属预期,这属可接受副作用,但值得在文档写明"剩余非空即按内容走",免得实现者误以为还能进一步分辨"这句是不是说给小歌的"。
- **引用微瑕(不影响结论)**:§5.5 标的 `kws_interrupt.py:247` 实际旁路透传在 `KwsTapAudioInput.__anext__`([kws_interrupt.py:257-260](examples/voice_agents/kws_interrupt.py:257)),247 是该类 docstring。结论无误,行号顺手修一下即可。
- **调参面在变大(产品视角提醒)**:到 v3 已有 9 个 env、4 个数值旋钮(N/L/M/t)+ 默认开。功能本身没问题,但默认开 + 多旋钮意味着上线后"误进/该问没问/不该问却问"的体感都依赖这组默认值的现场表现,建议灰度阶段就把这 4 个值当作要观测/回收的对象,而非一次定死。

## 复评结论

| 项 | 结论 |
|---|---|
| v2 命令词回声 bug | 已正确闭环(§5.5 内容感知剥词) |
| v3 三处体验优化 | 方向对、逻辑自洽,均经源码核验 |
| 新增正确性问题 | **无** |
| 实现必守边界 | UI gate 只动广播/`_live`,勿伤 `tts_node` 与 `_on_stt` 早处理 |
| 小注 | 剥词一轮固有边界写明;行号微修;灰度观测 N/L/M/t |
| 可否开工 | **可以** |

> 三轮评审聚焦在 host 接线面与框架契约,已收敛。`listening_mode.py` 控制器的内部算法以 §10 单测覆盖即可;落地后建议补一条端到端手测:KWS 退出 + 连说整理("小歌干活了,帮我整理下")走通注入摘要——这是改动最密集、回归风险最高的一条路径。
