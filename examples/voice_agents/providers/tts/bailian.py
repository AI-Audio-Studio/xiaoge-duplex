"""百炼 qwen-tts-realtime 单次合成(非流式)TTS + 共用的整段合成回调/ChunkedStream。

QwenStreamingTTS(流式变体,含预热池)在 qwen_stream.py,复用本模块的
_BailianCallback / _BailianChunkedStream。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import threading

import dashscope
from dashscope.audio.qwen_tts_realtime.qwen_tts_realtime import (
    AudioFormat as BailianAudioFormat,
    QwenTtsRealtime,
    QwenTtsRealtimeCallback,
)

from livekit.agents import APIConnectOptions, tts, utils
from livekit.agents._exceptions import APIConnectionError, APIStatusError
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS
from providers.config import BailianTTSOptions


class _BailianCallback(QwenTtsRealtimeCallback):
    def __init__(self) -> None:
        self.audio = bytearray()
        self.done = threading.Event()
        self.error: Exception | None = None

    def on_open(self) -> None:
        return

    def on_close(self, close_status_code, close_msg) -> None:
        if not self.done.is_set():
            self.done.set()

    def on_event(self, message: dict) -> None:
        event_type = message.get("type")
        if event_type == "response.audio.delta":
            delta = message.get("delta") or message.get("response", {}).get("audio", {}).get(
                "delta"
            )
            if delta:
                self.audio.extend(base64.b64decode(delta))
        elif event_type in ("error", "response.error"):
            self.error = APIStatusError(str(message))
            self.done.set()
        elif event_type == "response.done":
            self.done.set()


def synthesize_once_sync(api_key: str, opts: BailianTTSOptions, text: str) -> bytes:
    """整段同步合成(connect→append→commit→等 done→finish/close)。丢线程里跑。"""
    # NOTE: dashscope.api_key 是进程级全局(SDK 设计如此)。当前单 key 场景没问题;
    # 若将来一个进程内并存多个不同 DASHSCOPE_API_KEY,这里会相互覆盖/竞争。
    dashscope.api_key = api_key
    callback = _BailianCallback()
    realtime = QwenTtsRealtime(model=opts.model, callback=callback)
    realtime.connect()
    try:
        realtime.update_session(
            voice=opts.voice,
            response_format=BailianAudioFormat.PCM_24000HZ_MONO_16BIT,
            speech_rate=opts.speech_rate,
            sample_rate=opts.sample_rate,
        )
        realtime.append_text(text)
        realtime.commit()
        if not callback.done.wait(timeout=30):
            raise APIConnectionError("Bailian TTS timed out")
        if callback.error:
            raise callback.error
        return bytes(callback.audio)
    finally:
        with contextlib.suppress(Exception):
            realtime.finish()
        with contextlib.suppress(Exception):
            realtime.close()


class BailianRealtimeTTS(tts.TTS):
    def __init__(
        self,
        *,
        model: str | None = None,
        voice: str | None = None,
        sample_rate: int = 24000,
        speech_rate: float = 1.0,
        api_key: str | None = None,
    ) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=sample_rate,
            num_channels=1,
        )
        key = api_key or os.getenv("DASHSCOPE_API_KEY")
        if not key:
            raise ValueError("DASHSCOPE_API_KEY is required for Bailian TTS")

        self._api_key = key
        self._opts = BailianTTSOptions(
            model=model or os.getenv("BAILIAN_TTS_MODEL", "qwen-tts-realtime"),
            voice=voice or os.getenv("BAILIAN_TTS_VOICE", "Ethan"),
            sample_rate=sample_rate,
            speech_rate=speech_rate,
        )

    @property
    def model(self) -> str:
        return self._opts.model

    @property
    def provider(self) -> str:
        return "Bailian"

    def synthesize(
        self, text: str, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> tts.ChunkedStream:
        return _BailianChunkedStream(tts=self, input_text=text, conn_options=conn_options)

    async def aclose(self) -> None:
        return

    def _synthesize_sync(self, text: str) -> bytes:
        return synthesize_once_sync(self._api_key, self._opts, text)


class _BailianChunkedStream(tts.ChunkedStream):
    """整段合成的 ChunkedStream;BailianRealtimeTTS 与 QwenStreamingTTS 共用
    (只要求宿主有 `_synthesize_sync(text) -> bytes` 与 sample_rate/num_channels)。"""

    def __init__(self, *, tts, input_text: str, conn_options: APIConnectOptions) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._tts = tts

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        audio_bytes = await asyncio.to_thread(self._tts._synthesize_sync, self.input_text)
        if not audio_bytes:
            raise APIConnectionError("Bailian TTS returned empty audio")

        output_emitter.initialize(
            request_id=utils.shortuuid("bailian-tts-"),
            sample_rate=self._tts.sample_rate,
            num_channels=self._tts.num_channels,
            mime_type="audio/pcm",
        )
        output_emitter.push(audio_bytes)
        output_emitter.flush()
