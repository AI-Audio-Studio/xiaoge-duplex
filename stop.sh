#!/usr/bin/env bash
# stop.sh — 停止后台运行的小歌语音助手

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$REPO_ROOT/.run/web_ui_agent.pid"

if [[ ! -f "$PID_FILE" ]]; then
    echo "No PID file found ($PID_FILE). Agent may not be running."
    exit 0
fi

PID=$(cat "$PID_FILE" 2>/dev/null || true)
if [[ -z "$PID" ]]; then
    echo "PID file is empty."
    rm -f "$PID_FILE"
    exit 0
fi

if ! kill -0 "$PID" 2>/dev/null; then
    echo "Process $PID is not running (already stopped)."
    rm -f "$PID_FILE"
    exit 0
fi

echo "Stopping agent (PID $PID)..."
kill "$PID"

# 等待进程退出（最多 10 秒）
for ((i=0; i<10; i++)); do
    sleep 1
    kill -0 "$PID" 2>/dev/null || break
done

if kill -0 "$PID" 2>/dev/null; then
    echo "Process did not exit; force-killing..."
    kill -9 "$PID" 2>/dev/null || true
fi

rm -f "$PID_FILE"
echo "Stopped."
