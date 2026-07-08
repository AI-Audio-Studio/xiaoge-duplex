"""行为锁定测试:并发改造 A1-F1——#3 优雅退出触发器纪律(webpanel `_request_graceful_exit`)。

#3(agent 小改):网关为**真实会话**的 /ws/audio 连接注入 `X-XG-Session` 头,断开时据此经 agent
循环 marshal `ctx.shutdown()`——跑 drain(录音收尾)后由监督者退出、池回收。

本测锁**我们这段触发器**的纪律(不依赖真 livekit 运行时):
- 有 `X-XG-Session`(网关标记)+ ctx/loop 就绪 → 触发 `ctx.shutdown()`;
- **无标记(PC/console 形态,头缺失)→ 天然不触发**(行为不变,"PC 形态不变");
- ctx 缺失 / loop 未运行 → 安全 no-op(不抛)。

**进程退出供池回收**由池 `default_kill`(terminate→wait→SIGKILL 兜底)保证(见 b_manager /
d_integration 回收测,A1-F1(b):不依赖 agent 自退);drain 路径共用 livekit `ctx.shutdown`→
`_on_shutdown`(job.py:655)。不依赖云/模型。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_AGENT_DIR = Path(__file__).resolve().parents[1] / "examples" / "voice_agents"
sys.path.insert(0, str(_AGENT_DIR))

from webpanel import server as ws  # noqa: E402


class _FakeLoop:
    def __init__(self, *, running: bool = True) -> None:
        self._running = running
        self.scheduled: list[Any] = []

    def is_running(self) -> bool:
        return self._running

    def call_soon_threadsafe(self, fn: Any, *a: Any) -> None:
        self.scheduled.append(fn)
        fn(*a)  # 测试内联执行,以观察 ctx.shutdown 是否被 marshal 调用


class _FakeCtx:
    def __init__(self) -> None:
        self.shutdown_reasons: list[str] = []

    def shutdown(self, reason: str = "") -> None:
        self.shutdown_reasons.append(reason)


def _wire(mp: pytest.MonkeyPatch, ctx: Any, loop: Any) -> None:
    mp.setattr(ws.runtime, "job_ctx", ctx, raising=False)
    mp.setattr(ws.runtime, "agent_loop", loop, raising=False)


def test_gateway_session_triggers_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, loop = _FakeCtx(), _FakeLoop()
    _wire(monkeypatch, ctx, loop)
    ws._request_graceful_exit("sess-abc")  # 网关标记会话断开
    assert ctx.shutdown_reasons == ["gateway session ended"]  # 触发优雅退出


def test_no_tag_does_not_trigger(monkeypatch: pytest.MonkeyPatch) -> None:
    """PC/console 形态:无 X-XG-Session → 头缺失 → 不触发(行为不变)。"""
    ctx, loop = _FakeCtx(), _FakeLoop()
    _wire(monkeypatch, ctx, loop)
    ws._request_graceful_exit(None)
    ws._request_graceful_exit("")
    assert ctx.shutdown_reasons == []  # 从不触发


def test_missing_ctx_or_stopped_loop_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """ctx 缺失 / loop 未运行 → 安全 no-op(不抛、不触发)。"""
    # ctx 缺失
    _wire(monkeypatch, None, _FakeLoop())
    ws._request_graceful_exit("sess-x")  # 不抛
    # loop 未运行
    ctx = _FakeCtx()
    _wire(monkeypatch, ctx, _FakeLoop(running=False))
    ws._request_graceful_exit("sess-x")
    assert ctx.shutdown_reasons == []
    # loop 缺失
    _wire(monkeypatch, _FakeCtx(), None)
    ws._request_graceful_exit("sess-x")  # 不抛
