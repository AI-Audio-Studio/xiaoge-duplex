"""时间顺序对齐的多轨录音(自动化测试 step 2)。

做法对齐 duplexMVP2 replay/audio.py,核心原则是**如实记录、最少变换**:
  - 采集:把每个音频帧**原样**存为 (at_us, mono_int16, native_rate),**不在采集时重采样**
    (与 EventTimeline 同源的单调时钟打戳)。
  - 渲染:每段按 at_us 放到真实时间位置,但用**抖动容差 + 直接赋值(非求和)**——连续帧
    紧贴拼接(避免逐帧时间抖动造成的碎缝/咔哒声),只有真实静默处才留空;**绝不逐块重采样、
    绝不把连续帧相加**(那会破坏音质)。
  - 仅当 user / assistant 两路原生采样率不同,才对**整条轨道做一次**连续重采样统一到公共率
    (而不是逐块),保证音质。
产出:
  runs/<ts>/user.wav        用户(麦克风)
  runs/<ts>/assistant.wav   小歌(TTS)
  runs/<ts>/duplex.wav      立体声:左=user / 右=assistant
  runs/<ts>/audio_manifest.json

硬约束:opt-in + 非阻塞 + 不外泄异常。采集路径只 frombuffer/下混 + 加锁追加;渲染/写盘在
后台线程;周期刷盘 + 原子替换(强杀也留得下产物)。tap 原样透传帧、转发 flush/clear_buffer。
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
from common.taps import TapAudioInput, TapAudioOutput

from livekit import rtc
from livekit.agents.voice import io

if TYPE_CHECKING:
    from livekit.agents import AgentSession

logger = logging.getLogger("test-recorder")

_JITTER_TOLERANCE_US = 30_000  # 段起点与游标相差 ≤30ms 视作连续 -> 紧贴拼接,避免碎缝/咔哒


def _now_us() -> int:
    return time.monotonic_ns() // 1_000


def _frames(delta_us: int, rate: int) -> int:
    return 0 if delta_us < 0 else int(round(delta_us * rate / 1_000_000))


def _frames_to_us(frame_count: int, rate: int) -> int:
    return int(round(frame_count * 1_000_000 / rate)) if (frame_count and rate) else 0


def _smoothed_track(
    segments: list[tuple[int, np.ndarray]], *, rate: int, base_at_us: int
) -> np.ndarray:
    """按 at_us 放置(抖动容差内紧贴游标拼接、直接赋值),返回 mono int16 @ rate。

    连续采集的帧会被紧贴拼接成无缝音频;只有真实停顿(desired 远超 cursor)处才插静音。
    用赋值而非求和:同一路是单一连续流,本不该重叠,赋值避免叠加造成的失真/过响。
    """
    if not segments:
        return np.zeros((0,), dtype=np.int16)
    ordered = sorted(segments, key=lambda s: s[0])
    tol = max(1, _frames(_JITTER_TOLERANCE_US, rate))
    placements: list[tuple[int, np.ndarray]] = []
    cursor = 0
    total = 0
    for at_us, mono in ordered:
        desired = _frames(at_us - base_at_us, rate)
        if not placements:
            start = desired
        elif abs(desired - cursor) <= tol or desired < cursor:
            start = cursor  # 连续帧紧贴拼接(消除采集抖动的碎缝)
        else:
            start = desired  # 真实停顿:在真实时间点重新落位(不漂移)
        placements.append((start, mono))
        cursor = start + int(mono.shape[0])
        total = max(total, cursor)
    track = np.zeros((total,), dtype=np.int16)
    for start, mono in placements:
        track[start : start + int(mono.shape[0])] = mono
    return track


def _resample_whole(track: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """对**整条**连续轨道做一次重采样(非逐块),音质干净。"""
    if src_rate == dst_rate or track.size == 0:
        return track
    rs = rtc.AudioResampler(
        input_rate=src_rate,
        output_rate=dst_rate,
        num_channels=1,
        quality=rtc.AudioResamplerQuality.HIGH,
    )
    frame = rtc.AudioFrame(
        data=track.tobytes(),
        sample_rate=src_rate,
        num_channels=1,
        samples_per_channel=int(track.shape[0]),
    )
    out = list(rs.push(frame)) + list(rs.flush())
    if not out:
        return np.zeros((0,), dtype=np.int16)
    return np.frombuffer(b"".join(bytes(f.data) for f in out), dtype=np.int16)


def _write_wav_atomic(path: Path, data: np.ndarray, *, rate: int, channels: int) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with wave.open(str(tmp), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(np.asarray(data, dtype=np.int16).reshape(-1).tobytes())
    os.replace(tmp, path)


def _write_text_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class TestRecorder:
    """安装到 AgentSession,按真实时间轴录 user/assistant 双轨 + duplex 立体声。"""

    def __init__(
        self,
        run_dir: str | Path,
        *,
        flush_interval_s: float = 2.0,
        write_mono_tracks: bool = True,
    ) -> None:
        self._dir = Path(run_dir)
        self._flush_interval_s = max(0.5, float(flush_interval_s))
        # single 档=仅写 duplex.wav(立体声左右分轨仍可审计,K1);full 档=另写 user/assistant 单轨。
        self._write_mono_tracks = bool(write_mono_tracks)
        self._lock = threading.Lock()
        # 每段:(at_us, mono_int16_at_native_rate, native_rate)。采集不重采样。
        self._user: list[tuple[int, np.ndarray, int]] = []
        self._assistant: list[tuple[int, np.ndarray, int]] = []
        self._paused = False  # 麦克风关闭时暂停录用户轨(线程间用简单标志即可)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._run_writer, name="test-recorder", daemon=True)
        # 关闭前刷盘握手:stop.ps1 写 <repo>/.run/recorder.flush 作为信号,本线程见到后立刻
        # 停采 + 最终刷盘 + 写 recorder.flushed 回执,stop.ps1 收到回执再杀进程 -> 不掉音。
        try:
            self._signal_dir = self._dir.resolve().parents[1] / ".run"
        except Exception:
            self._signal_dir = self._dir
        self._flush_flag = self._signal_dir / "recorder.flush"
        self._flushed_mark = self._signal_dir / "recorder.flushed"

    @property
    def directory(self) -> Path:
        return self._dir

    def set_paused(self, paused: bool) -> None:
        """麦克风关闭 -> 暂停录用户轨;开启 -> 继续。按真实时间放置,暂停段在录音里
        表现为该时段的静默(如实反映"那段时间没采用户音")。助手轨不受影响。"""
        self._paused = bool(paused)
        logger.info(
            "recording %s (mic %s)", "paused" if paused else "resumed", "off" if paused else "on"
        )

    # ── 采集(事件循环线程,非阻塞:仅 frombuffer/下混 + 加锁追加)──────────────
    @staticmethod
    def _to_mono(frame: rtc.AudioFrame) -> np.ndarray:
        arr = np.frombuffer(bytes(frame.data), dtype=np.int16)
        if frame.num_channels > 1:
            arr = arr.reshape(-1, frame.num_channels).mean(axis=1).astype(np.int16)
        return arr

    def record_user(self, frame: rtc.AudioFrame) -> None:
        if self._paused:  # 麦克风关闭期间不录用户音
            return
        try:
            arr = self._to_mono(frame)
            if arr.size:
                with self._lock:
                    self._user.append((_now_us(), arr.copy(), int(frame.sample_rate)))
        except Exception:
            pass

    def record_assistant(self, frame: rtc.AudioFrame) -> None:
        try:
            arr = self._to_mono(frame)
            if arr.size:
                with self._lock:
                    self._assistant.append((_now_us(), arr.copy(), int(frame.sample_rate)))
        except Exception:
            pass

    def install(self, session: AgentSession) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        if session.input.audio is not None:
            session.input.audio = _RecInput(session.input.audio, self)
        if session.output.audio is not None:
            session.output.audio = _RecOutput(session.output.audio, self)
        self._thread.start()

    # ── 后台周期刷盘(渲染 + 原子写,不阻塞事件循环)──────────────────────────
    def _run_writer(self) -> None:
        last = 0.0
        while not self._stop.is_set():
            self._wake.wait(0.4)  # 0.4s 轮询:既能及时响应刷盘信号,又不频繁重渲染
            self._wake.clear()
            # 关闭前刷盘握手:见到 flush 信号 -> 停采 + 最终刷盘 + 写回执 -> 退出
            try:
                if self._flush_flag.exists():
                    self._paused = True
                    self._flush_once()
                    try:
                        self._signal_dir.mkdir(parents=True, exist_ok=True)
                        self._flushed_mark.write_text("ok", encoding="utf-8")
                    except Exception:
                        pass
                    return
            except Exception:
                pass
            now = time.monotonic()
            if now - last >= self._flush_interval_s:
                self._flush_once()
                last = now
        self._flush_once()  # 优雅关闭路径(aclose)下的最终刷盘

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

    @staticmethod
    def _native_rate(segs: list[tuple[int, np.ndarray, int]]) -> int | None:
        # 取出现样本最多的采样率作为该轨原生率(正常情况下整轨同一率)。
        if not segs:
            return None
        counts: dict[int, int] = {}
        for _at, m, r in segs:
            counts[r] = counts.get(r, 0) + int(m.shape[0])
        return max(counts, key=counts.get)

    def _render_and_write(
        self,
        user: list[tuple[int, np.ndarray, int]],
        assistant: list[tuple[int, np.ndarray, int]],
    ) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        all_at = [s[0] for s in user] + [s[0] for s in assistant]
        base = min(all_at) if all_at else 0

        ur = self._native_rate(user)
        ar = self._native_rate(assistant)
        out_rate = ur or ar or 16_000
        if ur and ar and ur != ar:
            out_rate = max(ur, ar)

        # 各轨按原生率平滑放置(不逐块重采样),再整轨一次性统一到 out_rate。
        u = (
            _smoothed_track([(a, m) for (a, m, _r) in user], rate=ur, base_at_us=base)
            if ur
            else np.zeros((0,), np.int16)
        )
        a = (
            _smoothed_track([(a2, m) for (a2, m, _r) in assistant], rate=ar, base_at_us=base)
            if ar
            else np.zeros((0,), np.int16)
        )
        if ur and ur != out_rate:
            u = _resample_whole(u, ur, out_rate)
        if ar and ar != out_rate:
            a = _resample_whole(a, ar, out_rate)

        n = max(int(u.shape[0]), int(a.shape[0]))
        up = np.zeros((n,), dtype=np.int16)
        up[: u.shape[0]] = u
        ap = np.zeros((n,), dtype=np.int16)
        ap[: a.shape[0]] = a
        stereo = np.zeros((n, 2), dtype=np.int16)
        stereo[:, 0] = up  # 左 = 用户
        stereo[:, 1] = ap  # 右 = 助手
        if self._write_mono_tracks:  # full 档:另写 user/assistant 单轨
            _write_wav_atomic(self._dir / "user.wav", up, rate=out_rate, channels=1)
            _write_wav_atomic(self._dir / "assistant.wav", ap, rate=out_rate, channels=1)
        _write_wav_atomic(self._dir / "duplex.wav", stereo, rate=out_rate, channels=2)
        mono_file = self._write_mono_tracks
        manifest = {
            "baseAtUs": base,
            "sampleRate": out_rate,
            "userNativeRate": ur,
            "assistantNativeRate": ar,
            "format": "pcm_s16le_wav",
            "tracks": [
                {
                    "name": "user",
                    "file": "user.wav"
                    if mono_file
                    else None,  # single 档单轨不落盘,数据在 duplex 左声道
                    "channels": 1,
                    "firstSampleAtUs": min((s[0] for s in user), default=None),
                    "segmentCount": len(user),
                    "frameCount": int(up.shape[0]),
                    "durationUs": _frames_to_us(int(up.shape[0]), out_rate),
                },
                {
                    "name": "assistant",
                    "file": "assistant.wav" if mono_file else None,  # single 档数据在 duplex 右声道
                    "channels": 1,
                    "firstSampleAtUs": min((s[0] for s in assistant), default=None),
                    "segmentCount": len(assistant),
                    "frameCount": int(ap.shape[0]),
                    "durationUs": _frames_to_us(int(ap.shape[0]), out_rate),
                },
            ],
            "duplex": {
                "file": "duplex.wav",
                "channels": 2,
                "left": "user",
                "right": "assistant",
                "frameCount": n,
                "durationUs": _frames_to_us(n, out_rate),
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


class _RecInput(TapAudioInput):
    def __init__(self, source: io.AudioInput, rec: TestRecorder) -> None:
        super().__init__(source, label="test-rec-input")
        self._rec = rec

    def _on_frame(self, frame: rtc.AudioFrame) -> None:
        self._rec.record_user(frame)


class _RecOutput(TapAudioOutput):
    def __init__(self, next_output: io.AudioOutput, rec: TestRecorder) -> None:
        super().__init__(next_output, label="test-rec-output")
        self._rec = rec

    def _on_frame(self, frame: rtc.AudioFrame) -> None:
        self._rec.record_assistant(frame)
