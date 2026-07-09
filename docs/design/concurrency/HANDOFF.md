# 并发改造 · 研发交接文档(接手工程师从这里开始)

> **给接手"小歌服务器并发改造 + 真实上线"的云服务研发工程师。** 读完这一页,你能知道:这是什么、
> 现在到哪了、还差什么、怎么继续、有哪些铁律不能破。细节都在同目录其它文档,本页是**入口 + 路线图 +
> 规矩**。截至 **2026-07-09**。

---

## 1. 一分钟全局

- **做的事**:把"小歌"(全双工中文语音 agent,LiveKit Agents fork)从**单进程单会话**改造成**服务器多用户
  并发**——单机 N 路会话,浏览器多用户 + 协议客户端(C/MATLAB/Python SDK)共池。
- **架构**:公网 →(HTTPS)→ **网关(gateway)** →(127.0.0.1)→ **池管理器(poolmgr)** → **N 个 agent 进程**
  (每个=今天的单会话 agent,全路由)。只有网关对外;内部口全 loopback(M3)。
- **现在到哪了**:**编码主体(agent 六处小改 / 录音审计子系统 / 池管理器 / 网关 / 集成 harness / M5 / 浸泡
  harness / 部署启动器 / 运维文档)已全部合入 main**;已在**阿里云 `60.205.197.165`(24C/122G)部署跑起来**、
  机上门(R4 崩溃自拉、M3 内网绑定)PASS。
- **还差什么**:两条评审已通过的分支待推+合(A1-F1 测、PR-E 方案);**PR-E 的使能代码(网关 `/status`
  管理面 + 远端 harness + R5 时间戳修正 + 部署卫生)待编码**;然后过**上线五门**才能真上线。

---

## 2. 先读这些(顺序)

**交接范围 = 整个 `docs/design/concurrency/` 文件夹**——R4/R5/R6/R7 门、§7.2 注入表、§11 监控七项、P-9 判据
都定义在 `CONCURRENCY_DESIGN.md` 内,`README.md` 是索引;**勿只交 PR-E 片段**,否则 R 号/§/P 无处解析。
打包时**剔除 `GATEWAY_MOBILE_REVIEW.md`**(无关的网关移动端评审,见 §7 末)。

全部在 `docs/design/concurrency/`(先读该目录 [README.md](README.md) 的关系图):

| 顺序 | 文件 | 干嘛的 |
| --- | --- | --- |
| 1 | [README.md](README.md) | 文档索引 + 关系图 + 维护规则(**先读**) |
| 2 | [CONCURRENCY_DESIGN.md](CONCURRENCY_DESIGN.md) | **规格唯一权威**:决策总账 D-01~D-23、网关六路由规则(§6.1)、env 注入总表(§7.2)、20 条 checklist(§12.2) |
| 3 | [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) | 实施方案 + **全部评审往返**(A/B/C/D/M5/soak/OPS/PR-E 逐轮意见 + 应答);**§12.2 台账**(20 条覆盖情况);**PR-E 方案**在尾部 |
| 4 | [DEPLOYMENT.md](DEPLOYMENT.md) | HTTPS-only/无 SSH 约束下的部署与验证方案(职责切分、`/status` 使能、远端 harness) |
| 5 | [OPS_CHECKLIST.md](OPS_CHECKLIST.md) | **运维交接手册**(运维已按它部署 + 回填结果,含机器规格/env 实值) |
| 6 | [CONCURRENCY_PROBE_REPORT.md](CONCURRENCY_PROBE_REPORT.md) | 外部容量摸底(N≤4 下界)+ 判据 |
| — | [GATEWAY_MOBILE_REVIEW.md](GATEWAY_MOBILE_REVIEW.md) | **另一条线**:网关面向 Android/移动端的评审(GW-1~GW-4),见 §7 待办 |

**规格与实现的对应**:每条 D-xx/M3/R4/§6.1 规则在代码里都有落点;看不懂某处代码为什么这么写,顺编号回
DESIGN §2 总账,再顺"溯源"列回 `CONCURRENCY_REVIEW_ARCHIVE.md`。

---

## 3. 代码在哪 · 怎么跑

运行目录:`examples/voice_agents/`。包管理 `uv`。

| 组件 | 路径 | 启动 |
| --- | --- | --- |
| 网关 | `gateway/`(`main.py` 六路由 / `proxy.py` WS 泵+宽限窗 / `affinity.py` cookie+状态机 / `config.py`) | `python -m gateway`(读 `XG_*`) |
| 池管理器 | `poolmgr/`(`manager.py` 状态机 / `control_api.py` / `transcoder.py` / `launcher.py`) | `python -m poolmgr`(读 `XG_POOL_*`)**先起** |
| agent | `web_ui_agent.py` + `webpanel/` + `app/` + `providers/`(池 spawn 的 `web_ui_agent.py console`) | 池管理器 spawn,内网 191xx |
| 压测 harness | `harness/soak.py`(churn + 采样查泄漏;`--remote` 待加,见 PR-E) | `python -m harness.soak` |

**部署基线 = 不可变 tag `concurrency-deploy-v1`**(=合并 commit `3ea50df`)。运维无 SSH、库是私有——**给 tag
让运维 `git clone` + `checkout` 该 tag**(勿指会动分支);见 OPS_CHECKLIST §A1。

**测试**:`uv run pytest tests/test_ours_concurrency_*.py`(**逐个列文件路径,别用 `-k`**——`-k` 会触发全仓
collection,而 test_tts/test_vad 等需云账号会 collection 报错)。本机全绿约 108 + a1f1 3 例。

---

## 4. 已完成 vs 待办

### 已合入 main
agent 六处小改(PR-A1/#5=PR-A2)· 池管理器 + 转码器(PR-B)· 网关(PR-C)· 全链集成 harness(PR-D)·
M5(asr/tts 隐藏 404)· 浸泡 harness(含 SK-1 修)· 部署启动器(`python -m poolmgr`/`gateway`)· 运维文档
(DEPLOYMENT/OPS_CHECKLIST)· off-nit 修。**tag `concurrency-deploy-v1` 已打**。

### 未合分支(评审已过,待负责人授权合入)
| 分支 | 内容 | 状态 |
| --- | --- | --- |
| `feat/concurrency-a1f1-exit-efficacy` | A1-F1(#3 优雅退出触发器测 + marshal 隔离断言) | **评审通过**;本地领先 origin 2 commit(复评应答未推)。**建议先合** |
| `docs/concurrency-post-deploy-plan` | **PR-E 方案**(零代码) | **方案通过**;本地领先 origin 1 commit(4 评审点已并入)。**A1-F1 后合、需 rebase** |

> ⚠️ 交接时这两条分支有**本地未推提交**(评审复评的应答 + marshal 测)。接手第一件事:把它们 `push`
> (`git push --force-with-lease` a1f1、`git push` PR-E),再走合入(A1-F1 先、PR-E 后 rebase)。

### PR-E 待编码(方案已通过,负责人批准后写码;详见 IMPLEMENTATION_PLAN 尾部 PR-E 节)
- **E-1 网关 `/status` 管理面**(HTTPS 验证的地基,优先级最高):聚合池态 + **网关 + 池主进程 RSS/FD**
  (SK-1 正解)+ 录音磁盘 + 转码积压 + 时钟(UTC/NTP)+ R7;`XG_ADMIN_TOKEN` 门(缺/错→404 不泄漏拓扑);
  **token 保管在 E-1 落地前定**。
- **E-2 远端 harness**:`soak.py`/probe 加 `--remote <url> --admin-token [--insecure]`,经 `wss://` 打目标机 +
  轮询 `/status`;`--insecure` **仅显式 opt-in**。做目标机 N 摸底 + 浸泡客户端侧。**浸泡验 drain<5s**(A1-F1 watch)。
- **E-3 R5 时间戳自适应**:现录音目录名/timeline 用 **localtime(非 UTC,不达标)**——抽 `record_stamp()` 助手 +
  `XIAOGE_RECORD_TZ`(代码默认 `local` 保 PC 形态、池注入 `utc`);**R5 门 open 至此落地**。
- **E-4 部署卫生**(门资格限定,非仅卫生):服务现跑 **zip 版非 tag**,R4/M3/R5 回填**暂定**至服务切 tag 版 +
  核验等价;**池管理器仍 nohup 未上 systemd**(R4 只验了网关一半)。

---

## 5. 铁律与流程(**必须守,踩了会返工**)

1. **双重门**:动工程代码前须 ①**实施前评审通过**(评审组)+ ②**项目负责人批准**。方案(零代码)也走
   评审 + 批准,**批准后才写码**。**合入 ≠ 上线**。
2. **评审组只读**:评审组(另一 Claude 角色)只在文档追加意见,**从不改工程代码**;设计者(你)应答 + 改码。
3. **评审意见处理流程(2026-07-09 起)**:评审意见**先放本地**(不直接进 GitHub)→ 你分析 → **与负责人讨论
   确认** → **确认后才推**。
4. **先红后绿**:每个改动配行为锁定测,**能证明"没这个改动就判红"**(见 B-C-2/SK-1/marshal 的先红验证)。
5. **门禁**:改动登记进 `ourcode.txt` → `make lint-ours`(0 违规 0 noqa)+ `scripts/check_line_counts.py`
   (**500 行硬上限 / 400 警**)+ `ruff format`。提交前跑绿。
6. **真 I/O 集成测**:进程/连接/端口/宽限窗类组件**必配真子进程 + 真端口 + 真 WS 集成测**,不以假时序单测
   代替——本项目反复栽在"假 I/O 单测放过真缺陷"(B-1~B-4、C-1、OPS-1、SK-1)。
7. **PC/测试形态不变**:服务器行为经 **env 注入**开启,**代码默认 = 单机/PC 口径**(M3/§7.2/M5/E-3 都这套);
   改默认前想清楚会不会动到单机行为。
8. **上线五门**(未过不上线、不承诺产能 N):目标机 N=8/10 摸底 + B5 · 真 4 路×2h 浸泡 · M3 · R4/R5/R7 · Q5 合规。

---

## 6. 部署现实(HTTPS-only / 无 SSH)

- **开发侧对目标机无 shell**,只能经 HTTPS 访问服务。故:机上一切(部署/systemd/证书/env/kill 测/磁盘)=
  **运维做**;开发侧只能**经 HTTPS 驱动负载 + 读服务暴露的信号**(→ 这就是 E-1 `/status` 必须做的原因)。
- **运维交互 = 唯一文件 [OPS_CHECKLIST.md](OPS_CHECKLIST.md)**:运维经 GitHub 读该 tag 的文件、填值/勾选/记
  结果**回 PR/评论**(开发侧唯一反馈通道)。已回填一轮(机器规格、env 实值、R4/M3/R5 结果)。
- **当前部署实况**(运维回填,OPS_CHECKLIST §A~§E):`https://60.205.197.165:10099/`(自签证书、IP 直连、
  无域名、无准入口令)、pool ready=4、`XG_POOL_SPAWN_TIMEOUT_S=240`(agent 冷启慢)。**R4/M3 PASS(暂定,
  见 E-4)、R5 open(localtime)**。

---

## 7. 立刻该做什么(执行序 + 决策依赖 + 分工)

> 本节采纳评审组 2026-07-09"交接说明"——不改方案实质,只把"照着做"的排序/依赖显式化,避免返工。

**第 0 步 · 推 + 合两条已过分支**:push a1f1 + PR-E → 负责人授权 → **A1-F1 先合、PR-E rebase 后合**。

**执行序(⚠️ 必须先做 E-4.1,否则量的是未核验产物、数据不作数)**:
1. **E-4.1 先行(运维)**:把服务从 zip 版(`xiaoge-duplex-main`)**切到 tag `concurrency-deploy-v1` + 核验等价**
   (`git status`/diff 或校验和)。**在此之前,R4/M3/R5 回填与任何 E-2 远端摸底数据都不作数**。
2. **E-4.2(运维)**:池管理器上 systemd(补 R4 池侧自拉;当前只有网关上了 systemd)。
3. **E-1(`/status`,研发)** → **E-2(`--remote` 摸底/浸泡,研发)** → **E-3(R5 时间戳,研发)**;
   **E-1 是 E-2 远端采样的前置**(远端读服务端资源/池态靠 `/status`)。各带先红后绿测 + 门禁。
4. **经 HTTPS 做目标机验证**:N=2→4→8→10 摸底(客户端 KPI + `/status` 资源)、真 2h 浸泡(含 drain<5s)、
   R5(注 `utc` 后)、R7 接入。
5. **过五门 → 负责人授权上线**。

**决策依赖(先要来再定稿——见 §8):**
| E 项 | 卡在哪个开放决策 | 不定的后果 |
| --- | --- | --- |
| **E-1 定稿** | `/status` **admin token 保管/轮换**(运维) | token 无归属 → 端点无法安全暴露 → E-1 落不了地 |
| **E-3 默认** | **R5 是否强制 UTC**(产品) | 默认 tz 未定 → `record_stamp()` 与注入 `XIAOGE_RECORD_TZ` 口径悬空 |
| **上线(非本期)** | HMAC 持久 · `XG_ACCESS_CODE` · Q5 保留期 · 真证书+域名 · SSH 限源 | 属上线门入参,**不阻塞 E 编码** |

**分工**:**E-1/E-2/E-3 = 研发写码**(各先红后绿测 + 门禁);**E-4 = 运维动作**(tag 切换、systemd),研发驱动、
运维执行回填。

**边界重申**:方案通过 ≠ 编码放行(仍需负责人批准);**合入 ≠ 上线,上线仍锁五门**(目标机 N 摸底 + B5、真 2h
浸泡、M3、R4/R5/R7、Q5)。

> **另一条线(不在本期 E 范围)**:[GATEWAY_MOBILE_REVIEW.md](GATEWAY_MOBILE_REVIEW.md) 的 GW-1~GW-4 是**网关面向
> 移动/Android 客户端**的评审(GW-1 是真安全口子:准入口令挡不住 `/ws/audio` 无 cookie 路;GW-2/GW-3 移动静默
> 掉线下会话立不住 / REATTACH 名存实亡;GW-4 `clients/PROTOCOL.md` 未同步 wire 契约)——**须网关侧 + 需求方先出
> 结论**,Android 方案见 `docs/project/ANDROID_SDK_PLAN.md`。**它与并发上线正交,打包并发交接时剔除**(但接手人
> 应知其存在)。

---

## 8. 开放决策(等产品 / 运维拍板,是 PR-E 编码 / 上线的入参)

- **产品**:R5 录音时间戳是否强制 UTC(定 E-3 默认)· `XG_HMAC_SECRET` 是否需会话持久 · `XG_ACCESS_CODE` 是否
  设(注意 GW-1)· 录音保留期/清理策略(Q5)· 上线是否要真证书 + 域名。
- **运维**:`/status` admin token 谁签发/保管/轮换 · SSH 22 是否限源 IP · 确认录音命名实跑口径。

---

## 9. 环境 / 工具速查

- 开发机 Windows;PowerShell + Bash 双 shell。Python `.venv/Scripts/python.exe`(3.10)。
- `make install`(=`uv sync --all-extras --dev`)· `make lint-ours`(自有代码规范)· `make check`。
- **私有 GitHub 库 `github.com/cxqhh/xiaoge-duplex`**;运维无直连,靠 **git bundle** 传 + 固定 tag。
- 临时文件放会话 scratchpad,别进仓库;录音/runs/ 已 gitignore;`docs/reports/concurrency_soak_*.md` 已 gitignore。

---

## 10. 别再踩的坑(前人血泪)

- **SK-1**:soak 的 RSS/FD 判据要量**主进程**,别整棵进程树求和(agent 子进程随回收增减会污染 → 假阳)。
- **B-C-2**:网关 `handle_audio` 上游连接失败分支**必须** `on_audio_disconnect`,否则会话永停 ACTIVE、泄漏 + cookie 锁死用户。
- **C-1**:双标签页被拒连接不能拿 conn_id(结构守卫),否则误调 disconnect 会错杀真会话。
- **D-07 vs 宽限窗**:协议客户端即断即杀(deadline=now)、浏览器才享宽限窗;**收尾统一走 sweep,不放请求
  finally**(连接取消会打断 finally 的 await)。
- **OPS-1**:池管理器要有生产入口(`python -m poolmgr`),别让运维照文档跑不起来。
- **aiohttp**:默认 cookie jar **拒收 IP 主机(127.0.0.1)cookie**——测试/客户端要 `CookieJar(unsafe=True)`。
- **agent 冷启慢**:运维实测 `XG_POOL_SPAWN_TIMEOUT_S=240`;摸底/浸泡的 ready 等待要据此放宽。
- **marshal 承重墙**:面板在独立线程,`web→agent` 必经 `*_threadsafe`,别改直调(a1f1 已加断言锁死)。

---

> **一句话交接**:编码主体已进 main、已在阿里云跑起来、机上门过;**接手就是:推合两条过审分支 → 编 PR-E
> 的 `/status`+远端 harness+R5+卫生 → 经 HTTPS 做目标机摸底/浸泡/验收 → 过五门上线**;全程守双重门 +
> 先红后绿 + 门禁,评审意见先本地讨论再推。有疑问顺 IMPLEMENTATION_PLAN 的评审往返查"当初为什么"。
