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
    export XG_LISTEN_PORT="${XG_LISTEN_PORT:-${WEB_UI_PORT:-10099}}"
    export XG_SSL_CERT="${XG_SSL_CERT:-/data/home/allen.wangmh/software/MiniCPM/server/ssl/cert.pem}"
    export XG_SSL_KEY="${XG_SSL_KEY:-/data/home/allen.wangmh/software/MiniCPM/server/ssl/key.pem}"
    export XG_POOL_CONTROL_HOST="${XG_POOL_CONTROL_HOST:-127.0.0.1}"
    export XG_POOL_CONTROL_PORT="${XG_POOL_CONTROL_PORT:-19000}"
    export XG_POOL_API="${XG_POOL_API:-http://${XG_POOL_CONTROL_HOST}:${XG_POOL_CONTROL_PORT}}"
    export XG_POOL_BASE_PORT="${XG_POOL_BASE_PORT:-19100}"
    export XG_POOL_PORT_SPAN="${XG_POOL_PORT_SPAN:-100}"
    export XG_GRACE_SECONDS="${XG_GRACE_SECONDS:-12}"
    export XG_POOL_SIZE="${XG_POOL_SIZE:-4}"
    export XG_POOL_SPAWN_TIMEOUT_S="${XG_POOL_SPAWN_TIMEOUT_S:-240}"
    # apikey 准入(模式A/协议客户端):有效集合 = DB(sys_api_key,status='0') ∪ 静态列表。
    # required=0 兼容/观察(恒放行仅日志),=1 强制(缺/错拒)。非敏感 DB 参数走 .env。
    export XG_API_KEY_REQUIRED="${XG_API_KEY_REQUIRED:-0}"
    export XG_API_KEYS="${XG_API_KEYS:-}"
    export XG_API_KEY_DB_HOST="${XG_API_KEY_DB_HOST:-}"
    export XG_API_KEY_DB_PORT="${XG_API_KEY_DB_PORT:-3306}"
    export XG_API_KEY_DB_USER="${XG_API_KEY_DB_USER:-}"
    export XG_API_KEY_DB_PASSWORD="${XG_API_KEY_DB_PASSWORD:-}"
    export XG_API_KEY_DB_NAME="${XG_API_KEY_DB_NAME:-}"
    export XG_API_KEY_REFRESH_SEC="${XG_API_KEY_REFRESH_SEC:-60}"
    # DB 口令可能含 # / 空格,_load_env 会在 # 处截断且删空格;故口令从独立文件整行读取
    # ($BASE/.xg_db_password,权限 600,勿提交)。已由环境显式注入则不覆盖。
    if [[ -z "$XG_API_KEY_DB_PASSWORD" && -f "$BASE/.xg_db_password" ]]; then
        export XG_API_KEY_DB_PASSWORD="$(head -n1 "$BASE/.xg_db_password" | tr -d '\r\n')"
    fi
    # 服务器无麦克风,agent 用文字模式(实际音频走 WEB_AUDIO /ws/audio);本地有麦克风留空即可
    export XIAOGE_AGENT_CONSOLE_ARGS="${XIAOGE_AGENT_CONSOLE_ARGS:---text}"
    export PYTHONUTF8=1
    export PYTHONIOENCODING=utf-8
    export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
    export TRANSFORMERS_OFFLINE="${TRANSFORMERS_OFFLINE:-1}"
}

# ── 工具函数 ──────────────────────────────────────────────────────────────────

_pool_status() {
    curl -s --connect-timeout 1 "$XG_POOL_API/status" 2>/dev/null || echo '{}'
}

_pool_ready() {
    python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('ready',0))" \
        "$(_pool_status)" 2>/dev/null || echo 0
}

_kill_agents() {
    # agent 进程监听 XG_POOL_BASE_PORT 起始的端口段。
    # 双来源:① ss 端口段提取 pid;② pgrep -f web_ui_agent.py 兜底。
    # 单靠 ss 在本环境会漏:agent 的 listening socket 在 ss -p 里常不显示 pid
    # (SO_REUSEPORT / fork 后 socket 所有权分散),导致 stop 留下孤儿 agent 占着
    # 19100-19105,下次 start bind 冲突 -> spawn timeout -> recycle -> 死循环。
    # pgrep 抓 web_ui_agent.py(进程名独有,不误伤),补上 ss 漏掉的孤儿。
    local start end pids ss_pids pgrep_pids
    start="${XG_POOL_BASE_PORT:-19100}"
    end=$((start + ${XG_POOL_PORT_SPAN:-100} - 1))
    ss_pids=$(ss -tnlp 2>/dev/null \
        | awk -v start="$start" -v end="$end" '
            {
                port = ""
                if (match($4, /:[0-9]+$/)) {
                    port = substr($4, RSTART + 1, RLENGTH - 1)
                }
                if (port >= start && port <= end && match($0, /pid=([0-9]+)/, m)) {
                    print m[1]
                }
            }' \
        | sort -u)
    pgrep_pids=$(pgrep -f "web_ui_agent\.py" 2>/dev/null | sort -u || true)
    pids=$(printf '%s\n%s\n' "$ss_pids" "$pgrep_pids" | grep -E '^[0-9]+$' | sort -u || true)
    if [[ -n "$pids" ]]; then
        echo "  killing agents: $(echo "$pids" | tr '\n' ' ')"
        echo "$pids" | xargs kill -9 2>/dev/null || true
    else
        echo "  no agent processes"
    fi
}

_kill_pid_file() {
    local pid_file=$1 label=$2 pid
    pid=$(cat "$pid_file" 2>/dev/null || true)
    if [[ -n "$pid" && "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
        if kill -KILL "$pid" 2>/dev/null; then
            echo "  $label process killed: $pid"
        else
            echo "  $label process kill failed: $pid"
        fi
    else
        echo "  $label not running"
    fi
    rm -f "$pid_file"
}

_pid_state() {
    local pid_file=$1 pid
    pid=$(cat "$pid_file" 2>/dev/null || true)
    if [[ -n "$pid" && "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
        echo "pid=$pid  running"
    else
        echo "pid=${pid:-'-'}  NOT RUNNING"
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
    _setup_env
    echo "── gateway ──────────────────────────"
    # 先停 systemd（防止 Restart= 重拉起）
    systemctl --user stop xiaoge-gateway.service 2>/dev/null && echo "  systemd gateway stopped" || true
    _kill_pid_file "$RUN/gateway.pid" "gateway"

    echo "── poolmgr ──────────────────────────"
    _kill_pid_file "$RUN/poolmgr.pid" "poolmgr"

    echo "── agents ───────────────────────────"
    _kill_agents

    echo "── wait port $XG_POOL_CONTROL_PORT free ─────────────"
    _wait_port_free "$XG_POOL_CONTROL_PORT"
    echo "done."
}

# ── start ─────────────────────────────────────────────────────────────────────

do_start() {
    _setup_env
    mkdir -p "$RUN"

    if [[ "$(_pool_ready)" -gt 0 ]]; then
        echo "ERROR: poolmgr already running on :$XG_POOL_CONTROL_PORT. Run: $0 stop" >&2
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
    _setup_env
    local STATUS READY ASSIGNED SPAWNING
    STATUS=$(_pool_status)
    READY=$(python3    -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('ready','-'))"    "$STATUS" 2>/dev/null || echo '?')
    ASSIGNED=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('assigned','-'))" "$STATUS" 2>/dev/null || echo '?')
    SPAWNING=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('spawning','-'))" "$STATUS" 2>/dev/null || echo '?')

    echo "pool:    ready=$READY  assigned=$ASSIGNED  spawning=$SPAWNING"

    echo "gateway: $(_pid_state "$RUN/gateway.pid")"

    echo "poolmgr: $(_pid_state "$RUN/poolmgr.pid")"

    local AGENT_CNT AGENT_START AGENT_END
    AGENT_START="${XG_POOL_BASE_PORT:-19100}"
    AGENT_END=$((AGENT_START + ${XG_POOL_PORT_SPAN:-100} - 1))
    AGENT_CNT=$(ss -tnlp 2>/dev/null | awk -v start="$AGENT_START" -v end="$AGENT_END" '
        {
            port = ""
            if (match($4, /:[0-9]+$/)) {
                port = substr($4, RSTART + 1, RLENGTH - 1)
            }
            if (port >= start && port <= end) {
                count += 1
            }
        }
        END { print count + 0 }')
    echo "agents:  $AGENT_CNT listening (ports ${AGENT_START}-${AGENT_END})"

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
