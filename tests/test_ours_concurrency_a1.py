"""行为锁定测试:并发改造 PR-A1(agent 六处小改之 1/2/3/4/6)。

覆盖:session_id() 回退语义、append_turn_log 的 [sid] 前缀(env 门控、未设时逐字节不变)、
WEB_UI_PORT 代码默认 8787(D-23)、/healthz 结构、X-XG-Session 断开的优雅退出(未标记/
无 ctx 时天然不触发)。均为纯逻辑,无云依赖。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_AGENT_DIR = Path(__file__).resolve().parents[1] / "examples" / "voice_agents"
sys.path.insert(0, str(_AGENT_DIR))

from common import runtime  # noqa: E402


# ── #1/#4 session_id() 回退语义 ──────────────────────────────────────────────
def test_session_id_uses_env_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XIAOGE_SESSION_ID", "gw7")
    assert runtime.session_id() == "gw7"


def test_session_id_falls_back_to_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XIAOGE_SESSION_ID", raising=False)
    sid = runtime.session_id()
    assert sid == f"p{os.getpid() % 10000:04d}"
    assert sid.startswith("p") and len(sid) == 5


def test_session_id_blank_env_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XIAOGE_SESSION_ID", "   ")
    assert runtime.session_id().startswith("p")  # 空白视同未设


# ── #4 append_turn_log 前缀:未设 env 逐字节不变;设了才加 [sid] ──────────────
def _capture_one_line(monkeypatch: pytest.MonkeyPatch, line: str) -> str:
    import queue

    fresh: queue.Queue = queue.Queue(maxsize=10)
    monkeypatch.setattr(runtime, "_log_queue", fresh)
    monkeypatch.setattr(runtime, "_ensure_log_thread", lambda: None)  # 不起写线程
    runtime.append_turn_log(line)
    return fresh.get_nowait()


def test_log_no_prefix_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XIAOGE_SESSION_ID", raising=False)
    out = _capture_one_line(monkeypatch, "TURN_USER text='hi'")
    # 时间戳后直接是内容,无 [..] 前缀(与改造前逐字节一致)
    assert " TURN_USER text='hi'\n" in out
    assert "[" not in out.split(" TURN_USER")[0]


def test_log_prefix_when_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XIAOGE_SESSION_ID", "gw7")
    out = _capture_one_line(monkeypatch, "TURN_USER text='hi'")
    assert "[gw7] TURN_USER text='hi'\n" in out


# ── #6 WEB_UI_PORT 代码默认 8787(D-23);override 仍生效 ──────────────────────
def _web_port_with_env(env_val: str | None) -> int:
    env = {k: v for k, v in os.environ.items() if k != "WEB_UI_PORT"}
    if env_val is not None:
        env["WEB_UI_PORT"] = env_val
    code = (
        f"import sys; sys.path.insert(0, r'{_AGENT_DIR}');"
        "from webpanel.state import WEB_PORT; print(WEB_PORT)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60
    )
    assert out.returncode == 0, out.stderr
    return int(out.stdout.strip())


def test_web_port_default_is_8787() -> None:
    assert _web_port_with_env(None) == 8787


def test_web_port_env_override() -> None:
    assert _web_port_with_env("19123") == 19123


# ── #2 /healthz 结构:agent 未就绪时 ready=False ─────────────────────────────
def test_healthz_reports_not_ready_without_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from app.session_state import runtime as app_runtime
    from webpanel import server

    monkeypatch.setattr(app_runtime, "agent_loop", None)
    monkeypatch.setattr(app_runtime, "session", None)
    resp = asyncio.run(server._handle_healthz(None))  # 处理器不读 request
    body = json.loads(resp.body.decode())
    assert body == {"ready": False, "agent_loop_running": False}


# ── #3 X-XG-Session 断开的优雅退出:无 ctx / 无标记时天然不触发 ───────────────
def test_graceful_exit_inert_without_ctx(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.session_state import runtime as app_runtime
    from webpanel import server

    monkeypatch.setattr(app_runtime, "job_ctx", None)
    monkeypatch.setattr(app_runtime, "agent_loop", None)
    server._request_graceful_exit("gw7")  # 不得抛异常、不得崩


def test_graceful_exit_calls_ctx_shutdown_when_marked(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.session_state import runtime as app_runtime
    from webpanel import server

    calls: list[str] = []

    class _FakeCtx:
        def shutdown(self, reason: str = "") -> None:
            calls.append(reason)

    class _FakeLoop:
        def is_running(self) -> bool:
            return True

        def call_soon_threadsafe(self, fn, *a) -> None:
            fn(*a)  # 同步执行以便断言

    monkeypatch.setattr(app_runtime, "job_ctx", _FakeCtx())
    monkeypatch.setattr(app_runtime, "agent_loop", _FakeLoop())
    server._request_graceful_exit("gw7")
    assert calls == ["gateway session ended"]


def test_dir_suffix_unique_across_pids(monkeypatch: pytest.MonkeyPatch) -> None:
    """#1:同秒两"进程"(不同 XIAOGE_SESSION_ID)目录名不撞。"""
    monkeypatch.setenv("XIAOGE_SESSION_ID", "a1")
    a = f"{time.strftime('%Y%m%d_%H%M%S')}_{runtime.session_id()}"
    monkeypatch.setenv("XIAOGE_SESSION_ID", "b2")
    b = f"{time.strftime('%Y%m%d_%H%M%S')}_{runtime.session_id()}"
    assert a != b and a.endswith("_a1") and b.endswith("_b2")
