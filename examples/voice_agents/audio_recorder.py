"""音频录制：把麦克风输入和 TTS 输出混音成单个对话 WAV 文件。

麦克风帧作为时间骨架连续写入，TTS 帧到达后缓存进混音队列，
与下一批麦克风帧叠加后一起落盘。两路都重采样到 16 kHz mono。

用法：
    recorder = AudioRecorder(session_dir="recordings")
    recorder.install(session)
    ctx.add_shutdown_callback(recorder.aclose)

生成文件：
    recordings/<YYYYmmdd_HHMMSS>/conversation.wav
"""

from __future__ import annotations

import asyncio
import logging
import struct
import sys
import threading
import time
import traceback
import wave
from pathlib import Path
from typing import TYPE_CHECKING

from livekit import rtc
from livekit.agents.voice import io

if TYPE_CHECKING:
    from livekit.agents import AgentSession

logger = logging.getLogger("audio-recorder")

try:
    import numpy as _np

    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False


def _dbg(msg: str) -> None:
    print(f"[audio-recorder] {msg}", file=sys.stderr, flush=True)


def _mix_pcm16(mic: bytes, tts: bytes) -> bytes:
    """将 tts 叠加到 mic（同为 16-bit LE mono），长度以 mic 为准。"""
    if not tts:
        return mic
    n = min(len(mic), len(tts)) // 2  # 可混音的采样数
    if _HAS_NUMPY:
        a = _np.frombuffer(mic, dtype=_np.int16).astype(_np.int32)
        b = _np.frombuffer(tts[: n * 2], dtype=_np.int16).astype(_np.int32)
        a[:n] = _np.clip(a[:n] + b, -32768, 32767)
        return a.astype(_np.int16).tobytes()
    else:
        result = bytearray(mic)
        for i in range(n):
            off = i * 2
            m = struct.unpack_from("<h", mic, off)[0]
            t = struct.unpack_from("<h", tts, off)[0]
            struct.pack_into("<h", result, off, max(-32768, min(32767, m + t)))
        return bytes(result)


class _ConversationWavWriter:
    """麦克风 + TTS 混音写入单个 WAV 文件（16 kHz mono）。

    write_mic() 驱动时间线，write_tts() 缓存 TTS 等待混入。
    线程安全（write_* 在事件循环线程，close 在线程池）。
    """

    TARGET_RATE = 16_000

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._wav: wave.Wave_write | None = None
        self._closed = False
        self._frame_count = 0

        self._mic_rs: rtc.AudioResampler | None = None
        self._mic_rs_rate: int = 0
        self._tts_rs: rtc.AudioResampler | None = None
        self._tts_rs_rate: int = 0

        self._tts_buf: bytearray = bytearray()  # 待混入的 TTS PCM

    # ── 重采样 ────────────────────────────────────────────────────────────

    def _resample_mic(self, frame: rtc.AudioFrame) -> bytes:
        if frame.sample_rate == self.TARGET_RATE and frame.num_channels == 1:
            return bytes(frame.data)
        if self._mic_rs is None or self._mic_rs_rate != frame.sample_rate:
            self._mic_rs = rtc.AudioResampler(
                input_rate=frame.sample_rate,
                output_rate=self.TARGET_RATE,
                num_channels=1,
                quality=rtc.AudioResamplerQuality.MEDIUM,
            )
            self._mic_rs_rate = frame.sample_rate
        return b"".join(bytes(f.data) for f in self._mic_rs.push(frame))

    def _resample_tts(self, frame: rtc.AudioFrame) -> bytes:
        if frame.sample_rate == self.TARGET_RATE and frame.num_channels == 1:
            return bytes(frame.data)
        if self._tts_rs is None or self._tts_rs_rate != frame.sample_rate:
            self._tts_rs = rtc.AudioResampler(
                input_rate=frame.sample_rate,
                output_rate=self.TARGET_RATE,
                num_channels=1,
                quality=rtc.AudioResamplerQuality.MEDIUM,
            )
            self._tts_rs_rate = frame.sample_rate
        return b"".join(bytes(f.data) for f in self._tts_rs.push(frame))

    # ── 写入 ──────────────────────────────────────────────────────────────

    def _open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._wav = wave.open(str(self._path), "wb")
        self._wav.setnchannels(1)
        self._wav.setsampwidth(2)
        self._wav.setframerate(self.TARGET_RATE)
        _dbg(f"opened {self._path.name}  rate={self.TARGET_RATE}  ch=1")

    def write_mic(self, frame: rtc.AudioFrame) -> None:
        try:
            pcm = self._resample_mic(frame)
            if not pcm:
                return
            with self._lock:
                if self._closed:
                    return
                if self._wav is None:
                    self._open()
                # 取出等量的 TTS 缓存进行混音
                tts_slice = bytes(self._tts_buf[: len(pcm)])
                del self._tts_buf[: len(pcm)]
                self._wav.writeframes(_mix_pcm16(pcm, tts_slice))
                self._frame_count += 1
        except Exception:
            _dbg(f"write_mic error:\n{traceback.format_exc()}")

    def write_tts(self, frame: rtc.AudioFrame) -> None:
        try:
            pcm = self._resample_tts(frame)
            if not pcm:
                return
            with self._lock:
                if not self._closed:
                    self._tts_buf.extend(pcm)
        except Exception:
            _dbg(f"write_tts error:\n{traceback.format_exc()}")

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                if self._wav is not None:
                    if self._tts_buf:
                        # 结尾还有未混的 TTS，直接追加（无麦克风底噪更干净）
                        self._wav.writeframes(bytes(self._tts_buf))
                        self._tts_buf.clear()
                    self._wav.close()
                    _dbg(f"closed {self._path.name}  frames={self._frame_count}")
                else:
                    _dbg(f"close {self._path.name} — no frames written")


# ── AudioInput / AudioOutput 旁路 ─────────────────────────────────────────────


class RecordingTapAudioInput(io.AudioInput):
    def __init__(self, source: io.AudioInput, writer: _ConversationWavWriter) -> None:
        super().__init__(label="recording-tap-input", source=source)
        self._writer = writer

    async def __anext__(self) -> rtc.AudioFrame:
        frame = await super().__anext__()
        self._writer.write_mic(frame)
        return frame


class RecordingTapAudioOutput(io.AudioOutput):
    def __init__(self, next_output: io.AudioOutput, writer: _ConversationWavWriter) -> None:
        super().__init__(
            label="recording-tap-output",
            next_in_chain=next_output,
            sample_rate=next_output.sample_rate,
            capabilities=io.AudioOutputCapabilities(pause=next_output.can_pause),
        )
        self._writer = writer

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        if self.next_in_chain:
            await self.next_in_chain.capture_frame(frame)
        await super().capture_frame(frame)
        self._writer.write_tts(frame)

    def flush(self) -> None:
        super().flush()
        if self.next_in_chain:
            self.next_in_chain.flush()

    def clear_buffer(self) -> None:
        if self.next_in_chain:
            self.next_in_chain.clear_buffer()


# ── 对外接口 ──────────────────────────────────────────────────────────────────


class AudioRecorder:
    """安装到 AgentSession，把麦克风和 TTS 混音录成单个对话 WAV。"""

    def __init__(self, session_dir: str | Path = "recordings") -> None:
        ts = time.strftime("%Y%m%d_%H%M%S")
        self._dir = Path(session_dir) / ts
        self._writer: _ConversationWavWriter | None = None

    def install(self, session: AgentSession) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        _dbg(f"install()  dir={self._dir}")
        _dbg(f"  input.audio  = {session.input.audio!r}")
        _dbg(f"  output.audio = {session.output.audio!r}")

        self._writer = _ConversationWavWriter(self._dir / "conversation.wav")

        if session.input.audio is not None:
            session.input.audio = RecordingTapAudioInput(session.input.audio, self._writer)
            _dbg("mic tap installed")
        else:
            _dbg("WARNING: session.input.audio is None")

        if session.output.audio is not None:
            session.output.audio = RecordingTapAudioOutput(session.output.audio, self._writer)
            _dbg("tts tap installed")
        else:
            _dbg("WARNING: session.output.audio is None")

    async def aclose(self) -> None:
        await asyncio.to_thread(self._close_sync)

    def _close_sync(self) -> None:
        if self._writer:
            self._writer.close()
        _dbg(f"session closed: {self._dir}")

    @property
    def directory(self) -> Path:
        return self._dir
