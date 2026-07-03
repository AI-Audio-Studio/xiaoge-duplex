"""百炼 qwen-tts-realtime 流式 TTS(每轮一条连接 + size-1 预热池)。

每轮一条连接：connect()/update_session() -> 按句 append_text() -> finish()
合成并收尾 -> close()。打断时直接 close() 中止服务端合成。
每轮独立连接 + 独立 callback，杜绝跨轮串台，打断语义干净可靠。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import queue
import threading
import time

import dashscope
from dashscope.audio.qwen_tts_realtime.qwen_tts_realtime import (
    AudioFormat as BailianAudioFormat,
    QwenTtsRealtime,
    QwenTtsRealtimeCallback,
)

from livekit.agents import APIConnectOptions, tts
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS
from livekit.agents.utils import shortuuid
from providers.config import BailianTTSOptions
from providers.helpers import drain_audio_queue, iter_sentence_chunks
from providers.tts.bailian import _BailianChunkedStream, synthesize_once_sync

logger = logging.getLogger("custom-audio-providers")


class _QwenStreamCallback(QwenTtsRealtimeCallback):
    """Callback that bridges Qwen TTS audio data from WebSocket thread to asyncio.

    目标 queue/event 通过 bind() 在每轮开始时挂入，构造时为空——这样连接可以提前
    预热（connect+update_session）而不绑定到某一轮的 queue。预热到 bind 之间不会
    append_text/commit，服务端不吐 audio.delta，悬空的 delta 直接丢弃即可。
    """

    def __init__(self) -> None:
        self.audio_queue: queue.Queue[bytes | Exception | None] | None = None
        self.audio_done: threading.Event | None = None

    def bind(
        self, audio_queue: queue.Queue[bytes | Exception | None], audio_done: threading.Event
    ) -> None:
        self.audio_queue = audio_queue
        self.audio_done = audio_done

    def on_event(self, message: dict) -> None:
        event_type = message.get("type")
        if event_type == "response.audio.delta":
            q = self.audio_queue
            if q is None:
                return  # 预热连接未绑定到某轮，丢弃悬空音频
            delta = message.get("delta") or message.get("response", {}).get("audio", {}).get(
                "delta"
            )
            if delta:
                try:
                    q.put(base64.b64decode(delta))
                except Exception as e:
                    q.put(Exception(f"Failed to decode audio: {e}"))
        elif event_type == "response.done":
            if self.audio_done is not None:
                self.audio_done.set()
        elif event_type in ("error", "response.error"):
            if self.audio_queue is not None:
                self.audio_queue.put(Exception(f"Qwen TTS error: {message}"))

    def on_close(self, close_status_code, close_msg) -> None:
        pass  # drain handles shutdown via audio_done + queue sentinel


class QwenStreamingTTS(tts.TTS):
    """Streaming TTS using Qwen TTS Realtime(见模块 docstring)。"""

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
            capabilities=tts.TTSCapabilities(streaming=True),
            sample_rate=sample_rate,
            num_channels=1,
        )
        key = api_key or os.getenv("DASHSCOPE_API_KEY")
        if not key:
            raise ValueError("DASHSCOPE_API_KEY is required for Qwen TTS")

        self._api_key = key
        self._opts = BailianTTSOptions(
            model=model or os.getenv("BAILIAN_TTS_MODEL", "qwen-tts-realtime"),
            voice=voice or os.getenv("BAILIAN_TTS_VOICE", "Ethan"),
            sample_rate=sample_rate,
            speech_rate=speech_rate,
        )
        # 预热连接：池 size=1。connect()+update_session() 的 ~1s 握手是每轮 tts_ttfb 的
        # 大头，提前在用户说话期间建好，下一轮 _run 直接取用。超过 TTL 视为可能被服务端
        # 闲置关闭，丢弃重建（宁可慢一轮也不用半死连接）。
        self._warm_lock = threading.Lock()
        self._warm_conn: tuple[QwenTtsRealtime, _QwenStreamCallback] | None = None
        self._warm_at = 0.0
        self._warm_ttl = float(os.getenv("BAILIAN_TTS_WARM_TTL", "20"))

    def _build_connection(self) -> tuple[QwenTtsRealtime, _QwenStreamCallback]:
        dashscope.api_key = self._api_key
        opts = self._opts
        callback = _QwenStreamCallback()
        synth = QwenTtsRealtime(model=opts.model, callback=callback)
        synth.connect()
        synth.update_session(
            voice=opts.voice,
            response_format=BailianAudioFormat.PCM_24000HZ_MONO_16BIT,
            speech_rate=opts.speech_rate,
            sample_rate=opts.sample_rate,
        )
        return synth, callback

    def take_connection(self) -> tuple[QwenTtsRealtime, _QwenStreamCallback]:
        """取一条就绪连接：命中预热则零握手，否则现建（与改前同延迟）。"""
        with self._warm_lock:
            conn = self._warm_conn
            self._warm_conn = None
            fresh = conn is not None and (time.monotonic() - self._warm_at) <= self._warm_ttl
        if conn is not None and not fresh:
            with contextlib.suppress(Exception):
                conn[0].close()
            conn = None
        if conn is not None:
            return conn
        return self._build_connection()

    def prewarm_connection(self) -> None:
        """后台预建一条连接挂起。已有则跳过；建多了关掉富余的。阻塞调用，请丢线程里跑。"""
        with self._warm_lock:
            if self._warm_conn is not None and (time.monotonic() - self._warm_at) <= self._warm_ttl:
                return
        try:
            conn = self._build_connection()
        except Exception as exc:  # noqa: BLE001
            logger.debug("tts prewarm skipped: %s", exc)
            return
        stale: tuple[QwenTtsRealtime, _QwenStreamCallback] | None = None
        with self._warm_lock:
            if self._warm_conn is None:
                self._warm_conn = conn
                self._warm_at = time.monotonic()
            else:
                stale = conn
        if stale is not None:
            with contextlib.suppress(Exception):
                stale[0].close()

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

    def stream(
        self, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> tts.SynthesizeStream:
        return _QwenSynthesizeStream(tts=self, conn_options=conn_options)

    def _synthesize_sync(self, text: str) -> bytes:
        return synthesize_once_sync(self._api_key, self._opts, text)

    async def aclose(self) -> None:
        with self._warm_lock:
            conn = self._warm_conn
            self._warm_conn = None
        if conn is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(conn[0].close)


class _QwenSynthesizeStream(tts.SynthesizeStream):
    """SynthesizeStream using Qwen TTS with single-commit strategy.

    Buffers all incoming text and commits once when input ends. This is
    reliable but has the same latency profile as non-streaming TTS.
    """

    def __init__(
        self,
        *,
        tts: QwenStreamingTTS,
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(tts=tts, conn_options=conn_options)
        self._tts = tts

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        opts = self._tts._opts

        # 当轮专属连接 + 专属 callback：取一条就绪连接（命中预热则零握手）后把当轮
        # queue/event 绑上去。连接仍是每轮独占、用完即关，杜绝残留音频串台——预热只是
        # 把握手提前到上一轮播放期，不改变"不跨轮复用"的语义。
        audio_queue: queue.Queue[bytes | Exception | None] = queue.Queue()
        audio_done = threading.Event()

        synth, callback = await asyncio.to_thread(self._tts.take_connection)
        callback.bind(audio_queue, audio_done)

        output_emitter.initialize(
            request_id=shortuuid("qwen-tts-"),
            sample_rate=opts.sample_rate,
            num_channels=self._tts.num_channels,
            mime_type="audio/pcm",
            stream=False,
        )

        drain_task = asyncio.create_task(drain_audio_queue(audio_queue, output_emitter))
        any_text = False

        try:
            # 按句边界增量 append，让第一句的合成与 LLM 后续生成重叠，
            # 而不是攒齐整段再发（首包延迟从"等整段"降到"等第一句"）。
            async for sentence in iter_sentence_chunks(self._input_ch):
                await asyncio.to_thread(synth.append_text, sentence)
                any_text = True

            if any_text:
                # commit() 触发合成（与 _synthesize_sync 同一条经过验证的路径）；
                # 等 response.done 落定后再 finish()+close() 收尾。
                await asyncio.to_thread(synth.commit)
                done = await asyncio.to_thread(audio_done.wait, 30)
                if not done:
                    raise TimeoutError("Qwen TTS synthesis timed out")

            audio_queue.put(None)

        except BaseException:
            # 打断/异常（含 asyncio.CancelledError——框架打断时 cancel 本 _run）。
            # 每轮独立连接，直接 close() 即可确定性中止服务端合成、丢弃在途音频，
            # 不会污染下一轮（下一轮本就是全新连接）。
            audio_queue.put(None)
            drain_task.cancel()
            with contextlib.suppress(Exception):
                await asyncio.to_thread(synth.close)
            raise

        with contextlib.suppress(Exception):
            await asyncio.to_thread(synth.finish)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(synth.close)
        await drain_task
