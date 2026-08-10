"""反向代理:HTTP 反代 + WS 双向透传泵 + **宽限窗上游持有**(PR-C,T2/D-16/R6)。

核心 = T2:客户端 `/ws/audio` 断开时**不关内部上游 WS**——把它持在会话上,浏览器重连(REATTACH)
时把新客户端 WS **接回同一上游**(帧续接,agent 无感)。上游→客户端泵长活(宽限窗内无客户端
则丢帧,D-16 不回放);客户端→上游泵随每条客户端连接生灭。上游连接带 `X-XG-Session` 头(agent
#3 据此在真结束时退出)。R6:单帧大小上限。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import aiohttp
import aiohttp.web

from gateway import affinity as af

logger = logging.getLogger("gateway-proxy")

# 反代 HTTP 不转发的逐跳头。
_HOP_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "keep-alive"}


class _RateLimiter:
    """令牌桶:每连接消息速率上限(R6 ③)。clock 可注入以便确定性测试。"""

    def __init__(self, rate_per_s: float, clock: Any = time.monotonic) -> None:
        self._rate = max(1.0, float(rate_per_s))
        self._clock = clock
        self._tokens = self._rate
        self._last = clock()

    def allow(self) -> bool:
        now = self._clock()
        self._tokens = min(self._rate, self._tokens + (now - self._last) * self._rate)
        self._last = now
        if self._tokens < 1.0:
            return False
        self._tokens -= 1.0
        return True


@dataclass
class _SessionIO:
    upstream: aiohttp.ClientWebSocketResponse  # 持久上游(ACTIVE+PENDING 期不断,T2)
    up2cli_task: asyncio.Task | None = None
    client: aiohttp.web.WebSocketResponse | None = None  # 当前客户端 WS(重连可换)
    closing: bool = False


class Proxy:
    def __init__(self, config: Any, table: af.AffinityTable) -> None:
        self._cfg = config
        self._table = table
        self._io: dict[str, _SessionIO] = {}
        self._session: aiohttp.ClientSession | None = None

    async def _client(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _open_upstream(self, session: af.Session) -> aiohttp.ClientWebSocketResponse:
        sess = await self._client()
        # X-XG-Session 让 agent(webpanel #3)把这条连接识别为真实会话:真结束时才退出进程。
        return await sess.ws_connect(
            f"http://127.0.0.1:{session.port}/ws/audio",
            headers={"X-XG-Session": session.session_id},
            heartbeat=30,
        )

    async def _open_session_upstream(
        self,
        session: af.Session,
        request: aiohttp.web.Request,
        access_token: str,
    ) -> aiohttp.ClientWebSocketResponse:
        sess = await self._client()
        fwd = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_HEADERS}
        fwd["X-XG-Session"] = session.session_id
        fwd["Authorization"] = f"Bearer {access_token}"
        return await sess.ws_connect(
            f"http://127.0.0.1:{session.port}/ws/session",
            headers=fwd,
            heartbeat=30,
        )

    # ── /ws/audio(浏览器音频通道 + 协议客户端)──────────────────────────────
    async def handle_audio(
        self,
        client_ws: aiohttp.web.WebSocketResponse,
        session: af.Session,
        result: str,
        conn_id: str,
    ) -> None:
        """一条 /ws/audio 客户端连接的生命周期。FRESH:建上游 + 起上游→客户端泵;
        REATTACH:接回既有上游、换到新客户端(帧续接)。返回即本客户端连接结束。"""
        sid = session.session_id
        io = self._io.get(sid)
        if result == af.CONNECT_FRESH or io is None:
            try:
                up = await self._open_upstream(session)
            except Exception as exc:
                logger.warning("upstream connect failed sid=%s: %s", sid, exc)
                # B-C-2:上游失败也必须解绑本连接 → 会话转 PENDING → sweep release + cookie 解锁;
                # 否则会话永停 ACTIVE(sweep 只扫 PENDING),槽泄漏且该 cookie 永久命中双标签页页。
                self._table.on_audio_disconnect(sid, conn_id)
                await client_ws.close(code=1011, message=b"upstream unavailable")
                return
            io = _SessionIO(upstream=up, client=client_ws)
            self._io[sid] = io
            io.up2cli_task = asyncio.create_task(self._pump_up2cli(sid))
        else:  # REATTACH:上游不动,只把泵改投新客户端(T2 帧续接)
            io.client = client_ws
        await self._pump_cli2up(sid, client_ws, conn_id)

    async def _pump_cli2up(
        self, sid: str, client_ws: aiohttp.web.WebSocketResponse, conn_id: str
    ) -> None:
        """客户端→上游(随本连接生灭)。连接结束**不关上游**,只解绑当前客户端 + 触发宽限窗。"""
        io = self._io.get(sid)
        limiter = _RateLimiter(self._cfg.msg_rate_per_s)  # R6 ③:每连接消息速率上限
        try:
            async for msg in client_ws:
                if io is None or io.closing:
                    break
                if not limiter.allow():  # 速率超限 → 断开该连接(宽限窗接管重连)
                    logger.warning("msg rate exceeded sid=%s", sid)
                    break
                if msg.type == aiohttp.WSMsgType.BINARY:
                    if len(msg.data) > self._cfg.max_frame_bytes:  # R6:超限帧 → 断开该连接
                        logger.warning("frame too large sid=%s (%d bytes)", sid, len(msg.data))
                        break
                    await io.upstream.send_bytes(msg.data)
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    await io.upstream.send_str(msg.data)
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                    break
        finally:
            if io is not None and io.client is client_ws:
                io.client = None  # 解绑:上游→客户端泵转入丢帧(宽限窗静默)
            self._table.on_audio_disconnect(sid, conn_id)  # 最后一条断 → PENDING_DISCONNECT

    async def _pump_up2cli(self, sid: str) -> None:
        """上游→客户端(随上游长活)。无当前客户端(宽限窗内)→ 丢帧(D-16 不回放缺失段)。"""
        io = self._io.get(sid)
        if io is None:
            return
        try:
            async for msg in io.upstream:
                if io.closing:
                    break
                cli = io.client
                if cli is not None and not cli.closed:
                    if msg.type == aiohttp.WSMsgType.BINARY:
                        await cli.send_bytes(msg.data)
                    elif msg.type == aiohttp.WSMsgType.TEXT:
                        await cli.send_str(msg.data)
                if msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                    break
        except Exception as exc:
            logger.info("up2cli pump ended sid=%s: %s", sid, exc)

    async def close_session_io(self, sid: str) -> None:
        """宽限窗超时/会话关闭:关上游 + 停泵(网关侧收尾;池 release 由调用方另做)。"""
        io = self._io.pop(sid, None)
        if io is None:
            return
        io.closing = True
        if io.up2cli_task is not None:
            io.up2cli_task.cancel()
        with contextlib.suppress(Exception):
            await io.upstream.close()

    async def handle_ws_state(
        self, client_ws: aiohttp.web.WebSocketResponse, session: af.Session
    ) -> None:
        """反代 /ws 状态通道:简单双向泵(**无宽限窗**——状态通道无独立于页面的复用语义,
        任一端断即收)。断开后对 IDLE 会话调 on_state_disconnect → sweep 回收槽位。"""
        sid = session.session_id
        sess = await self._client()
        try:
            up = await sess.ws_connect(
                f"http://127.0.0.1:{session.port}/ws",
                headers={"X-XG-Session": session.session_id},
                heartbeat=30,
            )
        except Exception:
            await client_ws.close(code=1011, message=b"upstream unavailable")
            self._table.on_state_disconnect(sid)
            return
        self._table.on_state_connect(sid)  # 状态连接建立:清零 idle 计时

        async def _pump(src: Any, dst: Any) -> None:
            with contextlib.suppress(Exception):
                async for m in src:
                    if m.type == aiohttp.WSMsgType.TEXT:
                        await dst.send_str(m.data)
                    elif m.type == aiohttp.WSMsgType.BINARY:
                        await dst.send_bytes(m.data)
                    elif m.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                        break

        c2u = asyncio.create_task(_pump(client_ws, up))
        u2c = asyncio.create_task(_pump(up, client_ws))
        try:
            await asyncio.wait({c2u, u2c}, return_when=asyncio.FIRST_COMPLETED)
            for t in (c2u, u2c):
                t.cancel()
            with contextlib.suppress(Exception):
                await up.close()
        finally:
            self._table.on_state_disconnect(
                sid
            )  # CancelledError 时也必须执行,否则 state_idle_since 停在 0.0 永不 sweep

    async def handle_ws_session(
        self,
        client_ws: aiohttp.web.WebSocketResponse,
        session: af.Session,
        request: aiohttp.web.Request,
        *,
        access_token: str,
    ) -> None:
        """R5.2.2 合并 WSS:JSON control/data + PCM binary 原样双向透传。"""
        try:
            up = await self._open_session_upstream(session, request, access_token)
        except Exception:
            await client_ws.close(code=1011, message=b"upstream unavailable")
            return

        async def _pump(src: Any, dst: Any, *, validate_client_frames: bool = False) -> int:
            limiter = _RateLimiter(self._cfg.msg_rate_per_s)
            close_code = 1000
            try:
                async for m in src:
                    if not limiter.allow():
                        close_code = int(aiohttp.WSCloseCode.POLICY_VIOLATION)
                        break
                    if m.type == aiohttp.WSMsgType.TEXT:
                        if validate_client_frames:
                            if len(m.data.encode("utf-8")) > self._cfg.max_frame_bytes:
                                return 4400
                            try:
                                json.loads(m.data)
                            except json.JSONDecodeError:
                                return 4400
                        await dst.send_str(m.data)
                    elif m.type == aiohttp.WSMsgType.BINARY:
                        if len(m.data) > self._cfg.max_frame_bytes:
                            close_code = int(aiohttp.WSCloseCode.MESSAGE_TOO_BIG)
                            break
                        await dst.send_bytes(m.data)
                    elif m.type in (
                        aiohttp.WSMsgType.ERROR,
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSING,
                    ):
                        close_code = getattr(src, "close_code", None) or close_code
                        break
            except Exception as exc:
                logger.info("session pump ended: %s", exc)
                close_code = getattr(src, "close_code", None) or 1011
            return int(getattr(src, "close_code", None) or close_code)

        c2u = asyncio.create_task(_pump(client_ws, up, validate_client_frames=True))
        u2c = asyncio.create_task(_pump(up, client_ws))
        tasks = {c2u, u2c}
        close_code = 1000
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            codes = [task.result() for task in done]
            close_code = next((code for code in codes if code != 1000), codes[0])
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # No reader may remain on client_ws while close() waits for the peer close reply.
            # Otherwise the reader can consume that reply and turn a valid 44xx close into 1006.
            with contextlib.suppress(Exception):
                await up.close(code=close_code)
            if not client_ws.closed:
                with contextlib.suppress(Exception):
                    await client_ws.close(code=close_code)

    # ── HTTP 反代(POST /api/*)─────────────────────────────────────────────────
    async def proxy_http(
        self, request: aiohttp.web.Request, session: af.Session
    ) -> aiohttp.web.Response:
        """反代到上游 agent(路径原样)。逐跳头剔除。"""
        sess = await self._client()
        url = f"http://127.0.0.1:{session.port}{request.rel_url}"
        fwd = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_HEADERS}
        # Never trust a client-supplied affinity marker; overwrite it after authentication.
        fwd["X-XG-Session"] = session.session_id
        body = await request.read()
        async with sess.request(request.method, url, data=body, headers=fwd) as up:
            data = await up.read()
            return aiohttp.web.Response(status=up.status, body=data, content_type=up.content_type)

    async def aclose(self) -> None:
        for sid in list(self._io):
            await self.close_session_io(sid)
        if self._session is not None and not self._session.closed:
            await self._session.close()
