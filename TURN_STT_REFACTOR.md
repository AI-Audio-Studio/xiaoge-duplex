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

## 阶段1 状态:实现+自测+手测均通过 → 提交合入(review-before-merge)。

