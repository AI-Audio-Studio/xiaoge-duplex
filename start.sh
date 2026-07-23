#!/usr/bin/env bash
# start.sh — 在 Linux 服务器上启动小歌语音助手（Web 面板对外开放）
#
# 用法：
#   ./start.sh            # 前台运行，Ctrl+C 停止
#   ./start.sh -b         # 后台运行，日志写入 .run/web_ui_agent.log
#   ./start.sh -p 8788    # 指定端口
#   ./start.sh -t         # 启用测试模式（timeline + 多轨录音）
#   ./start.sh -T         # 文本输入模式（不用麦克风）
#   ./start.sh -a         # 开启 WebSocket 音频模式（WEB_AUDIO=1）
#
# WebSocket 音频模式（-a）：
#   客户端（机器人/浏览器）通过 ws://server:port/ws/audio 推送 PCM 帧，接收 TTS PCM 帧。
#   音频格式：16-bit LE PCM，16000 Hz，单声道（原始二进制帧，无封装）。
#   浏览器客户端需 HTTPS，设 WEB_SSL_CERT / WEB_SSL_KEY 或在浏览器启用不安全来源。
#   无声卡的服务器需先加载虚拟声卡：sudo modprobe snd-dummy
#
# 停止后台进程：
#   ./stop.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_DIR="$REPO_ROOT/examples/voice_agents"
RUN_DIR="$REPO_ROOT/.run"
PID_FILE="$RUN_DIR/web_ui_agent.pid"
LOG_FILE="$RUN_DIR/web_ui_agent.log"
PYTHON="$REPO_ROOT/.venv/bin/python"

# ── 参数解析 ─────────────────────────────────────────────────────────────────
BACKGROUND=0
TEXT_MODE=0
TEST_MODE=0
AUDIO_MODE=0
PREFERRED_PORT=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        -b|--background) BACKGROUND=1;    shift ;;
        -T|--text)       TEXT_MODE=1;     shift ;;
        -t|--test)       TEST_MODE=1;     shift ;;
        -a|--audio)      AUDIO_MODE=1;    shift ;;
        -p|--port)       PREFERRED_PORT="$2"; shift 2 ;;
        -h|--help)
            sed -n '/^# /{ s/^# //; p }' "$0" | head -20
            exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

# ── 检查虚拟环境 ──────────────────────────────────────────────────────────────
if [[ ! -x "$PYTHON" ]]; then
    echo "Error: virtual environment not found at $REPO_ROOT/.venv" >&2
    echo "Run:  cd $REPO_ROOT && uv sync --all-extras --dev  (or make install)" >&2
    exit 1
fi

# ── 防止重复启动 ──────────────────────────────────────────────────────────────
if [[ -f "$PID_FILE" ]]; then
    OLD_PID=$(cat "$PID_FILE" 2>/dev/null || true)
    if [[ -n "$OLD_PID" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Agent already running (PID $OLD_PID). Stop it first: ./stop.sh" >&2
        exit 1
    fi
fi

# ── 加载 .env ─────────────────────────────────────────────────────────────────
ENV_FILE="$REPO_ROOT/.env"
if [[ -f "$ENV_FILE" ]]; then
    # 只导出不以 # 开头、包含 = 的行，忽略空行
    while IFS= read -r line; do
        line="${line%%#*}"      # 去除行内注释
        line="${line//[$'\t\r\n ']}"  # 去除空白
        [[ -z "$line" || "$line" != *=* ]] && continue
        key="${line%%=*}"
        val="${line#*=}"
        [[ -z "$key" ]] && continue
        export "$key=$val"
    done < "$ENV_FILE"
    echo "Loaded .env"
else
    echo "Warning: .env not found — services may be unreachable." >&2
fi

# ── LiveKit 占位凭据（console 模式需要）───────────────────────────────────────
export LIVEKIT_URL="${LIVEKIT_URL:-ws://127.0.0.1:7880}"
export LIVEKIT_API_KEY="${LIVEKIT_API_KEY:-devkeydevkeydevkeydevkeydevkey12}"
export LIVEKIT_API_SECRET="${LIVEKIT_API_SECRET:-devsecretdevsecretdevsecretdevse}"

# ── 服务器模式：绑定 0.0.0.0 以接受外部浏览器访问 ────────────────────────────
export WEB_UI_HOST="${WEB_UI_HOST:-0.0.0.0}"

# ── 测试模式 ──────────────────────────────────────────────────────────────────
if [[ $TEST_MODE -eq 1 ]]; then
    export AGENT_TIMELINE=1
    echo "Test mode ON: timeline + recording -> runs/<timestamp>/"
fi

# ── WebSocket 音频模式 ────────────────────────────────────────────────────────
if [[ $AUDIO_MODE -eq 1 ]]; then
    export WEB_AUDIO=1
    echo "WebSocket audio mode ON: clients connect to ${WS_SCHEME:-ws}://server:${PORT:-8787}/ws/audio"
    echo "  Audio format: 16-bit PCM, 16000 Hz, mono, raw binary frames"
    if ! lsmod 2>/dev/null | grep -q snd_dummy && ! aplay -l 2>/dev/null | grep -q .; then
        echo "  WARNING: no audio device detected."
        echo "  Load dummy driver if console mode fails: sudo modprobe snd-dummy"
    fi
fi

# ── UTF-8 + 离线模型 ──────────────────────────────────────────────────────────
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
export TRANSFORMERS_OFFLINE="${TRANSFORMERS_OFFLINE:-1}"

# ── 选空闲端口 ────────────────────────────────────────────────────────────────
PREFERRED="${PREFERRED_PORT:-${WEB_UI_PORT:-8787}}"
PORT="$PREFERRED"
for ((i=0; i<20; i++)); do
    if ! ss -tlnH "sport = :$PORT" 2>/dev/null | grep -q .; then
        break
    fi
    PORT=$((PORT + 1))
done
if [[ "$PORT" -ge $((PREFERRED + 20)) ]]; then
    echo "Error: no free port near $PREFERRED" >&2
    exit 1
fi
if [[ "$PORT" -ne "$PREFERRED" ]]; then
    echo "Warning: port $PREFERRED in use, using $PORT instead."
fi
export WEB_UI_PORT="$PORT"

# ── 获取本机对外 IP（仅用于显示提示） ────────────────────────────────────────
SERVER_IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '/src/{print $7; exit}' || hostname -I 2>/dev/null | awk '{print $1}' || echo "<server-ip>")
SCHEME="http"
WS_SCHEME="ws"
if [[ -n "${WEB_SSL_CERT:-}" && -n "${WEB_SSL_KEY:-}" ]]; then
    SCHEME="https"
    WS_SCHEME="wss"
fi

# ── 组装命令 ──────────────────────────────────────────────────────────────────
SCRIPT="web_ui_agent.py"
CMD_ARGS=("$SCRIPT" "console")
[[ $TEXT_MODE -eq 1 ]] && CMD_ARGS+=("--text")

mkdir -p "$RUN_DIR"

echo ""
echo "Starting voice agent (Web UI on ${SCHEME}://${SERVER_IP}:${PORT}) ..."
echo ""

if [[ $BACKGROUND -eq 1 ]]; then
    cd "$AGENT_DIR"
    nohup "$PYTHON" "${CMD_ARGS[@]}" </dev/null >"$LOG_FILE" 2>&1 &
    AGENT_PID=$!
    echo "$AGENT_PID" > "$PID_FILE"
    echo "$PORT"       > "$RUN_DIR/web_ui_agent.port"
    echo "Started in background. PID $AGENT_PID → $PID_FILE"
    echo "Log: $LOG_FILE"
    echo ""
    echo "Open in browser: ${SCHEME}://${SERVER_IP}:${PORT}"
    echo "Stop with:       ./stop.sh"
else
    # 前台运行：不写 PID（Ctrl+C 直接停止）
    echo "Running in foreground. Press Ctrl+C to stop."
    echo "Open in browser: ${SCHEME}://${SERVER_IP}:${PORT}"
    echo ""
    cd "$AGENT_DIR"
    exec "$PYTHON" "${CMD_ARGS[@]}"
fi
