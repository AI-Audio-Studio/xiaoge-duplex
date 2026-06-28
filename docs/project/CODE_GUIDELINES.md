# 代码规范(本项目自有代码)

> 目标:文件不过大、函数不过长、模块解耦。**仅约束我们自己写的代码**,**不适用于 livekit 母体工程**(`livekit-agents/`、`livekit-plugins/`)与上游示例文件。
> 数字是"闻味器"不是 KPI——**内聚 > 行数**;超标先问"是不是职责多了",而不是"怎么把行数压下去"。

## 1. 适用范围 / 约束索引

- **约束**:本项目为这个全双工语音引擎新写的应用与工具代码。
- **不约束**:`livekit-agents/`、`livekit-plugins/`(开源母体,不改不约束)、`examples/voice_agents/` 下的**上游示例**(`basic_agent.py`、`multi_agent.py`、`weather_agent.py` 等)。

> 母体与我们的代码在 `examples/voice_agents/` 下**交织**,且整库由同一账号导入(git 作者无法区分),所以范围只能用**显式清单**界定。

**权威清单 = `makefile` 的 `OUR_CODE` 变量**(下面镜像一份便于阅读,改动以 makefile 为准):

```
examples/voice_agents/
  web_ui_agent.py            主应用(入口)
  listening_mode.py          聆听模式状态机
  mute_gate.py               真关麦门
  text_sanitizer.py          LLM 文本净化
  live_transcript.py         Web 实时转写气泡
  turn_config.py             判停参数集中
  kws_interrupt.py           KWS 强打断
  online_interrupt.py        在线 2pass 抢断
  funasr_stream_stt.py       FunASR 流式主 STT
  iflytek_stt.py             讯飞 RTASR
  custom_audio_providers.py  STT/TTS 适配器
  audio_recorder.py          正常模式录音
  test_recorder.py           测试多轨录音
  event_timeline.py          测试时间线
  turn_metrics.py            判停 KPI
  scripted_audio.py          录音回放注入
  probe_funasr_2pass.py      FunASR 探针
```

**新增自有文件时**:把它加进 `makefile` 的 `OUR_CODE`,即自动纳入约束。

## 2. 量化标准(软目标 / 硬上限)

| 维度 | 软目标 | 硬上限 | 通行度 / 出处 |
|---|---|---|---|
| 单文件行数 | ≤ 400 | ≤ 500 | 工程约定(无统一标准,Python 生态偏小);本仓 review 把关 |
| 单函数/方法行数 | ≤ 40(一屏) | ≤ 75 | 约定;`PLR0915` 语句数 ≤ 50 近似(pylint 默认) |
| 圈复杂度(分支) | ≤ 8 | ≤ 10 | **强共识**:McCabe(1976)+ 几乎所有 linter 默认 10(`C901`) |
| 函数参数 | ≤ 4 | ≤ 5 | pylint 默认 `max-args=5`(`PLR0913`) |
| 嵌套层级 | ≤ 3 | ≤ 4 | 约定;用早返回(卫语句)压平 |
| 类 | ≤ 200 行 / ≤ 12 公有方法 | ≤ 300 行 | 经验值,SRP 为纲 |
| 行宽 | — | 100 | 本仓 ruff 既有(`pyproject.toml`) |

> 注:**复杂度/参数有学术与工具默认背书**;**文件/函数行数是弱指标**(实证上对缺陷预测力弱),与复杂度一起看。

## 3. 解耦原则(比行数更重要)
1. **单一职责**:一个模块只回答一件事。样板:`mute_gate.py`(33 行,只管关麦)、`text_sanitizer.py`(只管净化)、`turn_config.py`(只管判停参数)。
2. **纯逻辑与 I/O 分离**(本仓默认范式):`listening_mode.py` 是纯同步状态机(无 asyncio/IO),host(`web_ui_agent.py`)负责喂事件与 I/O ⇒ core 可单测、host 薄接线。
3. **依赖单向、无环**:同层模块只用对方**显式公共 API**(私有加 `_` 前缀),不互相 import 内部细节。
4. **配置集中**:走 dataclass + `from_env()`(如 `TurnConfig`/`ListeningController`),不散落 `os.getenv`。

## 4. 如何强制
- **命令**:`make lint-ours` —— 用 `ruff-ours.toml` 对 `OUR_CODE` 跑额外规则。
- **隔离**:`make check`(全仓 `ruff check .`,沿用 `pyproject.toml` 宽松规则)**保持不变**,母体 `livekit-*` 照常通过;加严规则只在 `make lint-ours` 里、只作用于 `OUR_CODE`。
- **规则映射**(`ruff-ours.toml`):`C901`(复杂度≤10)、`PLR0913`(参数≤5)、`PLR0915`(语句≤50)、`PLR0912`(分支≤12)、`PLR0911`(return≤6)。
- 建议把 `make lint-ours` 纳入提交前/CI 检查。

## 5. 历史挂账(重构待办)
为让门禁现在就绿、只拦**新增**违规,以下既有文件暂在 `ruff-ours.toml` 的 `per-file-ignores` 挂账;重构后逐条删除即收紧:

| 文件 | 挂账规则 | 优先级 |
|---|---|---|
| `web_ui_agent.py` | C901, PLR0915, PLR0912 | **高**(2100+ 行"上帝文件",建议按职责拆:Web 面板/事件钩子/STT-TTS 构建/tap 装配) |
| `custom_audio_providers.py` | C901, PLR0913 | 中(多 Provider,可拆文件) |
| `probe_funasr_2pass.py` | C901, PLR0912, PLR0915 | 低(探针工具) |
| `kws_interrupt.py` | PLR0911 | 低(单函数 return 偏多) |
| `turn_metrics.py` | C901 | 低 |

**收紧流程(ratchet)**:重构某文件达标后,从 `ruff-ours.toml` 删掉对应挂账行 → `make lint-ours` 继续绿,且该文件此后不允许回退。

## 6. 一条总纲
**内聚 > 数字。** 为凑行数把一段高内聚逻辑硬切成多个只调用一次的碎函数,比一个 60 行的清晰函数更糟。阈值用来"触发一次审视",不是用来"达标交差"。
