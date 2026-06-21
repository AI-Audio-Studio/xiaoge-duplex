"""FunASR online 旁路打断流。

主 STT 仍走 offline（final 快、质量稳——2pass 当主链路时服务端 VAD 把 final
延后 ~1.2s，已回退，见坑6）。这里另开一条 mode:"2pass" 的 WS 流，只消费
~600ms 粒度的 2pass-online 增量转写，专做"压话打断"判定：用户压着 AI 播报
说话，字数一够就掐播放，不必等整句说完出 final。

转写质量不重要（不进对话、不出回复），只用来数字数/匹配停止词。
判定策略（数几个字、哪些字算数）在 agent 文件里，这里只管传输：
推音频、收增量文本、断线重连。缺 URL 或被 env 关闭时降级 no-op。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import ssl
from collections.abc import Callable
from dataclasses import dataclass

import aiohttp

from livekit import rtc
from livekit.agents.voice import io

logger = logging.getLogger("online-interrupt")


@dataclass(slots=True)
class OnlineInterruptConfig:
    enable: bool = True
    ws_url: str | None = None
    verify_ssl: bool = False
    sample_rate: int = 16_000
    min_chars: int = 3
    reconnect_delay: float = 2.0

    @classmethod
    def from_env(cls) -> OnlineInterruptConfig:
        return cls(
            enable=_parse_bool(os.getenv("XIAOGE_ONLINE_INTERRUPT_ENABLE", "1")),
            ws_url=os.getenv("FUNASR_WS_URL", "").strip() or None,
            verify_ssl=_parse_bool(os.getenv("FUNASR_VERIFY_SSL", "0")),
            min_chars=_parse_int(os.getenv("XIAOGE_ONLINE_INTERRUPT_MIN_CHARS"), 3),
        )


def unavailable_reason(config: OnlineInterruptConfig) -> str | None:
    if not config.enable:
        return "disabled (XIAOGE_ONLINE_INTERRUPT_ENABLE=0)"
    if not config.ws_url:
        return "FUNASR_WS_URL missing"
    return None


class OnlineAsrTap:
    """持续把 mic 音频推给 FunASR 2pass 流，增量转写回调到事件循环。

    on_text(piece, segment_end)：segment_end=False 是 2pass-online 增量片段，
    True 是该段的 2pass-offline 收尾（用来通知上层清累加器，避免 online+offline
    对同一段语音双重计数）。回调都在事件循环上执行。
    """

    def __init__(
        self,
        config: OnlineInterruptConfig,
        *,
        hotwords: str = "",
        on_text: Callable[[str, bool], None],
    ) -> None:
        self._config = config
        self._hotwords = hotwords
        self._on_text = on_text
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=512)
        self._resampler: rtc.AudioResampler | None = None
        self._resampler_rate: int | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="online-interrupt")

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def push(self, frame: rtc.AudioFrame) -> None:
        if self._closed:
            return
        pcm = self._to_pcm(frame)
        if not pcm:
            return
        try:
            self._queue.put_nowait(pcm)
        except asyncio.QueueFull:
            # 上传跟不上时丢最旧的，打断判定只关心最近的话音
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(pcm)

    def _to_pcm(self, frame: rtc.AudioFrame) -> bytes:
        if frame.sample_rate == self._config.sample_rate and frame.num_channels == 1:
            return bytes(frame.data)
        if self._resampler is None or self._resampler_rate != frame.sample_rate:
            self._resampler = rtc.AudioResampler(
                input_rate=frame.sample_rate,
                output_rate=self._config.sample_rate,
                num_channels=1,
                quality=rtc.AudioResamplerQuality.MEDIUM,
            )
            self._resampler_rate = frame.sample_rate
        return b"".join(bytes(out.data) for out in self._resampler.push(frame))

    async def _run(self) -> None:
        session = aiohttp.ClientSession()
        try:
            while not self._closed:
                try:
                    await self._run_ws(session)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — 旁路链路，任何错都只重连不冒泡
                    logger.warning("online interrupt stream error: %s", exc)
                if not self._closed:
                    await asyncio.sleep(self._config.reconnect_delay)
        finally:
            await session.close()

    async def _run_ws(self, session: aiohttp.ClientSession) -> None:
        assert self._config.ws_url is not None
        ssl_ctx = None
        if self._config.ws_url.startswith("wss://") and not self._config.verify_ssl:
            ssl_ctx = ssl._create_unverified_context()

        async with session.ws_connect(self._config.ws_url, ssl=ssl_ctx, heartbeat=30) as ws:
            init_payload = {
                "mode": "2pass",
                # 主链路用标准 600ms 块（[5,10,5]）；这条旁路改 480ms 块压首包延迟
                # （首包 = 起音确认 + 凑满一块 + 解码 + RTT，凑块是最大可调项）。
                # 流式模型按 600ms 块训练，小块准确度会掉，但旁路转写只数字数不进
                # 对话，掉点无所谓。若服务端不认/回包异常，改回 [5, 10, 5]。
                "chunk_size": [5, 8, 4],
                "chunk_interval": 10,
                "wav_name": "online-interrupt",
                "wav_format": "pcm",
                "audio_fs": self._config.sample_rate,
                "is_speaking": True,
                "itn": False,
            }
            if self._hotwords:
                init_payload["hotwords"] = self._hotwords
            await ws.send_str(json.dumps(init_payload, ensure_ascii=False))
            logger.info("online interrupt stream connected: %s", self._config.ws_url)

            async def _send() -> None:
                while True:
                    pcm = await self._queue.get()
                    await ws.send_bytes(pcm)

            send_task = asyncio.create_task(_send(), name="online-interrupt-send")
            try:
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    payload = json.loads(msg.data)
                    mode = payload.get("mode", "")
                    text = (payload.get("text") or "").strip()
                    if mode == "2pass-online":
                        if text:
                            self._on_text(text, False)
                    elif mode == "2pass-offline":
                        # 段收尾：offline 结果是同段语音的重识别，只用作清累加信号
                        self._on_text(text, True)
            finally:
                send_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await send_task
        raise ConnectionError("online interrupt websocket closed")


class OnlineTapAudioInput(io.AudioInput):
    """透传包装：每帧原样返回给管线，同时旁路喂给 online 打断流。"""

    def __init__(self, source: io.AudioInput, tap: OnlineAsrTap) -> None:
        super().__init__(label="online-interrupt-tap", source=source)
        self._tap = tap

    async def __anext__(self) -> rtc.AudioFrame:
        frame = await super().__anext__()
        self._tap.push(frame)
        return frame


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_int(value: str | None, default: int) -> int:
    if not value:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
