"""网关装配与启动(PR-C,§6):六路由规则 + Q6 准入 + R6 安全 + 宽限窗 sweep + TLS 终结。

单一对外端口(TLS 终结),内部反代到池管理器分配的 agent 进程。路由规则见 §6.1(六条):
1. `GET /` 无 cookie:准入(D-18)→ 池 alloc → 种亲和 cookie → 入口页;池满 → 繁忙页。
2. `/ws`·`/ws/audio`·`/api/*` 带 cookie:反代到绑定进程;cookie 无效/进程亡 → WS 4001 /
   HTTP 409(前端整页刷新,规则1 重分配)。
3. `GET /ws/audio` 无 cookie = 协议客户端:直接分配;池满回 WS busy(即断即杀,无宽限窗)。
4. `GET /ws` 无 cookie:明确拒绝(4001)。
5. 宽限窗(D-16):由 proxy 持有上游 + 本模块 sweep 循环驱动超时收尾。
6. 双标签页(R3):根页面允许刷新;同 cookie 已有活跃音频时由 WS 层拒绝第二路音频。

安全(§6.2):`/api/*` 白名单(mic/asr/tts/knowledge,余 404);亲和 cookie Secure+HttpOnly+SameSite=Strict;
每连接帧大小+速率上限(proxy);错误响应不泄漏内部拓扑。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import random
import ssl
import time
from pathlib import Path
from typing import Any

import aiohttp
import aiohttp.web as web

from gateway import affinity as af
from gateway.apikey import ApiKeyStore
from gateway.config import GatewayConfig
from gateway.pool_client import PoolClient
from gateway.proxy import _HOP_HEADERS, Proxy

logger = logging.getLogger("gateway")

COOKIE = "xg_aff"  # 亲和 cookie
ACCESS_COOKIE = "xg_access"  # 准入凭证 cookie(D-18)
# §6.2 ①:白名单转发 mic/asr/tts。asr/tts 隐藏由 agent 侧 XIAOGE_ADMIN_ROUTES 门控(M5/D-19:
# 池管理器服务器形态注入 0 → 不注册 → 404;本地默认显示);网关照常反代、取回 agent 的 404 原样回。
# knowledge 已迁至独立 /api/knows/* 路由(apikey 准入 + 无亲和反代,见 _Router.knows_api)。
_API_WHITELIST = {"mic", "asr", "tts"}
_SWEEP_INTERVAL_S = 2.0
_PENDING_SESSION_TTL_S = 30.0

_PAGE = "<!doctype html><meta charset=utf-8><title>小歌</title>"
_ENTRY_HTML = _PAGE + "<h3>小歌</h3><p>已就绪。</p>"  # 静态文件异常时的兜底页
_BUSY_HTML = (  # 规则1 池满:静态繁忙页 + 自动重试(不依赖 WS 内 busy)
    _PAGE + "<h3>当前繁忙</h3><p>座位已满,正在重试…</p>"
    "<script>setTimeout(()=>location.reload(),5000)</script>"
)
_ACCESS_HTML = (  # D-18 最低准入表单
    _PAGE + "<h3>请输入访问口令</h3>"
    "<form method=post action=/access><input name=code type=password autofocus>"
    "<button>进入</button></form>"
)


class _PathOnlyAccessLogger(web.AbstractAccessLogger):
    """Log the path but never the query string, which may contain rejected secrets."""

    def log(self, request: web.BaseRequest, response: web.StreamResponse, time: float) -> None:
        self.logger.info(
            "%s %s %s %s %.3fs",
            request.remote or "-",
            request.method,
            request.path,
            response.status,
            time,
        )


def _html(body: str, status: int = 200) -> web.Response:
    return web.Response(text=body, content_type="text/html", status=status)


def _index_html(*, debug_query_token: bool) -> str:
    try:
        html = (
            Path(__file__).resolve().parents[1] / "webpanel" / "static" / "index.html"
        ).read_text(encoding="utf-8")
        return html.replace(
            "var DEMO_QUERY_TOKEN_ENABLED=false;",
            f"var DEMO_QUERY_TOKEN_ENABLED={str(debug_query_token).lower()};",
        )
    except Exception:
        logger.exception("failed to read webpanel index")
        return _ENTRY_HTML


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


def _bearer_token(request: web.Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth.removeprefix("Bearer ").strip()
    return ""


def _api_key_from_request_headers(request: web.Request) -> str:
    presented = request.headers.get("X-API-Key") or request.headers.get("X-Api-Key")
    if presented:
        return presented.strip()
    auth = request.headers.get("Authorization", "").strip()
    if auth.startswith("ApiKey "):
        return auth.removeprefix("ApiKey ").strip()
    return ""


async def _api_key_from_create_session_request(request: web.Request) -> str:
    presented = _api_key_from_request_headers(request)
    if presented:
        return presented
    try:
        body = await request.json()
    except Exception:
        return ""
    credential = body.get("credential")
    if isinstance(credential, str):
        return credential.strip()
    if isinstance(credential, dict):
        for key in ("api_key", "apikey", "key"):
            value = credential.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    for key in ("api_key", "apikey"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


async def _is_legacy_webpanel_create_session(request: web.Request) -> bool:
    try:
        body = await request.json()
    except Exception:
        return False
    return body.get("device_id") == "web-panel-x3" and body.get("connect_reason") != "call_button"


class _Router:
    """六路由规则的处理器集合(持 config/table/proxy/pool,便于 TestClient 集成测)。"""

    def __init__(
        self,
        config: GatewayConfig,
        table: af.AffinityTable,
        proxy: Proxy,
        pool: PoolClient,
        apikeys: ApiKeyStore,
    ) -> None:
        self._cfg = config
        self._table = table
        self._proxy = proxy
        self._pool = pool
        self._apikeys = apikeys
        self._token_sessions: dict[str, str] = {}
        self._token_issued_at: dict[str, float] = {}
        self._active_session_ws: set[str] = set()

    async def root(self, request: web.Request) -> web.Response:  # 规则1 / 6
        if not _access_ok(request, self._cfg):  # Q6 准入前置(D-18)
            return _html(_ACCESS_HTML, status=401)
        # R5.2.2: 页面加载本身不占池；只有 create_session 才分配 agent。
        resp = _html(_index_html(debug_query_token=self._cfg.webpanel_debug_query_token))
        resp.headers["Cache-Control"] = "no-store"
        resp.del_cookie(COOKIE, path="/")
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
        else:  # 规则3:协议客户端(模式A),apikey 准入 → 直接分配(共池)
            presented = _api_key_from_request_headers(request)
            if not self._apikeys.authorize(presented):
                with contextlib.suppress(Exception):
                    await ws.send_str('{"type":"error","code":1001,"message":"auth failed"}')
                await ws.close(code=4401, message=b"unauthorized")
                return ws
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

    async def create_session(self, request: web.Request) -> web.Response:
        if await _is_legacy_webpanel_create_session(request):
            return web.json_response({"code": "call_required"}, status=403)
        presented = await _api_key_from_create_session_request(request)
        if not self._apikeys.authorize(presented):
            return web.json_response({"code": "auth_failed"}, status=401)
        await self._cleanup_pending_sessions()
        info = await self._pool.alloc()
        if info is None:
            await self._cleanup_pending_sessions(force=True)
            info = await self._alloc_after_cleanup()
        if info is None:
            return web.json_response({"code": "resource_exhausted"}, status=503)
        session = self._table.register(
            info["session_id"], info["proc_id"], info["port"], browser=False
        )
        resp = await self._proxy.proxy_http(request, session)
        if resp.status != 200:
            self._table.close(session.session_id)
            await self._pool.release(session.session_id, "create_session rejected")
            return resp
        try:
            body = json.loads(resp.body or b"{}")
            token = str(body.get("access_token") or "")
        except Exception:
            body = {}
            token = ""
        if not token:
            self._table.close(session.session_id)
            await self._pool.release(session.session_id, "create_session bad response")
            return web.json_response({"code": "protocol_error"}, status=502)
        self._token_sessions[token] = session.session_id
        self._token_issued_at[token] = time.monotonic()
        scheme = "wss" if request.secure else "ws"
        body["ws_url"] = f"{scheme}://{request.host}/ws/session"
        resp = web.json_response(body)
        _set_affinity_cookie(resp, self._table.cookie_for(session.session_id) or "", self._cfg)
        return resp

    async def ws_session(self, request: web.Request) -> web.WebSocketResponse:
        token = "" if "access_token" in request.query else _bearer_token(request)
        return await self._serve_ws_session(request, token)

    async def debug_ws_session(self, request: web.Request) -> web.WebSocketResponse:
        token = request.query.get("access_token", "").strip()
        return await self._serve_ws_session(request, token)

    async def _serve_ws_session(self, request: web.Request, token: str) -> web.WebSocketResponse:
        await self._cleanup_pending_sessions()
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        session_id = self._token_sessions.get(token)
        session = self._table.get(session_id or "") if session_id else None
        if session is None:
            await ws.close(code=4401, message=b"auth_failed")
            return ws
        if session.session_id in self._active_session_ws:
            await ws.close(code=4009, message=b"duplicate_connection")
            return ws
        self._active_session_ws.add(session.session_id)
        self._token_issued_at.pop(token, None)
        try:
            await self._proxy.handle_ws_session(ws, session, request, access_token=token)
        finally:
            self._active_session_ws.discard(session.session_id)
            self._token_sessions.pop(token, None)
            self._token_issued_at.pop(token, None)
            self._table.close(session.session_id)
            await self._pool.release(session.session_id, "ws session ended")
        return ws

    async def _cleanup_pending_sessions(self, *, force: bool = False) -> None:
        now = time.monotonic()
        expired = [
            (token, session_id)
            for token, session_id in self._token_sessions.items()
            if token in self._token_issued_at
            and (force or now - self._token_issued_at[token] >= _PENDING_SESSION_TTL_S)
        ]
        for token, session_id in expired:
            self._token_sessions.pop(token, None)
            self._token_issued_at.pop(token, None)
            if self._table.close(session_id) is not None:
                await self._pool.release(session_id, "create_session pending timeout")

    async def _alloc_after_cleanup(self) -> dict[str, Any] | None:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            info = await self._pool.alloc()
            if info is not None:
                return info
            await asyncio.sleep(0.25)
        return None

    async def api(self, request: web.Request) -> web.Response:  # 规则2 + §6.2 ① 白名单
        top = request.match_info.get("tail", "").split("/", 1)[0]
        if top not in _API_WHITELIST:  # 未知路径默认拒绝,不泄漏拓扑
            return web.json_response({"error": "not found"}, status=404)
        session = self._table.resolve(request.cookies.get(COOKIE, ""))
        if session is None:  # 规则2:cookie 无效/进程亡 → 409 强制回页
            return web.json_response({"error": "session expired"}, status=409)
        return await self._proxy.proxy_http(request, session)

    # ── /knows 知识库独立管理(apikey 准入 + 无亲和反代)──────────────────────────
    async def _pick_any_port(self) -> int | None:
        """从池里取一个 READY 端口(不 alloc/release,不占槽不杀进程)。

        /knows 是高频运维请求,绝不能走 pool.alloc/release——alloc 占 ASSIGNED 槽,
        release 触发 _recycle **kill 进程并重启**(manager.py:185),高频请求会清空整个池。
        用 list_ready 拿 READY 端口,任意选一个;空时 fallback 取已分配端口(罕见,启动初期)。
        """
        ports = await self._pool.list_ready()
        if ports:
            return int(random.choice(ports)["port"])
        # 兜底:pool 全 SPAWNING/RECYCLING 时取一个 ASSIGNED 端口(避免 503)
        status = await self._pool.status()
        if status.get("assigned", 0) > 0:
            # list_ready 只返 READY,ASSIGNED 取不到端口——只能再等等 READY
            return None
        return None

    async def _proxy_to_port(self, request: web.Request, port: int, *, prefix: str) -> web.Response:
        """无亲和反代:把 request 透传到 http://127.0.0.1:{port}{prefix}{tail}。

        与 Proxy.proxy_http 的区别:不依赖 Session(无 cookie 亲和);每次请求新建上游连接
        (够用,知识库不是高频热路径)。逐跳头剔除 + body 原样转发 + 响应原样回。
        """
        tail = request.match_info.get("tail", "")
        url = f"http://127.0.0.1:{port}{prefix}{tail}"
        if request.query_string:
            url = f"{url}?{request.query_string}"
        sess = aiohttp.ClientSession()
        try:
            fwd = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_HEADERS}
            body = await request.read()
            try:
                async with sess.request(request.method, url, data=body, headers=fwd) as up:
                    data = await up.read()
                    return web.Response(status=up.status, body=data, content_type=up.content_type)
            except Exception as exc:
                logger.warning("knows proxy upstream failed port=%d: %s", port, exc)
                return web.json_response({"error": f"upstream unavailable: {exc}"}, status=502)
        finally:
            await sess.close()

    async def knows_page(self, request: web.Request) -> web.Response:
        """GET /knows:apikey 准入 → 选任意 READY agent → 反代 webpanel 的 /knows 页面。

        浏览器加载 HTML 时无法带自定义头,所以这条路由额外支持 query string
        `?apikey=<KEY>`(仅页面本身,/api/knows/* 仍只认头)。knows.html 加载后
        会把 key 写入 localStorage 并清掉 URL 上的 token,避免泄露到 history。
        """
        presented = _api_key_from_request_headers(request) or request.query.get("apikey") or request.query.get("api_key")
        if not self._apikeys.authorize(presented):
            return web.json_response({"code": "auth_failed"}, status=401)
        port = await self._pick_any_port()
        if port is None:
            return web.json_response({"code": "resource_exhausted"}, status=503)
        return await self._proxy_to_port(request, port, prefix="/knows")

    async def knows_api(self, request: web.Request) -> web.Response:
        """* /api/knows/{tail}:apikey 准入 → 选任意 READY agent → 反代 webpanel 的 /api/knows/{tail}。

        无亲和:任意 agent 都能写/读 user_knowledge.md(文件共享),rebuild 后 meta.json mtime
        变化触发其他 agent 的 _maybe_reload 热更新。高频运维请求,**不走 alloc/release**
        (release 会 kill 进程,见 _pick_any_port 注释)。
        """
        presented = _api_key_from_request_headers(request)
        if not self._apikeys.authorize(presented):
            return web.json_response({"code": "auth_failed"}, status=401)
        port = await self._pick_any_port()
        if port is None:
            return web.json_response({"code": "resource_exhausted"}, status=503)
        return await self._proxy_to_port(request, port, prefix="/api/knows/")

    async def healthz(self, _: web.Request) -> web.Response:
        return web.json_response({"ok": True, "pool": await self._pool.status()})


def build_gateway_app(
    config: GatewayConfig,
    table: af.AffinityTable,
    proxy: Proxy,
    pool: PoolClient,
    apikeys: ApiKeyStore | None = None,
) -> web.Application:
    """装配路由(与启动/ TLS 解耦,便于 TestClient 集成测)。"""
    apikeys = apikeys or ApiKeyStore(config)
    r = _Router(config, table, proxy, pool, apikeys)
    app = web.Application()
    app.router.add_get("/", r.root)
    app.router.add_post("/access", r.access)
    app.router.add_post("/create_session", r.create_session)
    app.router.add_get("/healthz", r.healthz)
    app.router.add_get("/ws/session", r.ws_session)
    if config.webpanel_debug_query_token:
        app.router.add_get("/debug/ws/session", r.debug_ws_session)
    app.router.add_get("/ws", r.ws_state)
    app.router.add_get("/ws/audio", r.ws_audio)
    # 知识库独立管理界面:apikey 准入 + 无亲和反代。必须在 /api/{tail:.*} 之前注册,
    # 否则 /api/knows/list 会被 r.api(白名单 + cookie 亲和)拦截 → 404/409。
    app.router.add_get("/knows", r.knows_page)
    app.router.add_route("*", "/api/knows/{tail:.*}", r.knows_api)
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


def _build_components(
    config: GatewayConfig,
) -> tuple[af.AffinityTable, Proxy, PoolClient, ApiKeyStore]:
    table = af.AffinityTable(grace_seconds=config.grace_seconds, secret=config.hmac_secret)
    proxy = Proxy(config, table)
    pool = PoolClient(config.pool_api)
    apikeys = ApiKeyStore(config)
    return table, proxy, pool, apikeys


async def run_gateway(config: GatewayConfig) -> None:
    """启动网关:装配 app + sweep 循环 + TLS 终结,常驻直到取消。"""
    table, proxy, pool, apikeys = _build_components(config)
    await apikeys.refresh()  # 启动即载一次有效集合(失败保留空快照,由刷新循环补齐)
    app = build_gateway_app(config, table, proxy, pool, apikeys)
    runner = web.AppRunner(app, access_log_class=_PathOnlyAccessLogger)
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
    refresh = asyncio.create_task(apikeys.run_refresh_loop())  # apikey 有效集合后台刷新
    try:
        await asyncio.Event().wait()  # 常驻
    finally:
        for task in (sweep, refresh):
            task.cancel()
            with contextlib.suppress(Exception):
                await task
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
