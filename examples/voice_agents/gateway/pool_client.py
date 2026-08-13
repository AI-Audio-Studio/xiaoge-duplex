"""网关侧池控制 API 客户端(PR-C,P-2)。

异步 HTTP 调池管理器 `control_api` 的 `/alloc`·`/release`·/status`——**网关只经此 API 调度,
不直接 spawn 进程**(职责边界,v4 §5)。所有错误吞成安全默认(alloc→None 视同繁忙、
release→False、status→{}),绝不让池侧抖动打挂网关。
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

logger = logging.getLogger("gateway-poolclient")


class PoolClient:
    def __init__(self, base_url: str, *, timeout: float = 3.0) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=float(timeout))
        self._session: aiohttp.ClientSession | None = None

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def alloc(self) -> dict[str, Any] | None:
        """POST /alloc → {proc_id, port, session_id};503 繁忙 / 任何错误 → None。"""
        try:
            sess = await self._sess()
            async with sess.post(f"{self._base}/alloc") as r:
                return await r.json() if r.status == 200 else None
        except Exception as exc:
            logger.warning("pool alloc failed: %s", exc)
            return None

    async def release(self, session_id: str, reason: str = "") -> bool:
        """POST /release → ok。"""
        try:
            sess = await self._sess()
            payload = {"session_id": session_id, "reason": reason}
            async with sess.post(f"{self._base}/release", json=payload) as r:
                return bool((await r.json()).get("ok")) if r.status == 200 else False
        except Exception as exc:
            logger.warning("pool release failed: %s", exc)
            return False

    async def status(self) -> dict[str, Any]:
        try:
            sess = await self._sess()
            async with sess.get(f"{self._base}/status") as r:
                return await r.json() if r.status == 200 else {}
        except Exception as exc:
            logger.warning("pool status failed: %s", exc)
            return {}

    async def list_ready(self) -> list[dict[str, Any]]:
        """GET /list_ready → [{proc_id, port, state}, ...];失败/无就绪 → []。

        无亲和路由(/knows 等)用此取可用 agent 端口,**绕开 alloc/release 语义**
        (alloc 占槽、release 杀进程,/knows 高频调用会清空整个池)。
        """
        try:
            sess = await self._sess()
            async with sess.get(f"{self._base}/list_ready") as r:
                if r.status != 200:
                    return []
                data = await r.json()
                return list(data.get("ports", []))
        except Exception as exc:
            logger.warning("pool list_ready failed: %s", exc)
            return []

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
