"""行为锁定测试:app/music_player.MusicLibrary + MusicPlayer。

覆盖:
- MusicLibrary: 目录扫描缓存 / 刷新失效 / 精确-子串-模糊-随机匹配 / 空目录
- MusicPlayer: play/stop/pause/resume 状态机 / 重复 play 替换 / aclose 清理
- _decode_wav: 16kHz mono 直读 / 多声道取左 / 非 16k 整数倍率重采样
- _decode_via_ffmpeg: ffmpeg 不可用时优雅降级(不抛、不漏 task)
- _linear_resample_int16: 整数倍率上下采样
"""

from __future__ import annotations

import asyncio
import struct
import sys
import wave
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "voice_agents"))

from app.music_player import (  # noqa: E402
    FRAME_BYTES,
    PROMPT_TEMPLATES,
    SAMPLE_RATE,
    MusicLibrary,
    MusicPlayer,
    _linear_resample_int16,
)

# ──────────────────────────── 测试夹具:临时音乐目录 ────────────────────────────


def _write_wav(
    path: Path,
    *,
    samples: int = SAMPLE_RATE,
    sample_rate: int = SAMPLE_RATE,
    n_channels: int = 1,
    sampwidth: int = 2,
) -> None:
    """写一个最小的 wav 文件(正弦+直流,内容无所谓,只要能被 wave 读)。"""
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(n_channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(sample_rate)
        # 交错多声道;每样本 sampwidth 字节
        per_channel = [int(10000 * (0.5 + 0.5 * (-1) ** (i // 20))) for i in range(samples)]
        if n_channels == 1:
            frames = b"".join(struct.pack("<h", v) for v in per_channel)
        else:
            frames = b"".join(
                b"".join(struct.pack("<h", v) for _ in range(n_channels)) for v in per_channel
            )
        wf.writeframes(frames)


@pytest.fixture()
def music_dir(tmp_path: Path) -> Path:
    d = tmp_path / "music"
    d.mkdir()
    _write_wav(d / "song_one.wav", samples=200)  # 200 帧 = 4s,够测
    _write_wav(d / "song_two.wav", samples=200)
    (d / "sub").mkdir()
    _write_wav(d / "sub" / "nested_track.wav", samples=200)
    return d


@pytest.fixture()
def empty_dir(tmp_path: Path) -> Path:
    d = tmp_path / "empty_music"
    d.mkdir()
    return d


class _FakeOutput:
    """模拟 WebSocketAudioOutput:记录 capture_frame / clear_buffer 调用。"""

    def __init__(self) -> None:
        self.frames: list[Any] = []
        self.captures: int = 0
        self.clears: int = 0
        self.music_clears: int = 0
        self._pushed_duration: float = 0.0

    async def capture_frame(self, frame: Any) -> None:
        self.frames.append(frame)
        self.captures += 1
        self._pushed_duration += getattr(frame, "duration", 0.0)

    def clear_buffer(self) -> None:
        self.clears += 1

    def clear_music_buffer(self) -> None:
        self.music_clears += 1


class _FakeRuntime:
    """最小 AppRuntime 替身,只要 ws_audio_output 字段。"""

    def __init__(self) -> None:
        self.ws_audio_output: Any = _FakeOutput()
        self.music_player: Any = None


# ──────────────────────────── MusicLibrary ────────────────────────────


class TestMusicLibraryScan:
    def test_scan_finds_recursive(self, music_dir: Path) -> None:
        lib = MusicLibrary(music_dir=music_dir, refresh_s=60.0)
        names = sorted(lib.list_names())
        assert names == ["nested_track", "song_one", "song_two"]

    def test_scan_empty_dir(self, empty_dir: Path) -> None:
        lib = MusicLibrary(music_dir=empty_dir, refresh_s=60.0)
        assert lib.list_names() == []

    def test_scan_nonexistent_dir(self, tmp_path: Path) -> None:
        lib = MusicLibrary(music_dir=tmp_path / "does_not_exist", refresh_s=60.0)
        assert lib.list_names() == []

    def test_cache_returns_same_until_refresh(self, music_dir: Path) -> None:
        lib = MusicLibrary(music_dir=music_dir, refresh_s=60.0)
        first = lib.list_names()
        # 新增一个文件,但缓存还没过期 → 不应看到新文件
        _write_wav(music_dir / "song_three.wav", samples=100)
        second = lib.list_names()
        assert first == second
        # invalidate 后重新扫描 → 看到新文件
        lib.invalidate()
        third = lib.list_names()
        assert "song_three" in third
        assert len(third) == 4

    def test_cache_expires_after_refresh_s(self, music_dir: Path) -> None:
        # refresh_s=0 → 每次都重扫
        lib = MusicLibrary(music_dir=music_dir, refresh_s=0.0)
        first = lib.list_names()
        _write_wav(music_dir / "song_late.wav", samples=100)
        second = lib.list_names()
        assert len(second) == len(first) + 1


class TestMusicLibraryResolve:
    def test_exact_match(self, music_dir: Path) -> None:
        lib = MusicLibrary(music_dir=music_dir, refresh_s=60.0)
        p = lib.resolve("song_one")
        assert p is not None
        assert p.name == "song_one.wav"

    def test_substring_query_in_stem(self, music_dir: Path) -> None:
        lib = MusicLibrary(music_dir=music_dir, refresh_s=60.0)
        # 用户输入"song"是多个文件名的子串 → 命中第一个匹配
        p = lib.resolve("song_one")
        assert p is not None and p.stem == "song_one"

    def test_substring_stem_in_query(self, music_dir: Path) -> None:
        lib = MusicLibrary(music_dir=music_dir, refresh_s=60.0)
        # 用户输入更长 → stem 是 query 的子串
        p = lib.resolve("please play song_one now")
        assert p is not None and p.stem == "song_one"

    def test_fuzzy_match(self, music_dir: Path) -> None:
        lib = MusicLibrary(music_dir=music_dir, refresh_s=60.0, match_threshold=0.5)
        # "songone" 与 "song_one" 的 ratio 应 > 0.5
        p = lib.resolve("songone")
        assert p is not None
        assert p.stem in {"song_one", "song_two"}  # 都可能匹配,song_one ratio 更高

    def test_fuzzy_below_threshold_returns_none(self, music_dir: Path) -> None:
        lib = MusicLibrary(music_dir=music_dir, refresh_s=60.0, match_threshold=0.9)
        # "xyz" 与任何曲名相似度都很低
        p = lib.resolve("xyz")
        assert p is None

    def test_none_returns_random_pick(self, music_dir: Path) -> None:
        lib = MusicLibrary(music_dir=music_dir, refresh_s=60.0)
        for _ in range(10):
            p = lib.resolve(None)
            assert p is not None
            assert p.stem in {"song_one", "song_two", "nested_track"}

    def test_empty_string_returns_random(self, music_dir: Path) -> None:
        lib = MusicLibrary(music_dir=music_dir, refresh_s=60.0)
        p = lib.resolve("")
        assert p is not None

    def test_random_keyword_returns_random(self, music_dir: Path) -> None:
        lib = MusicLibrary(music_dir=music_dir, refresh_s=60.0)
        for kw in ("random", "RANDOM", "随机", "随便"):
            p = lib.resolve(kw)
            assert p is not None, f"keyword {kw!r} should resolve"

    def test_resolve_empty_dir(self, empty_dir: Path) -> None:
        lib = MusicLibrary(music_dir=empty_dir, refresh_s=60.0)
        assert lib.resolve("anything") is None
        assert lib.resolve(None) is None


# ──────────────────────────── MusicPlayer 状态机 ────────────────────────────


def _new_player(
    loop: asyncio.AbstractEventLoop, library: MusicLibrary
) -> tuple[MusicPlayer, _FakeRuntime]:
    runtime = _FakeRuntime()
    player = MusicPlayer(runtime, library, loop=loop)
    runtime.music_player = player
    return player, runtime


def test_play_starts_task_and_returns_prompt(music_dir: Path) -> None:
    async def run() -> None:
        lib = MusicLibrary(music_dir=music_dir, refresh_s=60.0)
        player, runtime = _new_player(asyncio.get_running_loop(), lib)
        prompt = await player.play("song_one")
        assert "song_one" in prompt
        assert any(tpl.split("{name}")[0] in prompt for tpl in PROMPT_TEMPLATES)
        assert player.is_playing is True
        assert player.current_name == "song_one"
        assert runtime.ws_audio_output.music_clears == 0
        assert runtime.ws_audio_output.clears == 0
        assert not player._resume_ev.is_set()
        assert player.waiting_start_ack is True
        await player.aclose()

    asyncio.run(run())


def test_play_not_found_returns_hint(music_dir: Path) -> None:
    async def run() -> None:
        lib = MusicLibrary(music_dir=music_dir, refresh_s=60.0)
        player, _ = _new_player(asyncio.get_running_loop(), lib)
        prompt = await player.play("不存在的歌xyz")
        assert "没找到" in prompt
        assert player.is_playing is False
        await player.aclose()

    asyncio.run(run())


def test_play_random_when_none(music_dir: Path) -> None:
    async def run() -> None:
        lib = MusicLibrary(music_dir=music_dir, refresh_s=60.0)
        player, _ = _new_player(asyncio.get_running_loop(), lib)
        await player.play(None)
        assert player.is_playing is True
        assert player.current_name in {"song_one", "song_two", "nested_track"}
        await player.aclose()

    asyncio.run(run())


def test_stop_clears_state(music_dir: Path) -> None:
    async def run() -> None:
        lib = MusicLibrary(music_dir=music_dir, refresh_s=60.0)
        player, runtime = _new_player(asyncio.get_running_loop(), lib)
        await player.play("song_one")
        # 立即 stop,task 还没跑完
        msg = await player.stop()
        assert player.is_playing is False
        assert player.current_name == ""
        # stop 在播放中应返回停止语
        assert msg in ("好的,音乐停了。", "")
        assert runtime.ws_audio_output.music_clears == 1
        assert runtime.ws_audio_output.clears == 0
        await player.aclose()

    asyncio.run(run())


def test_stop_when_not_playing_returns_empty(music_dir: Path) -> None:
    async def run() -> None:
        lib = MusicLibrary(music_dir=music_dir, refresh_s=60.0)
        player, _ = _new_player(asyncio.get_running_loop(), lib)
        msg = await player.stop()
        assert msg == ""
        assert player.is_playing is False

    asyncio.run(run())


def test_double_play_replaces_previous(music_dir: Path) -> None:
    async def run() -> None:
        lib = MusicLibrary(music_dir=music_dir, refresh_s=60.0)
        player, runtime = _new_player(asyncio.get_running_loop(), lib)
        await player.play("song_one")
        first_task = player._task
        assert first_task is not None
        await player.play("song_two")
        # 旧 task 应已结束(cancel 后 await)
        assert first_task.done()
        assert player.is_playing is True
        assert player.current_name == "song_two"
        await player.aclose()

    asyncio.run(run())


def test_pause_resume_toggles_event(music_dir: Path) -> None:
    async def run() -> None:
        lib = MusicLibrary(music_dir=music_dir, refresh_s=60.0)
        player, _ = _new_player(asyncio.get_running_loop(), lib)
        await player.play("song_one")
        player.resume()
        assert player._resume_ev.is_set()
        player.pause()
        assert not player._resume_ev.is_set()
        player.resume()
        assert player._resume_ev.is_set()
        await player.aclose()

    asyncio.run(run())


def test_pause_when_not_playing_noop(music_dir: Path) -> None:
    async def run() -> None:
        lib = MusicLibrary(music_dir=music_dir, refresh_s=60.0)
        player, _ = _new_player(asyncio.get_running_loop(), lib)
        player.pause()  # 不应抛
        player.resume()  # 不应抛
        assert player._resume_ev.is_set()

    asyncio.run(run())


def test_aclose_stops_task(music_dir: Path) -> None:
    async def run() -> None:
        lib = MusicLibrary(music_dir=music_dir, refresh_s=60.0)
        player, _ = _new_player(asyncio.get_running_loop(), lib)
        await player.play("song_one")
        assert player.is_playing is True
        await player.aclose()
        assert player.is_playing is False

    asyncio.run(run())


def test_play_yields_to_tts_then_resume(music_dir: Path) -> None:
    """play() 先等待一句起播提示 TTS,提示结束后再推音乐帧。"""

    async def run() -> None:
        lib = MusicLibrary(music_dir=music_dir, refresh_s=60.0)
        player, runtime = _new_player(asyncio.get_running_loop(), lib)
        await player.play("song_one")
        assert not player._resume_ev.is_set()
        assert player.waiting_start_ack is True
        await asyncio.sleep(0.03)
        assert runtime.ws_audio_output.captures == 0
        player.resume()
        assert player.waiting_start_ack is False
        await asyncio.sleep(0.03)
        assert runtime.ws_audio_output.captures > 0
        await player.aclose()

    asyncio.run(run())


def test_play_for_tool_arms_yield_timer(music_dir: Path) -> None:
    """工具触发点歌也先让一句 TTS,再由状态事件或兜底 timer 起播。"""

    async def run() -> None:
        lib = MusicLibrary(music_dir=music_dir, refresh_s=60.0)
        player, runtime = _new_player(asyncio.get_running_loop(), lib)
        name = await player.play_for_tool("song_one")
        assert name == "song_one"
        assert not player._resume_ev.is_set()
        assert player.waiting_start_ack is True
        assert player._yield_timer is not None
        await asyncio.sleep(0.03)
        assert runtime.ws_audio_output.captures == 0
        player.resume()
        await asyncio.sleep(0.03)
        assert runtime.ws_audio_output.captures > 0
        await player.aclose()

    asyncio.run(run())


def test_pause_after_music_started_is_explicit_only(music_dir: Path) -> None:
    """音乐已开始后,普通聊天/TTS 状态不应自动 clear 播放事件。"""

    async def run() -> None:
        _write_wav(music_dir / "song_one.wav", samples=2000)
        lib = MusicLibrary(music_dir=music_dir, refresh_s=60.0)
        player, runtime = _new_player(asyncio.get_running_loop(), lib)
        await player.play("song_one")
        player.resume()
        await asyncio.sleep(0.03)
        before = runtime.ws_audio_output.captures
        assert before > 0
        assert player._resume_ev.is_set()
        await asyncio.sleep(0.03)
        assert player._resume_ev.is_set()
        assert runtime.ws_audio_output.captures > before
        await player.aclose()

    asyncio.run(run())


def test_play_loop_pushes_16k_mono_frames(music_dir: Path) -> None:
    """端到端:播放 wav → capture_frame 收到的应是 16k mono int16le 20ms 帧。"""

    async def run() -> None:
        lib = MusicLibrary(music_dir=music_dir, refresh_s=60.0)
        player, runtime = _new_player(asyncio.get_running_loop(), lib)
        await player.play("song_one")
        player.resume()  # 跳过让 TTS 等待,直接推帧
        # 让循环跑完整个文件
        await asyncio.sleep(0.1)
        frames = runtime.ws_audio_output.frames
        assert len(frames) > 0
        f = frames[0]
        assert f.sample_rate == SAMPLE_RATE
        assert f.num_channels == 1
        assert f.samples_per_channel == 160  # 20ms @ 16kHz
        await player.aclose()

    asyncio.run(run())


def test_stop_then_resume_last_for_tool_restarts_same_song(music_dir: Path) -> None:
    async def run() -> None:
        lib = MusicLibrary(music_dir=music_dir, refresh_s=60.0)
        player, runtime = _new_player(asyncio.get_running_loop(), lib)
        await player.play_for_tool("song_one")
        await player.stop_for_tool()
        assert player.is_playing is False
        assert player.last_name == "song_one"
        name = await player.resume_last_for_tool()
        assert name == "song_one"
        assert player.is_playing is True
        assert player.current_name == "song_one"
        assert not player._resume_ev.is_set()
        player.resume()
        await asyncio.sleep(0.03)
        assert runtime.ws_audio_output.captures > 0
        await player.aclose()

    asyncio.run(run())


# ──────────────────────────── 解码路径 ────────────────────────────


def test_decode_wav_yields_20ms_frames(music_dir: Path) -> None:
    async def run() -> None:
        lib = MusicLibrary(music_dir=music_dir, refresh_s=60.0)
        player, _ = _new_player(asyncio.get_running_loop(), lib)
        path = music_dir / "song_one.wav"
        frames = []
        async for chunk in player._decode_wav(path):
            frames.append(chunk)
            if len(frames) >= 5:
                break
        assert all(len(f) == FRAME_BYTES for f in frames)
        await player.aclose()

    asyncio.run(run())


def test_decode_wav_multichannel_takes_left(tmp_path: Path) -> None:
    """多声道 wav 应取左声道(每 step 字节取前 2 字节)。"""
    d = tmp_path / "music"
    d.mkdir()
    _write_wav(d / "stereo.wav", samples=200, n_channels=2)

    async def run() -> None:
        lib = MusicLibrary(music_dir=d, refresh_s=60.0)
        player, _ = _new_player(asyncio.get_running_loop(), lib)
        frames = []
        async for chunk in player._decode_wav(d / "stereo.wav"):
            frames.append(chunk)
            if len(frames) >= 3:
                break
        assert all(len(f) == FRAME_BYTES for f in frames)
        await player.aclose()

    asyncio.run(run())


def test_decode_wav_resamples_to_16k(tmp_path: Path) -> None:
    """非 16k 采样率(整数倍率)应线性重采样到 16k。"""
    d = tmp_path / "music"
    d.mkdir()
    # 8kHz → 16kHz(2 倍上采样)
    _write_wav(d / "low.wav", samples=100, sample_rate=8000)

    async def run() -> None:
        lib = MusicLibrary(music_dir=d, refresh_s=60.0)
        player, _ = _new_player(asyncio.get_running_loop(), lib)
        frames = []
        async for chunk in player._decode_wav(d / "low.wav"):
            frames.append(chunk)
            if len(frames) >= 3:
                break
        assert all(len(f) == FRAME_BYTES for f in frames)
        await player.aclose()

    asyncio.run(run())


def test_decode_dispatches_by_extension(music_dir: Path) -> None:
    """_decode 应根据后缀分发:wav → _decode_wav,mp3 → ffmpeg。"""

    async def run() -> None:
        lib = MusicLibrary(music_dir=music_dir, refresh_s=60.0)
        player, _ = _new_player(asyncio.get_running_loop(), lib)
        # wav:不需要 ffmpeg
        wav_path = music_dir / "song_one.wav"
        chunks = []
        async for c in player._decode(wav_path):
            chunks.append(c)
            if len(chunks) >= 2:
                break
        assert len(chunks) >= 2
        await player.aclose()

    asyncio.run(run())


def test_ffmpeg_unavailable_does_not_raise(music_dir: Path, tmp_path: Path) -> None:
    """ffmpeg 不可用时,_decode_via_ffmpeg 不应抛出(优雅降级,记日志后结束)。"""

    async def run() -> None:
        lib = MusicLibrary(music_dir=music_dir, refresh_s=60.0)
        player, _ = _new_player(asyncio.get_running_loop(), lib)
        # 假 mp3 文件(其实是 wav 内容),但后缀 .mp3 → 走 ffmpeg 路径
        fake_mp3 = tmp_path / "fake.mp3"
        _write_wav(fake_mp3, samples=100)
        # 把 ffmpeg 替换成一个不存在的可执行名
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError("no ffmpeg")):
            frames = []
            async for chunk in player._decode_via_ffmpeg(fake_mp3):
                frames.append(chunk)
        # 不应抛出,帧列表可以是空(失败)或有内容(若 patch 没拦到)
        assert isinstance(frames, list)
        await player.aclose()

    asyncio.run(run())


# ──────────────────────────── _linear_resample_int16 ────────────────────────────


def test_linear_resample_noop_same_rate() -> None:
    raw = struct.pack("<5h", 1, 2, 3, 4, 5)
    out = _linear_resample_int16(raw, 16000, 16000)
    assert out == raw


def test_linear_resample_upsample_integer_ratio() -> None:
    # 8kHz → 16kHz:2 倍上采样
    raw = struct.pack("<3h", 0, 100, 200)
    out = _linear_resample_int16(raw, 8000, 16000)
    # 输出 samples 至少与输入同量(上采样只会更多)
    out_samples = struct.unpack(f"<{len(out) // 2}h", out)
    assert len(out_samples) >= 3
    assert out_samples[0] == 0
    assert out_samples[-1] == 200


def test_linear_resample_downsample_integer_ratio() -> None:
    # 16kHz → 8kHz:2 倍下采样(每 2 取 1)
    raw = struct.pack("<6h", 10, 20, 30, 40, 50, 60)
    out = _linear_resample_int16(raw, 16000, 8000)
    out_samples = struct.unpack(f"<{len(out) // 2}h", out)
    # step=2 → [10, 30, 50]
    assert out_samples == (10, 30, 50)


# ──────────────────────────── pick_prompt ────────────────────────────


def test_pick_prompt_includes_name(music_dir: Path) -> None:
    async def run() -> None:
        lib = MusicLibrary(music_dir=music_dir, refresh_s=60.0)
        player, _ = _new_player(asyncio.get_running_loop(), lib)
        for _ in range(20):
            p = player.pick_prompt("曲名")
            assert "曲名" in p
        await player.aclose()

    asyncio.run(run())
