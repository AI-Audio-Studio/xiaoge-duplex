"""在线 2pass 旁路早打断的 host 侧:累加/闸门/判定/装配(评审#9 自 setup_taps 拆出)。

传输层在 online_interrupt.py(推音频/收增量/重连);本模块只做策略:
聆听闸门、压话累加、停止词强打断、实义字数 + VAD 佐证的软打断,以及 tap 装配。
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING

from common.runtime import append_turn_log as _log
from common.text_rules import ACK_STRIP_RE, OVERLAP_ACK_CHARS, should_ignore_user_turn
from online_interrupt import (
    OnlineAsrTap,
    OnlineInterruptConfig,
    OnlineTapAudioInput,
    unavailable_reason as _online_unavailable_reason,
)
from providers.config import funasr_hotwords
from webpanel.bridge import broadcast, broadcast_audio_ctrl

from app.listening_host import listen_interrupt_blocked
from app.session_state import runtime
from livekit.agents import JobContext

if TYPE_CHECKING:
    from app.setup_taps import SessionWiring

logger = logging.getLogger("web-ui-agent")

# 在线软打断的 VAD 佐证宽限(秒):VAD 刚停说话后这段时间内仍接受打断(容忍识别滞后)。
ONLINE_VAD_GRACE = float(os.getenv("XIAOGE_ONLINE_VAD_GRACE", "0.6"))


def _accumulate_online_text(w: SessionWiring, piece: str, segment_end: bool) -> str | None:
    """在线增量累加与前置闸门。返回累加文本;返回 None = 本条不判(清态/防抖)。"""
    state = w.online_state
    if listen_interrupt_blocked():  # 聆听期/退出保护窗:用户语音不得打断小歌
        state["accum"] = ""
        return None
    if segment_end:
        state["accum"] = ""
        return None
    out = runtime.ws_audio_output
    browser_playing = out is not None and out._pushed_duration > 0
    if w.session.agent_state != "speaking" and not browser_playing:
        state["accum"] = ""
        return None
    accum = str(state["accum"]) + piece
    state["accum"] = accum
    if time.monotonic() - float(state["fired_at"]) < 1.0:  # 打断后 1s 防抖
        return None
    return accum


def _judge_online_interrupt(w: SessionWiring, min_chars: int, accum: str) -> None:
    """压话判定:停止词 → 强打断;实义字数够 + VAD 佐证 → 软打断。"""
    state = w.online_state
    session = w.session
    now = time.monotonic()
    if should_ignore_user_turn(accum):
        state["fired_at"] = now
        state["accum"] = ""
        session.interrupt(force=True)
        broadcast({"type": "clear"})
        broadcast_audio_ctrl({"type": "clear"})
        _log(f"STOP_ONLINE_EARLY text={accum!r} -> force_interrupt")
        if w.timeline is not None:
            w.timeline.emit("interrupt.online", {"text": accum, "kind": "stop"}, source="online")
        return
    core = ACK_STRIP_RE.sub("", accum)
    meaningful = sum(1 for ch in core if ch not in OVERLAP_ACK_CHARS)
    if meaningful >= min_chars:
        # VAD 佐证:仅当 VAD 也确认用户此刻(或刚刚)在说话,才认为是真打断。
        # 在线2pass 文本比音频滞后 ~0.5s,而 VAD 开口几乎即时——若文本到了 VAD 仍
        # 从未开口,几乎必是幽灵词/识别噪声 → 不打断、清掉累积,免误打断。
        vad_ok = bool(state["vad_speaking"]) or (
            now - float(state["vad_off_ts"]) < ONLINE_VAD_GRACE
        )
        if not vad_ok:
            state["accum"] = ""
            _log(f"ONLINE_INTERRUPT_SKIP_NO_VAD text={accum!r} chars={meaningful}")
            return
        state["fired_at"] = now
        state["accum"] = ""
        session.interrupt()
        broadcast({"type": "clear"})
        broadcast_audio_ctrl({"type": "clear"})
        _log(f"OVERLAP_ONLINE_INTERRUPT text={accum!r} chars={meaningful} -> interrupt")


def setup_online_interrupt(ctx: JobContext, w: SessionWiring) -> None:
    """在线 2pass 旁路早打断:压话字数/停止词判定 + live 气泡扇出。"""
    session = w.session
    online_cfg = OnlineInterruptConfig.from_env()

    def _online_text_fanout(piece: str, segment_end: bool) -> None:
        # 打断逻辑优先、原样执行;显示为 best-effort(feed_online 内部已全兜底,不会拖慢打断)。
        accum = _accumulate_online_text(w, piece, segment_end)
        if accum is not None:
            _judge_online_interrupt(w, online_cfg.min_chars, accum)
        # 显示:流式主STT 模式下气泡由主STT 驱动(同源),此处不再喂在线2pass 以免双驱动。
        if w.live is not None and not w.live_from_main:
            w.live.feed_online(piece, segment_end)

    online_reason = _online_unavailable_reason(online_cfg)
    if online_reason is None and session.input.audio is not None:
        online_tap = OnlineAsrTap(
            online_cfg, hotwords=funasr_hotwords(), on_text=_online_text_fanout
        )
        online_tap.start()
        session.input.audio = OnlineTapAudioInput(session.input.audio, online_tap)
        ctx.add_shutdown_callback(online_tap.aclose)
        _log(f"ONLINE_INTERRUPT_ACTIVE min_chars={online_cfg.min_chars}")
    else:
        _log(f"ONLINE_INTERRUPT_DISABLED reason={online_reason!r}")
