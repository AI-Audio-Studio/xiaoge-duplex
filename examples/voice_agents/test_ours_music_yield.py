#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""音乐让位 TTS(yield-for-tts)单测 — 2026-09-16。

交叉播放根因:pause() 在 web/headless 形态零调用方(agent_state_changed 从不
派发 speaking,setup_taps 实测 27 会话)→ 播报 TTS 帧与音乐帧同经
capture_frame → broadcast_audio 同一 WS 流逐帧交错。

修复三件:
  ① web_audio: TTS 真实出声首帧 → player.yield_for_tts();
     播报 playout 结束(_headless_wait_for_playout)→ player.resume()
  ② music_player: yield_for_tts() 暂停推帧 + 15s 暂停兜底(回调不至防卡死),
     起播等待中调用则把 10s 起播兜底换成暂停兜底(长提示语不被压断)
  ③ 音乐帧改走 capture_music_frame 独立通道,不污染 TTS 段计数/突发锚点
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import music_player as mp
from app import web_audio as wa
from app.music_player import MusicLibrary, MusicPlayer
from app.session_state import runtime
from livekit import rtc


def _frame() -> rtc.AudioFrame:
    return rtc.AudioFrame(
        data=b"\x00" * 320, sample_rate=16_000, num_channels=1, samples_per_channel=160
    )


class _FakePlayer:
    """只带让位接口的假播放器(计数器验证 web_audio 侧触发时机)。"""

    def __init__(self, playing: bool = True) -> None:
        self.playing = playing
        self.yields = 0
        self.resumes = 0

    @property
    def is_playing(self) -> bool:
        return self.playing

    def yield_for_tts(self) -> None:
        self.yields += 1

    def resume(self) -> None:
        self.resumes += 1


# ---------------------------------------------------------------- web_audio 侧


def test_tts_burst_pauses_and_playout_resumes(monkeypatch):
    """TTS 出声首帧触发让位(一次突发只触发一次),播报 playout 结束恢复。"""
    player = _FakePlayer()
    monkeypatch.setattr(runtime, "music_player", player, raising=False)

    async def run():
        out = wa.WebSocketAudioOutput()
        for _ in range(3):
            await out.capture_frame(_frame())
        assert player.yields == 1  # 首帧让位,后续帧不重复
        assert player.resumes == 0
        out.flush()
        await asyncio.sleep(0.15)  # 播放时长 3*0.02s,等 playout 结论
        assert player.resumes == 1

    asyncio.run(run())


def test_music_frames_dont_pollute_tts_accounting(monkeypatch):
    """音乐帧走独立通道:不计入 _pushed_duration/不设 burst 锚点/不触发让位。"""
    player = _FakePlayer()
    monkeypatch.setattr(runtime, "music_player", player, raising=False)

    async def run():
        out = wa.WebSocketAudioOutput()
        for _ in range(2):
            await out.capture_music_frame(_frame())
        assert out._pushed_duration == 0.0
        assert out.burst_start_ts is None
        assert player.yields == 0
        # 随后 TTS 帧照常:突发锚点在 TTS 首帧才设,让位触发
        await out.capture_frame(_frame())
        assert out.burst_start_ts is not None
        assert player.yields == 1
        assert out._pushed_duration > 0.0

    asyncio.run(run())


def test_yield_noop_when_player_absent_or_idle(monkeypatch):
    """music_player 缺失 / 不在播:首帧让位空转,不炸。"""
    monkeypatch.setattr(runtime, "music_player", None, raising=False)

    async def run():
        out = wa.WebSocketAudioOutput()
        await out.capture_frame(_frame())  # player=None
        out.flush()
        await asyncio.sleep(0.05)
        # 不在播的 player
        idle = _FakePlayer(playing=False)
        monkeypatch.setattr(runtime, "music_player", idle, raising=False)
        await out.capture_frame(_frame())
        out.flush()
        await asyncio.sleep(0.05)
        assert idle.yields == 0 and idle.resumes == 0

    asyncio.run(run())


def test_interrupt_path_resumes_music(monkeypatch):
    """TTS 被打断(clear_buffer → _interrupted_ev):waiter 立即结论并恢复音乐。"""
    player = _FakePlayer()
    monkeypatch.setattr(runtime, "music_player", player, raising=False)

    async def run():
        out = wa.WebSocketAudioOutput()
        for _ in range(3):
            await out.capture_frame(_frame())
        assert player.yields == 1
        out.clear_buffer()  # 打断:_pushed_duration>0 → _interrupted_ev 置位
        out.flush()
        await asyncio.sleep(0.05)  # 无需等播放时长,interrupted 立即结论
        assert player.resumes == 1

    asyncio.run(run())


# ------------------------------------------------------------ music_player 侧


def _mk_player(rt: SimpleNamespace, loop: asyncio.AbstractEventLoop) -> MusicPlayer:
    return MusicPlayer(rt, MusicLibrary(Path("/nonexistent")), loop=loop)


def test_yield_for_tts_pauses_and_resume_restores():
    """在播时 yield_for_tts → ev clear;resume → ev set。"""
    async def run():
        loop = asyncio.get_running_loop()
        rt = SimpleNamespace(ws_audio_output=None)
        player = _mk_player(rt, loop)
        # 假任务让 is_playing=True(不真解码)
        player._task = loop.create_task(asyncio.Event().wait())
        try:
            assert player.is_playing
            player._resume_ev.set()
            player.yield_for_tts()
            assert not player._resume_ev.is_set()  # 已暂停
            player.resume()
            assert player._resume_ev.is_set()  # 已恢复
            assert not player._waiting_start_ack
        finally:
            player._task.cancel()
            try:
                await player._task
            except asyncio.CancelledError:
                pass

    asyncio.run(run())


def test_yield_for_tts_noop_when_not_playing():
    """不在播:yield_for_tts 直接返回,不挂兜底。"""
    async def run():
        loop = asyncio.get_running_loop()
        player = _mk_player(SimpleNamespace(ws_audio_output=None), loop)
        player.yield_for_tts()  # 无任务,不炸
        assert player._pause_fallback_task is None
        assert player._resume_ev.is_set()

    asyncio.run(run())


def test_pause_fallback_auto_resume(monkeypatch):
    """playout 回调不至:15s(测试改 0.05s)兜底强制恢复推帧。"""
    monkeypatch.setattr(mp, "MUSIC_TTS_PAUSE_FALLBACK_S", 0.05)

    async def run():
        loop = asyncio.get_running_loop()
        player = _mk_player(SimpleNamespace(ws_audio_output=None), loop)
        player._task = loop.create_task(asyncio.Event().wait())
        try:
            player._resume_ev.set()
            player.yield_for_tts()
            assert not player._resume_ev.is_set()
            await asyncio.sleep(0.2)
            assert player._resume_ev.is_set()  # 兜底已强制恢复
            assert not player._waiting_start_ack
        finally:
            player._task.cancel()
            try:
                await player._task
            except asyncio.CancelledError:
                pass

    asyncio.run(run())


def test_yield_during_start_wait_disarms_short_timer(monkeypatch):
    """起播等 ack 期间 TTS 起念:3s 起播兜底让位,音乐不被提前压上来。"""
    monkeypatch.setattr(mp, "MUSIC_YIELD_TIMEOUT_S", 0.05)
    monkeypatch.setattr(mp, "MUSIC_TTS_PAUSE_FALLBACK_S", 5.0)

    async def run():
        loop = asyncio.get_running_loop()
        player = _mk_player(SimpleNamespace(ws_audio_output=None), loop)
        player._task = loop.create_task(asyncio.Event().wait())
        try:
            # 模拟 play() 起播态:等 ack,ev clear,3s 兜底已挂
            player._waiting_start_ack = True
            player._resume_ev.clear()
            player._arm_yield_timer()
            # TTS 首帧到达(长提示语还在念)
            player.yield_for_tts()
            await asyncio.sleep(0.15)  # > 0.05s 起播兜底,远 < 5s 暂停兜底
            assert not player._resume_ev.is_set()  # 没被 3s timer 压上来
            player.resume()  # 提示语念完
            assert player._resume_ev.is_set()
        finally:
            player._task.cancel()
            try:
                await player._task
            except asyncio.CancelledError:
                pass

    asyncio.run(run())


def test_push_frame_prefers_music_channel():
    """新输出走 capture_music_frame;老输出(getattr 缺失)回落 capture_frame。"""

    class NewOutput:
        def __init__(self):
            self.music = 0
            self.tts = 0

        async def capture_music_frame(self, frame):
            self.music += 1

        async def capture_frame(self, frame):
            self.tts += 1

    class OldOutput:
        def __init__(self):
            self.n = 0

        async def capture_frame(self, frame):
            self.n += 1

    async def run():
        loop = asyncio.get_running_loop()
        new = NewOutput()
        player = _mk_player(SimpleNamespace(ws_audio_output=new), loop)
        await player._push_frame(b"\x00" * 320)
        assert new.music == 1 and new.tts == 0
        old = OldOutput()
        player2 = _mk_player(SimpleNamespace(ws_audio_output=old), loop)
        await player2._push_frame(b"\x00" * 320)
        assert old.n == 1

    asyncio.run(run())


def test_stop_disarms_pause_fallback():
    """停止时清暂停兜底,不留悬挂 task。"""
    async def run():
        loop = asyncio.get_running_loop()
        player = _mk_player(SimpleNamespace(ws_audio_output=None), loop)
        player._task = loop.create_task(asyncio.Event().wait())
        try:
            player._resume_ev.set()
            player.yield_for_tts()
            assert player._pause_fallback_task is not None
            await player._stop_internal()
            assert player._pause_fallback_task is None
            assert player._resume_ev.is_set()
        finally:
            if player.is_playing:
                await player._stop_internal()

    asyncio.run(run())
