# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project scope

This repository is a Python/uv monorepo based on a LiveKit Agents fork. The project-specific application is **Xiaoge Duplex Speech**, a full-duplex Chinese voice interaction engine with browser test panel. It layers Qwen-compatible LLM, FunASR/Qwen3 streaming STT, DashScope/CosyVoice/Bailian TTS, and local sherpa-onnx KWS interruption on top of LiveKit Agents.

Primary app entrypoint: `examples/voice_agents/web_ui_agent.py`.

Documentation index: `docs/README.md`; architecture: `docs/guide/ARCHITECTURE.md`; run guide: `docs/guide/RUN.md`.

## Common commands

Run commands from the repository root (`xiaoge-duplex/xiaoge-duplex`). The project uses `uv`.

### Install / setup

```bash
make install                 # uv sync --all-extras --dev for the monorepo
```

Windows local app setup uses the project script instead of syncing every plugin:

```powershell
.\setup.ps1                  # create .venv, editable-install MVP deps, download turn-detector
.\setup.ps1 -SkipModelDownload
```

### Run the Xiaoge app

```powershell
.\start.ps1                  # voice/microphone mode, web panel defaults to http://localhost:8787
.\start.ps1 -Text            # text console mode
.\start.ps1 -Port 8770       # choose web panel port
.\start.ps1 -Background      # logs to .run\web_ui_agent.log and .run\web_ui_agent.log.err
.\start.ps1 -Test            # enable timeline + recordings under runs\<timestamp>\
.\stop.ps1                   # stop this project’s agent process
```

Double-click wrappers:

```text
start_agent.cmd              # starts with -Test by default
stop_agent.cmd
```

LiveKit Agents CLI modes also exist for agent files:

```bash
python examples/voice_agents/web_ui_agent.py console
python myagent.py console
python myagent.py dev
python myagent.py start
python myagent.py connect --room <room> --identity <id>
```

### Code quality

```bash
make format                  # ruff format .
make format-check            # ruff format --check .
make lint                    # ruff check .
make lint-fix                # ruff check --fix .
make type-check              # uv run python scripts/check_types.py
make lint-ours               # stricter checks for project-owned code in ourcode.txt
make check                   # format-check, lint, type-check, lint-ours
make fix                     # format + lint-fix
```

`ruff==0.15.18` is pinned in `pyproject.toml` to match CI. Line length is 100, Python target is 3.10+, mypy is strict.

### Tests

```bash
uv run pytest                                      # all tests, examples ignored by pytest config
uv run pytest tests/test_tools.py                  # single test file
uv run pytest tests/test_tools.py -k test_name     # single test selection
make unit-tests                                    # curated LiveKit unit-test list
cd tests && make unit-tests                        # unit tests excluding cloud/service-heavy test groups
cd tests && make realtime-tests                    # realtime tests only
cd tests && make test PLUGIN=<plugin> PYTEST_ARGS="..."  # docker/toxiproxy TTS test harness
```

Project-owned tests are named `tests/test_ours_*.py` and are included in `ourcode.txt`.

### Local python-rtc SDK development

```bash
make link-rtc
make link-rtc-local
make unlink-rtc
make status
make doctor
```

## Architecture overview

### Layers

The running Xiaoge app has four conceptual layers:

1. **Local I/O**: console-mode microphone/speaker via sounddevice/PortAudio; browser panel over local aiohttp/WebSocket.
2. **LiveKit Agents core** (`livekit-agents/livekit/agents/`): `AgentServer`, `JobContext`, `AgentSession`, audio recognition, turn detection, LLM/STT/TTS abstractions, telemetry, IPC, CLI.
3. **Xiaoge application layer** (`examples/voice_agents/`): assembles model backends, interruption taps, runtime state, web panel bridge, recording, turn metrics, text sanitization, listening mode, gateway/pool management.
4. **Remote/local model backends**: Qwen OpenAI-compatible LLM, FunASR/Qwen3/IFlyTek STT, DashScope/CosyVoice/Bailian/HTTP TTS, local Silero/turn-detector/KWS ONNX models.

Runtime is thread-heavy: the job loop owns most application orchestration, while PortAudio callbacks, KWS decoding, synchronous DashScope calls, audio recording close, turn-detector inference, and the web panel may run in separate threads/processes. Bridge back into asyncio with `call_soon_threadsafe` or `run_coroutine_threadsafe` rather than touching loop-owned objects directly.

### Core LiveKit concepts

- `AgentServer` (`livekit-agents/livekit/agents/worker.py`): main scheduler/server that launches agent sessions.
- `JobContext` (`livekit-agents/livekit/agents/job.py`): per-session context, room access, process userdata, shutdown callbacks.
- `Agent` (`livekit-agents/livekit/agents/voice/agent.py`): LLM-based application with instructions, tools, and lifecycle hooks.
- `AgentSession` (`livekit-agents/livekit/agents/voice/agent_session.py`): manages the conversation between agent, user, audio recognition, LLM, TTS, room I/O, and events.

### Project-owned code organization

The authoritative list of project-owned code is `ourcode.txt`; add new project-owned files there so `make lint-ours` covers them. `docs/project/CODE_GUIDELINES.md` applies to this list only, not to the LiveKit upstream body (`livekit-agents/`, `livekit-plugins/`) or upstream examples.

Important project-owned areas:

- `examples/voice_agents/web_ui_agent.py`: thin main entrypoint that builds the session, wires backends, registers event handlers, installs taps, and starts the browser panel.
- `examples/voice_agents/app/`: assembly/runtime layer (`AppRuntime`, backend registry/factories, switchable STT/TTS, listening host, web audio, tap setup).
- `examples/voice_agents/common/`: shared pure helpers and small infrastructure (`text_rules`, env/config parsing, runtime logging, tap base classes).
- `examples/voice_agents/providers/`: STT/TTS provider adapters split by backend; legacy files such as `custom_audio_providers.py`, `funasr_stream_stt.py`, and `iflytek_stt.py` are compatibility shims/re-exports.
- `examples/voice_agents/webpanel/`: aiohttp server, panel state, browser bridge, and static UI.
- `examples/voice_agents/kws_interrupt.py`, `online_interrupt.py`, `mute_gate.py`, `listening_mode.py`, `turn_config.py`, `text_sanitizer.py`, `live_transcript.py`: interruption, input gating, listening-mode state, turn parameters, TTS/display cleanup, and live transcript behavior.
- `examples/voice_agents/gateway/` and `poolmgr/`: protocol/gateway and concurrency/pool-management work.
- `clients/python/` and `clients/matlab/bridge/`: Xiaoge client integrations.

### Voice turn flow

Default upstream mode sends mic frames through tap wrappers (KWS, online interruption, recorder) into `AgentSession`. Audio recognition uses VAD and STT; non-streaming STT goes through a `StreamAdapter` that waits for a speech segment before final recognition. Final transcripts feed end-of-turn detection and `VoiceAgent.on_user_turn_completed`, then LLM streaming, transcript broadcast, TTS sanitization/synthesis, and output audio.

Optimized/streaming backends such as `funasr-stream` or IFlyTek can bypass the `StreamAdapter` and perform their own streaming/VAD aggregation.

### Interruption model

Interruption is a core feature. There are four complementary paths:

1. Framework VAD interruption: low-latency but content-blind.
2. Local KWS (`kws_interrupt.py`): sherpa-onnx keyword spotting in a real thread; hit callback forces `session.interrupt(force=True)`.
3. Online ASR interruption (`online_interrupt.py`): parallel FunASR 2pass stream used while the agent is speaking.
4. Offline final-text fallback: stop-word/backchannel handling in the final transcript path.

Audio taps must observe and then pass frames through unchanged unless they are explicitly a gate such as `MuteGate`.

## Configuration and local assets

Configuration is a flat root `.env` copied from `.env.example`; there is intentionally no separate config directory. Common variables include `QWEN_BASE_URL`, `QWEN_API_KEY`, `QWEN_MODEL`, `STT_BACKEND`, `FUNASR_WS_URL`, `QWEN3_ASR_STREAM_WS_URL`, `DASHSCOPE_API_KEY`, `BAILIAN_TTS_MODEL`, `BAILIAN_TTS_VOICE`, `XIAOGE_KWS_*`, `WEB_UI_PORT`, and `LIVEKIT_LOG_LEVEL`.

`models/` is gitignored and used for local model assets, especially `models/kws/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20`. Missing KWS assets/dependencies should degrade to no-op rather than blocking startup.

Generated/runtime outputs are gitignored: `.venv/`, `.run/`, `models/`, `recordings/`, `runs/`, logs, and turn-metric logs.

## Code style constraints for project-owned code

For files in `ourcode.txt`, follow `docs/project/CODE_GUIDELINES.md`:

- Keep project-owned modules small and cohesive; split responsibilities rather than growing `web_ui_agent.py`.
- Prefer pure logic separated from I/O, as in `listening_mode.py`.
- Keep dependencies one-way and avoid import cycles.
- Centralize env parsing in dataclasses/config helpers instead of scattering `os.getenv`.
- `make lint-ours` enforces stricter complexity/branch/argument/statement/return limits plus the project line-count gate.

When modifying the LiveKit upstream body, match upstream style and do not apply Xiaoge-only constraints unless the file is in `ourcode.txt`.
