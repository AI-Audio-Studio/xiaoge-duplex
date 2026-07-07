"""行为锁定测试:并发改造 PR-C 网关之 PoolClient(P-2)。

**端到端**:PoolClient ↔ 真 `poolmgr.control_api`(真 HTTP、ephemeral 端口),验证
alloc/release/status 语义 + 池不可达时的安全默认(不打挂网关)。堵"假 I/O 逃过"。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_AGENT_DIR = Path(__file__).resolve().parents[1] / "examples" / "voice_agents"
sys.path.insert(0, str(_AGENT_DIR))

from aiohttp.test_utils import TestServer  # noqa: E402
from gateway.pool_client import PoolClient  # noqa: E402
from poolmgr.control_api import build_control_app  # noqa: E402


class _FakeManager:
    def __init__(self, alloc_result: dict | None = None) -> None:
        self._alloc = alloc_result
        self.released: list[tuple[str, str]] = []

    def alloc(self) -> dict | None:
        return self._alloc

    def release(self, session_id: str, reason: str = "") -> bool:
        self.released.append((session_id, reason))
        return session_id == "known"

    def status(self) -> dict:
        return {"size": 2, "ready": 1}


def _run_against_control_api(manager, call):
    async def _main():
        server = TestServer(build_control_app(manager))
        await server.start_server()
        client = PoolClient(f"http://127.0.0.1:{server.port}")
        try:
            return await call(client)
        finally:
            await client.close()
            await server.close()

    return asyncio.run(_main())


def test_alloc_ok() -> None:
    mgr = _FakeManager(alloc_result={"proc_id": "p1", "port": 19100, "session_id": "p1"})
    r = _run_against_control_api(mgr, lambda c: c.alloc())
    assert r is not None and r["port"] == 19100 and r["session_id"] == "p1"


def test_alloc_busy_returns_none() -> None:
    mgr = _FakeManager(alloc_result=None)  # 控制 API 回 503
    assert _run_against_control_api(mgr, lambda c: c.alloc()) is None


def test_release_ok_and_payload() -> None:
    mgr = _FakeManager()
    ok = _run_against_control_api(mgr, lambda c: c.release("known", "done"))
    assert ok is True and mgr.released == [("known", "done")]


def test_release_unknown_false() -> None:
    mgr = _FakeManager()
    assert _run_against_control_api(mgr, lambda c: c.release("nope")) is False


def test_status() -> None:
    mgr = _FakeManager()
    st = _run_against_control_api(mgr, lambda c: c.status())
    assert st["size"] == 2 and st["ready"] == 1


def test_pool_unreachable_safe_defaults() -> None:
    """池不可达:alloc→None(视同繁忙)、release→False、status→{},绝不抛出打挂网关。"""

    async def _main():
        client = PoolClient("http://127.0.0.1:1", timeout=0.5)  # 无监听端口,短超时
        try:
            assert await client.alloc() is None
            assert await client.release("s1") is False
            assert await client.status() == {}
        finally:
            await client.close()

    asyncio.run(_main())
