# 会话协议 v2(protocol-v2)设计——协议分面、传输合线

| 项 | 值 |
| --- | --- |
| 版本 | v1.9(R5.2.2 no-legacy 合同修订版;内容基线 = v1.8 + 云侧开工前问题闭环 + 负责人确认不兼容旧客户端) |
| 状态 | **设计期·零代码**——只修订需求、协议、合同和验收口径;工程实现仍需评审通过后另行批准 |
| 缘起 | 负责人提议统一客户端 SDK 并按控制/数据双通道拆分(2026-07-22);经 Q1-Q3 三问修正为"报文分面、传输合线"终案(负责人认可 2026-07-23) |
| 评审存档 | [PROTOCOL_V2_DESIGN_REVIEW.md](PROTOCOL_V2_DESIGN_REVIEW.md)(r1 意见、应答、拍板台账——只读,溯源唯一入口) |
| 关联 | [../voice-cmd/VOICE_CMD_DESIGN.md](../voice-cmd/VOICE_CMD_DESIGN.md)(cmd 帧契约,本设计 M1 载体) · [../../../clients/PROTOCOL.md](../../../clients/PROTOCOL.md)(现行协议) · [../../guide/CLIENT_INTEGRATION.md](../../guide/CLIENT_INTEGRATION.md)(网关对接指南:模式 A/B、宽限窗、限频、认证预留) · [../concurrency/](../concurrency/README.md)(网关/池架构,"1 连接=1 会话=1 进程"不变量) |
| 目录索引 | [README.md](README.md)(文件清单与维护规则) |

**版本沿革**(详细过程见评审存档)

| 版本 | 日期 | 里程碑 |
| --- | --- | --- |
| v1 | 2026-07-23 | 初稿:单会话连接 + ctrl.*/data.* 命名空间;方言协商;caps;迁移 M1-M3;五项已否决入档 |
| v1.1 | 2026-07-28 | 同步 main 67d2d83:五帧现状、网关模式 A/B 与宽限窗、限频、认证预留 |
| v1.2 | 2026-07-28 | r1 应答(0A+5B+6C 全接受):B1 caps 口径、B2 网关帧上限事实重写、B3 音频先行、B4 hello 幂等、B5 utterance_id 分配等 |
| v1.3 | 2026-07-28 | 负责人六项拍板:DV2-7/11 修订版 + DV2-12~15 |
| v1.4 | 2026-07-28 | 定稿整理(结构重组,零语义变更) |
| v1.5 | 2026-07-31 | 全双工 R5 修订:HTTPS create_session + WSS 实时面、P0 单命令 `data.cmd`、`data.cmd_ack`/`data.cmd_result` P0、历史批量字段降为 P1 |
| v1.6 | 2026-07-31 | 全双工 R5.1 修订:WSS token 唯一承载为 `Authorization: Bearer <access_token>`;`ctrl.hello` 不带 token;补齐 create_session/WSS auth/hello/ready schema;`protocol_error=4400`,`affinity_lost=4001`;JSON 8KB 与二进制 32KB 分离 |
| v1.7 | 2026-07-31 | 全双工 R5.2 修订:每类 P0 WSS JSON 显式冻结 `type`;`ctrl.hello.role` 必填;`data.stt/data.reply/ctrl.clear` 纳入权威 schema;ack/result/error 字段与样例帧对齐 |
| v1.8 | 2026-07-31 | 全双工 R5.2.1 最小补丁:create_session 补齐 `client_version/config_snapshot`;`ctrl.status/get/set/ack` 标注为 P1/非本轮 P0;`ctrl.clear.reason?` 与 schema/样例对齐 |
| v1.9 | 2026-08-03 | 全双工 R5.2.2 修订:本版本不兼容旧客户端;P0 caps 机器枚举冻结为 `audio/text/cmd/state`;移除 legacy close code;G2 必测 8KB JSON;P0 识别多命令但不自动编排执行 |

---

## 1. 背景与目标

### 1.1 现状(均已代码核实)

- `/ws/audio`:设备唯一连接。二进制帧=语音(上行麦克风 PCM / 下行 TTS);文本帧=服务端→客户端**五种** JSON——`ready/clear/busy` + `user_partial/message` 转写帧(main 67d2d83 起 bridge `_AUDIO_FORWARD_TYPES` 无差别转发,PROTOCOL.md §2 五帧表);客户端→服务端只发音频,上行文本被静默忽略。转写帧转发即本设计 data 面下发的"旧方言先行版",老 SDK 容错已获真流量验证。
- `/ws`:浏览器面板单向推送(气泡/状态灯/聆听态);按钮命令走 REST `/api/mic·asr·tts`(后两者服务器形态被 `XIAOGE_ADMIN_ROUTES=0` 隐藏)。main 后已按"模式 B"对带亲和 cookie 的协议客户端开放(CLIENT_INTEGRATION §6)——v2 收敛后回归调试面(DV2-7 修订版)。
- 网关现实(CLIENT_INTEGRATION.md):历史对接分**模式 A**(裸连 /ws/audio)与**模式 B**(GET / 拿亲和 cookie → /ws + /ws/audio + /api/mic,断线 12s 宽限窗接回同进程、帧续接);网关级限频 `XG_MSG_RATE=200 条/s`、帧上限 `XG_MAX_FRAME_BYTES=32KB`(**仅二进制**,§4.7)。R5.2.2 产品协议在 `create_session` 签发短期 `access_token`,WSS Upgrade 仅通过 `Authorization: Bearer <access_token>` 承载,网关在分配 agent 前校验;历史模式 A/B 不进入本版本合同、mock 或验收。
- 并发架构不变量:**1 条 WS = 1 会话 = 1 个池进程**,网关按连接做亲和(tag `concurrency-deploy-v1`)。

需求(负责人,2026-07-22):①面板与设备客户端统一为一个 SDK;②页面可配置模型/系统参数;③设备可上行状态;④通道语义清晰(控制 vs 数据),气泡文本归数据面;⑤指令帧归数据面。

### 1.2 目标

| 编号 | 目标 |
| --- | --- |
| GP1 | **统一客户端 SDK**:设备与用户面板共用一套协议与连接模型 |
| GP2 | **语义分面**:每种报文明确归属 ctrl 面或 data 面,文档与代码一眼可辨 |
| GP3 | **no-legacy 收敛**:本版本只验收新 SDK + `/ws/session` 产品协议;旧三端 SDK、旧端点和旧裸帧不进入 R5.2.2 合同与验收 |
| GP4 | **网关/池零改动**:保持"1 连接=1 会话=1 进程",不引入跨连接绑定 |
| GP5 | **打断正确性不降级**:clear 与音频的相对顺序保持传输层保证 |
| GP6 | **分阶段承载三需求**:R5.2.2 P0 冻结对话内容下发(data.*,caps 订阅);用户级配置(ctrl.get/set)与设备状态上行(ctrl.status)为 P1/非本轮 P0 预留 |
| GP7 | **轮次可关联**:data 面报文携带 utterance_id,转写/回复/指令/回执可对齐 |

### 1.3 非目标(V2 首期)

- 不做**应用层**会话恢复:宽限窗外/模式 A 重连 = 新会话(DV2-11 修订版;窗内由网关无缝接回,系网关机制)。
- 不做多设备寻址/一连接多会话(单客户端语义不变)。
- 不做服务端用户偏好存储(偏好客户端自带,§4.2)。
- P0 不做多命令编排执行:一轮识别到多个控制动作时进入 `multi_command_blocked`,返回拆分/选择提示,不生成任何 `data.cmd`;低风险顺序多命令作为 P1,复杂任务编排作为 P2。
- P0 不做未 ack 命令重放:断线前未收到 `data.cmd_ack` 的命令不得重发,避免端侧重复执行。
- 浏览器调试面板 `/ws` 与其页面不属产品协议(DV2-7 修订版)。

---

## 2. 讨论沿革(结论的来路)

| 轮次 | 内容 | 产出 |
| --- | --- | --- |
| R1(负责人提案) | 双端点拆分:/ws→/ws/ctrl,/ws/audio→/ws/data | 方向确立:控制面/数据面分离;统一 SDK 愿景 |
| R2(设计者分析) | 双端点两笔入场费:会话绑定 + clear 跨通道竞态(epoch 机制) | 竞态显性化;面板语音模式即跨通道 clear 活样本 |
| R3(负责人三问) | Q1 气泡为何走 /ws;Q2 clear 迁移得不偿失?;Q3 并发后 REST 还适合每用户配置? | Q2 砍掉最贵入场费(clear 不迁);Q3 定"用户级配置走会话内消息,REST 归运维" |
| R4(设计者终案) | **协议分面、传输合线**:一条会话连接,报文按 ctrl.*/data.* 分面;双端点降为"可无损拆分"后门 | 负责人认可(2026-07-23) |

**归面规则(一句话)**:跟"某一轮对话"相关的内容归 **data 面**(音频、转写、回复、指令、执行回执);跟"会话/设备/系统"相关的交互归 **ctrl 面**(握手、准入、打断、状态、配置、上报)。

---

## 3. 方案总述

一条 WebSocket 会话连接(端点 `/ws/session`,R5.2.2 产品协议唯一实时入口):

- **会话准备**:客户端先 `POST /api/v1/sessions`,完成设备鉴权、能力协商、配置快照与短期 `access_token` 签发;该短连接不占 agent 池位。
- **WSS 鉴权**:`GET /ws/session` 的 Upgrade 请求必须携带 `Authorization: Bearer <access_token>`;`ws_url` 不带 token,`ctrl.hello` 不带 token。无/错/过期 token 在 agent 分配前拒绝。
- **二进制帧**:语音,格式不变(16k/单声道/int16 LE 裸 PCM)。
- **文本帧**:JSON 报文,`type` 按点号命名空间分 `ctrl.*` 与 `data.*` 两面。
- **握手纪律**:客户端首帧必须发 `ctrl.hello`。R5.2.2 不提供旧方言协商、不做旧帧翻译;未按新协议握手的连接按协议错误处理。
- **caps 订阅**:hello 声明消费面,服务端按订阅下发——纯音频小设备零文本开销。
- **入口收敛**(DV2-16):产品客户端一律 `create_session` + 单连接 `/ws/session` + caps;历史 `/ws/audio`、裸 `cmd`、历史批量字段不进入本版本合同;`/ws` 回归浏览器调试面板。
- 物理同线 ⇒ 全部帧序有保证 ⇒ 打断(ctrl.clear)与音频天然有序(GP5)——"传输合线"的核心红利。

```
统一客户端 SDK ──── 一条 WS /ws/session ──── 小歌服务端
  设备形态           二进制面:PCM ↑ / TTS ↓(不变)                1 连接=1 会话=1 进程
  用户面板形态        ctrl.* 面:hello↑ ready↓ busy↓ clear↓        (网关亲和照旧)
  (caps 订阅)               state↓ ; status↑/get-set↑/ack↓ 为 P1 预留
                     data.* 面:stt↓ reply↓ cmd↓ cmd_ack↑ cmd_result↑
                              (全部带 utterance_id)

  浏览器调试面板 /ws + REST 运维面(/healthz、poolmgr control_api):保持现状,不属产品协议
```

---

## 4. 详细设计

### 4.1 连接、握手与 no-legacy 准入

- **产品客户端**:先 `create_session` → 拿到 `trace_id/session_id/access_token/ws_url/granted_caps/config_snapshot` → 以 `Authorization: Bearer <access_token>` 建立 `/ws/session` → 首个 JSON 文本帧发 `ctrl.hello`(非敏感 `device_id/caps/prefs`,不带 token)→ 服务端回 `ctrl.ready`(session id、采样率、授予 caps、配置版本)→ 该连接按 R5.2.2 报文收发。
- **no-legacy 规则**:R5.2.2 不兼容旧客户端协议。旧 `/ws/audio`、无 `ctrl.hello`、裸 `ready/clear/busy/user_partial/message/cmd`、历史批量字段均不进入产品协议、schema、examples、close-code 合同和 G2 mock 验收。
- **音频面先行条款**:二进制音频帧可在 `ctrl.ready` 前后流动,但必须建立在 WSS 鉴权通过和会话已创建的前提上。SDK 建连后需立即具备收播能力;欢迎语完整性作为集成测试项。
- **hello 幂等可重发**:已建立的 R5.2.2 连接再收 hello → 覆盖 caps/prefs、回同一 `ctrl.ready`(session 不变),不重置会话。网关宽限窗接回的是**同一条 agent 侧连接**(proxy.py REATTACH:上游不动、只换客户端);SDK 重连后重发 hello 无害且推荐。
- **非法握手**:hello 缺失、解析失败、字段非法、携带 token、未知 caps 等均视为 `protocol_error`,返回 `data.error.code=protocol_error` 或关闭 `4400`;不得静默降级为旧方言。
- **紧急开关**:`XIAOGE_PROTO_V2=0` 仅可用于本地/运维调试禁用新协议入口,不得作为生产旧协议回退承诺;生产回退走版本回滚或流量切换。

### 4.2 会话模型、身份与偏好

- `ctrl.ready` 下发 `session`(会话 id):**复用网关会话标识**(DV2-12)——agent 侧透出 `X-XG-Session`(网关注入,server.py 已读取);无网关形态(PC 调试)回退本地随机短 id。它同时是日志与 utterance_id 的对账前缀(voice-cmd 的 `u-<会话短id>-<自增>` 共用同一短 id)。会话断开→优雅退出走 `_request_graceful_exit`(池回收)。
- **重连口径**(DV2-11 修订版):**宽限窗内 = 同会话延续**——网关无缝接回同一 agent 侧连接,hello/caps/prefs/握手状态存续(配 §4.1 幂等条);**窗外 = 新会话**(与池回收一致,不做应用层恢复)。
- **宽限窗内下行丢帧与命令 gate**(R5 修订):窗内(客户端缺位)上游下行帧**丢弃不回放**;进入 `PendingReconnect` 后 `send_allowed=false`,小歌不得下发新的 `data.cmd`。已发送但未收到 `data.cmd_ack` 的命令按 `delivery_timeout`/未投递处理,P0 不重放;已 ack 后的迟到 `data.cmd_result` 只进入审计或按策略补播,不污染当前轮。
- `hello` 携带 `device_id`(设备持久标识)与 `prefs`(用户偏好)。**偏好客户端自带、服务端仅本会话应用、不落盘**(DV2-5)——池进程用完即回收,服务端存偏好需外部存储,首期不做;跨设备同步需求出现再议(§10)。

### 4.3 caps 订阅

- `create_session.request.caps`、`ctrl.hello.caps`、`create_session.response.granted_caps`、`ctrl.ready.granted_caps` 的 P0 机器枚举冻结为 `["audio","text","cmd","state"]`,数组必须非空且去重。
- `audio` → PCM/TTS 二进制实时音频面;`text` → `data.stt`+`data.reply`;`cmd` → `data.cmd`+`data.cmd_ack`+`data.cmd_result`;`state` → `ctrl.state`+`ctrl.frontend_state`。
- `granted_caps` 回授予集(服务端白名单 ∩ 请求集);未授予的面不得发送。未知 caps 项按 `protocol_error` 拒绝,不做静默忽略,防止端云各自扩展。
- `ctrl.status`、`ctrl.get/set` 为 P1/非 R5.2.2 P0 预留面,不进入本轮权威 P0 schema;后续若启用,由准入与限频约束(§4.7)。

### 4.4 报文总表

信封纪律(DV2-9,双向对称):文本帧必为 JSON 对象;`type` 必填且**序列化首位**;未知 `type` 双向静默忽略(仅 debug log);单帧上限 8KB(音频二进制除外);违规帧丢弃计数。

**WSS Upgrade 鉴权(R5.2.2 权威口径)**:`Authorization: Bearer <access_token>` 是 P0 唯一 token 承载方式。`access_token` 由 `create_session` 返回,`ws_url` 不含 token;`ctrl.hello`、URL query、后续 JSON 帧均不得携带 token。

**ctrl.\* 面**

| type | 方向 | 字段 | 说明 |
| --- | --- | --- | --- |
| `ctrl.hello` | ↑ | trace_id, session_id, proto=2, role(device/panel), device_id, caps[], prefs{} | 连接首帧;建立 R5.2.2 会话;幂等可重发(§4.1);不得携带 token |
| `ctrl.ready` | ↓ | trace_id, session_id, sample_rate, granted_caps[], config_version | hello 应答;会话建立 |
| `ctrl.busy` | ↓ | message | 准入拒绝(语义同现行 busy) |
| `ctrl.clear` | ↓ | utterance_id?, reason? | 打断清播放;与音频同线帧序天然有保证(§4.5);`reason=barge_in/user_stop/system_cancel/sleep` |
| `ctrl.state` | ↓ | trace_id, session_id, link_state, interaction_mode, engine_gate, resource_state, ts_ms, pending_confirmation? | 状态推送;GUI 三态以 `interaction_mode=sleeping/dialogue/listening` 为准 |
| `ctrl.frontend_state` | ↑ | trace_id, session_id, seq, ts_ms, ttl_ms, trust_level, wake_state, wake_event, vad, doa, lock_mode | 端侧主唤醒和状态标签;`trust_level=authoritative` 才可触发模式迁移 |
| `ctrl.status` | ↑ | seq, fields{} | P1/非 R5.2.2 P0;设备状态上报预留;限频默认 ≤1 帧/s、≤2KB(QV2-5);落状态存储+turn log+面板镜像 |
| `ctrl.get` / `ctrl.set` | ↑ | req_id, keys[] / set{} | P1/非 R5.2.2 P0;用户级配置读写预留(§4.6) |
| `ctrl.ack` | ↓ | req_id, ok, applied{}/error | P1/非 R5.2.2 P0;get/set 应答;req_id 幂等(§7) |

**data.\* 面**(全部带 `utterance_id`,GP7)

| type | 方向 | 字段 | 说明 |
| --- | --- | --- | --- |
| `data.stt` | ↓ | trace_id, session_id, utterance_id, text, final(bool), ts_ms | 用户转写;final=false 即现 user_partial 语义;P0 只强制 final |
| `data.reply` | ↓ | trace_id, session_id, utterance_id, intent_type, text, ts_ms, speak_policy? | 小歌回复文本(闲聊、查询、知识、回执、确认话术同走此报文) |
| `data.cmd` | ↓ | trace_id, session_id, utterance_id, cmd_id, capability_id, action, params, risk_level, ack_timeout_ms, result_timeout_ms, issued_at_ms | P0 单命令指令帧;`cmd_id` 云端生成且会话内唯一;多命令 group/step 字段仅 P1 预留 |
| `data.cmd_ack` | ↑ | trace_id, session_id, utterance_id, cmd_id, status, code, message, received_at_ms | SDK 投递确认;`status=accepted/rejected/duplicate`,不含 `unknown` |
| `data.cmd_result` | ↑ | trace_id, session_id, utterance_id, cmd_id, status, code, message, started_at_ms?, finished_at_ms?, duration_ms?, retryable? | P0 端侧执行进展/结果;`status=running/succeeded/failed/canceled/timeout` |
| `data.error` | 双向 | trace_id, session_id, code, message, retryable, ts_ms | 协议/权限/容量/unknown_cmd_id 错误;`unknown_cmd_id` 走 error/audit,不污染 ack 状态 |

**R5.2.2 机器合同锚点**:

- `data.reply.multi_command_blocked.ask_split`:输入话术 `往前走一米再挥手`,来源 `SEED-017`,关联 `FR-CMD-003`;期望状态 `multi_command_blocked`,话术风格 `ask_split`,输出类型只有 `data.reply`;合同字段显式禁止 `data.cmd`、`cmd_id` 和端侧执行副作用。
- `xiaoge-duplex-protocol-r5.2.2.source-check.json` 必须能从该 example 反查到工作簿 `P0 seed命令表/SEED-017` 与 `命令状态机/multi_command_blocked` 行。若 examples、工作簿或状态机任一处漂移,合同生成应失败。

**id 与时间字段规则**(GP7/R5 的实现根基):`trace_id` 由 `create_session` 建立根 trace 后贯穿 HTTPS/WSS;`session_id + utterance_id + cmd_id` 是 P0 必填组合。`cmd_id` 由云端生成,会话内唯一,云端与 SDK 均保留去重窗口。该轮**首个 partial/final 时刻**由**单一分配器**取 `utterance_id`;该轮全部 `data.*` 报文与 `ctrl.clear` 的关联字段共享同号。所有时间字段使用 UTC epoch milliseconds integer,字段名以 `_ms` 结尾。

### 4.5 打断语义(为什么 clear 不跨线)

`ctrl.clear` 挂控制面命名空间(语义归属),物理上与音频同一条连接(传输归属)——**语义分类与物理顺序解耦**。同线 ⇒ clear 之后到达的音频必属新一轮,客户端"清缓冲、继续收"即正确,机制与现行一致、零新增。跨线方案需 epoch/segment 对齐机制,已否决(§10 #2;负责人 Q2 结论)。v2 增量仅一处:clear 可带 `utterance_id` 与 `reason?`,打断从"清一切"精确到"作废第 N 轮"(客户端可选消费),并支持 GUI timeline/audit 展示原因。

### 4.6 用户级配置与运维边界

- **用户级(ctrl.get/set,白名单)**:P1/非 R5.2.2 P0 预留。判据三条——只影响本会话、不抢共享资源、是偏好非拓扑。首批候选键(QV2-2 实施前定稿):`mic.muted`、`tts.voice`(限运维放行集合)、`cmd.ack`、`listen.*`、`welcome.enabled`。
- **运维级(不给用户)**:STT/TTS/LLM 后端与端点、池参数、判停/VAD 阈值、热词全库、admin 开关——走 poolmgr control_api 与部署配置。
- `XIAOGE_ADMIN_ROUTES` 语义延续到消息级:关闭时 `ctrl.set` 对运维影子键一律 `ok:false, error:"operator-scope"`;REST `/api/asr·tts` 隐藏机制不变,`/api/mic` 保留给本地调试页。
- REST 归运维面:`/healthz` 与 poolmgr control_api 不变;**不再为产品功能新增 REST**(ctrl 连接本身即会话,Q3 结论)。

### 4.7 纪律与防护

- **限频限大小**:`ctrl.status`/`ctrl.get/set` 为 P1/非 R5.2.2 P0 预留,若启用则分别限制为 `ctrl.status` ≤1 帧/s、≤2KB;`ctrl.get/set` ≤5 请求/s;超限丢弃+计数+log(数值 QV2-5 实测校准)。R5.2.2 将 **WSS JSON 文本单帧上限冻结为 8KB(8192 bytes,按 UTF-8 序列化后字节数)**,由 SDK 发送前限制 + agent/sessproto 应用层校验;超限返回 `data.error.code=protocol_error` 或关闭 `4400`。G2 必测 8192 bytes 通过、8193 bytes 拒绝。**网关对齐(按代码事实)**:网关消息速率 `XG_MSG_RATE=200 条/s` 对**所有帧**生效;帧大小 `XG_MAX_FRAME_BYTES=32KB` **仅约束二进制 PCM**(gateway/proxy.py `_pump_cli2up` 的 TEXT 分支透传无大小检查)。因此 8KB JSON 与 32KB binary 是两个不同上限,不得混用。
- **status 信任面**:`ctrl.frontend_state.trust_level` 是端侧状态标签可信度,只在鉴权会话、有效 ttl、单调 seq、授予 `state` caps 下参与状态迁移;`observe/hint/authoritative` 的语义以合同说明为准,不得越权驱动高危命令。
- **保留字规则生命周期**:R5.2.2 不再为旧 C/MATLAB 朴素解析器兼容而收窄 P0 协议;注册表仍应避免 action/枚举值与协议 `type` 名、错误码、close code 名同名,该规则是新协议命名卫生,不是旧客户端兼容验收。
- **认证**(DV2-13/R5.2.2):默认采用设备注册表 + HMAC/JWT 短期 `access_token`;若依/mTLS 作为后续可替换认证源。P0 唯一承载方式为 WSS `Authorization: Bearer <access_token>`,网关在**分配 agent 前**校验;URL query 和 `ctrl.hello` 不携带 token。鉴权失败不得占用 agent/pool ready。
- **错误码与关闭码**(R5.2.2):`protocol_error=4400`;`token_expired=4401`;`permission_denied=4403`;`duplicate_connection=4009`;`resource_exhausted/busy` 可用 HTTP 503 或 WS `1013`。`affinity_lost=4001` 为历史 Gateway 兼容语义,不进入 R5.2.2 P0 合同。能力错误统一为 `capability_unsupported`,不得再使用 `capability_missing`。

---

## 5. 与既有系统的关系与实施落点

| 对象 | 关系 |
| --- | --- |
| 网关/池(concurrency) | **零改动**(GP4):仍 1 连接=1 会话=1 进程;上行 TEXT 透传已由评审代核闭合(proxy.py `_pump_cli2up` TEXT 分支逐帧透传) |
| voice-cmd | R5 起以 `data.cmd` 单命令 schema 为主契约;注册表/风险/回执需产出 `capability_id/action/params/risk_level` 并绑定 `utterance_id/cmd_id` |
| voice-cmd 执行闭环 | `data.cmd_ack` + `data.cmd_result` 已进入全双工 P0;投递超时和执行超时分别验收 |
| 浏览器调试面板 /ws | 回归调试面板;不作为产品协议实时数据面 |
| clients/PROTOCOL.md | R5.2.2 产品协议只认 `create_session`、`/ws/session`、`ctrl.frontend_state/state`、`data.cmd_ack/result/error`;旧裸 `cmd`、历史批量字段不进入合同 |
| 测试基建 | 录音/回放/timeline 不受影响;v2 报文进 turn log(§8) |

**实施落点速览**(实施期展开为逐文件清单):

| 落点 | 阶段 | 内容 |
| --- | --- | --- |
| `gateway` / session API | G1 | `POST /api/v1/sessions`、token、scope、close/error code、busy/resource_exhausted、trace_id |
| 新模块(暂名 `sessproto/`) | G1/G2 | 纯核:信封校验/R5.2.2 握手状态机/caps 枚举过滤/限频器/req_id 幂等/utterance_id/cmd_id 分配器、ack/result 状态机 |
| `webpanel/bridge.py` 或其调用侧 | G2/G3 | `user_partial/message` 规范化为 `data.stt/reply`;新增 `data.cmd_ack/result/error` timeline |
| `webpanel/server.py` 路由 | G1 | `/ws/session` 注册;旧 `/ws/audio` 不进入 R5.2.2 产品协议与验收 |
| `clients/python/`(统一 SDK v2) | G1/G3 | create_session、token WSS、hello/caps、frontend_state、cmd_ack/result、错误码、no replay |
| `.env.example` / `ourcode.txt` / `tests/test_ours_sessproto_*.py` | G1-G6 | R5.2.2 schema、token TTL、timeout、dedupe、capacity 参数与契约测试 |

---

## 6. 迁移计划(每步独立可发、旧客户端不纳入验收)

| 阶段 | 内容 | 风险控制 | 验收要点 |
| --- | --- | --- | --- |
| **G1 schema freeze** | 冻结 `create_session`、caps 枚举、`ctrl.state/frontend_state`、`data.cmd/cmd_ack/cmd_result/error`、close code、trace/id/time 字段 | 不改工程;只放行契约评审;旧协议明确 out of scope | 同一 JSON 样例端云通过;unknown caps、旧入口、旧裸帧均有拒绝口径 |
| **G2 mock freeze** | 云端 fake server 与客户端 fake SDK/executor 按 schema 回放 | 端云可并行开发,不互等真实模块 | ack_timeout/result_timeout/duplicate/unknown/multi-command ask_split、caps enum、8192/8193 bytes JSON 用例可回放 |
| **G3 command loop** | 单命令 `data.cmd -> cmd_ack -> cmd_result`、高危确认、GUI timeline | P0 不做未 ack 重放;不自动编排多命令 | P0 seed 正/负例逐条通过;多命令只识别+阻断+提示 |
| **G4 multi-command P1** | 低风险、明确顺序的 2-3 条命令分组执行 | 在 G3 单命令闭环稳定后进入;新增 group/step 字段需重新评审 | 串行 step ack/result、部分失败策略、取消策略和回执聚合通过 |
| **G5 deployment/local** | 生产入口、鉴权、容量和本地 PC 开发模式验收 | 本地调试可保留 dev-only 路径,但不进入产品合同 | 公网只暴露新入口;本地 smoke 不回归;N+1 busy/resource_exhausted 可测 |
| **G6 capacity** | Demo Hot 参数验收;生产 N/SleepingWarm 压测后签收 | 不把 Demo 结论外推生产 | N+1 busy/resource_exhausted 可测 |

voice-cmd 与 protocol-v2 **并行不悖**:R5.2.2 先冻结 schema、seed 和 mock;工程阶段按 `data.cmd` 主协议实现。多命令在 P0 只做识别、阻断和提示;P1 另走 group/step 设计增补,不得反向污染 P0 schema。

## 7. 失败模式与降级

| 场景 | 行为 |
| --- | --- |
| `XIAOGE_PROTO_V2=0` | 仅本地/运维调试禁用新协议入口;生产不得承诺旧协议回退 |
| hello 非法/解析失败 | `protocol_error`:返回 `data.error` 或关闭 `4400`;不得静默降级 |
| 已建立连接再收 hello | 幂等:覆盖 caps/prefs、回同一 ctrl.ready(session 不变),不重置会话 |
| 未知 caps 项 | `protocol_error`;G2 negative example 必测 |
| `ctrl.status` 超频/超大 | P1/非 R5.2.2 P0 预留行为:丢弃+计数;连续违规仅 log,不断连 |
| `ctrl.set` 越权(运维影子键) | P1/非 R5.2.2 P0 预留行为:`ok:false, error:"operator-scope"` |
| `ctrl.set` 未知键 | P1/非 R5.2.2 P0 预留行为:`ok:false, error:"unknown-key"` |
| req_id 重复 | P1/非 R5.2.2 P0 预留行为:幂等重放上次结果(窗口=会话内最近 N 条,N 实施前定) |
| 旧客户端/旧服务端组合 | 不属于 R5.2.2 验收;需要使用旧版本服务或升级客户端 |
| 宽限窗内下行 | 网关丢帧不回放(§4.2);pending reconnect 期间不发送新 `data.cmd`;未 ack 命令 P0 不重放 |

## 8. 可观测性

- turn log:`PROTO_HELLO role=.. device=.. caps=[..]`、`PROTO_DROP reason=ratelimit|oversize|badjson|unknown_cap|bad_hello n=..`;P1/非 R5.2.2 P0 预留 `CTRL_STATUS seq=.. bytes=..`、`CTRL_SET keys=.. ok=..`。
- 会话短 id 贯穿:网关日志/agent 日志/utterance_id/面板同源(DV2-12)。
- 面板镜像:P1/非 R5.2.2 P0 预留 `ctrl.status`/`ctrl.set` 结果镜像到调试面板 `/ws`(沿用 broadcast)。

## 9. 验证计划

- **单测**(纯核):R5.2.2 P0 覆盖信封校验(type 首位/未知忽略/大小上限);握手状态机(hello 前后/非法/幂等重发/开关禁用);caps 枚举矩阵;8192/8193 bytes JSON 上限;utterance_id 分配器(同轮同号/残轮作废)。get/set 白名单与越权、req_id 幂等、status 限频器为 P1/非本轮 P0 预留测试。
- **集成测**(真进程/真端口):新 SDK × 新服务端(R5.2.2 全量,含**欢迎语完整性**)、旧入口/旧裸帧拒绝、混连(一设备 + 调试面板)。
- **真机**(realdevice-log-loop):M1 随 voice-cmd 门A;M2 单独出包验 hello/caps 与 P0 data.* 主链路;配置/状态链路为 P1/非 R5.2.2 P0 预留,日志对照 §7 表。

## 10. 已考虑并否决(含 revisit 触发)

| # | 方案 | 否决理由 | revisit 触发 |
| --- | --- | --- | --- |
| 1 | 双物理端点 /ws/ctrl + /ws/data(负责人原提案) | 网关需跨连接会话绑定(破 GP4);嵌入式双连接配对;ctrl-only 客户端需求不存在。**命名空间设计保留无损拆分后门**:届时 ctrl.* 原样搬独立端点,报文定义零改动 | 出现真实 ctrl-only 客户端需求 |
| 2 | clear 迁出音频连接(+epoch/segment 机制) | 跨 TCP 无帧序保证→漏音竞态;补偿机制=三端+服务端新复杂度;收益仅语义纯洁(负责人 Q2)。面板语音模式即此竞态活样本(本地低延迟掩盖) | 无 |
| 3 | REST 承载每用户配置 | 多用户下 REST 需自建会话粘滞;ctrl 连接本身即会话(负责人 Q3) | 无 |
| 4 | 服务端存用户偏好 | 池进程用完即回收,需外部存储;客户端自带天然无状态 | 跨设备偏好同步成真需求 |
| 5 | 气泡文本留 /ws 不进数据面 | 设备拿不到转写;无轮次关联;与统一 SDK 矛盾;caps 消化小设备带宽顾虑(Q1) | 无 |

## 11. 风险

| 风险 | 缓解 |
| --- | --- |
| 旧客户端无法直接接入新服务 | R5.2.2 明确 no-legacy;端侧统一升级 SDK;如需旧端继续工作,另维持旧版本服务,不得混入本合同 |
| caps/prefs 键膨胀 | 白名单集中单一权威表,未知键拒绝;新增键走设计增补 |
| 双 ready + 音频先行混淆实现者 | 协议时序图写死;SDK v2 封装内消化 |
| JSON 8KB 漏校验 | SDK 发送前限制 + agent/sessproto 二次校验 + G2 8192/8193 bytes mock 用例硬卡 |
| WS 库文本消息上限未知值 | M1 实施期核实数值并落文(文本大小权威在 agent 侧的前提) |

## 12. 决策总账与开放问题

### 决策总账(DV2-1~15;溯源指向[评审存档](PROTOCOL_V2_DESIGN_REVIEW.md))

| 编号 | 结论 | 溯源 |
| --- | --- | --- |
| DV2-1 | 协议分面、传输合线:一条会话连接 + ctrl.*/data.* 命名空间;双端点转否决#1 留后门 | 负责人认可 2026-07-23;r1 核验成立 |
| DV2-2 | 历史结论:方言协商 = hello 触发,连接属性;无版本号。R5.2.2 被 DV2-16 覆盖:不再兼容旧方言 | 同上;r1 补音频先行与幂等条款;2026-08-03 no-legacy 修订 |
| DV2-3 | clear 留同线挂 ctrl.* 命名空间;不做跨线打断对齐 | 负责人 Q2 结论;r1 核验(clear 已同线) |
| DV2-4 | caps 订阅制,授予集 = 白名单 ∩ 请求集;R5.2.2 P0 机器枚举为 `audio/text/cmd/state`,未知项按 `protocol_error` | 2026-07-23;r1 B1 口径统一;2026-08-03 云侧问题闭环 |
| DV2-5 | 身份/偏好客户端自带(hello.device_id+prefs),服务端不落盘 | 2026-07-23;池回收模型背书 |
| DV2-6 | 用户级配置后续走 ctrl.get/set(白名单);REST 归运维;admin 语义延续消息级。R5.2.2 标注为 P1/非本轮 P0,不进入本轮权威 schema | 负责人 Q3 结论 + R5.2.1 第 8.3 修订 + R5.2.2 版本更新 |
| DV2-7(修订版) | 历史结论为端点 `/ws/session` 作为 `/ws/audio` 演进别名;R5.2.2 被 DV2-16 覆盖:`/ws/session` 是产品协议唯一实时入口,旧路径不进入合同 | 负责人拍板 2026-07-28(QV2-8);2026-08-03 no-legacy 修订 |
| DV2-8 | R5.2.2 P0 主协议为单命令 `data.cmd`;旧裸 `cmd` 和历史批量字段不进入合同。多命令在 P0 只识别+阻断+提示,P1 另增 group/step 设计 | 2026-07-31 R5 修订;2026-08-03 多命令分阶段修订 |
| DV2-9 | 信封纪律:type 必填且首位、未知双向忽略、限频限大小 | 2026-07-23;r1 核验 |
| DV2-10 | 已否决五项入档(§10)带 revisit 触发 | 2026-07-23;r1 五项理由全核实 |
| DV2-11(修订版) | **宽限窗内=同会话延续**(接回继承状态,hello 幂等);**窗外/模式 A=新会话**;窗内下行丢帧不回放,R5 以 `data.cmd_ack`/`data.cmd_result` 和 no replay 策略处理 | 2026-07-31 R5 修订 |
| DV2-12 | 会话身份复用网关标识(X-XG-Session 透出;无网关回退本地短 id) | 负责人拍板 2026-07-28(QV2-6) |
| DV2-13 | R5.2.2 默认设备注册表 + HMAC/JWT 短期 `access_token`;若依/mTLS 预留为认证源;P0 唯一承载为 WSS `Authorization: Bearer <access_token>`,网关分配前校验,hello 仅带非敏感 device_id | 负责人拍板 2026-07-28(QV2-9,"要 token") + R5.1/R5.2/R5.2.1/R5.2.2 复审修订 |
| DV2-14 | 网关不补文本帧大小上限;文本大小权威 = SDK 发送前限制 + agent/sessproto 应用层校验 + WS 库上限;G2 必测 8192/8193 bytes | 负责人拍板 2026-07-28(QV2-10);2026-08-03 云侧问题闭环 |
| DV2-15 | `XIAOGE_PROTO_V2` 保持默认开;紧急回退语义,理由 §4.1 | 负责人拍板 2026-07-28(QV2-11) |
| DV2-16 | R5.2.2 no-legacy:本版本不兼容之前客户端协议,不提供旧协议翻译/降级/兼容矩阵;旧入口若为本地调试保留,必须标记 dev-only 且不进入合同验收 | 负责人确认 2026-08-03 |

### 开放问题(全部不阻塞设计放行)

| 编号 | 问题 | 状态 |
| --- | --- | --- |
| QV2-1 | caps 粒度 | 已闭合(R5.2.2):P0 枚举 `audio/text/cmd/state`;后续拆分新增 caps 必须走协议增补 |
| QV2-2 | 用户级配置白名单首批键 | 不阻塞:候选表起步,`tts.voice` 放行集合 M2 实施前由运维给定 |
| QV2-3 | prefs 键规范/req_id 幂等窗口 N | 已闭合(r1):单键拒绝不整帧拒;与 set 白名单同一权威表;N 实施前定 |
| QV2-4 | status 最小字段集 | 不阻塞:信封先行;battery/charging 优先(联动 voice-cmd D-16 升级路径);外部依赖比照 voice-cmd Q-4 |
| QV2-5 | 限频/大小默认值 | 不阻塞:起步值可用;M2 实测校准;表述以 §4.7 事实版为准 |
| QV2-6~11 | — | 全部已决 → DV2-12~15 与 DV2-7/11 修订版 |

---

## 附录 A · 报文样例

```jsonc
// 短连接建会话(HTTPS ↑)
{"device_id":"robot-x3-001","credential":{"key_id":"dev-key","signature":"hmac-signature"},"caps":["audio","text","cmd","state"],"prefs":{"welcome.enabled":true},"audio_format":{"sample_rate":16000,"channels":1,"sample_format":"int16le"},"client_version":"x3-sdk-r5.2.2"}
// 建会话响应(HTTPS ↓)
{"type":"session.created","trace_id":"trace-20260731-0001","session_id":"sess-0001","access_token":"jwt-short-lived","expires_in_ms":600000,"ws_url":"wss://host/ws/session","granted_caps":["audio","text","cmd","state"],"config_snapshot":{"config_version":"cfg-001"}}
// WSS Upgrade: GET /ws/session
// Authorization: Bearer jwt-short-lived
// 握手(↑ 首帧)
{"type":"ctrl.hello","trace_id":"trace-20260731-0001","session_id":"sess-0001","proto":2,"role":"device","device_id":"dev-a1b2","caps":["audio","text","cmd","state"],"prefs":{"tts.voice":"longxiaochun_v3","welcome.enabled":true}}
// 会话建立(↓;session 复用网关标识)
{"type":"ctrl.ready","trace_id":"trace-20260731-0001","session_id":"sess-0001","sample_rate":16000,"granted_caps":["audio","text","cmd","state"],"config_version":"cfg-001"}
// 设备状态(P1/非 R5.2.2 P0 预留;↑,≤1/s,≤2KB)
{"type":"ctrl.status","seq":17,"fields":{"battery":82,"charging":false}}
// 配置写与应答(P1/非 R5.2.2 P0 预留)
{"type":"ctrl.set","req_id":"r-3","set":{"cmd.ack":"off"}}
{"type":"ctrl.ack","req_id":"r-3","ok":true,"applied":{"cmd.ack":"off"}}
// 一轮对话的数据面(↓,同 utterance_id,自首个 partial 取号)
{"type":"data.stt","trace_id":"trace-20260731-0001","session_id":"sess-0001","utterance_id":"utt-0003","text":"往前走一米","final":true,"ts_ms":1789000000100}
{"type":"data.cmd","trace_id":"trace-20260731-0001","session_id":"sess-0001","utterance_id":"utt-0003","cmd_id":"cmd-0001","capability_id":"motion.move","action":"navigation.move","params":{"direction":"forward","distance_cm":100},"risk_level":"medium","ack_timeout_ms":800,"result_timeout_ms":5000,"issued_at_ms":1789000000200}
{"type":"data.cmd_ack","trace_id":"trace-20260731-0001","session_id":"sess-0001","utterance_id":"utt-0003","cmd_id":"cmd-0001","status":"accepted","code":"sdk_received","message":"accepted by SDK","received_at_ms":1789000000300}
{"type":"data.cmd_result","trace_id":"trace-20260731-0001","session_id":"sess-0001","utterance_id":"utt-0003","cmd_id":"cmd-0001","status":"succeeded","code":"done","message":"completed","started_at_ms":1789000000400,"finished_at_ms":1789000001600,"duration_ms":1200}
{"type":"data.reply","trace_id":"trace-20260731-0001","session_id":"sess-0001","utterance_id":"utt-0003","intent_type":"control_cmd","text":"好的，已完成。","ts_ms":1789000001700}
// 多命令阻断合同样例(data.reply.multi_command_blocked.ask_split;SEED-017;FR-CMD-003;不得生成 data.cmd/cmd_id/端侧执行副作用)
{"type":"data.reply","trace_id":"trace-20260803-multi-0001","session_id":"sess-0001","utterance_id":"utt-multi-0001","intent_type":"control_cmd","text":"我听到了两个操作：往前走一米、挥手。请拆成两句，或告诉我先执行哪一个。","ts_ms":1789000000800,"speak_policy":"ack"}
// 打断(↓,同线有序)
{"type":"ctrl.clear","trace_id":"trace-20260731-0001","session_id":"sess-0001","utterance_id":"utt-0004","reason":"barge_in"}
```

---

## 附录 B · 小歌全双工评审修订补充（2026-07-31）

本节是“小歌全双工语音交互需求”四轮评审后的协议侧补充，作为
`outputs/xiaoge_full_duplex_20260731/xiaoge_full_duplex_requirements_design_20260731_r5_2_2_review.xlsx`
的设计依据。它只修订设计口径，不表示工程代码已经实现。

### B.1 已确认协议基线

| 决策 | 协议影响 | 需求ID |
| --- | --- | --- |
| 生产客户端只连接小歌 Gateway | 设备侧通过 HTTPS 创建会话，再用单条 WSS 长连接承载实时语音、文本、状态、命令；客户端不直连 Agent/PoolManager。 | FR-CONN-001, FR-CONN-002, FR-CONN-003, FR-CONN-004 |
| 短连接只做会话准备 | HTTPS `create_session` 负责设备鉴权、能力协商、配置快照、短期 `access_token` 签发；WSS 负责实时交互。`access_token` 仅通过 WSS `Authorization: Bearer` 承载。 | FR-CONN-002 |
| 机器人侧主唤醒 | 小歌不做主语义唤醒。客户端在本地 KWS/按钮/GUI 唤醒后，发送 `ctrl.frontend_state` 和增强后 PCM。 | FR-MODE-002, FR-AUD-002 |
| sleeping 保持 WSS | `sleeping` 是交互模式，不是断链。GUI 必须能同时看到 `link_state=connected` 与 `interaction_mode=sleeping`。 | FR-MODE-002, FR-GUI-003, FR-GUI-004 |
| P0 Demo 默认 SleepingHot | Demo 保持 Agent 热态；生产通过 `idle_sleep_release_policy` 在 `SleepingHot/SleepingWarm/ReleasedIdle` 间切换。 | FR-OPS-003, NFR-CAP-001 |
| 命令投递确认与执行结果分离 | 新增 `data.cmd_ack`；`data.cmd_result` 进入全双工 P0 设计范围，不再只作为远期预留。 | FR-CMD-006, FR-CMD-008 |
| P0 不做命令重放 | 未 ack 命令在断线后标记为未投递/失败，重连后不自动重发，避免机器人动作重复执行。 | FR-CMD-008 |

### B.2 状态与配置消息补充

`ctrl.state` 建议固定拆分以下维度，避免把链路、交互和资源状态混成单一状态：

```jsonc
{
  "type": "ctrl.state",
  "trace_id": "trace-20260731-0001",
  "session_id": "sess-0001",
  "link_state": "connected",          // connecting | connected | reconnecting | closed
  "interaction_mode": "sleeping",     // sleeping | dialogue | listening
  "engine_gate": "closed",            // closed | open | kws_only
  "resource_state": "SleepingHot",    // SleepingHot | SleepingWarm | ActiveAgent | ReleasedIdle
  "ts_ms": 1789000000100
}
```

客户端可用 `ctrl.frontend_state` 上报端侧前端标签或命令提示，小歌按可信度决定是否作为模式切换、ASR 辅助或仅审计：

```jsonc
{
  "type": "ctrl.frontend_state",
  "trace_id": "trace-20260731-0001",
  "session_id": "sess-0001",
  "seq": 17,
  "ts_ms": 1789000000200,
  "ttl_ms": 1000,
  "trust_level": "authoritative",
  "wake_event": "local_kws",
  "wake_state": "awake",
  "vad": "speech",
  "doa": 15,
  "lock_mode": false
}
```

GUI 配置和后台配置均走白名单。用户侧可配置角色、音色、说话风格等；后台侧可配置命令策略、风险等级、休眠资源策略等。实现时需在 `create_session` 配置快照和 `ctrl.set`/HTTP API 的权限模型中区分用户可改项与后台管理项。

### B.3 命令消息补充

全双工 P0 命令链路采用 `data.cmd -> data.cmd_ack -> data.cmd_result`：

```jsonc
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

```jsonc
{
  "type": "data.cmd_ack",
  "trace_id": "trace-20260731-0001",
  "session_id": "sess-0001",
  "utterance_id": "utt-0001",
  "cmd_id": "cmd-0001",
  "status": "accepted",               // accepted | rejected | duplicate
  "code": "sdk_received",
  "message": "SDK accepted command delivery",
  "received_at_ms": 1789000001120
}
```

```jsonc
{
  "type": "data.cmd_result",
  "trace_id": "trace-20260731-0001",
  "session_id": "sess-0001",
  "utterance_id": "utt-0001",
  "cmd_id": "cmd-0001",
  "status": "succeeded",              // running | succeeded | failed | canceled | timeout
  "code": "done",
  "message": "completed",
  "started_at_ms": 1789000001200,
  "finished_at_ms": 1789000002400,
  "duration_ms": 1200
}
```

异常口径：

| 场景 | 协议处理 |
| --- | --- |
| `sent` 后未收到 `data.cmd_ack` | 进入 `delivery_timeout`，按投递失败处理，不等待执行结果。 |
| 已 ack 但未收到 `data.cmd_result` | 进入 `execution_timeout`，语音回执应表达执行结果未知或请稍后确认。 |
| 断线前未 ack | P0 不重放，命令标记为 `failed/unacked`。 |
| 重复 ack/result | 记录为 duplicate，不重复驱动业务回执。 |
| unknown `cmd_id` | 走 `data.error.code=unknown_cmd_id` 并审计；不能进入 `data.cmd_ack.status`，也不能影响当前轮会话状态。 |
| late result | 记录为 late，不污染当前轮话术和 GUI 状态。 |

### B.4 验收和图表对应

本节对应四张评审图：

| 图 | 文件 | 覆盖重点 |
| --- | --- | --- |
| 端云主时序图 | `xiaoge_r5_2_end_cloud_sequence.svg` | HTTPS 会话、WSS Authorization bearer、机器人侧唤醒、单命令 cmd/ack/result。 |
| 命令投递与执行状态图 | `xiaoge_r5_2_command_delivery_state.svg` | 高危确认、ack_timeout_ms、result_timeout_ms、no replay、unknown_cmd_id。 |
| 唤醒/休眠/退出门控图 | `xiaoge_r5_2_wake_sleep_gate_state.svg` | sleeping 保持 WSS、engine_gate、SleepingHot/Warm。 |
| Gateway 会话与资源状态图 | `xiaoge_r5_2_gateway_resource_state.svg` | 服务器部署、并发、资源分配、容量拒绝。 |
