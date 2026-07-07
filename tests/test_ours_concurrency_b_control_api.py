"""行为锁定测试:并发改造 PR-B 控制 API(P-2/M3)。

覆盖:/alloc 成功与繁忙(503)、/release 语义(有/无 session_id)、/status、serve 只绑 loopback。
用假 manager 隔离 API 层;aiohttp TestClient,无云依赖。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

_AGENT_DIR = Path(__file__).resolve().parents[1] / "examples" / "voice_agents"
sys.path.insert(0, str(_AGENT_DIR))

from poolmgr.control_api import build_control_app, serve  # noqa: E402


class _FakeManager:
    def __init__(self, alloc_result: dict | None = None) -> None:
        self._alloc_result = alloc_result
        self.released: list[tuple[str, str]] = []

    def alloc(self) -> dict | None:
        return self._alloc_result

    def release(self, session_id: str, reason: str = "") -> bool:
        self.released.append((session_id, reason))
        return session_id == "known"

    def status(self) -> dict:
        return {"size": 2, "ready": 1, "assigned": 1, "spawning": 0, "ready_below_threshold": True}


async def _req(app, method: str, path: str, json=None):
    async with TestClient(TestServer(app)) as client:
        resp = await client.request(method, path, json=json)
        return resp.status, await resp.json()


def test_alloc_ok() -> None:
    mgr = _FakeManager(alloc_result={"proc_id": "p1", "port": 19100, "session_id": "p1"})
    status, body = asyncio.run(_req(build_control_app(mgr), "POST", "/alloc"))
    assert status == 200 and body["port"] == 19100 and body["session_id"] == "p1"


def test_alloc_busy_503() -> None:
    mgr = _FakeManager(alloc_result=None)  # 池满
    status, body = asyncio.run(_req(build_control_app(mgr), "POST", "/alloc"))
    assert status == 503 and "error" in body


def test_release_known() -> None:
    mgr = _FakeManager()
    status, body = asyncio.run(
        _req(
            build_control_app(mgr),
            "POST",
            "/release",
            json={"session_id": "known", "reason": "done"},
        )
    )
    assert status == 200 and body["ok"] is True
    assert mgr.released == [("known", "done")]


def test_release_unknown_returns_ok_false() -> None:
    mgr = _FakeManager()
    status, body = asyncio.run(
        _req(build_control_app(mgr), "POST", "/release", json={"session_id": "nope"})
    )
    assert status == 200 and body["ok"] is False


def test_release_missing_session_id_400() -> None:
    mgr = _FakeManager()
    status, body = asyncio.run(_req(build_control_app(mgr), "POST", "/release", json={}))
    assert status == 400 and "error" in body


def test_status() -> None:
    mgr = _FakeManager()
    status, body = asyncio.run(_req(build_control_app(mgr), "GET", "/status"))
    assert status == 200 and body["size"] == 2 and body["ready_below_threshold"] is True


def test_serve_rejects_non_loopback() -> None:
    with pytest.raises(ValueError, match="loopback"):
        serve(_FakeManager(), host="0.0.0.0", port=19000)  # M3:禁止公网绑定
