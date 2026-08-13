"""本地音乐播放器:ffmpeg 解码 mp3 + wave 解码 wav,16kHz mono int16le 帧推 /ws/audio。

设计要点:
- 出向通道:复用 WebSocketAudioOutput.capture_frame(),不新建 WebSocket
- 让 TTS:agent_state == "speaking" 时暂停(asyncio.Event),TTS 结束后恢复
- 停止:task.cancel() + ws_audio_output.clear_buffer()
- 音乐发现:目录扫描缓存(默认 60s 刷新)+ difflib 模糊匹配(阈值 0.5)
- 解码:mp3 走 ffmpeg subprocess 流式读 stdout;wav 走 wave 标准库
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import os
import random
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from common.runtime import append_turn_log as _log
from webpanel.bridge import broadcast

from livekit import rtc

if TYPE_CHECKING:
    from app.session_state import AppRuntime

logger = logging.getLogger("web-ui-agent")

# 16kHz mono int16le,20ms 一帧(LiveKit AudioFrame 标准)
SAMPLE_RATE = 16_000
SAMPLES_PER_FRAME = 160  # 20ms
FRAME_BYTES = SAMPLES_PER_FRAME * 2
SUPPORTED_EXTS = (".mp3", ".wav")
DEFAULT_MATCH_THRESHOLD = 0.5
DEFAULT_REFRESH_S = 60.0
# 起播让 TTS 的兜底超时:LLM 拿到提示语后若 N 秒内 agent_state 没切回非 speaking,
# 强制 resume 起播(避免 LLM 不念提示语时音乐卡死)。
MUSIC_YIELD_TIMEOUT_S = float(os.getenv("XIAOGE_MUSIC_YIELD_TIMEOUT", "3.0"))
# 注意:这些模板是给 LLM 看的工具返回值,不是给用户直接念的话。LLM 会基于它生成
# 自然回应。所以模板要直陈"在播什么",避免"再来一首/换一个"之类会被误解成"换歌"的措辞。
PROMPT_TEMPLATES = (
    "已开始播放《{name}》",
    "正在播放《{name}》",
    "已为你放上《{name}》",
    "播放《{name}》",
)


@dataclass
class MusicLibrary:
    """音乐目录扫描缓存(仿 xiaozhi:目录 + refresh_time + 模糊匹配)。"""

    music_dir: Path
    refresh_s: float = DEFAULT_REFRESH_S
    match_threshold: float = DEFAULT_MATCH_THRESHOLD
    _files: list[Path] | None = None
    _scan_ts: float = 0.0

    def _scan(self) -> list[Path]:
        files: list[Path] = []
        if not self.music_dir.exists():
            return files
        for p in self.music_dir.rglob("*"):
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS:
                files.append(p)
        return files

    def _ensure_fresh(self) -> list[Path]:
        now = time.time()
        if self._files is None or now - self._scan_ts > self.refresh_s:
            self._files = self._scan()
            self._scan_ts = now
        return self._files

    def list_names(self) -> list[str]:
        """返回不带扩展名的曲名列表(用于 LLM 提示或匹配)。"""
        return [p.stem for p in self._ensure_fresh()]

    def resolve(self, music_id: str | None) -> Path | None:
        """music_id → 实际文件路径。None 或空 → 随机一首;模糊匹配(阈值)。"""
        files = self._ensure_fresh()
        if not files:
            return None
        if not music_id or music_id.strip().lower() in {"random", "随便", "随机"}:
            return random.choice(files)
        query = music_id.strip()
        # 1) 精确 stem 匹配
        for p in files:
            if p.stem == query:
                return p
        # 2) 子串包含(用户说"声动未来"能命中"声动未来-聚力同行")
        for p in files:
            if query in p.stem or p.stem in query:
                return p
        # 3) difflib 模糊匹配
        best: Path | None = None
        best_ratio = 0.0
        for p in files:
            ratio = difflib.SequenceMatcher(None, query, p.stem).ratio()
            if ratio > best_ratio and ratio >= self.match_threshold:
                best_ratio = ratio
                best = p
        return best

    def invalidate(self) -> None:
        """强制下次 resolve 重新扫描。"""
        self._files = None
        self._scan_ts = 0.0


class MusicPlayer:
    """音乐播放器:play/stop/pause/resume,推帧到 ws_audio_output。

    线程语义:实例方法在 agent 循环线程调用;内部 _play_loop 是该循环上的 task。
    """

    def __init__(
        self,
        runtime: AppRuntime,
        library: MusicLibrary,
        *,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._runtime = runtime
        self._library = library
        self._loop = loop
        self._task: asyncio.Task[None] | None = None
        self._resume_ev = asyncio.Event()
        self._resume_ev.set()  # 默认不暂停
        self._current_path: Path | None = None
        self._current_name: str = ""
        self._last_path: Path | None = None
        self._last_name: str = ""
        self._stopping = False
        self._yield_timer: asyncio.Task[None] | None = None
        self._waiting_start_ack = False

    @property
    def is_playing(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def waiting_start_ack(self) -> bool:
        return self._waiting_start_ack

    @property
    def current_name(self) -> str:
        return self._current_name

    @property
    def last_name(self) -> str:
        return self._last_name

    def pick_prompt(self, name: str) -> str:
        return random.choice(PROMPT_TEMPLATES).format(name=name)

    async def play(self, music_id: str | None) -> str:
        """开始播放指定曲目(或随机)。已在播则先停再起。返回提示语(供 LLM 回话)。"""
        path = self._library.resolve(music_id)
        if path is None:
            _log(f"MUSIC_PLAY_NOTFOUND query={music_id!r}")
            return "没找到这首歌呢,要不换一首?"
        await self._stop_internal()
        self._current_path = path
        self._current_name = path.stem
        self._last_path = path
        self._last_name = path.stem
        self._stopping = False
        self._waiting_start_ack = True
        self._resume_ev.clear()
        self._task = self._loop.create_task(self._play_loop(path))
        self._arm_yield_timer()
        prompt = self.pick_prompt(self._current_name)
        _log(f"MUSIC_PLAY name={self._current_name!r} path={path} (yield-to-tts)")
        return prompt

    async def play_for_tool(self, music_id: str | None) -> str | None:
        """LLM 工具调用入口:起播并让 TTS 提示先完成,返回曲名(供工具结果拼装)。"""
        path = self._library.resolve(music_id)
        if path is None:
            _log(f"MUSIC_PLAY_NOTFOUND query={music_id!r}")
            return None
        await self._stop_internal()
        self._current_path = path
        self._current_name = path.stem
        self._last_path = path
        self._last_name = path.stem
        self._stopping = False
        self._waiting_start_ack = True
        self._resume_ev.clear()
        self._task = self._loop.create_task(self._play_loop(path))
        self._arm_yield_timer()
        _log(f"MUSIC_PLAY name={self._current_name!r} path={path} (yield-to-tts, tool)")
        return self._current_name

    async def resume_last_for_tool(self) -> str | None:
        path = self._last_path
        if path is None:
            return None
        await self._stop_internal()
        self._current_path = path
        self._current_name = path.stem
        self._last_name = path.stem
        self._stopping = False
        self._waiting_start_ack = True
        self._resume_ev.clear()
        self._task = self._loop.create_task(self._play_loop(path))
        self._arm_yield_timer()
        _log(f"MUSIC_RESUME_LAST name={self._current_name!r} path={path} (yield-to-tts)")
        return self._current_name

    async def stop(self) -> str:
        """停止播放并清空出向缓冲(让浏览器立即停)。"""
        if not self.is_playing:
            return ""
        name = self._current_name
        await self._stop_internal()
        _log(f"MUSIC_STOP name={name!r}")
        return "好的,音乐停了。"

    async def stop_for_tool(self) -> bool:
        """LLM 工具调用入口:停止播放,返回是否确实在播。"""
        was = self.is_playing
        name = self._current_name
        await self._stop_internal()
        if was:
            _log(f"MUSIC_STOP name={name!r} (tool)")
        return was

    def pause(self) -> None:
        """TTS 起来时调用:暂停推帧(已推的浏览器播完,新帧阻塞)。"""
        if self.is_playing:
            self._resume_ev.clear()
            _log("MUSIC_PAUSE")

    def resume(self) -> None:
        """TTS 结束后调用:恢复推帧。"""
        if self.is_playing and not self._resume_ev.is_set():
            self._resume_ev.set()
            _log("MUSIC_RESUME")
        self._waiting_start_ack = False
        # 无论如何都 disarm 兜底 timer:要么正常 resume 了,要么本来就没 clear
        self._disarm_yield_timer()

    def _arm_yield_timer(self) -> None:
        """起一个兜底 timer:若 N 秒后 _resume_ev 还 clear,自动 set()。
        场景:LLM 拿到提示语但没念(直接 end_turn),agent_state 没切 speaking
        → 永远等不到 resume(),音乐卡死。兜底 N 秒后强制起播。
        """
        self._disarm_yield_timer()
        try:
            self._yield_timer = self._loop.create_task(self._yield_timer_body())
        except RuntimeError:
            pass  # loop 未运行,play() 不该被调到

    async def _yield_timer_body(self) -> None:
        try:
            await asyncio.sleep(MUSIC_YIELD_TIMEOUT_S)
            if not self._resume_ev.is_set():
                self._resume_ev.set()
                self._waiting_start_ack = False
                _log(f"MUSIC_YIELD_TIMEOUT auto-resume after {MUSIC_YIELD_TIMEOUT_S}s")
        except asyncio.CancelledError:
            pass

    def _disarm_yield_timer(self) -> None:
        t = self._yield_timer
        if t is not None and not t.done():
            t.cancel()
        self._yield_timer = None

    async def aclose(self) -> None:
        """关闭:停掉当前任务,释放资源。"""
        await self._stop_internal()

    async def _stop_internal(self) -> None:
        self._stopping = True
        self._waiting_start_ack = False
        self._disarm_yield_timer()
        self._resume_ev.set()  # 唤醒可能阻塞的循环
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
        self._current_path = None
        self._current_name = ""
        # 清浏览器出向缓冲(若可用),让正在播的尾音立即停
        output = self._runtime.ws_audio_output
        if output is not None:
            try:
                output.clear_buffer()
            except Exception:
                pass

    async def _play_loop(self, path: Path) -> None:
        """解码 + 推帧主循环。每 20ms 一帧,按实时速率节流 + 让 TTS。

        headless 模式下 WebSocketAudioOutput.next_in_chain=None,super().capture_frame
        不阻塞在播放队列 → 没有背压。若不节流,整个文件会在 0.2s 内灌进 ws 发送
        缓冲,要么撑爆 ws send queue,要么浏览器调度一堆 BufferSource 起点全挤
        在 nextPlayTime 上,实际播不出来。所以这里按 frame_duration 实时节流。
        """
        pushed = 0
        t0 = time.monotonic()
        frame_duration = SAMPLES_PER_FRAME / SAMPLE_RATE  # 0.02s
        next_t = t0 + frame_duration
        try:
            async for pcm_frame in self._decode(path):
                await self._resume_ev.wait()
                if self._stopping:
                    return
                # pause 期间也跟着挂钟走,避免 resume 后追帧 Burst
                next_t += frame_duration
                await self._push_frame(pcm_frame)
                pushed += 1
                if pushed == 1:
                    _log(f"MUSIC_PUSH_FIRST name={path.stem!r} after={time.monotonic()-t0:.2f}s")
                elif pushed % 250 == 0:  # 每 ~5s 一次,够看进度
                    _log(f"MUSIC_PUSH name={path.stem!r} frames={pushed} elapsed={time.monotonic()-t0:.1f}s")
                sleep_s = next_t - time.monotonic()
                if sleep_s > 0:
                    await asyncio.sleep(sleep_s)
                elif sleep_s < -frame_duration * 10:
                    # 落后超过 200ms,说明解码/推帧跟不上实时(罕见),重锚避免追帧 Burst
                    next_t = time.monotonic() + frame_duration
            _log(f"MUSIC_END name={path.stem!r} frames={pushed} elapsed={time.monotonic()-t0:.1f}s")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("music play loop failed: %s", path)
            _log(f"MUSIC_PLAY_ERROR name={path.stem!r}")

    async def _push_frame(self, pcm: bytes) -> None:
        """把 16kHz mono int16le PCM 帧推到 ws_audio_output。"""
        output = self._runtime.ws_audio_output
        if output is None:
            return
        frame = rtc.AudioFrame(
            data=pcm,
            sample_rate=SAMPLE_RATE,
            num_channels=1,
            samples_per_channel=SAMPLES_PER_FRAME,
        )
        try:
            await output.capture_frame(frame)
        except Exception as exc:
            logger.debug("music frame push skipped: %s", exc)

    async def _decode(self, path: Path) -> Any:
        """生成器:按文件类型解码,逐帧 yield 16kHz mono int16le PCM(20ms)。"""
        suffix = path.suffix.lower()
        if suffix == ".wav":
            async for frame in self._decode_wav(path):
                yield frame
        elif suffix == ".mp3":
            async for frame in self._decode_mp3_ffmpeg(path):
                yield frame
        else:
            logger.warning("unsupported music format: %s", path)

    async def _decode_wav(self, path: Path) -> Any:
        """wav:用 wave 标准库;若采样率/声道不对,转 ffmpeg 统一处理。"""
        try:
            with wave.open(str(path), "rb") as wf:
                n_channels = wf.getnchannels()
                sampwidth = wf.getsampwidth()
                framerate = wf.getframerate()
                # 只支持 16-bit PCM;其他格式让 ffmpeg 兜底
                if sampwidth != 2 or framerate not in (8000, 16000, 24000, 32000, 44100, 48000):
                    async for frame in self._decode_via_ffmpeg(path):
                        yield frame
                    return
                # 一次性读到内存(音乐文件通常 < 10MB,可接受)
                raw = wf.readframes(wf.getnframes())
            # 转换声道:多声道 → 取左声道(int16 数组取每隔 channel*2 字节)
            if n_channels > 1:
                left = bytearray()
                step = n_channels * 2
                for i in range(0, len(raw) - step + 1, step):
                    left.extend(raw[i : i + 2])
                raw = bytes(left)
            # 重采样到 16kHz(若需要):简单线性抽取/插值;非整数倍率走 ffmpeg 兜底
            if framerate != SAMPLE_RATE:
                if SAMPLE_RATE % framerate == 0 or framerate % SAMPLE_RATE == 0:
                    raw = _linear_resample_int16(raw, framerate, SAMPLE_RATE)
                else:
                    async for frame in self._decode_via_ffmpeg(path):
                        yield frame
                    return
            # 切成 20ms 帧
            for i in range(0, len(raw), FRAME_BYTES):
                chunk = raw[i : i + FRAME_BYTES]
                if len(chunk) < FRAME_BYTES:
                    chunk = chunk + bytes(FRAME_BYTES - len(chunk))  # 尾部补零
                yield chunk
        except Exception:
            logger.exception("wav decode failed: %s", path)
            async for frame in self._decode_via_ffmpeg(path):
                yield frame

    async def _decode_mp3_ffmpeg(self, path: Path) -> Any:
        """mp3:启动 ffmpeg 子进程,流式读 stdout PCM。"""
        async for frame in self._decode_via_ffmpeg(path):
            yield frame

    async def _decode_via_ffmpeg(self, path: Path) -> Any:
        """通用 ffmpeg 解码:输出 16kHz mono s16le,stdout 流式读。"""
        cmd = [
            "ffmpeg",
            "-loglevel", "error",
            "-i", str(path),
            "-ar", str(SAMPLE_RATE),
            "-ac", "1",
            "-f", "s16le",
            "pipe:1",
        ]
        proc: asyncio.subprocess.Process | None = None
        _log(f"MUSIC_FFMPEG_START path={path}")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert proc.stdout is not None
            buf = bytearray()
            while True:
                if self._stopping:
                    break
                chunk = await proc.stdout.read(8192)
                if not chunk:
                    break
                buf.extend(chunk)
                while len(buf) >= FRAME_BYTES:
                    frame = bytes(buf[:FRAME_BYTES])
                    del buf[:FRAME_BYTES]
                    yield frame
            await proc.wait()
            stderr_tail = b""
            if proc.stderr is not None:
                try:
                    stderr_tail = await asyncio.wait_for(proc.stderr.read(), timeout=0.5)
                except (asyncio.TimeoutError, Exception):
                    pass
            _log(
                f"MUSIC_FFMPEG_END rc={proc.returncode} "
                f"stderr={stderr_tail.decode('utf-8','replace')[:200]!r}"
            )
        except asyncio.CancelledError:
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise
        except Exception:
            logger.exception("ffmpeg decode failed: %s", path)
            _log(f"MUSIC_FFMPEG_FAIL path={path}")
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass


def _linear_resample_int16(raw: bytes, src_rate: int, dst_rate: int) -> bytes:
    """简单线性重采样 int16 PCM(仅整数倍率)。"""
    if src_rate == dst_rate:
        return raw
    import struct

    samples = struct.unpack(f"<{len(raw) // 2}h", raw)
    if dst_rate > src_rate:
        # 上采样:线性插值
        ratio = dst_rate / src_rate
        out: list[int] = []
        for i in range(len(samples) - 1):
            out.append(samples[i])
            # 插值点
            for j in range(1, int(ratio)):
                t = j / ratio
                out.append(int(samples[i] * (1 - t) + samples[i + 1] * t))
        out.append(samples[-1])
        return struct.pack(f"<{len(out)}h", *out)
    else:
        # 下采样:整数抽取
        step = src_rate // dst_rate
        out = samples[::step]
        return struct.pack(f"<{len(out)}h", *out)


def broadcast_music_state(state: str, name: str = "") -> None:
    """通知前端音乐状态变化(可选,前端可显示播放器)。"""
    msg: dict[str, Any] = {"type": "music", "state": state}
    if name:
        msg["name"] = name
    broadcast(msg)
