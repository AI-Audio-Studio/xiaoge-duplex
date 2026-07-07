"""行为锁定测试:并发改造 PR-C 网关 proxy(T2/D-16/R6)。

**真 WS 集成**(非 mock):client → gateway(Proxy)→ 假 agent WS,验证:
- FRESH:双向透传(agent 开场 id + echo);
- **T2 宽限窗**:客户端断开后**上游不关**、重连(REATTACH)接回**同一上游**——用假 agent
  连接计数证明"整个过程 agent 只被连了一次"(上游被网关持有、未重开);
- HTTP 反代 /api/*;宽限窗超时 close_session_io 关上游。
承前几轮教训:进程/连接类组件必配真 I/O 集成测,不以单测时序代替。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import aiohttp
import aiohttp.web
from aiohttp.test_utils import TestServer

_AGENT_DIR = Path(__file__).resolve().parents[1] / "examples" / "voice_agents"
sys.path.insert(0, str(_AGENT_DIR))

from gateway import affinity as af  # noqa: E402
from gateway.config import GatewayConfig  # noqa: E402
from gateway.proxy import Proxy  # noqa: E402


def _make_agent_app(agent_conns: list[int]) -> aiohttp.web.Application:
    """假 agent:/ws/audio 开场发连接 id + echo;/api/mic 回 JSON。agent_conns 记每次连接。"""

    async def ws_audio(request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
        ws = aiohttp.web.WebSocketResponse()
        await ws.prepare(request)
        agent_conns.append(1)
        await ws.send_str(f"agent-conn-{len(agent_conns)}")
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                await ws.send_bytes(b"echo:" + msg.data)
            elif msg.type == aiohttp.WSMsgType.TEXT:
                await ws.send_str("echo:" + msg.data)
            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                break
        return ws

    async def api_mic(request: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.json_response({"proxied": True, "path": str(request.rel_url)})

    app = aiohttp.web.Application()
    app.router.add_get("/ws/audio", ws_audio)
    app.router.add_post("/api/mic", api_mic)
    return app


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


async def _wait_state(table: af.AffinityTable, sid: str, state: str, timeout: float = 2.0) -> bool:
    end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end:
        s = table.get(sid)
        if s is not None and s.state == state:
            return True
        await asyncio.sleep(0.02)
    return False


async def _setup(clock: _Clock):
    agent_conns: list[int] = []
    agent = TestServer(_make_agent_app(agent_conns))
    await agent.start_server()
    cfg = GatewayConfig(max_frame_bytes=32_768)
    table = af.AffinityTable(grace_seconds=10.0, secret="s", clock=clock)
    proxy = Proxy(cfg, table)
    table.register("sess1", "p1", agent.port)

    async def gw_audio(request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
        ws = aiohttp.web.WebSocketResponse()
        await ws.prepare(request)
        result, session, conn_id = table.on_audio_connect("sess1")
        if result in (af.CONNECT_FRESH, af.CONNECT_REATTACH):
            await proxy.handle_audio(ws, session, result, conn_id)
        else:
            await ws.close()
        return ws

    async def gw_api(request: aiohttp.web.Request) -> aiohttp.web.Response:
        return await proxy.proxy_http(request, table.get("sess1"))

    gw_app = aiohttp.web.Application()
    gw_app.router.add_get("/gw/audio", gw_audio)
    gw_app.router.add_post("/api/mic", gw_api)
    gw = TestServer(gw_app)
    await gw.start_server()
    return agent, gw, proxy, table, agent_conns


def test_fresh_echo() -> None:
    async def _main():
        agent, gw, proxy, table, _ = await _setup(_Clock())
        try:
            async with aiohttp.ClientSession() as cs:
                ws = await cs.ws_connect(f"http://127.0.0.1:{gw.port}/gw/audio")
                assert (await ws.receive()).data == "agent-conn-1"  # 上游开场透传
                await ws.send_str("hi")
                assert (await ws.receive()).data == "echo:hi"  # 双向透传
                await ws.close()
        finally:
            await proxy.aclose()
            await gw.close()
            await agent.close()

    asyncio.run(_main())


def test_grace_holds_upstream_and_reattach() -> None:
    """T2 核心:断开→上游持有→重连接回同一上游(agent 全程只被连一次)。"""

    async def _main():
        agent, gw, proxy, table, agent_conns = await _setup(_Clock())
        try:
            async with aiohttp.ClientSession() as cs:
                ws1 = await cs.ws_connect(f"http://127.0.0.1:{gw.port}/gw/audio")
                assert (await ws1.receive()).data == "agent-conn-1"
                await ws1.send_str("a")
                assert (await ws1.receive()).data == "echo:a"
                await ws1.close()
                # 断开 → 宽限窗:上游不关、会话转 PENDING
                assert await _wait_state(table, "sess1", af.PENDING_DISCONNECT)
                assert "sess1" in proxy._io  # 上游仍被持有
                # 重连(REATTACH):不重开 agent 连接,接回同一上游
                ws2 = await cs.ws_connect(f"http://127.0.0.1:{gw.port}/gw/audio")
                assert await _wait_state(table, "sess1", af.ACTIVE)
                await ws2.send_str("b")
                assert (await ws2.receive()).data == "echo:b"  # 同一上游续接、echo 正常
                assert len(agent_conns) == 1  # ★ agent 全程只被连一次(上游被持有、未重开)
                await ws2.close()
        finally:
            await proxy.aclose()
            await gw.close()
            await agent.close()

    asyncio.run(_main())


def test_grace_timeout_closes_upstream() -> None:
    async def _main():
        clock = _Clock()
        agent, gw, proxy, table, _ = await _setup(clock)
        try:
            async with aiohttp.ClientSession() as cs:
                ws = await cs.ws_connect(f"http://127.0.0.1:{gw.port}/gw/audio")
                await ws.receive()
                await ws.close()
                assert await _wait_state(table, "sess1", af.PENDING_DISCONNECT)
                clock.t = 11.0  # 越过宽限窗
                for s in table.sweep_expired():  # 主循环会做的事
                    await proxy.close_session_io(s.session_id)
                assert "sess1" not in proxy._io  # 上游已关
        finally:
            await proxy.aclose()
            await gw.close()
            await agent.close()

    asyncio.run(_main())


def test_http_proxy() -> None:
    async def _main():
        agent, gw, proxy, table, _ = await _setup(_Clock())
        try:
            async with aiohttp.ClientSession() as cs:
                async with cs.post(f"http://127.0.0.1:{gw.port}/api/mic", json={"x": 1}) as r:
                    body = await r.json()
                assert r.status == 200 and body["proxied"] is True
        finally:
            await proxy.aclose()
            await gw.close()
            await agent.close()

    asyncio.run(_main())
