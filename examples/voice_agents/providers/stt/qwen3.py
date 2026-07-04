"""Qwen3-ASR 离线(growing-buffer WS)STT。

支持预热连接（prewarm_connection）：用户开始说话时提前建好 WS，把握手延迟
藏进说话期间，用户说完后直接复用已建连接发音频。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time

import aiohttp

from livekit.agents import APIConnectOptions, LanguageCode, stt, utils
from livekit.agents._exceptions import APIConnectionError
from livekit.agents.types import NOT_GIVEN, NotGivenOr
from livekit.agents.utils import shortuuid
from providers.config import WS_CONNECT_TIMEOUT
from providers.helpers import acquire_http_session, resample_pcm

logger = logging.getLogger("custom-audio-providers")


class Qwen3ASROfflineSTT(stt.STT):
    def __init__(
        self,
        *,
        websocket_url: str | None = None,
        sample_rate: int = 16000,
        chunk_size: int = 3200,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=False,
                interim_results=False,
                aligned_transcript=False,
                offline_recognize=True,
            )
        )
        self._url = websocket_url or os.getenv(
            "QWEN3_ASR_WS_URL", "ws://60.205.197.165:10091/ws/transcribe"
        )
        self._sample_rate = sample_rate
        self._chunk_size = chunk_size
        self._session = session
        self._owns_session = False
        self._warm_ws: aiohttp.ClientWebSocketResponse | None = None
        self._prewarming: bool = False

    @property
    def model(self) -> str:
        return "qwen3-asr"

    @property
    def provider(self) -> str:
        return "Qwen3ASR"

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session, owns = acquire_http_session()
            if owns:
                self._owns_session = True
        return self._session

    async def prewarm_connection(self) -> None:
        """用户开始说话时调用：提前建好 WS，将握手延迟藏进说话期间。

        asyncio 单线程：无需锁。prewarm 未完成时 _recognize_once 直接开新连接，
        不会等待 prewarm——避免连接慢时阻塞识别。
        """
        if self._warm_ws is not None and not self._warm_ws.closed:
            return
        if self._prewarming:
            return
        self._prewarming = True
        try:
            ws = await asyncio.wait_for(
                self._ensure_session().ws_connect(
                    self._url, timeout=aiohttp.ClientWSTimeout(ws_close=5.0)
                ),
                timeout=WS_CONNECT_TIMEOUT,
            )
            self._warm_ws = ws
            logger.info("asr prewarm: connected to %s", self._url)
        except Exception as exc:
            logger.info("asr prewarm failed: %s", exc)
        finally:
            self._prewarming = False

    async def _send_audio_and_recv(
        self, ws: aiohttp.ClientWebSocketResponse, pcm: bytes, conn_options: APIConnectOptions
    ) -> str:
        """在已建立的 WS 上发送音频、等待识别结果。"""
        for i in range(0, len(pcm), self._chunk_size):
            await ws.send_bytes(pcm[i : i + self._chunk_size])
        await ws.send_str(json.dumps({"action": "finalize"}))

        transcript = ""
        deadline = time.monotonic() + max(5.0, conn_options.timeout)
        got_response = False

        while time.monotonic() < deadline:
            recv_timeout = 0.4 if got_response else max(0.1, deadline - time.monotonic())
            try:
                msg = await ws.receive(timeout=recv_timeout)
            except asyncio.TimeoutError:
                break

            if msg.type == aiohttp.WSMsgType.TEXT:
                payload = json.loads(msg.data)
                full_text = (payload.get("full_text") or payload.get("text") or "").strip()
                if full_text:
                    transcript = full_text
                got_response = True
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE):
                break
            elif msg.type == aiohttp.WSMsgType.ERROR:
                raise APIConnectionError("Qwen3-ASR websocket error")

        return transcript

    async def _recognize_once(self, pcm: bytes, conn_options: APIConnectOptions) -> str:
        # 无锁取预热连接：asyncio 单线程，两行赋值之间无 yield，天然原子。
        # prewarm 若还在 await ws_connect() 中，_warm_ws 仍为 None，直接走新连接，
        # 不等待——这是关键：recognition 绝不因 prewarm 慢而阻塞。
        warm_ws = self._warm_ws
        self._warm_ws = None

        if warm_ws is not None and not warm_ws.closed:
            logger.info("asr: using prewarmed connection")
            try:
                return await self._send_audio_and_recv(warm_ws, pcm, conn_options)
            finally:
                with contextlib.suppress(Exception):
                    await warm_ws.close()

        logger.info("asr: opening new connection (no prewarm, warm_ws=%s)", warm_ws)
        ws = await asyncio.wait_for(
            self._ensure_session().ws_connect(
                self._url, timeout=aiohttp.ClientWSTimeout(ws_close=5.0)
            ),
            timeout=WS_CONNECT_TIMEOUT,
        )
        try:
            return await self._send_audio_and_recv(ws, pcm, conn_options)
        finally:
            with contextlib.suppress(Exception):
                await ws.close()

    async def _recognize_impl(
        self,
        buffer: utils.AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions,
    ) -> stt.SpeechEvent:
        pcm = resample_pcm(buffer, self._sample_rate)
        if not pcm:
            return stt.SpeechEvent(
                type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                alternatives=[stt.SpeechData(language=LanguageCode("zh"), text="")],
            )

        try:
            transcript = await self._recognize_once(pcm, conn_options)
        except (aiohttp.ClientError, ConnectionError, OSError) as e:
            raise APIConnectionError(f"Qwen3-ASR connection failed: {e}") from e

        lang = LanguageCode(language) if language is not NOT_GIVEN else LanguageCode("zh")
        request_id = shortuuid("qwen3-asr-")
        logger.info("qwen3-asr final transcript request_id=%s text=%r", request_id, transcript)
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            request_id=request_id,
            alternatives=[stt.SpeechData(language=lang, text=transcript)],
        )

    async def aclose(self) -> None:
        warm_ws = self._warm_ws
        self._warm_ws = None
        if warm_ws is not None:
            with contextlib.suppress(Exception):
                await warm_ws.close()
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None
            self._owns_session = False
