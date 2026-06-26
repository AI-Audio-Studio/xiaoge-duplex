# 评审反馈：`TURN_STT_DESIGN.md`(流式主 STT + 判停优化)

> 注:文中 `文件:行号` 为撰写时快照,代码已演进,**以符号名/当前代码为准**。
> 注:本文为评审过程快照(分轮次),部分结论/行号已被最终实现取代,**最终以 TURN_STT_DESIGN.md 与代码为准**。

| 项 | 内容 |
|---|---|
| 评审对象 | `TURN_STT_DESIGN.md`(待评审版,2026-06-24) |
| 评审方式 | 逐条核对设计声明与上游/工程代码,带 `file:line` 证据;不臆测 |
| 评审范围 | 仅评审,未改动任何文件 |
| 结论 | **可进入实现,但有 3 个必须先解决/降级的点(见第六节)** |

---

## 总评

方案整体扎实、方向正确,纪律性强(零改上游、可开关、可回退)。我把关键技术声明逐条对着代码核了一遍,**大部分声明属实且引用准确**。根因分析也站得住脚。

但存在:
- **一个被低估的硬伤** —— felt 延迟预算(G4 ≤1.5s 与 G1 不切分直接冲突);
- **一个文档没点透的现存 bug** —— 流式模式下"麦克风静音"已经失效;
- 以及几个开放问题,我有明确倾向(见第四节)。

下面有理有据地展开。

---

## 一、核对属实的关键声明(给予肯定)

| 设计声明 | 核对结果 |
|---|---|
| 离线 STT 空结果被 `StreamAdapter` 静默丢弃 | ✅ 属实,见 `stream_adapter.py:129-132`(`len==0` 或 `not text` 直接 `continue`)。文中引用行号 131 略偏,逻辑无误。 |
| "无 FINAL 时 `_audio_transcript` 为空,EOU 早返回、不提交" | ✅ 属实,见 `audio_recognition.py:1125`。INTERIM 只写 `_audio_interim_transcript`(`:1030`),FINAL 才 `+=` 到 `_audio_transcript`(`:911`)。 |
| "续话取消靠 AgentSession 独立 VAD,与换不换主 STT 无关" | ✅ 属实。VAD `START_OF_SPEECH` 取消 pending EOU 任务,见 `audio_recognition.py:1084-1085`。前提(仍传 `vad=`)在 `web_ui_agent.py:1003` 已满足。 |
| "续话合并 = 累加同一轮" | ✅ 属实。`_audio_transcript` 仅在 commit 时清空(`:1291`),取消提交不清空,下条 FINAL 继续 `+=`。 |
| `RecognizeStream(sample_rate=16000)` 基类自动重采样 | ✅ 属实,见 `stt.py:462-471`。 |
| 主 STT 与上游 VAD 读同一路音频 | ✅ 属实。两者都由 `push_audio` 同帧扇出(stt_pipeline `:569` / vad_ch `:572`)。"两处 VAD 读同一路麦克风"成立。 |

**根因分析站得住脚。** Phase 3 实测"eot 顶在 min_delay≈460ms、没走 max_delay → turn detector 把碎片判成完整句 → 立即提交",正是过度切分真因。GAP 方案绕开"语义判断是否完整"这一不可靠环节,用声学静默直接决定轮边界,**比调 `unlikely_threshold` 更鲁棒**。这个取舍是对的。

> **表述瑕疵(建议修正)**:§4.1/§5.4 说上游"逐 FINAL 才提交"。更准确的说法是——`turn_detection` 传的是 `MultilingualModel` 实例而非字符串,所以 `_vad_base_turn_detection=True`(`:163`),**VAD `END_OF_SPEECH` 也会触发 `_run_eou_detection`**(`:1107`),只是因 transcript 为空而早返回。结论一致,但机制不是"只有 FINAL 触发"。文中引了 `:1125` 说明作者是懂的,建议把文字改精确,免得后人误解。

---

## 二、最重要的 concern:felt 延迟预算(G4 与 G1 直接冲突)

这是头号风险。**§8 一笔带过、§2 的"≤1.5s 红线"过于乐观。**

按 §5.3 伪代码,GAP 从 **VAD `END_OF_SPEECH` 事件时刻**(`last_voice_end`)起算。而 VAD 的 EOS 本身已包含 `vad_min_silence`(默认 0.35s,Phase 3 调到 0.8s)。EOU 提交前还要再睡 `endpointing_delay`(`audio_recognition.py:1213`)。所以一轮真实链路是:

```
felt ≈ vad_min_silence + GAP + endpointing_min_delay + EOU推理 + LLM_TTFT + TTS_TTFB
     ≈   0.35~0.8     + 1.2~1.5 +      0.3        +  ……
     ≈ 1.85s 起步(还没算模型/网络)
```

**GAP=1.2 + min_delay=0.3 已等于 1.5s,给 VAD 静默、EOU、LLM、TTS 留的预算是 0。** Phase 1 注入实测 felt 中位已是 1472–1603ms(在没有 GAP 的离线链路上),叠加 GAP 后几乎必然突破 1.5s。

**这不是调参能解决的,是结构性张力**:GAP 要够大才能合并自然停顿(G1 不切分),但 GAP 直接进 felt(G4)。建议:

1. **GAP 从"真实最后一帧有声"起算,而非 VAD EOS 事件时刻**;并把主 STT 内置 VAD 的 `min_silence` 压到接近 0,让 GAP 独吞静默等待,避免与 `vad_min_silence` 叠加。
2. 把 `endpoint_min_delay` 在 optimized 下降到 ~0(GAP 已是主边界,min_delay 的耐心是冗余的)。
3. **把 G4 从"红线"降级为"目标,LIVE 实测取舍"**,并在文档明确写出"G4 与 G1 存在固有 trade-off,GAP↑则 felt↑"。现有"守 ≤1.5s 红线"措辞,落地大概率打脸。

---

## 三、文档没点透的现存 bug:流式模式下"麦克风静音"失效

> (已实现修复:关麦主机制改为 MuteGate——输入链路最内层静音门,关麦时全链路收静音帧 + 录音暂停;不再依赖 `_switchable_stt`。下文为评审当时的现状分析。)

§8 和 §12.5 把 mute 列为"开放问题",**但其实现状已经是坏的**,值得升级措辞。

- 静音的唯一实现在 `SwitchableSTT._recognize_impl`(muted 时返回空,`web_ui_agent.py:249-253`)。
- 但流式分支里 `_switchable_stt = None`(iflytek 路径 `:980`;funasr-stream 同理)。
- 面板点静音 → `_handle_mic` 取 `stt = _switchable_stt`(`:662`)→ `None` → 返回 503 "agent not ready"。

**即:optimized/流式模式下,静音按钮直接不工作**,连带 `_test_recorder.set_paused` 的暂停录音耦合(`:667`)也失效。这不是"语义待定",是功能缺失。实现阶段必须显式补:在输入层 gate 帧、或在自研 STT 上挂 `muted` 标志。建议在 §11 影响文件里直接列为必做项,而非开放问题。

---

## 四、对 7 个开放问题的明确意见

**Q1(输出门控 vs 输入门控)→ 选输出门控(你们的主选),但注意 offline 校正。**
输出门控对 2pass 协议更安全:静音不送会触发 FunASR 空闲超时/重连,且 `offline` 段尾校正可能覆盖跨"静音-有声"边界的整段文本,输入门控会把校正打碎。唯一风险是噪声触发 VAD 误判语音(§8 已列)。建议输出门控 + 对被门控丢弃的文本打 KPI 计数,便于观测幽灵率。

**Q2(两处 VAD 是否合一)→ 不合一,但请把理由写进文档。**
这恰恰是 Q2 的答案:两处 VAD 需要**不同的静默阈值**——上游 VAD 要"短"(barge-in/打断要灵敏),自研 STT 的 GAP 要"长"(合并停顿)。合一就得二选一,必然牺牲一头。这是双 VAD 复杂度**最强的正当理由**,目前文档没说透。
顺带:`live_transcript.py` 已有 `new_turn_gap` 的同类逻辑驱动 live 气泡——GAP 概念在工程里已被验证可用,可作佐证;但也意味着会有两个 GAP 计时器,注意别打架。

**Q3(轮边界放自研 STT vs 调上游 turn_detection)→ 认同放自研 STT。** 理由见第一节:上游靠 EOU 语义判断,实测不可靠;GAP 声学判断更稳、且零改上游。

**Q3 的延伸隐忧(建议补充论证)**:有人会问"那为什么不直接把 `vad_min_silence` 调到 1.2s 就好?"。答案是:(a) 长段离线丢字是另一条独立问题,必须靠流式解决;(b) 调大上游 `vad_min_silence` 会同时拖慢打断和 START/END 事件。**但请在文档里主动回答这个问题**,否则评审/接手者会反复质疑 GAP 层的必要性。换言之:流式(治丢字)和 GAP(治切分)是**两个可分离的关注点**,方案把它们捆在一起,文档应说明捆绑的合理性。

**Q4(GAP/max_delay 默认值)→ 见第二节。** 起点建议 GAP=1.0(不是 1.2–1.5)、min_delay≈0、内置 VAD min_silence≈0,LIVE 往上加。先把延迟压住,再看切分够不够。

**Q5(mute 语义)→ 见第三节,升级为必做项。**

**Q6(长会话重连)→ 当前"断了降级离线"不够,至少要做重连去重。** 一条 WS 贯穿整轮会话 + 2pass,重连间隙音频丢失(§8 已列)会再现"丢字",正是本方案要根治的问题。建议至少:重连后用 `offline` 段重叠做文本去重,或重连窗口内回退在线 2pass 兜底。可作为 Phase 2 已知风险,但要写明"重连 = 已知丢字风险点"。

**Q7(默认 upstream 还是 optimized)→ 先 upstream 默认,optimized 充分 LIVE 验证后再翻默认。** 理由:optimized 引入新外部依赖路径(流式 WS 长连)+ mute 待补 + 延迟预算待验证。保守默认、A/B 充分后再切,符合"review-before-merge / 可回退"硬约束。

---

## 五、其他较小问题

1. **preemptive_generation 未在设计中讨论。** 默认 `preemptive_tts=True`(`turn_config.py:84`),FINAL 一来就预生成回复(`audio_recognition.py:948`)。GAP 方案下 FINAL 即"一轮终稿",预生成与续话合并(取消提交后又来一条 FINAL)会产生"残片回复"观感。Phase 3 .env 起点已设 `preemptive=false`,建议在 §6 旋钮表和 `XIAOGE_STACK` 映射里**显式登记 preemptive**,别让它隐式默认 True。

2. **显示同源切换的衔接。** §5.5 改用主 STT INTERIM 驱动气泡,可行(`on_interim_transcript` 会触发,`:1024`)。但 `live_transcript.py` 现有"new_turn_gap 起新气泡"的逻辑要迁移到新源,否则连续说话会气泡重开/串台。fallback 到在线 2pass 时两源混用要防闪烁。建议明确"主源失效多久才 fallback"。

3. **"两层判停"的冗余度。** 实际绝大多数续话由 GAP 这一层处理;上游语义层只在"停顿落在 (GAP, GAP+max_delay) 窄窗"时才起作用。文档把它写成对等"两层",略高估了上游层贡献。建议如实描述为"GAP 主裁 + 上游语义薄兜底"。

---

## 六、结论与必办项

**方案可以进入实现,但有 3 个必须先解决/降级的点:**

1. **延迟预算(第二节)**——头号风险。修正 GAP 起算点、压内置 VAD 静默与 min_delay,并把 ≤1.5s 从"红线"改为"实测目标"。否则 G1 与 G4 会在 LIVE 阶段正面冲突。
2. **流式模式静音失效(第三节)**——从"开放问题"升级为 §11 必做项。
3. **长会话重连去重(Q6)**——至少写明风险与兜底,避免重现丢字。

**开放问题倾向汇总**:输出门控 / 双 VAD 不合一(补论证)/ 轮边界放自研 STT / 默认先 upstream。

文档质量本身很高(自包含、有实测留痕、有回滚)。上述都是"把张力说透 + 补两个落地缺口",不动总体架构。修正后可以拍板实现。

---

## 附 A:实现阶段 checklist(评审通过后)

- [ ] **延迟**:GAP 从真实 voice-end 起算;内置 VAD `min_silence≈0`;optimized 下 `endpoint_min_delay≈0`;LIVE 实测 felt 后再加 GAP。
- [ ] **静音**:流式模式补 mute 实现(输入层 gate 帧 / 自研 STT `muted` 标志),并恢复 `_test_recorder.set_paused` 耦合。
- [ ] **重连**:2pass WS 长连重连 + `offline` 段重叠去重;或重连窗口回退在线 2pass。
- [ ] **防幽灵**:输出门控;对被丢弃文本打 KPI 计数(幽灵率可观测)。
- [ ] **preemptive**:在 `XIAOGE_STACK` 映射与 §6 旋钮表显式登记;optimized 下默认 false。
- [ ] **显示同源**:把 `new_turn_gap` 起新气泡逻辑迁到主 STT INTERIM 源;定义 fallback 切换延时。
- [ ] **文档**:修正"逐 FINAL 才提交"表述;补"为何不直接调大 vad_min_silence"的论证;G4 改为目标。

## 附 B:核对用到的代码位置(便于设计者复核)

| 主题 | 位置 |
|---|---|
| 空结果静默丢弃 | `livekit-agents/livekit/agents/stt/stream_adapter.py:129-132` |
| EOU 空 transcript 早返回 | `livekit-agents/livekit/agents/voice/audio_recognition.py:1125` |
| INTERIM 只写 interim 字段 | `…/audio_recognition.py:1030` |
| FINAL 累加 `_audio_transcript` | `…/audio_recognition.py:911` |
| VAD SOS 取消 pending EOU(续话取消) | `…/audio_recognition.py:1084-1085` |
| commit 时才清空 transcript(续话合并) | `…/audio_recognition.py:1291` |
| vad_base 模式 / VAD EOS 触发 EOU | `…/audio_recognition.py:163, 1107` |
| endpointing 提交前 sleep | `…/audio_recognition.py:1213` |
| 预生成触发 | `…/audio_recognition.py:948` |
| 基类自动重采样 | `livekit-agents/livekit/agents/stt/stt.py:462-471` |
| 流式模式 `_switchable_stt=None` | `examples/voice_agents/web_ui_agent.py:980` |
| 静音实现(仅 Switchable 路径) | `examples/voice_agents/web_ui_agent.py:249-253, 662-667` |
| 仍传 `vad=`(续话取消前提) | `examples/voice_agents/web_ui_agent.py:1003` |
| preemptive 默认 True | `examples/voice_agents/turn_config.py:84` |

---

# 第二轮评审(针对 `TURN_STT_DESIGN.md` v2)

| 项 | 内容 |
|---|---|
| 评审对象 | `TURN_STT_DESIGN.md` **v2**(已纳入第一轮意见) |
| 结论 | **通过,可进入实现**;实现前仅需捏合 §5.2↔§5.7 的静音矛盾 |

## 总评

v2 **逐条回应了第一轮 7 项反馈,且修法大多比原建议更到位**。尤其延迟那条——第一轮建议"内置 VAD `min_silence≈0`",设计者更进一步指出那会抖动,改用"逐帧语音概率产出 `last_voiced_ts`"(§5.3),这是更正确的解法。已核实 silero 的 `INFERENCE_DONE` 事件确实带 `probability`/`speaking`/`timestamp`(`vad.py:55-62`),可落地。机制澄清(§5.4)、双 VAD 论证(§4.3)、`vad_min_silence` 论证(§5.3.1)、决策记录表(§12)都诚实清楚。

下面只列 **v2 新引入的一个矛盾** + 两个落地提醒,已了结的点不再重提。

## 一、必须修正:§5.2 与 §5.7 在"静音"时直接打架(v2 新增内容引入)

- **§5.2**:"**始终**把音频送 FunASR(避免空闲超时/重连)"。
- **§5.7**:"muted 时**输入帧不送 FunASR**、且不发任何事件"。

两者对"静音时是否送帧"指令相反。后果:若按 §5.7 静音即停送,长静音 → FunASR WS 空闲超时断开 → 解除静音瞬间正好撞上 §5.8 的"重连间隙丢字"。等于静音功能亲手制造了本方案要根治的问题。

**建议(且能简化实现)**:静音 = **把 §5.2 的 VAD 输出门控强制关闭**——音频照常送 FunASR(保活 WS),但所有文本一律按"静音期幽灵"丢弃、不累加、不发 INTERIM/FINAL。好处:
1. 与 §5.2 保活一致,无重连风险;
2. 复用已有输出门控通路,不需要第二套"muted 不送帧"逻辑;
3. 语义干净:静音 ≡ "VAD 永远判静音"。

把 §5.7 的"输入帧不送 FunASR"改成"强制门控丢弃全部文本(帧仍送以保活)"即可。

## 二、落地提醒(不阻断,实现时注意)

1. **felt 真实落点要有预期。** Phase 1 离线 felt 中位 1472–1603ms,其中"判停等待"约 0.65s(vad_min_silence 0.35 + min_delay 0.3)。v2 用 GAP=1.0 + min_delay≈0 替换这段 → 净增 ~0.35s → **optimized felt 预计 ~1.8–2.0s**(再加 EOU/LLM/TTS 抖动)。不是问题(已是目标非红线),但请在 LIVE 前接受"≤1.5s 多半够不着,要靠 §5.9 preemptive 砍 LLM_TTFT 才有机会逼近"。§5.9 把 preemptive 定位为延迟杠杆,逻辑自洽。

2. **逐帧概率会让 `last_voiced_ts` 受单帧噪声影响、轻微拉长轮次。** 偶发高于 activation 的噪声帧会刷新 `last_voiced_ts`、把 FINAL 后推(延迟↑,但不误切、不丢字——方向安全)。activation 阈值能挡大部分;若 LIVE 发现噪声拖尾,可对 `last_voiced_ts` 加"连续 N 帧有声才更新"的去抖。备选,起步不必做。

3. **§5.9 "仅在无续话合并时采用预生成结果"实现偏复杂**(续话合并发生时要丢弃已预生成回复)。既然 preemptive 默认 false、定位备选,建议标注"开启 preemptive 时再处理",别进 MVP 关键路径。

## 三、结论

- 7 项反馈全部纳入,修法技术上成立(延迟机制 / 双 VAD / 静音 / 重连 / preemptive / 措辞 / 幽灵率 KPI)。
- **唯一需在实现前捏合的是 §5.2↔§5.7 静音矛盾**(见第一节,有简化解法)。
- 第二节三条为 LIVE 阶段提醒,不阻断拍板。

**意见:修掉静音那处矛盾后即可实现**,其余在 LIVE 调参中迭代。文档质量已很高,§12 决策记录 + §11 checklist 对接手者友好。

## 附:第二轮新增的代码核对

| 主题 | 位置 |
|---|---|
| VAD `INFERENCE_DONE` 带 probability/speaking/timestamp(支撑 §5.3 逐帧 `last_voiced_ts`) | `livekit-agents/livekit/agents/vad.py:55-62` |

---

# 第三轮评审(针对 `TURN_STT_DESIGN.md` v3)

| 项 | 内容 |
|---|---|
| 评审对象 | `TURN_STT_DESIGN.md` **v3 收尾版** |
| 结论 | **核心架构通过,可实现**;唯一必改:§5.7 的一句"隐私"声明与工程现状不符 |

## 总评

第二轮三点全部妥善纳入,且静音矛盾的修法比原建议更好——**静音 = 送静音帧保活 + 输出门控拒收 + 录音暂停**(§5.7);felt 预期(~1.8–2.0s)写进 §2/§9;去抖与 preemptive 复杂度都明确移出 MVP。这些干净利落,无异议。

但 v3 给静音**新加的"隐私"卖点引入一个事实性错误**,必须改。

## 一、必须修正:§5.7「隐私:真实人声不出本机」不成立

§5.7 / §12-Q5 称静音三重好处之一是"**隐私:真实人声不出本机**"。**这条与现状不符**:

静音只给主 STT(funasr-stream)送静音帧,但**在线 2pass 打断 tap 仍在把真实麦克风音频持续推给远端 FunASR 服务器**(`FUNASR_WS_URL` = `wss://60.205.197.165:10090`,见 `online_interrupt.py:45/60/144`,"持续把 mic 音频推给 FunASR 2pass 流")。该 tap 独立装在 `session.input.audio` 上(`web_ui_agent.py:1260`),与主 STT 的 `muted` 标志无关。

静音时音频实际去向:
- 主 STT:送静音帧 ✅(隐私 OK)
- **在线 2pass tap:真实人声照样上传远端 ❌**(隐私声明在此破功)
- KWS:本地,无所谓;录音:§5.7 已暂停 ✅

**两个修法,二选一:**
- **(A) 弱化声明(推荐,最省)**:把"真实人声不出本机"改成"主 STT 不转写静音期音频";保活、零浪费两条好处仍成立,不夸大隐私。
- **(B) 真做到隐私**:静音时**同时门控在线 2pass tap**(`OnlineTapAudioInput` 在 muted 时送静音/停推),才谈得上"不出本机"。

倾向 (A)——MVP 不必为隐私去动在线打断链路;但别留一句站不住的卖点在文档里。

## 二、连带产品语义(顺带提示,非阻断)

承上:静音时在线 2pass tap 与 KWS 仍在听真实音频 → **用户"关麦"后仍可能用(静音的)说话打断小歌播报**。原始离线实现也有此局限(老 mute 只挡主 STT),v3 非新增。但既然 v3 重新定义了静音,建议顺手明确:**"关麦"是否应一并静默打断?** 若是,静音需统一作用于"主 STT + 在线 tap + KWS";若否(只是不进上下文),当前范围 OK,但文档要说清"关麦 ≠ 停止打断"。一句话决策,写进 §12 即可。

## 三、结论

- 第二轮三点全部落实,修法到位。
- **唯一必改:§5.7 隐私声明**(选 A 弱化 / B 真做),属文档或小范围实现级,不动核心架构。
- 第二节是一句话的产品语义澄清,建议补但不阻断。

**意见:把 §5.7 隐私声明按 (A) 或 (B) 修正后即可拍板实现。** 架构、判停模型、延迟取舍、重连兜底均已评审收敛,无遗留结构性风险。

## 附:第三轮新增的代码核对

| 主题 | 位置 |
|---|---|
| 在线 2pass tap 推真实音频到远端 FunASR(静音不受其约束 → 隐私声明破功) | `examples/voice_agents/online_interrupt.py:45, 60, 144` |
| 在线 tap 独立装在 `session.input.audio` | `examples/voice_agents/web_ui_agent.py:1260` |

---

# 第四轮评审 / 放行(针对 `TURN_STT_DESIGN.md` v3.1)

| 项 | 内容 |
|---|---|
| 评审对象 | `TURN_STT_DESIGN.md` **v3.1 收尾版** |
| 结论 | ✅ **可以进入开发**(无遗留阻断项;剩余皆 LIVE 调参) |

## 总评

v3.1 用**方案 B(关麦=真关麦)**彻底解决了第三轮的隐私问题,选得对、改得干净。三轮评审的所有结构性风险均已收敛。

## 一、v3.1 静音方案核验(第三轮唯一必改项 → 已解决)

新方案:输入链路源头插静音门,`muted` 时把真实麦克风帧替换为静音帧,位置在所有 tap 之前,使主 STT / 在线2pass / KWS / 录音一致拿到静音(§5.7)。

**架构成立**,对照现有 tap 链:
- 现链为 `OnlineTap(KwsTap(base))`(KWS `web_ui_agent.py:1209`、Online `:1260` 依次包裹),AgentSession 从最外层往里拉帧。
- 静音门作为**最内层**(紧贴基座、KWS/Online 之前)插入 → 关麦时所有上层 tap 与主管线都收到静音帧 → 不转写、不打断、真人声不出本机、各 WS 静音帧保活(无重连)。四重效果都站得住。
- 录音单独靠 `set_paused(True)` 处理,不依赖门位置 —— 正确。
- **产品语义"关麦 = 停止打断"已明确写定**(§5.7/§5.6/§12),第三轮第二点随之关闭。

## 二、实现期小提醒(非阻断)

1. **静音门必须是最内层包裹**(KWS/Online 之前)。插错到外层 → 内层 tap 仍拿真音频,隐私又破功。建议在 §5.7 或 §11 checklist 点明"源头/最内层"。
2. **`muted` 切换线程安全**:`/api/mic`(web 线程)vs 音频(agent loop)。沿用 `SwitchableSTT.muted` 的简单 bool(GIL-safe)即可,勿引锁。
3. 静音门默认直通、不设即零影响 —— 与"旁路功能 opt-in、不碰正常流"硬约束一致。

三条均为编码注意,不需再改设计。

## 三、是否可进开发 —— 明确判定:**可以**

| 项 | 状态 |
|---|---|
| 延迟取舍(GAP 起算 / min_delay≈0 / felt~1.8–2.0s 诚实写明) | ✅ 收敛 |
| 流式静音(方案 B 真关麦,无重连风险) | ✅ 收敛 |
| 2pass 重连(风险写明 + 在线兜底 + offline 去重) | ✅ 收敛 |
| preemptive(移出 MVP) | ✅ 收敛 |
| 防幽灵(输出门控 + 幽灵率 KPI) | ✅ 收敛 |
| 双 VAD 论证 / 上游零改 / 显示同源 fallback | ✅ 收敛 |

剩余项全是"只能 LIVE 实测才能定"的调参(`GAP`/`max_delay` 值、felt 体感、幽灵率),§12 已登记为"待 LIVE 验证",本属开发+联调阶段工作,**不构成开发前置阻断**。

**建议开发顺序**:先落地 `funasr_stream_stt.py`(GAP 聚合 + 输出门控)与静音门 → 注入 A/B 验丢字/覆盖率(可靠) → 再 LIVE 真人调判停旋钮。`XIAOGE_STACK` 默认保持 `upstream`,optimized 充分 LIVE 验证后再翻默认(Q7 已定)。

**放行。** 四轮评审收尾,可进入开发。

## 附:第四轮新增的代码核对

| 主题 | 位置 |
|---|---|
| tap 链包裹顺序(静音门须插在 KWS/Online 之前 = 最内层) | `examples/voice_agents/web_ui_agent.py:1209, 1260` |
| 既有线程安全 muted 标志范式(供静音门沿用) | `examples/voice_agents/web_ui_agent.py:249-253` |
