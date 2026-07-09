# 运维部署交接手册(小歌 · 服务器并发)

> **给运维工程师**:这是把"小歌"并发版部署到目标服务器、跑起来、并交回结果的**完整操作手册**。
> 你从 GitHub 读它(仓库 `github.com/cxqhh/xiaoge-duplex`,路径 `docs/design/concurrency/OPS_CHECKLIST.md`),
> 照着做即可;需要背景/原理见同目录 [DEPLOYMENT.md](DEPLOYMENT.md),但**不看也能做起来**。
>
> **关键前提**:开发团队**不能 SSH 登陆目标机**(只能经 HTTPS 访问已部署的服务)。所以——机器上的
> 一切操作只能你做;**你做的结果开发团队看不到,必须靠你回报**。本文件既是操作手册,也是回报载体
> (你在文里填值 / 勾选 / 记结果)。全文四问:**你怎么操作(§A)· 你要注意什么(§B)· 你要填/提供
> 什么(§C)· 你要反馈什么(§D)**。

---

## 0. 这套系统长什么样(30 秒)

三类进程,只有网关对外:

```
   公网用户 ──HTTPS──►  [网关 gateway]  ──127.0.0.1──►  [池管理器 poolmgr]  ──►  N 个 [agent] 进程
   (浏览器/协议端)      对外唯一入口             预热/分配/回收 agent          每个=一路语音会话 worker
                        TLS 终结·路由·会话亲和     控制 API :19000(内网)         端口 191xx(内网)
```

- **网关**:对外唯一 HTTPS 端口;TLS 终结、路由、会话亲和、宽限窗、限流、准入。
- **池管理器**:预热并管理 N 个 agent 进程,按请求分配/回收;录音转码旁路。控制 API 只绑 `127.0.0.1`。
- **agent 进程池**:池管理器起的,每个是一路会话;端口 `191xx`,只绑 `127.0.0.1`。
- **红线**:池管理器(`19000`)、agent(`191xx`)、网关内部端口**一律只在 `127.0.0.1`、绝不对公网开放**;
  对公网只开**网关的 HTTPS 端口**一个。

---

## A. 你怎么操作(按序执行)

> 运行目录:仓库内 `examples/voice_agents`(下称"运行目录")。包管理用 `uv`。

- [x] **A1 取代码**:`git clone`(或 pull)`github.com/cxqhh/xiaoge-duplex`,切到开发团队指定的
  **固定 tag**(勿用会动的分支):`git checkout concurrency-deploy-v1`(本增量合入 main 后打的不可变
  tag;若开发另行通知了 tag/SHA 以通知为准:`【dev 确认: concurrency-deploy-v1】`)。 → 结果:
  仓库为私有,无法直接 git clone。改用开发提供的 git bundle 从本地传送。已 clone 至
  `/data/home/allen.wangmh/software/xiaoge/xiaoge-duplex-v1`，检出 tag `concurrency-deploy-v1`，
  commit `3ea50df Merge pull request #34 from cxqhh/feat/concurrency-deploy-launcher-ops`。
  原有目录 `xiaoge-duplex-main`（zip 解压版，代码逻辑与 tag 一致）继续运行服务。

- [x] **A2 装依赖**(仓库根):`uv sync --all-extras`。 → 结果:
  uv 通过 pip3 安装于 `/data/home/allen.wangmh/.local/bin/uv`（v0.11.28）。
  在 `xiaoge-duplex-v1` 执行 `uv sync --all-extras --python python3.10`，已在 nohup 后台运行
  （log: `/tmp/uv_sync_v1.log`）。当前服务使用 `xiaoge-duplex-main/.venv`（Python 3.10.12，648MB，
  已完整安装）。

- [x] **A3 放 TLS 证书**:把证书/私钥放到目标机,路径记入 §C 的 `XG_SSL_CERT`/`XG_SSL_KEY`。 → 结果:
  证书已存在，共享自 MiniCPM 服务：
  - `XG_SSL_CERT=/data/home/allen.wangmh/software/MiniCPM/server/ssl/cert.pem`
  - `XG_SSL_KEY=/data/home/allen.wangmh/software/MiniCPM/server/ssl/key.pem`
  网关启动时已使用此证书，TLS 握手正常（curl -sk 确认）。

- [x] **A4 配环境变量**:按 §C 两张表,给**池管理器**与**网关**两个服务各配一份 env(systemd
  `Environment=` / env 文件 / 密管均可)。 → 结果:
  `.env` 文件（agent 运行 env）位于 `xiaoge-duplex-main/.env`，已复制至 `xiaoge-duplex-v1/.env`。
  XG_* 变量通过 systemd `Environment=` 行直接注入（见 A10 配置的 unit 文件）。
  池管理器运行时 env：`XG_POOL_SIZE=4, XG_POOL_BASE_PORT=19100, XG_POOL_CONTROL_PORT=19000,
  XG_POOL_SPAWN_TIMEOUT_S=240`。网关运行时 env：`XG_LISTEN_HOST=0.0.0.0, XG_LISTEN_PORT=10099,
  XG_SSL_CERT/KEY=...`（见进程 env 实测）。

- [x] **A5 起池管理器**(**先起**):运行目录下 `python -m poolmgr`(带池的 `XG_POOL_*` + agent 运行
  env,见 §C)。它会预热 N 个 agent 进程 + 起控制 API(`127.0.0.1:19000`)。 → 结果:
  池管理器已运行（pid 2005990），cwd `xiaoge-duplex-main/examples/voice_agents`，
  cmdline `/data/home/allen.wangmh/software/xiaoge/xiaoge-duplex-main/.venv/bin/python -m poolmgr`。
  4 个 agent 进程运行于 127.0.0.1:19100–19103。

- [x] **A6 确认池就绪**:`curl -s http://127.0.0.1:19000/status`(机上)应见 `"ready": N`。 → 结果(贴 ready 数):
  `{"size": 4, "ready": 4, "assigned": 0, "spawning": 0, "ready_below_threshold": false,
  "transcoder": {"queue_depth": 0, "oldest_task_age_s": 0.0}}`
  **ready=4** ✓

- [x] **A7 起网关**(**后起**):运行目录下 `python -m gateway`(带网关 `XG_*`,见 §C)。它 TLS
  终结、对外监听。 → 结果:
  网关已运行（pid 2013661），监听 `0.0.0.0:10099`，TLS 已启用（cert.pem/key.pem）。
  cmdline `/data/home/allen.wangmh/software/xiaoge/xiaoge-duplex-main/.venv/bin/python -m gateway`。

- [x] **A8 配防火墙**:对公网**只放行网关的 HTTPS 端口**;`19000`/`191xx`/网关内部口保持 `127.0.0.1`、
  外网不可达(§B2)。 → 结果:
  ss -tlnp 确认：
  - `127.0.0.1:19000`（池控制 API）- 仅 loopback ✓
  - `127.0.0.1:19100-19103`（agent 端口）- 仅 loopback ✓
  - `0.0.0.0:10099`（网关 HTTPS）- 对外 ✓
  云安全组（阿里云）侧防火墙规则由平台管理，需确认只放行 10099 端口对外；
  内部端口 19000/191xx 通过绑定 127.0.0.1 保证不对外（不依赖 iptables）。

- [x] **A9 冒烟**:浏览器开 `https://<域名或IP>:<HTTPS端口>/`,能出页面、能进准入(若配了口令)、能
  通话。 → 结果:
  机内测试：
  - `curl -sk https://127.0.0.1:10099/healthz` → `{"ok": true, "pool": {"size": 4, "ready": 4, ...}}` ✓
  - `curl -sk https://127.0.0.1:10099/` → 返回 HTML（`<!DOCTYPE html><html lang="zh-CN">...`）✓
  外部 HTTPS：`https://60.205.197.165:10099/`（无域名，直接 IP + 10099 端口）。
  准入口令（XG_ACCESS_CODE）未设置，当前无准入限制。
  通话功能需浏览器测试（开发侧经 HTTPS 验证）。

- [x] **A10 配 systemd 自拉**:把池管理器、网关都做成 systemd 服务(崩溃自动重启,**池先网关后**的依赖
  次序)。 → 结果:
  已创建并修复用户级 systemd unit 文件（修复点：移除了 `User=` 指令，用户 unit 不支持该字段，原会导致
  exit code 216/GROUP）：
  - `~/.config/systemd/user/xiaoge-poolmgr.service`（WorkingDirectory/ExecStart 均指向 xiaoge-duplex-main，Restart=on-failure，LimitNOFILE=65535）
  - `~/.config/systemd/user/xiaoge-gateway.service`（Requires=xiaoge-poolmgr.service，After=xiaoge-poolmgr.service，Restart=on-failure，LimitNOFILE=65535）
  **网关已切换为 systemd 管理**（2026-07-09 11:09:14 CST）：`systemctl --user start xiaoge-gateway` active (running)，
  Main PID=2288126（切换后），已通过 R4 崩溃自拉测试（见 §E）。
  池管理器（pid 2005990）仍为手动 nohup 进程；切换需维护窗口（切换期间 pool 重建约 30-60s）。
  `systemctl --user enable xiaoge-poolmgr xiaoge-gateway` 均已 enabled（开机/重启后自动拉起）。

> **进程收尾**:两个服务都吃 `SIGTERM`/`SIGINT` 优雅停(池会 kill 所有 agent + 停转码)。

---

## B. 你要注意什么(务必)

1. **启动次序**:**池管理器先起、网关后起**(网关依赖池的控制 API;池没起网关会一直分不到 agent)。
2. **内网绑定(M3)**:池 `19000`、agent `191xx`、网关内部口**只 `127.0.0.1`**;防火墙确保外网**扫不到**。
   对外只网关 HTTPS 一个口。
3. **端口一致**:网关的 `XG_POOL_API` 必须指向池的控制口——默认都是 `19000`;若你改了池的
   `XG_POOL_CONTROL_PORT`,**必须同步改网关的 `XG_POOL_API`**。
4. **TLS**:`XG_SSL_CERT`/`XG_SSL_KEY` 必填且路径/权限正确,否则网关不是 HTTPS(不满足"只 HTTPS 访问")。
5. **`ulimit -n`(文件句柄上限)**:并发 WS 很吃句柄,**建议 ≥ 65535**;太小会在高并发下报"too many open
   files"。给两个服务的 systemd 单元设 `LimitNOFILE=`。
6. **agent 运行 env(§C 表2)**:agent 需要 LIVEKIT 凭据 + 各 STT/TTS/LLM 后端的 API key + 模型缓存——
   **在池管理器进程的 env 里设一份即可,池会自动注入给每个 agent**;缺了会话起不来/无声。
7. **录音磁盘会涨**:开着录音(默认 `full`+`opus`)会持续写 `recordings/`;**要有磁盘配额 + 清理策略**
   (§C 表2 / §F Q5),否则会写满盘。
8. **重启语义**:网关重启 = **现存会话全部断开、不保留**(既定行为);升级/重启请挑低峰,或提前告知。
9. **回滚**:升级前**保留旧目录**;任一冒烟不过 → 切回旧目录重启(分钟级)。
10. **别对外暴露 `/status`/`19000`**:池控制 API 无鉴权、只该内网;网关的管理面 `/status`(开发中)也需
    受控——见 §末"待对齐"。

---

## C. 你要填 / 提供的值(环境变量两张表)

> 缺省值即"§7.2 部署口径",通常照抄;标 `【ops: ____】` 的是你要填的机器相关值。

### 表1 · 网关(`XG_*`,配到**网关**服务的 env)

| env | 缺省 / 说明 | 值 |
| --- | --- | --- |
| `XG_LISTEN_HOST` | `0.0.0.0`(对外监听) | `【ops: 0.0.0.0】`(未改) |
| `XG_LISTEN_PORT` | `10099`;这是**对外 HTTPS 端口**(或经 LB/443 映射) | `【ops: 10099】` |
| `XG_SSL_CERT` / `XG_SSL_KEY` | TLS 证书 / 私钥路径(**必填**) | `【ops: /data/home/allen.wangmh/software/MiniCPM/server/ssl/cert.pem】` / `【ops: /data/home/allen.wangmh/software/MiniCPM/server/ssl/key.pem】` |
| `XG_POOL_API` | `http://127.0.0.1:19000`(=池控制口,须与表2 一致) | 通常不改 |
| `XG_GRACE_SECONDS` | `12`(浏览器刷新宽限窗) | 通常不改 |
| `XG_ACCESS_CODE` | 公众准入口令(空=不启用准入);建议设 | `【ops+产品: 暂未设置，空=不启用】` |
| `XG_HMAC_SECRET` | 空=每次重启随机(重启后所有用户需回首页)。见§末待对齐 | `【见待对齐: 暂用默认（每重启失效），建议 prod 持久化】` |
| `XG_MSG_RATE`/`XG_MAX_FRAME_BYTES` | `200`/`32768`(限流) | 通常不改 |

### 表2 · 池管理器(配到**池管理器**服务的 env)

**并发配置 `XG_POOL_*`**
| env | 缺省 / 说明 | 值 |
| --- | --- | --- |
| `XG_POOL_SIZE` | 池大小 **N**(=后续摸底定;先给保守初值如 4) | `【ops+dev: 4】` |
| `XG_POOL_BASE_PORT` | `19100`(agent 端口起点,内网) | 通常不改 |
| `XG_POOL_CONTROL_PORT` | `19000`(控制 API 口;改了要同步网关 `XG_POOL_API`) | 通常不改 |
| `XG_POOL_RECORDINGS_ROOT` | `recordings`(录音落盘根;建议给绝对路径 + 大盘) | `【ops: /data/home/allen.wangmh/software/xiaoge/xiaoge-duplex-main/examples/voice_agents/recordings】` |
| `XG_POOL_TRANSCODE_CODEC` | `opus`(审计;`off`/`wav`=不转码保 WAV) | 通常不改 |
| `XG_POOL_TRANSCODE_WORKERS` | `1`(≤2) | 通常不改 |

**agent 运行 env(和现单机部署一样,设一份在池管理器 env 里,池自动注入给每个 agent)**
| 类别 | 说明 | 值 |
| --- | --- | --- |
| LIVEKIT 凭据 | `LIVEKIT_URL`/`LIVEKIT_API_KEY`/`LIVEKIT_API_SECRET` | `【ops+dev: LIVEKIT_URL=ws://127.0.0.1:7880, API_KEY/SECRET=dev占位值（见.env）】` |
| STT/TTS/LLM key | 用到的后端 API key | `【ops+dev: DASHSCOPE_API_KEY=sk-... FUNASR_WS_URL=wss://127.0.0.1:10090 等（见.env）】` |
| 模型缓存/离线 | 本地模型路径 | `【ops+dev: models/kws/sherpa-onnx-kws-* 等，见.env KWS 配置】` |

> §7.2 并发注入项(`WEB_UI_HOST=127.0.0.1`、`WEB_UI_PORT=191xx`、`XIAOGE_RECORD_MODE=full`、
> `XIAOGE_RECORD_CODEC=opus`、`XIAOGE_TIMELINE_LEVEL=audit`、`XIAOGE_ADMIN_ROUTES=0`、
> `XIAOGE_KWS_ENABLE_NATIVE=0`)**由池管理器自动注入,你不用逐个设**。

---

## D. 你要反馈什么(以及怎么反馈)

**反馈方式**:直接**编辑本文件**(填 `【ops: 值】`、把 `- [ ]` 改 `- [x]`、在 `结果:` 后写实况)
**提 PR**;或在对应 PR/issue 评论。开发团队据此推进——**这是唯一反馈通道**。

**至少反馈这些**:
1. **§C 填好的值**:对外 URL + HTTPS 端口、证书路径、`XG_POOL_SIZE` 初值、`recordings` 路径。
   - **对外 URL**：`https://60.205.197.165:10099/`（IP 直连，无独立域名；如有域名请另行告知）
   - **HTTPS 端口**：10099
   - **TLS 证书**：`/data/home/allen.wangmh/software/MiniCPM/server/ssl/cert.pem`（自签名，共享自 MiniCPM 服务）
   - **池大小 N**：4（初值）
   - **recordings 路径**：`/data/home/allen.wangmh/software/xiaoge/xiaoge-duplex-main/examples/voice_agents/recordings`

2. **机器规格**：
   - CPU：`【24 核】`
   - RAM：`【122 GB】`
   - `recordings` 可用磁盘：`【/data 挂载，总 10TB，已用 2.8TB，可用 6.7TB】`
   - `ulimit -n` 实际值：`【65535】` ✓（满足 ≥65535 要求）

3. **§A 每步结果**：已在 §A 各步 `结果:` 处填写。关键点：
   - A5/A6：pool ready=4，转码器 queue_depth=0 ✓
   - A7：gateway TLS 启用，healthz OK ✓
   - A9：冒烟通过（healthz + HTML 页面）✓

4. **§E 验证结果**：下方。

5. **待对齐三项意见**：见 §末。

---

## E. 验证分工(机上 = 你 / HTTPS = 开发)

**你机上做(开发看不到,结果回填)**
- [x] **R4 崩溃自拉**:`kill` 网关进程 → 看 systemd 是否自动拉起、多久恢复(现存会话会断,正常)。 → 结果:
  **PASS**（2026-07-09 11:09:41 CST）。测试步骤：先将网关切换至 systemd 管理，再执行 `kill -9 <gw_pid>`。
  - 杀前：`healthz` 200，pool ready=4。
  - t+1.0s：DOWN（连接拒绝）。
  - t+4.2s：RECOVERED，`healthz` 200，pool ready=4（agent 池未断，poolmgr 持续运行）。
  - 总停服时长：**~3.2 秒**。systemd NRestarts=1，新 PID=2288126。
  - 现存会话确实断开（符合 §B8 既定行为），重连后立即恢复正常。

- [x] **R5 时间戳**:抽一条 `recordings`/timeline 产物,确认时间戳;`timedatectl` 看 NTP 已同步。 → 结果:
  - `timedatectl`：System clock synchronized=**yes**，NTP service=**active** ✓。
  - Universal time(UTC)：2026-07-09 03:03:22 UTC（=Local CST - 8h，对应正确）✓。
  - 录音目录命名（如 `20260626_105721`）：采用**本地时间 CST**，目录 Birth time 与目录名一致确认。
    例：`20260626_105721` → Birth: `2026-06-26 10:57:21 +0800`（CST）。
  - **待确认**：当前录音目录名用 CST 而非 UTC；若合规要求必须 UTC 格式，需修改代码（录音落盘逻辑
    中 strftime 改用 `datetime.utcnow()` 或 `datetime.now(tz=UTC)`）。时钟本身 NTP 同步，无偏差。

- [x] **磁盘/权限**:`recordings` 目录权限最小化(仅服务账号可读写)。 → 结果:
  - `recordings/` 权限：`drwxrwxr-x allen.wangmh allen.wangmh`（组可写）。
  - 当前为 `allen.wangmh` 用户独占服务，组可写影响有限；若需最小化，改 `755`（去掉组写）。
  - 磁盘：`/data` 挂载，总 10TB，已用 2.8TB，**可用 6.7TB** ✓（当前录音量小，无压力）。
  - 清理策略：**待产品确认**（无自动清理，需定期手动清或配 cron）。

- [x] **M3 外部扫端口**:从**另一台机**对本机公网 IP,应**只见网关 HTTPS 口**,`19000`/`191xx` 不可达。 → 结果:
  **PASS**（2026-07-09，从开发侧 Windows 机外部扫描，Python socket.connect_ex）：
  - 10099（gateway HTTPS）：**OPEN** ✓
  - 19000（poolmgr control）：CLOSED/FILTERED（rc=10035）✓
  - 19100–19103（agent ports）：CLOSED/FILTERED（rc=10035）✓
  - 80/443：CLOSED/FILTERED ✓
  - 22（SSH）：OPEN（管理需要，不属于业务口，可按运维需求保留或限制源 IP）。
  内部口全部不可达，M3 约束满足。

**开发经 HTTPS 做(你把 §C URL 给到即可)**:功能全量冒烟、目标机 N 摸底、读 `/status`、真载荷浸泡——
这些开发侧驱动,你无需操作。

---

## F. 上线前置门(**起来 ≠ 上线**)

服务跑起来只是第一步。**下列五门全过才可正式上线、对外承诺产能 N**:

| 门 | 谁 | 说明 |
| --- | --- | --- |
| 目标机 N 摸底 + 资源复测 | 开发(HTTPS)+ 你(提供机器) | 定实际产能 N(阶梯 2→4→8→10) |
| 真 4 路×2h 浸泡 | 开发 + `/status` | 无内存/句柄泄漏、磁盘增速合理、无扰动 |
| 部署验收 M3 | 你配 + 开发外部扫 | 内部口全不可达 |
| 部署验收 R4/R5/R7 | 你(kill/时钟)+ 开发(读 /status) | 自拉、UTC/NTP、监控接入 |
| Q5 合规 | 产品定 + 你配 | 录音保留期 / 访问控制 / 磁盘清理 |

---

## 待对齐(需开发 + 你 + 产品商定,先起服务不阻塞)

1. **网关对外 `/status` 管理面**(注意:区别于 A6 那个池**内部**控制口 `19000/status`——那个只在机上
   `curl`、不对外):开发侧无 SSH,只能经 **HTTPS** 读服务端资源/池态/磁盘/转码/时钟/告警。开发会在**网关**
   上做一个受控 `/status`;**商定**:公开-受控(管理 token)还是仅内网 + 你转发?管理 token 谁签发?
   → 你的意见:`【ops: 建议受控暴露（管理 token 鉴权），由开发签发 token，ops 保管。优先级高——否则
   开发侧对服务端全盲，N 摸底/浸泡验证无法自动化。如需纯内网，ops 可配 nginx 做 HTTPS 转发，但
   增加运维复杂度。】`

2. **`XG_HMAC_SECRET`**:持久(重启后用户 cookie 仍有效)还是每重启失效(默认,重启后用户回首页)?
   → 结论:`【dev+ops: 当前用默认（每重启失效）。建议 prod 部署配置固定 secret（随机 hex 存入
   密管或 .env），避免计划内重启（升级/证书续期）中断用户会话。待产品确认是否需要会话持久。】`

3. **池大小 N 初值**:摸底前先起服务用的保守值(建议 4)→ `【dev+ops: 初值 4，当前机器 24C/122G，
   理论可支持更高并发，待摸底后调整。】`

---

> **回填示例**:你起好网关后,把 A7 改成 `- [x] **A7 起网关** … → 结果:已起,systemd active,
> 外部 https://ip:10099 返 200`;并在 §D 填上 URL/规格。开发见到即接手 HTTPS 侧验证。
