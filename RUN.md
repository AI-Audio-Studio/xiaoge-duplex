# Xiaoge Duplex Speech — 本地运行说明 / Local Quickstart (Windows)

**小歌（Xiaoge Duplex Speech）**：基于 LiveKit 的全双工中文语音交互引擎——**Qwen LLM**
（OpenAI 兼容网关）+ **FunASR / Qwen3 流式 STT** + **百炼(DashScope) TTS** + 可选
**sherpa-onnx KWS 强打断**。入口为 `examples/voice_agents/web_ui_agent.py`
（console 模式 + 浏览器测试面板）。

## 一次性构建环境

```powershell
.\setup.ps1
```

`setup.ps1` 会：
1. 删除旧的 `.venv`，用 Python 3.13 新建虚拟环境；
2. 以**可编辑(editable)**方式安装 MVP 真正用到的包：
   `livekit-agents` + 插件 `openai / silero / turn-detector` + `dashscope`；
3. 下载 turn-detector 判停模型（一次性，需联网；之后离线运行）。

> 只装 MVP 需要的依赖，不会同步整个 monorepo 的 60+ 插件。

## 启动 / 关闭

**双击运行（最简单）：**

- `start_agent.cmd` — 双击开启 Agent
- `stop_agent.cmd` — 双击关闭 Agent

（这两个 `.cmd` 只是壳，内部以 `-ExecutionPolicy Bypass` 调用下面的 PowerShell 脚本，
双击即可，无需先开终端。窗口会停在 “Press any key…” 让你看到启动/关闭结果。）

**命令行运行（可带参数）：**

```powershell
.\start.ps1          # 语音模式（麦克风），后台运行，日志写入 .run\web_ui_agent.log
.\start.ps1 -Text    # 文本模式，新开窗口手动输入
.\start.ps1 -Port 8770   # 指定 Web 面板端口
.\stop.ps1           # 关闭 Agent（连同子进程一起结束）
```

- 启动后浏览器测试面板地址会打印在控制台，默认 `http://localhost:8787`
  （由 `.env` 的 `WEB_UI_PORT` 配置，刻意避开 sibling 项目 `duplexMVP2` 占用的 8765）；
  若 8787 也被占用，会自动顺延到 8788…
- 进程号记录在 `.run\web_ui_agent.pid`，`stop.ps1` 据此关闭。
- `stop.ps1` 只会关闭**本项目**的 agent，不会误杀其他项目的进程。

## 配置管理

本工程用 **`.env`（仓库根目录）作为唯一的配置文件**，没有、也不需要单独的 `config/`
目录：

- 运行时由 `python-dotenv`（agent 内 `load_dotenv`）+ `start.ps1` 自动加载并注入进程环境；
- 配置都是扁平的 key=value，用 `.env` 这种 12-factor 风格最合适；单应用 MVP 再加一层
  `config/` 目录（YAML/loader）只会和 `.env` 重复、徒增间接层；
- 每个 `os.getenv("X", 默认值)` 在代码里都带**内置默认**，所以 `.env` 缺项也能跑；
- [.env.example](.env.example) 是**配置目录/清单**：列全了所有可用变量及说明（`.env` 本身被
  gitignore，所以用样例文件充当“配置 schema”）。首次用 `cp .env.example .env` 再改。

> `models/` 放的是**数据/模型文件**（如 KWS 模型），属于资产不是配置，单独于 `.env` 管理。

关键变量见 [.env.example](.env.example)；常用：

| 变量 | 说明 |
| --- | --- |
| `QWEN_BASE_URL` / `QWEN_API_KEY` / `QWEN_MODEL` | Qwen LLM 网关 |
| `STT_BACKEND` / `FUNASR_WS_URL` / `QWEN3_ASR_STREAM_WS_URL` | 语音识别后端（可运行时切换） |
| `DASHSCOPE_API_KEY` / `BAILIAN_TTS_MODEL` / `BAILIAN_TTS_VOICE` | 百炼 TTS |
| `XIAOGE_KWS_*` | 本地关键词强打断（见下） |
| `WEB_UI_PORT` / `LIVEKIT_LOG_LEVEL` | Web 面板端口 / 日志级别 |

## 关键词强打断 KWS（默认开启）

基于 sherpa-onnx 的本地关键词检测，说“停 / 别说了 / 等等”等可强行打断播报。

- 模型随工程放在 `models/kws/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20/`；
- `XIAOGE_KWS_ENABLE_NATIVE` 默认 **1**，`XIAOGE_KWS_MODEL_DIR` 默认指向上面这个目录
  （代码按仓库路径自动定位，**无需任何环境变量**）；
- 依赖 `sherpa-onnx` + `pypinyin`（`setup.ps1` 已包含）；缺模型/依赖时**自动降级**为
  no-op，不影响启动；
- `models/` 已 gitignore（39MB 二进制）。换机器时需手动把该模型目录拷到 `models/kws/` 下。

## 生成物（已加入 .gitignore）

- `.venv/`、`.run/`：环境与运行时产物
- `models/`：模型文件（KWS 等，本地拷入）
- `recordings/`：每次会话录音（`conversation.wav` 等）
- `logs/`、`qwen_voice_turn_metrics.log`：调试日志与逐轮时延指标
