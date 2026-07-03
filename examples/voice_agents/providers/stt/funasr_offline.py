"""FunASR 离线(offline 模式)STT —— `upstream` 装配的默认主 STT。

持久 WS 跨轮复用(省 ~190ms/turn 握手)+ `asyncio.Lock` 按轮串行;
超时未拿到 final 则重置连接防"串台";失败重连重试一次。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time

import aiohttp
from common.config_utils import env_bool

from livekit.agents import APIConnectOptions, LanguageCode, stt, utils
from livekit.agents._exceptions import APIConnectionError
from livekit.agents.types import NOT_GIVEN, NotGivenOr
from providers.config import WS_CONNECT_TIMEOUT, FunASROptions, funasr_init_payload
from providers.helpers import acquire_http_session, resample_pcm, unverified_ssl_ctx

logger = logging.getLogger("custom-audio-providers")


class FunASROfflineSTT(stt.STT):
    def __init__(
        self,
        *,
        websocket_url: str | None = None,
        sample_rate: int = 16000,
        verify_ssl: bool | None = None,
        language: str = "zh",
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
        self._opts = FunASROptions(
            websocket_url=websocket_url or os.getenv("FUNASR_WS_URL", "wss://60.205.197.165:10090"),
            sample_rate=sample_rate,
            verify_ssl=verify_ssl
            if verify_ssl is not None
            else env_bool("FUNASR_VERIFY_SSL", False),
            language=language,
        )
        self._session = session
        self._owns_session = False
        # 跨轮复用的持久 WS：每轮新建连接要付 ~190ms TCP+TLS+upgrade。实测远端
        # 支持同一连接连续多段 offline 识别，故连一次反复用。recognize 按轮串行，
        # 用 _ws_lock 串起来；发送/接收失败或服务端关闭则重连一次重试。
        self._ws: aiohttp.ClientWSResponse | None = None
        self._ws_lock = asyncio.Lock()

    @property
    def model(self) -> str:
        return "funasr-offline"

    @property
    def provider(self) -> str:
        return "FunASR"

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session, owns = acquire_http_session()
            if owns:
                self._owns_session = True
        return self._session

    async def _ensure_ws(self) -> aiohttp.ClientWSResponse:
        ws = self._ws
        if ws is not None and not ws.closed:
            return ws
        session = self._ensure_session()
        self._ws = await asyncio.wait_for(
            session.ws_connect(
                self._opts.websocket_url,
                ssl=unverified_ssl_ctx(self._opts.websocket_url, self._opts.verify_ssl),
                heartbeat=30,
                timeout=aiohttp.ClientWSTimeout(ws_receive=30.0),
            ),
            timeout=WS_CONNECT_TIMEOUT,
        )
        return self._ws

    async def _reset_ws(self) -> None:
        ws = self._ws
        self._ws = None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()

    async def _send_request(self, ws: aiohttp.ClientWSResponse, pcm: bytes) -> None:
        """握手 JSON + 全速分片上传 + 发"说完"信号。"""
        payload = funasr_init_payload(
            mode="offline",
            wav_name="livekit-console",
            sample_rate=self._opts.sample_rate,
            chunk_size=[5, 10, 5],
        )
        await ws.send_str(json.dumps(payload, ensure_ascii=False))
        # 离线模式：服务端攒齐 is_speaking:False 才识别，client 端限速毫无意义、
        # 只会拖慢"说完"信号。全速上传（仍分片以免单帧过大）。
        for i in range(0, len(pcm), self._opts.chunk_size):
            await ws.send_bytes(pcm[i : i + self._opts.chunk_size])
        await ws.send_str(json.dumps({"is_speaking": False}, ensure_ascii=False))

    async def _recv_result(
        self, ws: aiohttp.ClientWSResponse, conn_options: APIConnectOptions
    ) -> tuple[str, str, bool]:
        """等识别结果直到 is_final 或超时。返回 (transcript, request_id, got_final)。"""
        transcript = ""
        request_id = ""
        got_final = False
        deadline = time.monotonic() + max(5.0, conn_options.timeout)
        while time.monotonic() < deadline:
            timeout = max(0.1, deadline - time.monotonic())
            try:
                msg = await ws.receive(timeout=timeout)
            except asyncio.TimeoutError:
                break

            if msg.type == aiohttp.WSMsgType.TEXT:
                payload = json.loads(msg.data)
                request_id = payload.get("wav_name", request_id)
                if payload.get("text"):
                    transcript = payload["text"].strip()
                if payload.get("is_final") is True:
                    got_final = True
                    break
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE):
                raise APIConnectionError("FunASR websocket closed mid-recognition")
            elif msg.type == aiohttp.WSMsgType.ERROR:
                raise APIConnectionError("FunASR websocket closed with error")
        return transcript, request_id, got_final

    async def _recognize_once(self, pcm: bytes, conn_options: APIConnectOptions) -> tuple[str, str]:
        """单段 offline 识别，复用持久 ws。返回 (transcript, request_id)。
        失败（服务端关闭/出错）抛异常，由调用方重连重试。"""
        ws = await self._ensure_ws()
        await self._send_request(ws, pcm)
        transcript, request_id, got_final = await self._recv_result(ws, conn_options)
        # 没拿到 is_final（超时）说明这条连接状态不明，关掉它让下一轮重连，避免残留帧串台。
        if not got_final:
            await self._reset_ws()
        return transcript, request_id

    async def _recognize_impl(
        self,
        buffer: utils.AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions,
    ) -> stt.SpeechEvent:
        pcm = resample_pcm(buffer, self._opts.sample_rate)
        if not pcm:
            return stt.SpeechEvent(
                type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                alternatives=[stt.SpeechData(language=LanguageCode("zh"), text="")],
            )

        async with self._ws_lock:
            try:
                transcript, request_id = await self._recognize_once(pcm, conn_options)
            except (APIConnectionError, aiohttp.ClientError, ConnectionError, OSError):
                # 持久连接可能已被服务端/中间设备闲置断开：重连一次重试整段。
                await self._reset_ws()
                transcript, request_id = await self._recognize_once(pcm, conn_options)

        event_language = (
            LanguageCode(language)
            if language is not NOT_GIVEN
            else LanguageCode(self._opts.language)
        )
        logger.info(
            "funasr final transcript request_id=%s text=%r",
            request_id,
            transcript,
        )
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            request_id=request_id,
            alternatives=[stt.SpeechData(language=event_language, text=transcript)],
        )

    async def aclose(self) -> None:
        await self._reset_ws()
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None
            self._owns_session = False
