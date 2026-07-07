"""行为锁定测试:并发改造 PR-C 网关装配 main(六路由规则 + Q6 准入 + R6 安全)。

HTTP 路由决策用 TestClient + 假池(无需真 agent):规则1 分配/繁忙、Q6 准入门、规则2
409、规则4 拒绝、§6.2 白名单 404、规则6 双标签页页级提示。**协议客户端(规则3)+ D-07
即断即杀**走一条真 agent WS 集成测(承前教训:连接类路径配真 I/O,不以假时序代替)。
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path
from typing import Any

import aiohttp
import aiohttp.web
from aiohttp.test_utils import TestClient, TestServer

_AGENT_DIR = Path(__file__).resolve().parents[1] / "examples" / "voice_agents"
sys.path.insert(0, str(_AGENT_DIR))

from gateway import affinity as af, main as gwmain  # noqa: E402
from gateway.config import GatewayConfig  # noqa: E402
from gateway.proxy import Proxy  # noqa: E402


class _FakePool:
    """假池:按 seats 依次发座;seats 空即繁忙(None)。记录 release 调用。"""

    def __init__(self, seats: list[dict[str, Any]]) -> None:
        self._seats = list(seats)
        self.released: list[str] = []

    async def alloc(self) -> dict[str, Any] | None:
        return self._seats.pop(0) if self._seats else None

    async def release(self, session_id: str, reason: str = "") -> bool:
        self.released.append(session_id)
        return True

    async def status(self) -> dict[str, Any]:
        return {"free": len(self._seats)}

    async def close(self) -> None:
        pass


def _mk(
    config: GatewayConfig, pool: _FakePool
) -> tuple[aiohttp.web.Application, af.AffinityTable, Proxy]:
    table = af.AffinityTable(grace_seconds=10.0, secret=config.hmac_secret)
    proxy = Proxy(config, table)
    app = gwmain.build_gateway_app(config, table, proxy, pool)  # type: ignore[arg-type]
    return app, table, proxy


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ── 规则1:GET / 分配 / 繁忙 / 刷新 ────────────────────────────────────────────
def test_root_allocs_and_sets_cookie() -> None:
    async def _main() -> None:
        cfg = GatewayConfig(hmac_secret="s")
        pool = _FakePool([{"session_id": "sess1", "proc_id": "p1", "port": 19100}])
        app, _, _ = _mk(cfg, pool)
        async with TestClient(TestServer(app)) as cli:
            r = await cli.get("/")
            assert r.status == 200
            assert gwmain.COOKIE in r.cookies  # 种亲和 cookie(规则1)
            assert "小歌" in await r.text()

    _run(_main())


def test_root_busy_when_pool_full() -> None:
    async def _main() -> None:
        cfg = GatewayConfig(hmac_secret="s")
        app, _, _ = _mk(cfg, _FakePool([]))  # 无座
        async with TestClient(TestServer(app)) as cli:
            r = await cli.get("/")
            assert r.status == 503 and "繁忙" in await r.text()  # 规则1 繁忙页
            assert gwmain.COOKIE not in r.cookies

    _run(_main())


def test_root_refresh_with_cookie_does_not_realloc() -> None:
    async def _main() -> None:
        cfg = GatewayConfig(hmac_secret="s")
        pool = _FakePool([{"session_id": "sess1", "proc_id": "p1", "port": 19100}])
        app, table, _ = _mk(cfg, pool)
        table.register("sess1", "p1", 19100)  # 已有会话
        cookie = table.cookie_for("sess1")
        async with TestClient(TestServer(app)) as cli:
            r = await cli.get("/", cookies={gwmain.COOKIE: cookie})
            assert r.status == 200  # 刷新回页,不重分配
            assert len(pool._seats) == 1  # 池未被再分配(规则2:两通道不分家)

    _run(_main())


def test_root_double_tab_page(monkeypatch: Any = None) -> None:
    """规则6 页级:同 cookie 会话已有活跃音频连接 → 返回'另一窗口通话'提示页。"""

    async def _main() -> None:
        cfg = GatewayConfig(hmac_secret="s")
        app, table, _ = _mk(cfg, _FakePool([]))
        table.register("sess1", "p1", 19100)
        table.on_audio_connect("sess1")  # 变 ACTIVE + 有活跃音频连接
        cookie = table.cookie_for("sess1")
        async with TestClient(TestServer(app)) as cli:
            r = await cli.get("/", cookies={gwmain.COOKIE: cookie})
            assert r.status == 409 and "另一窗口" in await r.text()

    _run(_main())


# ── Q6 准入(D-18)──────────────────────────────────────────────────────────────
def test_access_gate_blocks_without_code() -> None:
    async def _main() -> None:
        cfg = GatewayConfig(hmac_secret="s", access_code="letmein")
        pool = _FakePool([{"session_id": "s1", "proc_id": "p1", "port": 19100}])
        app, _, _ = _mk(cfg, pool)
        async with TestClient(TestServer(app)) as cli:
            r = await cli.get("/")
            assert r.status == 401 and "口令" in await r.text()  # 准入门
            assert len(pool._seats) == 1  # 未分配(准入前置于 alloc)
            # 错误口令 → 仍 401,不泄漏拓扑
            bad = await cli.post("/access", data={"code": "nope"})
            assert bad.status == 401
            # 正确口令 → 种准入 cookie,随后放行分配
            ok = await cli.post("/access", data={"code": "letmein"}, allow_redirects=False)
            assert ok.status == 302 and gwmain.ACCESS_COOKIE in ok.cookies
            r2 = await cli.get("/")  # 客户端已带准入 cookie
            assert r2.status == 200 and gwmain.COOKIE in r2.cookies

    _run(_main())


# ── 规则2/4 + §6.2 白名单 ─────────────────────────────────────────────────────
def test_api_whitelist_and_session_guard() -> None:
    async def _main() -> None:
        cfg = GatewayConfig(hmac_secret="s")
        app, table, _ = _mk(cfg, _FakePool([]))
        async with TestClient(TestServer(app)) as cli:
            # 未知路径 → 404(白名单先于 cookie,不泄漏拓扑)
            r404 = await cli.post("/api/evil", json={})
            assert r404.status == 404
            # 白名单内但无 cookie → 409(规则2 强制回页)
            r409 = await cli.post("/api/mic", json={})
            assert r409.status == 409

    _run(_main())


def test_ws_state_without_cookie_rejected() -> None:
    """规则4:GET /ws 无 cookie → 4001 拒绝。"""

    async def _main() -> None:
        cfg = GatewayConfig(hmac_secret="s")
        app, _, _ = _mk(cfg, _FakePool([]))
        async with TestClient(TestServer(app)) as cli:
            ws = await cli.ws_connect("/ws")
            msg = await ws.receive()
            assert msg.type == aiohttp.WSMsgType.CLOSE and ws.close_code == 4001
            await ws.close()

    _run(_main())


def test_ws_audio_invalid_cookie_rejected() -> None:
    """规则2:/ws/audio 带无效 cookie → 4001。"""

    async def _main() -> None:
        cfg = GatewayConfig(hmac_secret="s")
        app, _, _ = _mk(cfg, _FakePool([]))
        async with TestClient(TestServer(app)) as cli:
            ws = await cli.ws_connect("/ws/audio", headers={"Cookie": f"{gwmain.COOKIE}=bogus"})
            msg = await ws.receive()
            assert msg.type == aiohttp.WSMsgType.CLOSE and ws.close_code == 4001
            await ws.close()

    _run(_main())


# ── 规则3 + D-07:协议客户端全链路(真 agent WS 集成)──────────────────────────
def _fake_agent_app(conns: list[int]) -> aiohttp.web.Application:
    async def ws_audio(request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
        ws = aiohttp.web.WebSocketResponse()
        await ws.prepare(request)
        conns.append(1)
        await ws.send_str("hello")
        async for m in ws:
            if m.type == aiohttp.WSMsgType.BINARY:
                await ws.send_bytes(b"e:" + m.data)
            elif m.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                break
        return ws

    app = aiohttp.web.Application()
    app.router.add_get("/ws/audio", ws_audio)
    return app


def test_protocol_client_flow_and_immediate_release() -> None:
    """规则3:无 cookie /ws/audio → 直接分配 + 透传到真 agent;D-07:断开后 sweep 即回收
    (deadline=now,不享宽限窗)—— 收尾走真实 _sweep_loop,证明与浏览器超时同一路径。"""

    async def _main() -> None:
        conns: list[int] = []
        agent = TestServer(_fake_agent_app(conns))
        await agent.start_server()
        cfg = GatewayConfig(hmac_secret="s")
        pool = _FakePool([{"session_id": "proto1", "proc_id": "p1", "port": agent.port}])
        app, table, proxy = _mk(cfg, pool)
        sweep = asyncio.create_task(
            gwmain._sweep_loop(table, proxy, pool, interval=0.05)  # 真 sweep 驱动收尾
        )
        try:
            async with TestClient(TestServer(app)) as cli:
                ws = await cli.ws_connect("/ws/audio")  # 无 cookie = 协议客户端
                assert (await ws.receive()).data == "hello"  # 已透传到真 agent
                await ws.send_bytes(b"x")
                assert (await ws.receive()).data == b"e:x"
                assert table.get("proto1").browser is False  # 登记为协议客户端
                await ws.close()
                # D-07:断开 → PENDING(deadline=now)→ 下一 sweep tick 回收 release
                for _ in range(60):
                    if "proto1" in pool.released:
                        break
                    await asyncio.sleep(0.02)
                assert "proto1" in pool.released and table.get("proto1") is None
        finally:
            sweep.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweep
            await proxy.aclose()
            await agent.close()

    _run(_main())
