#!/usr/bin/env bash
# log_monitor.sh — 小歌日志监控服务管理脚本
#
# 用法:
#   ./log_monitor.sh start    启动日志监控 web 服务（后台）
#   ./log_monitor.sh stop     停止
#   ./log_monitor.sh restart  重启
#   ./log_monitor.sh status    查看状态
#   ./log_monitor.sh fg        前台运行（调试用）

set -euo pipefail

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$BASE/.venv/bin/python"
SCRIPT="$BASE/log_monitor.py"
RUN="$BASE/.run"
PIDFILE="$RUN/log_monitor.pid"
LOGFILE="$RUN/log_monitor.out.log"
HOST="${XG_LOG_MONITOR_HOST:-0.0.0.0}"
PORT="${XG_LOG_MONITOR_PORT:-8020}"

mkdir -p "$RUN"

is_running() {
  [[ -f "$PIDFILE" ]] || return 1
  local pid; pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

cmd_start() {
  if is_running; then
    echo "log-monitor already running (pid $(cat "$PIDFILE"))"
    return 0
  fi
  echo "starting log-monitor on $HOST:$PORT ..."
  nohup "$PY" "$SCRIPT" --host "$HOST" --port "$PORT" \
    > "$LOGFILE" 2>&1 &
  local pid=$!
  echo "$pid" > "$PIDFILE"
  sleep 1
  if is_running; then
    echo "log-monitor started (pid $pid), log: $LOGFILE"
    echo "open: http://127.0.0.1:$PORT/  (内网 http://60.205.197.165:$PORT/ )"
  else
    echo "log-monitor failed to start, see $LOGFILE"
    tail -20 "$LOGFILE" 2>/dev/null || true
    return 1
  fi
}

cmd_stop() {
  if ! is_running; then
    echo "log-monitor not running"
    rm -f "$PIDFILE"
    return 0
  fi
  local pid; pid="$(cat "$PIDFILE")"
  echo "stopping log-monitor (pid $pid) ..."
  kill "$pid" 2>/dev/null || true
  # 兜底：杀残留（按脚本名匹配）
  for i in 1 2 3 4 5; do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.5
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "force killing ..."
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$PIDFILE"
  echo "stopped"
}

cmd_status() {
  if is_running; then
    local pid; pid="$(cat "$PIDFILE")"
    echo "log-monitor RUNNING (pid $pid) on $HOST:$PORT"
    echo "  open: http://127.0.0.1:$PORT/"
  else
    echo "log-monitor NOT RUNNING"
    return 1
  fi
}

cmd_fg() {
  "$PY" "$SCRIPT" --host "$HOST" --port "$PORT"
}

case "${1:-}" in
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  restart) cmd_stop; cmd_start ;;
  status)  cmd_status ;;
  fg)      cmd_fg ;;
  *)
    echo "usage: $0 {start|stop|restart|status|fg}" >&2
    exit 1 ;;
esac
