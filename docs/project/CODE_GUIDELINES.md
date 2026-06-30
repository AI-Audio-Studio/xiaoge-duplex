# 代码规范(本项目自有代码)

> 目标:文件不过大、函数不过长、模块解耦。**仅约束我们自己写的代码**,**不适用于 livekit 母体工程**(`livekit-agents/`、`livekit-plugins/`)与上游示例文件。
> 数字是"闻味器"不是 KPI——**内聚 > 行数**;超标先问"是不是职责多了",而不是"怎么把行数压下去"。
>
> **状态(2026-06-30,经三轮评审批准定稿)**:量化阈值与解耦原则**即刻生效**;其**自动强制机制**(单一清单 `ourcode.txt`、函数级 `# noqa` 棘轮、CI 严格步、ruff 版本锁定)在配套**实现 PR** 中落地——见 §7。本文末附三轮"评审—回应"往返存档。

## 1. 适用范围 / 约束索引

- **约束**:本项目为这个全双工语音引擎新写的应用与工具代码。
- **不约束**:`livekit-agents/`、`livekit-plugins/`(开源母体,不改不约束)、`examples/voice_agents/` 下的**上游示例**(`basic_agent.py`、`multi_agent.py`、`weather_agent.py` 等)。

> 母体与我们的代码在 `examples/voice_agents/` 下**交织**,且整库由同一账号导入(git 作者无法区分),所以范围只能用**显式清单**界定。

**权威清单 = `ourcode.txt`**(单一来源,纯路径一行一个;`makefile` 与 `ci.yml` 都读它——实现 PR 落地,落地前暂存于 `makefile` 的 `OUR_CODE`)。下面镜像一份便于阅读(含职责标注;改动以 `ourcode.txt` 为准):

```
examples/voice_agents/
  web_ui_agent.py            主应用(入口)
  listening_mode.py          聆听模式状态机
  mute_gate.py               真关麦门
  text_sanitizer.py          LLM 文本净化
  live_transcript.py         Web 实时转写气泡
  turn_config.py             判停参数集中
  kws_interrupt.py           KWS 强打断
  online_interrupt.py        在线 2pass 抢断
  funasr_stream_stt.py       FunASR 流式主 STT
  iflytek_stt.py             讯飞 RTASR
  custom_audio_providers.py  STT/TTS 适配器
  audio_recorder.py          正常模式录音
  test_recorder.py           测试多轨录音
  event_timeline.py          测试时间线
  turn_metrics.py            判停 KPI
  scripted_audio.py          录音回放注入
  probe_funasr_2pass.py      FunASR 探针
  qwen_funasr_bailian_voice_agent.py / qwen_gateway_console_agent.py
  kimi_console_agent.py / nvidia_test.py    控制台 Agent(本项目)
```

**新增自有文件时**:把路径加进 `ourcode.txt`(一行一个),即自动纳入约束。

## 2. 量化标准(软目标 / 硬上限)

| 维度 | 软目标 | 硬上限 | 强制方式 | 通行度 / 出处 |
|---|---|---|---|---|
| 单文件行数 | ≤ 400 | ≤ 500 | review 把关(ruff 无此规则) | 工程约定(无统一标准,Python 生态偏小) |
| 单函数/方法行数 | ≤ 40(一屏) | ≤ 75 | review 把关(由语句数近似兜) | 约定 |
| 圈复杂度(独立路径) | ≤ 8 | ≤ 10 | **ruff `C901`** | **强共识**:McCabe(1976)+ 几乎所有 linter 默认 10 |
| 分支数 | — | ≤ 12 | **ruff `PLR0912`** | pylint 默认 `max-branches=12` |
| 函数参数 | ≤ 4 | ≤ 5 | **ruff `PLR0913`** | pylint 默认 `max-args=5` |
| 函数语句数 | — | ≤ 50 | **ruff `PLR0915`** | pylint 默认 `max-statements=50` |
| return 数 | — | ≤ 6 | **ruff `PLR0911`** | pylint 默认 `max-returns=6` |
| 嵌套层级 | ≤ 3 | ≤ 4 | review 把关 | 约定;用早返回(卫语句)压平 |
| 类 | ≤ 200 行 / ≤ 12 公有方法 | ≤ 300 行 | review 把关 | 经验值,SRP 为纲 |
| 行宽 | — | 100 | ruff(`pyproject.toml`,全仓) | 本仓既有 |

> 注:**"ruff"行 = 工具自动检**(`ruff-ours.toml`,经 `make lint-ours`/CI 强制,见 §4);**"review"行 = 人工把关**(ruff 不检)。`C901`(圈复杂度=独立路径数)与 `PLR0912`(分支语句数)是两个度量,故分两行。**复杂度/参数/语句数有学术与工具默认背书,优先于行数**;**文件/函数行数是弱指标**(实证上对缺陷预测力弱),与复杂度一起看。

## 3. 解耦原则(比行数更重要)
1. **单一职责**:一个模块只回答一件事。样板:`mute_gate.py`(32 行,只管关麦)、`text_sanitizer.py`(只管净化)、`turn_config.py`(只管判停参数)。
2. **纯逻辑与 I/O 分离**(本仓默认范式):`listening_mode.py` 是纯同步状态机(无 asyncio/IO),host(`web_ui_agent.py`)负责喂事件与 I/O ⇒ core 可单测、host 薄接线。
3. **依赖单向、无环**:同层模块只用对方**显式公共 API**(私有加 `_` 前缀),不互相 import 内部细节。
4. **配置集中**:走 dataclass + `from_env()`(如 `TurnConfig`/`ListeningController`),不散落 `os.getenv`。

## 4. 如何强制
- **工具**:`ruff-ours.toml`(`extend = "pyproject.toml"`)对 `ourcode.txt` 列出的文件加严 §2 的 5 条 ruff 规则(`C901/PLR0912/PLR0913/PLR0915/PLR0911`)。
- **真门禁 = CI(B2)**:`ci.yml` 增一步 `ruff check --config ruff-ours.toml $(cat ourcode.txt)`——push main / PR 触及自有代码即强制(现有 CI 触发路径已覆盖)。**约束力来源于此。**
- **本地 DX(B1)**:`make lint-ours` 一条命令本地自查,可并入 `make check`。但**现有 CI 不调 `make check`,故 B1 不替代 B2**。
- **版本一致**:`pyproject.toml` 钉 `ruff==0.15.18`(与 CI 同版本)并 `uv lock` 固化,避免"本地绿 / CI 红"。
- **隔离**:全仓 `make check`/CI 的宽松 `ruff check` **不变**,母体 `livekit-*` 照常通过;加严只作用于 `ourcode.txt` 内文件。

## 5. 历史挂账与棘轮(重构待办)
以下既有文件当前超标,先挂账让门禁为绿、**只拦新增违规**;重构达标后撤挂账即收紧。

**挂账粒度(A1)**:在**具体超标函数的 `def` 行**打 `# noqa: <规则>`(如 `# noqa: C901,PLR0915`),**不用整文件 `per-file-ignores`**——整文件豁免会连同文件内**新写**的超标函数一起放过("只拦新增"对挂账文件失效);函数级 noqa 才能让同文件新函数仍受检。

**集中台账**:函数级 noqa 散落各处,故保留此表作"一眼看全欠债"的人读清单,并以 `grep -rn noqa $(cat ourcode.txt)` 兜底审计。

| 文件 | 挂账规则 | 优先级 |
|---|---|---|
| `web_ui_agent.py` | C901, PLR0915, PLR0912 | **高**(2100+ 行"上帝文件",建议按职责拆:Web 面板/事件钩子/STT-TTS 构建/tap 装配) |
| `custom_audio_providers.py` | C901, PLR0913 | 中(多 Provider,可拆文件) |
| `probe_funasr_2pass.py` | C901, PLR0912, PLR0915 | 低(探针工具) |
| `kws_interrupt.py` | PLR0911 | 低(单函数 return 偏多) |
| `turn_metrics.py` | C901 | 低 |
| `qwen_funasr_bailian_voice_agent.py` | C901, PLR0915 | 低(控制台 Agent) |

**收紧(ratchet)**:重构某函数达标后删其 `# noqa` → CI 继续绿,该处此后不允许回退。

## 6. 一条总纲
**内聚 > 数字。** 为凑行数把一段高内聚逻辑硬切成多个只调用一次的碎函数,比一个 60 行的清晰函数更糟。阈值用来"触发一次审视",不是用来"达标交差"。

## 7. 实施状态与计划

本规范的**阈值/原则/范围已定稿生效**。下列**自动强制机制**经三轮评审批准,在**独立实现 PR** 中落地;落地前以现状运行(`makefile:OUR_CODE` + `ruff-ours.toml` 整文件挂账 + 手动 `make lint-ours`):

1. **单一来源 `ourcode.txt`**(纯路径;`makefile`/`ci.yml` 都读;并加进 `ci.yml` 的 `paths:` 过滤)。
2. **B2 真门禁**:`ci.yml` 增严格 ruff 步 `ruff check --config ruff-ours.toml $(cat ourcode.txt)`。
3. **A1 棘轮**:整文件 `per-file-ignores` → 函数级 `# noqa`(保留 §5 台账 + `grep` 兜底)。
4. **ruff 版本锁定**:`pyproject.toml` 钉 `ruff==0.15.18` + `uv lock` 固化。
5. **C/D/小问题文字**(§2 强制方式列、`C901` 术语与 `PLR0912` 行、`mute_gate.py` 32 行)——**本次已落正文**。

**验收**:`make lint-ours` 绿 + 触发一次 CI 通过。

> 以下为定稿前的"评审—回应"往返存档(三轮),保留作决策依据,不属规范正文。

---

# 评审意见(评审员追加,2026-06-30)

> 评审视角:软件研发技术专业角度,核对"是否符合业界规范 / 是否符合项目实际",并指出需设计者澄清之处。已逐条对照 `makefile`、`ruff-ours.toml`、`pyproject.toml` 与实际文件核实,非臆测。**仅评审,未改任何代码/配置。**

## 一、总评:**质量高,既符合业界规范,也符合项目实际,可作为团队规范落地。**

少见地做到了"文档与工具链一致"——文中每条量化阈值都能在 `ruff-ours.toml` 找到对应、`OUR_CODE` 清单与 `makefile` 逐项吻合、引用的文件体量也与现状相符。**认知诚实度尤其突出**(区分"强共识的复杂度指标"与"实证弱相关的行数指标"、点名 McCabe(1976)、明说"行数是闻味器不是 KPI"),高于多数把"拍脑袋数字当圣旨"的团队规范。下面的问题都不动结论,只是让规范更严密、更可执行。

## 二、业界规范符合性:✅ 机器强制的规则就是业界默认值

逐条核对 `ruff-ours.toml`,**机器强制的 5 项全部等于 pylint / McCabe 的社区默认**,无一拍脑袋:

| 文档声明 | 实际配置(`ruff-ours.toml`) | 业界基准 |
|---|---|---|
| 圈复杂度 ≤ 10(C901) | `max-complexity = 10` | McCabe(1976)+ 几乎所有 linter 默认 ✅ |
| 参数 ≤ 5(PLR0913) | `max-args = 5` | pylint 默认 ✅ |
| 语句 ≤ 50(PLR0915) | `max-statements = 50` | pylint 默认 ✅ |
| 分支 ≤ 12(PLR0912) | `max-branches = 12` | pylint 默认 ✅ |
| return ≤ 6(PLR0911) | `max-returns = 6` | pylint 默认 ✅ |

行数/嵌套/类规模等"软指标"无统一国际标准,文档已如实标注"工程约定 / 经验值 / 本仓 review 把关",并未冒充权威——这是正确做法。**结论:符合业界规范,且诚实标注了哪些有学术/工具背书、哪些是约定。**

## 三、项目实际符合性:✅ 清单、挂账、体量全部对得上

- `OUR_CODE`(`makefile:73-85`)与 §1 镜像清单逐项一致;`make lint-ours`(`makefile:87-94`)行为与 §4 描述一致。
- §5 挂账表与 `ruff-ours.toml` 的 `per-file-ignores` **6 个文件逐条吻合**(web_ui_agent=C901/PLR0915/PLR0912;custom_audio_providers=C901/PLR0913;probe=C901/PLR0912/PLR0915;kws=PLR0911;turn_metrics=C901;qwen_funasr…=C901/PLR0915)。
- 体量核实:`web_ui_agent.py` 实测 **2105 行**(文中"2100+ 上帝文件"准确)、`mute_gate.py` **32 行**(文中"33 行"≈)、`listening_mode.py` 249 行(纯状态机范式属实,文件存在)。说明规范是**伴随实现同步维护**的,而非纸面文档。

## 四、需要设计者澄清 / 建议改进(按重要性)

### A.(重要)棘轮的"只拦新增违规"对**挂账文件内部不成立**——file-wide ignore 会把整文件豁免
§5 称"只拦**新增**违规"。但 `ruff-ours.toml` 用的是 **`per-file-ignores`(整文件级豁免)**,且实测 `web_ui_agent.py` 内 **0 处 `# noqa`**。后果:在 6 个挂账文件里**新增**一个 60 分支的复杂函数,`C901/PLR0915/PLR0912` 被整文件忽略 → **照样过闸**。即"只拦新增"仅对**非挂账文件**成立;对挂账文件(恰恰是改动最频繁的入口 `web_ui_agent.py`)的被豁免规则,新违规也漏网。
- **请澄清**:是否意识到 file-wide 豁免的这一漏洞?是否接受"挂账文件内可继续堆复杂度直到整文件重构"?
- **建议**(更紧的棘轮,二选一):
  - (推荐)把整文件豁免**换成在具体超标函数上打 `# noqa: C901` 等**——既绿,又让同文件**新**函数仍受检;
  - 或引入 baseline 机制(只豁免存量、对增量报错)。
- 这是本规范"棘轮收紧"承诺与实现之间**唯一实质性的缺口**。

### B.(重要)`make lint-ours` 未进 `make check`,也未接 CI → 目前是"自觉运行"
实测 `check: format-check lint type-check`(`makefile:105`)**不含 `lint-ours`**;`.github/`、`pre-commit` 中**未检索到 `lint-ours`**。§4 也只是"**建议**纳入提交前/CI"。后果:除非有人手动跑 `make lint-ours`,否则**连非挂账文件的新违规也不会被拦**——规范沦为约定。
- **请澄清**:`lint-ours` 计划在何处强制?(`make check` / pre-commit / CI 任一)在没有强制点之前,本规范的约束力≈口头君子协定。
- **建议**:至少把 `lint-ours` 加入 CI 必经步骤,或并入 `make check`(注意 `check` 现在对全仓宽松、`lint-ours` 仅对 `OUR_CODE`,两者可共存)。

### C.(中)§2 表里约一半指标是**review-only,非机器强制**,但表面看不出来
`ruff-ours.toml` 实际只强制 §2 的:复杂度、参数、语句、分支、return。而 **单文件行数(≤400/500)、单函数行数(≤40/75)、嵌套层级(≤3/4)、类规模(≤200/300/≤12 公有方法)ruff 都不检**——全靠人工 review。§2 的"通行度/出处"列虽有暗示,但读者很容易误以为 `make lint-ours` 覆盖整张表。
- **建议**:在 §2 加一列或加标记,**明确每行是"CI 强制"还是"review 把关"**,避免"以为门禁会拦其实不拦"。

### D.(小)术语:`C901` 是圈复杂度、`PLR0912` 是分支数,二者不同指标
§2 把行标成"圈复杂度(**分支**)",而 §4/`ruff-ours.toml` 另有 `PLR0912 分支≤12`。McCabe 圈复杂度(独立路径数)与 pylint 的 branches(分支语句数)是**两个度量**;§2 用"分支"描述 C901 易混。
- **建议**:§2 该行改述为"圈复杂度(独立路径)C901≤10",并补一行 `PLR0912 分支数≤12`,与 §4 对齐。

## 五、小问题(纠错级)
- §3 写 `mute_gate.py`(33 行),实测 **32 行**——无伤大雅,但既然举为"小而美"样板,顺手对齐数字更稳。
- §4 "规则映射"列了 `PLR0912(分支≤12)`,§2 表却无对应行(见 D)——补齐即一致。

## 六、给设计者的澄清清单(可直接回填)
1. **A**:是否接受 file-wide 豁免导致"挂账文件内新违规漏网"?改用函数级 `# noqa` 还是 baseline?
2. **B**:`lint-ours` 的强制点定在哪(CI / pre-commit / `make check`)?在此之前规范如何保证不被绕过?
3. **C**:§2 各指标的"强制/review"归属能否在表中显式标注?
4. **D**:`C901` 与 `PLR0912` 的术语/行项能否对齐?

> 评审结论:**规范本身专业、诚实、与工程同步,可落地**;唯一需要在"落地"前定调的是 **A(棘轮粒度)+ B(强制点)**——这两点决定它是"真门禁"还是"软约定"。其余为措辞/精确度优化,不阻断采用。

---

# 设计者回应(作者回复评审,2026-06-30)

> 已逐条**独立复核**评审意见(对照 `makefile`、`ruff-ours.toml`、`pyproject.toml` 与实际文件,非盲从)。结论:**四点全部成立,无可反驳**。下为复核证据、接受情况与处置方案。措辞/精确度问题(C、D、小问题)直接修订;**A(棘轮粒度)、B(强制点)涉及定调,待团队确认后实施**。

## 一、复核确认(均属实)
- **B 属实**:`check: format-check lint type-check`(`makefile`),**不含 `lint-ours`**;`.github/`、pre-commit **无引用** → 目前确是"自觉运行",未接强制点。
- **A 属实**:`web_ui_agent.py` 内 **0 处 `# noqa`**;`ruff-ours.toml` 用的是 **整文件级 `per-file-ignores`**。机理确凿(ruff per-file-ignores 为文件级=既定行为):在挂账文件里**新写**一个超复杂函数,其被豁免的规则(如 web_ui_agent 的 C901/PLR0915/PLR0912)**仍会过闸**。"只拦新增"对**挂账文件的被豁免规则不成立**——评审无误。
- **C 属实**:§2 七项里仅 **5 项**(复杂度/参数/语句/分支/return)被机器强制,其余(文件/函数行数、嵌套、类规模)全靠人工 review,表内未标清。
- **D 属实**:`C901`(圈复杂度=独立路径数)与 `PLR0912`(分支语句数)是两个度量;§2 写"圈复杂度(分支)"混用,且 §2 缺 PLR0912 行(§4 却列了)。
- **小问题属实**:`mute_gate.py` 实测 **32 行**(文中 33);§2/§4 关于 PLR0912 不一致。

## 二、处置方案

**直接修订(措辞/精确度,无需定调)**
- **C**:§2 增"**强制方式**"列,每行标注 `CI 强制` / `review 把关`。
- **D**:C901 行改述为"圈复杂度(独立路径)≤10";新增"分支数 `PLR0912`≤12"行,与 §4 对齐。
- **小问题**:`mute_gate.py` 33→32;§3 数字对齐。

**待定调(决定"真门禁 vs 软约定")**
- **A 棘轮粒度**:
  - **A1(设计者推荐,与评审一致)**:整文件豁免 → 改为在**具体超标函数**上打 `# noqa: C901`(等)。同文件**新**函数仍受检,棘轮名副其实;代价是动 6 个挂账文件的相应 `def` 行。
  - **A2**:维持整文件豁免,但文档**不 overclaim**(明示"挂账文件内被豁免规则的新违规不拦,需整文件重构才收紧")。
- **B 强制点**:
  - **B1(推荐)**:`lint-ours` **并入 `make check`**(当前为绿,并入不破坏;`check` 全仓宽松与 `lint-ours` 仅 `OUR_CODE` 可共存)。一行改动即成真门禁。
  - **B2**:接入 CI 必经步骤。 **B3**:pre-commit 钩子。

**设计者建议组合:A1 + B1** —— 棘轮真生效 + 本地 `make check` 即强制,零额外基础设施。

> 状态:C/D/小问题可立即落地;A、B 待团队拍板后实施并自测,本文档随之更新。

---

# 评审员复核(对设计者回应的二次评审,2026-06-30)

> 已核实设计者回应所依赖的关键前提(对照 `.github/workflows/ci.yml` 与 `makefile`,非臆测)。结论:**态度与复核质量都好,A1 方向正确,C/D/小问题处置无误;但 B1 的核心结论"并入 `make check` 即成真门禁"不成立,需纠正。**

## 一、方法与态度:认可
设计者做了**独立复核而非盲从**,四点全部确认属实,并诚实区分"可立即修订(C/D/小问题)"与"需定调(A/B)"。这是健康闭环。下面只纠一处实质错误 + 两个补充。

## 二、实质问题:B1「并入 `make check` 即成真门禁、零额外基础设施」——**不成立**

B1 默认了"`make check` 会被强制运行"。核实实际 CI,**此前提不成立**:
- 仓库**已有 CI**:`.github/workflows/ci.yml`,在 push main / PR 触及 `examples/voice_agents/**` 时触发。
- 但 CI 跑的是 **`ruff check examples/voice_agents`(`ci.yml:31-32`)——用根 `pyproject.toml` 宽松规则,既不调 `make check`,也不加载 `ruff-ours.toml`**。
- 即:**现有 CI 完全绕过 lint-ours / ruff-ours.toml**。

推论:
1. **B1 只对"本地自觉跑 `make check`"的开发者生效**;CI 不调 `make check` → 线上仍不拦。B1 改善的是本地体验(一条命令跑全套),**约束力与现状"自觉跑 lint-ours"同档**,不是"真门禁"。
2. 真正的强制点恰是设计者列为次选、未展开的 **B2(接 CI)**——而 **CI 已存在、已在正确 `paths` 触发**,加一步几乎零成本。

**纠正建议:A1 + B2(+B1 作本地体验);B2 才是关键。**
- **B2(真门禁)**:在 `ci.yml` 加一步严格检查。鉴于现有 CI 走轻量路线(`pip install ruff==0.15.18`、直接 `ruff check`,**不用 uv**),最自然是**直接复刻、不调 `make lint-ours`**(后者依赖 uv):
  ```yaml
  - name: Ruff lint (our code, strict)
    run: ruff check --config ruff-ours.toml <OUR_CODE 列表>
  ```
  `ruff-ours.toml` 已 `extend = "pyproject.toml"`,行宽/版本自动继承,与现有 CI 风格一致。
- **B1(本地体验)**:并入 `make check` 也好,但须明确 **B1 是 DX、B2 是 enforcement**,B1 不能替代 B2。

> 一句话:设计者说"零额外基础设施即成真门禁"——基础设施(CI)其实已经在那儿了,只是现在它没看 `ruff-ours.toml`;真门禁的代价是"在 `ci.yml` 加一步",而非"并进 `make check`"。

## 三、对 A1 的补充(认同方向,提示一个代价)
A1(整文件豁免 → 函数级 `# noqa`)正确且可行(C901/PLR0915/PLR0912/PLR0913/PLR0911 均锚定 `def` 行,`# noqa: C901,PLR0915` 打该行即精确豁免)。两点提示:
1. **债务台账由集中变分散**:现 §5 表 + `ruff-ours.toml` 6 行 ignore 是**集中可读的欠债清单**;散落各 `def` 行的 `# noqa` 会丢失"一眼看全欠债"的能力。建议 **A1 落地后保留 §5 表作为人读台账**(标注对应 noqa 所在函数),并用 `grep -rn "noqa" $(OUR_CODE)` 兜底审计。
2. A1 需**一次性**标注当前超标函数(去掉 file-wide ignore → 跑 ruff-ours → 按报错逐个 `def` 加 noqa);对 `web_ui_agent.py`(2105 行)可能涉及多个函数,但仅一次性。

## 四、其余
- **A2**(维持整文件豁免 + 文档不 overclaim)作退路诚实,但棘轮名不副实;同意优先 A1。
- **C/D/小问题**的直接修订:改法正确,无异议。

## 五、复核结论
- A1 ✅(补充:保留 §5 集中台账)。
- **B:需把推荐从"B1=真门禁"纠正为"A1 + B2(动 `ci.yml`)才是真门禁,B1 仅作本地体验"。** CI 已就位、触发路径正确,加一步 `ruff check --config ruff-ours.toml <OUR_CODE>` 即可,成本极低。
- C/D/小问题 ✅ 可立即落地。

## 附:本轮新增的代码核对
| 主题 | 位置 |
|---|---|
| 现有 CI 跑宽松 `ruff check`、不调 make check、不读 ruff-ours.toml | `.github/workflows/ci.yml:31-34` |
| CI 触发路径含全部 OUR_CODE | `.github/workflows/ci.yml:11-17` |
| `make check` 不含 lint-ours | `makefile:105` |

---

# 设计者回应(二)(回复二次评审,2026-06-30)

> 已独立核实二次评审依赖的关键前提(`.github/workflows/ci.yml`、`makefile`)。结论:**二次评审正确,我前一轮 B1 的结论有误,接受纠正。**

## 一、认错并复核:B1「并入 `make check` 即真门禁、零额外基础设施」——**错误,撤回**
我上一轮在核对 B 时**只 grep 了 `lint-ours`、漏看了 `ci.yml` 本身存在**,导致误判。核实 `ci.yml`:
- CI **已存在**,push main / PR 触及 `examples/voice_agents/**` 即触发(`ci.yml:11-17`)——覆盖全部 `OUR_CODE`。
- CI 实跑 `ruff check examples/voice_agents`(`ci.yml:32`)+ `ruff format --check` + `compileall`,**用 pip 装的 ruff、根 `pyproject.toml` 宽松规则**;**不调 `make check`、不读 `ruff-ours.toml`**。
- ⇒ **现有 CI 完全绕过 `lint-ours`**。

因此 **B1 不是真门禁**:CI 不跑 `make check`,B1 只改善"本地自觉跑一条命令"的体验,线上仍不拦。真正强制点是 **B2(在 `ci.yml` 加一步)**,且 CI 已就位、`paths` 正确,成本极低。**二次评审纠得对。**

## 二、修订后的最终结论
- **A1 ✅**:整文件豁免 → 具体超标函数上 `# noqa: <rule>`。**补充采纳二次评审**:落地后**保留 §5 集中台账**作"一眼看全欠债"的人读清单(标注各 `# noqa` 所在函数),并以 `grep -rn noqa $(OUR_CODE)` 兜底审计。
- **B:推荐由 B1 改为 `A1 + B2`**。B2 = 在 `ci.yml` 增一步:
  ```yaml
  - name: Ruff lint (our code, strict)
    run: ruff check --config ruff-ours.toml <OUR_CODE 文件清单>
  ```
  `ruff-ours.toml` 已 `extend = "pyproject.toml"`,行宽/版本自动继承,与现有 pip-ruff CI 风格一致。**B1 仅作本地 DX,不替代 B2。**
- **C / D / 小问题 ✅**:措辞/精确度修订,立即可落地。

## 三、落地细节(实施 B2 时一并处理)
`ci.yml` 不走 make,直接写文件清单会与 `makefile:OUR_CODE` **重复、易漂移**。→ 把 `OUR_CODE` 文件清单**抽到单一来源**(如 `ourcode.txt`),`makefile` 与 `ci.yml` 都读它,清单只维护一处。

> 净结果(待团队最终拍板后实施):**A1 + B2(改 `ci.yml`)+ B1(本地 DX)+ 保留 §5 台账 + OUR_CODE 单一来源 + C/D/小问题修订**。当前仅文档往来,未改任何代码/配置。

---

# 评审员复核(对设计者回应(二)的复核,2026-06-30)

> 已核实本轮依赖的前提(`.github/workflows/ci.yml`、`makefile`、`pyproject.toml`)。结论:**设计层面已完全收敛,无遗留争议;新增一个实现期必须处理的风险(ruff 版本不一致),并给单一来源方案补三个落地细节。**

## 一、总评:认可,设计往返可结束
设计者本轮做了三件对的事:**(1) 具体认错**(定位到"上轮只 grep 了 `lint-ours`、漏看 `ci.yml` 存在");**(2) 独立验证后收敛到 `A1 + B2 + B1(DX) + 保留 §5 台账`**,与二次评审一致;**(3) 自补一个真问题**(OUR_CODE 单一来源)。方案 `A1 + B2 + B1(DX) + 保留 §5 台账 + ourcode.txt 单一来源 + C/D/小问题` **无保留认可**。

## 二、新增风险(实现 B2 时务必处理):ruff 版本不一致 → "本地过、CI 挂"
双方此前均未提及,核实有据:
- **CI 钉死** `pip install ruff==0.15.18`(`ci.yml:30`)。
- **本地** `make lint-ours`/`make check` 走 `uv run ruff`(`makefile:89`),而 `pyproject.toml:88` 仅声明 **`"ruff"`(无版本约束)** → uv 解析版本可能 ≠ 0.15.18。
- 后果:ruff 跨版本对 `C901`/`PLR09xx` 计数偶有变化 → **本地绿、CI 红**(或反之),对"门禁"是致命体验问题。
- **建议**:B2 落地时**统一 ruff 版本** —— 要么 `pyproject.toml` 把 ruff 钉到与 CI 同版本(`ruff==0.15.18`),要么让 **CI 也用 `uv run ruff`**(`uv sync` + `uv run ruff check --config ruff-ours.toml $(cat ourcode.txt)`)与本地同源(更彻底,代价是 CI 装一次 uv)。

## 三、对"单一来源 `ourcode.txt`"(设计者新增,认同)补三个细节
1. **`ourcode.txt` 必须纯路径**(一行一个、无注释/中文):ci.yml 会用 `$(cat ourcode.txt)` 词分割成参数,混入注释会当文件名报错;职责标注留在文档 §1 镜像。
2. **把 `ourcode.txt` 加进 `ci.yml` 的 `paths:` 过滤**(`ci.yml:11-17`):否则只改清单(纳入某存量文件)而不动代码时 CI 不触发。
3. **文档 §1"权威清单 = makefile 的 OUR_CODE"改为"= `ourcode.txt`"**,makefile 与 ci.yml 都读它。

## 四、结论
- 设计讨论收敛,方案认可,**不再需要设计往返**。
- 我新增的均为**实现 PR 阶段**事项:(1)ruff 版本对齐(重要,防本地/CI 撕裂);(2)`ourcode.txt` 纯路径 + 进 paths + 文档指针(细节)。不改变方案,实施时别踩。
- 设计者"待团队拍板后实施"的节奏合适;落地后跑一次 `make lint-ours` + 触发一次 CI 自测即可收尾。

## 附:本轮新增的代码核对
| 主题 | 位置 |
|---|---|
| CI 钉死 ruff 版本(pip) | `.github/workflows/ci.yml:30` |
| 本地用 `uv run ruff`(锁定环境) | `makefile:89` |
| `pyproject.toml` 中 ruff **无版本约束** | `pyproject.toml:88` |

---

# 设计者回应(三)(回复第三轮复核,2026-06-30)

> 已独立核实第三轮新增的"ruff 版本不一致"风险(`.github/workflows/ci.yml`、`makefile`、`pyproject.toml`)。结论:**风险属实,接受;三轮评审收敛,方案定稿,剩余均为实现-PR 事项。**

## 一、核实新风险:ruff 版本不一致 → "本地绿 / CI 红" —— 属实,接受
- CI 钉死 `pip install ruff==0.15.18`(`ci.yml:30`);
- 本地 `make lint-ours` 走 `uv run ruff`(`makefile:89`);
- 但 `pyproject.toml:88` 仅 `"ruff"`、**无版本约束** → `uv sync` 可能解析出别的版本;
- 本地当前 0.15.18 **是巧合对上**,未被锁定。
⇒ 跨版本 `C901`/`PLR09xx` 计数可能变化 → 本地/CI 撕裂。评审无误。

**采纳的修法**:实现时在 `pyproject.toml` 把 ruff 钉为 **`ruff==0.15.18`**(与 CI 同版本)→ 本地 `uv run ruff` 与 CI `pip ruff` 同源,最小改动。(评审备选"CI 改用 uv"更彻底,但需 CI 装 uv,非必要。)

## 二、`ourcode.txt` 三细节:全采纳
1. **纯路径**(一行一个、无注释/中文)——供 `ci.yml` 的 `$(cat ourcode.txt)` 词分割成参数;职责标注留在文档 §1 镜像。
2. **把 `ourcode.txt` 加进 `ci.yml` 的 `paths:`**——只改清单(纳入某存量文件)而不动代码时,CI 也能触发。
3. **文档 §1 权威来源**由"`makefile` 的 `OUR_CODE`"改为"**`ourcode.txt`**";`makefile` 与 `ci.yml` 都读它,清单只维护一处。

## 三、定稿方案(三轮收敛,待团队拍板即实施)
1. **A1**:整文件豁免 → 具体超标函数 `# noqa: <rule>`;**保留 §5 表**作集中人读台账 + `grep -rn noqa $(cat ourcode.txt)` 兜底审计。
2. **B2(真门禁)**:`ci.yml` 增一步 `ruff check --config ruff-ours.toml $(cat ourcode.txt)`;**B1**(并入 `make check`)仅作本地 DX,不替代 B2。
3. **单一来源 `ourcode.txt`**(纯路径 + 进 `paths` + 文档指针)。
4. **ruff 钉 `==0.15.18`**(防本地/CI 撕裂)。
5. **C/D/小问题** 文档修订(§2 加"强制方式"列、C901 术语改"独立路径"+补 PLR0912 行、`mute_gate.py` 33→32)。

**验收**:落地后跑一次 `make lint-ours` + 触发一次 CI 自测。

> 至此设计往返结束,无遗留争议。当前仍仅文档往来,未改任何代码/配置;实施另起分支。

---

# 评审员定稿结论(对设计者回应(三)的复核,2026-06-30)

> 已核对定稿方案五项与工程现状(`ci.yml`、`makefile`、`pyproject.toml`、正文 §1–§6)。结论:**方案(决策)可以定稿,三轮收敛、无遗留争议;但文档正文终稿还差一步(C/D/小问题尚未落到正文),并提示一个实现注意点(`uv lock`)。**

## 一、方案定稿:✅ 认可
逐项核对,技术上均成立且覆盖全部评审点:

| 定稿项 | 判定 |
|---|---|
| A1:整文件豁免 → 函数级 `# noqa` + 保留 §5 台账 + `grep` 兜底 | ✅ 棘轮名副其实 |
| B2:`ci.yml` 加严格步骤(真门禁);B1 仅本地 DX | ✅ 强制点正确 |
| 单一来源 `ourcode.txt`(纯路径 + 进 `paths` + 文档指针) | ✅ DRY、无漂移 |
| ruff 钉 `==0.15.18`(本地/CI 同源) | ✅ 防撕裂、最小改动 |
| C/D/小问题文档修订 | ✅ 措辞/精确度 |

**设计层面无保留认可,设计往返到此结束。**

## 二、需分清"两种定稿"(拍板参考)
1. **方案定稿 ✅ 已达成** —— 决策锁定,可拍板。
2. **文档终稿 ⚠️ 还差一步** —— 当前正文 §1–§6 仍是**旧文**:§3 仍写"`mute_gate.py`(33 行)"、§2 表仍无"强制方式"列、§5 仍写"只拦新增违规"而未加"挂账文件被豁免规则不拦新违规"的 caveat。C/D/小问题设计者承诺"直接修订",但**尚未落到正文**(目前只在回应里的"将改"清单)。→ 定稿前建议把这些编辑**实际应用到正文**,并在文档头标注"规范已定稿 / 强制机制(A1+B2+ourcode+ruff-pin)待实现 PR"。
3. **实现 ⏭ 独立 PR** —— 验收(跑 `make lint-ours` + 触发一次 CI)已写明,正确。

## 三、实现注意点(不影响定稿,实施别漏)
钉 `pyproject` `ruff==0.15.18` 后,**务必 `uv lock` 固化该版本到锁文件**——否则 `uv run ruff` 仍可能解析到别的版本,版本对齐不生效。(`uv.lock` 中此前未直接见 ruff 条目,实施时一并确认锁文件已含 `ruff==0.15.18`。)

## 四、最终结论
**可以定稿。** 作为"方案/决策"已成熟,**拍板即可**;只要把设计者承诺的 C/D/小问题编辑**实际落到正文**(并标注"强制机制待实现 PR"),本文件即为名副其实的终稿。`A1 + B2 + ourcode.txt + ruff-pin` 为独立实现 PR,按既定验收收尾。三轮评审-回应闭环质量高,设计者每轮均独立复核、诚实纠偏,规范专业、与工程同步,**批准落地**。
