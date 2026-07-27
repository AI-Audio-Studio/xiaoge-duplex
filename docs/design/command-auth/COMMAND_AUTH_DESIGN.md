# 指令控制 + apikey 鉴权 · 简单版设计

> 需求来源：[../全双工语音引擎后端API需求文档.md](../全双工语音引擎后端API需求文档.md)（`function.call` 指令、§4.2/§8.3 鉴权）
> 现有协议：[../../../clients/PROTOCOL.md](../../../clients/PROTOCOL.md)（`/ws/audio` 权威协议）· 对接指南 [../../guide/CLIENT_INTEGRATION.md](../../guide/CLIENT_INTEGRATION.md)
> 落点代码：网关 `examples/voice_agents/gateway/main.py`、下行广播 `examples/voice_agents/webpanel/bridge.py`、人设/编排 `examples/voice_agents/web_ui_agent.py`

## 0. 本文范围（先明确不做什么）

只在**现有自研 `/ws/audio` 协议**上增量补两件事，不引入需求文档里的完整控制面 REST 与重消息信封：

- ✅ **apikey 鉴权**：云侧签发 apikey 入库（`sys_api_key`），客户端请求头携带，网关内存集合校验（DB ∪ 静态列表）。
- ✅ **指令控制**：LLM 识别意图后，云侧经 `/ws/audio` 文本帧下发指令给端侧执行；端侧可选回执结果。

**明确不做（v1 不涉及）**：能力查询接口、`POST /token` 动态签发、两阶段 create/start 会话模型、TTS 音频流独立通道、token 续期、多区域路由。这些留待需要时再按需求文档扩展。

**沿用现状**：
- 消息用现有**扁平** `{"type": "...", ...}` 结构，不改成 `{event, session_id, message_id, payload}`（保持三端 SDK 一致、RTOS 友好）。
- 二进制帧=PCM，文本帧=JSON 控制；上下行音频参数不变（16k/mono/Int16LE）。
- 一条 `/ws/audio` = 一个会话 = 一个 agent 进程（网关分配）。指令与回执都在这条连接上，无需新增通道。

---

## 1. apikey 鉴权

### 1.1 目标与定位

给**协议客户端（模式 A，直连 `/ws/audio`，无 cookie）补一道应用层准入门**。现状是模式 A 完全无鉴权（见对接指南 §8.2），只能靠网络层管控。本设计让网关在分配 agent 前校验 apikey。

模式 B（浏览器，共享口令 cookie）保持不变，不受影响。

### 1.2 签发与有效集合（DB 为主，静态兜底）

- apikey 由云侧签发并写入 **RuoYi `ry-cloud`.`sys_api_key`** 表（`status='0'` 为启用）。
- 形如 `sk-` 前缀 + 随机串，例：`sk-B8HkskPH...`。
- **有效集合 = DB（`SELECT api_key FROM sys_api_key WHERE status='0'`）∪ 静态 `XG_API_KEYS`**。
  - 网关启动即载一次，之后按 `XG_API_KEY_REFRESH_SEC`（默认 60s）后台线程池刷新，**不在 WS 热路径查库**；DB 抖动/失败保留上一份快照（不误杀已有 key）。
  - WS 建连只做**内存集合 O(1) 判定**，不阻塞 asyncio 事件循环。
- 增删 key = 改 DB `status` 字段，下一个刷新周期内生效，**无需重启网关**；静态 `XG_API_KEYS` 用作 DB 不可用时的兜底/灰度补充。

### 1.3 客户端携带方式

优先级从高到低，网关按序取第一个非空值：

| 方式 | 形式 | 适用 |
| --- | --- | --- |
| 请求头（首选） | `X-API-Key: sk-xxx` | 硬件/嵌入式/APP，能设自定义头 |
| 查询参数（回退） | `GET /ws/audio?apikey=sk-xxx` | 部分嵌入式 WS 库无法设头时 |

> 说明：WebSocket 握手就是一次 HTTP GET，可带请求头与查询串。查询参数会进网关访问日志，安全性略弱，仅作回退。

### 1.4 校验点与流程

落点：网关 `main.py` 的 `ws_audio()` 处理器，**在 `pool.alloc()` 分配 agent 之前**（对模式 A 分支；模式 B 带 cookie 分支沿用原逻辑，不加 apikey 校验）。

```
GET /ws/audio (X-API-Key: sk-xxx)
        │
        ├─ 无 cookie（模式 A）
        │     ├─ 取 apikey（头 > 查询）
        │     ├─ 缺失            → close 4401 {"type":"error","code":1001,...}
        │     ├─ 不在有效集合内  → close 4401 {"type":"error","code":1001,...}
        │     └─ 校验通过        → pool.alloc() → 正常建会话
        │
        └─ 带 cookie（模式 B）→ 沿用现有 cookie/亲和逻辑（不校验 apikey）
```

- 校验为**内存集合成员判定**（`presented in effective_set`，O(1)）；apikey 为高熵随机串，集合哈希查找的时序泄漏可忽略。
- 未配置任何 apikey / DB 不可用时的行为由开关决定（见 §1.6），默认**放行**以兼容现网，不静默拒绝。

### 1.5 失败语义

| 场景 | WS 关闭码 | 关闭前下发文本帧 |
| --- | --- | --- |
| apikey 缺失 / 无效 | `4401`（自定义：unauthorized） | `{"type":"error","code":1001,"message":"auth failed"}` |

- 复用需求文档错误码表：`1001` 鉴权失败（本 v1 只用到 1001；`1002` token 过期、`1003` 无权限暂不涉及，预留）。
- 关闭码 `4401` 是本项目自定义扩展，客户端应对：视为**准入失败，不重试**（换正确 key 再连），区别于 `1013`（池满，退避重连）。

### 1.6 网关配置（新增环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `XG_API_KEY_REQUIRED` | `0` | `1`=强制校验（缺/错 key 拒）；`0`=兼容模式，恒放行仅记日志（命中与否） |
| `XG_API_KEYS` | 空 | 逗号分隔的**静态**补充/兜底 key，如 `sk-aaa,sk-bbb`；与 DB 集合取并集 |
| `XG_API_KEY_DB_HOST` | 空 | MySQL 主机；三者（host/name/user）任一为空即**不查库**，只用静态列表 |
| `XG_API_KEY_DB_PORT` | `3306` | MySQL 端口 |
| `XG_API_KEY_DB_USER` | 空 | MySQL 用户 |
| `XG_API_KEY_DB_PASSWORD` | 空 | MySQL 口令（口令含 `#`/空格会被 `.env` 解析器截断，故改从 `$BASE/.xg_db_password` 整行读取，权限 600，勿提交） |
| `XG_API_KEY_DB_NAME` | 空 | 库名，本项目为 `ry-cloud`（连字符，非下划线） |
| `XG_API_KEY_REFRESH_SEC` | `60` | 后台刷新有效集合的周期（秒，下限 5s） |

> DB 凭据仅放服务器本地：非敏感项（host/port/user/name）在 `.env`，口令在 `.xg_db_password` 文件，均不落仓库（`xg.sh` 只做 passthrough + 非敏感默认 + 口令文件回退）。
> 灰度建议：先 `XG_API_KEY_REQUIRED=0` 配好 DB/静态 key 观察日志，客户端全部带 key 后再切 `1` 强制。

---

## 2. 指令控制（command）

### 2.1 目标

LLM 在对话中识别到"需要端侧执行某个动作"的意图（如调音量、开灯、翻页、拨号），云侧把该动作作为一条**指令**下发给端侧执行，而不是（或不只是）用语音回复。对应需求文档 §5.3.5 `function.call`。

### 2.2 产生路径（云侧内部，说明用；不改代码属本文范围外）

意图识别复用框架已有的 **LLM function calling**：给 `VoiceAgent`（`web_ui_agent.py`）挂 `@function_tool`，每个工具对应一类端侧指令。LLM 决定调用工具 → 工具处理器经 `broadcast_audio_ctrl()`（`bridge.py`）向 `/ws/audio` 下发 `command` 文本帧。

```
用户语音 → STT → LLM
                 └─(识别意图, 调用 tool set_volume)→ 工具处理器
                        └→ broadcast_audio_ctrl({"type":"command", ...}) → /ws/audio → 端侧
```

指令目录（command name 集合）由业务定义、可扩展；网关/协议层不关心具体 name，只透传。

### 2.3 下行：`command`（云 → 端）

文本帧，扁平结构：

```json
{
  "type": "command",
  "call_id": "c-7f3a9",
  "name": "device.set_volume",
  "args": { "level": 60 },
  "require_reply": true,
  "timeout_ms": 5000
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `type` | string | 是 | 固定 `"command"` |
| `call_id` | string | 是 | 本次指令唯一 ID，云侧生成；回执按此关联 |
| `name` | string | 是 | 指令名，业务约定，建议 `域.动作`，如 `device.set_volume`、`nav.open` |
| `args` | object | 否 | 指令参数，结构随 `name` 而定；无参可省或 `{}` |
| `require_reply` | bool | 否 | 是否要求端侧回执 `command_result`，默认 `false` |
| `timeout_ms` | int | 否 | 建议端侧执行超时；`require_reply=true` 时云侧也按此等待回执 |

### 2.4 上行：`command_result`（端 → 云，可选）

**仅当** `require_reply=true` 时端侧需回执。这是当前协议里**唯一的上行文本帧**（现状上行只有二进制音频）——端侧实现需支持在 `/ws/audio` 上行发文本帧。

```json
{
  "type": "command_result",
  "call_id": "c-7f3a9",
  "success": true,
  "result": { "level": 60 },
  "error_code": null,
  "error_message": null
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `type` | string | 是 | 固定 `"command_result"` |
| `call_id` | string | 是 | 对应下行 `command` 的 `call_id` |
| `success` | bool | 是 | 执行是否成功 |
| `result` | object/string | 否 | 执行结果，回喂给 LLM 作为工具返回值 |
| `error_code` | string | 否 | 失败时的端侧错误码（业务定义） |
| `error_message` | string | 否 | 失败时的可读信息 |

### 2.5 回执时序与超时

```
云侧(tool)                         端侧
   │── {type:command, call_id, require_reply:true, timeout_ms:5000} ─▶│
   │                                          执行指令(调音量…)        │
   │◀── {type:command_result, call_id, success:true, result} ─────────│
   │  用 result 作为 tool 返回值继续 LLM 生成回复                       │
```

- **单向下发**（`require_reply=false`）：云侧发完即返回，工具立即以"已下发"作为返回值，LLM 继续；端侧执行不阻塞对话。适合开灯/调音量这类本地动作。
- **等待回执**（`require_reply=true`）：工具 `await` 回执，最长 `timeout_ms`；超时按失败处理（错误码 `5003`），LLM 侧收到"指令执行超时"。
- `call_id` 用于把异步回执匹配回等待中的工具调用；未匹配到的回执（迟到/重复）**丢弃**。

### 2.6 指令相关错误码

复用需求文档 §9 的 Function Call 段：

| 码 | 含义 | 触发方 | 说明 |
| --- | --- | --- | --- |
| `5001` | 指令不支持 | 端侧 | 端侧不认识该 `name`，回执 `success:false, error_code:"5001"` |
| `5002` | 指令参数非法 | 端侧 | `args` 校验不过 |
| `5003` | 指令回执超时 | 云侧 | 等待 `command_result` 超过 `timeout_ms`，云侧本地判定 |

---

## 3. 客户端处理约定（增量）

在现有 `/ws/audio` 五种下行消息（`ready`/`clear`/`busy`/`user_partial`/`message`）之外，新增：

**下行需新增处理**：
| 消息 | 客户端动作 |
| --- | --- |
| `{"type":"command", ...}` | 按 `name`+`args` 执行本地动作；若 `require_reply=true`，执行完回 `command_result` |
| `{"type":"error","code":1001,...}` | 鉴权失败提示，随后连接会被关（4401），不重试 |

**上行需新增能力**：
| 消息 | 何时发 |
| --- | --- |
| `{"type":"command_result", ...}` | 收到 `require_reply=true` 的 `command` 且执行完毕后 |

**建连需新增**：握手带 `X-API-Key`（或 `?apikey=`）。

> 未接入指令能力的老客户端：忽略未知 `type` 即可（现有 SDK 已按此宽松处理），不影响纯语音对讲。

---

## 4. 落地清单（供后续实现，本文不改代码）

| 项 | 落点 | 动作 |
| --- | --- | --- |
| apikey 配置 | `gateway/config.py` | ✅ 加 8 项 `XG_API_KEY*`（required/静态/DB×5/refresh）+ `api_key_db_enabled`/`api_keys_static_set` 属性 |
| apikey 有效集合 | `gateway/apikey.py`（新增）| ✅ `ApiKeyStore`：pymysql 惰性导入 + 线程池查库 + 后台刷新循环 + `authorize()` 灰度语义 |
| apikey 校验 | `gateway/main.py` `ws_audio()` 模式 A 分支 | ✅ alloc 前取头/查询 → `store.authorize()` → 失败 close 4401 |
| 指令下发 | `bridge.py` | 复用 `broadcast_audio_ctrl({"type":"command",...})` |
| 指令回执接收 | `web_audio.py` 上行文本帧处理 | 解析 `command_result` → 按 `call_id` 唤醒等待中的工具 |
| 意图→指令 | `web_ui_agent.py` `VoiceAgent` | 挂 `@function_tool`，处理器下发 command + 可选 await 回执 |
| 协议同步 | `clients/PROTOCOL.md`、三端 SDK、`CLIENT_INTEGRATION.md §8` | 补 `command`/`command_result`/apikey |

---

## 5. 与需求文档的对应关系

| 需求文档 | 本简单版 |
| --- | --- |
| §5.3.5 `function.call`（下行指令） | `{"type":"command"}`，扁平化 |
| §5.2.7 `function.call.result`（回执） | `{"type":"command_result"}`，`require_reply` 时才要 |
| §4.2 `POST /token` / §8.3 token 接入 | 简化为 apikey 请求头 + 网关内存集合（DB `sys_api_key` ∪ 静态）校验，无 REST 签发 |
| §9 错误码 1001 / 5001 / 5002 / 5003 | 沿用 |
| `{event,session_id,message_id,payload}` 信封 | **不采用**，沿用现有扁平 `{"type":...}` |
