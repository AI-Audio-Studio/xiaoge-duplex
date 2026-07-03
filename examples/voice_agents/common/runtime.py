"""进程运行时小件:UTF-8 stdio、毫秒格式化、turn 指标日志落盘。

此前完整复制在两个 agent 文件里,收敛到这里。`append_turn_log` 当前为同步写
(与原实现一致);性能阶段再改为后台线程写,接口不变。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

TURN_METRICS_LOG = Path(os.getenv("TURN_METRICS_LOG", "qwen_voice_turn_metrics.log")).resolve()


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
    """追加一行带毫秒时间戳的 turn 指标日志。"""
    now = time.time()
    ts = f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))}.{int((now % 1) * 1000):03d}"
    with TURN_METRICS_LOG.open("a", encoding="utf-8") as f:
        f.write(f"{ts} {line}\n")
