"""评审#2 单测:CosyVoice 陈旧连接恢复的归还时序(T1 终稿顺序)。

核心用例(T1-c):**冷建窗口注入取消**,断言旧连接被 close+归还各恰好一次、
state 处于安全态——把"取消穿透导致外层对已归还对象二次触碰"的矛盾锁进测试。
本测试按评审 §E 提醒先于实现编写:对修复前顺序必须失败(红),修复后通过(绿)。
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from websocket import WebSocketConnectionClosedException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "voice_agents"))

from providers.tts.cosyvoice import (  # noqa: E402
    _CosyVoiceCallback,
    _CosyVoiceSynthesizeStream,
)


class FakeSynth:
    def __init__(self, name: str, *, stale: bool = False) -> None:
        self.name = name
        self.stale = stale
        self.calls: list[str] = []
        self.closed = 0
        self.cancelled = 0
        self.completed = 0

    def streaming_call(self, text: str) -> None:
        if self.stale:
            raise WebSocketConnectionClosedException("Connection is already closed.")
        self.calls.append(text)

    def streaming_complete(self) -> None:
        self.completed += 1

    def streaming_cancel(self) -> None:
        self.cancelled += 1

    def close(self) -> None:
        self.closed += 1


class FakeTTS:
    sample_rate = 24000
    num_channels = 1

    def __init__(self, old: FakeSynth, build_fn) -> None:
        self._old = old
        self._build_fn = build_fn
        self.released: list[tuple[Any, bool]] = []

    def take_synth(self, callback):
        return self._old, True  # 借出陈旧池连接

    def _release_synth(self, synth, pooled) -> None:
        self.released.append((synth, pooled))

    def _build_synth(self, callback):
        return self._build_fn(callback)


def _make_stream(tts: FakeTTS, sentences_then_hang: bool) -> _CosyVoiceSynthesizeStream:
    stream = _CosyVoiceSynthesizeStream.__new__(_CosyVoiceSynthesizeStream)
    stream._tts = tts  # 只用到 _tts / _input_ch,绕过基类构造

    async def _input():
        yield "第一句。"
        if sentences_then_hang:
            await asyncio.Event().wait()  # 输入悬置:让取消只可能落在冷建窗口

    stream._input_ch = _input()
    return stream


def test_cancel_during_cold_rebuild_touches_old_exactly_once() -> None:
    """T1 核心:冷建窗口内取消 → 旧连接恰好 close 一次 + 归还一次,无二次触碰。"""
    old = FakeSynth("old", stale=True)
    build_started = threading.Event()
    build_unblock = threading.Event()

    def slow_build(callback):
        build_started.set()
        build_unblock.wait(10)  # 悬置在冷建窗口,等测试注入取消
        return FakeSynth("fresh")

    tts = FakeTTS(old, slow_build)
    stream = _make_stream(tts, sentences_then_hang=True)

    async def main() -> None:
        task = asyncio.create_task(stream._run(MagicMock()))
        await asyncio.to_thread(build_started.wait, 5)  # 确认已进入冷建窗口
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(main())
    finally:
        build_unblock.set()  # 释放悬置线程,避免拖住解释器退出

    old_releases = [(s, p) for s, p in tts.released if s is old]
    assert len(old_releases) == 1, f"旧连接应恰好归还一次,实际 {len(old_releases)} 次(双重归还!)"
    assert old_releases[0][1] is True, "归还旧连接时应保留其 pooled=True 语义"
    assert old.closed == 1, "旧连接应在归还前被 close 恰好一次(S2-2b 双保险)"
    assert old.cancelled == 0, "外层清理不得再对旧连接 streaming_cancel(应只见安全态)"


def test_stale_first_sentence_recovers_and_replays() -> None:
    """快乐路径:首句借到陈旧连接 → 冷建重放,回复完整,旧连接单次归还。"""
    old = FakeSynth("old", stale=True)
    fresh = FakeSynth("fresh")
    tts = FakeTTS(old, lambda cb: fresh)
    stream = _make_stream(tts, sentences_then_hang=False)

    asyncio.run(stream._run(MagicMock()))

    assert fresh.calls == ["第一句。"], "已发句子应在新连接上完整重放"
    assert fresh.completed == 1
    assert len([1 for s, _ in tts.released if s is old]) == 1, "旧连接单次归还"
    assert len([1 for s, _ in tts.released if s is fresh]) == 1, "新连接正常收尾归还"


def test_callback_survives() -> None:
    """恢复路径复用同一 callback(音频队列/收到标志跨连接延续)。"""
    cb = _CosyVoiceCallback()
    assert cb.received_audio is False
    cb.on_data(b"x")
    assert cb.received_audio is True


# 供 _run 的 except 分支使用:suppress 需要真实 contextlib(防误删 import)
_ = contextlib.suppress
