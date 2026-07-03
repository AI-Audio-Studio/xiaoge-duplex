"""行为锁定测试:common.runtime.append_turn_log(阶段4改为后台线程写盘)。

断言:非阻塞入队后,行(带毫秒时间戳前缀)最终落盘、顺序保持。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "voice_agents"))

from common import runtime  # noqa: E402


def _wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_append_turn_log_writes_in_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log_file = tmp_path / "metrics.log"
    monkeypatch.setattr(runtime, "TURN_METRICS_LOG", log_file)

    runtime.append_turn_log("FIRST alpha")
    runtime.append_turn_log("SECOND beta")

    assert _wait_for(lambda: log_file.exists() and "SECOND" in log_file.read_text("utf-8"))
    lines = log_file.read_text("utf-8").splitlines()
    assert [ln.split(" ", 2)[2] for ln in lines] == ["FIRST alpha", "SECOND beta"]
    # 每行带 "YYYY-mm-dd HH:MM:SS.mmm " 前缀
    assert all(len(ln.split(" ", 2)) == 3 for ln in lines)
