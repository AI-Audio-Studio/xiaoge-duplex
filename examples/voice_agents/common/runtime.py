"""进程运行时小件:UTF-8 stdio、毫秒格式化、turn 指标日志落盘。

`append_turn_log`(阶段4):时间戳在调用线程生成(保持顺序语义),写盘移到后台
daemon 线程——原实现每条 STT final 都在 agent 事件循环上同步 open/write,磁盘慢会
拖延迟。队列满(磁盘卡死)时丢日志不丢事件循环;进程退出时 atexit 排空队列。
"""

from __future__ import annotations

import atexit
import contextlib
import os
import queue
import sys
import threading
import time
from pathlib import Path

TURN_METRICS_LOG = Path(os.getenv("TURN_METRICS_LOG", "qwen_voice_turn_metrics.log")).resolve()

_log_queue: queue.Queue[str | None] = queue.Queue(maxsize=1000)
_log_thread: threading.Thread | None = None
_log_thread_lock = threading.Lock()


def _drain_pending(first: str) -> list[str]:
    lines = [first]
    with contextlib.suppress(queue.Empty):
        while True:
            nxt = _log_queue.get_nowait()
            if nxt is None:
                break
            lines.append(nxt)
    return lines


def _log_writer_loop() -> None:
    while True:
        line = _log_queue.get()
        if line is None:
            return
        lines = _drain_pending(line)  # 批量攒一次写,降低 open/close 频率
        try:
            with TURN_METRICS_LOG.open("a", encoding="utf-8") as f:
                f.writelines(lines)
        except Exception:
            pass  # 指标日志尽力而为,绝不影响主流程


def _flush_log_at_exit() -> None:
    with contextlib.suppress(Exception):
        _log_queue.put_nowait(None)
        if _log_thread is not None:
            _log_thread.join(timeout=2.0)


def _ensure_log_thread() -> None:
    global _log_thread
    if _log_thread is not None:
        return
    with _log_thread_lock:
        if _log_thread is None:
            t = threading.Thread(target=_log_writer_loop, name="turn-metrics-log", daemon=True)
            t.start()
            atexit.register(_flush_log_at_exit)
            _log_thread = t


def configure_utf8_stdio() -> None:
    """Windows 控制台 UTF-8 化(GBK 控制台打中文日志不炸)。幂等,失败静默。"""
    os.environ.setdefault("PYTHONUTF8", "1")
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleCP(65001)
    except Exception:
        pass


def ms(value: float | None) -> str:
    """秒 -> "123.4ms";None -> "-"(指标日志的统一格式)。"""
    if value is None:
        return "-"
    return f"{value * 1000:.1f}ms"


def append_turn_log(line: str) -> None:
    """追加一行带毫秒时间戳的 turn 指标日志(非阻塞:入队,后台线程写盘)。"""
    now = time.time()
    ts = f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))}.{int((now % 1) * 1000):03d}"
    _ensure_log_thread()
    with contextlib.suppress(queue.Full):
        _log_queue.put_nowait(f"{ts} {line}\n")
