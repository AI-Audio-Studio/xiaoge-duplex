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
from gateway.apikey import ApiKeyStore  # noqa: E402
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
    config: GatewayConfig, pool: _FakePool, grace: float = 10.0
) -> tuple[aiohttp.web.Application, af.AffinityTable, Proxy]:
    table = af.AffinityTable(grace_seconds=grace, secret=config.hmac_secret)
    proxy = Proxy(config, table)
    apikeys = ApiKeyStore(config)  # 默认 required=False → 兼容模式恒放行,不影响既有断言
    app = gwmain.build_gateway_app(config, table, proxy, pool, apikeys)  # type: ignore[arg-type]
    return app, table, proxy


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _dead_port() -> int:
    """绑一个端口后立即释放 → 该端口此刻无人监听(用于制造真实的上游连接失败)。"""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _fake_agent_http_app(body: str = "<html>PANEL</html>") -> aiohttp.web.Application:
    """假 agent HTTP 面板:任意路径返回固定 HTML —— 供反代根路由(GET /)集成测取回上游页。"""

    async def any_path(request: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.Response(text=body, content_type="text/html")

    app = aiohttp.web.Application()
    app.router.add_route("*", "/{tail:.*}", any_path)
    return app


# ── 规则1:GET / 分配 / 繁忙 / 刷新 ────────────────────────────────────────────
def test_root_allocs_and_sets_cookie() -> None:
    async def _main() -> None:
        agent = TestServer(_fake_agent_http_app("<html>PANEL</html>"))
        await agent.start_server()
        cfg = GatewayConfig(hmac_secret="s")
        pool = _FakePool([{"session_id": "sess1", "proc_id": "p1", "port": agent.port}])
        app, _, proxy = _mk(cfg, pool)
        try:
            async with TestClient(TestServer(app)) as cli:
                r = await cli.get("/")
                assert r.status == 200
                assert gwmain.COOKIE in r.cookies  # 种亲和 cookie(规则1)
                assert "PANEL" in await r.text()  # 反代取回上游 agent 面板
                setck = "; ".join(r.headers.getall("Set-Cookie", []))  # R6②:cookie 属性
                assert "HttpOnly" in setck and "SameSite=Strict" in setck
        finally:
            await proxy.aclose()
            await agent.close()

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
        agent = TestServer(_fake_agent_http_app("<html>PANEL</html>"))
        await agent.start_server()
        cfg = GatewayConfig(hmac_secret="s")
        pool = _FakePool([{"session_id": "sess1", "proc_id": "p1", "port": 19100}])
        app, table, proxy = _mk(cfg, pool)
        table.register("sess1", "p1", agent.port)  # 已有会话,端口指向假 agent
        cookie = table.cookie_for("sess1")
        try:
            async with TestClient(TestServer(app)) as cli:
                r = await cli.get("/", cookies={gwmain.COOKIE: cookie})
                assert r.status == 200 and "PANEL" in await r.text()  # 刷新反代回页,不重分配
                assert len(pool._seats) == 1  # 池未被再分配(规则2:两通道不分家)
        finally:
            await proxy.aclose()
            await agent.close()

    _run(_main())


def test_root_refresh_allowed_even_with_active_audio() -> None:
    """规则6(docstring 6):根页面允许刷新——双标签页拒绝下移到 WS 层。同 cookie 会话即便
    已有活跃音频连接,GET / 仍反代回页(200),不返回页级 409。"""

    async def _main() -> None:
        agent = TestServer(_fake_agent_http_app("<html>PANEL</html>"))
        await agent.start_server()
        cfg = GatewayConfig(hmac_secret="s")
        app, table, proxy = _mk(cfg, _FakePool([]))
        table.register("sess1", "p1", agent.port)
        table.on_audio_connect("sess1")  # 变 ACTIVE + 有活跃音频连接
        cookie = table.cookie_for("sess1")
        try:
            async with TestClient(TestServer(app)) as cli:
                r = await cli.get("/", cookies={gwmain.COOKIE: cookie})
                assert r.status == 200 and "PANEL" in await r.text()  # 允许刷新,不落 409
        finally:
            await proxy.aclose()
            await agent.close()

    _run(_main())


# ── Q6 准入(D-18)──────────────────────────────────────────────────────────────
def test_access_gate_blocks_without_code() -> None:
    async def _main() -> None:
        agent = TestServer(_fake_agent_http_app("<html>PANEL</html>"))
        await agent.start_server()
        cfg = GatewayConfig(hmac_secret="s", access_code="letmein")
        pool = _FakePool([{"session_id": "s1", "proc_id": "p1", "port": agent.port}])
        app, _, proxy = _mk(cfg, pool)
        try:
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
                r2 = await cli.get("/")  # 客户端已带准入 cookie → 分配 + 反代回页
                assert r2.status == 200 and gwmain.COOKIE in r2.cookies
        finally:
            await proxy.aclose()
            await agent.close()

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


# ── apikey 准入(模式A):required=1 强制;缺/错拒 4401,命中放行 ──────────────────
def test_ws_audio_apikey_missing_rejected_4401() -> None:
    """required=1 且无 cookie(模式A):不带 apikey → 4401,且未触达 pool.alloc()。"""

    async def _main() -> None:
        cfg = GatewayConfig(hmac_secret="s", api_key_required=True, api_keys_static="sk-good")
        pool = _FakePool([{"session_id": "s1", "proc_id": "p1", "port": 19100}])
        app, _, _ = _mk(cfg, pool)
        async with TestClient(TestServer(app)) as cli:
            ws = await cli.ws_connect("/ws/audio")  # 无 cookie、无 apikey
            first = await ws.receive()  # 关闭前先下发错误文本帧
            assert first.type == aiohttp.WSMsgType.TEXT and '"code":1001' in first.data
            close = await ws.receive()
            assert close.type == aiohttp.WSMsgType.CLOSE and ws.close_code == 4401
            assert len(pool._seats) == 1  # 校验前置于 alloc,未占座
            await ws.close()

    _run(_main())


def test_ws_audio_apikey_valid_passes_gate() -> None:
    """required=1:带命中 apikey 通过准入门 → 继续 alloc(空池 → 1013 busy,非 4401),证明放行。"""

    async def _main() -> None:
        cfg = GatewayConfig(hmac_secret="s", api_key_required=True, api_keys_static="sk-good")
        app, _, _ = _mk(cfg, _FakePool([]))  # 空池:过门后 alloc 返回 None → busy
        async with TestClient(TestServer(app)) as cli:
            ws = await cli.ws_connect("/ws/audio", headers={"X-API-Key": "sk-good"})
            first = await ws.receive()  # 过门后池满 → busy 文本帧,而非 1001 鉴权失败
            assert first.type == aiohttp.WSMsgType.TEXT and first.data == '{"type":"busy"}'
            close = await ws.receive()
            assert close.type == aiohttp.WSMsgType.CLOSE and ws.close_code == 1013  # 过门 → 池满
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


# ── B-C-2:上游连接失败不得泄漏会话 / 锁死用户(真实失败路径)─────────────────────
def test_upstream_fail_releases_session_not_leak_or_lock() -> None:
    """B-C-2:agent 在 alloc 与浏览器 /ws/audio 连接之间死亡 → 上游连接失败。会话**不得永停
    ACTIVE**(否则 sweep 只扫 PENDING、永不 release,pool 槽泄漏 + 该 cookie 永久命中双标签页页、
    用户锁死)。制造真实失败:分配一个无人监听的端口。"""

    async def _main() -> None:
        cfg = GatewayConfig(hmac_secret="s")
        pool = _FakePool([])  # 会话直接登记(端口无人监听),不经反代根路由
        app, table, proxy = _mk(cfg, pool, grace=0.1)  # 小宽限窗,便于快速验 release
        table.register("s1", "p1", _dead_port())  # 端口此刻无人监听 → /ws/audio 上游连不上
        cookie = table.cookie_for("s1")
        sweep = asyncio.create_task(gwmain._sweep_loop(table, proxy, pool, interval=0.03))
        try:
            async with TestClient(TestServer(app)) as cli:
                ws = await cli.ws_connect(  # 携 cookie → FRESH → 上游连不上
                    "/ws/audio", headers={"Cookie": f"{gwmain.COOKIE}={cookie}"}
                )
                msg = await ws.receive()
                assert msg.type == aiohttp.WSMsgType.CLOSE  # 网关回 1011 关闭
                with contextlib.suppress(Exception):
                    await ws.close()
                # 修复核心:会话转 PENDING(非永停 ACTIVE)→ sweep release
                for _ in range(80):
                    if "s1" in pool.released:
                        break
                    await asyncio.sleep(0.02)
                assert "s1" in pool.released  # 已 release(未泄漏)
                assert table.get("s1") is None  # 已移出表(未永停 ACTIVE)
                assert table.resolve(cookie) is None  # cookie 不再命中(未锁死用户)
        finally:
            sweep.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweep
            await proxy.aclose()

    _run(_main())
