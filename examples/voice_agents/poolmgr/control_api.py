"""池管理器本地控制 API(PR-B,P-2)。

网关经此 HTTP JSON API 调度进程——**只调 API,不直接 spawn**(职责边界,v4 §5):
  - `POST /alloc`   → 200 {proc_id, port, session_id} | 503 {error} 池满繁忙
  - `POST /release` {session_id, reason} → 200 {ok: bool}
  - `GET  /status`  → 200 {size, ready, assigned, spawning, ready_below_threshold, transcoder?}
  - `GET  /list_ready` → 200 {ports: [{proc_id, port, state}, ...]}  (只读,无亲和路由用)

**只绑 127.0.0.1(M3)**:内部端口无 TLS 无鉴权,外网不可达;网关是唯一调用方。
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp.web

logger = logging.getLogger("poolmgr-control")


def build_control_app(manager: Any) -> aiohttp.web.Application:
    """把 PoolManager(或鸭子类型等价物)包装成 aiohttp 控制 app。"""

    async def _alloc(request: aiohttp.web.Request) -> aiohttp.web.Response:
        result = manager.alloc()
        if result is None:
            return aiohttp.web.json_response({"error": "pool busy"}, status=503)
        return aiohttp.web.json_response(result)

    async def _release(request: aiohttp.web.Request) -> aiohttp.web.Response:
        try:
            data = await request.json()
        except Exception:
            data = {}
        session_id = str(data.get("session_id", "")).strip()
        if not session_id:
            return aiohttp.web.json_response({"error": "session_id required"}, status=400)
        ok = bool(manager.release(session_id, str(data.get("reason", ""))))
        return aiohttp.web.json_response({"ok": ok})

    async def _status(request: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.json_response(manager.status())

    async def _list_ready(request: aiohttp.web.Request) -> aiohttp.web.Response:
        # 只读端口发现:无亲和路由(/knows 等)用此拿 READY 端口,不走 alloc/release
        # (release 会 kill 进程,/knows 高频调用会清空整个池)。
        return aiohttp.web.json_response({"ports": manager.list_ready()})

    app = aiohttp.web.Application()
    app.router.add_post("/alloc", _alloc)
    app.router.add_post("/release", _release)
    app.router.add_get("/status", _status)
    app.router.add_get("/list_ready", _list_ready)
    return app


def serve(manager: Any, *, host: str = "127.0.0.1", port: int = 19000) -> None:
    """阻塞式起控制 API。host 固定 127.0.0.1(M3);外部传入非本地地址将拒绝。"""
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(f"control API must bind loopback only (M3), got {host!r}")
    logger.info("pool control API on http://%s:%d", host, port)
    aiohttp.web.run_app(build_control_app(manager), host=host, port=port, print=None)
