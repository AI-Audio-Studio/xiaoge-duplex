# 服务器并发部署 · 运维交互与验证方案(HTTPS-only 约束)

> 本文件 = PR-D "部署文档" 交付物。它把并发架构(网关 + 池管理器 + agent 池)的**上线部署与
> 验证**,落到一个**关键现实约束**下:**目标机只能经 HTTPS 访问已部署的服务功能,不能 SSH 登陆
> 操作**。规格以 [CONCURRENCY_DESIGN.md](CONCURRENCY_DESIGN.md) 为准(§/D 编号引用它)。
> 状态:**方案稿(零工程代码),待评审 + 与运维对齐**。

## 0. 约束与推论(本文件存在的原因)

- **约束**:开发侧对目标机**无 shell**——不能在机上跑命令 / 读文件 / 看 `/proc` / 从机上 nmap /
  在机上跑 soak·probe harness / 手改配置。唯一入口是服务经 HTTPS 暴露的功能面。
- **推论 1(职责必须切分)**:一切**机上操作**(部署、systemd、TLS 证书、env 注入、进程 kill 测试、
  磁盘/文件核查)只能由**运维(ops)**执行;开发侧只能经 HTTPS **驱动负载 + 读服务暴露的信号**。
- **推论 2(必须把服务端信号经 HTTPS 暴露)**:无 SSH → 服务端健康 / 资源 / 泄漏 / 时钟 / 告警若不
  经 HTTPS 暴露,开发侧对服务端**全盲**,只能靠 ops 口头回报,慢且易错。故**必须建一个受控的
  `/status` 管理面**(§2),否则"快速部署 + 验证"无从谈起。
- **推论 3(harness 必须能打远端)**:目标机 N 摸底 / 浸泡的**客户端侧**可经 HTTPS 由开发侧驱动,但
  harness 现只会在本机 spawn 假 agent——需加**远端模式**(§3)。

## 1. 职责切分(谁做 · 经什么)

| 活动 | 归属 | 通道 |
| --- | --- | --- |
| 部署/升级/回滚(拉包、`uv sync`、起停服务) | **ops** | 机上 |
| systemd 单元(网关/池管理器)、崩溃自拉(R4) | **ops** | 机上 |
| TLS 证书签发/续期、放到 `XG_SSL_CERT/KEY` | **ops** | 机上 |
| env 注入(§7.2 表,网关 `XG_*` + 池 `XIAOGE_*`) | **ops** | 机上 |
| 防火墙:仅放行对外 HTTPS 口、内部 191xx/19000/网关内口不可达(M3) | **ops** | 机上 |
| 进程 kill 测试(R4 崩溃语义)、磁盘/权限核查(Q5) | **ops** | 机上 |
| 功能冒烟(页面/通话/打断/聆听/双标签页/繁忙) | **dev** | HTTPS |
| M3 外网端口扫描(内部口不可达) | dev 或 ops | **从外部**网络(非机上) |
| 目标机 N 摸底(客户端 KPI:felt_latency/错误率) | **dev** | HTTPS(远端 harness §3) |
| 服务端资源/泄漏/池态/时钟/告警观测 | **dev 读** | HTTPS(`/status` §2) |
| 真 4 路×2h 浸泡:客户端载荷 = dev;磁盘增速/转码积压 = 经 `/status` 读 | dev + `/status` | HTTPS |

**要点**:凡"机上事实"(资源、磁盘、时钟、进程存活)开发侧要能核,**要么 ops 回报,要么经 `/status`
暴露**。本方案选后者为主(可自动化、可复验),ops 回报为兜底。

## 2. 关键使能:网关 `/status` 管理面(经 HTTPS,访问受控)—— 待实现

网关**已有** `GET /healthz`(返回 `{ok, pool: <池态>}`)。在其上扩展一个**受控管理面** `GET /status`,
把 HTTPS-only 下开发侧需要的服务端信号一次给全:

- **池态**:ready / assigned / spawning / busy(size=N;M4 就绪告警)。
- **进程资源**:网关 + 池管理器 + 各 agent 的 RSS / 句柄(服务自采 `psutil`;这是 SK-1 的正解——量
  常驻主进程,agent 子进程数另列)。
- **录音磁盘**:`recordings` 目录用量 + 增速估算(Q5/浸泡)。
- **转码积压**:转码器队列深度 / 已完成 / 失败(N2/D-13)。
- **时钟**:`ntp_synced`、`clock=UTC`、`now_utc`、时间戳单调性自检(R5)。
- **监控七项(R7,§11 表)**:逐项当前值 + 是否越阈。
- **运行元信息**:uptime、systemd restart 计数(R4 观测)、版本/commit。

**安全(R6/D-18)**:`/status` 暴露拓扑级信息,**必须访问受控**——不走公开路径、需管理 token
(强于 Q6 公众口令);对外只给**状态/计数/资源摘要**,不回**原始内部端口/进程号/路径**(不泄漏拓扑)。
**对齐项**:①公开-受控暴露 vs 仅内网 + ops 转发;②管理 token 由谁签发/轮换。

## 3. 远端 harness 模式(经 HTTPS 驱动)—— 待实现

现有 `harness/soak.py` 在本机拉真栈 + spawn 假 agent。加 **`--remote <https-url> [--admin-token …]`**:

- 不在本机 spawn;N 个虚拟用户经 **`wss://`** 打**目标机网关**(真 TLS、真会话、真宽限窗/回收路径)。
- 客户端侧采:felt_latency、错误/超时率、繁忙率(N 摸底 P50/P95 判据,§9/P-9)。
- 服务端侧采:**轮询 `/status`** 取池态/资源/转码积压(替代本机 `psutil` 树采样)。
- 覆盖:**目标机 N=8/10 摸底**(客户端 KPI + `/status` 资源)、**真载荷浸泡客户端侧**、**功能冒烟**。
- 仍需机上/ops:systemd kill 测试、防火墙、证书、录音目录权限(§1)。

`probe`(§9 摸底)同理加远端模式,或复用 soak 远端模式产 KPI。

## 4. 必要对齐信息(部署前与运维逐项敲定)

> 这是"能快速部署 + 验证"的前置清单。每项:**要什么 → 为什么 → 谁提供**。

1. **对外入口**:公网域名 + HTTPS 端口(网关是 TLS 终结点,§6.4 不另设 nginx)。是 443 还是 `XG_LISTEN_PORT`
   直暴露 / 前面是否有 LB?→ dev 拼 wss:// 地址、ops 配防火墙。**ops 提供 URL,dev 确认协议路径**。
2. **TLS 证书**:证书/私钥文件路径(`XG_SSL_CERT`/`XG_SSL_KEY`)、域名匹配、续期机制。→ **ops 提供**。
3. **env 注入总表(§7.2)**:逐 env 的部署值——网关 `XG_LISTEN_HOST/PORT`、`XG_POOL_API`、`XG_GRACE_SECONDS`、
   `XG_ACCESS_CODE`(Q6 公众口令)、`XG_HMAC_SECRET`(**持久 vs 每重启失效**,定 R4 语义);池
   `XIAOGE_*`(§7.2:`WEB_UI_HOST=127.0.0.1`、191xx、`RECORD_MODE=full`、`RECORD_CODEC=opus`、
   `TIMELINE_LEVEL=audit`、`XIAOGE_ADMIN_ROUTES=0`)。→ **dev 给表 + 缺省,ops 填机器相关值**。
4. **进程拓扑 + systemd**:池大小 **N**(=目标机摸底定,见 §6)、启动顺序(**池管理器先于网关**)、
   崩溃重启策略、日志去向。→ **dev 给拓扑,ops 给 unit + N 机器容量**。
5. **端口 + 防火墙(M3)**:对外仅 HTTPS 口;内部 `127.0.0.1` 上池控制口(19000)、网关内口(10099)、
   agent 191xx **一律不对外**。→ **ops 配 + 自查;dev 从外部 nmap 复验**。
6. **机器规格 + 限额**:CPU 核数、RAM(N 定容量)、`recordings` 磁盘容量、**`ulimit -n`(FD 上限——
   高并发 WS 关键)**。→ **ops 提供**。
7. **可观测面(§2 决策)**:是否暴露 `/status`、鉴权方式、管理 token。→ **dev+ops 对齐,dev 实现**。
8. **录音 + 合规(Q5)**:`recordings` 路径、保留期、磁盘配额与清理策略(谁清)、目录权限最小化、
   编码(opus 满足审计)。→ **产品定策略,ops 配权限/清理,dev 给开关(D-14 组合)**。
9. **部署 + 回滚机制**:ops 怎么部署(git pull + `uv sync`?容器?制品?)、回滚(WP-0 §8:旧目录保留切回)、
   升级窗口。→ **ops 提供机制,dev 给版本/依赖清单**。
10. **验证协议 + 结果交换**:哪些 ops 机上做(nmap/kill/磁盘)、哪些 dev 经 HTTPS 做(冒烟/摸底/`/status`)、
    结果怎么互通(dev 看不到机上——ops 回报 vs `/status` 自读)。→ **本文件 §5 即协议初稿**。

## 5. 部署步骤(ops 机上)+ 验证协议(dev 经 HTTPS)

**A. ops 机上(按序)**:①放证书 + 配 `XG_SSL_*`;②注入 §7.2 env(网关 + 池);③起**池管理器**
`python -m poolmgr`(读 `XG_POOL_*`;control_api 绑 127.0.0.1:19000)→ 待就绪数=N;④起**网关**
`python -m gateway`(TLS 终结,对外口);⑤防火墙仅放行对外 HTTPS、内部口 loopback-only;⑥确认 systemd 自拉。
> 运行入口(均已实现,运行目录 `examples/voice_agents`):`python -m poolmgr`(启动器
> `poolmgr/launcher.py`,`XG_POOL_*` env)、`python -m gateway`(`gateway/main.py`,`XG_*` env)。
> **逐步操作手册见 [OPS_CHECKLIST.md](OPS_CHECKLIST.md)**。

**B. dev 经 HTTPS 验证**:①**功能冒烟**——页面加载、准入口令、通话(/ws/audio 回声)、四打断、聆听、
双标签页拒绝、繁忙页;②**M3 外部 nmap**——公网只见 HTTPS 口,191xx/19000/网关内口不可达;③读
**`/status`**——池 ready=N、资源基线、时钟 UTC/NTP、R7 七项在阈;④**N 摸底**(远端 harness §3)——阶梯
2→4→8→10,客户端 KPI + `/status` 资源,P-9 判据定 N。

**C. 需 ops 配合(dev 看不到机上)**:R4 kill 测试(ops kill 网关→dev 观测 HTTPS 5xx→systemd 拉起→
dev 观测恢复 + 全员回页)、R5 时间戳抽查(ops 抽录音/timeline 或经 `/status` now_utc)、磁盘/权限核查。

## 6. 上线五门在本约束下的落地映射

| 门 | HTTPS-可验(dev) | 机上/ops | 阻塞判据 |
| --- | --- | --- | --- |
| **目标机 N 摸底 + B5** | 远端 harness 客户端 KPI + `/status` 资源 | ops 提供机器 + 规格 | P-9:P95 退化 >30% 或错误率 >1% 到顶 |
| **真 4 路×2h 浸泡** | 远端 churn + `/status` 磁盘/转码积压/资源趋势 | ops 提供机器长跑 | 无累积泄漏 + 磁盘增速符 full+opus + 在线 KPI 无扰动 |
| **部署验收 M3** | 外部 nmap | ops 配防火墙 | 内部口全不可达 |
| **部署验收 R4/R5/R7** | `/status` 读时钟/告警 + 观测重启恢复 | ops kill 测试 + 时间戳抽查 | 自拉成功、UTC/NTP、七项接入 |
| **Q5 合规** | — | 产品定保留期/访问控制 + ops 配 | 有正式结论或临时策略(仅落盘不外发) |
| **A1-F1(#3 退出)** | — | 开发机可核(console 实形态) | 池侧 kill 已兜底,非阻塞 |

**合入 ≠ 上线**:五门未过不上线、不对外承诺产能 N(边界不变)。

## 7. 待实现的使能代码(供评审 + 排期;实现前先过 §2/§4.7 对齐)

1. **网关 `/status` 管理面**(§2):扩 `/healthz` → 受控 `/status`,聚合池态 + 自采资源 + 磁盘 + 转码积压 +
   时钟 + R7;管理 token 鉴权;不泄漏拓扑。**这是 HTTPS-only 验证的地基,优先级最高。**
2. **harness 远端模式**(§3):`soak.py`/probe 加 `--remote <url> --admin-token`,经 wss:// 打目标机 + 轮询
   `/status`。
3. **(随 §7.2)** 池 `default_agent_env` 已注 `XIAOGE_ADMIN_ROUTES=0`;部署文档化其余注入值由 ops 填。

> 下一步:本方案过评审 + 与运维对齐 §4 清单后,按 §7 排期实现 `/status` 与远端 harness,即可在目标机
> 就绪时"快速部署 + 经 HTTPS 验证"。
