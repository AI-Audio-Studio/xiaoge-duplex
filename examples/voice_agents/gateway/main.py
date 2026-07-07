"""网关装配与启动(PR-C,§6):六路由规则 + Q6 准入 + R6 安全 + 宽限窗 sweep + TLS 终结。

单一对外端口(TLS 终结),内部反代到池管理器分配的 agent 进程。路由规则见 §6.1(六条):
1. `GET /` 无 cookie:准入(D-18)→ 池 alloc → 种亲和 cookie → 入口页;池满 → 繁忙页。
2. `/ws`·`/ws/audio`·`/api/*` 带 cookie:反代到绑定进程;cookie 无效/进程亡 → WS 4001 /
   HTTP 409(前端整页刷新,规则1 重分配)。
3. `GET /ws/audio` 无 cookie = 协议客户端:直接分配;池满回 WS busy(即断即杀,无宽限窗)。
4. `GET /ws` 无 cookie:明确拒绝(4001)。
5. 宽限窗(D-16):由 proxy 持有上游 + 本模块 sweep 循环驱动超时收尾。
6. 双标签页(R3):同 cookie 已有活跃音频 → 提示页 / WS 拒绝。

安全(§6.2):`/api/*` 白名单(mic/asr/tts,余 404);亲和 cookie Secure+HttpOnly+SameSite=Strict;
每连接帧大小+速率上限(proxy);错误响应不泄漏内部拓扑。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
import ssl
from typing import Any

import aiohttp.web as web

from gateway import affinity as af
from gateway.config import GatewayConfig
from gateway.pool_client import PoolClient
from gateway.proxy import Proxy

logger = logging.getLogger("gateway")

COOKIE = "xg_aff"  # 亲和 cookie
ACCESS_COOKIE = "xg_access"  # 准入凭证 cookie(D-18)
# §6.2 ①:白名单转发 mic/asr/tts。D-19/M5(asr/tts 服务器形态默认隐藏→404)属 agent 侧路由门控,
# **尚未实现**(PLAN §12.2 台账 M5 / 跟进 task);落地前 asr/tts 经此照常反代、由 agent 服务(非 404)。
_API_WHITELIST = {"mic", "asr", "tts"}
_SWEEP_INTERVAL_S = 2.0

_PAGE = "<!doctype html><meta charset=utf-8><title>小歌</title>"
_ENTRY_HTML = _PAGE + "<h3>小歌</h3><p>已就绪。</p>"  # 生产环境托管完整面板;此处最小占位
_BUSY_HTML = (  # 规则1 池满:静态繁忙页 + 自动重试(不依赖 WS 内 busy)
    _PAGE + "<h3>当前繁忙</h3><p>座位已满,正在重试…</p>"
    "<script>setTimeout(()=>location.reload(),5000)</script>"
)
_DOUBLE_TAB_HTML = _PAGE + "<h3>已在另一窗口通话</h3><p>请关闭其它标签页后重试。</p>"
_ACCESS_HTML = (  # D-18 最低准入表单
    _PAGE + "<h3>请输入访问口令</h3>"
    "<form method=post action=/access><input name=code type=password autofocus>"
    "<button>进入</button></form>"
)


def _html(body: str, status: int = 200) -> web.Response:
    return web.Response(text=body, content_type="text/html", status=status)


# ── Q6 准入(D-18)──────────────────────────────────────────────────────────────
def _access_token(secret: str) -> str:
    return hmac.new(secret.encode(), b"xg-access-granted", hashlib.sha256).hexdigest()[:32]


def _access_ok(request: web.Request, config: GatewayConfig) -> bool:
    if not config.access_required:
        return True
    return hmac.compare_digest(
        request.cookies.get(ACCESS_COOKIE, ""), _access_token(config.hmac_secret)
    )


def _set_affinity_cookie(resp: web.Response, value: str, config: GatewayConfig) -> None:
    # R6 ②:HttpOnly + SameSite=Strict;Secure 仅在 TLS 下(本地明文测试口 8787 需可用)。
    resp.set_cookie(
        COOKIE, value, httponly=True, samesite="Strict", secure=config.tls_enabled, path="/"
    )


class _Router:
    """六路由规则的处理器集合(持 config/table/proxy/pool,便于 TestClient 集成测)。"""

    def __init__(
        self, config: GatewayConfig, table: af.AffinityTable, proxy: Proxy, pool: PoolClient
    ) -> None:
        self._cfg = config
        self._table = table
        self._proxy = proxy
        self._pool = pool

    async def root(self, request: web.Request) -> web.Response:  # 规则1 / 6
        if not _access_ok(request, self._cfg):  # Q6 准入前置(D-18)
            return _html(_ACCESS_HTML, status=401)
        session = self._table.resolve(request.cookies.get(COOKIE, ""))
        if session is not None:  # 已有会话(刷新)——不重分配(规则2:两通道永不分家)
            if session.state == af.ACTIVE and session.audio_conns:  # 规则6:双标签页
                return _html(_DOUBLE_TAB_HTML, status=409)
            return _html(_ENTRY_HTML)
        info = await self._pool.alloc()  # 新用户 → 分配
        if info is None:  # 池满 → 繁忙页(规则1)
            return _html(_BUSY_HTML, status=503)
        self._table.register(info["session_id"], info["proc_id"], info["port"])
        resp = _html(_ENTRY_HTML)
        _set_affinity_cookie(resp, self._table.cookie_for(info["session_id"]) or "", self._cfg)
        return resp

    async def access(self, request: web.Request) -> web.Response:  # D-18 口令校验
        data = await request.post()
        code = str(data.get("code", ""))
        if not self._cfg.access_required or not hmac.compare_digest(code, self._cfg.access_code):
            return _html(_ACCESS_HTML, status=401)  # 不泄漏拓扑,仅提示重输
        resp = web.HTTPFound("/")  # HTTPException 亦是 Response,cookie 随 302 下发
        resp.set_cookie(
            ACCESS_COOKIE,
            _access_token(self._cfg.hmac_secret),
            httponly=True,
            samesite="Strict",
            secure=self._cfg.tls_enabled,
            path="/",
        )
        raise resp

    async def ws_audio(self, request: web.Request) -> web.WebSocketResponse:  # 规则2/3/5/6
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        cookie = request.cookies.get(COOKIE, "")
        if cookie:
            session = self._table.resolve(cookie)
            if session is None:  # 规则2:cookie 无效/进程亡 → 强制回页
                await ws.close(code=4001, message=b"affinity-lost")
                return ws
        else:  # 规则3:协议客户端,直接分配(共池)
            info = await self._pool.alloc()
            if info is None:  # 池满 → WS busy(PROTOCOL 语义)
                with contextlib.suppress(Exception):
                    await ws.send_str('{"type":"busy"}')
                await ws.close(code=1013, message=b"busy")
                return ws
            session = self._table.register(
                info["session_id"], info["proc_id"], info["port"], browser=False
            )
        result, session, conn_id = self._table.on_audio_connect(session.session_id)
        if result == af.CONNECT_REJECT_BUSY:  # 规则6:双标签页
            with contextlib.suppress(Exception):
                await ws.send_str('{"type":"busy","reason":"another_window"}')
            await ws.close(code=4002, message=b"another-window")
            return ws
        if result == af.CONNECT_REJECT_GONE:
            await ws.close(code=4001, message=b"affinity-lost")
            return ws
        # 断开后的收尾(浏览器宽限窗超时 / 协议客户端 D-07 即断)统一由 sweep 循环驱动,
        # 不放在请求 finally——连接取消会打断 finally 里的 await(release 漏做)。
        await self._proxy.handle_audio(ws, session, result, conn_id)
        return ws

    async def ws_state(self, request: web.Request) -> web.WebSocketResponse:  # 规则2/4
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        session = self._table.resolve(request.cookies.get(COOKIE, ""))
        if session is None:  # 规则4:无 cookie / 无效 → 拒绝(同规则2 关闭码)
            await ws.close(code=4001, message=b"affinity-lost")
            return ws
        await self._proxy.handle_ws_state(ws, session)
        return ws

    async def api(self, request: web.Request) -> web.Response:  # 规则2 + §6.2 ① 白名单
        top = request.match_info.get("tail", "").split("/", 1)[0]
        if top not in _API_WHITELIST:  # 未知路径默认拒绝,不泄漏拓扑
            return web.json_response({"error": "not found"}, status=404)
        session = self._table.resolve(request.cookies.get(COOKIE, ""))
        if session is None:  # 规则2:cookie 无效/进程亡 → 409 强制回页
            return web.json_response({"error": "session expired"}, status=409)
        return await self._proxy.proxy_http(request, session)

    async def healthz(self, _: web.Request) -> web.Response:
        return web.json_response({"ok": True, "pool": await self._pool.status()})


def build_gateway_app(
    config: GatewayConfig, table: af.AffinityTable, proxy: Proxy, pool: PoolClient
) -> web.Application:
    """装配路由(与启动/ TLS 解耦,便于 TestClient 集成测)。"""
    r = _Router(config, table, proxy, pool)
    app = web.Application()
    app.router.add_get("/", r.root)
    app.router.add_post("/access", r.access)
    app.router.add_get("/healthz", r.healthz)
    app.router.add_get("/ws", r.ws_state)
    app.router.add_get("/ws/audio", r.ws_audio)
    app.router.add_route("*", "/api/{tail:.*}", r.api)
    return app


async def _sweep_loop(
    table: af.AffinityTable, proxy: Proxy, pool: PoolClient, interval: float = _SWEEP_INTERVAL_S
) -> None:
    """宽限窗/即断即杀统一收尾(规则5 + D-07):PENDING 到期 → 关上游 + 池 release。"""
    while True:
        await asyncio.sleep(interval)
        try:  # N-1:单次迭代异常不得杀死 sweep 任务(否则所有宽限窗清理静默停摆),与 poolmgr 对齐
            for s in table.sweep_expired():
                await proxy.close_session_io(s.session_id)
                await pool.release(s.session_id, "grace timeout")
        except Exception:
            logger.exception("sweep iteration failed")


def _build_components(config: GatewayConfig) -> tuple[af.AffinityTable, Proxy, PoolClient]:
    table = af.AffinityTable(grace_seconds=config.grace_seconds, secret=config.hmac_secret)
    proxy = Proxy(config, table)
    pool = PoolClient(config.pool_api)
    return table, proxy, pool


async def run_gateway(config: GatewayConfig) -> None:
    """启动网关:装配 app + sweep 循环 + TLS 终结,常驻直到取消。"""
    table, proxy, pool = _build_components(config)
    app = build_gateway_app(config, table, proxy, pool)
    runner = web.AppRunner(app)
    await runner.setup()
    ssl_ctx: ssl.SSLContext | None = None
    if config.tls_enabled:
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.load_cert_chain(config.ssl_cert, config.ssl_key)
    site = web.TCPSite(runner, config.listen_host, config.listen_port, ssl_context=ssl_ctx)
    await site.start()
    scheme = "https" if config.tls_enabled else "http"
    logger.info("gateway listening on %s://%s:%d", scheme, config.listen_host, config.listen_port)
    sweep = asyncio.create_task(_sweep_loop(table, proxy, pool))
    try:
        await asyncio.Event().wait()  # 常驻
    finally:
        sweep.cancel()
        with contextlib.suppress(Exception):
            await sweep
        await proxy.aclose()
        await pool.close()
        await runner.cleanup()


def main(argv: Any = None) -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    config = GatewayConfig.from_env()
    try:
        asyncio.run(run_gateway(config))
    except KeyboardInterrupt:
        logger.info("gateway stopped")


if __name__ == "__main__":
    main()
