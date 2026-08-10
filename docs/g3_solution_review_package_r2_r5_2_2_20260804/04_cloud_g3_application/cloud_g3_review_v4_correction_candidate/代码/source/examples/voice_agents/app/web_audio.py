"""浏览器 WebSocket 音频 I/O(WEB_AUDIO=1):/ws/audio 的 PCM 入向源与出向转发。"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from webpanel.bridge import broadcast_audio, broadcast_audio_ctrl

from livekit import rtc
from livekit.agents.voice import io

logger = logging.getLogger("web-ui-agent")


class WebSocketAudioInput(io.AudioInput):
    """Audio source fed by binary PCM frames arriving over /ws/audio WebSocket."""

    SAMPLE_RATE = 16_000
    SAMPLES_PER_FRAME = 160

    def __init__(self) -> None:
        super().__init__(label="ws-audio-input")
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=400)
        self._silence = bytes(self.SAMPLES_PER_FRAME * 2)
        self._buf = bytearray()

    def _sync_push(self, data: bytes) -> None:
        self._buf.extend(data)
        frame_bytes = self.SAMPLES_PER_FRAME * 2
        while len(self._buf) >= frame_bytes:
            chunk = bytes(self._buf[:frame_bytes])
            del self._buf[:frame_bytes]
            try:
                self._queue.put_nowait(chunk)
            except asyncio.QueueFull:
                try:
                    self._queue.get_nowait()
                    self._queue.put_nowait(chunk)
                except Exception:
                    pass

    async def __anext__(self) -> rtc.AudioFrame:
        try:
            data = await asyncio.wait_for(self._queue.get(), timeout=0.05)
        except asyncio.TimeoutError:
            data = self._silence
        return rtc.AudioFrame(
            data=data,
            sample_rate=self.SAMPLE_RATE,
            num_channels=1,
            samples_per_channel=self.SAMPLES_PER_FRAME,
        )


class WebSocketAudioOutput(io.AudioOutput):
    """Forward TTS audio to /ws/audio clients, optionally wrapping local output."""

    TARGET_RATE = 16_000
    DEFAULT_CLEAR_SUPPRESS_TAIL_MS = 350

    def __init__(self, next_output: io.AudioOutput | None = None) -> None:
        sample_rate = next_output.sample_rate if next_output is not None else self.TARGET_RATE
        can_pause = next_output.can_pause if next_output is not None else False
        super().__init__(
            label="ws-audio-output",
            next_in_chain=next_output,
            sample_rate=sample_rate,
            capabilities=io.AudioOutputCapabilities(pause=can_pause),
        )
        self._rs: rtc.AudioResampler | None = None
        self._rs_rate: int = 0
        self._pushed_duration: float = 0.0
        self._capture_start: float = 0.0
        self._flush_task: asyncio.Task[None] | None = None
        self._interrupted_ev: asyncio.Event = asyncio.Event()
        self._drop_audio_until: float = 0.0

    @property
    def _clear_suppress_tail_s(self) -> float:
        raw = os.getenv("XIAOGE_CLEAR_SUPPRESS_TAIL_MS")
        if raw is None:
            return self.DEFAULT_CLEAR_SUPPRESS_TAIL_MS / 1000.0
        try:
            return max(0.0, int(raw)) / 1000.0
        except ValueError:
            return self.DEFAULT_CLEAR_SUPPRESS_TAIL_MS / 1000.0

    def _suppressing_stale_audio(self) -> bool:
        return time.monotonic() < self._drop_audio_until

    def _to_pcm16(self, frame: rtc.AudioFrame) -> bytes:
        if frame.sample_rate == self.TARGET_RATE and frame.num_channels == 1:
            return bytes(frame.data)
        if self._rs is None or self._rs_rate != frame.sample_rate:
            self._rs = rtc.AudioResampler(
                input_rate=frame.sample_rate,
                output_rate=self.TARGET_RATE,
                num_channels=1,
                quality=rtc.AudioResamplerQuality.MEDIUM,
            )
            self._rs_rate = frame.sample_rate
        return b"".join(bytes(f.data) for f in self._rs.push(frame))

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        if self.next_in_chain is None and not self._pushed_duration:
            self._capture_start = time.monotonic()
        if self.next_in_chain is not None:
            try:
                await self.next_in_chain.capture_frame(frame)
            except Exception as exc:
                logger.debug("local audio output skipped: %s", exc)
        await super().capture_frame(frame)
        if self._suppressing_stale_audio():
            logger.debug("drop stale websocket audio frame after clear")
            return
        pcm = self._to_pcm16(frame)
        if pcm:
            broadcast_audio(pcm)
            if self.next_in_chain is None:
                self._pushed_duration += frame.duration

    def flush(self) -> None:
        super().flush()
        if self.next_in_chain is not None:
            self.next_in_chain.flush()
        elif self._pushed_duration > 0:
            if self._flush_task and not self._flush_task.done():
                self._flush_task.cancel()
            self._flush_task = asyncio.create_task(self._headless_wait_for_playout())

    def clear_buffer(self) -> None:
        self._drop_audio_until = time.monotonic() + self._clear_suppress_tail_s
        if self.next_in_chain is not None:
            self.next_in_chain.clear_buffer()
        elif self._pushed_duration > 0:
            self._interrupted_ev.set()
        broadcast_audio_ctrl({"type": "clear"})

    async def _headless_wait_for_playout(self) -> None:
        total_duration = self._pushed_duration
        capture_start = self._capture_start
        interrupted_task = asyncio.create_task(self._interrupted_ev.wait())
        playout_task = asyncio.create_task(asyncio.sleep(total_duration))
        try:
            done, _ = await asyncio.wait(
                [interrupted_task, playout_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            interrupted = interrupted_task in done
        finally:
            interrupted_task.cancel()
            playout_task.cancel()
        if interrupted:
            elapsed = time.monotonic() - capture_start
            position = min(max(0.0, elapsed), total_duration)
        else:
            position = total_duration
        self.on_playback_finished(playback_position=position, interrupted=interrupted)
        self._pushed_duration = 0.0
        self._capture_start = 0.0
        self._interrupted_ev.clear()
