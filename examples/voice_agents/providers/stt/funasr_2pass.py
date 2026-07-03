"""FunASR 2pass 流式 STT(经框架 RecognizeStream 协议;interim + final)。

Interim transcripts make the `min_words` interruption gate functional under VAD-only
interruption, so short backchannels ("嗯") can be blocked while multi-word real
interruptions still cut playback mid-speech.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os

import aiohttp
from common.config_utils import env_bool

from livekit import rtc
from livekit.agents import APIConnectOptions, LanguageCode, stt, utils
from livekit.agents._exceptions import APIConnectionError
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, NotGivenOr
from providers.config import FunASROptions, funasr_init_payload
from providers.helpers import acquire_http_session, unverified_ssl_ctx

logger = logging.getLogger("custom-audio-providers")

# FunASR 2pass 常给碎片 final 加前导标点（"，这"/"。这片子"），判停模型会
# 当成"句子没说完"而干等到 max_delay。剥掉前导标点让 EOU 判断更准。
_LEAD_PUNCT = "，,。.、！!？?；;：:～~ \t\n"


class FunASRStreamingSTT(stt.STT):
    """Streaming FunASR (2pass) STT: emits interim (2pass-online) + final (2pass-offline)."""

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
                streaming=True,
                interim_results=True,
                aligned_transcript=False,
                offline_recognize=False,
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

    @property
    def model(self) -> str:
        return "funasr-2pass"

    @property
    def provider(self) -> str:
        return "FunASR"

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session, owns = acquire_http_session()
            if owns:
                self._owns_session = True
        return self._session

    async def _recognize_impl(
        self,
        buffer: utils.AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions,
    ) -> stt.SpeechEvent:
        raise NotImplementedError("FunASRStreamingSTT only supports stream()")

    def stream(
        self,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> _FunASRStream:
        return _FunASRStream(
            stt=self,
            conn_options=conn_options,
            opts=self._opts,
            session=self._ensure_session(),
        )

    async def aclose(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None
            self._owns_session = False


class _FunASRStream(stt.RecognizeStream):
    def __init__(
        self,
        *,
        stt: FunASRStreamingSTT,
        conn_options: APIConnectOptions,
        opts: FunASROptions,
        session: aiohttp.ClientSession,
    ) -> None:
        super().__init__(stt=stt, conn_options=conn_options, sample_rate=opts.sample_rate)
        self._opts = opts
        self._session = session
        self._speaking = False

    async def _run(self) -> None:
        init_payload = funasr_init_payload(
            mode="2pass",
            wav_name="livekit-stream",
            sample_rate=self._opts.sample_rate,
            chunk_size=[5, 10, 5],
        )
        ws_timeout = aiohttp.ClientWSTimeout(ws_receive=30.0)
        async with self._session.ws_connect(
            self._opts.websocket_url,
            ssl=unverified_ssl_ctx(self._opts.websocket_url, self._opts.verify_ssl),
            heartbeat=30,
            timeout=ws_timeout,
        ) as ws:
            await ws.send_str(json.dumps(init_payload, ensure_ascii=False))
            tasks = [
                asyncio.create_task(self._send_loop(ws), name="funasr_send"),
                asyncio.create_task(self._recv_loop(ws), name="funasr_recv"),
            ]
            try:
                await asyncio.gather(*tasks)
            finally:
                await utils.aio.cancel_and_wait(*tasks)

    async def _send_loop(self, ws: aiohttp.ClientWSResponse) -> None:
        # FunASR 2pass does its own VAD segmentation, so FlushSentinel is ignored;
        # is_speaking:false is only sent once input ends to flush the final segment.
        try:
            async for data in self._input_ch:
                if isinstance(data, rtc.AudioFrame):
                    await ws.send_bytes(bytes(data.data))
        finally:
            with contextlib.suppress(Exception):
                await ws.send_str(json.dumps({"is_speaking": False}, ensure_ascii=False))

    async def _recv_loop(self, ws: aiohttp.ClientWSResponse) -> None:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                payload = json.loads(msg.data)
                if self._handle_text_payload(payload):
                    continue  # 空 online 文本:跳过(含本条的 is_final 检查,与拆分前一致)
                if payload.get("is_final") is True:
                    break
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE):
                break
            elif msg.type == aiohttp.WSMsgType.ERROR:
                raise APIConnectionError("FunASR websocket closed with error")

    def _handle_text_payload(self, payload: dict) -> bool:
        """把一条 2pass 消息转成 SpeechEvent。返回 True 表示"本条到此为止"(原 continue)。"""
        language = LanguageCode(self._opts.language)
        mode = payload.get("mode")
        text = (payload.get("text") or "").strip().lstrip(_LEAD_PUNCT)
        if mode == "2pass-online":
            if not text:
                return True
            if not self._speaking:
                self._speaking = True
                self._event_ch.send_nowait(
                    stt.SpeechEvent(type=stt.SpeechEventType.START_OF_SPEECH)
                )
            self._event_ch.send_nowait(
                stt.SpeechEvent(
                    type=stt.SpeechEventType.INTERIM_TRANSCRIPT,
                    alternatives=[stt.SpeechData(language=language, text=text)],
                )
            )
        elif mode == "2pass-offline":
            if text:
                self._event_ch.send_nowait(
                    stt.SpeechEvent(
                        type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                        alternatives=[stt.SpeechData(language=language, text=text)],
                    )
                )
            if self._speaking:
                self._speaking = False
                self._event_ch.send_nowait(stt.SpeechEvent(type=stt.SpeechEventType.END_OF_SPEECH))
        return False
