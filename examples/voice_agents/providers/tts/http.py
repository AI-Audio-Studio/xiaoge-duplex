"""流式 HTTP POST TTS(可选后端)。

POST /tts  {"text": ..., "speaker": ..., "speed": ...}
返回 audio/L16 PCM 流，24000Hz 单声道 16-bit。
用 HTTP_TTS_URL 环境变量覆盖默认地址。
"""

from __future__ import annotations

import logging
import os

import aiohttp

from livekit.agents import APIConnectOptions, tts, utils
from livekit.agents._exceptions import APIConnectionError
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS
from providers.config import TTS_CONNECT_TIMEOUT
from providers.helpers import acquire_http_session, iter_sentence_chunks

logger = logging.getLogger("custom-audio-providers")

# 历史行为:HTTP TTS 的句界不含换行符(与 Qwen/CosyVoice 的 SENTENCE_BOUNDARY 差一个 \n)。
_HTTP_BOUNDARY = "。！？!?；;"


class HttpStreamingTTS(tts.TTS):
    """TTS backed by a streaming HTTP POST endpoint."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        speaker: str = "default",
        speed: float = 1.0,
        sample_rate: int = 24000,
        chunk_size: int = 4096,
    ) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=True),
            sample_rate=sample_rate,
            num_channels=1,
        )
        self._url = (base_url or os.getenv("HTTP_TTS_URL", "http://10.212.164.230:8001")).rstrip(
            "/"
        ) + "/tts"
        self._speaker = speaker
        self._speed = float(os.getenv("HTTP_TTS_SPEED", str(speed)))
        self._chunk_size = chunk_size

    @property
    def model(self) -> str:
        return "http-tts"

    @property
    def provider(self) -> str:
        return "HttpTTS"

    def synthesize(
        self, text: str, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> tts.ChunkedStream:
        return _HttpChunkedStream(tts=self, input_text=text, conn_options=conn_options)

    def stream(
        self, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> tts.SynthesizeStream:
        return _HttpSynthesizeStream(tts=self, conn_options=conn_options)

    async def aclose(self) -> None:
        return

    async def _post_push(
        self, session: aiohttp.ClientSession, text: str, output_emitter: tts.AudioEmitter
    ) -> None:
        """POST 单句文本，把 PCM 块逐一 push 到 emitter。不调用 flush()，由调用方决定时机。"""
        payload = {"text": text, "speaker": self._speaker, "speed": self._speed}
        timeout = aiohttp.ClientTimeout(
            connect=TTS_CONNECT_TIMEOUT, sock_connect=TTS_CONNECT_TIMEOUT
        )
        async with session.post(self._url, json=payload, timeout=timeout) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise APIConnectionError(f"HTTP TTS returned {resp.status}: {body}")
            async for chunk in resp.content.iter_chunked(self._chunk_size):
                if chunk:
                    output_emitter.push(chunk)


class _HttpChunkedStream(tts.ChunkedStream):
    def __init__(
        self, *, tts: HttpStreamingTTS, input_text: str, conn_options: APIConnectOptions
    ) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._tts = tts

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        output_emitter.initialize(
            request_id=utils.shortuuid("http-tts-"),
            sample_rate=self._tts.sample_rate,
            num_channels=self._tts.num_channels,
            mime_type="audio/pcm",
        )
        session, owns = acquire_http_session()
        try:
            await self._tts._post_push(session, self.input_text, output_emitter)
            output_emitter.flush()
        finally:
            if owns:
                await session.close()


class _HttpSynthesizeStream(tts.SynthesizeStream):
    def __init__(self, *, tts: HttpStreamingTTS, conn_options: APIConnectOptions) -> None:
        super().__init__(tts=tts, conn_options=conn_options)
        self._tts = tts

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        output_emitter.initialize(
            request_id=utils.shortuuid("http-tts-"),
            sample_rate=self._tts.sample_rate,
            num_channels=self._tts.num_channels,
            mime_type="audio/pcm",
            stream=False,
        )

        session, owns = acquire_http_session()
        any_text = False
        try:
            # 按句边界逐句发 POST：LLM 生成第一句结束就立即合成，
            # 收音频期间 LLM 继续生成后续句，首包延迟从"等整段"降到"等第一句"。
            async for sentence in iter_sentence_chunks(self._input_ch, boundary=_HTTP_BOUNDARY):
                text = sentence.strip()
                if text:
                    await self._tts._post_push(session, text, output_emitter)
                    any_text = True

            if any_text:
                output_emitter.flush()
        finally:
            if owns:
                await session.close()
