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

### 四、测试保真护栏(评审组§四建议 → **已采纳并实测确认**)

原端到端用例的 `_FAKE_AGENT`(`serve_forever` 无信号处理、SIGTERM 即死)只**正向证明**"死后
重起",端口释放太快、对旧"立即同端口 spawn"未必稳定判红,作回归护栏偏弱。**设计者已强化**
(提交 `8ff8620`):`_FAKE_AGENT` 装 SIGTERM handler、收到后 `sleep(1.5)` 再退(仿真 agent
优雅收尾数秒持端口)。**评审组核实**:POSIX 下——旧 bug(立即同端口 spawn)会在旧进程仍持端口
的 1.5s 窗口 bind → EADDRINUSE → 用例稳定**判红**;现修复("kill 确认死后才 spawn",
`default_kill.wait` 于 ~1.5s<5s 等到子进程退出、不触 SIGKILL)→ 用例**绿**——护栏由此具备
判别力。**限制(已诚实标注)**:Windows `terminate` 硬杀不走 handler、fake agent 即死,故该
判别力**仅 POSIX 生效**(部署/CI 目标为 Linux,覆盖到位;本机 Windows 上该用例保持正向通过)。
修复本身与持端口时长无关(spawn 严格在确认死之后),此为护栏强化。**该建议已闭环。**

### 五、最终裁定(全闭环,无遗留项)

- **B-1 / B-2 / B-3 / B-4(含 B-4b):全部修复确认通过**——B-1 转码后 manifest 无悬空(实测)、
  B-2 kill 锁外、B-3 kill 先于入队、B-4 死后同端口重起(真端口用例)、B-4b SIGKILL 兜底
  (真进程用例);spawn-after-death 由代码结构保证。
- **§四测试护栏建议已采纳**(fake agent SIGTERM 后持端口 1.5s,POSIX 下稳定判旧 bug 红)——
  无遗留评审项。
- 其余(状态机/API/转码器/§7.2/D-21·22/P-5/**144 绿**)达标。
- **PR-B 全部评审项闭环,可合入。**

> 评审组签署(PR-B 合并稿·全闭环):B-1~B-4(含 B-4b)全部实测/看码确认修复;真端口·真进程
> 集成测坐实"死后同端口重起"+ SIGKILL 兜底真杀;§四护栏建议已采纳(1.5s 持端口,POSIX 判别力
> 确认、Windows 侧诚实标注为正向通过)。**PR-B 无遗留评审项,可合入。** 评审组只读,未改任何
> 工程代码。

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

## PR-C(网关,v4 §6)· 评审组结论(**阶段性:3/5 模块**,2026-07-07)

> 分支 `feat/concurrency-c-gateway`,已推 origin。当前 **3/5 模块**:`gateway/config.py`(§3.5)、
> `affinity.py`(P-4 cookie + P-3/T2/D-16/R3 状态机)、`pool_client.py`(P-2)。**未到:`proxy.py`
> (HTTP 反代 + WS 双向透传 + 宽限窗上游持有)、`main.py`(装配/TLS/sweep 循环)**。故本节为
> **已交付 3 模块的阶段性评审,非 PR-C 合入裁定**。评审组全程只读、实测取证,未改工程代码。

### 一、已交付 3 模块——质量达标(实测/看码取证)

- **门禁(评审组重跑)**:`test_ours_*` **159 passed**、ruff 全绿、行数门禁 exit 0;4 个 gateway
  文件 + 2 测试文件已入 ourcode.txt(与 diff 一致)。
- **config.py(§3.5)**:`XG_GRACE_SECONDS=12`(D-16)、`XG_ACCESS_CODE`(Q6)、`XG_HMAC_SECRET`
  空→`secrets.token_hex(16)` 随机(R4 重启失效)、`XG_MSG_RATE`/`XG_MAX_FRAME_BYTES`(R6)、
  `pool_api`——与 §3.5 逐项一致;`listen_host` 默认 `0.0.0.0`(对外网关本体,正确;内部进程
  127.0.0.1 由 poolmgr 注入,M3 归属清楚)。
- **affinity.py(P-4/P-3/T2/D-16/R3)**:HMAC-SHA256 cookie、**常数时间比对**(`hmac.compare_digest`)、
  篡改/换密钥拒绝(测试锁定);状态机 IDLE→ACTIVE→PENDING_DISCONNECT(注入时钟宽限窗)→
  REATTACH/CLOSED,双标签页 REJECT_BUSY 不改原状态——**测试均以注入时钟走真边界**(9.9 未过、
  10.0 过期、窗内重连 REATTACH、超时 CLOSED→再连 REJECT_GONE),非空壳。
- **pool_client.py(P-2)**:/alloc·/release·/status 与 poolmgr control_api 对齐;**错误吞成安全
  默认**经**真不可达地址**(`127.0.0.1:1`,非 mock)实测——alloc→None/release→False/status→{}。
- **范围核实**:`qwen_gateway_console_agent.py` 虽在 ourcode.txt,但 `git` 证其为 **main 既有文件**
  (初始提交起),**不在 PR-C diff**——非本 PR 引入,无涉。

### 二、须在 proxy.py/main.py(剩余 2/5)落实或验证的项

- **C-1(契约硬约束,须 proxy.py 遵守)**:`affinity.on_audio_disconnect` **无条件**递减
  `audio_conns`,且无"仅对已接受连接生效"的守卫。若 proxy.py 对一条 **REJECT_BUSY(双标签页
  被拒)** 的连接在其关闭时**也调 on_audio_disconnect**,会把真会话的 `audio_conns` 从 1 降到 0
  → **误转 PENDING_DISCONNECT、错杀正在通话的第一标签页**。**必须**:proxy.py 只对
  `on_audio_connect` 返回 FRESH/REATTACH(已接受)的连接配对调用 on_audio_disconnect;REJECT_*
  连接直接关、不得回调 disconnect。建议加一条专项用例(拒绝的双标签页关闭 → 原会话仍 ACTIVE)。
- **T2/D-16 真行为未覆盖**:affinity 只给**状态决策**(FRESH/REATTACH/…),`Session.upstream`
  字段全程未被读写——"网关持有上游、重连帧续接"的**真实现在 proxy.py**,当前不可验证(阶段性
  正常,但 T2 的实质须待 proxy.py + 真 WS 集成测)。
- **sweep 驱动缺失**:`sweep_expired()` 是被动方法,需 main.py 起周期 sweep 循环(类 poolmgr
  poll)才会真触发宽限窗超时回收;当前无驱动。
- **R1/R3/D1/Q6/R6 集成验收**(§6 映射均属 PR-C/D):真 WS 断开→窗内重连延续/超时新会话、
  双标签页提示页、关闭码 4001/409、准入口令、限流——须待 proxy.py/main.py + harness 真链路。

### 三、阶段性裁定

- 已交付 3 模块(config/affinity/pool_client)**质量达标、测试非空壳、门禁 159 绿**,无阻塞级缺陷。
- **C-1 为剩余 proxy.py 的必守契约**(现无守卫,极易错→错杀会话),须落实 + 专项用例。
- **不构成 PR-C 合入裁定**:待 proxy.py/main.py 交付 + R1/R3/D1/Q6/R6 真链路集成测齐备后整体复核。

> 评审组签署(PR-C 阶段性·3/5):config/affinity/pool_client 达标(HMAC 常数时间、状态机注入
> 时钟真边界、pool_client 真不可达安全默认均实测);C-1(on_audio_disconnect 无条件递减)列为
> proxy.py 必守契约 + 必补测;T2 真上游持有/sweep 驱动/集成验收待剩余 2/5。评审组只读,未改
> 任何工程代码。

### 设计者应答(PR-C 阶段性·3/5 + C-1,2026-07-07)

**阶段性达标收到**(config/affinity/pool_client、159 绿、注入时钟真边界、真不可达安全默认)。
**C-1 抓得极准且我做了比"proxy 纪律 + 补测"更硬的处置——改成结构守卫,让 footgun 从
"靠 proxy 自律"变成"想错都错不了":**

- `on_audio_connect` 现返回 **(结果, session, conn_id)**:FRESH/REATTACH 带唯一 conn_id;
  **REJECT_BUSY/REJECT_GONE 的 conn_id=None**。`Session.audio_conns` 由计数改为 **conn_id 集合**。
- `on_audio_disconnect(session_id, conn_id)`:**仅当 conn_id 在集合内才生效**;被拒连接的
  None(或重复断开的旧 id)天然无操作。故即便 proxy.py 对一条被拒双标签页连接误调
  disconnect,也**不会把正在通话的真会话降到 PENDING**——评审担心的"错杀第一标签页"在
  affinity 层被结构性堵死,不依赖 proxy 自律。
- 补测:`test_c1_rejected_double_tab_close_does_not_kill_session`(被拒连接关闭→真会话仍
  ACTIVE;真连接关闭才转 PENDING;**重复断开幂等**)。9→10 条 affinity 单测,全量 160 绿。

**其余观察接受、归属正确**(proxy.py/main.py 的活):T2"网关持有上游 + 重连帧续接"的真
实现、`Session.upstream` 的真读写、sweep 周期驱动(main.py 起类 poll 循环)、R1/R3/D1/Q6/R6
真链路集成——都待剩余 2/5 模块 + **真 WS 集成测**(承前几轮教训:这类组件必配真 I/O 测)。
proxy.py 将从一开始就用上述安全 API(conn_id 配对),不留 C-1 隐患。

**门禁**:160 单测全绿,lint/format/行数门禁过。继续 proxy.py + main.py。

### 评审组确认(C-1 修复,2026-07-07)

C-1 已修,且**优于评审建议**——我原建议"proxy.py 纪律 + 补测",设计者改为**结构守卫**
(提交 `63a25a5`),看码 + 实测确认:

- **结构**:`Session.audio_conns` 由 `int` 计数改 **`set[str]` conn_id 集合**;`on_audio_connect`
  返 `(结果, session, conn_id)`——FRESH/REATTACH 生成 `uuid4` conn_id 并入集,**REJECT_BUSY/GONE
  的 conn_id=None**;`on_audio_disconnect(session_id, conn_id)` 仅当 `conn_id in audio_conns` 才生效。
  被拒连接拿不到 conn_id → 误调 disconnect(None/陌生 id)**天然无操作**——footgun **结构性堵死**,
  不再依赖调用方纪律(比原建议更硬)。
- **实测**:`test_c1_rejected_double_tab_close_does_not_kill_session`——第一标签页 FRESH(cid1)、
  第二标签页 REJECT_BUSY(cid2=None);`on_audio_disconnect(s1, None)` 后**真会话仍 ACTIVE、
  audio_conns=1**;仅 `on_audio_disconnect(s1, cid1)` 才转 PENDING;重复断开幂等(cid1 已移除→无操作)。
  该用例对旧"无条件递减"代码会判红,具判别力。
- **无回归**:签名 3 元组变更无 in-tree 旧调用方(proxy.py 未写,仅测试用);全量 **160 绿**、
  lint/行数门禁过;其余状态机用例(FRESH/REATTACH/GONE/双标签页)同步更新仍绿。

**C-1 闭环**。3/5 阶段性评审的唯一 actionable 项已消除;其余(T2 真上游持有/reattach 帧续接、
sweep 驱动、R1/R3/D1/Q6/R6 真链路集成测)仍随 `proxy.py`/`main.py`(剩余 2/5)交付时验证——
届时以真 WS 集成测坐实,不以单测时序代替。

> 评审组签署(C-1 确认):结构守卫落实、实测判别力确认、无回归、160 绿;C-1 闭环。已交付
> 3/5 模块无遗留评审项;PR-C 整体裁定待剩余 2/5 + 集成测。评审组只读,未改任何工程代码。

## PR-C(网关,v4 §6)· 整体评审(5/5 完成,2026-07-07)

> 分支 `feat/concurrency-c-gateway` 全 5 模块交付:config/affinity/pool_client(前评已达标)+
> **proxy.py(T2/D-16/R6 上游持有+透传泵+反代)、main.py(六路由/Q6/R6/sweep/D-07)**。
> 评审组全程只读、实测/看码取证。**裁定:整体质量达标;一处中危 B-C-2 建议合入前修(1 行+补测)。**

### 一、核心行为——实测坐实(非采信)

- **门禁(评审组重跑)**:`test_ours_*` **174 passed**、ruff 全绿、行数门禁 exit 0;5 模块均 <500、
  入 ourcode.txt。
- **T2/D-16 真端到端**(此前 3/5 评审"不可验",现坐实):`test_grace_holds_upstream_and_reattach`
  用**真 aiohttp WS + 真网关 app + 真假 agent 上游**——ws1 连→回声→断→**断言 PENDING 且
  `"sess1" in proxy._io`(上游被持有)**→ws2 重连→ACTIVE→回声正常→**`len(agent_conns)==1`
  (agent 全程只被连一次=上游真持有、未重开)**。宽限窗超时关上游亦有真测。
- **六路由规则(§6.1)**:TestClient + 假池实测——规则1 分配/繁忙页、规则2 cookie 失效 4001/409、
  规则3 协议客户端直分配 + **即断即杀(D-07,`test_protocol_client_flow_and_immediate_release`)**、
  规则4 无 cookie /ws 拒绝、规则6 双标签页页/4002。
- **affinity(P-3/P-4/R3/C-1)**:HMAC 常数时间比对、状态机注入时钟真边界、**C-1 结构守卫**
  (conn_id 集合,被拒连接 conn_id=None→disconnect 天然无操作,`test_c1_*` 判别力确认)。
- **D-07 分档**(看码):`on_audio_disconnect` deadline = `now + (T if browser else 0)`——协议
  客户端 0 宽限、下一 sweep tick(≤2s)即 release,与浏览器超时走同一 sweep 路径(不放请求
  finally,规避连接取消打断收尾)。`register(*, browser=True)` 签名兼容 root/协议两调用点。
- **Q6/R6**:准入 HMAC cookie 门(`test_access_gate_*`);/api 白名单默认 404、cookie
  `HttpOnly+SameSite=Strict`(Secure 随 TLS)、错误响应不泄漏拓扑(泛化 error)、每连接令牌桶
  速率 + 单帧上限(proxy `_RateLimiter`/`max_frame_bytes`)。
- **pool_client**:错误吞成安全默认(真不可达实测,前评已确认)。

### 二、B-C-2(中危,建议合入前修;代码确认、测试未覆盖)——上游连接失败泄漏会话

- **事实链(看码)**:`main.ws_audio` 先 `on_audio_connect`(会话转 ACTIVE、conn_id 入集)再
  `proxy.handle_audio`;`handle_audio` 的 **FRESH 分支 `_open_upstream` 失败**(proxy.py:90-95)→
  `client_ws.close(1011)` + `return`,**未调 `on_audio_disconnect`**、未 `close_session_io`。
- **后果**:该会话**永远停在 ACTIVE**(conn_id 幽灵留在 audio_conns)→ `sweep_expired` 只扫
  PENDING → **永不 release**,pool 槽/会话表泄漏;更糟——该 cookie 后续 `GET /` 命中
  `ACTIVE and audio_conns` → **永久"已在另一窗口通话"页,用户被锁死**;`/ws/audio` 重连恒
  REJECT_BUSY。
- **触发**:agent 在 alloc 与浏览器 `/ws/audio` 连接之间死亡/被 poolmgr 回收(数秒窗口,池
  churn 下现实存在)。**非理论**。
- **修法(1 行)**:`handle_audio` 上游失败分支 return 前 `self._table.on_audio_disconnect(sid,
  conn_id)`(→ 会话转 PENDING → sweep release,cookie 解锁)。**补测**:`_open_upstream` 抛错时
  会话被清理、pool release 被调用。**当前 proxy/main 测试无一覆盖 upstream-fail**——同类"真实
  失败路径单测漏覆盖",与 A2/B 教训一致。

### 三、低危(非阻塞,建议随修或 PR-D)

- **N-1(健壮性)**:`_sweep_loop` 循环体无 try/except——某次迭代意外抛错会**静默杀死 sweep
  任务**、宽限窗清理全停(poolmgr `poll_loop` 用了 suppress,此处宜对齐)。当前 `pool.release`/
  `close_session_io` 各自内部 suppress,概率低,但缺兜底。
- **N-2(UX,PR-D 真浏览器验)**:`root()` 双标签页判据 `ACTIVE and audio_conns` 在**快速刷新**
  (旧 `/ws/audio` 关闭尚未被网关处理,新 `GET /` 先到)时可能**误判为双标签页**、给用户
  "另一窗口通话"页。时序相关,建议 PR-D 真浏览器回归验证刷新路径。
- **N-3(覆盖)**:R6 的速率/帧上限、cookie 属性无专项断言(代码在);建议补。

### 四、整体裁定

- 核心(T2 真持有/reattach、六路由、C-1、D-07、Q6/R6)**实测坐实、门禁 174 绿**,架构与实现达标。
- **B-C-2(中)建议合入前修**——核心路径泄漏 + 用户锁死,1 行修 + 1 补测,成本极低;N-1~N-3 低危可随修。
- 修 B-C-2 + 补 upstream-fail 测后,PR-C 达合入标准。**其后 PR-D**:R1 三断言、R3、D1、Q6、R6、
  B4、批量断开等**全链真集成/浸泡**(§6/§12.1),及 A1-F1(#3 console 退出)、目标机 N 摸底。

> 评审组签署(PR-C 整体):5/5 模块达标,T2/六路由/C-1/D-07/Q6/R6 实测坐实(174 绿);中危
> B-C-2(upstream-连接失败→会话泄漏+用户锁死,代码确认、测试未覆盖)建议合入前 1 行修+补测;
> N-1~N-3 低危。评审组只读,未改任何工程代码。

### 设计者应答(PR-C 整体评审,2026-07-07)

**全部采纳。B-C-2 属实、且是真缺陷,已修 + 补先红后绿真失败路径测;N-1、N-3 一并做;N-2 接受、
随理由入 PR-D。门禁:90 并发用例绿(174→+3),lint-ours/format/行数门禁过。**

- **B-C-2(中,已修)**——**看码+实测坐实事实链**:`handle_audio` FRESH/`io is None` 分支
  `_open_upstream` 失败(proxy.py:90-95)只 `close(1011)+return`,而 `on_audio_disconnect` 仅在
  `_pump_cli2up` 的 finally——该失败路径根本不进泵。会话遂永停 ACTIVE(conn_id 幽灵留 audio_conns)
  → `sweep_expired` 只扫 PENDING → 永不 release(槽/表泄漏)+ 该 cookie 的 `GET /` 恒命中
  `ACTIVE and audio_conns` → 永久"另一窗口通话"锁死。触发窗口(alloc 与浏览器 `/ws/audio` 连接
  之间 agent 被 poolmgr 回收/死亡)在池 churn 下现实存在,评审判"非理论"成立。
  **修法(采纳 1 行)**:失败分支 return 前 `self._table.on_audio_disconnect(sid, conn_id)`——把
  滞留连接**并入既有 PENDING→sweep→release 路径**(浏览器 now+T、协议 now),cookie 随之解锁,
  与全局"收尾只走 swep、不放请求 finally"一致(不新增 proxy 侧 pool I/O)。
  **补测(先红后绿,真失败)**:`test_upstream_fail_releases_session_not_leak_or_lock`——分配一个
  **无人监听的真端口**(bind→close 取空闲口)制造真实 `ClientConnectorError`;断言会话被 release、
  移出表、cookie 再 `GET /` 不落双标签页页。**去掉修复行该测判红(实测 `assert 's1' in []`)**,
  具判别力;呼应 A2/B/C-1"真实失败路径必配真 I/O 测,不以假时序代替"。
- **N-1(已修)**:`_sweep_loop` 迭代体裹 try/except(`logger.exception` 后继续),单次异常不再
  静默杀死 sweep 任务——与 poolmgr `poll_loop` 的 suppress 对齐。这条对 B-C-2 修复尤其关键:
  release 现全依赖 sweep 存活。
- **N-3(已补)**:`test_rate_limiter_token_bucket`(注入时钟,验令牌桶初始满=rate、同刻至多 rate 条、
  按时间线性补充)+ `test_oversized_frame_dropped`(真 WS:>`max_frame_bytes` 帧→断连、**不透传
  agent**,小帧先证链路通)+ root 测补断言 Set-Cookie 含 `HttpOnly`+`SameSite=Strict`(R6②)。
- **N-2(接受,入 PR-D)**:`root()` 双标签页判据在**快速刷新**(旧 `/ws/audio` 关闭事件晚于新
  `GET /` 到达)时可能误判。页级判据只是 UX 兜底,**权威双标签页守卫在 `/ws/audio` 的
  REJECT_BUSY**(仍准确);且刷新误判无法脱离真浏览器时序复现/验证。故不在单测层强改(易引入反向
  漏判),**随 PR-D 真浏览器刷新回归验证**(§12.1 已含刷新/重连路径)。

> 应答小结:B-C-2 已修 + 真失败路径测先红后绿坐实;N-1/N-3 落实;N-2 入 PR-D。90 绿、门禁过。
> 待评审组复核 B-C-2 修复与 4 项新测后给 PR-C 合入裁定。

### 评审组确认(PR-C 整体应答:B-C-2 / N-1 / N-3 / N-2,2026-07-07)

四项处置逐条实测/看码复核通过(提交 `edc0419`):

- **B-C-2(已修·实测判别)**:`handle_audio` 上游失败分支 return 前已加
  `self._table.on_audio_disconnect(sid, conn_id)`(proxy.py)——会话转 PENDING → sweep release、
  cookie 解锁。**测试真实且判别**:`test_upstream_fail_releases_session_not_leak_or_lock` 用**真
  无监听端口**造 ClientConnectorError,经 TestClient 全链(GET/ 种 cookie → /ws/audio FRESH →
  上游连不上 → 网关 1011),断言 `pool.released` 含 s1、`table.get("s1") is None`(未永停 ACTIVE)、
  再 GET/ 不落双标签页页(cookie 未锁)；起真 sweep 循环跑通 PENDING→release;去掉修复行即判红。
- **N-1(已修)**:`_sweep_loop` 迭代体裹 `try/except`(log 后继续),单次异常不再静默杀死 sweep;
  与 poolmgr `poll_loop` 对齐。
- **N-3(已补)**:`test_rate_limiter_token_bucket`(注入时钟,桶上限=rate、线性补充,R6③)+
  `test_oversized_frame_dropped`(真 WS,>max_frame_bytes 断连、不透传 agent)+ root 测补
  cookie `HttpOnly/SameSite` 断言(R6②)。
- **N-2(接受入 PR-D)**:双标签页快速刷新页级误判——页级判据仅 UX 兜底,权威守卫在 `/ws/audio`
  的 REJECT_BUSY;PR-D 真浏览器回归验刷新路径。同意。

**门禁(评审组重跑)**:`test_ours_*` **177 passed**、ruff 全绿、行数门禁 exit 0。

**PR-C 全部评审项闭环,达合入标准。** 剩余非 PR-C 事项(PR-D 全链集成/浸泡:R1 三断言/R3/D1/
Q6/R6/B4/批量断开;A1-F1 #3 console 退出;目标机 N 摸底 + B5;Q5 合规)按既定顺序推进。

> 评审组签署(PR-C 整体·闭环):B-C-2 修复实测判别(真 upstream 失败→释放不锁死)、N-1 sweep
> 兜底、N-3 R6 补测、N-2 合理入 PR-D;177 绿。**PR-C 无遗留评审项,可合入。** 评审组只读,
> 未改任何工程代码。

## PR-D(集成回归 + 浸泡 + 部署文档)· 实施记录

**PR-C 已合入 main(PR#30,c3fe77e)。PR-D 分支 `feat/concurrency-d-integration`。**

### 首个增量:全链集成 harness(已交付,待评审)

单组件测(c_main/c_proxy)用 in-process 假 agent 覆盖了路由/宽限窗单点逻辑;PR-D 补的是**跨组件、
真进程**的一环:**真池管理器(spawn 假 agent 子进程)→ 真控制 API → 真网关 → 真 WS/HTTP 客户端**。

- `tests/_fake_agent_server.py`(83 行):池管理器像真 agent 一样 spawn 的假 agent 子进程,暴露
  `/healthz`(池探活 + `pid`/`audio_total`)/`/ws/audio`(身份帧+回声)/`/ws`/`/api/mic`;**零云/模型
  依赖**(不复活已暂停的音频注入课题),生命周期由池管理器 kill 回收。
- `tests/test_ours_concurrency_d_integration.py`(5 用例,真子进程,~10s):
  1. **全链往返**:GET/ 分配→种 cookie→/ws/audio 身份帧+回声→/api/mic 反代(跨网关+池+agent)。
  2. **宽限窗跨真进程(R1)**:断开→`_agent_healthz` 证上游仍被网关持有(`audio_conns==1`、`pid` 不变);
     窗内重连 REATTACH→回声正常且 **`audio_total==1`**(agent 全程只被连一次、同进程);超时→网关
     sweep release→池**同端口回收换新进程**(`pid` 变)。end-to-end 坐实 T2/D-16/D-07 收尾。
  3. **N+1 繁忙**:占满座位后新浏览器 GET/ → 池 503 → 网关繁忙页。
  4. **断开→回收→槽复用**:断开→sweep release→池回收→`status.ready` 复位→可再分配。
  5. **批量断开**:N=3 路同断→全部 release→池同端口全回收复位(`ready==n`)。
- **门禁**:94 并发用例绿(90→+4;另批量+1)、lint-ours/format/行数门禁过。**踩坑**:aiohttp 默认
  cookie jar 拒收 IP 主机(127.0.0.1)cookie,浏览器会话须用 `CookieJar(unsafe=True)` 才携亲和 cookie。

### 待办(本增量之后)

- **浸泡 harness(§7)**:4 路 × 2h 长循环 + 进程树 RSS/句柄/磁盘增速/转码积压采样脚本(长跑,落
  `docs/reports/`);N2 转码并发 ≤2 已在 `b_transcoder` 单测坐实,浸泡再观测在线路 KPI 无扰动。
- **N-2 真浏览器**:双标签页快速刷新误判回归(需真浏览器时序,非子进程可复现)。
- **A1-F1**:#3 优雅退出在 console/ThreadJobExecutor 实际形态的效力核实(已有池侧 kill 兜底,非阻塞)。
- **部署文档 + 部署验收项**:M3 内网 nmap、R4 systemd 拉起、R5 时钟/时间戳、R7 七项告警(§6 表标 PR-D)。
- **上线前置门(不随本 PR)**:目标机 N=8/10 摸底 + B5 复测、Q5 合规——两坎未过不上线/不承诺产能 N。

### 评审组结论(PR-D 集成 harness 增量,2026-07-07)

**已交付增量达标、真跨进程实测坐实;但 PR-D 作为末个编码 PR,§12.2 20 条 checklist 缺
统一 done/pending 台账,三项(B4-full/D1-集成/R1c-跨进程)未明确归属——须补台账,非阻塞增量本身。**

**一、增量实测确认(看码 + 重跑)**:
- 门禁:`test_ours_*` **182 passed**(并发 95)、ruff 全绿、行数门禁 exit 0;harness/用例入 ourcode.txt。
- **harness 真度**:`_stack` 拉起**真 PoolManager(spawn 假 agent 子进程)+ 真 control API + 真网关
  app + 真 sweep 循环**,仅 agent 内部(模型)以子进程 stub——被测组件(池调度/回收、控制 API、
  网关路由、proxy WS 泵 + 宽限窗持有)全真、跨真进程真 WS。
- **case2(T2/D-16/D-07 跨真进程)最硬**:断开后 **agent 自身 /healthz 报 `audio_conns==1`
  (网关真持上游)、`pid` 不变**;窗内重连 **`audio_total==1`**(agent 全程只被连一次);超时→
  sweep release→池同端口回收 **`pid` 变**。这是短于真浏览器的最强验证。其余 4 例(全链往返/
  N+1 繁忙/回收槽复用/批量断开)均真子进程。`CookieJar(unsafe=True)` 踩坑标注诚实。
- 待办(soak/N-2/A1-F1/部署验收 M3·R4·R5·R7/上线前置门)**诚实列出**,无过度声称。

**二、checklist 台账缺口(PD-1,低~中,须补)**:PR-D 是末个编码 PR,§12.2 是实施前评审的
验收契约,但当前无"20 条 → 已覆盖/待办"的统一映射,三项未明确归属、也不在待办:
- **B4(千次 healthz 探测→0 重启→真实会话无扰动)**:健康→不回收已隐式覆盖(`poll_once` 多次
  保持 READY),死亡→回收显式(b_manager);**但"千次 + 真实会话 KPI 无扰动"未测**——宜显式
  归入 soak 待办。
- **D1(进程被杀/回收→旧 cookie 解析失败→4001/409→前端刷新→重分配)**:机制已覆盖(affinity
  `resolve` 对 CLOSED/gone 同样返 None→4001,c_main 单测);**但"真回收后旧 cookie→4001→GET/
  重分配"的集成走查缺**——harness 已在,补一个小集成用例即可(低成本)。
- **R1(c) 协议客户端即断即杀**:c_main 单测覆盖,**跨进程集成未覆盖**。
- 建议:PR-D 收尾前补一张 §12.2 done/pending 台账(每条 → 覆盖它的测试文件/用例 或 明确
  待办归属),使末 PR 不漏项;并补 D1 小集成用例(harness 现成)。

**三、裁定**:集成 harness 增量**真、强、绿,达标可合入**;PR-D 距"完成"仍差 soak/部署文档/
部署验收 + A1-F1 + 上述 checklist 台账(PD-1)。两道上线前置门(目标机 N=8/10 摸底+B5、Q5 合规)
不变、未过不上线。

> 评审组签署(PR-D 增量):真跨进程集成 harness 达标(case2 以 agent 自身 healthz 坐实 T2 上游
> 持有 + 同进程 reattach + 超时换 pid,182 绿);PD-1——末 PR 须补 §12.2 done/pending 台账,
> B4-full 归 soak、D1 补集成用例、R1c 跨进程待补。评审组只读,未改任何工程代码。

### 设计者应答(PD-1,2026-07-07):§12.2 done/pending 台账 + D1/R1c 补测

**采纳。两处集成用例已补(harness 现成);B4-full 归 soak;并按要求给出 §12.2 20 条统一台账。
建台账过程发现一处真缺口:M5(asr/tts 隐藏)未实现——诚实标 ✗ 并已开独立跟进。**

**D1 / R1c 已补(真跨进程)**:
- `test_d1_recycled_cookie_rejected_then_reallocates`:断开→宽限窗超时→sweep release→池同端口
  回收(会话移出表);旧 cookie **/api/mic → 409、/ws/audio → 4001**,再 **GET/ → 200 重分配**
  (规则1 单入口)。end-to-end 坐实 D1 关闭码 + 重分配。
- `test_r1c_protocol_client_immediate_kill_cross_process`:协议客户端(无 cookie)连真 agent→断开,
  在 **grace=15s** 下**~秒级**即回收换 pid(远早于宽限窗)——证协议端 D-07 即断即杀跨真进程。
- 门禁:并发 **97 绿**(+D1+R1c);d_integration 共 7 例。

**§12.2 实施前评审 checklist(20 条)· done/pending 台账**

图例:✅ 已测覆盖 · 🧪 浸泡(soak)待办 · 🚀 部署验收待办 · ⛔ 上线前置门 · ❌ 未实现须补

| # | 条目 | 状态 | 覆盖 / 归属 |
|---|---|---|---|
| 1 | R1 宽限窗三断言 | ✅ | c_affinity(状态机三态)+ c_proxy(持有/reattach)+ **d_integration case2**(跨进程:持有·同进程reattach·超时换pid)+ **R1c**(协议端即断即杀跨进程) |
| 2 | R3 双标签页 | ✅ | c_affinity `test_double_tab` + c_main `test_root_double_tab_page` |
| 3 | D1 关闭码/重分配单入口 | ✅ | c_main(4001/409·规则1单入口)+ **d_integration D1**(真回收后旧cookie→409/4001→重分配) |
| 4 | Q6 准入 | ✅ | c_main `test_access_gate_blocks_without_code` |
| 5 | R6 安全四条 | ✅ | c_main(白名单404·cookie HttpOnly/SameSite)+ c_proxy(令牌桶·超帧断连) |
| 6 | B4 healthz 千次专项 | 🧪 | b_manager(健康→不回收/死亡→回收);**千次探测 + 真实会话 KPI 无扰动 → soak** |
| 7 | M3 内网绑定 | ✅ / 🚀 | b_control_api(serve 强制 loopback)+ default_agent_env `WEB_UI_HOST=127.0.0.1`;**外网 nmap 不可达 → 部署验收** |
| 8 | R2 转码归属 | ✅ | b_transcoder + b_manager(转码在池侧·per-dir 队列·崩溃不阻塞 alloc/在线) |
| 9 | N1 分档校验 | ✅ | b_transcoder(D-21:FLAC 采样数逐一 / Opus 时长差≤0.07s;失败留 WAV 不删源) |
| 10 | N2 限流 + 批量断开 | ✅ / 🧪 | b_transcoder(≤2 worker·os.nice);批量断开释放 → d_integration case5;**在线路 KPI 无扰动 → soak** |
| 11 | R4 故障模型 | 🚀 | HMAC key 随机·重启失效=config 语义(§6.3);**systemd 自拉·崩溃全断语义 → 部署验收** |
| 12 | M4 就绪告警/心跳 | ✅ / 🚀 | b_manager(`ready_below_threshold`·SPAWNING spawn_timeout vs READY fail_limit);**size N+1~2 部署配置·聆听静默不误杀=agent 存量运行时** |
| 13 | K3 解耦两条 | ✅ | a2(record_settings 默认=现状·非重采样轨逐字节) |
| 14 | 三开关默认值=现状 | ✅ | a2(`RecordSettings.from_env` 默认) |
| 15 | 分段+目录id/healthz/断开退出/日志sid | ✅ | a1(#1 session_id·#2 healthz·#3 X-XG-Session shutdown·#4 log 前缀)+ a2(segment_seconds) |
| 16 | **M5 asr/tts 隐藏 404** | ❌ | **未实现**:`webpanel/server.py:293-294` 无条件注册 /api/asr·/api/tts;需 env 门控(默认隐藏→404)+ tab 注入联动。**开独立跟进(PR-D 收尾或小改 PR)** |
| 17 | D-23 WEB_UI_PORT 默认8787 | ✅ | a1(#6 默认 8787) |
| 18 | R5 时钟/UTC/单调性 | 🚀 | 时间戳 UTC=录音/timeline 代码;**NTP 同步·单调性抽查 → 部署验收** |
| 19 | R7 监控七项告警 | 🚀 | **接入 + 告警阈值 → 部署验收** |
| 20 | Q5 合规 | ⛔ | 保留期/访问控制正式结论 → **上线前置门**(临时策略:仅落盘不外发·目录权限最小化) |

**小结(按主状态计,合计=20,不重复)**:11 条纯 ✅ + 3 条 ✅(带 soak/部署尾:#7 M3、#10 N2、
#12 M4)+ 1 条纯 🧪 soak(#6 B4)+ 3 条纯 🚀 部署验收(#11 R4、#18 R5、#19 R7)+ 1 条 ⛔ 前置门
(#20 Q5)+ **1 条 ❌(#16 M5,真缺口,已开跟进)** = 20。编码可测项已全绿;M5 是唯一未实现的功能项,
须在 上线 前补齐(不影响本 harness 增量合入)。soak/部署验收/前置门按既定顺序,两坎(目标机 N、Q5)未过不上线。

### 评审组确认(PD-1 应答 + M5 缺口,2026-07-07)

**PD-1 处置到位、实测确认;台账暴露的 M5 是真缺口(评审组独立看码坐实),处理透明——采纳。**

- **D1 集成用例(真跨进程,判别性)**:`test_d1_recycled_cookie_rejected_then_reallocates`——真栈
  分配→断开→sweep release→**池真回收**;随后旧 cookie `/api/mic`→**409**、`/ws/audio`→CLOSE
  **close_code 4001**、`GET /`→200 重分配。端到端坐实 D1 关闭码 + 规则1 单入口重分配。✓
- **R1c 集成用例(真跨进程,判别性)**:`test_r1c_protocol_client_immediate_kill_cross_process`——
  **grace=15s** 下无 cookie 协议端断开后 **10s 内 pid 变**(远早于 15s 宽限窗),坐实 D-07 协议端
  即断即杀不受宽限窗约束。判别力清晰(若协议端误享宽限,pid 不会 10s 内变)。✓
- **B4-full 归 soak**、**§12.2 20 条台账**:逐条对到测试文件/用例或明确待办归属,图例清晰;
  评审组抽验条 1/3/16 与代码/用例一致。台账小结 dual-status 行(✅/🚀)计数略含重复,属摘要口径,
  20 行状态各自明确、不影响结论。

**M5(❌,真缺口,评审组看码坐实——建台账的价值正在于此)**:
- **事实**:`webpanel/server.py:293-294` **无条件**注册 `/api/asr`·`/api/tts`,无 env 门控、无隐藏
  404;而网关 `main.py:37` 白名单含 asr/tts 且注释"隐藏态由 agent 侧 404(D-19)"——**该 agent
  404 从不发生**,网关照常反代。故 D-19/M5"服务器形态默认隐藏"**端到端完全未强制**。
- **后果(非仅缺测)**:任何浏览器用户可切换自己会话的 ASR/TTS 后端——product 明确不欲暴露的
  管理面,且扩大远端配额消耗面。属**未强制的产品/访问控制决策**(中),非崩溃/丢数据。
- **根因(过程教训)**:M5/D-19 是 §8.4 的独立决策,**未被纳入 A1~C 任一 PR 的实现范围**,静默
  漏实现穿过四个编码 PR;**恰由本轮要求的 §12.2 台账兜出**——印证"末 PR 必须逐条对账"的价值。
- **处理**:设计者透明标 ❌ + 定位 + `spawn_task`(task_7681fc12)独立跟进,未遮掩——认可。
- **裁定**:M5 **不阻塞本集成 harness 增量合入**(正交),但为**硬上线门**——D-19 是产品拍板的
  访问控制,须在上线前以 env 门控(默认隐藏→404 + tab 注入联动)实现;并同步修正网关注释
  (在 agent 真 404 落地前,该注释是失真的)。

**门禁(评审组重跑)**:并发 **97 绿**、ruff 全绿、行数门禁 exit 0。

> 评审组签署(PD-1 + M5):D1/R1c 真跨进程集成用例判别性确认、§12.2 20 条台账完整诚实;M5 经
> 看码坐实为真缺口(server.py:293-294 无条件注册、网关注释失真、D-19 端到端未强制)——不阻塞
> harness 增量,列**硬上线门**、已独立跟进。PR-D 集成增量 + 台账达标可合入;M5/soak/部署验收/
> A1-F1 + 两前置门(目标机 N、Q5)按序推进,未过不上线。评审组只读,未改任何工程代码。

### 设计者应答(PD-1 + M5 确认,2026-07-07)

**收到,两点即办:①网关失真注释已修正;②台账小结改为按主状态计=20(去重)。M5 作硬上线门
接受,已跟进,网关注释同步纠偏。集成 harness 增量达标待合入。**

- **网关注释纠偏(采纳)**:`gateway/main.py` `_API_WHITELIST` 注释原写"asr/tts 隐藏态由 agent
  侧 404(D-19)"——在 M5 落地前**失真**(该 404 从不发生)。已改为如实注明:白名单转发 mic/asr/tts,
  D-19/M5 隐藏 404 **尚未实现**、落地前 asr/tts 照常反代由 agent 服务(指向 §12.2 台账 M5 / 跟进 task)。
- **台账小结去重(采纳)**:改为按主状态计、显式列出 3 条 dual-status(#7/#10/#12)与各纯状态,合计=20。
- **M5 硬上线门(接受)**:D-19 是产品拍板的访问控制,非仅缺测;实现范围界定=agent 侧 env 门控
  (默认隐藏→注册跳过→404)+ 前端 tab 注入联动 + 网关注释在真 404 落地时再回改;已 `spawn_task`
  (task_7681fc12)独立跟进,不塞入本集成增量(避免半测安全改动混入 test PR)。
- **过程教训认领**:M5/D-19 静默漏穿 A1~C 四个编码 PR,恰由本轮 §12.2 台账兜出——坐实"末 PR 必须
  逐条对账"的价值;后续若再有 §8.4 独立决策,实现前须先挂到 PR 范围或台账,避免再漏。
- 门禁不变:并发 97 绿、lint-ours/format/行数门禁过(仅改注释与文档,无逻辑改动)。

### 评审组裁定(先合入 PR-D 集成增量?2026-07-07)——**同意先合入,附边界**

两处即办已看码核实(非采信):网关注释纠偏(`main.py` 白名单注释改为"D-19/M5 隐藏 404
尚未实现、落地前 asr/tts 照常反代",与代码一致)、台账小结去重=20;并发 97 绿、ruff/行数门禁过。

**同意先合入本集成增量,理由(事实支撑):**
1. **纯测试 + 文档增量,零生产逻辑改动**——本 PR 内容 = `tests/_fake_agent_server.py` + 集成用例 +
   §12.2 台账 + 一处网关注释纠偏;无任何 agent/网关/池的行为改动(门禁确认)。合入不可能回归
   生产行为,只是把跨进程集成测带上 main 持续跑。
2. **M5 与本增量正交,且非本 PR 引入**——asr/tts 无条件注册是 A1~C 已在 main 的既有遗漏
   (PR-C 已合),合入这个 test PR **不使 M5 变差**;把"半成品安全改动"塞进 test PR 反而更糟。
   设计者拆到独立 task(7681fc12)跟进是正确卫生。
3. **台账即活契约**——M5 ❌、soak 🧪、部署 🚀、前置门 ⛔ 全部显式在册,随代码上 main;合入不丢项。

**边界(合入 ≠ 上线,须钉死):**
- 本裁定仅为**评审组"可合入"意见**;实际合入 + 上线授权属项目负责人(第二门)。
- **上线仍被硬门锁死**,须全部达成方可:①**M5 实现**(D-19 产品拍板的访问控制,env 门控默认
  隐藏→404 + tab 注入 + 网关注释回改)②soak 浸泡 ③部署验收(M3 nmap/R4 systemd/R5 NTP/R7 告警)
  ④A1-F1(#3 console 退出,池侧 kill 已兜底)⑤**两前置门**:目标机 N=8/10 摸底+B5、Q5 合规。
- **M5 建议为合入后第一优先**:它是台账唯一 ❌ 功能项、且是产品拍板的访问控制,虽当前无真实
  用户(网关未部署)不构成即时风险,但不应久悬;实现时按 D-19 范围(默认隐藏)+ 补隐藏态 404
  的真集成测(harness 现成)。

> 评审组签署(先合入裁定):PR-D 集成增量为纯测试/文档、零生产逻辑,达标且正交于 M5——
> **评审组同意先合入**(合入/上线授权归负责人);上线五门(M5/soak/部署验收/A1-F1/两前置门)
> 未过不上线,M5 建议合入后第一优先。评审组只读,未改任何工程代码。
