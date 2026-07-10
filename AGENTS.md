# AGENTS.md

> 文档总索引见 [docs/README.md](docs/README.md);架构总览见 [docs/guide/ARCHITECTURE.md](docs/guide/ARCHITECTURE.md);运行见 [docs/guide/RUN.md](docs/guide/RUN.md)。

## Build and Development Commands

This project uses **uv** as the package manager. All commands run from the repository root.

### Installation
```bash
make install          # Install all dependencies with dev extras (uv sync --all-extras --dev)
```

### Code Quality
```bash
make format           # Format code with ruff
make lint             # Run ruff linter
make lint-fix         # Run ruff linter and auto-fix issues
make type-check       # Run mypy type checker (strict mode)
make check            # Run all checks (format-check, lint, type-check)
```

### Testing
```bash
uv run pytest                           # Run all tests
uv run pytest tests/test_tools.py       # Run a single test file
uv run pytest tests/test_tools.py -k "test_name"  # Run specific test
cd tests && make unit-tests             # Run unit tests that doesn't require cloud accounts
```

### Running Agents
```bash
python myagent.py console   # Terminal mode with local audio I/O (no server needed)
python myagent.py dev       # Development mode with hot reload (connects to LiveKit)
python myagent.py start     # Production mode
python myagent.py connect --room <room> --identity <id>  # Connect to existing room
```

### Linking Local python-rtc (for SDK development)
```bash
make link-rtc         # Link to local python-rtc with downloaded FFI artifacts
make link-rtc-local   # Build and link local rust SDK from source (requires cargo)
make unlink-rtc       # Restore PyPI version
make status           # Show current linking status
make doctor           # Check development environment health
```

## Architecture Overview

### Core Concepts
- **AgentServer** (formerly known as **Worker**) (`worker.py`): Main process coordinating job scheduling, launches agents for user sessions
- **JobContext** (`job.py`): Context provided to entrypoint functions for connecting to LiveKit rooms
- **Agent** (`voice/agent.py`): LLM-based application with instructions, tools, and model integrations
- **AgentSession** (`voice/agent_session.py`): Container managing interactions between agents and end users

### Key Directories
```
livekit-agents/livekit/agents/
├── voice/              # Core voice agent: AgentSession, Agent, room I/O, transcription
├── llm/                # LLM integration: chat context, tool definitions, MCP support
├── stt/                # Speech-to-text with fallback and stream adapters
├── tts/                # Text-to-speech with fallback and stream pacing
├── ipc/                # Inter-process communication for distributed job execution
├── cli/                # CLI commands (console, dev, start, connect)
├── inference/          # Remote model inference (LLM, STT, TTS)
├── telemetry/          # OpenTelemetry traces and Prometheus metrics
└── utils/              # Audio processing, codecs, HTTP, async utilities

livekit-plugins/        # 50+ provider plugins (openai, anthropic, google, deepgram, etc.)
tests/                  # Test suite with mock implementations (fake_stt.py, fake_vad.py)
examples/               # Example agents and use cases
```

### Plugin System
Plugins in `livekit-plugins/` provide STT, TTS, LLM, and specialized services. Each plugin is a separate package following the pattern `livekit-plugins-<provider>`. Plugins register via the `Plugin` base class in `plugin.py`.

### Model Interface Pattern
STT, TTS, LLM, Realtime models have provider-agnostic interfaces with:
- Base classes defining the interface (`stt/stt.py`, `tts/tts.py`, `llm/llm.py`, `llm/realtime.py`)
- Fallback adapters for resilience
- Stream adapters for different streaming patterns

### Job Execution Flow
1. Worker receives job request from LiveKit server
2. Job is dispatched to process/thread pool (`ipc/proc_pool.py`)
3. Entrypoint function receives `JobContext`
4. Agent connects to room via `ctx.connect()`
5. `AgentSession` manages the conversation lifecycle

## Environment Variables
- `LIVEKIT_URL`: WebSocket URL of LiveKit server
- `LIVEKIT_API_KEY`: API key for authentication
- `LIVEKIT_API_SECRET`: API secret for authentication
- `LIVEKIT_AGENT_NAME`: Agent name for explicit dispatch (optional)
- Provider-specific keys: `OPENAI_API_KEY`, `DEEPGRAM_API_KEY`, `ANTHROPIC_API_KEY`, etc.

## Code Style
- Line length: 100 characters
- Python 3.10+ compatibility required
- Google-style docstrings
- Strict mypy type checking enabled
- Use `make check` and `make fix` before committing
- **自有代码规范**(文件/函数大小、复杂度、解耦):见 [docs/project/CODE_GUIDELINES.md](docs/project/CODE_GUIDELINES.md);用 `make lint-ours` 检查(仅约束我们写的代码,不含 livekit 母体)

## Multi-Agent 协作约定

### 角色分工
- **Review Agent**：评审代码变更，将待修项写入 `.claude/reviews/` 目录
- **Code Agent**：读取指定评审文档，按优先级逐项修改，完成后标记 `[x]`

### 评审文档规则
- 路径：`.claude/reviews/REVIEW_YYYY-MM-DD.md`（同一天多轮则加后缀 `_r2`、`_r3`）
- 每条 item 自包含：包含文件路径、行号、问题描述、修改建议，Code Agent 无需读对话历史
- 状态标记：`[ ]` 待修 / `[x]` 已完成，Code Agent 完成一项立即更新状态
- 文档**永久保留**，作为变更决策记录；不删除，不覆盖

### Code Agent 工作流
1. 用户指定评审文档（如"读 `.claude/reviews/REVIEW_2026-07-10.md` 修代码"）
2. 读取文档，按 P1 → P2 → P3 优先级处理 `[ ]` 项
3. 每项修改完成后将 `[ ]` 改为 `[x]`，并在行尾注明 commit hash 或修改说明
4. 全部完成后告知用户，由 Review Agent 验收
