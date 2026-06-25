# 判停 & 主STT 改造 — 过程审计文档

> 供审计。记录每一阶段的:设计 → 自审(从严)→ 修订 → 可行性结论 → 实现 → 自测 → 手测步骤。
> 工作流(用户定):我出方案、自己从严评审、改、再评审,直到认为可行才实现;实现后自测;
> 自测通过再通知用户手测。全程留痕于此。

## 总目标(验收标准)
1. 连续说 = 一个气泡 = 一轮 = 一次回复;停了才判停、才回一次。
2. 不丢字:说了的都进上下文。
3. 显示 / 上下文 / 录音 同源。
4. felt 延迟 ≤ 1.5s(预设,实测后可调)。

## 既定决策(用户确认)
- 顺序:阶段1 注入 → 阶段2 上半段(换流式主STT)→ 阶段3 下半段(调判停)→ 阶段4 回归。
- 流式主STT 候选优先级:`qwen3-stream` →(不通)讯飞 RTASR →(不通)在线2pass 兼任主STT。
- 显示改为主STT 同源(流式落地后),在线2pass 显示路降为 fallback。
- 硬约束:解耦/模块化/不阻塞/不影响其他功能/稳定;全程可切换可回退;review-before-merge。

## 密钥处理
- 讯飞 `IFLYTEK_APPID` / `IFLYTEK_API_KEY` 存于本地 `.env`(已被 .gitignore 忽略,不入仓库)。
- 本文档及任何提交文件**只用掩码**(如 `de2e****`),不出现明文。

---

# 阶段 1:录音注入(测量闭环)

目的:能把"用户说的话"用**录音回放**注入会话,让判停问题**可复现**,为阶段2/3 的 A/B 与扫参提供同一段输入。不依赖合成 TTS。

## 设计 v1
- 新模块 `scripted_audio.py`:`ScriptedAudioInput(io.AudioInput)`,从 wav 生成 `rtc.AudioFrame`,按真实节奏吐帧;在所有 tap 之前替换 `session.input.audio`。
- 门控:env `AGENT_SCENARIO`(wav 或 json 路径),默认不设=正常麦克风,零影响。
- 覆盖率 KPI:场景可声明 `expect`(应识别文本),`turn_metrics` 增 `CoverageDetector` 算"识别覆盖率"≈丢字率。

## 自审(从严)——发现的问题与修订
1. **结束行为**:wav 放完若 `raise StopAsyncIteration`,输入流结束 → 会话可能误判输入关闭而异常。
   → **修订**:wav 放完后**持续吐静音**(让 VAD 判到 end-of-speech → 触发回复;会话保持存活,供观察/录音)。不结束迭代。
2. **开场白重叠**:注入从 session 起就开始,会与开场白(`session.say`)抢话 → 干扰"干净一轮"测量。
   → **修订**:wav 前加 **lead 静音**(默认 4.0s,可配),让开场白先放完再注入。
3. **真实节奏漂移/阻塞**:逐帧 `sleep` 须防累计漂移、且非阻塞。
   → **修订**:用 `loop.time()` 计算 `目标=起点+n×帧长` 的绝对时刻 sleep,避免漂移;纯 await,不阻塞循环。
4. **尾帧不整除**:wav 长度非帧整数倍 → 末帧补零到整帧。
5. **采样率/声道**:wav 可能 24k/立体声 → 读时下混单声道;按 wav 原生率吐帧(下游 VAD/STT 自行重采样,录音器原生记录)。
6. **wav 读取阻塞**:在 `__init__`(启动期、对话开始前)同步读一次(~MB 级,几十 ms),可接受;不在对话中读盘。
7. **覆盖率与注入解耦**:`turn_metrics` 不 import `scripted_audio`;由 entrypoint 读到 `expect` 后调 `TurnMetrics.set_expected()` 注入,保持两模块独立。
8. **覆盖率算法**:用 **LCS 召回** = `LCS(expect, 识别合并文本)/len(expect)`,对增删替换鲁棒;短文本 O(n·m) 可接受;无 `expect` 时该 detector 输出 `{enabled:false}`,不影响其他 KPI。
9. **替换 input.audio 的时机**:必须在 recorder/KWS/online 包裹**之前**(session.start 之后立即替换),使注入音频被如实录进 user.wav、并经各 tap。现有 tap 也是 start 后重赋值且生效,模式一致。

## 可行性结论
修订后无悬而未决风险:opt-in、可回退(不设 AGENT_SCENARIO 即原样)、非阻塞、解耦、异常兜底。**判定可行,进入实现。**

## 实现清单
- 新增 `scripted_audio.py`(`ScriptedAudioInput` + `load_scenario` + `_read_wav_mono`)。
- `turn_metrics.py`:新增 `CoverageDetector` + `TurnMetrics.set_expected()`。
- `web_ui_agent.py`:session.start 后、recorder 前注入;读 `expect` → set_expected;`_turn_metrics` 初始化为 None。
- `.env.example`:登记 `AGENT_SCENARIO`。

## 实现摘要(本次落地的文件)
- 新增 `examples/voice_agents/scripted_audio.py`:`ScriptedAudioInput` + `load_scenario` + `_read_wav_mono` + `_resolve`(相对路径回退仓库根)。
- `examples/voice_agents/turn_metrics.py`:新增 `CoverageDetector`(LCS 覆盖率)+ `_lcs_len` + `TurnMetrics.set_expected()`,并入 `_detectors`。
- `examples/voice_agents/web_ui_agent.py`:`session.start` 后、recorder 前注入(`AGENT_SCENARIO`);读 `expect`→`set_expected`;`_turn_metrics` 预置 None。
- `.env.example`:登记 `AGENT_SCENARIO` 用法。
- 影响面:不设 `AGENT_SCENARIO` → 零行为变更;注入失败 → 自动退回正常麦克风。

## 自测结果(全部通过)
- `py_compile` 三文件通过;`ruff check` All passed;`ruff format` 已规范。
- ScriptedAudioInput 冒烟:
  - 立体声 24k wav 读取并**下混单声道**正确(12000→12000 单声道样本);
  - 帧计划 = lead(400) + wav(50) = 450,末帧补零;
  - **真实节奏**:连取 5 帧实耗 0.051s ≈ 5×10ms,且 await 非阻塞;
  - wav 放完后**持续吐全静音帧**(不结束迭代)。
- 路径解析:cwd 在 `examples/voice_agents` 时,`runs/<ts>/user.wav` 经 `_resolve` 正确回退到仓库根绝对路径。
- CoverageDetector:expect="我想让你讲一个长一点的故事"(13)、识别="我想让你讲故事"(7)→ coverage=0.538(LCS=7/13),`_lcs_len("abcde","ace")=3`。

## 手测步骤(请用户验证)
前提:阶段0 的 `feat/turn-tuning-phase0` 已合入(KPI 仪表盘);本机有可回放的录音(如 `runs/20260622_181317/user.wav`)。

1. 在 `.env` 末尾加一行(指向要复现的录音;路径相对仓库根即可):
   ```
   AGENT_SCENARIO=runs/20260622_181317/user.wav
   ```
   (可选:做成 json 以带 expect 算覆盖率:`AGENT_SCENARIO=scenarios/s1.json`,内容
   `{"wav":"runs/20260622_181317/user.wav","expect":"<你那段话的文字>"}`)
2. `.\stop_agent.cmd ; .\start_agent.cmd`(默认带 -Test)。
3. **不用说话**:开场白后约 4s,注入音频自动按真实节奏回放。观察网页 live 气泡 + 小歌行为。
4. `.\stop_agent.cmd`。
5. 看本次 `runs/<新ts>/`:
   - `user.wav` 应 == 注入的那段(回放被如实录下);
   - `turn_kpis.json` 看 `over_segmentation / double_reply / felt_latency`(有 expect 还有 `coverage`)。
6. **可复现性验证**:改 `.env` 里 `TURN_*` 参数,**用同一个 AGENT_SCENARIO 再跑**,对比 `turn_kpis.json` —— 这就是阶段2/3 A/B 与扫参的基础。

预期:本阶段只交付"可复现注入 + 覆盖率度量",**不改判停/STT 行为**;用它复现 181317 应能稳定重现"过度切分/丢字"的 KPI 数字。

## 手测结果(用户跑 + 我复核,2026-06-23 run 144940)
注入 `runs/20260622_181317/user.wav`,对照原始现场 181317:
- **注入生效**:`SCENARIO_INJECT` 已记录,产出 3 轮(非空跑)。
- **回放忠实**:识别出的特征文本与现场高度一致(含"我前面说了一大段话被你丢掉了""学习的成绩…""来帮助…""我们班现在有二十…");录制 user.wav 同为 24000Hz 单声道,335s = 4s lead + 72s 注入 + 余下静音(录到停止)。
- **可复现(核心)**:结构性 KPI 完全一致——user_turns=3、suspected_splits=1、by_missing_speech_window=1、double_reply.fragments=0;felt(1472 vs 1603ms)/eot(497 vs 539ms)仅运行间正常抖动。
- coverage `enabled:false`(未给 expect,可选)。
- 诚实说明:首条 final 顺序两轮略不同(离线STT 对超长段"空/非空"的服务端非确定性 + 判停计时抖动);**结构指标一致,调参对照足够**。

**结论:阶段1 达标——可复现注入 + KPI 度量就位,181317 已成为稳定回归用例。**

## 阶段1 状态:实现+自测+手测均通过 → 已合入 main(PR #8)。

---

# 阶段 2:上半段——流式主STT(根治丢字 + 显示同源)

## 2a 探活(2026-06-23)
TCP 连通性:
- `qwen3-stream` 10.212.164.230:10091 → **不通**(TimeoutError,内网地址)。
- `qwen3` 60.205.197.165:10091 → ConnectionRefused(端口未服务)。
- `funasr`(现用)60.205.197.165:10090 → OK 75ms。
- **讯飞 RTASR** rtasr.xfyun.cn:443 → OK 112ms。
→ 按降级链:qwen3-stream 不通 ⇒ **选讯飞 RTASR**。

## 2a 讯飞协议 + 真服务验证(用本机 .env 密钥,已通过)
- URL:`wss://rtasr.xfyun.cn/v1/ws?appid=&ts=&signa=`(signa 需 URL 编码)。
- signa = `base64(hmac_sha1(key=APIKey, msg=md5_hex(appid+ts)))`。
- 音频:PCM 16k/16bit/单声道,**1280 字节/帧、~40ms 间隔**,裸二进制发送;结束发文本 `{"end":true}`。
- 结果:`{action:"result", data:"<json串>"}`;`data.cn.st.type`:`"0"`=final / `"1"`=partial;
  文本路径 `data.cn.st.rt[].ws[].cw[].w` 拼接;`action:"started"`=鉴权成功。
- **验证**:发 16.8s 中文(KWS 测试 wav)→ started + 7 条 final、79 字忠实转写。鉴权/流式/解析全通。

## 2b 设计(讯飞流式主STT 接入)+ 自审
**provider**:`IFlyTekRTASR(stt.STT)`,capabilities streaming=True / interim_results=True;
`stream()`→ RecognizeStream:① 发送任务:输入帧→重采样16k单声道→攒1280字节→~40ms 节奏发,
会话结束发 `{"end":true}`;② 接收任务:解析 → 发 `INTERIM_TRANSCRIPT`(type1)/`FINAL_TRANSCRIPT`(type0)。
**整流式**:一条 WS 贯穿整轮会话(非每句新建),按 seg_id 出多条 final;判停仍由 AgentSession 的
VAD + turn detector 负责(只是主STT 换成流式、**不再过 StreamAdapter**)。
**接入**:启动期模式选择(`STT_BACKEND=iflytek` → session.stt=IFlyTekRTASR、跳过 StreamAdapter;
否则走现有离线路径),**保留离线为回退**;先不热切换(流式/非流式接口差异大,降风险)。
**显示同源**:后续用框架 `user_input_transcribed(is_final=False)` interim 驱动 live 气泡,在线2pass 降级 fallback。

**自审(从严)发现/对策**:
1. 15s 无音频会被服务端断开 → 会话中持续发帧(含静音)即可不触发;麦克风静音态需照发静音(注意)。
2. RTASR 是**整段连续流**:WS 须贯穿整轮会话、用完才 `{"end":true}`;长会话可能需重连(列为风险,先不做,断了降级离线)。
3. 发送过快会引擎报错 → 严格 ~40ms/1280 字节节奏,加节流。
4. 付费云服务 + 可能 IP 白名单(本机已验通)→ 失败要优雅降级离线,绝不卡死主流程。
5. 接口契合:须实现 livekit `stt.RecognizeStream`(_run/_event_ch/SpeechEvent),落地前对齐基类。
6. A/B 依赖注入录音 —— **但 runs/ 录音当前为空(见下),需先恢复一条长独白用例**。

## ⚠️ 数据问题:runs/ 目录为空
2026-06-23 复核时发现 `runs/` 下所有 run(含手测的 144940、181317)**已不存在**(listdir=0)。
我方未执行任何删除;与"不删 runs/recordings"约束相关,**待用户确认是否自行清理**。
影响:阶段2 A/B 计划用的 `181317/user.wav` 回归用例丢失 → 需重录一条长独白(或改用 recordings/ 旧的
conversation.wav)。

## 2c 实现
- 新增 `examples/voice_agents/iflytek_stt.py`:`IFlyTekRTASR(stt.STT)` + `_IFlyTekStream(stt.RecognizeStream)`。
  一条 WS 贯穿整流;发送任务攒 1280 字节 ~40ms 节流推、输入结束发 `{"end":true}`;接收任务把
  result 转 `INTERIM/FINAL` SpeechEvent。`sample_rate=16000` 由基类自动重采样。异常上抛交基类重试。
- `web_ui_agent.py`:`STT_BACKEND=iflytek` → 启动期用 IFlyTekRTASR、**不过 StreamAdapter**、
  `_switchable_stt=None`;否则走原离线路径(保留回退)。prewarm 加 `hasattr` 守卫。
- `.env.example`:STT_BACKEND 增 iflytek + 讯飞密钥占位(真值在本地 .env)。

## 2c 自测(全部通过)
- `py_compile` / `ruff` / `format` 全绿。
- **provider 真服务自测**:用 livekit `push_frame` 把 KWS 中文 wav(zh_0,5.6s)喂进
  `IFlyTekRTASR().stream()` → 8 interim + 3 final、转写忠实("对我做了介绍啊。那么我想说的是呢…")。
  证明 push_frame→重采样→WS→讯飞→SpeechEvent 端到端通。
- **集成自测**:`STT_BACKEND=iflytek` 构建 streaming/interim=True、offline=False、不过 StreamAdapter;
  默认 `funasr` 模式 `build_stt()` 仍正常。不设 iflytek 即零影响。

## 2c 已知限制(诚实记录)
- iflytek 模式下面板 ASR **热切换不可用**(改 .env 重启切换);WS 状态栏可能仍显示 "FunASR"(`_switchable_stt=None` 的占位,cosmetic)。
- 显示仍走在线2pass(**显示同源**留到 2d:改用框架 `user_input_transcribed` interim 驱动 live 气泡)。
- 讯飞为付费云服务 + 需联网(本机已验通);断连由基类重试,失败应降级离线(暂未做自动降级,先靠手动改回 funasr)。
- 长会话/15s 无音频:会话持续推帧(含静音)可避免;长会话重连为已知风险点,后续观察。

## 手测步骤(请用户验证:换讯飞后"丢字"是否根治)
1. `.env` 设 `STT_BACKEND=iflytek`(讯飞密钥已在本地 .env)。
2. `.\stop_agent.cmd ; .\start_agent.cmd`(带 -Test)。
3. **live 说那段会丢字的长独白**(像 181317 那样,中间带停顿)。重点看:
   - 小歌是否**完整接住**你说的内容(不再"我前面说的被你丢了");
   - `runs/<新ts>/` 的 `user.wav` 是你说的;`turn_kpis.json` 看 `over_segmentation`(切分,Phase 3 才调)、`felt_latency`。
4. **(可选)严格注入 A/B**:用同一段录音分别跑
   `STT_BACKEND=funasr` 与 `=iflytek`(都设 `AGENT_SCENARIO=runs/<ts>/user.wav`),对比 KPI/识别完整度。
5. 不满意可随时改回 `STT_BACKEND=funasr` 回退。

> 预期:讯飞流式应**根治"超长段丢字"**(无 VAD 硬切段、无离线超时空丢弃);**切分(回多次)仍存在**——那是 Phase 3 判停时序的事,不在本阶段。

## 阶段2 状态:provider 实现 + 自测(含真服务)通过 → 待用户手测确认 → 通过后提交合入。

---

# 阶段 3:下半段——判停时序调优(进行中)

## 现象与根因(基于 iflytek 注入实测)
- 讯飞已**根治丢字**(内容接全),但**仍切多轮/回多次**。
- 数据:iflytek 注入跑 `eot_delay 中位≈460ms`(= min_delay,**没走 max_delay**)→ turn detector 把"讯飞按停顿切出的 final"判成"说完了"→ 立即提交。**所以 max_delay 不是 iflytek 的杠杆。**
- 与离线不同:离线时 eot 顶在 max_delay(detector 判"没说完");iflytek 下 detector 判"说完"。

## 自测发现(从严,两次自动调参均不成立)
1. `TURN_ENDPOINT_MAX_DELAY=1.5`:切分 4/6,**没降**(eot 仍 460ms,未走 max)。
2. `TURN_VAD_MIN_SILENCE=0.8`(换讯飞后调大 VAD 已安全,不再丢字):切分 3/5,**仍没明显降**;felt 中位 8s、eot/felt p90 10–12s。
3. **关键结论:注入回放不适合调"判停时序"。** 注入停顿是固定的,而真实判停依赖"用户看小歌反应实时调整停顿";注入时小歌在播报、注入仍硬推 → 时间线失真、KPI 充满 8–12s 离群值,**不可用于时序调参**。
   - 注入对**丢字(内容、确定性)**可靠 ✓;**判停时序必须 LIVE 真人调**。

## 决定(修正方法论)
- 判停旋钮已就位:`TURN_VAD_MIN_SILENCE / TURN_ENDPOINT_MIN_DELAY / TURN_ENDPOINT_MAX_DELAY / TURN_UNLIKELY_THRESHOLD / TURN_PREEMPTIVE_TTS`(均在 turn_config.py + .env)。
- 新增 `unlikely_threshold` 旋钮并接入 `MultilingualModel(unlikely_threshold=...)`(已 compile/lint 通过)。
- **判停调参改为 LIVE 手测驱动**(用户真实说话 + turn_kpis 看 over_segmentation/felt),迭代旋钮值;注入仅用于丢字回归。
- 当前 .env 起点(供 LIVE 手测):`vad=0.8, min_delay=0.3, max_delay=1.2, unlikely=0.5, preemptive=false`。

## 阶段3 状态:旋钮就位 + 方法论修正(注入→LIVE);**待用户 LIVE 手测调参**。

---

# 设计评审收尾 → 进入开发(optimized 栈)

- **设计文档 `TURN_STT_DESIGN.md` 经四轮评审放行**(评审记录见 `TURN_STT_DESIGN_REVIEW.md`):延迟取舍/流式静音(B 真关麦)/2pass 重连/preemptive/防幽灵/双VAD/上游零改 全部收敛;剩余仅 LIVE 调参。
- 开发顺序(评审建议):① `funasr_stream_stt.py`(GAP 聚合 + VAD 输出门控)② 静音门 ③ 注入 A/B 验丢字/覆盖率 ④ LIVE 调判停。`XIAOGE_STACK` 默认 `upstream`,LIVE 充分验证后再翻 optimized。

## 开发 · Step 1:funasr_stream_stt.py(核心模块)— 已实现 + 自测通过
- 新增 `examples/voice_agents/funasr_stream_stt.py`:FunASR 2pass 流式 + **内置独立 silero VAD 输出门控防幽灵** + **GAP 轮次聚合**(以最后有声帧起算,静默≥GAP 发一条 FINAL);livekit `RecognizeStream(sample_rate=16000)`;零改上游。
- 自测:`py_compile`/`ruff` 通过;**真服务标准自测**——push 2.5s 语音 + 1.8s 尾随静音,得 3 条 INTERIM(边长)→ 静默≥GAP(1.0s)后**聚合出 1 条 FINAL**("对我做了介绍啊那么"),证明"流式识别 + 门控 + 一轮一 FINAL"成立。
## 开发 · Step 2/3a:静音门 + funasr-stream 接线 — 已实现 + 自测通过
- 新增 `mute_gate.py`:`MuteGate`(io.AudioInput),最内层包裹,`muted` 时输出等长静音帧。自测:直通非零、muted 全零且等长。
- `web_ui_agent.py`:
  - STT 选择加 `XIAOGE_STACK`(upstream/optimized)+ `STT_BACKEND=funasr-stream` 分支(用 FunASRStreamSTT、不过 StreamAdapter、`_switchable_stt=None`);默认仍 funasr(upstream),零行为变更。
  - session.start 后(injection 之后、recorder/KWS/online 之前)插 `MuteGate` 为**最内层**;`/api/mic` 改走静音门(关麦=真关麦:全链路收静音 + 录音暂停);WS 初始 muted 状态读静音门。
- `.env.example`:登记 `XIAOGE_STACK` / `funasr-stream` / `XIAOGE_AGG_GAP`。
- 自测:py_compile / ruff / format 通过;集成构建 OK。

## 开发 · Step 3b:显示同源 — 已实现 + 自测通过
- `live_transcript.py`:加 `feed_full(text)`(主STT 原生 interim 是全量文本,直接置换显示)。
- `web_ui_agent.py`:`_live_from_main = _stt_mode in {funasr-stream, iflytek}`;流式后端用主STT interim 驱动气泡(`_on_stt` 非 final 分支),`_online_text_fanout` 停止用在线2pass 喂气泡(免双驱动);在线2pass tap 仍保留作打断。
- 自测:py_compile/ruff 通过;feed_full 单气泡全量置换 OK。

## 开发 · 修复1:显示累积滞后(LIVE 手测发现)— 已定位 + 修复 + 延迟自测通过
- **现象**(用户手测):ASR 显示远远跟不上说话节奏,说得越久越落后。
- **证据**(`runs/20260624_165637`,mode=funasr-stream):45s 长故事 `transcription_delay=8375.9ms`、`end_of_turn_delay=8702.9ms` → 滞后随时长累积。
- **根因**:`_forward` 加了 `_SEND_INTERVAL=0.05` 的 sleep 节流,强制每块 ≥50ms + 每块开销 → **送音频慢于实时 → backlog 持续累积**。对照在线tap(`online_interrupt.py:166-167`)是队列即取即发、**无节流**。
- **修复**:`_forward` 改为**实时即送**(帧到即 `send_bytes`,去掉 buf/分块/sleep);`chunk_size` [5,10,5]→[5,8,4](480ms,与tap一致,更跟手)。
- **延迟自测**(22s 连续语音,实时喂):`last_interim` 落后说完 **-0.43s(≈0,不累积)**;旧代码此处会落后数秒。
- 教训:自测必须含**节奏/延迟**项,不能只验正确性(上次漏了,LIVE 才暴露)。

## 修复1 已合入 main(PR,cbcb0aa)。分支 feat/turn-stt-optimized 保留。

## LIVE 手测2 分析(`runs/20260624_172809`,修复1 之后)
- ✅ **显示滞后已根治**:`transcription_delay` 中位 **1010ms 且恒定**(17.5s 长句 965ms,不累积);felt 中位 2018ms(合设计预期);长连续句完整不丢字。
- ❌ **新暴露:判停过切**(backlog 修掉后才显形):`turn_kpis` 过切率 **25.8%**(24/93,by_small_gap)、残片回复 **8**、false_interruption 2、在线打断频繁(短词即打断)。实锤:用户说"你听不懂中文吗""你是搞笑吗""为什么停了呢";小歌把被切碎的半句当字面意思答非所问、两次道歉重启。
- 根因:① GAP=1.0 对带思考停顿/重复的口语过切;② 在线2pass 软打断只看文本无 VAD 佐证 → 短幽灵/接话误打断。

## 开发 · 修复2:判停过切 + 在线打断防幽灵(分支 feat/turn-judou-tuning)— 已实现 + 自测通过
- **#1 GAP 默认 1.0→1.5**(`funasr_stream_stt.py` + `.env.example`):减少思考停顿被误判一轮结束,代价 felt +~0.5s。
- **#2 在线软打断加 VAD 佐证**(`web_ui_agent.py`):`_online_state` 加 `vad_speaking`/`vad_off_ts`(由 `user_state_changed` 维护);软打断(`meaningful>=min_chars`)前要求 VAD 确认用户此刻在说话或刚停 <`XIAOGE_ONLINE_VAD_GRACE`(默认 0.6s),否则判幽灵→不打断+清累积。STOP-phrase 强打断与 KWS 不变。
- 自测:py_compile/ruff 通过;VAD 佐证逻辑 4 例(说话放行/纯幽灵拦截/刚停0.3s放行/停1.0s拦截)全过;GAP 默认=1.5 确认。
- 待 LIVE:看过切率↓、误打断↓、felt 体感是否可接受;再决定要不要做 #3(语义判停兜底)。

## 待续:#3 语义判停兜底(GAP-FINAL 若话未说完交上游 turn detector 多等)+ 注入 A/B(覆盖率/丢字)。
**optimized 已 LIVE 可测**:流式主STT + GAP 一轮一回复 + 不丢字 + 关麦真关麦 + 显示同源 + 实时不滞后 + 在线打断防幽灵。


---

# 优化方案定稿(阶段2+3 合并,待确认后实现)

> 经多轮讨论收敛。核心:用**流式 FunASR(2pass)做主STT**根治丢字+显示同源;判停的
> "轮边界"裁判从"VAD/上游 endpointing"挪到**我们 STT 层的 GAP 聚合**;并用 **VAD 给流式
> ASR 把关防幽灵**。**上游代码零改**,一切在 examples/voice_agents/,可 `XIAOGE_STACK` 一键 A/B。

## 设计纪律(5 条)
1. 零改上游(`livekit-agents/`、`livekit-plugins/` 不动)。
2. 每个优化可开关、默认保留原行为。
3. `XIAOGE_STACK=upstream|optimized` 一键切换整条 ④ 做 A/B。
4. 判停/聚合逻辑放我们 STT 层,上游 endpointing/turn-detect 不改、只消费我们的事件。
5. 注入测内容/准确度,LIVE 测判停时序;KPI/录音对比。

## 组件职责
| 组件 | 优化版职责 |
|---|---|
| **AgentSession 的 VAD**(`vad=` 传入,上游) | ① 发 START/END_OF_SPEECH 给上游 → **续话取消合并** ② 打断(声学) ③ 用户状态 |
| **我们 STT 内置的 VAD** | ① **防幽灵门控**(静音/底噪不喂 FunASR、不收其文本) ② **量静默驱动 GAP 轮次聚合** |
| **FunASR 2pass(我们的流式主STT)** | 对 VAD 确认的语音连续识别;online=增量(显示)、offline=段尾 |
| **轮次聚合(我们 STT 层)** | 累加本轮文本;**VAD 静音 ≥ GAP 才发一条 FINAL**(=一轮) |
| **上游 endpointing / turn detector**(不改) | 收到我们 FINAL 后:**语义判完句**(EOU 概率)+ **续话合并**(max_delay 窗内 VAD 又开口则取消提交、累加下一条 FINAL) |
| **打断源** | AgentSession-VAD(声学)/ KWS / 在线2pass(**+VAD 佐证防幽灵**) |

> 两处 VAD 都是 silero、读同一路麦克风,行为一致、互不冲突:上游那条管"打断 + 续话取消",我们那条管"防幽灵 + 聚合"。

## 防幽灵(FunASR 偶发"没说话却蹦字")
- 根因:流式 ASR 在静音/底噪上会凭空吐字;原始离线 ④ 因"VAD 先切段"天然带一点防护,换流式后丢了 → 必须补回。
- 主防线:**我们 STT 用 VAD 门控**——VAD 判静音时蹦出的文本一律丢弃(不累加、不计 GAP、不发 FINAL)→ 不会误提交、不会误回复。
- 打断侧:在线2pass 文本打断**加 VAD 佐证**(仅当 AgentSession-VAD 确认在出声才打断)。VAD 打断本身是声学的、天然抗幽灵。
- 兜底:既有附和/停止词过滤继续挡"嗯/啊"类。

## 两层"说完了"判断
1. **声学(我们 GAP,主边界)**:VAD 静音 ≥ GAP → 发一条 FINAL。
2. **语义(上游 turn detector,第二意见)**:读本轮文本判 EOU——
   - 说完了(完整句)→ min_delay → 提交、回复;
   - 没说完(半句/以"然后"结尾)→ max_delay 窗:窗内用户又开口(AgentSession-VAD)→ **取消提交、与下条 FINAL 合并成同一轮**;仍沉默 → 到点照常提交。

## 流程图
```
麦克风(连续音频;已经过 录音/KWS/在线2pass tap)= session.input.audio
   │
   ├─► AgentSession VAD(上游,独立读麦克风)
   │        ├─ START/END_OF_SPEECH ─► 上游 audio_recognition(续话取消 + 打断)
   │        └─ 声学打断 ─────────────────────────────┐
   │                                                  │
   └─► 我们的 funasr-stream STT ───────────────────┐  │
          VAD 判语音?                              │  │
            ├─否(静音/底噪)► 丢弃(防幽灵)        │  │
            └─是► 送 FunASR 2pass(流式)           │  │
                   online=增量 / offline=段尾        │  │
                   ▼                                  │  │
                累加"本轮文本" + 发 INTERIM ──► live 气泡(同源显示)
                   ▼                                  │  │
                VAD 静音 ≥ GAP ?(轮次聚合)          │  │
                  ├─否► 继续累加(同一轮)            │  │
                  └─是► 发【一条 FINAL】+ 清空 ──► 上游 audio_recognition
                                                       │  │
   ┌───────────────────────────────────────────────────┘  │
   ▼ (上游,代码不改)                                       │
 turn detector 读"本轮文本"判 EOU:                          │
   ├ 说完了 ─► min_delay ─► 提交 ─► 附和/停止词过滤 ─► 回复一次
   └ 没说完 ─► max_delay 窗内:
        ├ 用户又开口(AgentSession-VAD START_OF_SPEECH)► 取消提交 + 与下条FINAL合并=同轮
        └ 仍沉默到点 ─► 提交 ─► 回复一次
                                                          │
 打断 agent 播报 ◄── 声学(VAD)/ KWS / 在线2pass(+VAD佐证)┘
```

## `XIAOGE_STACK` 映射(一键 A/B)
| 维度 | upstream(原始,基线) | optimized(我们的) |
|---|---|---|
| 主STT | 离线FunASR+StreamAdapter(VAD硬切) | funasr-stream(VAD门控+GAP聚合) |
| 轮边界裁判 | VAD + 上游 endpointing | 我们 STT 的 GAP(+上游语义兜底) |
| 防幽灵 | VAD切段天然带一点 | VAD 门控显式防 |
| live 显示 | 关 | 开 + 同源(主STT interim) |
| 在线2pass 打断 | 纯文本 | +VAD 佐证 |
| 上游文件 | 原样 | 原样(零改) |

## 旋钮(env)
- `XIAOGE_STACK`(总开关)
- `XIAOGE_AGG_GAP`(GAP 聚合静默阈值,默认 1.2–1.5s)= 连续/新轮容忍窗
- STT 内 VAD 门控阈值(activation / min_silence)
- `TURN_ENDPOINT_MIN/MAX_DELAY`、`TURN_UNLIKELY_THRESHOLD`(上游语义兜底)
- 在线2pass `min_chars`
- 粒度 env 覆盖 profile,便于精细 A/B

## 实现范围(确认后)
- 新增 `funasr_stream_stt.py`:内置 VAD 门控 + 2pass 流式识别 + GAP 轮次聚合,发 INTERIM/FINAL(复用已验证的 2pass 协议)。
- `web_ui_agent.py`:`XIAOGE_STACK`/`STT_BACKEND=funasr-stream` 选用、不过 StreamAdapter、**继续传 `vad=`**;live 气泡改主STT 同源;在线2pass 打断加 VAD 佐证。
- `iflytek_stt.py` 保留为可选第三方(默认不用)。
- 离线原始 ④ 全程保留(`XIAOGE_STACK=upstream` 可切回)。
- 出 SVG 流程图入 diagrams/。

## 待确认(讨论点)
1. 两层判停(GAP 声学 + 上游语义兜底 + 续话合并)模型是否认可?
2. 两处 VAD(上游打断/续话 + 我们门控/聚合)是否接受?(可否合一另议)
3. `XIAOGE_AGG_GAP` 起点 1.2–1.5s、`max_delay` ~0.8–1.0s 是否合适?
4. 主STT 用 funasr-stream(不用讯飞)是否拍板?(实测它对你的音频更准 + 免费 + 同源)

---

# 开发 · #3 语义判停兜底(自适应 GAP)— 分支 feat/turn-semantic-endpoint

## 背景
LIVE 手测多版后:过切已 0%、显示跟手,但 GAP 固定 1.5s 使 felt 中位 ~2.4s。目标:felt→~1.9s 且**过切不退化**。

## 方案(自审从严后)
`funasr_stream_stt._gap_watchdog` 改双阈值:
- `GAP_MIN`(新,`XIAOGE_AGG_GAP_MIN` 默认 0.8s):句子"像说完"时静默达此值即提交(快)。
- `GAP_MAX`(= `XIAOGE_AGG_GAP` 1.5s):兜底上限,任何情况静默满 MAX 必提交。
- `_looks_complete(text)`:结尾 `。！？…` 或句末语气词(了/吧/吗/呢/啊/嘛/呀/哈)→ True;
  句中标点(，、；)/连接词悬词(然后/因为/的/把/这个/嗯…)/无明显信号 → False(保守等满 MAX)。
- `_commit_ready(silence,pending,min,max)`:≥MAX 必发;[MIN,MAX) 仅 `_looks_complete` 才发。
- `GAP_MIN ≥ GAP_MAX` → 退化为恒定 GAP(=一键关)。零改上游。

## 自审结论
- 过切不退化:不确定一律等 MAX,= 现状;只对"明显说完"的句子提速。
- FunASR 2pass-offline 带标点(实证),完成信号可靠。
- 残余风险:"对。然后…"在句号处提前切——但句号后静默 0.8s 多为真边界 + 上游 cancel-on-resume 兜底 + 可调 GAP_MIN。

## 自测(通过)
- 单测 `_looks_complete`:5 完成句全 True、7 未完成句全 False。
- 单测 `_commit_ready`:<MIN 不发 / [MIN,MAX)完成发未完成等 / ≥MAX 必发 / GAP_MIN≥MAX 关闭分支 —— 全过。
- 旋钮接线:GAP_MAX=1.5 / GAP_MIN=0.8。
- 真服务 smoke:端到端 1 条 FINAL,无崩溃(测试音尾"…想说"非完成信号→正确等满 MAX)。
- py_compile / ruff 通过。
- `.env.example` + 本机 `.env` 登记 `XIAOGE_AGG_GAP_MIN=0.8`。

## 待手测
LIVE 真人:句子以 。/语气词 收尾的轮次应"说完更快回"(felt↓),带停顿/连接词的轮次仍不被切。
felt 体感、过切率与上版对比。可调 `XIAOGE_AGG_GAP_MIN`(0.7~0.9)。

---

# 开发 · 气泡顺序两 bug 修复(LIVE 手测 runs/20260625_131151 发现)

## 现象(用户手测,快速连续说话)
- Bug1:答完立刻说下一句,我的新气泡跑到小歌上一条回复**之上**。
- Bug2:一直说,气泡涨着突然"很多内容消失"→ 续说进一个**小气泡**→ 停下后消失的+小气泡内容又**合并成一个大气泡**。

## 根因(都在前端 live 气泡,与判停/STT 无关)
- 时间线实证:`open=37 > 用户轮=32`;partial 文本几乎不回落(非文本缩水)。
- Bug1:助手气泡在 **LLM 生成完成**才广播(`transcription_node`);用户 live 气泡**一开口(VAD)即建**并 `appendChild` 到底 → 抢答时用户新气泡先落底、助手随后落到其下。
- Bug2:live 气泡开/关由 VAD 事件(`user_speaking`→`startLive`→`discardLive`)驱动,与主STT 流两条线;中途一个多余"开"把已涨大的气泡清空(消失),新 partial 进小气泡,整轮单 final 定稿时全量回填(合并)。

## 修复(纯前端 web_ui_agent 内嵌 JS,解耦零后端)
- Bug1:`addMsg` 收到 `assistant` 且有 live 气泡时 `insertBefore(liveBubble)` → 顺序恒为 [用户上轮][小歌回复][用户进行中]。
- Bug2:`startLive` 幂等 —— 已有正在涨的气泡则续用、不清空重建;气泡只由真正 final 收尾。

## 自测(通过)
py_compile / ruff / `node --check`(JS 语法)通过;HTML 标签全闭合;16 个 JS 函数完整;两处修复标记在位。
#3(自适应 GAP)本次**保留不动、不合**,待气泡修好后干净复测再定去留。

## 待手测
快速连续说话:① 新气泡不再跑到小歌回复上面;② 长说话气泡不再中途消失/分裂。

## 追加 · 后端根因修复(runs/20260625_140017 复测发现:open=75>用户轮=65,10 次中途多余"开")
- 根因:`live_transcript` 的"新轮 gap"按 interim 文本到达时刻算;funasr-stream 说话中途 FunASR 偶尔 >1.5s 不吐字,被误判新轮 → 后端多发"开"。
- 修复:`feed_full` 改为**只在未开时开一次、不按 gap 中途重开**(轮边界由主STT 真 final→`_close` 决定);`feed_online`(旧在线2pass,无 final、确需 gap 兜底)**不动**。
- 无副作用论证 + 自测:feed_full 中途模拟 5s 不吐字仍只 1 次"开"、真 final 后正常重开(2 次);feed_online gap 重开兜底保留(2 次)。py_compile/ruff 通过。与前端 startLive 幂等形成双保险。
- 注:气泡两 bug 为浏览器 DOM 行为,日志不可直接证实;本轮用户手测"不明显/暂无法确认",根因修复后理应消除。





