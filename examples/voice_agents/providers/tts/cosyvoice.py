"""CosyVoice(DashScope tts_v2)流式 TTS —— **默认 TTS 后端**。

每轮从官方对象池借一条连接(命中预热则零握手)、streaming_call() 按句增量送字、
streaming_complete() 收尾,用完归还;打断时 streaming_cancel() 确定性中止后归还
(池会自动 renew 这条失效连接)。借还语义对齐 Qwen 的"不跨轮复用"。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import queue
import threading

import dashscope
from dashscope.audio.tts_v2 import (
    AudioFormat as CosyAudioFormat,
    ResultCallback as CosyResultCallback,
    SpeechSynthesizer as CosySpeechSynthesizer,
    SpeechSynthesizerObjectPool as CosySynthPool,
)
from websocket import WebSocketConnectionClosedException

from livekit.agents import APIConnectOptions, tts
from livekit.agents._exceptions import APIConnectionError, APIStatusError
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS
from livekit.agents.utils import shortuuid
from providers.config import CosyVoiceTTSOptions
from providers.helpers import drain_audio_queue, iter_sentence_chunks

logger = logging.getLogger("custom-audio-providers")


class _WebsocketCloseNoiseFilter(logging.Filter):
    """只丢弃 websocket 库对"服务端正常关闭帧"(opcode=8 Bye/goodbye)的 ERROR 噪音。

    CosyVoice 预热池每 ~30s 主动 renew 连接时,旧连接会收到服务端正常关闭帧,被
    websocket-client 库按 ERROR 打印,看着像错误其实无害(合成全程正常)。这里只过滤
    这一种正常关闭噪音,**其他 websocket 错误(握手失败/异常断开等)照常放行**,不掩盖真问题。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        return not ("opcode=8" in msg and ("goodbye" in msg or "Bye" in msg))


# 安装到 websocket 库的 logger(仅作用于该 logger 的记录,精准、可逆)。
logging.getLogger("websocket").addFilter(_WebsocketCloseNoiseFilter())

_COSY_PCM_FORMATS = {
    16000: CosyAudioFormat.PCM_16000HZ_MONO_16BIT,
    22050: CosyAudioFormat.PCM_22050HZ_MONO_16BIT,
    24000: CosyAudioFormat.PCM_24000HZ_MONO_16BIT,
    48000: CosyAudioFormat.PCM_48000HZ_MONO_16BIT,
}

# 官方对象池(进程级单例):提前建好 WS 连接消除每轮握手,后台线程自动续连/renew。
# 预建的是与 model 无关的纯 WS 握手,borrow 时再套用我们的 model/voice/format/callback。
# **只构造一次**(单例的 __init__ 每次都会重跑并另起线程),故用模块级守卫 + 锁。
_cosy_pool: CosySynthPool | None = None
_cosy_pool_lock = threading.Lock()


def _ensure_cosy_pool(api_key: str, max_size: int) -> CosySynthPool | None:
    """幂等创建预热池。构造会阻塞(逐个开 max_size 条连接),请在后台线程里调。"""
    global _cosy_pool
    if _cosy_pool is not None:
        return _cosy_pool
    with _cosy_pool_lock:
        if _cosy_pool is None:
            dashscope.api_key = api_key
            _cosy_pool = CosySynthPool(max_size=max_size)
    return _cosy_pool


class _CosyVoiceCallback(CosyResultCallback):
    """把 CosyVoice WS 线程的裸 PCM 桥接到 asyncio(线程安全 queue + done 事件)。
    每轮一个实例(不预热复用),不存在悬空音频问题。"""

    def __init__(self) -> None:
        self.audio_queue: queue.Queue[bytes | Exception | None] = queue.Queue()
        self.audio_done = threading.Event()

    def on_open(self) -> None:
        return

    def on_data(self, data: bytes) -> None:
        if data:
            self.audio_queue.put(bytes(data))

    def on_complete(self) -> None:
        self.audio_done.set()

    def on_error(self, message: object) -> None:
        self.audio_queue.put(APIStatusError(f"CosyVoice TTS error: {message}"))
        self.audio_done.set()

    def on_close(self) -> None:
        return


class CosyVoiceStreamingTTS(tts.TTS):
    """Streaming TTS using DashScope CosyVoice (tts_v2.SpeechSynthesizer)。

    构造参数收敛为 model/voice/api_key + 完整 `opts`(CosyVoiceTTSOptions,含
    speech_rate/pitch_rate/instruction 等,默认走 env)。
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        voice: str | None = None,
        api_key: str | None = None,
        opts: CosyVoiceTTSOptions | None = None,
    ) -> None:
        opts = opts or CosyVoiceTTSOptions.from_env(model=model, voice=voice)
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=True),
            sample_rate=opts.sample_rate,
            num_channels=1,
        )
        key = api_key or os.getenv("DASHSCOPE_API_KEY")
        if not key:
            raise ValueError("DASHSCOPE_API_KEY is required for CosyVoice TTS")
        if opts.sample_rate not in _COSY_PCM_FORMATS:
            raise ValueError(f"unsupported CosyVoice sample_rate: {opts.sample_rate}")

        self._api_key = key
        self._opts = opts
        self._pool_size = max(1, int(os.getenv("COSYVOICE_POOL_SIZE", "3")))

    @property
    def model(self) -> str:
        return self._opts.model

    @property
    def provider(self) -> str:
        return "CosyVoice"

    def prewarm_connection(self) -> None:
        """预建 WS 连接池消除每轮握手。幂等(池为进程级单例,只建一次,后台自动续连)。
        建池会阻塞,故由调用方丢线程里跑(应用层在用户说话时 to_thread 调用);
        失败则跳过,后续退回冷连接,绝不影响主流程。"""
        try:
            _ensure_cosy_pool(self._api_key, self._pool_size)
        except Exception as exc:  # noqa: BLE001
            logger.debug("cosyvoice prewarm skipped: %s", exc)

    def take_synth(self, callback: CosyResultCallback) -> tuple[CosySpeechSynthesizer, bool]:
        """取一条就绪连接:命中预热池则零握手(返回 pooled=True 需归还);否则现建冷连接。"""
        opts = self._opts
        pool = _cosy_pool
        if pool is not None:
            try:
                synth = pool.borrow_synthesizer(
                    model=opts.model,
                    voice=opts.voice,
                    format=_COSY_PCM_FORMATS[opts.sample_rate],
                    speech_rate=opts.speech_rate,
                    pitch_rate=opts.pitch_rate,
                    instruction=opts.instruction or None,
                    callback=callback,
                )
                return synth, True
            except Exception as exc:  # noqa: BLE001
                logger.debug("cosyvoice borrow failed, cold build: %s", exc)
        return self._build_synth(callback), False

    @staticmethod
    def _release_synth(synth: CosySpeechSynthesizer, pooled: bool) -> None:
        """归还(池连接,即便已 cancel——池会自动 renew)或关闭(冷连接)。"""
        pool = _cosy_pool
        if pooled and pool is not None:
            with contextlib.suppress(Exception):
                pool.return_synthesizer(synth)
            return
        with contextlib.suppress(Exception):
            synth.close()

    def _build_synth(self, callback: CosyResultCallback | None) -> CosySpeechSynthesizer:
        dashscope.api_key = self._api_key
        opts = self._opts
        kwargs: dict[str, object] = {
            "model": opts.model,
            "voice": opts.voice,
            "format": _COSY_PCM_FORMATS[opts.sample_rate],
            "speech_rate": opts.speech_rate,
            "pitch_rate": opts.pitch_rate,
            "callback": callback,
        }
        if opts.instruction:
            kwargs["instruction"] = opts.instruction
        return CosySpeechSynthesizer(**kwargs)

    def synthesize(
        self, text: str, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> tts.ChunkedStream:
        return _CosyVoiceChunkedStream(tts=self, input_text=text, conn_options=conn_options)

    def stream(
        self, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> tts.SynthesizeStream:
        return _CosyVoiceSynthesizeStream(tts=self, conn_options=conn_options)

    def _synthesize_sync(self, text: str) -> bytes:
        synth = self._build_synth(None)
        try:
            audio = synth.call(text)
        finally:
            with contextlib.suppress(Exception):
                synth.close()
        return bytes(audio) if audio else b""

    async def aclose(self) -> None:
        return


class _CosyVoiceChunkedStream(tts.ChunkedStream):
    def __init__(
        self, *, tts: CosyVoiceStreamingTTS, input_text: str, conn_options: APIConnectOptions
    ) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._tts = tts

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        audio_bytes = await asyncio.to_thread(self._tts._synthesize_sync, self.input_text)
        if not audio_bytes:
            raise APIConnectionError("CosyVoice TTS returned empty audio")
        output_emitter.initialize(
            request_id=shortuuid("cosyvoice-tts-"),
            sample_rate=self._tts.sample_rate,
            num_channels=self._tts.num_channels,
            mime_type="audio/pcm",
        )
        output_emitter.push(audio_bytes)
        output_emitter.flush()


class _CosyVoiceSynthesizeStream(tts.SynthesizeStream):
    """按句增量 streaming_call,让第一句合成与 LLM 后续生成重叠以降首包延迟。"""

    def __init__(self, *, tts: CosyVoiceStreamingTTS, conn_options: APIConnectOptions) -> None:
        super().__init__(tts=tts, conn_options=conn_options)
        self._tts = tts

    async def _first_call_with_stale_retry(
        self, synth: CosySpeechSynthesizer, pooled: bool, callback: _CosyVoiceCallback, text: str
    ) -> tuple[CosySpeechSynthesizer, bool]:
        """首句 streaming_call;借到"服务端已关"的陈旧池连接时,换冷连接重试一次。

        实测(2026-07-03 回归,亦见于 6/20~6/26 历史 run):上一轮 finish 后归还的池连接
        可能已被服务端关闭而池尚未 renew(~0.7s 窗口),此时 streaming_call 抛
        WebSocketConnectionClosedException,该轮回复音频整段丢失。仅首句重试
        (尚无任何音频入队,无重复播放风险);冷连接保证新鲜,~0.8s 代价只在触发时支付。
        """
        try:
            await asyncio.to_thread(synth.streaming_call, text)
            return synth, pooled
        except WebSocketConnectionClosedException:
            logger.warning("cosyvoice pooled connection stale; retrying once on a cold connection")
            await asyncio.to_thread(self._tts._release_synth, synth, pooled)  # 池会 renew 这条
            fresh = await asyncio.to_thread(self._tts._build_synth, callback)
            await asyncio.to_thread(fresh.streaming_call, text)
            return fresh, False

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        callback = _CosyVoiceCallback()
        synth, pooled = await asyncio.to_thread(self._tts.take_synth, callback)

        output_emitter.initialize(
            request_id=shortuuid("cosyvoice-tts-"),
            sample_rate=self._tts.sample_rate,
            num_channels=self._tts.num_channels,
            mime_type="audio/pcm",
            stream=False,
        )

        drain_task = asyncio.create_task(drain_audio_queue(callback.audio_queue, output_emitter))
        any_text = False

        try:
            async for sentence in iter_sentence_chunks(self._input_ch):
                if not any_text:
                    synth, pooled = await self._first_call_with_stale_retry(
                        synth, pooled, callback, sentence
                    )
                else:
                    await asyncio.to_thread(synth.streaming_call, sentence)
                any_text = True

            if any_text:
                # 阻塞至服务端合成完成(on_complete 落定),此时全部 on_data 已入队。
                await asyncio.to_thread(synth.streaming_complete)

            callback.audio_queue.put(None)

        except BaseException:
            # 打断/异常(含 CancelledError):streaming_cancel() 确定性中止、丢弃在途音频,
            # 然后归还连接(池会 renew 这条已关闭的连接);不污染下一轮。
            callback.audio_queue.put(None)
            drain_task.cancel()
            with contextlib.suppress(Exception):
                await asyncio.to_thread(synth.streaming_cancel)
            await asyncio.to_thread(self._tts._release_synth, synth, pooled)
            raise

        await asyncio.to_thread(self._tts._release_synth, synth, pooled)
        await drain_task
