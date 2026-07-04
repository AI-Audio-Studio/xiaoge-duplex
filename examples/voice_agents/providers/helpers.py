"""跨后端共享积木(拆分前在各 Provider 里 1:1 重复的样板)。"""

from __future__ import annotations

import asyncio
import queue
import ssl
from collections.abc import AsyncIterable, AsyncIterator

import aiohttp

from livekit import rtc
from livekit.agents import tts, utils

# 句界:流式 TTS 攒到这些字符就把一句话交给后端合成,让第一句合成与 LLM 后续
# 生成重叠(首包延迟从"等整段"降到"等第一句")。
SENTENCE_BOUNDARY = "。！？!?；;\n"


def acquire_http_session() -> tuple[aiohttp.ClientSession, bool]:
    """Return (session, owns). Prefer LiveKit's shared http session; fall back to a
    private ClientSession the caller is responsible for closing (owns=True)."""
    try:
        return utils.http_context.http_session(), False
    except RuntimeError:
        return aiohttp.ClientSession(), True


def resample_pcm(buffer: utils.AudioBuffer, target_rate: int) -> bytes:
    """Merge an AudioBuffer and resample to mono 16-bit PCM at target_rate."""
    frame = utils.merge_frames(buffer) if isinstance(buffer, list) else buffer
    if frame.sample_rate != target_rate or frame.num_channels != 1:
        resampler = rtc.AudioResampler(
            input_rate=frame.sample_rate,
            output_rate=target_rate,
            num_channels=1,
            quality=rtc.AudioResamplerQuality.HIGH,
        )
        frames = resampler.push(frame)
        frames.extend(resampler.flush())
        frame = rtc.combine_audio_frames(frames)
    return bytes(frame.data)


def unverified_ssl_ctx(url: str, verify_ssl: bool) -> ssl.SSLContext | None:
    """内网自签证书端点:仅 wss:// 且 verify_ssl=False 时跳过校验(生产/公网请开校验)。"""
    if url.startswith("wss://") and not verify_ssl:
        return ssl._create_unverified_context()
    return None


async def iter_sentence_chunks(
    src: AsyncIterable, *, boundary: str = SENTENCE_BOUNDARY
) -> AsyncIterator[str]:
    """按句边界聚合文本流:累计文本以边界字符结尾即产出一段(原样,不 strip);
    输入结束后残余含非空白也原样产出。忽略非 str 项(如 FlushSentinel)。"""
    pending = ""
    async for data in src:
        if not isinstance(data, str):
            continue
        pending += data
        if pending and pending[-1] in boundary:
            yield pending
            pending = ""
    if pending.strip():
        yield pending


async def drain_audio_queue(
    audio_queue: queue.Queue[bytes | Exception | None], output_emitter: tts.AudioEmitter
) -> None:
    """把 SDK 回调线程灌进 queue 的 PCM 逐块推给 LiveKit;None 哨兵结束,Exception 上抛。
    无论如何收尾 flush(与拆分前 Qwen/CosyVoice 两处 1:1 重复的 _drain 一致)。"""
    try:
        while True:
            data = await asyncio.to_thread(audio_queue.get)
            if data is None:
                break
            if isinstance(data, Exception):
                raise data
            output_emitter.push(data)
    finally:
        output_emitter.flush()
