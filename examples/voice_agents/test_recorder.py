"""时间顺序对齐的多轨录音(自动化测试 step 2)。

参考 duplexMVP2 replay/audio.py 的做法:每段音频按 at_us(与 EventTimeline 同源的
单调时钟)打时间戳;渲染时**按绝对时间放置**——start = (at_us - base_at_us) 换算成帧位,
空档填静音、轻微抖动归并——于是 user(你)与 assistant(小歌)落在**同一条真实时间轴**上,
而不是简单拼接。产出:
    runs/<ts>/user.wav        用户(麦克风),16k 单声道
    runs/<ts>/assistant.wav   小歌(TTS),16k 单声道
    runs/<ts>/duplex.wav      立体声:左=user / 右=assistant(可辨说话人与重叠/打断)
    runs/<ts>/audio_manifest.json

硬约束:**opt-in + 非阻塞 + 不外泄异常**。录制路径只做“重采样 + 加段(加锁,极快)”;
渲染与写盘全部在**后台线程**进行,绝不在事件循环上做磁盘 I/O。tap 始终原样透传音频帧、
转发 flush/clear_buffer,绝不吞帧、不破坏打断语义。

落盘采用**周期性快照 + 原子替换**(参考 duplexMVP2 的 LiveDuplexArtifactWriter):即使进程
被强杀(stop.ps1 force kill,不会跑 shutdown 回调),磁盘上也已有最近一次(≤flush_interval)
的完整产物;优雅关闭时再补一次最终刷盘。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import wave
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from livekit import rtc
from livekit.agents.voice import io

if TYPE_CHECKING:
    from livekit.agents import AgentSession

logger = logging.getLogger("test-recorder")

_TARGET_RATE = 16_000
_JITTER_TOLERANCE_US = 20_000  # 段起点与游标相差 ≤20ms 视作连续,避免抖动产生碎空档


def _now_us() -> int:
    return time.monotonic_ns() // 1_000


def _frames_to_us(frame_count: int, sample_rate: int) -> int:
    return int(round(frame_count * 1_000_000 / sample_rate)) if frame_count else 0


def _offset_frames(delta_us: int, sample_rate: int) -> int:
    return 0 if delta_us < 0 else int(round(delta_us * sample_rate / 1_000_000))


def _render_timeline_track(
    segments: list[tuple[int, np.ndarray]], *, base_at_us: int
) -> np.ndarray:
    """按 at_us 把各段放到真实时间位置(空档填静音、抖动归并)。返回 mono int16。"""
    if not segments:
        return np.zeros((0,), dtype=np.int16)
    ordered = sorted(segments, key=lambda s: s[0])
    tol = max(1, _offset_frames(_JITTER_TOLERANCE_US, _TARGET_RATE))
    placements: list[tuple[int, np.ndarray]] = []
    cursor = 0
    total = 0
    for at_us, mono in ordered:
        desired = _offset_frames(at_us - base_at_us, _TARGET_RATE)
        if not placements:
            start = desired
        elif abs(desired - cursor) <= tol or desired < cursor:
            start = cursor  # 紧贴上一段,避免碎空档/回退重叠
        else:
            start = desired
        placements.append((start, mono))
        cursor = start + int(mono.shape[0])
        total = max(total, cursor)
    track = np.zeros((total,), dtype=np.int16)
    for start, mono in placements:
        track[start : start + int(mono.shape[0])] = mono
    return track


def _write_wav_atomic(path: Path, data: np.ndarray, *, channels: int) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with wave.open(str(tmp), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(_TARGET_RATE)
        wav.writeframes(np.asarray(data, dtype=np.int16).reshape(-1).tobytes())
    os.replace(tmp, path)  # 原子替换,强杀也不会留半截文件


def _write_text_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class TestRecorder:
    """安装到 AgentSession,按真实时间轴录 user/assistant 双轨 + duplex 立体声。"""

    def __init__(self, run_dir: str | Path, *, flush_interval_s: float = 2.0) -> None:
        self._dir = Path(run_dir)
        self._flush_interval_s = max(0.5, float(flush_interval_s))
        self._lock = threading.Lock()
        # 每段存原始采样率的 mono PCM:(at_us, mono_int16, src_rate)。
        # 重采样推迟到渲染时一次性带 flush 做,避免热路径上的有状态重采样把短促音频
        # (如被打断的开场白)缓冲在 resampler 里丢掉。
        self._user: list[tuple[int, np.ndarray, int]] = []
        self._assistant: list[tuple[int, np.ndarray, int]] = []
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._run_writer, name="test-recorder", daemon=True)

    @property
    def directory(self) -> Path:
        return self._dir

    # ── 录制(事件循环线程,非阻塞:只做 frombuffer/下混 + 加锁追加)──────────
    @staticmethod
    def _to_mono(frame: rtc.AudioFrame) -> np.ndarray:
        arr = np.frombuffer(bytes(frame.data), dtype=np.int16)
        if frame.num_channels > 1:
            arr = arr.reshape(-1, frame.num_channels).mean(axis=1).astype(np.int16)
        return arr

    def record_user(self, frame: rtc.AudioFrame) -> None:
        try:
            arr = self._to_mono(frame)
            if arr.size:
                with self._lock:
                    self._user.append((_now_us(), arr.copy(), frame.sample_rate))
        except Exception:
            pass

    def record_assistant(self, frame: rtc.AudioFrame) -> None:
        try:
            arr = self._to_mono(frame)
            if arr.size:
                with self._lock:
                    self._assistant.append((_now_us(), arr.copy(), frame.sample_rate))
        except Exception:
            pass

    @staticmethod
    def _resample_to_target(mono: np.ndarray, src_rate: int) -> np.ndarray:
        """渲染时一次性重采样到 16k,带 flush,不丢尾部。"""
        if src_rate == _TARGET_RATE:
            return mono
        if mono.size == 0:
            return mono
        rs = rtc.AudioResampler(
            input_rate=src_rate, output_rate=_TARGET_RATE,
            num_channels=1, quality=rtc.AudioResamplerQuality.MEDIUM,
        )
        frame = rtc.AudioFrame(
            data=mono.tobytes(), sample_rate=src_rate,
            num_channels=1, samples_per_channel=int(mono.shape[0]),
        )
        out = list(rs.push(frame)) + list(rs.flush())
        if not out:
            return np.zeros((0,), dtype=np.int16)
        pcm = b"".join(bytes(f.data) for f in out)
        return np.frombuffer(pcm, dtype=np.int16)

    def install(self, session: "AgentSession") -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        if session.input.audio is not None:
            session.input.audio = _RecInput(session.input.audio, self)
        if session.output.audio is not None:
            session.output.audio = _RecOutput(session.output.audio, self)
        self._thread.start()  # 周期性后台刷盘:强杀也能留下最近一次完整产物

    # ── 后台周期刷盘(渲染 + 原子写,不阻塞事件循环)──────────────────────────
    def _run_writer(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self._flush_interval_s)
            self._wake.clear()
            self._flush_once()
        self._flush_once()  # 最终补刷

    def _flush_once(self) -> None:
        with self._lock:
            user = list(self._user)
            assistant = list(self._assistant)
        if not user and not assistant:
            return
        try:
            self._render_and_write(user, assistant)
        except Exception:
            logger.warning("test recorder flush failed", exc_info=True)

    def _render_and_write(
        self,
        user: list[tuple[int, np.ndarray, int]],
        assistant: list[tuple[int, np.ndarray, int]],
    ) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        # 渲染时统一重采样到 16k(带 flush,不丢音频),再按 at_us 放到真实时间轴。
        user16 = [(at, self._resample_to_target(m, r)) for (at, m, r) in user]
        assistant16 = [(at, self._resample_to_target(m, r)) for (at, m, r) in assistant]
        all_at = [s[0] for s in user] + [s[0] for s in assistant]
        base = min(all_at) if all_at else 0
        u = _render_timeline_track(user16, base_at_us=base)
        a = _render_timeline_track(assistant16, base_at_us=base)
        n = max(int(u.shape[0]), int(a.shape[0]))
        up = np.zeros((n,), dtype=np.int16); up[: u.shape[0]] = u
        ap = np.zeros((n,), dtype=np.int16); ap[: a.shape[0]] = a
        stereo = np.zeros((n, 2), dtype=np.int16)
        stereo[:, 0] = up  # 左 = 用户
        stereo[:, 1] = ap  # 右 = 助手
        _write_wav_atomic(self._dir / "user.wav", up, channels=1)
        _write_wav_atomic(self._dir / "assistant.wav", ap, channels=1)
        _write_wav_atomic(self._dir / "duplex.wav", stereo, channels=2)
        manifest = {
            "baseAtUs": base,
            "sampleRate": _TARGET_RATE,
            "format": "pcm_s16le_wav",
            "tracks": [
                {
                    "name": "user", "file": "user.wav", "channels": 1,
                    "firstSampleAtUs": min((s[0] for s in user), default=None),
                    "segmentCount": len(user),
                    "frameCount": int(up.shape[0]),
                    "durationUs": _frames_to_us(int(up.shape[0]), _TARGET_RATE),
                },
                {
                    "name": "assistant", "file": "assistant.wav", "channels": 1,
                    "firstSampleAtUs": min((s[0] for s in assistant), default=None),
                    "segmentCount": len(assistant),
                    "frameCount": int(ap.shape[0]),
                    "durationUs": _frames_to_us(int(ap.shape[0]), _TARGET_RATE),
                },
            ],
            "duplex": {
                "file": "duplex.wav", "channels": 2,
                "left": "user", "right": "assistant",
                "frameCount": n, "durationUs": _frames_to_us(n, _TARGET_RATE),
            },
        }
        _write_text_atomic(
            self._dir / "audio_manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )

    async def aclose(self) -> None:
        import asyncio

        self._stop.set()
        self._wake.set()
        try:
            await asyncio.to_thread(self._thread.join, 5.0)
        except Exception:
            pass


class _RecInput(io.AudioInput):
    def __init__(self, source: io.AudioInput, rec: TestRecorder) -> None:
        super().__init__(label="test-rec-input", source=source)
        self._rec = rec

    async def __anext__(self) -> rtc.AudioFrame:
        frame = await super().__anext__()
        self._rec.record_user(frame)
        return frame


class _RecOutput(io.AudioOutput):
    def __init__(self, next_output: io.AudioOutput, rec: TestRecorder) -> None:
        super().__init__(
            label="test-rec-output",
            next_in_chain=next_output,
            sample_rate=next_output.sample_rate,
            capabilities=io.AudioOutputCapabilities(pause=next_output.can_pause),
        )
        self._rec = rec

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        if self.next_in_chain:
            await self.next_in_chain.capture_frame(frame)
        await super().capture_frame(frame)
        self._rec.record_assistant(frame)

    def flush(self) -> None:
        super().flush()
        if self.next_in_chain:
            self.next_in_chain.flush()

    def clear_buffer(self) -> None:
        if self.next_in_chain:
            self.next_in_chain.clear_buffer()
