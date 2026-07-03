# 重构基线（阶段 0 采集）

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

阶段 4 优化后按 §6 同方法复测填入：

| 状态 | CPU(优化后) | RAM(优化后) |
|---|---|---|
| ACTIVE(KWS 开) | 待测 | 待测 |
| IDLE(KWS 开) | 待测 | 待测 |

附加硬指标：`FELT_LATENCY`/`wall_clock_e2e` 不回退；`STOP_KWS_EARLY` 命中延迟不回退。
