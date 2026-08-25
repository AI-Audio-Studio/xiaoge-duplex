# 指令控制技能设计（结合智元 X3 语音交互技能清单）

> 需求来源：[../../智元X3语音交互需求0723-已讨论答复.xlsx](../../智元X3语音交互需求0723-已讨论答复.xlsx)（离线技能清单 249 行）
> 协议底座：[COMMAND_AUTH_DESIGN.md](COMMAND_AUTH_DESIGN.md) §2（`command`/`command_result` 已定协议，代码未落地）
> 安全约束：[COMMAND_AUTH_SECURITY_ASSESSMENT.md](COMMAND_AUTH_SECURITY_ASSESSMENT.md)（高危指令字段留 v2）
> 架构底座：[../../guide/ARCHITECTURE.md](../../guide/ARCHITECTURE.md)（LLM 全流程 + `on_user_turn_completed` 钩子 + 停止词规则层）
> 运行时补充：[COMMAND_SKILLS_RUNTIME_SPEC.md](COMMAND_SKILLS_RUNTIME_SPEC.md)（路由仲裁、指令状态机、回复策略、FAQ RAG 规格）
> 状态：设计稿，待评审。评审日期：2026-07-30。

---

## 1. 技能清单的三类分流

清单 249 行按处理路径归为三类，架构上走三条完全不同的链路：

| 类 | 内容 | 占比 | 处理路径 | 产物 |
| --- | --- | --- | --- | --- |
| **A 端侧指令** | 头/眼/眉/肢体动作、移动转身、拿物、导览控制、音量/TTS/语种设定、WiFi/蓝牙/开关机/充电 | ~150 行 | 意图识别 → `command` 下发端侧执行 | 指令目录 + 混合识别 |
| **B 查询类** | 天气/时间/单位换算/百科/健康/菜谱、查电量 | ~10 项 | 云侧 tool 执行，结果进 LLM 组织回复 | 云侧工具集 |
| **C 人设/知识库** | 个人人设(~15)、商品信息(~40)、企业知识库(~25) | ~80 行 | 提示词 + FAQ 知识检索注入，不是指令 | 人设卡 + QA 知识库 |

> **边界说明**：清单原始要求是"离线 ASR + NLU"（端侧离线技能）。本引擎是云侧全双工引擎（Excel 答复栏亦明确 GTK 不提供 ASR）。因此本方案实现的是**云侧同语义技能**——识别相同的句式、下发指令给端侧；端侧自身的离线兜底 NLU 不在本引擎范围。

---

## 2. 指令目录（Command Catalog）— A 类核心产物

~150 行技能**不映射成 150 个指令**，按"域.动作 + 参数"收敛为 **6 域约 25 个指令/配置名**（协议 `name` 字段，沿用 `域.动作` 约定）。LLM 工具层不必与这些细粒度 `name` 一一对应，v1 建议按 6 个域级 `@function_tool` 收敛，内部再映射到 catalog name（见运行时补充规格）。

### 2.1 motion 域（肢体）

| name | args | 覆盖的清单技能 |
| --- | --- | --- |
| `motion.head` | `action: nod\|shake\|look`, `direction: left\|right\|front\|back` | 点头/摇头/往左右前后看 |
| `motion.move` | `direction: forward\|back\|left\|right`, `distance_cm?: float`, `more?: bool` | 前后左右走、按距离走（米/厘米/公分，小数 1 位）、"再走一点" |
| `motion.turn` | `direction: left\|right\|back`, `slight?: bool`, `more?: bool` | 转身/左右转/向后转/再转一点 |
| `motion.body` | `action: stand\|squat\|sit\|lie\|get_up\|run\|stairs_up\|stairs_down\|slope_up\|slope_down` | 站蹲坐躺、摔倒爬起、跑步、上下台阶/坡道 |
| `motion.gesture` | `action: wave\|greet\|raise\|handshake\|salute\|thumbs_up\|fist_bump\|heart\|yeah\|move_hand\|turn_wave`, `hand: left\|right\|both` | 挥手/打招呼/举手/握手/敬礼/点赞/碰拳/比心/比耶/动动手/转身挥手 |
| `motion.perform` | `action: dance\|taichi\|pose\|custom\|next`, `name?: string`, `burst_count?: int` | 跳舞(#舞蹈名#)/太极/摆pose/N连拍/做动作(#动作名#)/换个动作 |

### 2.2 face 域（表情）

| name | args | 覆盖 |
| --- | --- | --- |
| `face.eye` | `action: close\|open\|blink\|squint\|widen\|roll\|spark\|wink_flirt\|spin`, `eye: left\|right\|both` | 眼睛全 20 项（闭/睁/眨/眯/瞪/翻白眼/转眼/放电/媚眼） |
| `face.brow` | `action: raise\|frown`, `side: left\|right\|both` | 眉毛 5 项 |
| `face.emotion` | `emotion: cry\|smile\|laugh\|smirk`, `intensity: small\|normal\|big` | 哭/笑系列 |

### 2.3 task 域（作业）

| name | args | 覆盖 |
| --- | --- | --- |
| `task.fetch` | `item: brochure\|water\|drink\|coffee`, `count?: int` | 拿宣传册/水/饮料/X 瓶 |
| `task.photo` | `action: lead_to_zone\|take` | 拍照区带领、一起拍照 |

### 2.4 tour 域（导览讲解）

| name | args | 覆盖 |
| --- | --- | --- |
| `tour.session` | `action: welcome\|start\|pause\|resume\|stop\|farewell\|return_origin` | 迎宾/开始/暂停/继续/结束讲解/告别/回原点 |
| `tour.point` | `action: prev\|next\|skip\|skip_next\|goto`, `index?: int`, `point_name?: string` | 讲解点管理 7 项（点位名走参数化配置，见 §5） |
| `tour.route` | `action: select`, `index?: int (0-99)`, `route_name?: string` | 讲解路线选择 |
| `tour.replay` | `language?: string` | 再讲一遍/换语种再讲 |
| `tour.mode` | `mode: auto\|manual` | 自动/手动讲解模式 |
| `tour.language` | `language: string` | 按指定语种讲解 |

### 2.5 speech 域（语音输出设定）——部分云侧自执行

音量在端侧扬声器 → 下发 command；TTS 音色/语速/风格在**云侧 TTS 引擎**（CosyVoice voice 参数、播报风格改提示词）→ 云侧直接执行，不下发或仅通知端侧：

| name | args | 执行方 |
| --- | --- | --- |
| `speech.volume` | `mode: absolute\|delta\|max\|min`, `value?: int(0-100)`, `delta?: up\|down` | 端侧（command） |
| `speech.voice` | `gender?: male\|female\|child`, `style?: sweet\|calm\|cartoon\|gentle...` | **云侧**（切 `COSYVOICE_VOICE`，复用 SwitchableTTS 热切换） |
| `speech.rate` | `rate: fast\|normal\|slow` | 云侧 |
| `speech.report_mode` / `speech.report_style` | `standard\|concise` / `professional\|humorous` | 云侧（改系统提示词片段） |
| `speech.language` | `language: string \| next` | 云侧（LLM/TTS 语种）+ 通知端侧 |

### 2.6 system 域（高危，需确认门）

| name | args | 风险 |
| --- | --- | --- |
| `system.connectivity` | `target: wifi\|bluetooth`, `state: on\|off` | 中（关 WiFi 会断自己的连接——下发前 TTS 提醒） |
| `system.power` | `action: reboot\|shutdown` | **高：先语音二次确认**（"确定要关机吗？"→ 用户确认后才下发） |
| `system.battery` | `action: query\|go_charge` | query 走 `require_reply=true`，回执电量值进 LLM 播报 |

---

## 3. 混合意图识别：三级漏斗

> 定位先明确：快路径是**延迟优化，不是正确性保障**——漏了落到 LLM 兜底，功能不受损。要提升的是"省掉 LLM 往返"的比例；误触发比漏召回危害大（机器人做错动作 vs 慢一秒），阈值取保守。

```
STT final → on_user_turn_completed
 ├─ ⓪ 停止词/附和（现有逻辑，不动）
 ├─ ① 正则/词表精确层：高频短指令（挥手/音量调到X/往前走X米）+ 槽位归一化
 │     命中即出 command + 短确认语 + StopResponse，~0ms，误报率≈0
 ├─ ② 语义召回层：句向量相似度匹配
 │     文本 → bge-small-zh 向量 → 与注册表内"扩写句式库"算余弦
 │     sim ≥ τ_high(如0.88) → 直接出指令（槽位再用①层规则抽取）
 │     τ_low ≤ sim < τ_high  → 不出指令，把 top-3 候选意图作为 hint 注入 LLM 提示词
 │     sim < τ_low           → 纯 LLM
 └─ ③ LLM function calling 兜底（泛化担当，~18 个 @function_tool 与指令目录一一对应）
       LLM 判定为指令意图 → 调 tool → 处理器下发 command → tool 返回值生成确认语
       LLM 判定为闲聊/查询 → B/C 路径
```

### 3.1 ① 正则/词表精确层

- **落点**：与现有停止词同层（`common/text_rules.py` 旁新增模块），复用其"引导词 + 核心词 + 尾词"正则骨架——该架构已在停止词上验证适合中文口语指令。
- **匹配表由注册表生成**（见 §5），每条 = 正则模板 + 槽位提取器 + 目标 `(name, args)`。仅收录**高频、无歧义**句式（清单"参考句式"列基本可直接转成模板）：如 `往[左右前后]?(边)?走 X (米|厘米|公分)`、`音量(调到)? X%`、`[左右双]?手?(挥手|比心|敬礼|点赞)`。
- **槽位归一化**：距离（"一米"→100cm、"50 公分"→50，小数 1 位）、音量（"七成"→70、"大一点"→delta up）、序数（"第 3 个"→3）。中文数字解析复用现有数字归一化逻辑并扩展。
- **歧义保护**：疑似但不确定的（如"你能不能走两步啊"）**不硬匹配，放行给下层**——精确层只吃确定性收益。
- **与抢先生成兼容**：命中后 `raise StopResponse`，框架作废已抢跑的 LLM 生成，语义与现有停止词一致，无需改框架。

### 3.2 ② 语义召回层（关键新增组件）

技术选型对比（按代价从低到高）：

| 方案 | 代价 | 评价 |
| --- | --- | --- |
| **句向量匹配（bge-small-zh-v1.5 / m3e-small，ONNX CPU）** | 无需训练；~24M 参数，单句推理 10–20ms | **✅ 选定**。句式库直接从注册表来（每技能挂 10–30 条扩写句式，LLM 离线批量生成 + 人工过一遍）；加技能=加句子，零训练。与本工程"本地 ONNX 小模型"技术栈（VAD/EOU/KWS）一致 |
| PaddleNLP UIE-nano/mini（零样本抽取） | 免训练，但 Paddle 依赖重 | 槽位抽取强，但引入第二套推理框架，不采用 |
| 训练式意图分类+槽位（JointBERT / RBT3 / TinyBERT 微调） | 需标注数据 + 训练管线 + 迭代维护 | 准确率上限最高，但 90 个意图的数据要造、技能一变要重训——**v2 再评估**，训练数据可先用②层运行日志积累 |
| 专职小 LLM（Qwen3-0.6B 本地结构化输出） | 100–300ms，额外部署一个模型 | 延迟优势相对主 LLM 不够大，性价比低，不采用 |

- **意图与槽位解耦**：②层只判意图，命中后槽位（距离/音量/左右手/第 N 个）仍用①层归一化规则抽——中文数量/方位槽位是封闭集合，规则抽取比模型可靠。槽位抽取失败则降级 LLM。
- **索引规模**：18 意图 × 10–30 句 ≈ 数百条向量，内存线性扫描即可，无需向量库。
- **预期效果**：①+② 合计对清单内指令直出率 80%+；τ_high 保守可把误触发压到接近零。

### 3.3 ③ LLM function calling 兜底

- v1 建议 6 个域级 tool 对应 §2 的 6 个域，参数用 `Annotated` 枚举约束（框架已有 `annotated_tool_args.py` 示范）；工具内部再映射到约 25 个 catalog name，降低 Qwen3-4B 的工具选择压力。
- 工具描述写清中文触发语境与参数解析规则（LLM 负责泛化："眨一下你的小眼睛"→`face.eye{blink,both}`）。
- Qwen3-4B 的工具选择准确率需实测；若域级 tool 仍影响准确率/首 token 延迟，可进一步合并为单个 `dispatch_command` 工具，`domain/action` 作参数。②层的 hint 注入（候选意图提示）也用于提升工具选择准确率。

---

## 4. 下发、回执与执行语义

沿用 [COMMAND_AUTH_DESIGN.md](COMMAND_AUTH_DESIGN.md) §2 协议（`command`/`command_result`/`call_id`/`require_reply`/`timeout_ms`/错误码 5001-5003），本方案补充策略层：

| 策略 | 规则 |
| --- | --- |
| **require_reply** | 默认 `false`（动作类发完即走，语音确认与执行并行）；`system.battery.query`、`tour.point.goto`（可能失败：点位不存在）等**需要结果影响回复**的置 `true`，`timeout_ms=5000`，超时按 5003 处理，LLM 播报"没能完成" |
| **回执等待实现** | `web_audio.py` 上行文本帧解析 `command_result` → 按 `call_id` 匹配 `asyncio.Future` 字典唤醒等待中的 tool（COMMAND_AUTH_DESIGN §4 落地清单已预留此项） |
| **动作互斥** | 云侧不做动作排队——串行/打断/合并是端侧运动控制的职责；云侧只保证同一会话指令按下发顺序到达。端侧忙时回 `error_code:"busy"`，云侧播报"我正在做上一个动作" |
| **确认语策略** | 快路径命中：极短确认（"好的"）或静默（按域配置：表情类静默、移动类确认）；LLM 路径：由 LLM 自然生成确认语 |
| **高危确认门** | `system.power` 类指令注册表标记 `confirm:true`：首轮只反问，会话状态记 pending_command（30s TTL），下一轮用户肯定答复才真正下发。安全评估报告建议的 `expires_at/seq/幂等` 等字段留作 v2，与换 Token 方案一并升级 |

---

## 5. 技能注册表：单一事实来源

清单多处要求"参数化配置"（语种名称、讲解点位、舞蹈/动作名单、音色列表）。设计一个 **`skills_registry.json`**（或 py 声明式表）作为唯一来源，启动时派生四份产物：

```
skills_registry.json
  ├─ 每条: { name, args_schema, fast_patterns[], example_utterances[],
  │          tool_desc, require_reply, confirm, exec: device|cloud }
  ├─→ 生成 ①精确层规则表（fast_patterns）
  ├─→ 生成 ②语义召回层句式向量索引（example_utterances）
  ├─→ 生成 ③@function_tool 集合（动态注册，框架有 dynamic_tool_creation.py 示范）
  └─→ 生成给端侧的指令目录文档（clients/PROTOCOL.md 附录）
```

- **动态枚举**（讲解点位/路线/舞蹈名）：端侧或后台通过配置注入（v1 用 `.env`/JSON 文件；v2 走会话建立时端侧上行 capabilities 帧）。注入后同时更新精确层模板、句式库与 tool 描述枚举。
- 加一个技能 = 注册表加一条，四处自动同步，不改代码。

---

## 6. B 查询类：云侧工具

### 6.1 工具通道选型：function calling，不用 MCP

MCP 与 function calling 不是对立技术：MCP 只是**工具的发现与传输协议**，到 LLM 侧仍是 function calling。选择标准是工具"住在哪"：

| | 进程内 `@function_tool` | MCP |
| --- | --- | --- |
| 延迟 | 函数直调，0 开销 | 多一跳进程/网络往返 |
| 会话耦合 | 天然可访问 session/bridge（**command 下发必须拿会话上下文**，指令工具只能进程内） | 跨进程拿会话状态很别扭 |
| 适用 | 本项目全部 A 类指令工具 + 天气/汇率等简单 HTTP 查询 | 工具由独立团队维护/多产品共享/第三方生态接入时 |

**结论：v1 全部走 `@function_tool`**（框架原生支持）。框架本身带 MCP 客户端（`llm/` 含 MCP 模块），且注册表是声明式的——将来若工具生态化（如导览点位服务独立成服务），把注册表里 `exec: cloud` 条目切到 MCP server 是局部改动，不动架构。

### 6.2 工具清单

| 技能 | 实现 |
| --- | --- |
| 查天气 | `@function_tool get_weather(city, district?, date?, metrics?)` → 外部天气 API（选型待定：和风/高德等，需 key）。上下文 5 轮由现有 ChatContext 天然满足 |
| 查时间/日历/时区/节假日 | 云侧本地计算（`zoneinfo` + 农历库 + 节假日表），无外部依赖 |
| 单位换算 | 度量衡本地算；汇率走 API + 快照缓存（对应清单"离线存最近汇率快照"要求） |
| 百科/健康/菜谱 | **不做独立工具，LLM 直答**——Qwen3-4B 自身知识覆盖清单示例问题；健康类在系统提示词加"就医提醒 + 紧急症状（胸痛/呼吸困难等）建议立即就医"安全约束 |
| 查电量 | 归 A 类 `system.battery`（require_reply 回执数值） |

---

## 7. C 人设与知识库：QA 检索式轻量 RAG

当前体量（人设+商品+企业 ~80 条 QA）小，但**不全量塞提示词**：3–6k token 常驻拖每轮 TTFT，与全双工低延迟目标冲突。

- **形态：FAQ 检索（QA-pair retrieval），不是文档切块 RAG**。清单本身就是"问题→标准答案"结构，对"问题句"做向量索引，命中后把**答案**注入——比 chunk 检索准确得多，也天然满足"话术必须官方口径"要求（售价/安全性答案不允许 LLM 自由发挥）。
- **检索栈**：bge-small-zh 向量 + 内存线性扫描（几百条不需要向量库；上千条再上 sqlite-vec）。**与 §3.2 语义召回层共用同一个 embedding 模型和 ONNX 推理会话**——一份模型，两个索引（指令句式库 / 知识 QA 库）。
- **注入点**：`on_user_turn_completed` 之后、LLM 调用前，检索 top-3（阈值过滤）作为 context 消息注入本轮 ChatContext，标注"以下为官方口径，据此回答"。检索 ~15ms，不影响抢先生成。
- **提示词分层**：`核心人设（名字/身份/定位，常驻系统提示词，保证任何话题下人设不崩） + 播报风格片段（speech.report_style 可切） + 场景知识（按部署态配置，检索注入）`——与 §2.5 云侧自执行指令联动。当前 VoiceAgent 的小歌人设需替换/参数化为灵犀 X2/X3 人设。
- **演进**：8 个部署态场景知识扩展后，加 bge-reranker（粗排→精排）和场景命名空间（按部署态加载对应库）。仓库 `llamaindex-rag` 示例可参考但**不引入 LlamaIndex**——几百条 QA 自研 ~200 行更可控，符合项目最小依赖取向。

---

## 8. 落地路线（3 期）

| 期 | 内容 | 依赖 |
| --- | --- | --- |
| **P1 通路** | `command` 下发（bridge）+ `command_result` 回执（web_audio + Future 匹配）+ 注册表骨架 + LLM tools（motion/face 两域先行）+ 端侧 SDK/PROTOCOL.md 同步 | 协议已定，无外部依赖 |
| **P2 全目录+快路径** | 18 指令全量 + ①精确层 + ②语义召回层（bge-small-zh ONNX + 扩写句式库） + 槽位归一化 + 高危确认门 + speech 域云侧执行（TTS 热切换联动） | P1；句式库生成 |
| **P3 查询+知识库** | 天气/汇率 API 接入、时间/换算本地工具、人设卡替换、QA 知识检索注入、动态枚举注入（讲解点位等） | 外部 API key、人设/口径资料由需求方提供 |

**验收对齐清单指标**：指令类在线句准率 ≥95%（安静）——把清单"参考句式"列直接转成回归测试集（文本层测 ①②直出率 + ③工具选择准确率，不依赖音频），挂进现有 pytest。

---

## 9. 需要新增的资产与开放项

**新增资产**：
- 各技能扩写句式库（每技能 10–30 条，LLM 离线批量生成 + 人工过一遍）——供 ②语义召回 + 回归测试集。
- 官方口径 QA 库（人设/商品/企业，需求方提供权威版本）。
- bge-small-zh-v1.5 ONNX 模型文件（放 `models/`，与 KWS 同为数据资产，gitignore）。

**开放项（需与需求方/端侧确认）**：
1. **speech.voice 音色映射**：清单要"男/女/童声 + 甜美/沉稳/卡通"，需确认 CosyVoice 可用音色列表能否覆盖（童声、卡通不一定有现成 voice）。
2. **天气/汇率 API 选型**及 key 采购。
3. **人设资料**：X2/X3 参数、售价话术、公司知识库文本的权威版本。
4. **端侧指令目录确认**：§2 的 name/args 需与端侧执行方（智元）对齐后冻结进 `clients/PROTOCOL.md`。
