#!/usr/bin/env bash
# xg.sh — 小歌并发架构管理脚本
#
# 用法:
#   ./xg.sh start    启动 poolmgr + gateway（等待 pool ready）
#   ./xg.sh stop     停止所有进程（gateway / poolmgr / agents）
#   ./xg.sh restart  stop + start
#   ./xg.sh status   当前状态
#   ./xg.sh logs     实时查看 gateway 日志

set -euo pipefail

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VA="$BASE/examples/voice_agents"
PY="$BASE/.venv/bin/python"
RUN="$BASE/.run"
ENV_FILE="$BASE/.env"

# ── 环境 ─────────────────────────────────────────────────────────────────────

_load_env() {
    [[ ! -f "$ENV_FILE" ]] && return
    while IFS= read -r line; do
        line="${line%%#*}"
        line="${line//[$'\t\r\n ']}"
        [[ -z "$line" || "$line" != *=* ]] && continue
        key="${line%%=*}"; val="${line#*=}"
        [[ -z "$key" ]] && continue
        export "$key=$val"
    done < "$ENV_FILE"
}

_setup_env() {
    _load_env
    export XG_LISTEN_HOST="${XG_LISTEN_HOST:-0.0.0.0}"
    export XG_LISTEN_PORT="${WEB_UI_PORT:-10099}"
    export XG_SSL_CERT="${XG_SSL_CERT:-/data/home/allen.wangmh/software/MiniCPM/server/ssl/cert.pem}"
    export XG_SSL_KEY="${XG_SSL_KEY:-/data/home/allen.wangmh/software/MiniCPM/server/ssl/key.pem}"
    export XG_POOL_API="${XG_POOL_API:-http://127.0.0.1:19000}"
    export XG_GRACE_SECONDS="${XG_GRACE_SECONDS:-12}"
    export XG_POOL_SIZE="${XG_POOL_SIZE:-4}"
    export XG_POOL_SPAWN_TIMEOUT_S="${XG_POOL_SPAWN_TIMEOUT_S:-240}"
    # 服务器无麦克风,agent 用文字模式(实际音频走 WEB_AUDIO /ws/audio);本地有麦克风留空即可
    export XIAOGE_AGENT_CONSOLE_ARGS="${XIAOGE_AGENT_CONSOLE_ARGS:---text}"
    export PYTHONUTF8=1
    export PYTHONIOENCODING=utf-8
    export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
    export TRANSFORMERS_OFFLINE="${TRANSFORMERS_OFFLINE:-1}"
}

# ── 工具函数 ──────────────────────────────────────────────────────────────────

_pool_status() {
    curl -s --connect-timeout 1 http://127.0.0.1:19000/status 2>/dev/null || echo '{}'
}

_pool_ready() {
    python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('ready',0))" \
        "$(_pool_status)" 2>/dev/null || echo 0
}

_kill_agents() {
    # agent 进程监听 19100-19199 端口
    local pids
    pids=$(ss -tnlp 2>/dev/null \
        | awk '/191[0-9]{2}/ { if (match($0, /pid=([0-9]+)/, m)) print m[1] }' \
        | sort -u)
    if [[ -n "$pids" ]]; then
        echo "  killing agents: $(echo "$pids" | tr '\n' ' ')"
        echo "$pids" | xargs kill -9 2>/dev/null || true
    else
        echo "  no agent processes"
    fi
}

_wait_port_free() {
    local port=$1 max=${2:-10}
    for ((i=0; i<max; i++)); do
        curl -s --connect-timeout 1 "http://127.0.0.1:$port/" &>/dev/null || break
        sleep 1
    done
}

# ── stop ─────────────────────────────────────────────────────────────────────

do_stop() {
    echo "── gateway ──────────────────────────"
    # 先停 systemd（防止 Restart= 重拉起）
    systemctl --user stop xiaoge-gateway.service 2>/dev/null && echo "  systemd gateway stopped" || true
    # 按进程名 kill（兜住 PID 文件过时的情况）
    if pkill -KILL -f 'python.*-m gateway' 2>/dev/null; then
        echo "  gateway process killed"
    else
        echo "  gateway not running"
    fi
    rm -f "$RUN/gateway.pid"

    echo "── poolmgr ──────────────────────────"
    if pkill -KILL -f 'python.*-m poolmgr' 2>/dev/null; then
        echo "  poolmgr process killed"
    else
        echo "  poolmgr not running"
    fi
    rm -f "$RUN/poolmgr.pid"

    echo "── agents ───────────────────────────"
    _kill_agents

    echo "── wait port 19000 free ─────────────"
    _wait_port_free 19000
    echo "done."
}

# ── start ─────────────────────────────────────────────────────────────────────

do_start() {
    _setup_env
    mkdir -p "$RUN"

    if [[ "$(_pool_ready)" -gt 0 ]]; then
        echo "ERROR: poolmgr already running on :19000. Run: $0 stop" >&2
        exit 1
    fi

    echo "── start poolmgr ────────────────────"
    cd "$VA"
    nohup "$PY" -m poolmgr >"$RUN/poolmgr.log" 2>&1 &
    POOL_PID=$!
    echo "$POOL_PID" > "$RUN/poolmgr.pid"
    echo "  PID=$POOL_PID  log=$RUN/poolmgr.log"

    local wait_seconds wait_steps
    wait_seconds="$XG_POOL_SPAWN_TIMEOUT_S"
    wait_steps=$(( (wait_seconds + 1) / 2 ))

    echo "── wait pool ready (max ${wait_seconds}s) ────────"
    READY=0
    for i in $(seq 1 "$wait_steps"); do
        sleep 2
        STATUS=$(_pool_status)
        READY=$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('ready',0))" "$STATUS" 2>/dev/null || echo 0)
        printf "  [%2d/%d] ready=%-2s  %s\n" "$i" "$wait_steps" "$READY" "$STATUS"
        [[ "$READY" -ge 1 ]] && break
    done
    [[ "$READY" -lt 1 ]] && { echo "ERROR: pool not ready after ${wait_seconds}s" >&2; exit 1; }

    echo "── start gateway ────────────────────"
    cd "$VA"
    nohup "$PY" -m gateway >"$RUN/gateway.log" 2>&1 &
    GW_PID=$!
    echo "$GW_PID" > "$RUN/gateway.pid"
    echo "  PID=$GW_PID  log=$RUN/gateway.log"

    sleep 3
    echo ""
    do_status
}

# ── status ────────────────────────────────────────────────────────────────────

do_status() {
    local STATUS READY ASSIGNED SPAWNING
    STATUS=$(_pool_status)
    READY=$(python3    -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('ready','-'))"    "$STATUS" 2>/dev/null || echo '?')
    ASSIGNED=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('assigned','-'))" "$STATUS" 2>/dev/null || echo '?')
    SPAWNING=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('spawning','-'))" "$STATUS" 2>/dev/null || echo '?')

    echo "pool:    ready=$READY  assigned=$ASSIGNED  spawning=$SPAWNING"

    local GW_PID GW_ST
    GW_PID=$(cat "$RUN/gateway.pid" 2>/dev/null || echo '-')
    pgrep -f 'python.*-m gateway' &>/dev/null && GW_ST="running" || GW_ST="NOT RUNNING"
    echo "gateway: pid=$GW_PID  $GW_ST"

    local PM_PID PM_ST
    PM_PID=$(cat "$RUN/poolmgr.pid" 2>/dev/null || echo '-')
    pgrep -f 'python.*-m poolmgr' &>/dev/null && PM_ST="running" || PM_ST="NOT RUNNING"
    echo "poolmgr: pid=$PM_PID  $PM_ST"

    local AGENT_CNT
    AGENT_CNT=$(ss -tnlp 2>/dev/null | grep -c '191[0-9][0-9]' || echo 0)
    echo "agents:  $AGENT_CNT listening (ports 191xx)"

    systemctl --user is-active xiaoge-gateway.service &>/dev/null \
        && echo "systemd: gateway active" \
        || echo "systemd: gateway inactive"
}

# ── logs ──────────────────────────────────────────────────────────────────────

do_logs() {
    local target="${2:-gateway}"
    case "$target" in
        gateway) tail -f "$RUN/gateway.log" ;;
        pool*)   tail -f "$RUN/poolmgr.log" ;;
        *)       echo "用法: $0 logs [gateway|poolmgr]" ;;
    esac
}

# ── main ──────────────────────────────────────────────────────────────────────

case "${1:-help}" in
    start)   do_start ;;
    stop)    do_stop ;;
    restart) do_stop; sleep 2; do_start ;;
    status)  do_status ;;
    logs)    do_logs "$@" ;;
    *)
        echo "用法: $0 {start|stop|restart|status|logs [gateway|poolmgr]}"
        exit 1 ;;
esac
