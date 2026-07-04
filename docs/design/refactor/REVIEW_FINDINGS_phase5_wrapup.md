# 代码缺陷评审 — refactor/phase5-wrapup(定稿版)

> 状态(2026-07-04):**三轮评审-回应闭环结束,全部事项定稿**;本文件为整理后的终稿,
> 供评审组最终确认。**确认后方开工**;当前工程代码自评审开始未改动一行。
> 结构:§A 定稿结论总表 → §B 实施计划(工作包/顺序/验收) → §C 全局验收清单 →
> §D 修复落地记录(实施时回填) → 附录:三轮评审-回应原文存档(未改动,审计用)。

---

## §A 定稿结论总表

裁定口径:🔴/🟠/🟡=严重度;定性=本分支新引入 / 既有(重构前即存在) / 规范 / PLAUSIBLE;
处置均为三轮往返后的**终稿**(细节依据见附录对应轮次)。

| # | 严重度 | 定性 | 终稿处置 | 级别 |
|---|---|---|---|---|
| 1 | 🔴 | 新引入 | 两个入口文件**最顶部**(先于一切自有包 import)执行 `load_dotenv(override=True)`(E402 头段或微模块固定顺序);`TURN_METRICS_LOG` 等模块级缓存评估改惰性读取。**附 subprocess 形式的 import 顺序守护测试**(哨兵变量仅存在于 .env,断言模块常量取到) | P0 |
| 2 | 🔴 | 新引入(v1/v2 修复的次生缺陷) | 恢复函数终稿顺序(T1 修正 1):`old` 存局部 → `state` 置安全态(None/False,外层清理只见新态) → `close(old)`(幂等 suppress) → `return_synthesizer(old)`(仅此一次) → 冷建(窗口内零旧对象责任) → 重放。**附单测:冷建窗口注入取消,断言 old 被 close+归还各恰好一次** | P0 |
| 3 | 🟠 | 新引入 | `_drain_pending` 返回 saw_sentinel(或哨兵回插队头),写完批次立即退出(覆盖"哨兵被吞后窃取后续哨兵"路径);atexit 哨兵改阻塞 put 带上限(~0.5s)重试,失败即放弃且不 join——**最坏退出耗时严格 < 现状 2s**。补"行+哨兵同批"单测 | P0 |
| 9 | 🟡 | 规范 | `setup_taps.py`(实测 503 行)拆至 ≤450(候选缝:在线打断三函数独立成模块,或指标日志两函数迁出);>100 字符长行拆行;**`make lint-ours` 增行数门禁**(>500 报错 / >400 警告,读 ourcode.txt);BASELINE.md 数字勘误并注明 | P0 |
| 4 | 🟠 | 既有 | `_QwenStreamCallback` 的 error 分支与 `on_close` 补 `audio_done.set()`(照抄 Bailian 同款处理);drain 异常尽早浮出单独小改 | P1 |
| 5 | 🟡 | 回归(类未接线,无线上影响) | `_FunASRStream._run` 开头 `self._speaking = False`,一行 | P1 |
| 6 | 🟡 | 既有 | funasr_stream 握手改调 `funasr_init_payload(...)`消除双源;**热词加权属显式行为变更**,独立提交 + 基线录音 A/B(停止词召回/误识对比)佐证,不做静默修改 | P2 |
| 7 | 🟡 | PLAUSIBLE(阶段2 API 收窄与 shim 文案矛盾) | 按 (a-显式):`__init__` 显式列出 `sample_rate/speech_rate/pitch_rate/instruction`(default None,非 None 映射进 opts + DeprecationWarning);**与 `opts` 混用 → `ValueError`(禁止,docstring 写明)**;`def` 行 `# noqa: PLR0913` 按 CODE_GUIDELINES §5 棘轮规则登记台账;BASELINE 表述同步改"复杂度豁免 0,兼容性豁免 1(已登记)" | P2 |
| 8 | 🟡 | 清理(双份轮次策略;分叉(a)经复核为功能差异非漂移,评审组已撤回该定性) | **WEB_UI 开关 + 零 env console 薄壳**:`WEB_UI`(默认 1)仅在 `web_ui_agent.__main__` 运行期消费(彼时 dotenv 已加载,无 #1 时序问题);`qwen_funasr_bailian_voice_agent.py` 改 ~10 行纯入口薄壳(**不设任何 env**,经自身 `__main__` run_app,天然无面板)。**console 入口显式行为变更**:开场白即兴→固定、默认音色 qwen→cosyvoice(想要旧音色 `.env` 写 `TTS_BACKEND=qwen`),写入提交说明与 `.env.example`;**console 入口回放 A/B** 实证等价;ARCHITECTURE §13 / CODE_GUIDELINES §1 同步 | P2 |
| 10 | 🟡 | PLAUSIBLE(弱化;received_audio 门为正确设计) | except 扩为保守连接类集合(WS-close + `ConnectionError`/`BrokenPipeError` 级);**`APIStatusError` 语义类错误刻意排除**(不重放),在 except 处注释写明为已知残余;`sent.append` 条件化时保留"完成门"语义 | P2 |

**§五清理项裁定**:1(env_bool 内联×2)/2(FunASR URL 字面量×4 → `providers/config.py` 常量)/5(webpanel 两处结构重复)→ P2 清理提交;3(Qwen 继承 Bailian)→ 排在 #4 之后;4(共享 session)→ 改造须守 `acquire_http_session` 的 owns 语义(**仅 owns=True 才关**);6(三个 shim)→ 缓删,合并后随文档扫尾 PR;7(显示净化/聆听谓词收敛)→ 立项,不在本轮。
**§四两条排除候选**:双方确认排除,不再排查。

---

## §B 实施计划(评审确认后执行)

### WP-1:P0 修复(本分支 `refactor/phase5-wrapup` 追加,合并前必须)

| 提交 | 内容 | 涉及文件 | 验收 |
|---|---|---|---|
| C1(评审#1) | dotenv 提前 + 守护测试 | `web_ui_agent.py`、`qwen_funasr_bailian_voice_agent.py`(顶部头段);新增 `tests/test_ours_env_loading.py`(subprocess:临时 cwd + 仅 .env 含哨兵变量,断言 `webpanel.state`/`common.runtime`/`app.setup_taps` 常量生效) | 守护测试绿;**直接 `python web_ui_agent.py console` 启动**实测 WEB_AUDIO/WEB_UI_PORT/XIAOGE_ONLINE_VAD_GRACE 三组 .env 值生效 |
| C2(评审#2) | 恢复函数按 §A-2 终稿顺序重写 | `providers/tts/cosyvoice.py`;`tests/test_ours_cosyvoice_recovery.py`(用 `__new__`+Mock `_tts`,含冷建窗口取消用例) | 单测断言 old 恰好 close+归还一次、state 安全态;触发录音(20260626_103029/143255)回放无 `_tts_inference_task` 错误 |
| C3(评审#3) | 哨兵传递 + atexit 上限 | `common/runtime.py`;`tests/test_ours_runtime_log.py` 增"行+哨兵同批"用例 | 复现脚本场景A/B 均 <0.1s 正常退出;单测绿 |
| C4(评审#9) | setup_taps 拆分 + 行数门禁 + 勘误 | `app/setup_taps.py`(拆出新模块)、`makefile`(lint-ours 行数检查)、长行拆行、`ourcode.txt`、`BASELINE.md` 勘误 | `make lint-ours` 含行数门禁全绿;`wc -l` 全量复测无 >500;82+ 单测全绿 |

### WP-2:P1 行级修(本分支追加,合并前顺手)

| 提交 | 内容 | 涉及文件 | 验收 |
|---|---|---|---|
| C5(评审#4) | error/on_close 补 `audio_done.set()` | `providers/tts/qwen_stream.py` | 对照 Bailian 行为一致;lint/单测绿 |
| C6(评审#5) | `_run` 重置 `_speaking` | `providers/stt/funasr_2pass.py` | lint/单测绿 |

### WP-3:合并评审与合并

- WP-1/WP-2 落地并回填 §D 后,分支链 `phase0→…→phase5-wrapup` 交合并评审;
  评审组按 §D"提交号↔评审编号"核销。

### WP-4:P2 独立提交/PR(合并后)

| PR | 内容 | 验收 |
|---|---|---|
| P2-a(评审#6) | funasr_stream 改用 `funasr_init_payload`(带热词) | 基线录音 A/B:停止词召回不降、误识不升 |
| P2-b(评审#7) | legacy 四参数 + ValueError 混用禁止 + noqa 台账登记 | lint 绿(含登记);构造兼容用例单测 |
| P2-c(评审#8) | WEB_UI 开关 + console 薄壳 + docs/.env.example 同步 | console 入口回放 A/B(KPI+事件序列)与 web 入口对照;`WEB_UI=0` 下面板线程不启动实测 |
| P2-d(评审#10) | except 扩保守集合 + 已知残余注释 + `sent.append` 条件化 | 单测覆盖新异常类;触发录音回放 |
| P2-e(清理 1/2/3/4/5) | env_bool 内联、URL 常量、Qwen 继承 Bailian(#4 后)、共享 session(守 owns 语义)、webpanel 去重 | lint/单测/回放冒烟 |
| P2-f(清理 6 + 文档扫尾) | 删三个 shim + ARCHITECTURE/ourcode.txt 同步;providers re-export A/B-only 注释 | 全仓 grep 无残留引用 |
| 立项(清理 7) | 显示净化/聆听谓词单点化 | 单独设计小节后另行评审 |

---

## §C 全局验收清单(WP-1/WP-2 完成时逐项打勾)

1. `make lint-ours`(含新行数门禁)0 违规;复杂度豁免 0(#7 的兼容性豁免在 P2-b 才引入)。
2. 全部 `tests/test_ours_*.py`(82+ 及新增 C1/C2/C3 用例)通过。
3. **直接 python 启动**路径 .env 生效实测(评审#1 的暴露路径)。
4. 触发录音回放(#2)+ 基线录音回放(整体不回退:turns/felt/事件序列)。
5. `wc -l` 全量复测 + BASELINE.md 勘误一致。
6. §D 落地记录回填完整。

## §D 修复落地记录(实施时回填,供合并评审核销)

| 提交号 | 评审编号 | 说明 | 验收凭据 |
|---|---|---|---|
| `f6a1351` | #1(C1) | env_bootstrap 微模块置于两入口首个自有 import;XIAOGE_DOTENV 测试钩子;E402 文件级豁免(仅此规则,注明理由);twin 顺带统一 override=True(已声明) | 守护测试 `test_ours_env_loading.py` 绿(子进程+哨兵 .env,三处常量生效);lint-ours 0 违规;"直接 python 启动"实测在 WP-1/2 收口时执行 |
| `afd5622` | #2(C2) | 恢复函数 T1 终稿顺序(摘 state→close→归还→冷建→重放);外层清理只见安全态 | **先红后绿**:`test_ours_cosyvoice_recovery.py` 冷建窗口取消用例对旧顺序实测双重归还(2 次),修复后 close×1+归还×1、cancelled=0;3 用例全绿;收口回放中恢复路径**实战命中 2 次**(20260704_190724/191528 各 1 次),零 `_tts_inference_task` 错误 |
| `4fe601d` | #3(C3) | _drain_pending 传递 saw_sentinel;atexit 阻塞 put(0.3s)+join(1.5s),最坏 1.8s < 2s | "行+哨兵同批"单测绿;评审复现脚本 A/B:2.004s+线程泄漏 → 0.000s 干净退出 |
| `30cfec4` | #9(C4) | setup_taps 503→417 行(在线打断三函数 → app/online_interrupt_host.py);行数门禁进 make lint-ours(scripts/check_line_counts.py,>500错/>400警);5 处长行拆行;BASELINE 勘误 | 门禁全量 0 超标;env 守护测试同步指向新模块仍绿 |
| `b115c54` | #4(C5) | _QwenStreamCallback error/on_close 补 audio_done.set()(照抄 Bailian) | 行为 spot-check:error 与 on_close 均置 done;lint/单测绿 |
| `40406e7` | #5(C6) | _FunASRStream._run 开头重置 _speaking(防跨框架重连残留) | 源码断言 + lint/单测绿 |

**§C 六项验收结果(2026-07-04 收口)**:
1. ✅ strict lint 0 违规(复杂度豁免 0);行数门禁 0 超标(>400 警告 3 个,软目标带内)。
2. ✅ 全部 `tests/test_ours_*.py` **87 通过**(82 存量 + C1 守护 1 + C2 恢复 3 + C3 哨兵 1)。
3. ✅ **直接 `python web_ui_agent.py console` 启动实测**:面板服务于 **8787(.env 值)**,8765(代码默认)无监听——评审#1 修复在暴露路径上生效(修复前必落 8765)。
4. ✅ 收口回放:触发录音 20260626_103029(25 轮/felt 1898ms)与基线 20260630_093520(19/14 轮/felt 2052ms,处于历次回放 18~19 轮的判停抖动带)均 tb=0/err=0。
5. ✅ 行数全量复测无 >500;BASELINE.md 勘误已同步。
6. ✅ 本表回填完整(C1~C6 ↔ 评审 #1/#2/#3/#9/#4/#5)。

**WP-1/WP-2 全部落地,分支链就绪,可交合并评审(按本表核销);P2 各项按 §B WP-4 合并后执行。**
| `4fe601d` | #3(C3) | _drain_pending 返回 saw_sentinel(写完本批即退);atexit 阻塞 put(0.3s)+join(1.5s),失败即放弃——最坏 1.8s < 修复前固定 2s | 新增"行+哨兵同批"单测绿;评审复现脚本 A/B 复验:2.004s+线程泄漏 → 0.000s 干净退出 |
| `30cfec4` | #9(C4) | setup_taps 503→417(在线打断策略拆出 app/online_interrupt_host.py);行数门禁进 make lint-ours(scripts/check_line_counts.py,>500错/>400警);5 处点名长行拆行;BASELINE 勘误注明 | 门禁实跑:无 >500 文件(3 个 400~500 警告);87 单测全绿;env 守护测试同步指向新模块 |
| `b115c54` | #4(C5) | error/response.error 与 on_close 补 audio_done.set()(照抄 Bailian) | 行为抽查:error/on_close 后 done 置位断言通过;lint 绿 |
| `40406e7` | #5(C6) | _run 开头重置 _speaking(防跨框架重连残留) | 源码断言 + lint 绿;类为 A/B 备用未接线 |

---

## §E 评审组终稿确认(2026-07-04)

**确认:本文件即为终稿,可据此开工。** 确认依据(逐项核对完成):

1. **共识吸收完整**:§A 十项处置与三轮往返的最终裁定逐条一致——T1 顺序修正已按
   "修正 1"原样进入 §A-2 与 C2(含"冷建窗口注入取消"单测断言);T2 混用策略取
   `ValueError`(评审组给出的两个选项之一,理由成立,认可);#8 零 env 薄壳、
   #3 最坏耗时 < 2s 上限、C1 守护测试覆盖 `webpanel.state`/`common.runtime`/
   `app.setup_taps` 三处、行数门禁与 #9 同一提交——均与裁定一致,无走样。
2. **事实性抽查通过**:`_FunASRStream._run`(§A-5)与当前代码类名一致;§C-1
   "复杂度豁免 0"与 #7 豁免在 P2-b 才引入的时序自洽;附录存档三轮俱全。
3. **过程约束守住**:自评审开始至本确认,工程代码未改动一行(git status 仅本文件
   与一个评审前既有的未跟踪 zip)。

**开工授权与两点执行提醒**(不新增要求,只是把易漏项前置):

- WP-1 四个提交(C1-C4)按序落地,**每个提交落地即回填 §D 一行**,不要攒到最后;
- C2 的单测请**先写断言、看它对旧顺序失败**,再实施新顺序(T1 的矛盾正是靠断言
  锁住的,红→绿的顺序是它生效的证明);
- P2 各项开工前无须再过评审组;WP-1/WP-2 完成、§C 六项打勾后,分支链交合并评审。

> 评审组签署:三轮评审-回应闭环,10 项发现全部形成终稿处置,无遗留争议。
> 本确认为评审阶段最后一笔;下一次评审组介入即合并评审(按 §D 核销)。

---

# 附录:评审-回应三轮往返原文存档(未改动)

# 代码缺陷评审报告 — refactor/phase5-wrapup

> 评审日期:2026-07-04
> 评审范围:`refactor/phase5-wrapup` 分支 vs `main`(59 文件,+6090/−4530)
> 评审方式:8 个独立查找角度(逐行扫描 / 删除行为审计 / 跨文件追踪 / 复用 / 简化 / 效率 / 深度 / 规范)并行扫全量 diff,去重后每条候选独立验证,其中 2 处附脚本级实测复现。
> 本文档仅记录问题,**未对分支做任何代码修改**。

## 总体结论

重构搬运本身相当忠实:import 全部可解析、无循环依赖、82 个单测通过、tap 顺序与 FunASR 握手载荷 1:1。但存在 **3 个本分支新引入的已确认缺陷**(其中 2 个已实测复现)、3 个搬运暴露/延续的既有缺陷、以及若干重复与规范问题。

**合并前建议至少修复 #1、#2、#3。**

| # | 严重度 | 结论 | 位置 | 一句话 |
|---|--------|------|------|--------|
| 1 | 🔴 高 | CONFIRMED(实测复现) | webpanel/state.py:23 等 3 处 | 环境变量 import 期读取早于 load_dotenv,.env 配置静默失效 |
| 2 | 🔴 高 | CONFIRMED | providers/tts/cosyvoice.py:271 | 恢复路径可能把同一连接双重归还池,跨轮串音/丢音 |
| 3 | 🟠 中 | CONFIRMED(实测复现) | common/runtime.py:31 | 日志线程关停哨兵被吞,每次退出固定卡 2 秒 + 丢日志 |
| 4 | 🟠 中 | CONFIRMED(既有) | providers/tts/qwen_stream.py:71 | Qwen TTS 服务端报错后每轮 30 秒死寂 |
| 5 | 🟡 低 | CONFIRMED(回归,类未接线) | providers/stt/funasr_2pass.py:120 | _speaking 跨重连残留,丢 START_OF_SPEECH |
| 6 | 🟡 低 | CONFIRMED(既有) | providers/stt/funasr_stream.py:184 | 握手漏 hotwords,optimized 栈主 STT 无热词加权 |
| 7 | 🟡 低 | PLAUSIBLE | providers/tts/cosyvoice.py:118 | __init__ 丢弃旧关键字参数,与 shim 兼容承诺矛盾 |
| 8 | 🟡 低 | CONFIRMED(清理) | qwen_funasr_bailian_voice_agent.py:124 | 控制台/web 轮次策略双份且已分叉 |
| 9 | 🟡 低 | CONFIRMED(规范) | app/setup_taps.py | 503 行超 500 硬上限,BASELINE.md 声明不实 |
| 10 | 🟡 低 | PLAUSIBLE(弱化) | providers/tts/cosyvoice.py:283 | 恢复只捕获 WS-close 一种症状 |

---

## 一、本分支新引入的缺陷(合并前必修)

### #1 环境变量在 `load_dotenv()` 之前被读取,`.env` 配置被静默忽略 🔴

- **位置**:
  - `examples/voice_agents/webpanel/state.py:21-25`(WEB_AUDIO / WEB_UI_PORT / WEB_UI_HOST / WEB_SSL_CERT / WEB_SSL_KEY)
  - `examples/voice_agents/common/runtime.py:19`(TURN_METRICS_LOG)
  - `examples/voice_agents/app/setup_taps.py:56`(XIAOGE_ONLINE_VAD_GRACE)
- **根因**:重构把这些 env 读取从 `web_ui_agent.py` 中 `load_dotenv(override=True)` **之后**的位置,搬到了新模块的**模块体(import 期)**;而这些模块在 `web_ui_agent.py:81` 调 `load_dotenv` 之前就已被 import。旧代码全部生效;新代码只认进程环境变量,`.env` 里的值(全部在 `.env.example` 有文档)被静默忽略。
- **实测复现**:`.env` 含 `WEB_AUDIO=1`、`WEB_UI_PORT=9999`、`XIAOGE_ONLINE_VAD_GRACE=9.9`,`import web_ui_agent` 后 `os.environ` 已有这些值,但 `webpanel.state.WEB_AUDIO=False`、`WEB_PORT=8765`、`ONLINE_VAD_GRACE=0.6`、`TURN_METRICS_LOG` 仍为默认路径。
- **后果**:浏览器语音模式静默失效(`/ws/audio` 路由不注册、通话按钮不出现)、面板绑错端口、TLS 配置被忽略退回明文 HTTP、回归指标日志写错文件(回放工具读到空日志)、在线打断 VAD 宽限期调参无效。
- **修复方向**(二选一):
  1. 这些模块级常量改为函数内惰性读取(或 property / 显式 init 函数);
  2. 在两个入口文件最顶部、任何 `app/`/`webpanel/`/`common/` import 之前先执行 `load_dotenv(override=True)`。
  注意 `start.ps1` 预导出环境变量的启动方式会掩盖此问题,直接 `python web_ui_agent.py console/dev` 的文档化启动方式必现。

### #2 CosyVoice 陈旧连接恢复路径可能把同一连接双重归还连接池 🔴

- **位置**:`examples/voice_agents/providers/tts/cosyvoice.py:271`(本分支 ee40021/e7ee249 新增的恢复逻辑)
- **根因**:`_rebuild_cold_and_replay` 的顺序是——271 行先把陈旧 synth 归还池(`_release_synth(state["synth"], state["pooled"])`),272 行才重建并覆盖 `state["synth"]`,273 行才翻转 `state["pooled"]=False`。冷重建是 ~0.8s 的网络操作(`to_thread(_build_synth)`);若此窗口内用户打断(CancelledError,语音场景常态)或重建抛异常,异常会穿透到 `_run` 的 `except BaseException`(329-336 行),对**已归还池的** synth 再次执行 `streaming_cancel()` + `_release_synth()`,且此时 `pooled` 仍为 True → 同一对象第二次 `pool.return_synthesizer()`。
- **池实现核实**(dashscope 1.25.23 `speech_synthesizer.py` `return_synthesizer` ~1044 行):**无身份去重**,仅有进程级借出计数守卫。并发下同一 `SpeechSynthesizer` 对象会进池两次,后续两次借出拿到同一底层 WS 连接,`__update_params` 跨轮改写回调 → 丢音或跨轮串音;即便单流场景计数守卫拦下第二次归还,335 行也已对池认为可借的连接执行了 `streaming_cancel()`。
- **修复方向**:调整顺序——先建新、再还旧;或归还前先置空 `state["synth"]` / 翻转 `state["pooled"]`,让后续清理路径不再触碰旧对象。

### #3 指标日志后台写线程的关停哨兵会被吞,每次退出固定卡 2 秒 🟠

- **位置**:`examples/voice_agents/common/runtime.py:26-54`(本分支 eea999f 新增的"指标日志下线程")
- **根因**(三段,均已核实):
  1. `_drain_pending`(26-34 行)批量取行时遇到 `None` 哨兵只 `break` 本地循环,**不向调用方传递任何信号**;
  2. `_log_writer_loop`(37-47 行)只在阻塞 `get()` 处检查哨兵,批次写完后直接回到 `get()`,中途落入批次的哨兵永久丢失 → 线程永不退出;
  3. `_flush_log_at_exit`(50-54 行)用 `put_nowait(None)` + 吞异常,队列满时哨兵直接丢弃;`join(timeout=2.0)` 在"退出时还有行未写"(atexit 恰在最后一波日志之后,是常见情形)下必然烧满 2 秒,守护线程被杀,哨兵之后入队的日志行丢失。
- **实测复现**:队列预置 `["line1","line2",None]` 再启动写线程 → `join` 耗时 2.009s、线程仍存活;对照组哨兵单独在队 → 0.000s 退出。回放回归循环每轮运行多付 2s 死等。
- **修复方向**:`_drain_pending` 返回"见到哨兵"标志(或把哨兵放回队列头),让 `_log_writer_loop` 写完批次后立即退出;退出路径的哨兵改用阻塞 `put`(或重试)。

---

## 二、搬运暴露/延续的既有缺陷(建议随手修或立项)

### #4 Qwen 流式 TTS 服务端报错后不置 done 事件,每个受影响轮次 30 秒死寂 🟠

- **位置**:`examples/voice_agents/providers/tts/qwen_stream.py:71-76`
- **根因**:`_QwenStreamCallback.on_event` 收到 `error`/`response.error` 只把 Exception 塞进音频队列,**不 `audio_done.set()`**,`on_close` 也是 `pass`。drain 任务虽立即抛错(`providers/helpers.py:79-80`),但要到 264 行 `await drain_task` 才被观察,而 `_run` 先阻塞在 244 行 `await asyncio.to_thread(audio_done.wait, 30)` → 报错后整整 30 秒死气,然后 TimeoutError,真实错误被丢弃。
- **对照**:兄弟类 `_BailianCallback`(`providers/tts/bailian.py:38-51`)在 error 和 close 都正确 `done.set()`。
- **溯源**:1:1 搬自 main(`custom_audio_providers.py:850-855,1074`),属既有潜伏缺陷,非重构回归。
- **触发**:`TTS_BACKEND=qwen` 且服务端 commit 后返回错误(无效音色、配额超限、会话被拒)。
- **修复方向**:error 分支和 on_close 里补 `audio_done.set()`(照抄 Bailian 的处理);顺带把 drain 任务异常尽早浮出。

### #5 `funasr_2pass` 的 `_speaking` 标志跨框架自动重连残留 🟡(重构回归,但类当前未接线)

- **位置**:`examples/voice_agents/providers/stt/funasr_2pass.py:120`
- **根因**:旧代码(main `custom_audio_providers.py:635`)的 `speaking` 是 `recv_task` 闭包局部变量,每次 `_run` 重置;重构提升为实例属性,仅在 `__init__` 初始化,`_run` 不重置。框架(`livekit-agents/livekit/agents/stt/stt.py:378-407` `_main_task`)在 `APIError`(含 `APIConnectionError`,`_recv_loop` 在 `WSMsgType.ERROR` 时正是抛它,168 行)后对**同一实例**重试 `_run`。
- **后果**:话中 WS 断连时 `_speaking=True` 残留,重连后下一句的首个 `2pass-online` 不发 START_OF_SPEECH(178 行分支被跳过),语音边界事件失配直到下一个 `2pass-offline` final。
- **缓解因素**:`FunASRStreamingSTT` 当前仅作 A/B 备用("当前未用,留作 A/B 对照"),运行栈不实例化它。
- **修复方向**:`_run` 开头 `self._speaking = False`,一行。

### #6 funasr-stream 手写握手 JSON 漏 `hotwords` 字段 🟡(既有不一致的忠实搬运)

- **位置**:`examples/voice_agents/providers/stt/funasr_stream.py:183-196`
- **事实**:`providers/config.py:52-68` 的 `funasr_init_payload()` 会注入 `hotwords`(来自 `FUNASR_HOTWORDS` 或 `DEFAULT_HOTWORDS`,如 `"停": 40`,专为提升短停止词召回);`funasr_2pass.py:123` 和 `funasr_offline.py:102` 都用它,funasr_stream 手写载荷无此字段。溯源 main 旧文件字节级一致,非回归。
- **影响**:`XIAOGE_STACK=optimized` 默认 `STT_BACKEND=funasr-stream`(`app/setup_taps.py:90-102`),即主力栈最终转写没有热词加权。缓解:打断走的并行 online_interrupt 通路(`setup_taps.py:496`、`online_interrupt.py:161-162`)自己传了 hotwords,停止词打断召回有部分覆盖。
- **修复方向**:改调 `funasr_init_payload(mode="2pass", wav_name="funasr-stream", sample_rate=_SAMPLE_RATE, chunk_size=[5, 8, 4])`,同时消除握手双源(后续 itn/chunk 调整只改一处)。

---

## 三、次级问题(PLAUSIBLE / 清理 / 规范)

### #7 CosyVoice `__init__` 丢弃旧关键字参数,与 shim 兼容承诺矛盾 🟡 PLAUSIBLE

- **位置**:`examples/voice_agents/providers/tts/cosyvoice.py:118-125`
- 旧签名(main `custom_audio_providers.py:1173-1183`)接受 `sample_rate/speech_rate/pitch_rate/instruction`;新版只留 `model/voice/api_key/opts`。而 shim(`custom_audio_providers.py:1-5`)承诺"本模块仅做 re-export,保证旧引用/文档示例不失效"。仓库内唯一调用点(`app/backends.py:77`)只传 model/voice 不受影响;功能经 `opts=CosyVoiceTTSOptions(...)` 仍可达。属对**仓库外旧引用**的潜在 TypeError 破坏。
- **修复方向**:要么在新 `__init__`(或 shim 层)补关键字转发到 opts,要么把 shim 的承诺文案收窄为"仅保证 import 路径"。

### #8 控制台 agent 与 web 栈的轮次策略/指标日志/预热三处逐字双份,且已分叉 🟡 清理

- **位置**:`examples/voice_agents/qwen_funasr_bailian_voice_agent.py:124-155, 213-238, 333-348` vs `examples/voice_agents/app/setup_taps.py:120-156, 248-261`、`web_ui_agent.py:228-261`
- **已核实的分叉**(不只是重复,行为已经不同):
  - (a) web 的停止词强打断有 `listen_interrupt_blocked()` 门(`web_ui_agent.py:233-234`),控制台无条件 `self.session.interrupt(force=True)`(控制台 134 行);
  - (b) overlap 状态:控制台用模块级 `_overlap_turn_state` dict(69 行),web 用 `runtime.overlap_turn_state`(`app/session_state.py:48-50`)。
- TURN_USER/TURN_ASSISTANT 格式是文档层契约(ARCHITECTURE.md:283/337 等),目前无代码解析(回归走 event_timeline/turn_metrics 结构化路径),紧迫性中等;但下次策略调整必漏改一边,两个入口静默运行不同打断行为。
- **修复方向**:抽 `common/turn_policy.py`(或复用 app/setup_taps 的函数,broadcast 等宿主差异注入/可选参数化);控制台文件已 import `app.backends`,机械改造。

### #9 `setup_taps.py` 503 行超自家 500 行硬上限,BASELINE.md 收官声明不实 🟡 规范

- `wc -l` 实测 `examples/voice_agents/app/setup_taps.py` = **503 行**,违反 `docs/project/CODE_GUIDELINES.md` §2"单文件行数 硬上限 ≤ 500"(review 把关项,ruff 无此检查,`make lint-ours` 绿不覆盖)。
- `docs/design/refactor/BASELINE.md` 声称"超 500 行文件 3 个 → **0**"、"最大自有文件 app/setup_taps.py ~490 行",与实测矛盾——要么文件在基线写完后又长了,要么当时就数错;需拆文件或修正声明,二者对齐。
- 另:零散超 100 行宽的行(`setup_taps.py:122,156`、`qwen_funasr_bailian_voice_agent.py:215,238` 的 f-string ~113 字符;`webpanel/state.py:38` 的 CSS 字符串 162 字符),因 pyproject 忽略 E501 而未被工具拦截,均可用隐式字符串拼接拆行。

### #10 CosyVoice 恢复只捕获 `WebSocketConnectionClosedException` 一种症状 🟡 PLAUSIBLE(弱化)

- **位置**:`examples/voice_agents/providers/tts/cosyvoice.py:283, 295`
- **先说明不算缺陷的部分**:"收到音频后不重放"(`received_audio` 门)是提交 e7ee249 写明的刻意取舍("已收到音频则维持原失败语义,避免重复播放"),且框架本身(`tts.py:514-516`)也拒绝部分音频后重试——此设计正确,不要求改。
- **实质残留**:两处 except 只认 `WebSocketConnectionClosedException`;`on_error` 回调路径(103-105 行)注入的 `APIStatusError` 及其他 socket 异常(broken pipe 等)**完全绕过恢复**——恰是该修复想盖住的场景的变体。
- **顺带(代价极小)**:288 行 `sent.append(text)` 在重放已永久不可能(`received_audio=True` 或 retry 已消耗)后仍整轮累积,可加条件;注意 `sent` 兼作 323 行 `if sent:` 的完成门,改动时保留该语义。
- **修复方向**:except 扩为一组"陈旧连接症状"(WS-close + on_error 注入的连接类错误);更深一层的做法是包一层池借出时的活性校验,但 dashscope 池是第三方 SDK,包装成本自行权衡。

---

## 四、已核实并排除的候选(避免实现者重复排查)

| 候选 | 排除理由 |
|------|----------|
| `app/setup_taps.py:213,215,261` prewarm 任务 fire-and-forget 被 GC | 1:1 搬自 main(旧 web_ui_agent.py:1816-1818,1863);运行中任务被 loop 就绪队列/executor future 链强引用,GC 风险为 RUF006 式理论问题;项目 lint 未启用 RUF,属接受的既有风格 |
| `app/web_audio.py:112` flush() 取消 playout 任务丢 on_playback_finished | 与 main 字节级一致的搬运;框架串行化(`agent_activity.py:2420-2423` 每段 await `wait_for_playout()`,打断路径 flush 先于 clear_buffer)使触发场景不可构造,该 cancel 分支实为死防御代码 |

## 五、未逐条验证的低危清理项(顺手可做,不阻塞合并)

1. **env_bool 内联重复**:`providers/stt/funasr_stream.py:118`(FUNASR_VERIFY_SSL,同文件已 import `env_float`)、`app/setup_taps.py:268`(AGENT_TIMELINE)仍手写 truthy 集合,应换 `common.config_utils.env_bool`。
2. **FunASR 默认 WS URL 字面量 4 处拷贝**:funasr_stream.py / funasr_offline.py:47 / funasr_2pass.py:54 / app/backends.py:46,应收敛为 `providers/config.py` 单一常量。
3. **`QwenStreamingTTS` 整体复制 `BailianRealtimeTTS`**(`providers/tts/qwen_stream.py:91`):__init__/属性/synthesize 逐字相同,应改继承,~40 行重复(注:先修 #4 再合并,避免把缺陷继承过去)。
4. **每流新建 `aiohttp.ClientSession`**:`funasr_stream.py:178`、`iflytek.py:108` 未走 `providers/helpers.acquire_http_session()`(其余后端都走了),每轮对话多一次 session+connector 建拆。
5. **webpanel 结构性复制**:`server.py:195` 的 `_handle_switch_asr/_handle_switch_tts` ~30 行仅差注册表/属性名(且 TTS 侧多持久化 `runtime.tts_backend_key` 已是不对称);`bridge.py:13` 三组 broadcast 死客户端清扫循环重复,可抽 `_broadcast(clients, sender)`。
6. **三个零调用方兼容 shim**:`custom_audio_providers.py`、`funasr_stream_stt.py`、`iflytek_stt.py` 仓库内已无 importer(两个 agent 本分支已直连 providers/*),若确认外部引用清零可删(同步更新 ARCHITECTURE.md 两处提及与 ourcode.txt);`providers/__init__.py` 里 `FunASRStreamingSTT`/`BailianRealtimeTTS` 的 re-export 无实例化方,建议显式标注 A/B-only。
7. **显示层净化/聆听模式门分散**(深度类,可立项不必本分支做):LEADING_PUNCT_RE 只在最终气泡应用、live 气泡走原文(同一句话浏览器先后显示不一致);聆听模式抑制在 transcription_node 与 _handle_stt_event 用了不同谓词(`ctrl.active` vs `active or tail_pending`),新增 producer 易漏判——建议收敛到 webpanel/bridge 或 ListeningController 单点。

---

*评审工具:Claude Code /code-review(high effort,8 查找角度 + 逐条独立验证);全程只读。*

---

# 设计者回应(2026-07-04,回复评审组)

> 已逐条**独立复核**(对照当前分支代码实测,非盲从;方法与新增证据见下)。结论:
> **评审整体成立,无可实质反驳**。#1/#2/#3 确认为本分支新引入缺陷,接受"合并前必修";
> #9 声明不实属实,一并列为合并前必改。**当前为评审期,本回应未改动任何工程代码**;
> 处置在评审定稿后按 §R3 分级实施,逐条对应提交。

## R1. 复核方法与新增证据

- **#1**:`web_ui_agent.py` 实测 `load_dotenv(override=True)` 在 **81 行**,而
  `app/`/`webpanel/`/`common/` 的 import 在 25~65 行——模块级 `os.getenv` 确在
  dotenv 之前执行。评审关于"三处位置/后果/`start.ps1` 掩盖"的描述全部准确。
- **#2**:实测 `cosyvoice.py` 271~273 行顺序为"先归还→再冷建(~0.8s 可取消窗口)→
  后翻 pooled"——取消/异常穿透到 `_run` 外层清理时对已归还对象二次
  `streaming_cancel()`+`return_synthesizer()`,成立。
- **#3**:用评审同款脚本独立复现:场景A(行+哨兵同批)`join` 2.004s、线程存活 ✔。
  **并补充一个比报告更糟的事实**:复现中场景B(哨兵单独在队)也卡死了——因为场景A
  泄漏的写线程仍阻塞在 `get()`,把 B 的哨兵**抢走**了。即一次哨兵被吞后,泄漏线程会
  持续窃取后续哨兵;好在生产只有单写线程+进程随即退出,实际危害即报告所述
  (每次退出固定 2s + 尾部日志丢失),但修复时须连带覆盖"哨兵被吞后再补发"的路径。
- **#9**:`wc -l` 实测 `app/setup_taps.py` = **503 行**,超硬上限属实。归因:BASELINE
  终值(~490)写于阶段5 摘 noqa 之前,其后为消 C901 追加的 `_handle_*` 提取又加了
  行数,**收官时未复测行数**——流程失误,认。
- **#4/#5/#6/#7 及 §五清理项 1/2/4**:逐条对照当前源码核实,与评审描述一致。

## R2. 逐条结论

| # | 复核结论 | 处置(定稿后) | 级别 |
|---|---|---|---|
| 1 | **接受,本分支回归**。补充诚实说明:47 次回放回归全部经 `start.ps1`(预导出 .env)启动,恰好落在掩盖路径上——"直接 `python web_ui_agent.py console` 启动"是回归盲区,将补进回归清单 | 修复取评审方向 2 为主:两个入口文件**最顶部**(先于一切自有包 import)执行 `load_dotenv`(以独立微模块或 `# noqa: E402` 头段实现,保证 ruff 下顺序不可漂移);对 `TURN_METRICS_LOG` 等确需模块级缓存的,评估改惰性读取。修后用**直接 python 启动**路径实测三组变量生效 | **P0 合并前** |
| 2 | **接受,本分支回归**(v1/v2 修复引入的次生缺陷) | 调整为"先摘旧(置 `state["pooled"]=False`,外层清理只 close 不归还)→冷建→建成后才对旧连接做一次真归还";若冷建被取消,旧连接仅被 close(幂等无害),宁可池少一枚可借计数,绝不双重归还。修后用触发录音(20260626_103029/143255)回放验证 | **P0 合并前** |
| 3 | **接受,本分支回归**(含 R1 补充的哨兵窃取) | `_drain_pending` 返回 saw_sentinel(或哨兵回插队头),写完批次即退出;`_flush_log_at_exit` 哨兵改阻塞 put(带短超时重试)。补单测:"行+哨兵同批"用例进 `tests/test_ours_runtime_log.py` | **P0 合并前** |
| 4 | **接受,既有缺陷**(与 Bailian 对照成立,溯源准确) | error/on_close 补 `audio_done.set()`(照抄 Bailian);drain 异常尽早浮出维持现结构、单独小改 | P1 合并前顺手(行级) |
| 5 | **接受,重构回归**(闭包→实例属性的搬运失误;类未接线故无线上影响) | `_run` 开头 `self._speaking = False`,一行 | P1 合并前顺手 |
| 6 | **接受方向,但定性补充**:改用 `funasr_init_payload` 会给 optimized 主栈**新增热词加权**,这是行为变更(改善,符合热词设计初衷),不属"零变更搬运"——须显式提交+回放 A/B(对比停止词召回与误识)佐证,不做静默修改 | 改调 `funasr_init_payload(...)` 消除握手双源;随修复跑基线录音 A/B | P2 合并后独立提交 |
| 7 | **接受(PLAUSIBLE 定性同意)**。背景:收窄签名是阶段2 为摘 `PLR0913` 的刻意取舍,当时已在提交说明中声明"API 收窄",但 shim 文案未同步收窄,自相矛盾是实 | 两案请评审组定调,设计者倾向 a:(a) `__init__` 增 `**legacy` 映射 `sample_rate/speech_rate/pitch_rate/instruction` → opts(不增名义参数计数,兼容承诺得守);(b) shim 文案收窄为"仅保证 import 路径与仓内调用" | P2(定调后) |
| 8 | **接受重复事实;分叉(a)定性商榷**:console 版**没有聆听模式**,`listen_interrupt_blocked()` 门属 web 栈功能差异而非漂移(console 的 `listen_ctrl` 恒 None,该门恒 False,两者此处行为实际等价);(b) 属状态载体差异,语义相同。但"双份必漂移"的风险判断完全成立 | 抽公共轮次过滤器(打断门作可注入参数,console 传 None),console 复用 `setup_taps` 的指标处理器;不阻塞合并 | P2 合并后独立 PR |
| 9 | **接受,声明不实认账**(见 R1) | 拆分 `setup_taps.py`(候选缝:在线打断三函数 ~100 行独立成 `app/online_interrupt_host.py`,或指标日志两函数并入既有模块)至 ≤450;**同步更正 BASELINE.md 数字并注明勘误**;>100 宽长行一并拆 | **P0 合并前**(硬上限是自家红线) |
| 10 | **接受(弱化定性同意)**:`received_audio` 门与"不重放部分音频"是设计取舍,评审已正确区分;except 只认单一异常类确是残留 | except 扩为保守的"连接类症状"集合(`WebSocketConnectionClosedException` + `ConnectionError`/`BrokenPipeError` 级),**不含** `APIStatusError` 语义类错误(服务端拒绝不应重放);`sent.append` 加条件时保留"完成门"语义 | P2 合并后独立提交 |

## R3. §五清理项与排除项

- 清理 1(env_bool 内联×2)、2(FunASR URL 字面量×4 收敛 `providers/config.py`)、
  5(webpanel 两处结构重复):**接受**,P2 一个清理提交。
- 清理 3(Qwen 继承 Bailian):**接受**,按评审提示排在 #4 之后。
- 清理 4(每流新建 ClientSession):**接受但注意**——二者是 main 上的既有风格(原
  `funasr_stream_stt.py`/`iflytek_stt.py` 即如此);改共享 session 时严禁在流关闭路径
  `close()` 共享 session(仅 owns=True 才关),按 `acquire_http_session` 语义改。
- 清理 6(删三个 shim):**建议缓删**——ARCHITECTURE.md/导读仍引用旧名,且 shim 成本
  3 行;倾向合并后连同文档扫尾一个 PR 删除;`providers/__init__` 对 A/B-only 类加注释,接受。
- 清理 7(显示净化/聆听谓词分散):**接受立项不本分支做**。补充:两处谓词差异
  (`ctrl.active` vs `active or tail_pending`)是**忠实搬运**自旧代码(分别 gate 助手
  气泡与用户 live 气泡,对象不同),收敛到单点时需先明确二者应否同窗。
- §四两条排除:**同意**,不再排查。

## R4. 实施与验收承诺(评审定稿后执行)

1. P0(#1/#2/#3/#9)+P1(#4/#5)在本分支追加提交,逐条引用评审编号;P2 各自独立提交/PR。
2. 验收:82+ 单测(含 #3 新增用例)与 `lint-ours` 全绿;**直接 python 启动**路径验证 #1;
   触发录音回放验证 #2;`.env` 三组变量注入实测;`wc -l` 全量复测并同步 BASELINE.md 勘误。
3. 本回应之外不做任何超出评审范围的改动。

> 设计者小结:评审 10 条中 9 条全盘接受、1 条(#8a)仅对"分叉"定性提出商榷但接受其
> 清理主张;#1 同时暴露了回归方法的盲区(单一启动路径),已纳入方法改进。感谢评审组
> 的脚本级复现——#3 的复现设计让我们额外发现了"泄漏线程窃取后续哨兵"这一更深一层的行为。

---

# 设计者回应·补充(2026-07-04,#8 处置方案更新,经产品负责人拍板)

**#8 的处置由"抽公共 turn_policy 层"改为"WEB_UI 开关 + console 薄壳"**,从根上消除双份
策略代码(原 R2 表中 #8 处置行以本节为准):

1. **`WEB_UI` 开关**(默认 `1`,Web 面板默认保留):`web_ui_agent.py` 的 `__main__` 在
   `WEB_UI=0` 时不起面板线程、不开浏览器;其余链路不变。技术依据:`webpanel/bridge.py`
   的 broadcast 系列在 web 循环未启动时本就静默 no-op,聆听横幅/气泡广播自然降级,
   需要 gate 的只有"起线程 + 开浏览器"。
2. **`qwen_funasr_bailian_voice_agent.py` 不删,改为 ~10 行薄启动壳**:
   `os.environ.setdefault("WEB_UI", "0")` 后 import `web_ui_agent` 的 `server` 并
   `cli.run_app`——入口文件名/启动命令保留,实现复用同一份(47 次回放验证过的)代码。
   依赖方向:**薄壳 → web_ui_agent 单向**;web_ui_agent 不感知薄壳。
3. **显式行为变更声明(仅 console 入口,web 主路径零变化)**:console 入口开场白由
   LLM 即兴改为固定文案、TTS 默认由 qwen 改为 cosyvoice(如需保留旧音色,薄壳内
   `os.environ.setdefault("TTS_BACKEND", "qwen")` 一行);旧 400 行实现由 git 历史留存。
4. 与 #1 修复的衔接:薄壳在 import web_ui_agent **之前**设 env,与"dotenv 先于自有包
   import"的修复顺序兼容(薄壳的 setdefault 不覆盖用户显式配置)。
5. 级别:仍为 **P2 合并后独立 PR**;实施时同步更新 ARCHITECTURE §13、CODE_GUIDELINES §1
   对该文件的职责描述("console 薄壳,等价于 WEB_UI=0 的 web_ui_agent")。

---

# 评审组复审意见(第二轮,2026-07-04,回复设计者)

> 对设计者回应逐条复核(对照当前分支代码实测,只读)。**总体:回应与代码事实相符,
> 处置分级(P0 #1/#2/#3/#9、P1 #4/#5、其余 P2)同意,评审可定稿放行**。以下仅列
> 需要在实施前定稿的方案细节——多数是"方向对、落笔时别踩坑"级别。

## S1. 对 R1 新增证据的确认

- **#3 哨兵窃取现象互证**:我方验证复现时同样观察到"泄漏线程窃取后续哨兵"(首轮
  复现记录原话:"the stuck thread also stole a later sentinel"),与 R1 场景 B 一致。
  该现象已列入修复覆盖面,好。
- **#1/#2/#9 复核**:与我方证据一致,无补充。

## S2. 对 R2 处置方案的意见

| # | 意见 | 细节 |
|---|------|------|
| 1 | ✅ 方案可行 | 追加一条:除"直接 python 启动实测"外,建议加一个**import 顺序守护测试**(单测里 monkeypatch 一个只存在于 .env 的变量,断言 import 后模块级常量取到它),防止未来有人在入口顶部段之上再插自有包 import 让问题静默回潮。 |
| 2 | ✅ 方向正确(先摘旧、外层只 close、绝不双归还;接受计数漂移的取舍合理) | 三点实现注意:(a) "建成后才对旧连接真归还"需把旧引用**另存局部变量**,归还后立即置 None——否则"建成→归还旧→重放 streaming_call 异常"的窗口会重蹈双触碰;(b) "归还陈旧连接、池会 renew"是 docstring 声明的假设,而回归#3 正源于 renew 有 ~0.7s 窗口——建议归还前先对旧连接 `close()`(幂等)再归还,或实测确认 `return_synthesizer` 同步触发 renew,避免死连接在窗口内被下一轮借走;(c) 验收除触发录音回放外,补"重建窗口内取消"的单测(假池即可),这是本缺陷的核心路径,录音回放不一定命中。 |
| 3 | ✅ 方案完整(含哨兵窃取路径) | 一个边界:阻塞 put"带短超时重试"仍可能失败(磁盘卡死、队列恒满),失败时应放弃并跳过 join(或 join(0)),别让 atexit 卡得比现在更久——修"固定卡 2s"别引入"最坏卡更久"。 |
| 4 | ✅ 照抄 Bailian,无异议 | — |
| 5 | ✅ 一行修复,无异议 | — |
| 6 | ✅ 同意其定性补充 | "改善也是行为变更、须显式提交+A/B"的立场比评审原文更严谨,采纳。 |
| 7 | 倾向 (a) 可以,但**反对用裸 `**legacy` 收集** | `**kwargs` 会把任意拼写错误的关键字静默吞掉(或迫使手写白名单校验,复杂度反超)。建议显式列出四个 legacy 关键字参数(default=None,非 None 时映射进 opts,可加 DeprecationWarning);若 PLR0913 参数计数是当初收窄的动机,单行 `# noqa: PLR0913` 加注释说明,比吞错别字便宜。若嫌啰嗦,(b) 收窄文案也完全可接受。 |
| 8a | ✅ 接受商榷 | console 无聆听模式、`listen_ctrl` 恒 None → 门恒 False,行为等价成立。撤回"分叉(a)"的回归定性,保留"双份必漂移"的清理主张(设计者亦未否认)。 |
| 9 | ✅ 拆分+勘误 | 附加建议:把 500 行上限**工具化**(make lint-ours 里一行 `awk 'END{...}'` 或 pytest 收集 ourcode.txt 逐个 wc),本次"收官未复测"正是人肉把关失效的实例。 |
| 10 | ✅ 保守集合合理 | 留意已知残余:若 dashscope 把连接类失败经 on_error 包成 `APIStatusError` 注入队列,仍绕过恢复——按方案这是刻意排除(语义类错误不重放),接受为**已知残余**并在注释里写明即可,不要求盖。 |

## S3. 对 #8 补充方案(WEB_UI 开关 + console 薄壳)的意见

方向可行,两个技术前提已代码核实成立:bridge 三组 broadcast 均有
`web_loop is None or not running` 早退(bridge.py:26-28,44-46,62-64)✔;面板线程
与浏览器启动确在 `__main__` 内(web_ui_agent.py:331-338),import 不触发 ✔。
但有三个点须在实施前定稿:

1. **WEB_UI 的消费点要先定义**。薄壳走的是"import server + 自己 run_app",**不经过**
   web_ui_agent 的 `__main__`——面板本就不会启动。若 WEB_UI 只在 `__main__` 消费,
   薄壳的 `setdefault("WEB_UI","0")` 实际是装饰性的;若打算让 session/setup 代码也
   消费它(比如条件跳过 web 专属 tap),则见下一条。请在设计里写明谁读这个开关。
2. **与 #1 修复的交互比 R2 表述的更微妙**。#1 会把 `load_dotenv(override=True)` 提到
   web_ui_agent import 最顶部;薄壳"先 setdefault 再 import"意味着:若用户 `.env` 里
   恰好写了 `WEB_UI=1`,override=True 会**覆盖薄壳设的 0**(dotenv 覆盖进程环境,
   方向与"setdefault 不覆盖用户显式配置"恰好相反)——console 入口会静默变成 web 模式。
   `TTS_BACKEND` 的 setdefault 同理。两个解法任选:(a) console 语义必须恒定 →
   WEB_UI 改为**运行期读取**,薄壳在 import 之后、run_app 之前硬赋值;(b) 允许 .env
   翻转 → 在 .env.example 和薄壳 docstring 里写明该行为。
3. **"等价于 WEB_UI=0 的 web_ui_agent"建议给一次回放佐证**。行为变更声明(开场白、
   TTS 默认)已列,但 console 独有路径(本地音频 I/O + KWS)换底后建议至少跑一次
   console 入口回放 A/B,替代"应该等价"的推断。

## S4. 对 R3/R4 的意见

- R3 全部同意;清理 4 的"仅 owns=True 才关共享 session"提醒正确且关键。
- R4 验收清单认可。补一条:P0/P1 修复合入后,请在本文件追加"修复落地记录"
  (提交号 ↔ 评审编号对照),便于合并评审时核销。

> 复审小结:设计者回应 10 条处置全部方向正确,无一条需要推翻;需收口的只有
> #2 的归还时序细节(S2-2)、#7 的 `**legacy` 反对(S2-7)、#8 薄壳与 dotenv
> override 的交互(S3-2)三处。**本轮复审同样未改动任何工程文件**(仅追加本节)。

---

# 设计者回应(二)(2026-07-04,回复复审;实施前方案定稿)

> 复审三处收口点(S2-2/S2-7/S3-2)全部接受并给出定稿方案;S3-1 的追问尤其有价值——
> 它直接把 #8 薄壳方案**简化**了(见 R2-8')。本轮仍未改动任何工程文件。

## R2'. 三处收口点的定稿

**S2-2(#2 归还时序)——三点全部采纳,落笔顺序定稿为:**
```
old, old_pooled = state["synth"], state["pooled"]   # (a) 旧引用另存局部
state["synth"], state["pooled"] = None哨兵*, False   # 外层清理自此不再触碰旧对象
fresh = 冷建(可取消窗口)                              # 取消→外层只处理哨兵/新态,旧连接由下行局部收尾
close(old)(幂等,suppress)                            # (b) 先关死再归还,不赌 renew 窗口
return_synthesizer(old) if old_pooled                # 真归还,仅此一次
state["synth"] = fresh; 重放
```
*哨兵实现细节(外层 `streaming_cancel(state["synth"])` 需 None 安全)在实施时定,
原则不变:**旧对象的 cancel/close/归还全部收敛到恢复函数内的局部引用,外层清理只见新态**。
(c) 补"重建窗口内取消"单测:用 `__new__` 构造流对象 + Mock `_tts`(慢 `_build_synth` +
计数 `_release_synth`),取消后断言旧连接恰好被处理一次——不依赖真池与录音回放。
另按 S2-2b 备注:即便归还后陈旧连接在 renew 窗口内被下一轮借走,下一轮的同款恢复
路径也会兜住(降级冷建),close-before-return 属双保险而非唯一防线。

**S2-7(#7 兼容参数)——接受"反对裸 `**legacy`",定稿取显式方案:**
`__init__` 显式列出 `sample_rate/speech_rate/pitch_rate/instruction` 四个 legacy 关键字
(default None,非 None 映射进 opts + `DeprecationWarning`),`def` 行加
`# noqa: PLR0913` 并按 CODE_GUIDELINES §5 棘轮规则**登记台账**(理由:shim 已对外
承诺兼容,守约优先;一条有台账、有注释、有弃用告警的豁免,好过静默吞错别字或毁约)。
"noqa 清零"表述在 BASELINE 勘误时同步改为"复杂度豁免 0 → 1(兼容性豁免,已登记)"。
如产品侧更看重清零指标,可退回 (b) 收窄 shim 文案——默认按上述 (a-显式) 实施。

**S3-1/S3-2(#8 薄壳与 dotenv 交互)——接受追问,方案随之简化:**
- **S3-1 答复:`WEB_UI` 的唯一消费点 = `web_ui_agent.py` 的 `__main__`(运行期读取,
  彼时 dotenv 早已加载,无 #1 类时序问题)**。由此推论:薄壳经自己的 `__main__` 调
  `cli.run_app`,根本不执行 web_ui_agent 的 `__main__`,面板天然不启动——
  **薄壳不需要也不再设置 `WEB_UI`**,原方案中该 setdefault 确属装饰性,删除。
- **S3-2 答复:薄壳定稿为"零 env 操作"**——不 setdefault `TTS_BACKEND`,console 入口
  默认与 web 版完全一致(cosyvoice);想要旧音色的用户在 `.env` 写 `TTS_BACKEND=qwen`
  (对两个入口同时生效,语义直白)。这同时消除了"dotenv override 反噬 setdefault"的
  整个交互面:薄壳里没有任何会被 .env 覆盖的东西。行为变更声明相应更新:
  console 入口的开场白(即兴→固定)与默认音色(qwen→cosyvoice)均为显式变更,
  写入提交说明与 `.env.example`。
- **S3-3 采纳**:实施后用 console 入口跑一次基线录音回放,与 web 入口回放对照
  (KPI + 事件序列),以实证替代"应该等价"。

## R3'. 其余复审意见

- **S2-1(#1 import 顺序守护测试)**:采纳。用 subprocess 形式(临时 cwd + 仅存在于
  .env 的哨兵变量,断言 import 后 `webpanel.state`/`common.runtime` 模块级常量取到它),
  防回潮进 `tests/test_ours_*`。
- **S2-3(#3 atexit 最坏耗时)**:采纳。阻塞 put 带上限(~0.5s)重试,失败即放弃且
  不再 join(记一条 stderr 提示),保证修复后的最坏退出耗时严格小于现状 2s。
- **S2-9(500 行上限工具化)**:采纳。`make lint-ours` 增行数检查(读 ourcode.txt 逐个
  计数,>500 报错、>400 警告),把本次人肉失效的 review 项变成门禁;涉及 makefile,
  与 P0-#9 拆分同一提交落地。
- **S2-10(已知残余)**:同意,按"刻意排除"在 except 处注释写明 `APIStatusError`
  语义类错误不入恢复路径。
- **S1 / S2-4 / S2-5 / S2-6 / S2-8a / S4**:确认无分歧;P0/P1 合入后在本文件追加
  "修复落地记录"(提交号 ↔ 评审编号对照)供合并评审核销。

> 设计者小结(二):复审无一条需要抗辩;S3-1 的追问使 #8 薄壳从"三行 env 胶水"
> 缩为"零配置纯入口",方案因评审而变简单——这是这轮往返里最有价值的一笔。
> 待评审组对 R2' 三处定稿(尤其 #7 的 a/b 取向)无异议,即按 R4 顺序实施。

---

# 评审组复审意见(第三轮,2026-07-04,对 R2' 定稿的裁定)

> 三处定稿中 **#7、#8 无异议,即为终稿**;**#2 的定稿伪代码有一处自相矛盾,须改一行
> 顺序后方可作为终稿**(改法见 T1,不影响该项 P0 定级与整体放行)。除此之外全部
> 通过,修复可按 R4 顺序实施。本轮仍未改动任何工程文件(仅追加本节)。

## T1. #2 定稿伪代码的矛盾:取消发生在冷建窗口时,"下行局部收尾"不会执行

R2' 伪代码的注释声称"取消→外层只处理哨兵/新态,**旧连接由下行局部收尾**",但按所写
顺序,`close(old)` 与 `return_synthesizer(old)` 位于 `fresh = 冷建(可取消窗口)` **之后**
——CancelledError 在冷建处抛出时直接穿透出恢复函数,下行两行根本不会执行:旧连接
既不 close 也不归还,借出计数 +1 漂移、socket 悬挂到 GC。这与注释的声明冲突(虽然
"宁可少一枚计数"的取舍框架下尚可容忍,但既然写了"局部收尾"就应真的收尾)。

**修正(二选一,推荐前者):**

1. **把 close(old) + return(old) 挪到冷建之前**:
   ```
   old, old_pooled = state["synth"], state["pooled"]
   state["synth"], state["pooled"] = None, False   # 外层自此只见新态
   close(old)(幂等,suppress)                        # 旧连接已知陈旧,无须留到建成后
   return_synthesizer(old) if old_pooled            # 真归还,仅此一次
   fresh = 冷建(可取消窗口)                          # 此窗口内已无任何旧对象责任
   state["synth"] = fresh; 重放
   ```
   第一轮"建成后才真归还"的动机是防双重归还,而双重归还的真正解药是**先从 state 摘除**
   (第 2 行)——摘除之后,归还发生在冷建前还是后与双归还无关;提前归还反而让可取消
   窗口内不再持有任何旧对象责任,取消路径零收尾,比 try/finally 更简单。close 先于
   return 的双保险语义不变。
2. 若坚持建成后归还,则 close(old)/return(old) 必须放进 `try/finally`(finally 里对
   old 判非 None 后收尾),接受多一层嵌套。

(c) 单测方案(Mock `_tts` + 计数 `_release_synth`,断言旧连接恰好处理一次)很好,
按修正后顺序,断言应细化为:**取消发生在冷建窗口时,old 也已被 close+归还各一次**
——这恰好把本节矛盾锁进测试。

## T2. #7 定稿裁定:按 (a-显式) 通过

显式四参数 + DeprecationWarning + `# noqa: PLR0913` 登记台账 + BASELINE 勘误同步改
"豁免 0 → 1(兼容性,已登记)"——评审组**定调采纳 (a)**,理由同设计者:守约优先,
有台账的豁免优于毁约或吞错。一个实施细节请在 docstring 写明:**legacy 关键字与
`opts` 同时显式传入时的优先级**(建议:legacy 非 None 时覆盖 opts 对应字段并告警;
或直接 `ValueError` 禁止混用——二选一,写清即可,别留静默合并的歧义)。

## T3. #8 定稿裁定:通过,零 env 薄壳优于原案

- S3-1 的答复(WEB_UI 唯一消费点 = `__main__`、运行期读取)成立,且由此把薄壳缩为
  零配置纯入口、整个消除 dotenv override 交互面——正确的简化,通过。
- console 默认音色 qwen→cosyvoice、开场白即兴→固定,作为**显式声明的行为变更**
  (提交说明 + `.env.example`)接受;S3-3 的 console 回放 A/B 采纳,验收凭据齐。

## T4. R3' 各条:全部通过

subprocess 形式的 import 顺序守护测试(S2-1)、atexit 最坏耗时严格 < 2s 的上限设计
(S2-3)、行数门禁进 `make lint-ours` 并与 #9 拆分同一提交(S2-9)、`APIStatusError`
刻意排除写注释(S2-10)——均无异议。

> 第三轮小结:**除 T1 一处顺序修正外,全部定稿通过**。T1 属方案落笔层面的修正,
> 不改变 #2 的 P0 定级、不新增工作量(反而少一层 try/finally)。设计者确认 T1 修正
> 后即可按 R4 顺序实施,无须再走一轮评审;实施后按 R4/S4 约定在本文件追加
> "修复落地记录"供合并评审核销。

---

# 设计者回应(三)(2026-07-04,确认 T1 修正;设计往返定稿)

## T1 确认:评审指出的矛盾属实,采纳修正 1

独立复核我方 R2' 伪代码:`CancelledError` 在 `fresh = 冷建(...)`(`asyncio.to_thread`
的 await 点)抛出时,恢复函数立即出栈,其后的 `close(old)`/`return_synthesizer(old)`
确实不会执行——注释"旧连接由下行局部收尾"与所写顺序自相矛盾,评审无误,认。

**采纳修正 1(close+归还挪到冷建之前)**,并同意其推理:防双重归还的真正解药是
"先从 state 摘除",摘除之后归还时机与双归还无关;提前收尾使可取消窗口内**零旧对象
责任**,取消路径无需任何 finally——比我方原稿更简单也更正确。终稿顺序:

```
old, old_pooled = state["synth"], state["pooled"]
state["synth"], state["pooled"] = None, False   # 外层清理自此只见新态(None 安全)
close(old)(幂等,suppress)                        # 陈旧连接无须留到建成后
return_synthesizer(old) if old_pooled            # 真归还,仅此一次
fresh = 冷建(可取消窗口,窗口内无旧对象责任)
state["synth"] = fresh; 重放
```

(c) 单测断言按 T1 细化采纳:**在冷建窗口注入取消,断言 old 已被 close+归还各恰好
一次、且 state 为安全态**——把本矛盾锁进测试,防止实现时回退到旧顺序。

## T2 确认:#7 按 (a-显式) 实施;混用策略定为"禁止"

docstring 将写明:**legacy 关键字与 `opts` 同时显式传入 → `ValueError`(禁止混用)**。
理由:`opts` 是新 API,legacy 参数只为不知道 `opts` 存在的旧调用方而设,二者并存
必属调用方混乱;显式报错比"覆盖+告警"少一种需要记忆的合并规则,无静默歧义。

## T3/T4:确认无分歧

#8 零 env 薄壳、console 行为变更声明与回放 A/B、以及 R3' 各条(import 顺序守护测试 /
atexit 上限 / 行数门禁 / 已知残余注释)均按裁定执行,无补充。

## 定稿声明

至此三轮评审-回应闭环结束,**全部 10 项 + 补充方案定稿**,无遗留争议。实施顺序与
验收按 R4(P0 #1/#2/#3/#9 → P1 #4/#5 → P2 各独立提交;82+ 单测/lint-ours/直接
python 启动实测/触发录音回放/console A/B);每项落地后在本文件"修复落地记录"
登记提交号 ↔ 评审编号供合并评审核销。**本轮回应仍未改动任何工程文件**;
收到实施指令后开工。
