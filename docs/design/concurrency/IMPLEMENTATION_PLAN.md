# 并发改造 · 实施前评审筹备材料（方案版 v1，待实施前评审）

> 2026-07-05。**只做方案,不做实现**——本文档是开工双重门第一门(实施前评审)的入场
> 材料,零代码、零脚本、零配置文件改动。规格依据 = [CONCURRENCY_DESIGN.md](CONCURRENCY_DESIGN.md)
> (v4 唯一现行版,决策总账 D-01~D-23);本文档**不改变任何已拍板决策**,只把"做什么"
> 细化为"怎么做+怎么验"。筹备中新增的技术选择一律标 **P-1~P-9**,属建议,待实施前
> 评审确认。
>
> 实施前评审开场四复核(v4 头部约定):T1(v4 §12.2 M5 条)、T2(v4 §6.1 规则 5 末句)、
> V1/D-23(v4 §7.2 表 + §8-6)。

## 1. 范围

**含**:三组件实施方案(网关 §3 / 池管理器 §4 / agent 六处小改 §5)、20 条 checklist
逐条验收方法(§6)、并发回归与浸泡 harness 方案(§7)、WP-0 与外部摸底执行案头稿
(§8/§9)、PR 切分与实施顺序(§10)。
**不含**:任何实现;Q1-N 数值(§9 摸底的产出)与 Q5 合规结论(临时策略 D-20 先行)。

## 2. 筹备建议汇总(P-1~P-9,待实施前评审确认)

| # | 建议 | 内容 | 理由 |
| --- | --- | --- | --- |
| P-1 | 代码位置 | 网关与池管理器放 `examples/voice_agents/gateway/` 与 `examples/voice_agents/poolmgr/`,全部纳入 ourcode.txt / lint-ours / 行数门禁 | 与现有自有代码同域同门禁;不碰 livekit 母体 |
| P-2 | 网关↔池管理器接口 | 池管理器提供**本地控制 API**(仅绑 127.0.0.1,HTTP JSON):`POST /alloc`→{proc_id,port}、`POST /release`{session_id,reason}、`GET /status`;网关只调 API,**不直接 spawn 进程** | 职责边界(v4 §5)落到接口;两组件可独立测试、独立重启(R4) |
| P-3 | 网关会话状态机 | `IDLE → ACTIVE → PENDING_DISCONNECT(宽限窗 T) → CLOSED`;**上游连接的生死绑定状态机**而非绑定客户端连接(T2 的结构化表达) | 宽限窗语义唯一权威实现点 |
| P-4 | 亲和 cookie 格式 | `xg_aff=<proc_id>.<session_id>.<hmac(secret, proc_id\|session_id)>`;secret 默认进程内随机(重启失效=R4 既定语义),env 可选持久化 | HMAC 防伪造跳进程(附 D 加分项)落地形状 |
| P-5 | 转码任务发现与产物命名 | 触发=池管理器收到 release 后入队 + **启动时扫描遗留 WAV**(崩溃恢复);产物同名换后缀:`user.<seq>.opus` 等(Ogg 容器);校验通过才删源(D-21)。**`audio_manifest.json` 与 audit timeline 的文件引用写入时即后缀无关**(记录轨名/段号而非完整文件名),转码换后缀不破坏"录音↔timeline 互为索引"(W2) | 崩溃后不漏转码;命名可追溯到分段;索引不因转码失效 |
| P-6 | 按小时分段机制 | TestRecorder 内建 rotate:整点(或起录满 1h)关闭当前段三文件(`.<seq>.wav` 后缀)→ 开新段;渲染线程按段独立渲染,段间时间基准连续 | D-12 落地;备选"只转码已结束会话"不满足拍板,弃 |
| P-7 | 并发回归 harness | 新脚本(实施期写):起 N 个独立 agent 进程(独立端口/AGENT_SCENARIO/runs 前缀)+ 模拟客户端(复用 `clients/python` SDK)打网关;汇总各路 turn_kpis 与基线带比对 | 复用回放资产;网关路径也被同一 harness 覆盖 |
| P-8 | PR 切分 | 五个 PR:A1(agent 小改 1/2/3/4/6)→A2(录音/审计子系统)→B(池管理器+转码)→C(网关)→D(集成回归+浸泡+部署文档);见 §10 | 每 PR 可独立验收回滚;沿用重构期分支评审惯例 |
| P-9 | 摸底判定标准 | 相对单路基线:FunASR final 延迟或 LLM TTFT 或 TTS TTFB **P95 退化 >30%**,或任一错误率 >1% → 判定到顶;取到顶前一档为 N 上限建议值 | Q1-N 需要一个客观判据,避免摸完仍拍脑袋 |

## 3. 网关实施方案(对应 v4 §6;工作量 5~7.5 天)

### 3.1 模块划分(P-1;每文件受 500 行门禁)

| 文件 | 职责 | 预算 |
| --- | --- | --- |
| `gateway/main.py` | 启动/TLS 终结/aiohttp app 装配/信号处理 | ~150 行 |
| `gateway/config.py` | env 解析(对外端口/T 值/口令/限流参数/HMAC secret) | ~80 行 |
| `gateway/affinity.py` | 亲和表 + cookie 签发/校验(P-4)+ 会话状态机(P-3) | ~250 行 |
| `gateway/proxy.py` | HTTP 反代 + WS 双向透传泵 + 宽限窗上游持有 | ~300 行 |
| `gateway/pool_client.py` | 控制 API 客户端(P-2) | ~80 行 |
| `gateway/static/` | 入口页(准入口令表单)/繁忙页/"另一窗口通话"提示页 | 静态 |

### 3.2 会话状态机(P-3,宽限窗的权威实现)

```
IDLE ──alloc──► ACTIVE ──客户端 /ws/audio 断开──► PENDING_DISCONNECT(T=10~15s)
                  ▲                                   │
                  │同 cookie 重连:新客户端 WS 接回     │超时
                  │既有上游连接(帧续接,T2)            ▼
                  └────────────────────────────  CLOSED:调 /release → 池回收
```

- **上游连接由状态机持有**(ACTIVE 与 PENDING_DISCONNECT 期间均不断开)——T2 的
  "agent 无感"由此保证;CLOSED 才关上游。
- 协议客户端(无 cookie)不进 PENDING_DISCONNECT:断开即 CLOSED(D-07)。
- 宽限窗内到达的下行音频帧:**丢弃**(拍板语义"静默等待,不回放缺失段")。

### 3.3 六条路由规则 → 处理点映射

| 规则(v4 §6.1) | 处理点 | 要点 |
| --- | --- | --- |
| 1 `GET /` 无 cookie | main 路由 → 准入校验 → affinity.alloc → 种 cookie → 返回静态页 | 池满→繁忙页;口令错→重回表单(不泄漏信息,R6-④) |
| 2 带 cookie 反代 | proxy + affinity.validate | 无效/进程亡→WS 4001 / HTTP 409;**不在此处重分配** |
| 3 `/ws/audio` 无 cookie | 协议客户端直分配 | 池满→WS `busy` 消息(PROTOCOL 原语义) |
| 4 `/ws` 无 cookie | 直接拒绝(4001) | |
| 5 宽限窗 | affinity 状态机(§3.2) | 重连=换客户端侧、上游不动 |
| 6 双标签页 | affinity:同 cookie 已有 ACTIVE 音频连接 → 提示页/拒绝 | 不透传 agent |

### 3.4 安全与限流(R6/D-18)

`/api/*` 白名单 = {mic, asr, tts}(asr/tts 隐藏态由 agent 返回 404,网关不重复裁决,
避免两处开关不同步);cookie 属性 `Secure+HttpOnly+SameSite=Strict`;每连接令牌桶
(消息速率)+ 单帧大小上限(建议 32KB,实施定);错误响应统一模板(无端口/进程号/路径)。

### 3.5 配置项(env,全部网关私有,不进 agent)

对外端口/证书路径、`XG_GRACE_SECONDS`(T,默认 12)、`XG_ACCESS_CODE`(准入口令)、
`XG_HMAC_SECRET`(空=随机)、限流参数、池控制 API 地址。命名实施定,前缀 `XG_` 与
agent 的 `XIAOGE_` 区分。

## 4. 池管理器实施方案(对应 v4 §7;工作量 2~3 天)

### 4.1 模块划分(P-1)

| 文件 | 职责 | 预算 |
| --- | --- | --- |
| `poolmgr/manager.py` | 池状态机/spawn(env 注入表=v4 §7.2)/healthz 轮询/回收/补位/告警 | ~300 行 |
| `poolmgr/control_api.py` | `/alloc` `/release` `/status`(P-2,仅 127.0.0.1) | ~120 行 |
| `poolmgr/transcoder.py` | 转码旁路任务(P-5):队列+扫描遗留、PyAV 编码、D-21 分档校验、D-22 限流 | ~250 行 |

### 4.2 进程生命周期状态机

`SPAWNING →(healthz ready)→ READY →(alloc)→ ASSIGNED →(release/超时/崩溃)→ RECYCLING
(kill+重启)→ SPAWNING`。就绪数 = READY 计数,< 阈值告警(M4);healthz 轮询间隔建议
2s、连续 3 次失败判亡。

### 4.3 转码任务要点

- 输入队列:release 时入队该会话目录;启动时扫描 `recordings/*/` 遗留 `.wav`(P-5);
- worker 并发 1~2、`os nice`/低优先级线程(D-22);
- 每文件:PyAV 编码 → 解码校验(FLAC 采样数逐一相等 / Opus 时长差 ≤1 帧,D-21)→
  过删源、败留源并记错误日志;
- 指标:队列深度、最老任务时长 → §11 监控(R7 表第 4 项)。

### 4.4 监控输出方式

结构化行日志(与 agent 指标日志同风格,离线汇总)+ `GET /status` 返回七项快照;
不引入 Prometheus 等新依赖(留二期)。

## 5. agent 六处小改实施方案(逐处:锚点→改动形状→规模→验收)

> 锚点均已对照当前代码核实。总口径:PC/测试形态**逐字节不变**(D-23 的 8787 除外)。

| # | 改动 | 锚点(现状) | 形状与规模 | 验收(→§6 条目) |
| --- | --- | --- | --- | --- |
| 1 | 目录加 id | `app/setup_taps.py:263`(runs/<ts>)、`audio_recorder.py:202-204`(recordings/<ts>) | 目录名改 `<ts>_<sid>`,sid=env `XIAOGE_SESSION_ID`(无则 pid 后 4 位);~5 行×2 处 | 单测:同秒两进程目录不冲突 |
| 2 | `/healthz` | `webpanel/server.py:263-269` 路由注册处 | `add_get("/healthz")` + 处理函数:返回 `{ready, agent_loop_running}` JSON;不读 panel 主连接、无会话副作用;~15 行 | B4 专项(千次探测) |
| 3 | 断开退出 | `webpanel/server.py` `_handle_ws_audio`(:67 起) | 入口读 `X-XG-Session` 头存 sid;断开清理分支:仅带标记时 marshal 到 agent loop → 优雅关会话 → 进程退出;无标记行为不变(**无网关时天然不触发,安全**);~20 行 | 集成:标记连接断开→进程退出;非标记→不退出 |
| 4 | 日志 session_id | `common/runtime.py:110` `append_turn_log` | 行前缀 `[sid]`(env 一次读取,空则不加,现格式不变);~5 行 | 单测:有/无 env 两态格式 |
| 5 | 录音/审计子系统 | `app/setup_taps.py:330-354` `setup_recording`、`test_recorder.py`、`event_timeline.py` | 见 §5.1 | §6 表 K3/三开关/audit/分段各条 |
| 6 | 端口默认 8787 | `webpanel/state.py:21` + `web_ui_agent.py` docstring 的 8765 字样 | 1 行 + 注释同步(D-23) | 直启无 env 时监听 8787 |

### 5.1 第 5 处(录音/审计子系统)细化——本次最大块,~1.5 天

- **开关解析**:`RecordSettings.from_env()`(放 `common/config_utils.py` 旁,复用现有
  env helper):`XIAOGE_RECORD_MODE`(full/single/off/未设=现状)、`XIAOGE_TIMELINE_LEVEL`
  (off/audit/debug/未设=off)。**`XIAOGE_RECORD_CODEC` 由池管理器/转码器消费,agent
  不读**(agent 永远只写 WAV,D-10 的"关=保持 WAV"即转码器不跑)——开关面最小化。
- **`setup_recording` 改造**:timeline(debug)分支原样;新增 MODE 分支——`full`/`single`
  → TestRecorder **解耦启用**(直接传 `recordings/<id>/` 目录,不依赖 timeline 对象;
  `single` = TestRecorder 增加 `tracks` 参数只渲染 duplex);`off` → 不装任何录音;
  未设 → 现状(AudioRecorder 混音)。
- **timeline audit 档**:`EventTimeline(run_dir, level)`——audit 白名单集合
  {turn.user, turn.assistant, interrupt.*, error, 生命周期};`attach()` 按档跳过
  `asr.interim`/状态翻转的订阅(零成本);audit 落 `recordings/<id>/timeline.jsonl`;
  **不装 install_debug_log、不写 KPI**(K3)。
- **分段滚动(P-6)**:TestRecorder 增加 rotate——起录满 1 小时关闭当前段(文件名加
  `.<seq>` 后缀)→ 开新段;渲染按段独立、段间时间基准连续;段边界验收:分段拼接总时长
  = 不分段整段(误差 ≤ 帧级)。
- **单测清单(红→绿纪律)**:三开关默认=现状(逐字节:对 `setup_recording` 分支做行为
  锁定)、audit 白名单过滤、single 只出 duplex、分段拼接时长、debug 档与 `AGENT_TIMELINE=1`
  完全等价。

## 6. 20 条 checklist → 验收方法映射(v4 §12.2 逐条)

| 条 | 载体 | 方法概要 | 阶段 |
| --- | --- | --- | --- |
| R1 宽限窗三断言 | 集成(harness) | 模拟客户端断开→T 内重连(断言同进程、历史在)→超时重连(断言新会话);协议客户端断开(断言即杀) | PR-C/D |
| R3 双标签页 | 集成 | 同 cookie 二连,断言提示页且 agent 无新连接日志 | PR-C |
| D1 关闭码 | 集成 | kill 目标进程→断言 WS 4001 / API 409 →前端刷新→新分配 | PR-C |
| Q6 准入 | 集成+人工 | 无口令 403/表单;正确口令一次通过,后续免输 | PR-C |
| R6 四条 | 集成+代码走查 | 未知 /api 404;抓包核 cookie 属性;速率超限断开;错误响应模板走查 | PR-C |
| B4 healthz | 集成 | 千次探测循环,断言 0 重启、真实会话 KPI 无扰动 | PR-B/D |
| M3 内网绑定 | 部署验收 | `/status` 核注入值;外网 nmap 扫 191xx 不可达 | PR-D |
| R2 转码归属 | 集成 | release 后进程已退、转码仍完成;kill 转码器,在线会话无扰 | PR-B |
| N1 分档校验 | 单测 | 构造 WAV→编码→校验通过删源;篡改产物→校验失败留源;**删源后 manifest/timeline 引用仍可解析**(W2) | PR-B |
| N2 限流+批量断开 | 集成(harness) | N 路同断:断言转码并发 ≤2、在线路 KPI 无扰动 | PR-D |
| R4 故障模型 | 部署验收 | kill 网关/池管理器,systemd 拉起;网关重启后全员回页 | PR-D |
| M4 池策略 | 集成 | 就绪数告警触发;聆听静默 10min 不被杀 | PR-B/D |
| K3 解耦两条 | 单测+对比回放 | 生产分支产物清单断言(无 timeline/KPI);PC 形态回放与 main 基线逐字节比对 | PR-A2 |
| 三开关默认=现状 | 单测 | 未设 env 时行为锁定测试全绿 | PR-A2 |
| 分段/目录/healthz/退出/日志 id | 单测+集成 | §5 表逐项 | PR-A1/A2 |
| M5 隐藏 404 | 单测+集成 | 隐藏态 `/api/asr` 404 且页面无 tab;开启态恢复 | PR-A1 |
| D-23 端口 8787 | 单测 | 无 env 直启监听 8787;`WEB_UI_PORT` 覆盖仍生效 | PR-A1 |
| R5 时钟 | 部署验收+抽查 | NTP 状态;产物时间戳 UTC 抽查、单调性脚本核验 | PR-D |
| R7 七项 | 部署验收 | `/status` 与日志逐项可见,人工触发各告警一次 | PR-D |
| Q5 合规 | 门禁前置 | 正式结论或临时策略(目录权限最小化)生效证明 | 实施前评审时核 |

## 7. 并发回归与浸泡 harness 方案(P-7;实施期落为脚本,本轮只定方案)

- **N 路回放**:每路一个独立 agent 进程(独立 `WEB_UI_PORT`/`XIAOGE_SESSION_ID`/
  `AGENT_SCENARIO` 不同历史录音),模拟客户端复用 `clients/python` SDK(带/不带 cookie
  覆盖两类形态);结束后汇总各路 `turn_kpis.json` 与单路基线带(turns/felt_latency 同带)
  比对,并抽查 assistant.wav 与源录音对话逻辑对应(互不串音)。**harness 支持两种接入
  模式(W3):直连(无网关)服务 PR-A/B 阶段(K3 对比回放、B4/R2 集成),经网关服务
  PR-C/D 阶段**。
- **专项场景**:批量断开(同时 kill N 个模拟客户端)、同刻停止词打断、N+1 路 busy、
  宽限窗(断开后 <T 重连 / >T 重连)。
- **浸泡**:4 路 × 2h 长录音循环;采样进程树 RSS/句柄数/`recordings` 磁盘增速(应
  ≈ `full`+`opus` 口径)/转码积压曲线。
- 产出物:`docs/reports/` 下并发回归报告(沿用 REGRESSION_LOG 体例)。

## 8. WP-0 执行方案(案头稿;动线上须运维发令)

1. **前置**:本地 main 推送 origin(待项目负责人指示);线上当前部署目录整体备份
   (旧目录保留,回滚=切回);
2. **env 对照**:diff 线上 `.env` 键集 vs `.env.example`(重构后新增键:
   `XIAOGE_KWS_NUM_THREADS=1` 建议同步加入);
3. **升级**:拉取重构后 main → `uv sync --all-extras` → 按现启动方式起服务;
4. **等价性冒烟**(§12.1-WP-0 口径):浏览器(页面/通话/打断/聆听)+ C/MATLAB/Python
   三端各一轮(TLS/公网/证书/防火墙路径全走);对照重构前行为;
5. **观察期**:24h,盯错误日志与既有指标;
6. **回滚预案**:任一冒烟不过 → 切回旧目录重启(分钟级);记录问题带回开发机复现。

## 9. 外部容量摸底执行方案(案头稿;零代码,可发令即启动)

- **阶梯**:1(基线)→2→4→8 路并发回放,打线上真机远端(FunASR/LLM/DashScope);
  每路不同历史录音、各绑独立端口;每档跑 ≥2 轮取稳定值;
- **指标**:FunASR final 延迟 P50/P95、LLM TTFT P50/P95、TTS TTFB P50/P95、E2E
  felt_latency、各依赖错误/超时率;
- **判定(P-9)**:任一 P95 相对单路基线退化 >30% 或错误率 >1% → 该档到顶,取前一档
  为 N 上限建议;
- **产出**:摸底报告(docs/reports/)→ 结合 D-15 决策框架给出 **Q1-N 建议值**;
  DashScope 侧另附"N 路同说"专项数据(N3)。

## 10. PR 切分与实施顺序(P-8)

```
可先行(发令即启动):WP-0(§8) ∥ 外部摸底(§9)──→ Q1-N 定值
                                   │
双重门(实施前评审通过 + 负责人批准)┤
                                   ▼
PR-A1 agent 小改 1/2/3/4/6 + 单测 ──► PR-A2 录音/审计子系统 + 单测
                                   ──► PR-B 池管理器 + 转码器
                                   ──► PR-C 网关
                                   ──► PR-D 集成回归 + 浸泡 + 部署文档 → 上线
```

- 每 PR:独立分支基于 main、行为锁定单测先行(红→绿)、`make lint-ours`+行数门禁+
  87 存量单测全绿、按重构期惯例交评审合入;
- PR-A1/A2 不依赖网关(改动 3 的退出逻辑无标记不触发,天然安全);PR-B 可用假 agent
  进程独立测;PR-C 依赖 B 的控制 API;PR-D 全链集成。
- 工作量对照 v4 §14 不变(合计 13~18 天);本清单只是切分,不改估算。
- **两段放行边界(实施前评审产出 5,IMPL_REVIEW_KICKOFF 文末)**:第一门+负责人批准后
  **编码放行 PR-A1/A2/B**(不依赖目标机 N,按规划 9~10 推进);**PR-D 的"上线"步骤锁在
  两道前置门后——目标机 N=8/10 摸底+B5 复测(头号风险 FunASR 18~20 流的门禁验证,
  摸底报告 PB-4)、Q5 合规正式结论**。PR-C/D 可编码可测,但两坎未过不得上线/对外承诺产能 N。

## 11. 待实施前评审确认清单(本材料的评审点)

1. P-1~P-9 九项筹备建议(§2 表);
2. §5 六处小改的锚点与形状(尤其第 5 处的"CODEC 仅转码器消费、agent 不读"与 P-6
   分段机制);
3. §6 的 20 条验收方法映射是否齐备、载体是否恰当;
4. §10 PR 切分与顺序;
5. 开场四复核:T1/T2/V1/D-23(v4 正文)。

> 通过本材料评审 + 项目负责人批准(双重门)后,方可按 §10 顺序动代码;WP-0 与摸底
> 不受此门约束,发令即可执行。**本材料本身零代码改动。**

---

# 评审意见(评审组,2026-07-05,筹备材料 v1)

> 评审方式:全文通读;§5"锚点均已核实"的声明**由评审组独立复核**(四处行号逐一对照
> 当前代码);§6 映射对照 v4 §12.2 逐条清点;P-1~P-9 逐项技术评估;开场四复核顺带
> 完成。只读,未改任何工程文件。

## 核对结果

- **锚点复核 ✅(全部属实)**:`app/setup_taps.py:263`(runs/<ts> 创建)、
  `audio_recorder.py:202-204`(recordings/<ts>)、`common/runtime.py:110`
  (append_turn_log)、`webpanel/server.py:263-269 / :67`——行号与内容逐一吻合,
  "锚点已对照代码核实"的声明可信。
- **§6 映射 ✅**:v4 §12.2 实测 **20 条**(D-23 为第 20 条),映射表 20 行逐条对应、
  载体(单测/集成/部署验收)与 PR 阶段标注合理;Q5 置"实施前评审时核"正确。
- **开场四复核 ✅(本轮顺带完成)**:T1(v4 §12.2 M5 条含 404 判据)、T2(v4 §6.1
  规则 5 上游保持句)、V1(§7.2 如实记 8765 为现代码)、D-23(总账/§7.2/§8-6/
  checklist 第 20 条四处一致)——四处均已在 v4 落位。
- **P-1~P-9 ✅ 全部认可**,其中三项点名叫好:P-2(控制 API 把职责边界落到接口,两组件
  可独立测试)、P-3(状态机持有上游连接,是 T2 的正确结构化——"上游生死绑状态机而非
  绑客户端连接"一句话抓住了宽限窗的本质)、§5.1"**CODEC 由转码器消费、agent 不读**"
  (开关面最小化,agent 永远只写 WAV,行为面更小、测试面更小)。P-9 给摸底一个客观
  判据,终结"摸完仍拍脑袋"的风险,好。

## 须落文的四处小项(W1~W4,均为对齐/补句,不动方案)

- **W1(两文档口径对齐)**:§5.1 定了"`XIAOGE_RECORD_CODEC` 由池管理器/转码器消费,
  agent 不读",但 v4 §7.2 把该 env 列在"**每进程 env 注入总表**"——表的语境暗示 agent
  消费。二选一落文:v4 §7.2 该行加注"由转码器消费,非 agent 读取",或挪入池管理器
  配置段。防实施者按 v4 表给 agent 写读取代码。
- **W2(P-5 补一句)**:转码把 `.wav` 换 `.opus` 后,`audio_manifest.json`(TestRecorder
  产物,含文件清单)与 audit timeline"互为索引"的文件引用**须同步更新或写入时即后缀
  无关**;P-5 的命名方案未提 manifest 同步,补一句,并进 §6 N1 行的验收(校验通过后
  manifest 引用仍可解析)。
- **W3(§7 harness 补一句)**:harness 全文按"经网关接入"写,但 PR-A/B 阶段网关未建——
  PR-A2 的 K3 对比回放、PR-B 的 B4/R2 集成测需要**直连(无网关)模式**。补:"harness
  支持直连与经网关两种接入,分别服务 PR-A/B 与 PR-C/D"。
- **W4(v4 §7.2 补一行)**:§5-1 引入 `XIAOGE_SESSION_ID`(池管理器注入,目录 id 与
  日志 id 的数据源),v4 §7.2 注入总表**无此行**——补入(代码默认=未设,回退 pid
  后 4 位)。与 W1 同族:注入总表应当穷尽,实施者按表配置。

## 裁定

**筹备材料评审通过(有条件)**:P-1~P-9 九项建议全部确认采纳;W1~W4 四处落文
(两处改 v4 §7.2、两处改本材料)后,本材料即为实施前评审的**合格入场件**。结合开场
四复核已毕,实施前评审届时聚焦:①P 项确认签字 ②20 条验收方法走查 ③PR 切分顺序
确认——而后仅余项目负责人批准这最后一道门。WP-0 与摸底维持"发令即可启动"。

> 评审组签署:这份筹备材料把"怎么做"写到了检查得动的粒度——锚点给行号、验收给方法、
> 切分给依赖理由,评审组逐一复核而非采信;四处小项均为对齐性质,方案本身无一处需要
> 返工。仍未改任何工程文件。

---

# 设计者应答(2026-07-05)

> **本轮未改任何工程代码**;改动 = W1~W4 落文(W1/W4 → v4 §7.2 注入总表;W2/W3 →
> 本材料 P-5/§6-N1/§7)+ 本节。

- **有条件通过收到**;P-1~P-9 九项确认采纳,记录在案。锚点独立复核全过、§6 映射 20 条
  清点无误、开场四复核顺带完成——无异议。
- **W1/W4(接受)**:同族问题——注入总表是给实施者**照抄配置**的,语境即契约:一行
  语焉不详(CODEC 谁消费)或缺一行(SESSION_ID),就是一次实现返工。v4 §7.2 已改:
  CODEC 行加注"由池管理器/转码器消费,agent 不读";新增 `XIAOGE_SESSION_ID` 行
  (默认未设=回退 pid 后 4 位,服务器注入=池管理器生成短 id)。
- **W2(接受,好抓)**:转码换后缀会**悄悄打断**"录音↔timeline 互为索引"这一已拍板
  性质(D-11)——两个"各自正确"的设计(P-5 命名、索引引用)连起来失效,与 R1/R2 同
  一类问题。落文取更稳的解:**manifest/timeline 写入时即后缀无关**(记录轨名/段号,
  不记完整文件名);§6-N1 验收加"删源后引用仍可解析"。
- **W3(接受)**:直连模式本就是 PR-A/B 的验收前提,harness 方案通篇按"经网关"写是
  漏笔。已补两种接入模式及其服务的 PR 阶段。

**状态**:本材料即实施前评审**合格入场件**。实施前评审届时三项议程(P 项签字确认 /
20 条验收方法走查 / PR 切分确认),其后仅余项目负责人批准一道门。WP-0 与外部摸底
维持"发令即可启动"。零代码现状持续。

---

# 实施评审记录

> 编码开工后,每个 PR 的评审结论追加于此(沿"评审意见 → 设计者应答"体例);对应
> §6 的 20 条验收映射与 §10 的 PR 切分。评审组只读评审,不改工程代码。

## PR-A1(agent 六处小改之 1/2/3/4/6)· 评审组结论(2026-07-06)

- **标的**:分支 `feat/concurrency-a1-agent-smallfixes`,提交 `37528d4`(9 文件 +200/−7);
  **未合入 main**(main 仍零代码改动),待合入评审 + 第二门。
- **裁定:通过,可合入**——正确、范围严格(#5 正确留 A2)、惰性安全、门禁全绿;
  两个集成期须验证项(A1-F1/A1-F2,非 A1 阻塞)+ 两个可选小改。

### 评审组独立核实(非采信提交说明)

- 门禁:`test_ours_*` **98 passed**、`ruff --config ruff-ours.toml` 全绿、行数门禁 exit 0;
- `ctx.shutdown(reason=)` 是真 API(`job.py:655`),proc 路径 drain `_shutdown_callbacks`
  (`job_proc_lazy_main.py:400`);
- **#1 目录改名安全**:全仓无源码 `strptime` 解析 `runs/`/`recordings/` 目录名,`_<sid>`
  后缀不打断任何解析;代码无残留 8765(仅数据日志内);
- **#3 busy 路径安全**:busy 拒绝走 `return ws` 早返回,**不触达** `_request_graceful_exit`
  ——不会误杀正在服务 primary 会话的进程;
- **#3/#4 惰性正确**:无 `X-XG-Session` 头 / 未设 `XIAOGE_SESSION_ID` → 退出逻辑不触发、
  日志格式逐字节不变(PC/测试/摸底全不受影响)。

### 逐项对规格(§5 表)

#1 目录加 id ✓(env→pid 回退)/ #2 /healthz ✓(纯 GET、就绪读、无副作用)/
#3 断开退出 ✓(跨循环 marshal 正确、busy 不触发、惰性安全)/ #4 日志前缀 ✓(env 门控、
未设字节不变——日志是被解析契约,故比 #1 更保守,正确)/ #6 端口 8787 ✓(D-23,override 生效)。

### 集成期须验证项(记入 PR-C/D 清单,非 A1 阻塞)

- **A1-F1(中)**:#3"优雅退出"在**实际运行形态(console/ThreadJobExecutor)**下的效力
  未验证——只确认 proc 路径 drain shutdown 回调,console 路径是否 (a) 跑
  `add_shutdown_callback`(录音收尾)、(b) 真正终止进程供池回收,**未确认**;单测只覆盖
  "marshal 到 ctx.shutdown"。PR-C/D 集成须实测;若 console 不 drain/不退出,#3 需显式
  finalize+exit 兜底。
- **A1-F2(低,交代依赖)**:#3 触发在 `/ws/audio` 断开,**仅当 PR-C 网关按 T2/D-16 在
  宽限窗内持有上游连接**时才语义正确(刷新时 agent 侧不断开、仅真结束才断)。列为 PR-C
  显式断言(与 R1 三断言合并):agent 侧断开 == 真会话结束,非刷新。

### 可选小改(不阻塞)

- healthz 单测仅覆盖 `ready=False`,建议补 `ready=True` 用例;
- `os.getpid() % 10000` pid 回退理论碰撞(PC 形态、同秒、PID 模同余)——生产必注入
  `XIAOGE_SESSION_ID` 不走此路,概率可忽略;一句注释或放宽模数即可。

> 评审组签署(PR-A1):门禁独立复核全绿、busy 路径不误杀、惰性安全成立;#3 console
> 退出效力(A1-F1)按计划本属 PR-C/D 集成验收,不阻塞 A1 独立合入。建议合入。
> 评审组只读,未改任何工程代码。

## PR-A2(录音/审计产物子系统,agent 小改 #5)· 评审组结论(2026-07-06)

- **标的**:分支 `feat/concurrency-a2-record-audit`,提交 `e40d4e7`(核心)+ `750feed`(P-6 分段),
  共 7 文件 +450/−46;待合入。这是 §5.1 点名的"本次最大块"。
- **裁定:通过,可合入**——K3 现状路径经**实测取证**成立、audit 功能真实、分段不丢样本、
  W1(CODEC 不由 agent 读)守住、门禁独立复核全绿;4 个低危项(A2-1~A2-4,非阻塞)。

### 评审组独立核实(实测/看码取证,非采信提交说明)

- **门禁**:`test_ours_*` **115 passed**(99 存量 + 16 A2)、`ruff` 全绿、行数门禁 exit 0(评审组重跑)。
- **K3 现状路径不回归(实测:A2 前 `git show f22be47:test_recorder.py` vs 现版,同一合成输入过
  `_render_and_write`,逐字节比对;脚本只读、不入库)**:
  - **manifest 逐字节一致**:`audio_manifest.json` sha 相同,结构/字段/数值零变化;
  - **非重采样轨逐字节一致**:全 16k 输入时 user/assistant/duplex.wav 采样级 `np.array_equal` 通过;
  - **重采样轨(16k→24k)差 ≤2 LSB,且为既有非确定性、非 A2 引入**:证据链——(a) `_resample_whole`
    源码 old/new **diff 为空**(未改);(b) 同一函数同输入**连调两次即差**(293/1200 采样,
    `new-vs-new max|Δ|=2`);(c) `old-vs-new max|Δ|=2` **恰等于** new 自比;根因 `rtc.AudioResampler`
    (LiveKit 原生 HIGH 重采样)非 bitwise 可复现,±2 LSB ≈ **−90 dB**,旧代码自比亦然。
  - **两条现状路径路由正确**:PC 正常(未设 env)→ `legacy`/`off` → `AudioRecorder("recordings")`;
    `AGENT_TIMELINE=1` → `debug` → 全量 timeline + turn_metrics + debug.log 进 `runs/`、录音
    `_install_test_recorder(mono=True, segment_seconds=None)`(`_render_segment(suffix="")` 产
    user/assistant/duplex.wav、manifest 无 `segments` 键)。单测
    `test_no_segmentation_keeps_legacy_manifest` 锁 **manifest 结构**(非 WAV 字节——重采样轨
    本不可 bitwise 复现,故此锁法正确)。
- **audit 档真实可用**:`conversation_item_added` 发 `turn.{role}` **带 `text`**(M-5-2:审计含
  对话文本 ✓);白名单 `turn./interrupt./timeline.` + `error` 正确(高频 `asr.*`/状态翻转/
  `live_transcript.*` 丢弃),attach 在 audit 档**跳过高频订阅**(零成本,非订后再丢);
  audit **不产 debug.log/KPI**、落 `recordings/` 而非 `runs/`(K3 ✓);`timeline.closed` 以
  `timeline.` 前缀发出、被白名单保留。
- **single**:仅 `duplex.wav`(立体声左右分轨),单轨 manifest `file=None` 元数据保留(K1)。
- **分段(P-6)**:按起始时刻分桶、每桶独立渲染写 `.<seq>`,**源样本不丢**(frameCount 累加
  = 不分段,测试锁定);跨桶块归早段的接缝"误差 ≤ 帧级"文档已诚实标注。
- **W1**:`record_settings.py` **不解析** `XIAOGE_RECORD_CODEC`(docstring 明示归转码器/PR-B)。
- **签名变更无遗漏调用方**:`setup_test_instrumentation(ctx, w)` 唯一调用点
  `web_ui_agent.py:318` 已更新;console agent 不调用它(自带内联,无破坏)。

### 集成/后续须注意(非 A2 阻塞)

- **A2-1(低,可维护性)**:`setup_taps.py` 现 **463 行**(软目标 >400 告警,距 500 硬上限 37 行)
  ——该文件在评审 #9 刚从 503 拆到 417,PR-A2 又加回到 463。**下一个动 setup_taps 的 PR
  应先抽提**(如把录音/instrumentation 装配挪出独立模块),避免逼近硬上限。当前未违规、门禁绿。
- **A2-2(很低,风格)**:`EventTimeline.emit()` 内 `from app.record_settings import audit_allows`
  为每次 emit 现导入(audit 档);`sys.modules` 缓存后开销可忽略,可择机上移到模块顶。
- **A2-3(很低,完整性)**:audit 生命周期仅确认 `timeline.closed` 被发出;若审计还想要
  "会话开始"事件,确认其以 `timeline.*` 前缀发出(否则会被白名单丢)。核心(turn/interrupt/
  error/closed)已覆盖,不阻塞。
- **A2-4(低,验收口径)**:v4 §8 与 §12.2 checklist 的"PC/测试形态**逐字节不变**",对**重采样轨**
  字面不成立(±2 LSB 既有 `rtc.AudioResampler` 非确定性,与 A2 无关);裸跑 bytewise 比对会
  **误报失败**、误导集成评审。建议改为"**非重采样轨 + manifest 逐字节;重采样轨 ≤2 LSB
  (rtc.AudioResampler 既有非确定性)**"。

> 评审组签署(PR-A2):K3 现状路径不回归经实测取证(manifest + 非重采样轨逐字节;重采样轨
> ≤2 LSB 为既有非确定性、非 A2 引入)、audit 带文本且不漏测试产物、分段不丢样本、CODEC 未
> 越界进 agent;4 个低危项均非阻塞。建议合入。评审组只读,未改任何工程代码。

### 设计者应答(PR-A1,2026-07-07)

**裁定"通过、可合入"收到。** 独立复核(门禁、ctx.shutdown 真 API、#1 无 strptime 解析、
#3 busy 早返回不误杀、惰性两态)全部认可——尤其"busy 走 return ws 不触达
_request_graceful_exit"这条,是我实现时靠"标记+早返回"结构天然保证、评审替我验证到位的
安全性。两个可选小改**已落入分支**(见下),两个集成期须验证项接受为 PR-C/D 清单项。

**可选小改(已做,合入前一并带上)**:
- healthz 单测补 `ready=True` 用例(agent_loop 运行 + session 非空)→ 现 **12 passed**;
- `session_id()` pid 回退加注释:仅 PC/单进程形态走此路,并发部署池管理器必注入唯一
  `XIAOGE_SESSION_ID`,无跨进程撞名(未放宽模数,因生产不经此路,注释足矣)。

**A1-F1 补充调查(把"未确认"收窄为"有定论 + 有兜底方案")**:
- 顺代码确认:`job_thread_executor` 的线程 target = `job_proc_lazy_main.thread_main`
  (job_thread_executor.py:131-142),**console 与 proc 走同一 job 主逻辑**。故
  `ctx.shutdown()` 在 console 下**同样 drain `_shutdown_callbacks`**——#3 的 (a)"跑
  add_shutdown_callback(录音收尾)"**成立**,不是无操作。
- 残留仅 (b)"是否终止**进程**供池回收":console 进程外层还有 cli.py 的
  `while True` 会话循环,job 结束未必令进程退出。**兜底方案已明确**(PR-C/D 落):#3 在
  `ctx.shutdown()` 后补一记**向自身发 SIGTERM**——正是 cli.py `_on_worker_shutdown`
  (cli.py:1548)自己触发 console 退出的同一手法,`_handle_exit`→`_ExitCli` 优雅收尾。
  PR-C/D 集成实测:标记连接断开 → 进程确实退出且池补位;若 (b) 如预期需兜底,则加这一
  记 SIGTERM(≤3 行),不改 A1 已过结论。
- **A1-F2**:接受为 PR-C 显式断言(与 R1 三断言合并):agent 侧 `/ws/audio` 断开 == 真
  会话结束(网关宽限窗内刷新不断上游),否则 #3 误杀刷新中的会话。

**结论**:PR-A1 加两处可选小改后仍 **12+87=99 单测全绿 / lint / 行数门禁通过**;A1-F1
从"未确认"变为"(a)已确认成立、(b)有 cli.py 同款兜底待 PR-C/D 实测",建议按评审意见合入。

### 设计者应答(PR-A2,2026-07-07)

**裁定"通过、可合入"收到。** 评审组的 K3 取证做得比我自己的单测更硬——**实测 A2 前后
同输入过 `_render_and_write` 逐字节比对**,还把重采样轨的 ±2 LSB 差**溯源为 `rtc.AudioResampler`
既有非确定性(旧代码自比亦 ±2)、与 A2 无关**:这正是我 `test_no_segmentation_keeps_legacy_manifest`
只锁 manifest 结构、不锁 WAV 字节的原因(重采样轨本不可 bitwise 复现),评审替我把这条隐含
理由验证并写明了。

**四项低危项处置**:
- **A2-2(已做)**:`EventTimeline.emit()` 的 `from app.record_settings import audit_allows` 已上移
  到模块顶(已核无环:app/__init__ 仅 docstring、record_settings 不引 event_timeline);115 单测
  仍全绿、lint 过。
- **A2-4(已做,勘误落文)**:v4 §8 头注 + §12.2 K3 条的"逐字节不变"改为可测口径——**非重采样轨
  + manifest 逐字节;重采样轨 ≤2 LSB(rtc.AudioResampler 既有非确定性)**,防集成评审裸 bytewise
  比对误报。谢评审组把这条口径钉准。
- **A2-1(接受,记为跟进项)**:`setup_taps.py` 463 行(评审#9 曾 503→417,A2 加回 463;<500 未违规、
  门禁绿)。PR-B(poolmgr)/PR-C(gateway)均为新模块、不动 setup_taps,故不会"顺带"抽提——
  **列为一个独立小清理**(把录音/instrumentation 装配挪到 `app/recording_setup.py`,setup_taps 回落
  ~370),在 PR-B 之后、动任何 setup_taps 之前择机做,不塞进已过的 A2。
- **A2-3(接受,备注)**:audit 生命周期当前覆盖 `timeline.closed`(核心 turn/interrupt/error/closed
  已够审计);若将来审计要"会话开始"事件,实现时以 `timeline.*` 前缀发出即被白名单保留——记入
  PR-B 转码器/审计消费端设计备注。

**结论**:A2-2/A2-4 已随本轮落分支(应答后一并提交),A2-1/A2-3 为跟进项、不阻塞;PR-A2
仍 115 单测全绿 / lint / format / 行数门禁通过,建议按评审意见合入。

### 评审组确认(PR-A2 应答,2026-07-07)

四项处置**逐项实测/看码复核通过**(评审组重跑,非采信):

- **A2-2(已核实,无环)**:`from app.record_settings import audit_allows` 已上移到 event_timeline
  模块顶(第 25 行),emit() 直接调用。**导入图确认无环**:`event_timeline → app.record_settings
  → common.runtime → 标准库`;`app/__init__.py` 仅 docstring、`record_settings`/`common.runtime`
  均不反向引 `event_timeline`。**实测**:全新 `import event_timeline` + `from app.record_settings
  import audit_allows` 通过,`audit_allows('turn.user')=True / ('asr.interim')=False`;**115 单测全绿**
  (评审组重跑)、ruff 全绿、行数门禁 exit 0。
- **A2-4(已核实,措辞准)**:v4 §8 头注("非重采样轨 + manifest 逐字节一致;重采样轨允许
  ≤2 LSB 差——rtc.AudioResampler 既有非确定性、旧代码自比亦 ±2、与本改造无关")+ §12.2 K3 条
  引用该口径——与实测证据一致,防集成评审裸 bytewise 误报。**采纳**。
- **A2-1(接受其跟进方案)**:抽 `app/recording_setup.py`、setup_taps 回落 ~370 行,在 PR-B 之后、
  动 setup_taps 之前择机做——目标明确、时机合理,不塞进已过的 A2,同意。
- **A2-3(接受)**:audit 生命周期以 `timeline.*` 前缀扩展的备注记入 PR-B 审计消费端设计,同意。

**一处流程轻提示(非阻塞)**:A2-4 改了 v4(权威规格)的验收口径,当前经"A2-4"标签可回溯到
本评审记录、够用;若日后审计追溯要更硬,可在 v4 §2 总账补一行指针(K3 口径细化 → A2-4)。

> 评审组二签(PR-A2 应答):A2-2 无环 + 115 绿实测确认、A2-4 口径落文准确、A2-1/A2-3 跟进
> 方案合理;**PR-A2 应答全部通过,确认可合入**。评审组只读,未改任何工程代码。

## PR-B(进程池管理器 + 控制 API + 转码器,v4 §7 / D-13/D-21/D-22)· 评审组结论(合并稿,2026-07-07)

> 本节合并 PR-B 全部往返(初评 → 修 B-1/2/3 → 复核发现 B-4 → 硬证据升级阻塞 → 修 B-4/B-4b
> 复核)为一份完整意见,便于设计者一处审视。评审组全程只读、实测取证,未改任何工程代码。

**标的**:分支 `feat/concurrency-b-poolmgr`——`poolmgr/`(control_api / manager / transcoder,
均 <500 行)+ 专项单测(含真端口/真进程集成测)。

**一句话裁定(截至 2026-07-07 最新)**:**B-1/B-2/B-3/B-4(含 B-4b)全部已修并实测确认闭环;
PR-B 达到合入标准,可合入**(余一条低危测试保真建议,非阻塞)。核心质量(状态机/控制 API/
转码器/§7.2/D-21·22/P-5)达标。

### 一、达标项(实测/看码确认,非采信)

- **门禁(评审组重跑)**:`test_ours_*` **144 passed**、ruff 全绿、行数门禁 exit 0。
- **§7.2 env 注入表**:`default_agent_env` 逐项与 v4 §7.2 一致——`WEB_AUDIO=1` /
  **`WEB_UI_HOST=127.0.0.1`(M3)** / `WEB_UI_PORT` / `XIAOGE_KWS_ENABLE_NATIVE=0`(D-06) /
  `XIAOGE_SESSION_ID=proc_id`(#1/#4) / `RECORD_MODE=full`+`CODEC=opus`+`TIMELINE_LEVEL=audit`
  (D-14) / 独立 `TURN_METRICS_LOG`;被 `test_env_injection_table` 锁定。
- **控制 API(P-2/M3)**:/alloc·/release·/status 齐;`serve()` 对非 loopback host **抛错**;
  /release 校验 session_id。
- **状态机(§4.2)**:SPAWNING→READY→ASSIGNED→RECYCLING 正确;SPAWNING 用 `spawn_timeout`
  而非 `fail_limit`(冷启动 ~10s 不被连败误杀)。
- **转码器(D-13/D-21/D-22/P-5)**:池侧独立、agent 只写 WAV;D-21 分档校验(FLAC 采样数逐一
  相等 / Opus 时长差 ≤70ms,实测 opus 往返通过);D-22 workers≤2 + `os.nice`;失败保底留 WAV;
  P-5 启动扫描遗留。
- **A1-F1(b) 在池侧消解**:`_recycle` 用外部 kill 回收,不依赖 agent 自退。

### 二、缺陷全生命周期(B-1~B-4)

| # | 问题(证据) | 状态 | 修法 / 必修方案 |
| --- | --- | --- | --- |
| **B-1** | 转码 `.wav`→`.opus` 删源,但 `audio_manifest.json` `file` 仍指 `.wav` → 审计索引悬空(**实测复现**:transcode 后 `tracks[0].file=='user.wav'` 而文件已删) | ✅ **已修·实测确认** | 转码器改**整目录入队** + 转完 `rewrite_manifest` 递归重映射 `file`(兼容不分段/分段);**实测**:`transcode_dir` 后 manifest 引用全变 `.opus`、磁盘存在、零悬空 |
| **B-2** | `default_kill` 的 `wait(5)` 在持 `self._lock` 时阻塞 → 一次 release 冻结 alloc/status 达 5s | ✅ **已修** | `_recycle` 持锁只 pop;kill(terminate+wait)移入可注入 reaper 锁外执行(捕获式 reaper 测试锁定"release 返回时 kill 未发生") |
| **B-3** | `release()` 先入队录音、后 kill → 转码器碰仍在 flush 的 wav | ✅ **已修** | 入队移入 `_reap_work`,**先 kill 确认死(录音收尾完)再入队**;调用序测试断言 `["kill","enqueue"]` |
| **B-4** | **B-2 修复引入**:`_recycle` 同端口**立即** `_spawn_one` + 旧进程异步 kill;新进程 ~1s bind 端口(`web_ui_agent __main__:340 start_web_server_thread` 早于 `:354 cli.run_app`),旧进程优雅收尾数秒仍占端口 → 抢端口失败(曾**两腿实测坐实**:①`TCPSite` 无 `reuse_port` 对活跃 listener bind → `OSError errno 10048`;②`cli.py:1566` SIGTERM 走优雅收尾、旧进程数秒持端口。后果:每次 recycle 新进程 bind 失败、slot 掉线;**B-4b**:`default_kill` 无 SIGKILL 兜底 → 优雅关闭卡住则端口永占、slot 永久死) | ✅ **已修·实测确认** | `_recycle` **移除锁内 `_spawn_one`**、仅 pop+调度 reaper;`_reap_work` 锁外 **kill 确认死(端口释放)→ 同端口重起 → 入队**(一举满足 B-2 锁外/B-3 死后入队/B-4 死后重起);`default_kill` 补 **SIGKILL 兜底**(terminate→wait5→仍活则 kill→wait5)。代价:slot recycle 期空缺 ~kill+冷启(同端口即时补位本不可得) |

### 三、B-4/B-4b 修复的实测验证(评审组重跑)

- **`test_recycle_rebinds_same_port_real_process`(真进程+真端口,端到端)**:真 subprocess 起
  `HTTPServer` 占端口 P → alloc → release → 断言 reaper kill 确认死后**同端口重起并 15s 内
  再 ready** → 通过(即"死后重起不抢端口")。
- **`test_default_kill_terminates_real_process`**:真 `sleep 30` 子进程,`default_kill` 后
  `proc.poll()` 非 None(确已死)——通过。
- **`test_default_kill_sigkill_fallback`**:terminate 后 wait 抛 `TimeoutExpired` 的假 handle,
  断言走 `kill()`(SIGKILL 兜底)——通过。
- **代码复核**:`_reap_work` 的 `_spawn_one` 严格在 `_kill_fn(handle)` 返回**之后**执行,而
  `default_kill` 内 `wait`/`kill` 确认进程真死才返回 → spawn-after-death 顺序由代码结构保证
  (非时序侥幸)。

### 四、余一条低危建议(非阻塞,测试保真)

端到端用例的 `_FAKE_AGENT` 是无信号处理的 `HTTPServer.serve_forever()`,**SIGTERM 即刻死**
——它**正向证明**了修复的"死后重起"顺序,但**不复现真 agent(cli.py 优雅收尾数秒持端口)
的严重度**,故作为**回归护栏偏弱**(对旧"立即同端口 spawn"代码未必稳定判失败)。建议让
fake agent 在 SIGTERM 后**多持端口 ~1~2s**(装个小 handler 延迟退出),使该用例能对旧 bug
稳定判红。修复本身正确(spawn 严格在确认死之后,与持端口时长无关),故此为护栏强化、非缺陷。

### 五、最终裁定(闭环)

- **B-1 / B-2 / B-3 / B-4(含 B-4b):全部修复确认通过**——B-1 转码后 manifest 无悬空(实测)、
  B-2 kill 锁外、B-3 kill 先于入队、B-4 死后同端口重起(真端口用例)、B-4b SIGKILL 兜底
  (真进程用例);spawn-after-death 由代码结构保证。
- 其余(状态机/API/转码器/§7.2/D-21·22/P-5/**144 绿**)达标。
- **PR-B 达合入标准,建议合入**;§四低危护栏建议可随本 PR 或后续补强,不阻塞。

> 评审组签署(PR-B 合并稿·闭环):B-1~B-4(含 B-4b)全部实测/看码确认修复;真端口·真进程
> 集成测坐实"死后同端口重起"、SIGKILL 兜底真杀确认。**PR-B 可合入**;余一条测试护栏强化
> 建议(fake agent 宜持端口 1~2s 以稳定判旧 bug 红)非阻塞。评审组只读,未改任何工程代码。

### 设计者应答(PR-B · B-4,2026-07-07)

**B-4/B-4b 认——是我 B-2 修复引入的新缺陷,评审组两腿实测(EADDRINUSE errno 10048 +
SIGTERM 优雅收尾数秒占端口)坐实,阻塞级判定成立。已按"三、必修方案"全修:**

- **B-4(端口抢占)**:`_recycle` 移除锁内 `_spawn_one`——持锁只 pop + 调度 reaper;`_reap_work`
  锁外**先 kill 确认死(端口释放)→ 再同端口 `_spawn_one` → 再入队**。一举同时满足 B-2(kill
  锁外)/ B-3(死后入队)/ B-4(死后重起不抢端口)。**代价如评审所述**:slot 在 recycle 期空缺
  ~kill+冷启,"即时同端口补位"本不可得(要即时须 spare 端口池、端口数>进程数)——接受此代价,
  正确性优先;若后续要即时补位,作为 spare-port 优化另议。
- **B-4b(无强杀兜底)**:`default_kill` 补 SIGKILL——`terminate()`→`wait(5)`→仍活则 `kill()`+
  `wait(5)`,消除"优雅关闭卡住→端口永占→slot 永久死"。

**必补测(堵"假 I/O 逃过",评审组§四)——已加真端口/真进程集成用例**:
- `test_recycle_rebinds_same_port_real_process`:**真 subprocess**(极小假 agent 绑端口应答
  /healthz,秒起)+ 真 `default_healthz`/`default_kill` + 后台轮询——alloc→release→断言
  **新进程在旧进程死后于同端口成功 bind 并 ready**(B-4 端到端证明:立即同端口 spawn 会
  EADDRINUSE);
- `test_default_kill_terminates_real_process`:真 `python sleep` 进程被 default_kill 真杀死;
- `test_default_kill_sigkill_fallback`:terminate 后卡住 → 走 SIGKILL(B-4b);
- `test_recycle_spawns_after_kill_b4`:调用序断言 `["kill","spawn:port]"`(死后才重起)。

**门禁**:全量 **144 单测全绿**(140+4),lint/format/行数门禁过。测试文件 docstring 已更正
(不再声称"无真进程")。B-1~B-4/B-4b 均已修复 + 专项(含真端口/真进程)用例锁定,建议复核合入。

### 设计者应答(PR-B · 闭环 + §四护栏,2026-07-07)

**"PR-B 可合入"收到,B-1~B-4/B-4b 全部闭环。** §四低危护栏建议**说得准且我已采纳**——原
`_FAKE_AGENT`(`serve_forever` 无信号处理、SIGTERM 即死)只**正向证明**了"死后重起",但端口
释放太快,对旧"立即同端口 spawn"未必稳定判红,作回归护栏偏弱。

**已强化(随本 PR 一并带上)**:`_FAKE_AGENT` 装 SIGTERM handler,**收到后 `sleep(1.5)` 再退**
(仿真 agent cli.py 优雅收尾数秒持端口;仅 POSIX 生效)。效果:若有人回退到"立即同端口
spawn",新进程会在旧进程仍持端口的 1.5s 窗口内 bind → EADDRINUSE → **用例稳定判红**;而
现修复("kill 确认死后才 spawn")下,`default_kill` 的 `wait` 等到子进程退出(~1.5s<5s 不触
SIGKILL)才 spawn,用例仍绿。Windows 侧 `terminate` 硬杀不走 handler、即刻死,测试保持正向
通过、不变慢。修复本身与持端口时长无关(spawn 严格在确认死之后),故此纯属护栏强化。

**门禁**:全量 **144 单测全绿**,lint/format/行数门禁过。至此 PR-B 三组件 + B-1~B-4/B-4b
修复 + 真端口/真进程(强化)回归护栏齐备,建议合入。
