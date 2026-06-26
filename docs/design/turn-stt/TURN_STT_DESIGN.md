# 流式主 STT + 判停优化 — 设计文档 v2(评审版)

> 注:文中 `文件:行号` 为撰写时快照,代码已演进,**以符号名/当前代码为准**。

| 项 | 内容 |
|---|---|
| 版本 | **v3.1 收尾版**(已纳入第一、二、三轮评审意见,实现就绪) |
| 状态 | ✅ **四轮评审放行,进入开发**(剩余仅 LIVE 调参) |
| 日期 | 2026-06-24 |
| 适用 | 小歌全双工语音引擎(LiveKit Agents 的 fork),入口 `examples/voice_agents/web_ui_agent.py` |
| 关联 | 过程留痕 `TURN_STT_REFACTOR.md`;评审意见 `TURN_STT_DESIGN_REVIEW.md`;本文件为自包含设计规格 |
| 范围 | 重做 **④**(听清 + 判一轮):主 STT 换流式 + 判停轮边界改造 + 防幽灵 + 显示同源 + 流式静音 + 重连兜底 |

### v3.1 相对 v3 的变更(纳入第三轮评审)
- **静音改为方案 B「关麦 = 真关麦」**:第三轮指出 v3 的"真人声不出本机"不成立(在线 2pass tap 仍把真人声上传远端)。改为**在输入链路源头插静音门**:`muted` 时把真实麦克风帧替换为静音帧,**覆盖 主STT / 在线2pass tap / KWS** + 录音暂停 → 不转写、不打断、**真人声确实不出本机**、各 WS 静音帧保活。**关麦 = 停止打断**(产品语义已定)。

### v3 相对 v2 的变更(纳入第二轮评审)
1. **静音矛盾修正(必修)**:v2 的 §5.2「始终送音频」与原 §5.7「muted 不送帧」冲突 → 统一为 **静音 = 向 FunASR 送静音帧(零)保活 + 输出门控拒收全部文本 + 录音暂停**(§5.7;隐私更优、无重连丢字风险)。
2. **felt 预期写明**:optimized felt 预计 **~1.8–2.0s**(GAP 取舍的必然结果,§2/§9);≤1.5s 为目标,需 preemptive 才可能逼近,属产品取舍点。
3. §5.3 增 "`last_voiced_ts` 去抖(连续 N 帧有声才更新)" 为**可选加固、不进 MVP**。
4. §5.9 preemptive 的"续话合并丢弃预生成"复杂度**移出 MVP**(开启 preemptive 时再处理)。

### v2 相对 v1 的主要变更(回应第一轮评审)
1. **延迟(头号风险)**:`G4 ≤1.5s` 由"红线"降为"实测目标";**GAP 以"最后一帧有声"起算**(不与 `vad_min_silence` 叠加,也不把内置 VAD 静默设≈0);optimized 下 `endpoint_min_delay≈0`;明确写出 **G1↔G4 固有张力**。
2. **流式静音(现存 bug,升为必办)**:流式模式 `_switchable_stt=None` → 静音按钮失效。新增 §5.7 实现方案。
3. **长会话重连(必办)**:新增 §5.8,重连=已知丢字风险 + 兜底 + 去重。
4. 新增 §5.9 **preemptive 显式登记**(optimized 默认 false,定位为延迟优化备选)。
5. 补两段论证:**为何不直接调大 `vad_min_silence`**(§5.3.1)、**双 VAD 为何不合一**(§4.3)。
6. 措辞修正:上游"逐 FINAL 才提交"→ 准确机制(§5.4);`stream_adapter` 行号改 129–132。
7. 防幽灵加**幽灵率 KPI**;"两层判停"如实改为 **"GAP 主裁 + 上游薄兜底"**。

---

## 1. 背景与现状问题

工程是开源 **LiveKit Agents** 的 fork,会持续合并上游迭代。当前语音输入链路 ④:
**silero VAD → StreamAdapter(把非流式 STT 适配成流式)→ 离线 FunASR(主 STT)→ 上游 audio_recognition(endpointing/turn detector)**。

实测(可复现:注入 `runs/.../user.wav` + KPI 仪表盘)暴露三问题:
1. **长句丢字**:离线 FunASR 经 VAD 切段后整段识别;长段超时 → 返回空 → `StreamAdapter` **静默丢弃**(`stream_adapter.py:129-132`,`len==0` 或空文本直接 `continue`)。
2. **过度切分 / 回多次**:VAD 一停就 `END_OF_SPEECH` 切段,上游逐段提交 → 一段连续话被切成多轮(实测 ~27 轮/18 回复)。
3. **显示串台**:live 气泡由"在线 2pass FunASR"驱动,内容由主 STT 提供,两源不一致。

实测结论:同段真实录音,**流式 FunASR(2pass)转写最完整准确(101 字)**,讯飞 5 字(对该麦克风音频鲁棒性差,非限流),离线丢大半 → **主 STT 用流式 FunASR**(免费、本就是显示源)。判停时序**只能 LIVE 真人调**(注入停顿固定、无交互 → KPI 离群);注入只可靠用于"内容/准确度"。

---

## 2. 目标 / 非目标

**目标**
- G1 连续说 = 一轮 = 一次回复;停了才判一轮。
- G2 不丢字。
- G3 显示 / 上下文 / 录音 **同源**。
- **G4 felt(user_stop→agent_speak)目标 ≤ 1.5s**(**目标,非红线**;与 G1 存在固有 trade-off,见下)。
- G5 防幽灵词:无人说话时 ASR 蹦字,不触发提交/回复、不误打断。
- G6 不改上游,可一键 A/B(原始 ④ vs 优化 ④)。

> **G1 ↔ G4 固有张力(评审头号 concern)**:GAP 要够大才能合并自然停顿(G1),而 GAP 直接进 felt(必须等够 GAP 才能确认说完)→ GAP↑ 则 felt↑。这是结构性矛盾,**不能靠调参消除,只能取舍**。
> **实测预期 optimized felt ~1.8–2.0s**:现离线 felt 中位 ~1.5s 含 ~0.65s 判停等待(vad_min_silence 0.35 + min_delay 0.3),换成 GAP(起点曾试 1.0,实测过切已固化 1.5)+ min_delay≈0 净增更多。**≤1.5s 多半够不着**,只能靠 §5.9 preemptive 砍 LLM_TTFT 去逼近 → 这是**产品取舍点**,LIVE 时定可接受值;`GAP` 旋钮可随时往"快"调(代价是切分回升)。验收用"感知是否被打断/迟钝"而非纯 ms。

**非目标**:不改 TTS/LLM/四层打断总体架构;不追求多语种;不引入新付费外部依赖(讯飞仅作可选项)。

---

## 3. 设计原则(纪律)
1. **零改上游**(`livekit-agents/`、`livekit-plugins/` 不动);优化全在 `examples/voice_agents/`。
2. 可开关、默认保留原行为。
3. 一键 A/B:`XIAOGE_STACK=upstream|optimized`。
4. 判停/聚合放我们 STT 层,上游 endpointing/turn detector 不改、只消费我们的事件。
5. 测量驱动:注入测内容/准确度,LIVE 测判停;KPI/录音对比。

---

## 4. 总体架构与数据流

### 4.1 组件职责
| 组件 | 职责 |
|---|---|
| **AgentSession 的 VAD**(`vad=`,上游) | ① `START/END_OF_SPEECH` → 上游续话取消 + 打断;② 用户状态。**静默阈值要"短"**(打断灵敏) |
| **funasr-stream STT(新增,主 STT)** | 对 VAD 确认的语音连续 2pass 识别;**内置一处 VAD 出"最后有声时刻"**做防幽灵门控 + GAP 聚合;发 INTERIM/FINAL |
| **轮次聚合(在 funasr-stream 内)** | 累加本轮文本;**静默 ≥ GAP(以最后有声帧起算)才发一条 FINAL** |
| **上游 endpointing / turn detector**(不改) | 收 FINAL 后:语义判完句 + 续话合并(max_delay 窗内 VAD 又开口→取消提交、累加下条 FINAL)= **薄兜底** |
| **打断源** | AgentSession-VAD(声学)/ KWS / 在线 2pass(**+VAD 佐证防幽灵**) |
| **显示** | live 气泡由**主 STT INTERIM** 驱动(同源) |

### 4.2 流程图
```
麦克风(连续音频;已过 录音/KWS/在线2pass tap)= session.input.audio
   │
   ├─► AgentSession VAD(上游,独立,vad=;静默阈值短)
   │        ├─ START/END_OF_SPEECH ─► 上游 audio_recognition(续话取消 + 打断)
   │        └─ 声学打断 ───────────────────────────────────────┐
   │                                                            │
   └─► funasr-stream STT(主STT,内置 VAD 出"最后有声时刻")     │
          VAD 判语音?                                           │
            ├─ 否(静音/底噪)► 丢弃文本(★防幽灵, 计幽灵率KPI)  │
            └─ 是► 送 FunASR 2pass(online增量 / offline段尾)    │
                   ▼                                            │
                累加"本轮文本" + 发 INTERIM ─► live 气泡(同源)  │
                   ▼                                            │
                (now − 最后有声帧) ≥ GAP ?(轮次聚合)            │
                  ├─ 否► 继续累加(同一轮)                       │
                  └─ 是► 发【一条 FINAL】+清空 ─► 上游 audio_recognition
                                                               │
   ┌────────────────────────────────────────────────────────────┘
   ▼ 上游(代码不改):turn detector 读"本轮文本"判 EOU(薄兜底)
   ├ 完整句 ─► min_delay(实际 0.3)─► 提交 ─► 附和/停止词过滤 ─► 回复一次
   └ 半句   ─► max_delay 窗:
        ├ 用户又开口(AgentSession-VAD START_OF_SPEECH)► 取消提交 + 与下条FINAL合并=同一轮
        └ 仍沉默到点 ─► 提交 ─► 回复一次
                                                               │
 打断 agent ◄── 声学(VAD)/ KWS / 在线2pass(★+VAD佐证)─────────┘
```

### 4.3 为什么用两处 VAD、且不合一(评审 Q2,补论证)
两处 VAD **需要不同的静默阈值**,合一必牺牲一头:
- **上游 VAD 要"短"**:barge-in/打断要灵敏(用户一插话就要能取消提交/打断播报)。
- **自研 STT 的 GAP 要"长"**:要把"连续说+换气"合并成一轮(~1s 级)。
若复用同一条 VAD 事件,就得二选一(短→不合并/碎;长→打断迟钝)。**这是双 VAD 复杂度最强的正当性。**
注:`live_transcript.py` 已有 `new_turn_gap` 同类计时(驱动 live 气泡)——证明 GAP 概念在工程里可用;但实现时要**把它统一到主 STT 这条 GAP**,避免两个 GAP 计时器打架。

---

## 5. 详细设计

### 5.1 主 STT:`funasr-stream`(流式)
- 复用项目内**已验证的 FunASR 2pass 协议**(同一台 FunASR 服务,`FUNASR_WS_URL`,`mode:"2pass"`):`2pass-online`=增量、`2pass-offline`=段尾校正;取 `text`。
- 以 livekit `stt.STT`+`stt.RecognizeStream` 实现(`streaming=True, interim_results=True, offline_recognize=False`);`RecognizeStream(sample_rate=16000)` 由基类自动重采样(`stt.py:462-471`)。
- 一条 WS 贯穿整轮会话;断连见 §5.8。

### 5.2 VAD 门控防幽灵(评审 Q1:输出门控)
- **输出门控**:始终把音频送 FunASR(避免空闲超时/重连;**静音时改送静音帧保活,见 §5.7**),但**只接受"VAD 确认有语音"窗口内的文本**;VAD 判静音期蹦出的文本**丢弃**(不累加、不计 GAP、不发 FINAL)。
- 选输出门控而非输入门控的理由(评审一致):输入门控(静音不送)会触发 FunASR 空闲超时/重连,且 `offline` 段尾校正可能跨"静音-有声"边界、被输入门控打碎。
- **新增幽灵率 KPI**:对"被门控丢弃的文本"计数(条数/字数),进 `turn_kpis.json`,便于观测幽灵频率。
- 兜底:既有附和/停止词过滤继续挡"嗯/啊"。

### 5.3 轮次聚合(GAP)
```
prefix = ""                # 已收尾段落(offline 段尾累积)
seg    = ""                 # 当前段在线增量(online)
pending = prefix + seg      # 本轮已确认文本
last_voiced_ts = None      # 最后一帧"语音概率≥阈值"的时刻(monotonic)
voiced = False

每帧:  p = VAD推理(帧)
       if p ≥ activation: voiced = True;  last_voiced_ts = now()
       else:              voiced = False
# 门控 accepting():voiced 或 处于"最后有声后的 GAP 窗内"(尾巴容忍=整个 GAP 窗,容识别延迟)
on FunASR online(t):   if accepting(): seg = seg + t; emit INTERIM(prefix+seg)
                       else: 丢弃(幽灵, 计数)
on FunASR offline(s):  if accepting(): prefix = (prefix + s); seg = ""   # 段尾文本追加进已收尾前缀、清空在线增量段
                            emit INTERIM(prefix+seg)
                       else: 丢弃(幽灵, 计数)
watchdog(周期):        pending = (prefix + seg)
                       if pending and last_voiced_ts and (now − last_voiced_ts ≥ GAP):
                            emit FINAL(pending); prefix=""; seg=""; last_voiced_ts=None
```
- **关键(评审 #1 细化)**:GAP **以"最后有声帧"起算**,不从 VAD 的 EOS 段事件起算 → **不与 `vad_min_silence` 叠加**;内置 VAD **不设 min_silence≈0**(那会抖动),而是直接用"逐帧语音概率"产出 `last_voiced_ts`。
- `GAP` = 连续/新轮分界,`XIAOGE_AGG_GAP` 默认 **1.5s**(评审起点曾取 1.0 先压延迟;**1.0 实测过切**——带思考停顿/重复口语的半句被切成多轮,**已固化 1.5**;太碎调大、嫌慢调小)。
- 中途短停顿(< GAP)只累加;`offline` 段尾只做本段校正,**不**当轮边界。
- **去抖(可选,不进 MVP)**:逐帧概率会让偶发噪声帧刷新 `last_voiced_ts`、把 FINAL 后推(延迟↑,但**不误切、不丢字,方向安全**);起步靠 activation 阈值挡,若 LIVE 见噪声拖尾再加"连续 N 帧有声才更新 `last_voiced_ts`"。

#### 5.3.1 为什么不直接把上游 `vad_min_silence` 调到 ~1.2s 就好(评审 Q3 延伸)
"流式治丢字"和"GAP 治切分"是**两个可分离的关注点**,被本方案捆绑,理由:
- (a) **长段离线丢字是独立问题**,调 `vad_min_silence` 救不了——必须靠**流式 STT**(连续识别、无整段超时丢弃);
- (b) 调大上游 `vad_min_silence` 会**同时拖慢打断与 START/END 事件**(打断/续话取消都依赖它要灵敏);
- 故:**上游 VAD 维持"短"(管打断),把"长容忍"交给我们 STT 的 GAP**。这也正是 §4.3 双 VAD 的原因。

### 5.4 两层"说完了":GAP 主裁 + 上游语义薄兜底(措辞修正)
- **主裁(声学,我们 GAP)**:静默 ≥ GAP → 发 FINAL。**绝大多数续话由这一层处理。**
- **薄兜底(语义,上游 turn detector)**:仅在"停顿落在 (GAP, GAP+max_delay) 窄窗"才起作用——读本轮文本判 EOU:完整→`min_delay`(设计目标≈0,**代码/.env 实际固化 0.3,降到 0 未落地**)提交;半句→`max_delay` 窗内用户又开口则**取消提交、与下条 FINAL 合并**,仍沉默则提交。零成本(上游不改),保留以接住边角。
- **机制澄清(评审表述瑕疵)**:`turn_detection` 传 `MultilingualModel` 实例 → `_vad_base_turn_detection=True`,**VAD `END_OF_SPEECH` 也会触发 `_run_eou_detection`(`:1107`)**,只是 transcript 为空而早返回(`:1125`)→ 不是"只有 FINAL 才触发",而是"只有有 transcript(=收到 FINAL)才真正提交"。
- **续话取消为何仍有效**:喂上游的是 **AgentSession 独立 VAD**(`vad=`,`web_ui_agent.py:1003`),与换不换主 STT 无关;`START_OF_SPEECH` 取消 pending EOU(`:1084-1085`),`_audio_transcript` 仅 commit 时清空(`:1291`)→ 下条 FINAL 累加合并。

### 5.5 显示同源
- live 气泡改由**主 STT INTERIM**(`on_interim_transcript`,`:1024`)驱动 → 显示=内容=录音。
- **把 `live_transcript` 现有"new_turn_gap 起新气泡"的逻辑迁到这条主源**(否则连续说会气泡重开/串台)。
- 显示源为**启动期静态二选一**(`_live_from_main = _stt_mode in {funasr-stream, iflytek}`):流式后端用主 STT interim 驱动气泡,否则用在线 2pass;**无运行时定时 fallback**(原设计设想的"主源静默 N 秒才切在线 2pass"未实现,故不存在双源闪烁问题)。

### 5.6 打断与防幽灵
- VAD 声学打断:天然抗幽灵,保留。
- 在线 2pass 文本打断:**加 VAD 佐证**(仅 AgentSession-VAD 确认在出声才打断)。
- KWS:精确关键词,保留。
- **关麦时所有打断源收静音 → 不打断**(见 §5.7,关麦=停打断)。

### 5.7 流式模式的"麦克风静音"(评审 §3,现存 bug → 必办)
- 现状 bug:静音仅实现在 `SwitchableSTT._recognize_impl`(`web_ui_agent.py:249-253`);流式分支 `_switchable_stt=None`(`:980`)→ `_handle_mic`(`:662`)返回 503 → **静音按钮失效**,且 `_test_recorder.set_paused`(`:667`)耦合一并失效。
- 修复(**方案 B:关麦 = 真关麦**):在**输入链路源头**插一个**静音门**——`muted` 时把真实麦克风帧**替换为静音帧**,位置在 recorder / KWS / 在线2pass / 主STT **之前**,使下游所有消费者**一致拿到静音帧**:
  - 主 STT(funasr-stream):收静音 → 不转写、WS 静音帧保活;
  - 在线 2pass tap:收静音 → **不上传真人声**、不打断、WS 保活;
  - KWS:收静音 → 不触发关键词打断;
  - 录音:`_test_recorder.set_paused(True)` 暂停用户轨。
  `/api/mic` 改为切换该静音门(不再依赖 `_switchable_stt`)。
- 四重效果:**不转写、不打断、真人声确实不出本机、各 WS 静音帧保活(无重连)**。语义:**关麦 = 麦克风彻底关闭 = 停止打断**。
- **实现注意(第四轮)**:静音门须是**最内层**(紧贴基座、KWS/Online tap **之前**);插到外层 → 内层 tap 仍拿真音频、隐私破功。`muted` 用**简单 bool(GIL-safe)**(沿用 `SwitchableSTT.muted` 范式),不引锁;**默认直通、不设零影响**。
- 与 §5.2 的一致性:§5.2"始终送音频"指的是连接保活;静音期送的是**静音帧**,二者不再冲突。

### 5.8 长会话 / 重连(评审 Q6,必办)
- 一条 WS 贯穿整轮 + 2pass,重连间隙丢音频 = **会再现丢字**(本方案要根治的问题)→ 必须处理。
- MVP:**写明"重连=已知丢字风险"** + 重连窗口内**回退在线 2pass 兜底**(它已常驻)。
- 加固(紧随):重连后用 `offline` 段**重叠去重**(避免重复/缺字)。

### 5.9 preemptive(评审 §5.1,显式登记)
- 现状默认 `preemptive_tts=True`(`turn_config.py:84`),FINAL 一来即预生成(`:948`)。
- **optimized 默认 false**(避免"续话取消后又来 FINAL"的残片回复)。
- 澄清:聚合下 **FINAL = 一轮终稿(非碎片)**,故"对 FINAL 预生成"本身不产生残片,残片**只**可能在 (GAP, GAP+max_delay) 续话取消窄窗出现;**定位为"延迟优化备选"**(开启可砍 felt 里的 LLM_TTFT),待延迟实测后评估。注:"续话合并时需丢弃已预生成回复"的复杂度**移出 MVP**——preemptive 默认 false,**开启 preemptive 时再处理**,不进 MVP 关键路径。

---

## 6. 配置与开关

### 6.1 `XIAOGE_STACK` 映射
| 维度 | `upstream`(原始,默认) | `optimized` |
|---|---|---|
| 主 STT | 离线 FunASR + StreamAdapter | funasr-stream(VAD门控+GAP聚合) |
| 轮边界裁判 | VAD + 上游 endpointing | 我们 STT 的 GAP(+上游薄兜底) |
| 防幽灵 | VAD切段天然带一点 | VAD门控显式防 + 幽灵率KPI |
| 静音 | SwitchableSTT 路径(仅挡主STT) | **关麦=真关麦**:输入源头静音门(主STT/在线tap/KWS 全收静音)+ 录音暂停(§5.7)|
| live 显示 | 关 | 开 + 同源(主STT interim) |
| 在线2pass 打断 | 纯文本 | +VAD 佐证 |
| **preemptive** | 上游默认(true) | **false**(默认) |
| 上游文件 | 原样 | 原样(零改) |

### 6.2 旋钮(env;粒度项覆盖 profile)
| env | 含义 | 默认 |
|---|---|---|
| `XIAOGE_STACK` | 总开关 | **upstream**(LIVE 充分验证后再翻 optimized,评审 Q7) |
| `STT_BACKEND` | funasr / funasr-stream / iflytek | optimized 下 funasr-stream |
| `XIAOGE_AGG_GAP` | GAP 聚合上限(秒,以最后有声帧起算) | **1.5**(1.0 实测过切,固化 1.5) |
| `XIAOGE_AGG_GAP_MIN` | 自适应 GAP 下限:句子"像说完"时静默达此值即提交;设 ≥GAP 则退化为恒定 GAP | **0.8** |
| `XIAOGE_STREAM_VAD_ACTIVATION` | 自研 STT VAD 语音判定阈值(逐帧概率≥此值算有声) | **0.5** |
| `TURN_ENDPOINT_MIN_DELAY` | 上游确认等待 | 代码/.env 实际 **0.3**(min_delay≈0 未落地) |
| `TURN_ENDPOINT_MAX_DELAY` | 续话窗 | 代码默认 **0.6** / .env 固化 **1.2** |
| `XIAOGE_ONLINE_VAD_GRACE` | 在线软打断的 VAD 佐证宽限(文本到达时 VAD 须确认在说话或刚停 <此值) | **0.6** |
| `XIAOGE_ONLINE_INTERRUPT_ENABLE` | 在线2pass 文本抢先打断总开关 | **1** |
| `TURN_UNLIKELY_THRESHOLD` | 上游 EOU 判完句阈值 | 模型默认 |
| `TURN_PREEMPTIVE_TTS` | 预生成 | optimized **false** |
| `XIAOGE_ONLINE_INTERRUPT_MIN_CHARS` | 在线2pass 打断字数门槛 | 3 |

---

## 7. 上游零改保证
仅通过 **AgentSession 构造参数**(`stt=` 我们的流式 STT、`vad=`、`turn_handling=`)与**我们自己的 STT 模块**接入;不改任何 `livekit-agents/`、`livekit-plugins/`。现状已验证(全部改动在 `examples/voice_agents/` 等我们层,`git log` 可证)。收益:上游可干净合并;原始实现随时对照/回退。

---

## 8. 边界 / 失败模式 / 风险
| 项 | 处理 |
|---|---|
| 幽灵词 | VAD 输出门控丢弃 + 幽灵率KPI;在线2pass 打断加 VAD 佐证;附和/停止词兜底 |
| felt 与 GAP 张力 | GAP 以最后有声帧起算(不叠加 vad_min_silence)、min_delay 目标≈0(实际固化 0.3、未落地);G4 为目标,LIVE 取舍;感知验收 |
| 流式静音 | §5.7 必办(muted 标志 + 恢复录音暂停耦合) |
| 2pass WS 重连 | §5.8:风险写明 + 在线2pass 兜底 + offline 去重(加固) |
| 长会话/长静音 | 持续推帧保活;长会话重连为已知风险点,观察 |
| 噪声致 VAD 误判语音 | 噪声段进 FunASR 可能蹦字;靠 VAD 阈值 + 内容过滤 + 幽灵率KPI 观测 |
| online/offline 合并 | online 增量、offline 校正同段;以 offline 为段终稿、跨段累加 |
| 两处 VAD | 同模型同输入、职责正交(短/打断 vs 长/聚合);见 §4.3 |
| 显示双源闪烁 | 启动期静态选源(`_live_from_main`),无运行时切换 → 无闪烁(§5.5) |

---

## 9. 验证计划(A/B + KPI)
- **内容/准确度(注入,可靠)**:同段录音跑 `upstream` vs `optimized`,比覆盖率(LCS)/丢字。
- **判停时序(LIVE,真人)**:真实"长独白带停顿",看 `turn_kpis.json` 的 `over_segmentation`/`double_reply`/`felt_latency`;**felt 以感知验收(像不像被打断/迟钝),不只看 ms**。**预期 optimized felt ~1.8–2.0s**(GAP 取舍必然);若太慢,降 `GAP` 换回速度(代价是切分回升),或开 preemptive 砍 LLM_TTFT。
- **防幽灵**:静默/底噪环境,确认无 FINAL/回复/误打断,**幽灵率 KPI ≈ 0 误放行**。
- **建议通过标准(目标,评审定稿)**:同段连续话 `suspected_splits ≈ 0`;覆盖率显著高于离线;felt 体感可接受(目标 ≤1.5s,允许 LIVE 取舍);幽灵零误触发。

---

## 10. 回滚
`XIAOGE_STACK=upstream` 或 `STT_BACKEND=funasr` 一键回原始 ④;优化代码全在我们层、可整体禁用。

---

## 11. 影响文件 / 工作量 + 实现 checklist(评审通过后)

**文件**:新增 `funasr_stream_stt.py`;改 `web_ui_agent.py`(选用/不过 StreamAdapter/保留 vad=/显示同源/流式静音/在线2pass+VAD佐证);`iflytek_stt.py` 保留可选;出 SVG 入 `diagrams/`;离线原始 ④ 全程保留。

**Checklist**:
- [ ] **延迟**:GAP 以真实 voice-end 起算;内置 VAD 用逐帧概率(不设 min_silence≈0);optimized `endpoint_min_delay≈0`;LIVE 实测 felt 后再加 GAP。
- [ ] **静音(关麦=真关麦)**:输入源头插静音门(**最内层/紧贴基座,KWS/Online 之前**),`muted`(简单 bool,GIL-safe)时真实帧替换为静音帧,**覆盖 主STT/在线2pass/KWS** + 恢复 `_test_recorder.set_paused`;隐私(真人声不出本机)成立、关麦=停打断。
- [ ] **重连**:2pass 重连 + `offline` 段重叠去重;或重连窗口回退在线 2pass;写明丢字风险。
- [ ] **防幽灵**:输出门控;被丢文本打**幽灵率 KPI**。
- [ ] **preemptive**:在 `XIAOGE_STACK` 映射与旋钮表登记;optimized 默认 false。
- [ ] **显示同源**:`new_turn_gap` 起新气泡逻辑迁到主 STT INTERIM 源;选源为启动期静态二选一(`_live_from_main`),无运行时 fallback 切换。
- [ ] **文档**:措辞"逐 FINAL"已修正;`vad_min_silence` 论证已补;G4 改为目标。

---

## 12. 决策记录(原"开放问题",评审后已决)
| # | 决策 | 理由 |
|---|---|---|
| Q1 门控 | **输出门控** + 幽灵率KPI | 输入门控触发空闲超时、打碎 offline 校正 |
| Q2 双VAD | **不合一** | 两者需不同静默阈值(短/打断 vs 长/聚合),见 §4.3 |
| Q3 轮边界 | **放自研 STT(GAP)** | 上游 EOU 语义判断实测不可靠;声学更稳;零改上游(论证见 §5.3.1) |
| Q4 默认值 | GAP 起点 1.0(后过切固化 **1.5**)、min_delay 目标≈0(**实际固化 0.3,未落地**)、内置VAD用逐帧概率(activation **0.5**) | 先压延迟,LIVE 往上加;过切后回调 |
| Q5 静音 | **方案 B:关麦=真关麦**——输入源头静音门覆盖 主STT/在线tap/KWS + 录音暂停(§5.7) | 兑现隐私(真人声不出本机)、语义干净(**关麦=停止打断**);静音帧保活避免重连 |
| 关麦语义(第三轮§二) | **关麦 = 停止打断**(随 B) | 与"麦克风彻底关闭"直觉一致 |
| Q6 重连 | **风险写明 + 兜底 + 去重加固**(§5.8) | 重连丢字会抵消本方案收益 |
| Q7 默认栈 | **先 upstream** | 新长连路径 + 延迟待验,保守、可回退 |
| 仍待 LIVE 验证 | `GAP`/`max_delay` 具体值、felt 体感、幽灵率 | 只能真人/真环境实测 |

---

## 附:术语
**④**=听清+判一轮链路;**GAP**=判轮结束的静默阈值(以最后有声帧起算);**endpointing**=上游提交前等待;**2pass**=FunASR 流式(online 增量 + offline 段尾校正);**幽灵词**=无真实语音时 ASR 凭空输出;**同源**=显示/上下文/录音同一识别结果。
