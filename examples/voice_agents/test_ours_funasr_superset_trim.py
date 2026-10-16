"""超集裁剪单测: 2pass-offline 整窗重识别不再把已提交 FINAL 的内容重发进同一轮。

真实事故样本(20260914, 同轮两条 FINAL 被上游空格拼接 → 同句重复):
  F1="你之前给我介绍了一本书呀" → offline 重识别 F2="？你之前给我介绍了一本书呀，没完啊"
  F1="可看现现在天气晴了呀"     → F2="可我看着现在天气晴了呀，出大太阳了"(同音噪声)
裁剪后只剩新尾巴, 上游拼接 = 无重复。
"""

from __future__ import annotations

import asyncio
import json
import time

import aiohttp
import pytest

from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS
from providers.stt import funasr_stream as fs
from providers.stt.funasr_stream import FunASRStreamSTT, _FunASRStreamStream

_NOW = 1000.0
_F1A = "你之前给我介绍了一本书呀"
_F2A = "？你之前给我介绍了一本书呀，没完啊"
_F1B = "可我看现现在天气晴了呀"
_F2B = "可我看着现在天气晴了呀，出大太阳了"


# ── 纯函数: 前缀对齐 ──────────────────────────────────────────────────────────


def test_align_exact_prefix():
    assert fs._align_to_prefix("你好呀", "你好呀今天好") == (0, 3)


def test_align_substitution_noise():
    # 同音噪声: 着↔现 一次替换仍对上
    dist, used = fs._align_to_prefix(_F1B, "可我看着现在天气晴了呀")
    assert dist == 1
    assert used == 11


def test_align_tie_prefers_shortest_prefix():
    # b 中含 a 的重复: 距离并列取最短前缀, 重复留给尾巴自己重放
    dist, used = fs._align_to_prefix("你好呀", "你好呀你好呀")
    assert dist == 0
    assert used == 3


def test_align_body_too_short():
    dist, _ = fs._align_to_prefix(_F1A, "你之前")
    assert dist >= len(_F1A) - len("你之前")


# ── 纯函数: 裁剪 ──────────────────────────────────────────────────────────────


def test_trim_incident_book():
    assert fs._trim_recovered_prefix(_F2A, [(_NOW - 0.4, _F1A)], _NOW) == "没完啊"


def test_trim_incident_weather():
    assert fs._trim_recovered_prefix(_F2B, [(_NOW - 0.4, _F1B)], _NOW) == "出大太阳了"


def test_trim_pure_recover_returns_empty():
    assert fs._trim_recovered_prefix("？" + _F1A, [(_NOW - 0.4, _F1A)], _NOW) == ""


def test_trim_split_pieces():
    # 事故的另一种拆包: (a)整段重发 → 裁空; (b)只发新尾巴 → 原样(去前导标点)
    recent = [(_NOW - 0.6, _F1A)]
    assert fs._trim_recovered_prefix("？你之前给我介绍了一本书呀", recent, _NOW) == ""
    assert fs._trim_recovered_prefix("，没完啊", recent, _NOW) == "没完啊"


def test_trim_expired_ttl_keeps_text():
    old = _NOW - fs._RECENT_FINAL_TTL_S - 0.01
    assert (
        fs._trim_recovered_prefix(_F2A, [(old, _F1A)], _NOW)
        == _F2A.lstrip(fs._LEAD_PUNCT)
    )


def test_trim_short_final_skipped():
    # 短 final 撞前缀概率高, 不参与裁剪
    assert fs._trim_recovered_prefix("好的呀，没问题", [(_NOW - 0.4, "好的")], _NOW) == (
        "好的呀，没问题"
    )


def test_trim_unrelated_text_kept():
    assert fs._trim_recovered_prefix("明天会下雨", [(_NOW - 0.4, _F1A)], _NOW) == "明天会下雨"


def test_trim_no_recent_finals():
    assert fs._trim_recovered_prefix(_F2A, [], _NOW) == _F2A.lstrip(fs._LEAD_PUNCT)


def test_trim_punct_only_text():
    assert fs._trim_recovered_prefix("。", [(_NOW - 0.4, _F1A)], _NOW) == ""


def test_trim_latest_match_wins():
    recent = [(_NOW - 2.0, "很久以前说过的一句话呀"), (_NOW - 0.4, _F1A)]
    assert fs._trim_recovered_prefix(_F2A, recent, _NOW) == "没完啊"


def test_trim_repeated_sentence_keeps_second_copy():
    # 用户真把句子说两遍且 funasr 未内切: 重识别含两遍, 裁一次留第二遍
    text = "？" + _F1A + "，" + _F1A + "，没完啊"
    assert fs._trim_recovered_prefix(text, [(_NOW - 0.4, _F1A)], _NOW) == _F1A + "，没完啊"


def test_trim_residual_verbatim_restart():
    # 已知残余(极罕见): 改口重说整句且 funasr 已内切会被裁掉重复头
    recent = [(_NOW - 0.4, "今天天气怎么样")]
    assert fs._trim_recovered_prefix("今天天气怎么样啊", recent, _NOW) == "啊"


# ── 接线: _funasr_recv / _emit_final / env 开关 ────────────────────────────────


class _NoNetStream(_FunASRStreamStream):
    async def _run(self) -> None:  # 单测不连 funasr 服务器
        await asyncio.Event().wait()


class _Msg:
    def __init__(self, payload: dict) -> None:
        self.type = aiohttp.WSMsgType.TEXT
        self.data = json.dumps(payload, ensure_ascii=False)


class _FakeWS:
    def __init__(self, payloads: list[dict]) -> None:
        self._left = list(payloads)

    def __aiter__(self):
        return self

    async def __anext__(self) -> _Msg:
        if not self._left:
            raise StopAsyncIteration
        return _Msg(self._left.pop(0))


def _make_stream() -> _NoNetStream:
    engine = FunASRStreamSTT(vad=object())
    return _NoNetStream(stt=engine, conn_options=DEFAULT_API_CONNECT_OPTIONS)


def test_recv_offline_recover_trimmed():
    async def run() -> str:
        s = _make_stream()
        s._voiced = True
        s._emit_final(_F1A)
        await s._funasr_recv(_FakeWS([{"mode": "2pass-offline", "text": _F2A}]))
        return s._prefix

    assert asyncio.run(run()) == "没完啊"


def test_recv_ghost_untouched_by_trim():
    async def run() -> tuple[str, int]:
        s = _make_stream()
        s._voiced = False
        s._last_voiced = None
        s._emit_final(_F1A)
        await s._funasr_recv(_FakeWS([{"mode": "2pass-offline", "text": _F2A}]))
        return s._prefix, s._ghost_chars

    prefix, ghosts = asyncio.run(run())
    assert prefix == ""  # 静音期文本照旧走幽灵丢弃
    assert ghosts == len(_F2A)


def test_recv_online_increments_untouched():
    async def run() -> tuple[str, str]:
        s = _make_stream()
        s._voiced = True
        s._emit_final(_F1A)
        await s._funasr_recv(
            _FakeWS(
                [
                    {"mode": "2pass-offline", "text": _F2A},
                    {"mode": "2pass-online", "text": "还有呢"},
                ]
            )
        )
        return s._prefix, s._seg

    prefix, seg = asyncio.run(run())
    assert prefix == "没完啊"
    assert seg == "还有呢"


def test_emit_final_records_and_prunes():
    async def run() -> list[str]:
        s = _make_stream()
        s._recent_finals.append((time.monotonic() - 10.0, "很老的一句话呢"))
        s._emit_final(_F1A)
        return [t for _, t in s._recent_finals]

    assert asyncio.run(run()) == [_F1A]


def test_env_switch_disables_trim(monkeypatch):
    monkeypatch.setenv("XIAOGE_STT_SUPERSET_TRIM", "0")

    async def run() -> str:
        engine = FunASRStreamSTT(vad=object())
        s = _NoNetStream(stt=engine, conn_options=DEFAULT_API_CONNECT_OPTIONS)
        s._voiced = True
        s._emit_final(_F1A)
        await s._funasr_recv(_FakeWS([{"mode": "2pass-offline", "text": _F2A}]))
        return s._prefix

    assert asyncio.run(run()) == _F2A  # 关掉后回到旧行为(原样累加)


@pytest.mark.parametrize(
    "raw,expected",
    [(None, True), ("1", True), ("true", True), ("0", False), ("false", False)],
)
def test_env_superset_trim_default_on(raw, expected, monkeypatch):
    if raw is None:
        monkeypatch.delenv("XIAOGE_STT_SUPERSET_TRIM", raising=False)
    else:
        monkeypatch.setenv("XIAOGE_STT_SUPERSET_TRIM", raw)
    engine = FunASRStreamSTT(vad=object())
    assert engine._superset_trim is expected
