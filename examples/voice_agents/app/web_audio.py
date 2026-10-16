"""浏览器 WebSocket 音频 I/O(WEB_AUDIO=1):/ws/audio 的 PCM 入向源与出向转发。"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from app.session_state import runtime
from webpanel.bridge import audio_client_count, broadcast_audio, broadcast_audio_ctrl

from livekit import rtc
from livekit.agents.voice import io

logger = logging.getLogger("web-ui-agent")


class WebSocketAudioInput(io.AudioInput):
    """Audio source fed by binary PCM frames arriving over /ws/audio WebSocket."""

    SAMPLE_RATE = 16_000
    SAMPLES_PER_FRAME = 160
    # 门控生效且无客户端时的重查间隔: 只影响"客户端注册后静音恢复"的时延(≤该值),
    # 真实音频帧由队列唤醒, 不经此间隔。
    GATED_POLL_INTERVAL_S = 1.0

    def __init__(self) -> None:
        super().__init__(label="ws-audio-input")
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=400)
        self._silence = bytes(self.SAMPLES_PER_FRAME * 2)
        self._buf = bytearray()
        self._pump_gate = self._env_pump_gate()

    @classmethod
    def _env_pump_gate(cls) -> bool:
        # XIAOGE_SILENCE_PUMP_GATE=0 关掉门控, 回到"无客户端也持续泵静音"的旧行为。
        raw = os.getenv("XIAOGE_SILENCE_PUMP_GATE")
        if raw is None or not raw.strip():
            return True
        return raw.strip().lower() in ("1", "true", "yes", "on")

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
        # 静音泵门控: 无音频侧客户端时不合成静音帧。空转的 50ms 定时唤醒会让
        # livekit Rust 管线与 funasr 流常烧 ~1.8 核/worker。有客户端(含 R522 会话
        # 与麦克风被拒的会话, 都注册进 panel.audio_ws_clients)时保持旧行为不变。
        while True:
            if self._pump_gate and audio_client_count() == 0:
                try:
                    data = await asyncio.wait_for(
                        self._queue.get(), timeout=self.GATED_POLL_INTERVAL_S
                    )
                except asyncio.TimeoutError:
                    continue  # 仍无客户端: 不发帧, 重查门控
                return self._frame(data)
            try:
                data = await asyncio.wait_for(self._queue.get(), timeout=0.05)
            except asyncio.TimeoutError:
                data = self._silence
            return self._frame(data)

    def _frame(self, data: bytes) -> rtc.AudioFrame:
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
        self.burst_start_ts: float | None = None  # 本音频突发首帧 epoch(setup_taps e2e 锚点)
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
            # 本轮"突发"首帧(_pushed_duration 归零后第一帧 = TTS 真实推流首帧):
            # web/headless 无 playback_started 事件,setup_taps 以此作 e2e 起播锚点。
            self.burst_start_ts = time.time()
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
                if not self._pushed_duration:
                    # TTS 真实出声的首帧:让位(音乐暂停推帧,播报完恢复)。
                    # 放这而不是上面的突发首帧块:这里 stale 抑制窗已过,保证
                    # 只有真会播出去的语音才触发暂停;整段被抑制的 TTS 不会
                    # 让音乐白等一个不会来的 playout 结束(另有 15s 兜底)。
                    self._yield_music_to_tts()
                self._pushed_duration += frame.duration

    async def capture_music_frame(self, frame: rtc.AudioFrame) -> None:
        """音乐专用通道(music_player._push_frame 调用):只广播,不碰 TTS 侧账目。

        与 capture_frame 的差异:
        - 不走 super().capture_frame:base 的播放段计数/flush 语义纯归 TTS;
        - 不做 stale 抑制:那是给打断后的残留 TTS 帧用的,音乐从不该被它拦;
        - 不触发让位:音乐帧本身不是 TTS;
        - 不累计 _pushed_duration / 不设 burst_start_ts(那是 TTS e2e 锚点)。
        """
        if self.next_in_chain is not None:
            try:
                await self.next_in_chain.capture_frame(frame)
            except Exception as exc:
                logger.debug("local music output skipped: %s", exc)
            return
        pcm = self._to_pcm16(frame)
        if pcm:
            broadcast_audio(pcm)

    def _music_player(self):
        try:
            return runtime.music_player
        except Exception:
            return None

    def _yield_music_to_tts(self) -> None:
        """TTS 出声首帧:让在播音乐暂停推帧(已推的浏览器播完属正常拖尾)。"""
        player = self._music_player()
        if player is None:
            return
        try:
            if getattr(player, "is_playing", False):
                yield_fn = getattr(player, "yield_for_tts", None)
                if callable(yield_fn):
                    yield_fn()
        except Exception:
            logger.debug("music yield for tts failed", exc_info=True)

    def _resume_music_after_tts(self) -> None:
        """TTS 播报 playout 结束(或被打断):恢复音乐推帧。"""
        player = self._music_player()
        if player is None:
            return
        try:
            if getattr(player, "is_playing", False):
                resume_fn = getattr(player, "resume", None)
                if callable(resume_fn):
                    resume_fn()
        except Exception:
            logger.debug("music resume after tts failed", exc_info=True)

    def flush(self) -> None:
        super().flush()
        if self.next_in_chain is not None:
            self.next_in_chain.flush()
        elif self._pushed_duration > 0:
            if self._flush_task and not self._flush_task.done():
                self._flush_task.cancel()
            self._flush_task = asyncio.create_task(self._headless_wait_for_playout())

    def clear_buffer(self) -> None:
        self._clear_buffer(suppress_tail=True)

    def clear_music_buffer(self) -> None:
        self._clear_buffer(suppress_tail=False)

    def _clear_buffer(self, *, suppress_tail: bool) -> None:
        self._drop_audio_until = (
            time.monotonic() + self._clear_suppress_tail_s if suppress_tail else 0.0
        )
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
        # 播报结束(正常播完或被打断):恢复让位的音乐。若音乐这会儿没在播
        # (停了/自然结束),resume 内部与这里的 is_playing 门都会兜住,幂等。
        self._resume_music_after_tts()
