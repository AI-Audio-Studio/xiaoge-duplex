# 语音控制指令(voice-cmd)设计——定稿·开发指导版

| 项 | 值 |
| --- | --- |
| 版本 | v4.5(R5.2.2 no-legacy 合同修订版;内容基线 = v4.4 + 云侧开工前问题闭环 + 负责人确认多命令分阶段) |
| 状态 | **设计期·零代码**——只修订需求、协议、合同和验收口径;工程实现仍需评审通过后另行批准 |
| 参考件 | 仓库根 `语音语义解析控制器.md`(指令契约出发点,继承与分歧见 §2) |
| 评审存档 | [VOICE_CMD_DESIGN_REVIEW.md](VOICE_CMD_DESIGN_REVIEW.md)(r1/r2 意见、应答、复核、拍板台账——只读,溯源唯一入口) |
| 关联 | [../protocol-v2/PROTOCOL_V2_DESIGN.md](../protocol-v2/PROTOCOL_V2_DESIGN.md)(R5.2.2 `data.cmd`/ack/result 主契约) · [../../guide/ARCHITECTURE.md](../../guide/ARCHITECTURE.md) · [../../guide/CLIENT_INTEGRATION.md](../../guide/CLIENT_INTEGRATION.md) · [../../../clients/PROTOCOL.md](../../../clients/PROTOCOL.md) |
| 目录索引 | [README.md](README.md)(文件清单与维护规则) |

**版本沿革**(详细过程见评审存档;本表只记里程碑)

| 版本 | 日期 | 里程碑 |
| --- | --- | --- |
| v1 | 2026-07-20 | 初稿(选型/注册表/协议/验证骨架) |
| v2 | 2026-07-20 | 负责人四项拍板(D-9/10/12 + D-8 确认)+ 解耦纪律(D-11) |
| v3 | 2026-07-21 | r1 应答(2A+8B+8C 全接受):延迟账重写、热词三源、R 规则扩至 R12 等 |
| v3.1 | 2026-07-28 | 同步 main 67d2d83:五帧现状、续讲确认链序、多义词纪律 |
| v3.2 | 2026-07-28 | r2 应答(3 勘误+3 精度注) |
| v3.3 | 2026-07-28 | 拍板 D-15(confirm 语义)/D-16(check_battery 门A 范围)——**设计放行** |
| v4.0 | 2026-07-28 | 定稿整理(结构重组,零语义变更) |
| v4.1 | 2026-07-31 | 全双工 R5 修订:P0 单命令 `data.cmd`、`data.cmd_ack`/`data.cmd_result` P0、高危确认、no replay、P0 seed 验收 |
| v4.2 | 2026-07-31 | 全双工 R5.1 修订:seed 表的 `params_example` 与机器可校验 `P0 registry schema` 分离;能力错误统一 `capability_unsupported`;WSS token 口径跟随 protocol-v2 Authorization bearer |
| v4.3 | 2026-07-31 | 全双工 R5.2 修订:跟随 protocol-v2 v1.7 和工作簿全量 P0 schema,明确 `data.reply` 文本面、`type` 信封和 `ctrl.hello.role` 已冻结 |
| v4.4 | 2026-07-31 | 全双工 R5.2.1 最小补丁:跟随 protocol-v2 v1.8 和 R5.2.1 工作簿;命令业务字段不变 |
| v4.5 | 2026-08-03 | 全双工 R5.2.2 修订:本版本不兼容旧客户端;4.2 多命令示例改为阻断;P0 registry 参数类型收窄为 `enum/int`;明确 P0 seed 不承诺 200+ 泛化召回,P1 评估 embedding 召回层 |

---

## 1. 背景、目标与纪律

### 1.1 背景

小歌当前是纯闲聊语音助手:人设 prompt 驱动的单 LLM 对话(web_ui_agent.py `VoiceAgent`),无 function tool、无结构化输出。历史设备客户端曾通过 `/ws/audio` 收 TTS 音频与五种控制帧;R5.2.2 no-legacy 后,voice-cmd 产品协议只面向新 SDK 的 `/ws/session` + `data.cmd` 闭环。

需求:在保留闲聊的同时,识别用户语音中的**设备控制指令**,把结构化指令下发设备执行,并语音回执;指令集可扩到 **200+ 条**且**新增指令不改代码**(配置化)。R5.2.2 P0 只自动下发单命令 `data.cmd`;单句多控制动作必须被识别为多命令,但只返回拆分/选择提示,不自动编排执行。低风险顺序多命令作为 P1,复杂任务编排作为 P2。参考件给出 6 操作的 LLM 解析器 prompt(复合拆分/关系标注/省略还原/置信降级/JSON 输出),本设计继承其可扩展思路,但 P0 以单命令闭环、ack/result、高危确认和 seed/registry 验收为主。

### 1.2 目标

| 编号 | 目标 |
| --- | --- |
| G1 | 闲聊体验**零回归**:默认关(`XIAOGE_CMD_ENABLE=0`);开启后非指令语音路径与今天一致、零额外延迟 |
| G2 | 指令集**配置化**:200+ 指令定义在 YAML 注册表,增改指令 = 改配置 |
| G3 | P0 **单命令闭环 + 多命令识别阻断**:一轮只生成一个 `data.cmd`;多控制动作进入 `multi_command_blocked`,提示用户拆分或选择 |
| G4 | 结构化指令经 protocol-v2 `data.cmd` 下发 SDK;本版本不兼容旧客户端协议 |
| G5 | **语音回执与澄清**:投递 ack 与执行 result 分离;缺参/低置信反问,支持跨轮补槽 |
| G6 | **可量化验收**:语料评测出通过率报告;全链路可观测 |
| G7 | **零母体改动**:只用子类钩子与既有旁路机制,不碰 `livekit-agents/` |
| G8 | **独立模块**:单一开关整体启停,指令域可单独启停;与其他模块互不感知(耦合面白名单 §5.10) |

### 1.3 非目标(R5.2.2 P0)

- 服务端**不执行**设备动作、不维护设备状态——执行与状态在客户端侧。
- **条件执行的求值**不在服务端:标注并透传条件对象,由持设备状态的客户端判定(D-10)。
- 不做一轮多命令编排执行:识别到多个控制动作时不生成 `data.cmd`,只提示拆分或选择;P1 再设计低风险顺序多命令 group/step,P2 再设计复杂任务编排。
- 不做未 ack 命令重放:断线前未收到 `data.cmd_ack` 的命令不得自动重发。
- 混合句的闲聊残余不单独应答(D-13)。
- 不做本地(非 LLM)完整 NLU;本地只做触发词门。

### 1.4 设计纪律(D-11)

1. **功能模块能单独控制**:`XIAOGE_CMD_ENABLE` 一个开关整体启停(关 = 完全不装配);指令域可单独启停(域文件 `enabled` + `XIAOGE_CMD_DOMAINS` 白名单)。
2. **不与其他模块耦合**:voicecmd 是纯库包——不 import 聆听/KWS/打断/mute/providers/webpanel 任何模块,对外能力全部由 host 以 `Deps` 注入;其他模块一律不感知 voicecmd。§5.10 白名单是权威,评审按它验收。

---

## 2. 对参考件的契约继承与分歧

| 参考件内容 | 处置 |
| --- | --- |
| 输出字段 `action/params/confidence/reason/sequence_id/relation` | 继承;`reason` 默认不让模型输出(省 token),评测模式打开(`XIAOGE_CMD_REASON`) |
| relation 四执行关系 serial/parallel/conditional/mutex | 全部继承;conditional 由服务端标注、条件对象透传,客户端判定(D-10,§5.5) |
| relation 的**状态复用**(参考件例 6/7 把 unconfirmed/unsupported/truncated 写进 relation) | **不继承**:状态由 validator 内部管理,wire 上 relation 恒为四执行关系值;设备侧以本设计 §5.5 为准 |
| 指令上限 10 条、超限截断 | 继承(`XIAOGE_CMD_MAX_N=10`) |
| 置信度 <0.5 → unconfirmed | 继承(阈值 `XIAOGE_CMD_CONF_MIN`);unconfirmed 的处置(不下发、转澄清)为本设计新增 |
| 互斥组保留置信度最高 | 继承,由**代码校验器**执行(R5),不信任 LLM 自裁 |
| 非控制语音 → `[]` | 继承,`[]` = 回落闲聊(指令/闲聊双路衔接点) |
| 未知操作 → unsupported | 继承;unsupported 条目**不下发**(R1) |
| 6 操作定义表 | 泛化为注册表(§5.1);参考件 6 操作为种子(D-9) |
| adjust_volume 的 direction"必填 + 默认 up"(自相矛盾) | 种子迁移定为**可选 + `default: up`**(保参考件行为,回执透明化) |
| 纯解析器定位("绝对禁止输出闲聊") | 继承:解析器独立 LLM 调用、独立 prompt,与人设完全隔离 |
| 省略指代还原(句内) | 继承,并扩展到**跨轮补槽**(§5.6) |

---

## 3. 方案选型与延迟/成本账

### 3.1 选型:C·触发词门 + 专用解析 LLM

| | 方案 | 结论 |
| --- | --- | --- |
| A | Function Calling(指令注册为 LLM tools) | ❌ 200+ schema 每轮 token 爆炸;4B 多工具可靠性无背书;执行关系无法表达;闲聊轮背上开销违 G1 |
| B | 单调用混合 prompt(人设里加"是指令则输出 JSON") | ❌ 人设与解析互相污染;JSON 泄漏进 TTS;无法独立评测 |
| **C** | **触发词门 + 专用解析 LLM(选定,D-1)** | ✅ 指令/闲聊解耦各自调优;非指令轮零开销;参考件 prompt 思路直接复用 |
| D | 纯本地 NLU(规则/槽文法) | ❌ 200+ 口语变体不可维护;其触发词典思想被 C 的门吸收 |

### 3.2 延迟/成本账(按抢跑事实)

本工程**默认开着抢跑生成**:`TurnConfig.turn_handling()` 只传 `preemptive_tts`(turn_config.py `turn_handling()`),母体 `enabled` 缺省即 True(livekit turn.py `_PREEMPTIVE_GENERATION_DEFAULTS`),且本项目 `TURN_PREEMPTIVE_TTS=True`——STT final 一出,闲聊 LLM 与抢跑 TTS 已发射,先于 `on_user_turn_completed` 钩子。三种轮的真实账:

- **门未命中**:与今天完全一致,零变化(G1)。
- **门命中(真指令)**:钩子内走解析 LLM,`StopResponse` 后已发射的抢跑闲聊**整轮作废**——作废 token + 抢跑 TTS 合成费是每个命中轮的**常态成本**(同款双付今天已存在于停止词/聆听吞轮);用户体感延迟 ≈ 解析 + 校验 + 回执。解析与将废抢跑流在同一网关并发,基准按真实配置实测(§9.2)。
- **门误报(命中但解析 `[]`)**:钩子原样返回、不改消息,抢跑闲聊经母体等价性校验**被复用**(agent_activity.py 等价四项校验),不重新生成——净代价 = 解析 token + 约一次解析时长等待。

缓解旋钮:`TURN_PREEMPTIVE`(新增,映射 `preemptive_generation.enabled`,默认 True 零回归)——指令重度场景可关抢跑消除双付,代价闲聊首响变慢。**不做**"命中轮抑制抢跑":发射点在母体 AudioRecognition 内、无用户钩子,违 G7。

---

## 4. 架构与数据流

### 4.1 组件图(全部为新增自有代码,母体零改动)

```
              examples/voice_agents/commands/  指令注册表(YAML,按域分文件,域级 enabled)
                              │ 启动加载 + fail-fast 校验
                              ▼
      ┌────────────── voicecmd/registry.py(纯核)──────────────┐
      │ 四类导出: ①触发词典(→gate) ②prompt 片段(→prompt)       │
      │          ③校验规则(→validate) ④STT 热词(三源合并→env) │
      └───────────────────────────────────────────────────────┘

 用户语音 → STT final → VoiceAgent.on_user_turn_completed(agent 事件循环)
   │
   ├─ 既有链不动:聆听吞轮 → 续讲确认 → 自动进入 → 停止词/附和过滤 → 数字归一化
   │            (聆听期/停止词优先级天然高于指令)
   │
   └─ [新增末位] voicecmd host 钩子(XIAOGE_CMD_ENABLE=1 时)
        │
        ▼
      gate(触发词匹配,<1ms)
        ├─ miss(且无 pending)→ return —— 闲聊照旧(抢跑复用),零额外延迟
        └─ hit / pending 补槽
             ▼
           parser(专用 LLM,定域 prompt,总预算 5s)
             ├─ 超时/坏 JSON → 固定澄清话术 + StopResponse(D-8)
             ├─ [] → return —— 回落闲聊(门误报自愈)
             └─ 候选指令数组
                  ▼
                validator R1-R12(权威)
                  ├─ confirmed 恰好 1 条 → dispatcher(data.cmd + GUI timeline)
                  │                       → 按 cmd_ack/result 生成话术 → StopResponse
                  ├─ confirmed 多于 1 条 → multi_command_blocked
                  │                       → data.reply 拆分/选择提示,不生成 data.cmd → StopResponse
                  └─ 全 unconfirmed → 澄清反问 say + pending 登记 → StopResponse
```

### 4.2 一轮时序(命中指令)

```
示例 A:单命令成功下发
用户:"向左转30度"
  STT final → 既有过滤链通过 → gate 命中(触发词"向左转",候选域 {motion})
  → parser(L0 规则+L1 全量索引+L2 motion 详规+L3 上下文)
  → [{"action":"turn","params":{"direction":"left","angle":30}}]
  → validator 全过且 confirmed 恰好 1 条 → 下发 data.cmd(SDK ack/result 闭环)
  → say("好的,左转30度。") → StopResponse

示例 B:多命令识别但 P0 阻断
用户:"向左转30度然后暂停音乐"
  STT final → 既有过滤链通过 → gate 命中(触发词"向左转"+"暂停",候选域 {motion,music})
  → parser(L0 规则+L1 全量索引+L2 motion/music 详规+L3 上下文)
  → [{"action":"turn",...},{"action":"control_music",...}]
  → confirmed 多于 1 条 → 状态进入 multi_command_blocked
  → 下发 data.reply("我听到了两个操作:左转30度、暂停音乐。请分开说,或告诉我先执行哪一个。")
  → 不生成任何 data.cmd → StopResponse
```

---

## 5. 详细设计

### 5.1 指令注册表(200+ 规模的根)

**唯一权威**:`examples/voice_agents/commands/` 目录(D-12:指令语义经自然语言传递,属语音代理的一部分;路径按 voicecmd 包位置解析,不依赖 CWD),按设备域分文件。参考件 6 操作为**种子**先跑通全链(D-9);真实 200+ 表到货后按同 schema 填充,过规模重验证门(§9 门B)。

#### 5.1.1 Schema(单条指令)

```yaml
# examples/voice_agents/commands/motion.yaml —— 域文件示例
domain: motion            # 域 id
domain_name: 运动控制      # 中文名(进 prompt)
enabled: true             # 域级开关;false = 本域从四类导出产物整体消失
commands:
  - action: turn                    # 全局唯一 id(下发 action 字段)
    desc: 转向控制                   # 一句话说明(进索引 L1)
    params:
      - name: direction
        type: enum
        values: [left, right, back]
        required: true
        desc: 方向
      - name: angle
        type: int
        range: [0, 360]
        required: false
        default_by: {left: 90, right: 90, back: 180}   # 按 direction 取缺省
        desc: 角度
    triggers: [向左转, 向右转, 左转, 右转, 掉头, 向后转, 转向, 转一下, 转个]
    hotwords: [左转, 右转, 掉头]      # 导出 FunASR 热词(可省)
    mutex_group: null                # 互斥组;同组冲突按 R5 裁决
    confirm: false                   # 危险指令标记;true 的 V1 语义见 R12(D-15)
    ack: "{direction:cn}转{angle}度"  # 回执模板,不含问候前缀(渲染器统一加)
    ack_cn: {left: 左, right: 右, back: 向后}
    examples:                        # few-shot(进详规 L2,1~3 个/条)
      - text: 向左转30度
        out: {action: turn, params: {direction: left, angle: 30}}
      - text: 掉个头
        out: {action: turn, params: {direction: back, angle: 180}}
```

**R5.2.2 P0 参数类型系统 = enum | int**:注册表出现其他类型(含 `string/integer/number/boolean/object/array/date_expr`)启动即拒载。合同中的 registry schema 必须同样收窄到 `enum/int`,不得只靠正文约束。`string`(歌名/自由文本)是**已识别扩展点**:真实表若含 string 参数,须在门B 前完成小型设计增补(长度/字符集/ack 转义/R 规则扩展)再放行——不静默支持。

**R5.2.2 registry 消费边界**:合同文件 `xiaoge-duplex-voicecmd-registry-r5.2.2.schema.json` 的语义是“语音意图 seed/registry schema”,不是端侧可执行控制命令全集。端侧 clients/fake executor 只把 `delivery=data.cmd` 和 `delivery=data.cmd after confirmation` 的条目当作可执行契约;`cloud_tool + data.reply`、`cloud_knowledge + data.reply`、`ask_split only` 只由云端小歌处理并返回 `data.reply` + TTS,不得下发端侧执行。信息查询、知识问答和多命令阻断可以出现在意图 seed/追踪中,但不进入端侧执行器合同。

规模按 10~20 域 × 10~20 条估算,单文件百行级;`triggers` 全库约千词,门用集合/AC 自动机匹配。域级停用(`enabled: false` 或不在 `XIAOGE_CMD_DOMAINS` 白名单)= 该域从触发词典/索引/详规/热词四类产物**整体消失**——解析器"看不见"即天然不可能下发(R1 仍兜底)。

#### 5.1.2 加载与校验(fail-fast)

`voicecmd/registry.py`(纯 dataclass,可单测):启动加载全部域文件,任何一条不合法即**启动失败**并指明文件/指令/字段。校验规则:

- action 全局唯一;enum 值非空互异;range 有序;`default_by` 键 ⊆ 枚举值;required 参数不允许有 default;ack 占位符必须是已声明参数**且该参数必填或有缺省**;triggers 非空;参数类型 ∈ {enum, int};examples 的 out 必须通过本条 schema(**自举校验**)。
- **命名卫生规则**:全库任何注册表字符串(action、参数名、枚举值、域名)不得与协议 `type` 名、错误码、close code 名同名;R5.2.2 不再为旧 C/MATLAB 朴素解析器兼容而设置额外保留字。
- **触发词多义词纪律**:裸多义词(**继续、停、开始、好** 等)不得入 `triggers`——"停"类由停止词机制先拦;"继续"与续讲确认冲突(其在窄窗口把裸"继续/好/行"改写为"继续讲"再进链,§8);音乐恢复等用双词触发("继续播放""接着放")。校验 CLI 对裸多义词硬报错;禁入清单最小集**与 `_CONFIRM_CONTINUE_RE` 词表、停止词表同源维护**(三处词表一处漂移即 CI 报警)。
- **种子迁移注记**:`adjust_volume.direction` 按"可选 + `default: up`"迁移(divergence 见 §2 表)。
- 校验 CLI:`python -m voicecmd.registry commands/`,供改配置自查与 CI。

#### 5.1.3 四类导出产物

| 产物 | 消费方 | 说明 |
| --- | --- | --- |
| 触发词典 `{trigger → domain}` | gate | 全库 triggers 合并;命中得候选域集合 |
| prompt 片段 | parser | L1 全量索引 + L2 域详规(参数表+缺省+few-shot),预生成缓存 |
| 校验规则表 | validator | 类型/枚举/范围/缺省/互斥组,R 规则逐条执行 |
| STT 热词 | FunASR | `XIAOGE_CMD_HOTWORDS=1` 时 host 在 STT 装配前构造**三源合并集** `DEFAULT_HOTWORDS ∪ 用户 FUNASR_HOTWORDS ∪ 注册表热词`(优先级:用户 > DEFAULT > 注册表)写回 `FUNASR_HOTWORDS` env,供 providers/config.py `funasr_hotwords()` 照常读取。**必须三源合并的原因**:该函数语义是"env 非空即整体替换"(config.py `funasr_hotwords()`),漏并即冲掉停止词热词(停:40 等实测救回停止词召回的词),停止词 STT 回归。providers 零改动,host 仅只读 import `DEFAULT_HOTWORDS` 常量。**导出纪律**:注册表词上限 50、权重固定 20(低于停止词 30/40),防稀释 |

### 5.2 意图门(gate)

`voicecmd/gate.py` 纯函数:输入归一化后文本,输出 `GateResult{hit, domains}`。

- 触发词子串命中(2~4 字中文词,免分词);命中词按域聚合,取覆盖字符数最多的前 `XIAOGE_CMD_DOMAINS_TOPK`(默认 3)个域。
- **强制命中**:存在未过期 pending 补槽时,无论触发词是否命中都进解析("三十度""左边"通常不含触发词)。
- 模式 `XIAOGE_CMD_MODE`:`gate`(默认)/ `always`(每轮进解析,评测调试用,量化门漏报率)。
- 门漏报 = 该轮当闲聊,安全但影响体验;P0 seed 只承诺 trigger 覆盖和 seed 语料召回,不承诺 200+ 全量泛化召回或隐式指令召回。P1 再评估 embedding/semantic recall 层,且必须置于 validator 之前并受同一安全规则约束。门误报 = 解析 `[]` 自愈,代价一次解析延迟。

### 5.3 解析器(parser)

#### 5.3.1 专用 LLM 实例与预算

`build_cmd_llm()`:端点/模型默认继承 `QWEN_*`,可 `XIAOGE_CMD_MODEL/_BASE_URL/_API_KEY` 单独指向更强模型;`temperature=0`、`max_tokens=1024`(闲聊的 512 装不下 10 条 JSON)、`enable_thinking=False`。

- **单轮解析总预算 = `XIAOGE_CMD_TIMEOUT_S`(默认 5s)**:含首调 + JSON 修复重试(≤1 次,仅解析失败)+ 域外二次解析(≤1 次);预算耗尽按 D-8 处置。
- **排队语义(落文留痕)**:母体轮任务串行且从不取消用户代码(agent_activity.py "never cancel user code")——解析在飞期间用户再说话(含"别转了")只能排队,解析完成指令照常下发。总预算即"反悔拦不住 + 无人应答"的最坏窗口,故收紧到 5s(P95 目标 3s 的 1.67× 裕量)。

#### 5.3.2 定域 prompt(200+ 在 4B 上成立的关键,D-6)

| 层 | 内容 | 规模 |
| --- | --- | --- |
| L0 规则段 | 参考件 Workflow/Constrains 改写:切分、关系标注(条件句输出 condition 对象、不求值)、句内指代还原、互斥标注、≤N 截断、非指令→`[]`、纯 JSON、只允许索引内 action | ~300 token |
| L1 全量指令索引 | 每条一行 `action | 域 | 一句话`(种子期 6 行,满表 200 行)——保证候选域猜窄时模型仍知全集 | 满表 ~2‑3k token |
| L2 候选域详规 | 仅 gate 命中的 ≤3 域:完整参数表+缺省+few-shot | ~500 token/域 |
| L3 会话上下文 | 近 3 轮用户原文 + 最近下发指令摘要 + pending 补槽描述 | ~200 token |

**域外兜底**:解析结果出现"L1 有、L2 无"的 action(门猜窄了)→ 以该域重拼 L2 **二次解析**(≤1 次,计入总预算);评测追踪二次解析率。L0/L1/L2 片段注册表加载时预生成缓存。

#### 5.3.3 输出解析

提取首个顶层 JSON 数组(容忍前后缀噪声);失败 → 修复性重试 1 次;再失败按 D-8。**LLM 输出一律视为候选**,交 validator。

### 5.4 确定性校验器(权威)

`voicecmd/validate.py` 纯函数。**规则集以本表为准**(其他章节只引用"R 规则表",不复制区间):

| 规则 | 内容 | 处置 |
| --- | --- | --- |
| R1 | action 不在注册表(或域停用) | 剔除,log `unsupported`(不下发——设备安全底线) |
| R2 | 枚举参数值非法 | 该条降 unconfirmed(不猜) |
| R3 | 数值越界 | clamp 到边界并 log;类型错 → unconfirmed |
| R4 | 必填缺失 | 有 default/default_by → 填缺省;无 → unconfirmed(missing 参数名带出) |
| R5 | 同 `mutex_group` 冲突 | 保留置信度最高(平局取 sequence_id 靠前),其余剔除并 log |
| R6 | 条数 > `XIAOGE_CMD_MAX_N` | 截断,log 丢弃数 |
| R7 | confidence < `XIAOGE_CMD_CONF_MIN` | 降 unconfirmed |
| R8 | relation 非法/缺失 | **特判**:值 ∈ {unconfirmed, unsupported, truncated}(参考件状态复用风格)→ 降 unconfirmed 不下发;其余非法 → 归一 serial。合法值恒为四执行关系 |
| R9 | conditional 条目 | `condition.text` 缺失 → unconfirmed;`ref_action` 不在注册表 → 置 null 保 text;`expect` 键必须 ⊆ ref_action 已声明参数,否则置 null 保 text |
| R10 | params 含 schema 未声明键(模型幻觉字段) | 剥离并 log(未经校验字段绝不直透设备) |
| R11 | sequence_id 重复/乱序/删条出洞 | 按原相对顺序**重编号 1..N**——下发帧内连续无重复 |
| R12 | `risk_level=high` 或策略要求确认 | 进入 `pending_confirmation`;确认前不生成 `data.cmd`;确认通过后才进入 `send_allowed`;取消/超时不下发 |

输出 `(confirmed, unconfirmed)`:confirmed 下发+回执;unconfirmed 不下发——只差参数则并入澄清反问,全 unconfirmed 只澄清。

### 5.5 分发与协议扩展

#### 5.5.1 R5.2.2 P0 下行帧(`data.cmd`;单命令)

```json
{
  "type": "data.cmd",
  "trace_id": "trace-20260731-0001",
  "session_id": "sess-0001",
  "utterance_id": "utt-0001",
  "cmd_id": "cmd-0001",
  "capability_id": "motion.move",
  "action": "navigation.move",
  "params": {"direction": "forward", "distance_cm": 100},
  "risk_level": "medium",
  "ack_timeout_ms": 800,
  "result_timeout_ms": 5000,
  "issued_at_ms": 1789000001000
}
```

- 通道:R5.2.2 主路径为 `/ws/session` 的 WSS JSON `data.cmd`;旧 `/ws/audio` 裸 `cmd` 不进入本版本产品协议与验收。
- **单命令策略**:一轮仅允许一个控制动作生成 `data.cmd`。若解析出多个控制动作,状态进入 `multi_command_blocked`,小歌返回“请拆成两句/请告诉我先执行哪一个”类 `data.reply`,不生成任何 `data.cmd`。
- **多命令合同样例**:`data.reply.multi_command_blocked.ask_split` 是 G1/G2 机器合同锚点。输入 `往前走一米再挥手`,来源 `SEED-017`,关联 `FR-CMD-003`;期望只有 `data.reply` ask_split,并显式禁止 `data.cmd`、`cmd_id` 和端侧执行副作用。
- **id 纪律**:`trace_id/session_id/utterance_id/cmd_id` 必填;`cmd_id` 云端生成且会话内唯一;云端和 SDK 均保留去重窗口。
- **能力纪律**:`capability_id` 必填,来自 `create_session`/`ctrl.hello` 授权能力和 P0 seed registry。能力缺失时不下发,或 SDK 回 `data.cmd_ack.status=rejected, code=capability_unsupported`。
- **风险纪律**:`risk_level` 枚举固定为 `low/medium/high`;`high` 确认前不生成 `data.cmd`。
- **时间纪律**:所有时间字段为 UTC epoch milliseconds integer;`ack_timeout_ms` 对应 `delivery_timeout`, `result_timeout_ms` 对应 `execution_timeout`。
- `cmd_group_id`、`step_index`、`step_count`、`execution_policy` 等多命令字段仅 P1 预留。P0 seed 验收不接受批量帧或多个 `data.cmd`。

#### 5.5.2 对接面(实施期)

| 对象 | 改动 |
| --- | --- |
| [clients/PROTOCOL.md](../../../clients/PROTOCOL.md) | 增 `data.cmd`、`data.cmd_ack`、`data.cmd_result`、`data.error`；明确本版本不支持旧裸 `cmd`/历史批量字段 |
| [docs/guide/CLIENT_INTEGRATION.md](../../guide/CLIENT_INTEGRATION.md) | 增 create_session + WSS `Authorization: Bearer` + no replay + close/error code；旧客户端协议不进入 R5.2.2 |
| Python SDK | 处理 `data.cmd`；立即回 `data.cmd_ack.status=accepted/rejected/duplicate`；转交执行模块后回 `data.cmd_result` |
| C / MATLAB SDK | 后续按 R5.2.2 schema 接入；旧未知帧容错不作为 P0 验收主体 |

### 5.6 语音回执与澄清(含跨轮补槽)

- **话术回执**:模板渲染不走 LLM(D-7),且不得与协议 `data.cmd_ack` 混用。P0 单条命令可在 `data.cmd_ack.status=accepted` 后播“已收到，正在执行”，在 `data.cmd_result` 终态后按成功/失败/取消/超时补播；未确认高危命令不得播“正在执行”。经 `session.say(reply, add_to_chat_ctx=按 XIAOGE_CMD_CTX)` 播报,**可被打断**(打断不撤回已下发指令)。
- **澄清**:unconfirmed 缺参 → 反问话术由参数 `desc` 生成("要往哪边转呀?");登记 pending 补槽 `{action, 已有参数, 缺参数列表, 剩余轮数=XIAOGE_CMD_PENDING_TTL}`。
- **补槽轮**:pending 存续时 gate 强制命中;L3 注入 pending 描述,模型把"左边/三十度"解析为补全。解析出新指令 → 丢弃 pending 正常处理;`[]` → 丢弃 pending 回落闲聊;TTL 到期自动过期。
- **线程纪律**:voicecmd 全部可变状态(pending 等)仅在 agent 事件循环读写;下发经 bridge 线程安全广播,不引入新跨线程写(聆听模式评审教训)。

### 5.7 上下文记录策略

- 指令轮 `raise StopResponse` → 用户句不进 chat_ctx(机制经聆听评审证实,agent_activity.py StopResponse 路径)。
- `XIAOGE_CMD_CTX=1`(默认)时 ack 走 `say(ack, add_to_chat_ctx=True)`——**必须显式传参**(聆听评审教训:不许照抄默认);上下文只落 assistant 侧回执,用户原句不落(已知不对称,V1 接受:解析器跨轮依据走自己的 L3 状态)。
- 人设 prompt 增补"设备控制"一节(能帮对方控制设备;被问能力时提及;对方描述设备操作但无执行凭据时请他直接说指令)。

### 5.8 失败模式与降级

| 场景 | 行为 |
| --- | --- |
| 功能关(`XIAOGE_CMD_ENABLE=0`) | 不装配,与今天逐行为一致 |
| 注册表非法 | 启动 fail-fast |
| gate 漏报 | 当闲聊;安全优先。P0 只补 seed/trigger 覆盖,不承诺 200+ 泛化召回;P1 再评估 embedding 召回层 |
| gate 误报 | 解析 `[]` 回落闲聊;多付一次解析延迟 |
| parser 超时/网关异常 | **不回落闲聊**:固定话术 `say("这条指令我没接住,再说一遍好吗。", add_to_chat_ctx=False)` + StopResponse(D-8) |
| JSON 无效 | 修复重试 1 次(计入总预算)→ 仍失败按超时同款 |
| validator 全剔除(全 unsupported) | 视同 `[]` 回落闲聊 |
| 域停用 | 触发词不命中→闲聊;偶发解析出→R1 剔除 |
| 解析在飞用户再说话 | 排队(母体不取消用户代码),解析完照常下发、排队轮再处理;最坏等待=总预算 5s |
| 聆听模式激活 | 聆听吞轮优先,聆听期不执行指令 |
| 停止词/附和 | 既有过滤优先,"别说了"永不会被当指令 |

### 5.9 代码落点清单(实施期)

| 落点 | 类型 | 内容 |
| --- | --- | --- |
| `examples/voice_agents/commands/*.yaml` | 新增·配置 | 种子 6 指令按 §5.1.1 迁移;真实表到货过门B |
| `voicecmd/registry.py` | 新增·纯核 | schema/加载/校验/四类导出 + CLI |
| `voicecmd/gate.py` | 新增·纯核 | 触发词门 |
| `voicecmd/prompt.py` | 新增·纯核 | L0-L3 拼装(预生成缓存) |
| `voicecmd/validate.py` | 新增·纯核 | R 规则表(§5.4 为准,现 R1-R12) |
| `voicecmd/parser.py` | 新增·I/O | LLM 调用/总预算/JSON 提取/重试 |
| `voicecmd/controller.py` | 新增·状态机 | gate→parse→validate→dispatch→ack 编排 + pending + 二次解析 |
| `voicecmd/config.py` | 新增·配置 | `CmdConfig` dataclass + `from_env()` |
| `app/voicecmd_host.py` | 新增·接线 | **唯一耦合点**:构造 `Deps`(cmd LLM、dispatch=bridge 两函数、say、turn-log)注入;热词三源 env 预写;入口 `handle_turn()` |
| `web_ui_agent.py` | 修改·小 | 末位受保护调用 + 尾部小重排(数字归一化段两处早退改"先定终文本→host 调用→按需回写",约 10 行)。单测锁定:**host 拿到链内终文本(含续讲改写与归一化),非 `new_message` 原文**;entrypoint 装配 ~3 行;人设加"设备控制"节 |
| `turn_config.py` | 修改·小 | 新增 `TURN_PREEMPTIVE`(preemptive_generation.enabled 旋钮,默认 True 零回归) |
| `providers/config.py` | **零改动** | 热词经 host 预写 env 注入,providers 不感知 |
| `clients/PROTOCOL.md` + `xiaoge_client.py` + CLIENT_INTEGRATION.md | 修改·小 | §5.5.2 |
| `.env.example` / `ourcode.txt` | 修改 | `XIAOGE_CMD_*` 配置块;新文件入清单 |
| `tests/test_ours_voicecmd_*.py` | 新增·测试 | §9.1 |
| `harness/cmd_eval.py` + `tests/data/cmd_corpus/` | 新增·评测 | §9.2 |
| `docs/README.md` | 修改 | design 索引加行(与首个实施 PR 同车) |

每个新文件按 [CODE_GUIDELINES.md](../../project/CODE_GUIDELINES.md) 控制规模;全部进 `make lint-ours`。

### 5.10 模块边界与耦合面(权威白名单,D-11)

voicecmd 包是**纯库**:只依赖 stdlib + `common/config_utils`,外部能力经 host 以 `Deps{parse_llm, dispatch_audio, dispatch_panel, say, turn_log}` 注入。单测喂 fake Deps,不碰框架对象。

| 接触面 | 方向 | 内容 | 性质 |
| --- | --- | --- | --- |
| `web_ui_agent.py` | 入口 → voicecmd | 末位受保护调用(host 未装配即跳过)+ 尾部小重排约 10 行 + entrypoint 装配 ~3 行 + 人设增补 | 唯一代码接线点 |
| `webpanel/bridge.py` | voicecmd → bridge | host 把 `broadcast_audio_ctrl`/`broadcast` 作为**函数值**注入 Deps;bridge 零改动 | 下游 sink,非模块耦合 |
| `FUNASR_HOTWORDS` env | voicecmd → STT | host 在 STT 装配前三源合并预写;providers 零改动 | 环境变量,单向 |
| `providers/config.DEFAULT_HOTWORDS` | voicecmd(host) → providers | **只读 import 常量**做三源合并;不触碰其他符号 | 只读常量,单向 |
| `runtime`(session_state) | 装配态存放 | 仅新增一个可选属性挂 host 实例(默认 None) | 存放点,非逻辑耦合 |

**禁止项(评审红线)**:聆听/KWS/打断/mute/providers/webpanel 任何模块 import voicecmd;voicecmd import 上述任何模块;voicecmd 直接触碰框架对象(session/agent)——要能力经 Deps。全部可变状态仅在 agent 事件循环读写。

---

## 6. 配置项(env;默认值 = 功能关/最保守,不设即零回归)

| key | 默认 | 说明 |
| --- | --- | --- |
| `XIAOGE_CMD_ENABLE` | `0` | 总开关;0 = 完全不装配 |
| `XIAOGE_CMD_DIR` | 包旁 `commands/` | 注册表目录(按包位置解析,不依赖 CWD) |
| `XIAOGE_CMD_DOMAINS` | 空 | 域白名单(逗号分隔);空 = 全部 enabled 域 |
| `XIAOGE_CMD_MODE` | `gate` | `gate`/`always`(评测调试) |
| `XIAOGE_CMD_TIMEOUT_S` | `5` | 解析**总预算**(首调+修复重试+二次解析) |
| `XIAOGE_CMD_MAX_N` | `10` | 单句指令上限 |
| `XIAOGE_CMD_CONF_MIN` | `0.5` | unconfirmed 阈值 |
| `XIAOGE_CMD_DOMAINS_TOPK` | `3` | 定域详规最多注入域数 |
| `XIAOGE_CMD_ACK` | `template` | `template`/`off` |
| `XIAOGE_CMD_CTX` | `1` | 回执写入 chat_ctx |
| `XIAOGE_CMD_PENDING_TTL` | `1` | 跨轮补槽轮数;0 关 |
| `XIAOGE_CMD_REASON` | `0` | 模型输出 reason(评测开) |
| `XIAOGE_CMD_HOTWORDS` | `1` | 注册表热词三源合并导出 |
| `XIAOGE_CMD_PANEL` | `1` | data.cmd/ack/result GUI timeline 镜像 |
| `XIAOGE_CMD_MODEL` / `_BASE_URL` / `_API_KEY` | 继承 `QWEN_*` | 解析器独立端点 |
| `XIAOGE_CMD_MAX_TOKENS` | `1024` | 解析输出上限 |
| `TURN_PREEMPTIVE`(TurnConfig 家族) | `1` | 抢跑生成总开关;指令重度场景可关以消除命中轮双付 |

## 7. 可观测性

- **turn log**:`CMD_GATE hit domains=[...]|miss`、`CMD_PARSE ms=.. n_raw=..`、`CMD_REPARSE domain=..`、`CMD_VALID ok=.. drop={R1:..,R5:..}`、`CMD_DISPATCH uid=.. n=..`、`CMD_ACK text=..`、`CMD_PENDING set|hit|expire`、`CMD_FALLBACK reason=empty|timeout|badjson`——每条决策路径显式留痕。
- **timeline**(`AGENT_TIMELINE=1`):gate/parse/dispatch 三事件点入既有时间线。
- **面板/GUI**:`data.cmd`、`data.cmd_ack`、`data.cmd_result` 与话术气泡进入 timeline。

## 8. 与现有功能的关系

| 既有功能 | 关系 |
| --- | --- |
| 聆听模式 | 吞轮在前,聆听期只记录不执行;退出尾巴窗同理 |
| 续讲确认 | 在 gate **之前**:小歌刚问"要不要继续听[故事]"的窄窗口内,裸"好/继续/行"被改写为"继续讲"+注入 system 指令——到达 gate 的是改写产物,属对提问的应答,语义优先级正确;误触发由多义词纪律(§5.1.2)消除 |
| 停止词/附和过滤 | 在前,"行了别转了"被停止词拦下并强打断——正确语义 |
| 数字归一化 | 在前,gate 拿归一化后文本;中文数词由 parser 处理 |
| KWS/在线打断 | 不变;回执可被打断,已下发指令不撤回 |
| 判停 | 不变,指令轮与闲聊轮共用 |
| 抢跑生成 | 默认开;命中轮抢跑作废、误报轮被复用(账见 §3.2);`TURN_PREEMPTIVE` 可关 |
| 热词 | host 三源合并后写回 env(用户 > DEFAULT > 注册表),上限 50/权重 20 |
| 录音/回放测试基建 | 指令轮照常录音;回放注入可加控制场景 |
| 并发/网关 | 无交集:voicecmd 在单会话 agent 进程内;协议演进与 protocol-v2 M1 同车 |

## 9. 验证计划与验收门槛

R5.2.2 两段验收:**P0 seed 全链**——以 R5.2.2 工作簿 `P0 seed命令表` 为唯一 seed 基线,每条必须有人工作业冻结的来源行、intent_type、action、capability_id、`params_example`、risk_level、unsupported/权限行为、正例和负例;以 `P0 registry schema` 作为机器可校验注册表输入,字段覆盖 action、capability_id、参数类型(`enum/int`)、枚举/范围/单位、默认值、risk_level、owner 和 unsupported 行为。registry schema 是语音意图 seed/registry,端侧可执行边界由 `delivery=data.cmd` 或 `delivery=data.cmd after confirmation` 决定。seed 跑 §9.1 单测 + §9.2 缩尺评测 + §9.3 真机/协议回放,并覆盖 `data.cmd -> data.cmd_ack -> data.cmd_result`、高危确认、no replay、unknown_cmd_id、multi-command ask_split。**规模重验证**——真实 200+ 表到货后重跑校验 CLI、全量评测与延迟基准,过门槛才在生产开大表;表到货不阻塞 P0 seed 评审。

### 9.1 单元测试(无 LLM,常跑)

- `test_ours_voicecmd_registry.py`:schema 校验矩阵、四类导出、examples 自举、保留字/多义词规则。
- `test_ours_voicecmd_gate.py`:命中/漏配/多域聚合/topk/pending 强制命中。
- `test_ours_voicecmd_validate.py`:R 规则表逐规则(§5.4 为准,现 R1-R12)+ 组合(互斥+截断+缺省+重编号)。
- `test_ours_voicecmd_controller.py`:fake parser 驱动——单命令下发/澄清/补槽/TTL/二次解析/超时(总预算口径);高危确认;multi-command ask_split;StopResponse 与回落路径。
- `test_ours_voicecmd_protocol.py`:R5.2.2 JSON 样例帧校验;`data.reply.multi_command_blocked.ask_split` 只产出 `data.reply` 且不产出 `data.cmd/cmd_id`;`cmd_ack.status=accepted/rejected/duplicate`;`unknown_cmd_id` 走 `data.error`;`ack_timeout_ms/result_timeout_ms` 事件名分离;8192/8193 bytes JSON 上限由 G2 mock 必测。
- `test_ours_voicecmd_host.py`:热词**三源合并**断言(未设 env 时合并集仍含 `"停":40` 全部 DEFAULT;用户设时用户优先;上限 50/权重 20)。

### 9.2 语料量化评测(opt-in 打真网关;报告入库本目录 `CMD_EVAL_REPORT.md`)

语料 `tests/data/cmd_corpus/*.yaml`(P0 seed 缩尺,全表后重验证),类别:每域单指令 / P0 seed 正例 / P0 seed 负例 / 多命令 ask_split / 高危确认/取消/超时 / 口语变体 / 中文数词 / **负样本闲聊 ≥300 条** / 混合句 / 域外指令 / 跨轮补槽对话对 / 合成满表组(规模预检)。

| 指标 | R5.2.2 P0 seed 起步门槛 |
| --- | --- |
| 单指令 action 准确率 | ≥ 97% |
| 单指令参数准确率(含缺省填充) | ≥ 95% |
| 多命令 P0 负例 | 100% 不生成 `data.cmd`,返回拆分提示 |
| 负样本误触发率 | 起步门:≥300 条且 **0 误触发**;放量后 ≤0.3% |
| gate 对 P0 seed/注册触发说法召回(always 对照) | ≥ 99%;不承诺 200+ 泛化召回或隐式指令召回 |
| 解析延迟 | P50 ≤ 1.5s,P95 ≤ 3s(**preemptive on 真实配置**下测,含网关并发;实测后校准) |

评测 runner `harness/cmd_eval.py`:gate+parser+validator 全链(不 dispatch)→ 分类通过率 + 失败样本清单,报告与语料入库。

### 9.3 真机验收(realdevice-log-loop)

Python demo/fake SDK 接 `data.cmd` 打印并回传 `data.cmd_ack/result`;用例:单指令/高危确认/多命令识别阻断/unsupported/ack timeout/result timeout/打断回执/补槽/聆听期不执行/**停止词优先(开热词导出配置下跑,兼验三源合并不回归)**/关开关回归。日志逐行对照 §5.8 表,全通过才合入。

**查询/知识类范围(R5.2)**:信息查询和固定知识问答由小歌自行处理,返回 `data.reply` + TTS,不下发控制 `data.cmd`。端侧硬件状态查询若需要执行器参与,必须明确归为控制/配置 seed,并按 cmd_ack/result 闭环验收。

## 10. 回退

- 运行时:`XIAOGE_CMD_ENABLE=0` 一键回纯闲聊。
- 协议:R5.2.2 主路径关闭后不生成 `data.cmd`;旧裸 cmd 不作为本版本回退路径。
- 代码:voicecmd 独立包 + 入口少量接线,revert 面干净。

## 11. 风险

| 风险 | 缓解 |
| --- | --- |
| 4B 解析质量不够 | 定域 prompt;temperature=0;评测门槛硬卡;`XIAOGE_CMD_MODEL` 可换强模型 |
| 命中轮双付成本(作废抢跑 LLM+TTS) | 入账为常态成本;评测附费用估算;`TURN_PREEMPTIVE=0` 可关 |
| 解析延迟 | 砍 reason;定域注入;实测基准;必要时换模型 |
| 门漏报 | triggers 纪律 + always 模式量化 + 清单驱动补词 |
| 200+ 泛化召回不足 | P0 seed 明确只保 trigger/seed 覆盖;真实表到货后重验证;embedding/semantic recall 作为 P1 增强并受 validator 兜底 |
| STT 设备词错 | 注册表热词三源导出 |
| 200+ 表质量(触发词冲突/ack 缺失) | fail-fast + 自举校验 + CLI 进 CI |
| 误执行设备安全 | unsupported/unconfirmed/confirm:true 永不下发;互斥裁决在代码 |
| conditional 客户端实现差异 | 契约固定"不成立或无法判定一律跳过";结构化优先 text 兜底;评测含 condition 正确性 |
| 与聆听/停止词/续讲交互回归 | 钩子链末位,顺序单测锁定;真机用例覆盖 |

## 12. 决策总账与开放问题

### 决策总账(D-1~D-16;溯源列指向[评审存档](VOICE_CMD_DESIGN_REVIEW.md)对应轮次)

| 编号 | 结论 | 溯源 |
| --- | --- | --- |
| D-1 | 选型 C:触发词门 + 专用解析 LLM | v1 §3;r1 核验成立 |
| D-2 | validator 唯一权威,LLM 输出仅候选 | v1;r1 核验成立 |
| D-3 | 输出契约继承参考件字段与约束 | v1;分歧项见 §2 表(r1 B-3、C-2) |
| D-4 | R5.2.2 P0 主路径为 protocol-v2 `data.cmd`;旧 `/ws/audio` additive cmd、裸 cmd、历史批量字段不进入本版本合同 | v1 历史 + 2026-07-31 R5 修订 + 2026-08-03 no-legacy 修订 |
| D-5 | 默认关、全 opt-in、母体零改动 | v1;项目铁律 |
| D-6 | 定域两级注入(L1 索引+L2 详规)+ 域外二次解析 | v1;r1 B-8 增设合成满表预检背书 |
| D-7 | 回执走模板 say,不走 LLM | v1 |
| D-8 | parser 失败/超时 → 固定澄清话术,不回落闲聊 | 负责人确认(2026-07-20) |
| D-9 | 种子先行(6 指令跑通全链,门B 兜底 200+ 表) | 负责人拍板(2026-07-20) |
| D-10 | conditional 透传:标注 + condition 对象,客户端判定,无法判定跳过 | 负责人拍板(2026-07-20) |
| D-11 | 模块解耦纪律:纯库 + Deps 注入,耦合面白名单 §5.10;整体+域级开关 | 负责人增补(2026-07-20) |
| D-12 | 注册表目录 `examples/voice_agents/commands/`,按包位置解析 | 负责人拍板(2026-07-20) |
| D-13 | 混合句 V1 只回执指令,残余不应答 | Q-2 闭合(r1,2026-07-21) |
| D-14 | V1 选择不按在线状态改话术(能力可得,V1.5 可选注入) | Q-3 闭合(r1 C-7,2026-07-21) |
| D-15 | `confirm:true` V1 强制语义 = R12 永不自动下发转澄清;表可整载 | 负责人拍板①(2026-07-28,B-1×Q-7) |
| D-16 | 历史 check_battery 门A 只验解析+下发;R5 修订后信息/知识由小歌直接回答,需要端侧执行器的查询按 cmd_ack/result 闭环 | 2026-07-31 R5 修订 |
| D-17 | 多命令分阶段:P0 识别多命令但阻断自动执行;P1 做低风险顺序多命令 group/step;P2 做并行、条件、高危混合、取消/回滚等复杂编排 | 负责人确认 2026-08-03 |

### 开放问题

唯一余留:**Q-6**(§9.2 验收门槛数值)——R5.2.2 P0 seed 起步值已入表,首轮评测后校准终拍,不阻塞设计评审。历史 Q-1~Q-8 全部已决,去向见决策总账与评审存档。

---

## 附录 A · 生成的解析 prompt 样例(motion+music 命中,示意)

```
[L0] 你是语音指令解析器。把用户话语拆解为独立控制指令,标注执行关系。
规则:按连接词与语义边界切分;句内省略参数按前序指令还原;同互斥组冲突全部保留并标 mutex(裁决在下游);
条件句("如果/要是…就…")标 conditional 并输出 condition:{text,ref_action,expect},不判断真假;
最多 10 条,超出截断;不是设备控制的话语输出 [];只允许出现"指令索引"里的 action;
输出纯 JSON 数组,元素 {action,params,confidence,sequence_id,relation[,condition]},relation ∈ serial|parallel|mutex|conditional;禁止其它任何文字。

[L1] 指令索引(action | 域 | 说明):
turn | motion | 转向控制
adjust_volume | audio | 音量调节
control_music | music | 音乐播放控制
switch_anc | audio | 降噪模式开关
control_led | light | 灯光开关
check_battery | power | 电量查询
...(满表 200 行)

[L2] 候选域详规:
## motion/turn:direction ∈ left|right|back(必填);angle 0-360(缺省 left/right=90,back=180)
例:向左转30度 → [{"action":"turn","params":{"direction":"left","angle":30},...}]
例:掉个头 → [{"action":"turn","params":{"direction":"back","angle":180},...}]
## music/control_music:command ∈ play|pause|up|down(必填)
例:暂停音乐 → [...]

[L3] 上下文:上轮用户:"向左转45度";上次下发:turn(left,45)。
用户本轮:"再转一次"
```

## 附录 B · 客户端收帧最小接入(Python)

```python
client.on_cmd = lambda payload: executor.submit(payload["commands"])
# xiaoge_client._handle_text 新增分支:
#   elif kind == "cmd": self._call(self.on_cmd, msg)
```

---

## 附录 C · 小歌全双工评审修订补充（2026-07-31）

本节是“小歌全双工语音交互需求”四轮评审后的命令侧补充，作为
`outputs/xiaoge_full_duplex_20260731/xiaoge_full_duplex_requirements_design_20260731_r5_review.xlsx`
的设计依据。它只修订设计口径，不表示工程代码已经实现。

### C.1 已确认命令边界

| 决策 | 命令设计影响 | 需求ID |
| --- | --- | --- |
| 小歌负责语义理解和命令抽取 | 云端小歌要支持 X3“离线技能清单”中的控制命令、信息查询和固定知识问答；“离线”不是小歌云端不支持的理由。 | FR-CMD-001, FR-INFO-001, FR-KB-001 |
| 端侧执行模块不属于本工程 | 小歌解析出标准化 `data.cmd`，端侧 SDK/应用/执行器负责动作执行，并回传 ack/result。 | FR-DEV-001, FR-CMD-006, FR-CMD-008 |
| 高危命令确认前不生成 `data.cmd` | 关机、重启、移动类越界动作等高风险命令必须先进入 `pending_confirmation`；用户确认通过后才允许下发。 | FR-CMD-005 |
| 投递与执行分离 | `data.cmd_ack.status=accepted/rejected/duplicate` 表示 SDK 投递确认；`data.cmd_result.status=running/succeeded/failed/canceled/timeout` 表示端侧执行进展/结果。话术、GUI 和日志必须分开处理两类超时。 | FR-CMD-006, FR-CMD-007, FR-CMD-008 |
| P0 不做命令重放 | 未 ack 命令在断线后不能自动重发，避免重复执行机器人动作。 | FR-CMD-008 |

### C.2 命令生命周期

P0 生命周期按以下状态实现和验收：

| 状态 | 进入条件 | 输出/动作 | 异常 |
| --- | --- | --- | --- |
| `intent_ready` | ASR/LLM 解析出候选控制意图 | 进入风险和能力校验 | 无命中则走聊天/问答/查询 |
| `risk_gate` | 有候选命令 | 判断 `risk`、目标能力、端侧在线状态 | 能力缺失或目标离线进入 `send_blocked` |
| `multi_command_blocked` | 一轮识别到多个控制动作 | 返回拆分/选择提示，不生成任何 `data.cmd` | P1 另行设计 `cmd_group_id/step_index` 等字段 |
| `pending_confirmation` | 命中 high risk 或策略要求确认 | 向用户请求确认，不生成 `data.cmd` | 拒绝/超时进入 `send_blocked` |
| `send_allowed` | 低/中风险通过，或高风险确认通过 | 分配 `cmd_id`，生成 `data.cmd` | 发送失败进入投递失败 |
| `ack_waiting` | `data.cmd` 已写入 WSS | 等待 `data.cmd_ack` | `ack_timeout_ms` 触发 `delivery_timeout`、断线未 ack、不重放 |
| `ack_accepted` | 收到 SDK `data.cmd_ack.status=accepted` | 等待 `data.cmd_result` 或先给投递回执 | `result_timeout_ms` |
| `result_ready` | 收到 `data.cmd_result` 终态 | 生成语音/文本回执，更新 GUI timeline | late/duplicate/unknown 只审计 |

### C.3 回执话术原则

| 场景 | 小歌响应 |
| --- | --- |
| 投递成功但执行未完成 | 可以短答“已收到，正在执行”，不得暗示执行已经成功。 |
| 执行成功 | 根据 `data.cmd_result.status=succeeded` 给出完成类回执。 |
| 执行失败/取消 | 基于 `code/message` 给出简短失败原因和可恢复建议。 |
| `delivery_timeout` | 表达“命令没有成功送达设备”，不说“执行失败”。 |
| `execution_timeout` | 表达“设备已收到命令，但结果暂未返回”。 |
| late result | 记录到 timeline，不主动打断当前轮，必要时 GUI 标记迟到结果。 |

### C.4 X3/客户命令追踪

命令范围以 `需求文件/智元X3语音交互需求0723-已讨论答复.xlsx` 的
`离线技能清单（待更新）` 和 `需求文件/客户cmd.xlsx` 为来源。本轮设计工件已经生成
`X3命令注册表追踪` sheet，自动抽取并去重 283 条候选命令，按以下字段作为后续冻结输入：

| 字段 | 用途 |
| --- | --- |
| `source` / `source_row` | 追溯原始需求文件和行号，避免需求遗漏。 |
| `skill_text` / `examples` | 保留用户说法、同义说法和原始描述。 |
| `intent_type` | 区分 `control_cmd`、`info_query`、`knowledge_qa`、`config`。 |
| `suggested_action` | 作为 action schema 冻结前的候选值。 |
| `params_hint` | 标注方向、角度、音量、模式、目标等参数线索。 |
| `risk_level` | 驱动高危确认和执行策略。 |
| `priority` | P0/P1/P2 拆分实施批次。 |

注意：自动抽取只用于评审和拆解，不等价于最终 schema。进入工程实现前，需要由端云双方共同冻结 action、params、risk、capability、回执策略和 mock 行为。

### C.5 与协议和 GUI 的联动

| 联动项 | 要求 |
| --- | --- |
| `data.cmd` | 必须带 `trace_id`、`session_id`、`utterance_id`、`cmd_id`、`capability_id`、`action`、`params`、`risk_level`、`ack_timeout_ms`、`result_timeout_ms`、`issued_at_ms`。 |
| `data.cmd_ack` | SDK 必须回传 `accepted/rejected/duplicate`；`unknown_cmd_id` 走 `data.error`/audit。 |
| `data.cmd_result` | 端侧应用/执行器经 SDK 回传 `running/succeeded/failed/canceled/timeout`。 |
| GUI timeline | 展示 STT final、命令、ack、result、异常；同时展示 `link_state` 与 `interaction_mode`。 |
| 角色/风格配置 | 用户侧配置影响回复风格；后台配置影响命令策略、风险等级和休眠资源策略。 |

### C.6 验收补充

P0 评审用例至少覆盖：

| 用例 | 期望 |
| --- | --- |
| 普通控制命令 | 生成 `data.cmd`，收到 `data.cmd_ack` 后等待或播报执行结果。 |
| 高危命令 | 确认前不出现 `data.cmd`；确认通过后才生成命令。 |
| 一轮多控制命令 | P0 不生成任何 `data.cmd`，返回拆分提示。 |
| 信息查询 | 小歌自行调用工具/数据源，返回语音和文本，不下发控制命令。 |
| 固定知识问答 | 小歌自行回答，返回语音和文本，不下发控制命令。 |
| 断线未 ack | 不重放，timeline 标记未投递。 |
| 重复/迟到/未知 result | 不重复播报、不污染当前轮，只做审计和 GUI 标记。 |
