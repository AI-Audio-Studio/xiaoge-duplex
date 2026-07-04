# 重构基线（阶段 0 采集）

> **终值(阶段 5 收尾,2026-07-03)**:12 处 noqa 挂账 → **0**;超 500 行文件 3 个 → **0**;
> lint-ours **无复杂度豁免全绿**;82+ 条行为锁定单测全绿;CI 各步(宽松 lint/格式/编译)全绿。
> 回放回归与资源实测已完成(见 §3/§4 与 REGRESSION_LOG.md)。
>
> **勘误(2026-07-04,评审#9)**:上稿曾写"最大自有文件 setup_taps ~490 行"——该数字
> 采于阶段5 摘 noqa 的函数提取**之前**,收官时未复测;评审实测 503 行,超 500 硬上限。
> 已拆分(在线打断三函数 → `app/online_interrupt_host.py`,setup_taps 现 ~417 行),
> 并把行数上限**工具化**为 `make lint-ours` 门禁(`scripts/check_line_counts.py`,
> >500 报错/>400 警告),此后不再依赖人肉复测。

> 目的：为"功能不能变、性能要提升"提供前后对照。方案见重构计划（PR 描述）；
> 采集日期 2026-07-03，代码基线 = `main`@`85a25dd`（分支 `refactor/phase0-guardrails`）。

## 1. 规范基线（lint / 体量）

- `ruff check --config ruff-ours.toml $(cat ourcode.txt)`：**绿**（依赖 12 处函数级 `# noqa` 挂账）。
- 挂账清单（重构目标：全部摘除）：

| 文件:行 | 函数 | 规则 |
|---|---|---|
| web_ui_agent.py:1462 | `on_user_turn_completed` | C901, PLR0912, PLR0915 |
| web_ui_agent.py:1681 | `entrypoint` | C901, PLR0912, PLR0915 |
| kws_interrupt.py:263 | `_unavailable_reason` | PLR0911 |
| custom_audio_providers.py:222 | `_recognize_once` | C901 |
| custom_audio_providers.py:591 | `_FunASRStream._run` | C901 |
| custom_audio_providers.py:633 | `recv_task` | C901 |
| custom_audio_providers.py:1017 | `_QwenSynthesizeStream._run` | C901 |
| custom_audio_providers.py:1173 | `CosyVoiceStreamingTTS.__init__` | PLR0913 |
| custom_audio_providers.py:1321 | `_CosyVoiceSynthesizeStream._run` | C901 |
| turn_metrics.py:267 | `attach` | C901 |
| probe_funasr_2pass.py:64 | `main` | C901, PLR0912, PLR0915 |
| qwen_funasr_bailian_voice_agent.py:373 | `entrypoint` | C901, PLR0915 |

- 超 500 行硬上限的文件（重构目标：全部 ≤500，软目标 ≤400）：
  - `web_ui_agent.py` 2105 行
  - `custom_audio_providers.py` 1519 行
  - `qwen_funasr_bailian_voice_agent.py` 618 行

## 2. 行为基线（单测锁定）

新增行为锁定单测（**先于任何重构**按当前行为编写，重构后必须同绿）：

| 文件 | 覆盖 |
|---|---|
| `tests/test_ours_text_rules.py` | 停止词/附和/overlap-ack 判定、数字归一化、`_ms` 格式 |
| `tests/test_ours_text_sanitizer.py` | `strip_markdown` / `sanitize_stream`（句界冲刷、跨块标记） |
| `tests/test_ours_turn_config.py` | `TurnConfig` 默认值 / env 覆盖 / 坏值回退 / `turn_handling` 形状 |
| `tests/test_ours_listening_mode.py` | 聆听状态机：进/出、自动进入连击、尾巴切分、整理回答、临时缓冲 |

运行：`.venv\Scripts\python -m pytest tests\test_ours_*.py`（63 passed @ 基线）。

> TTS 分句边界（`。！？!?；;\n`）当前内嵌在 3 个 SynthesizeStream 的 `_run` 循环里，
> 无法在不重构的情况下单测；阶段 2 抽出 `_split_sentences()` 时补测（边界集合逐字符一致）。

## 3. 端到端行为基线（场景回放）

参照物：`runs/20260630_093520/`（在同一代码基线上产生）

> 阶段 0 采集时刻实测:FunASR(60.205.197.165:10090)**不可达**、LLM(10092)可达,
> 故未跑新回放;以上述既有 run 为基准。各阶段的回放回归需在后端可达时执行。
- 注入源：`runs/20260630_093520/user.wav`（约 5 分钟真实对话）
- KPI：`turn_kpis.json`；事件序列：`timeline.jsonl`

每阶段重构后跑：
```powershell
$env:AGENT_TIMELINE="1"; $env:AGENT_SCENARIO="runs\20260630_093520\user.wav"; .\start.ps1
# 回放结束后 stop_agent.cmd,对比新 runs/<ts>/turn_kpis.json 与基线
```
对比口径：turn 数、felt latency、coverage（LCS 召回）、interrupt 计数不劣化；
timeline 关键事件（STT final / STOP_* / 状态迁移）序列一致。

（注：回放经真实远端 STT/LLM/TTS，LLM 回复文本天然有随机性;对比看 KPI 与事件结构,不逐字比回复。）

## 4. 资源基线（RESOURCE_REPORT 口径）

沿用 `docs/reports/RESOURCE_REPORT.md`（2026-06 实测,同代硬件同方法）：

| 状态 | CPU(单核%) | RAM |
|---|---|---|
| ACTIVE(KWS 开) | 326%(峰336) | 400MB |
| IDLE(KWS 开) | 321%(峰324) | 360MB |
| IDLE(KWS 关) | 12.5% | 290MB |

阶段 4 优化后实测(2026-07-03,进程树采样,详见 REGRESSION_LOG.md §5):

| 状态 | CPU(工作进程,单核口径) | RAM(进程树) |
|---|---|---|
| IDLE(KWS 开,threads=2 默认) | 395% | 1162MB |
| IDLE(KWS 开,**threads=1**) | **98.6%(降 75%)** | 1172MB |

KWS 命中延迟守门:threads=1 与 2 命中同一停止词时刻 126.29s vs 126.64s,不回退。
`.env.example` 已推荐 `XIAOGE_KWS_NUM_THREADS=1`(代码默认仍 2)。
回放回归(46 条历史录音、47 次运行)全部通过,详见 REGRESSION_LOG.md。

附加硬指标：`FELT_LATENCY`/`wall_clock_e2e` 不回退；`STOP_KWS_EARLY` 命中延迟不回退。

### 阶段 4 已落地的改动（默认行为不变，待实测）

1. **指标日志写盘下线程**（`common/runtime.py`）：原每条 STT final 在 agent 事件循环上
   同步 open/write；现改为调用线程打时间戳 + 队列 + 后台 daemon 线程批量写。
   队列满(磁盘卡死)丢日志不丢事件循环;atexit 排空。
2. **KWS 线程数旋钮**：`XIAOGE_KWS_NUM_THREADS`（默认 2 = 原行为）。IDLE ~3.2 核的
   主嫌疑是 sherpa/onnxruntime 解码线程;设 1 复测 CPU 与 `STOP_KWS_EARLY` 延迟。
3. 核实后**不需要改**的项：`audio_recorder`/`test_recorder` 的重采样与 numpy 转换
   本就在锁外(此前分析报告有误);iflytek bytearray 切片在 40ms 节流循环内非热点,不动。

**实测步骤（后端可达时执行）**
```powershell
# A/B:默认 vs $env:XIAOGE_KWS_NUM_THREADS="1",各采 IDLE/ACTIVE(RESOURCE_REPORT §6 方法)
# 回放:AGENT_TIMELINE=1 + AGENT_SCENARIO 同基线录音,对比 KPI 与 STOP_KWS_EARLY 时延
```
若 num_threads=1 使 KWS 命中延迟实测回退,保持默认 2(旋钮保留)。
