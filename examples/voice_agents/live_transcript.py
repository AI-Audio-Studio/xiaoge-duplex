"""用户语音的实时转写显示(Web 面板 live 气泡的数据源)。

把"在线 2pass 增量转写"广播给网页,驱动一个边说边长的用户气泡,最终由主 STT 的
final 定稿(看似"修正")。与判停/打断逻辑解耦:本模块只读旁路、纯内存、非阻塞、
异常全兜底、默认可关。

设计要点(对应需求:解耦 / 模块化 / 不阻塞 / 不影响其他功能 / 稳定可靠):
  - 解耦:broadcast 依赖注入,不引用 web 服务器内部;自注册 session 事件监听。
  - 模块化:单文件单类,接口仅 attach() / feed_online()。
  - 不阻塞:仅字符串累加 + 非阻塞 broadcast,无磁盘/网络等待。
  - 不影响:打断主链路一字不改;本模块挂在在线 tap 的"扇出"之后,best-effort。
  - 稳定:全路径 try/except,异常绝不外抛;优雅降级(无在线转写 = 占位 + final)。
  - 可调:阈值集中在 LiveTranscriptConfig(env 可覆盖);气泡 开/续/关 可发调试事件。

气泡 = 一个"显示轮次":开口开启,连续说话(含换气小停顿 / VAD 抖动)只长不裂,
出 final 或超过 new_turn_gap_s 的长停顿才收尾换新(见 _maybe_open)。

未来的 TurnPolicy 层(Q3/Q4:助手残片过滤、只显示说出口、上下文截断)会挂在
transcription_node / conversation_item_added / 打断回调附近——本模块**不碰**这些路径,
给其留位,互不影响。
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("live-transcript")


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class LiveTranscriptConfig:
    """实时转写显示的可调旋钮(集中一处,全部 env 可覆盖,便于调试/优化)。"""

    enabled: bool = True
    # 距上次活动超过此秒数的"再次开口"视为新的一句 -> 新气泡;之内则并入当前气泡。
    # 正常出 final 已会收尾换新;此阈值主要给"没拿到 final 的掉轮次"兜底。
    new_turn_gap_s: float = 1.5

    @classmethod
    def from_env(cls) -> LiveTranscriptConfig:
        return cls(
            enabled=_env_bool("LIVE_TRANSCRIPT", True),
            new_turn_gap_s=float(os.getenv("LIVE_TRANSCRIPT_NEW_TURN_GAP", "1.5")),
        )


class LiveTranscript:
    """实时转写显示器:广播 user_speaking / user_partial 驱动前端 live 气泡。"""

    def __init__(
        self,
        broadcast: Callable[[dict[str, Any]], None],
        config: LiveTranscriptConfig | None = None,
        *,
        timeline: Any = None,
    ) -> None:
        self._broadcast_fn = broadcast
        self._cfg = config or LiveTranscriptConfig()
        self._timeline = timeline  # 可选:测试模式下记调试事件,None 则不记
        self._open = False
        self._prefix = ""  # 已收尾段落的累计文本
        self._seg = ""  # 当前段落的在线增量累计
        self._last_ts = 0.0

    # ── 安装:自注册 session 监听(与现有处理器并存,各自 try/except)────────
    def attach(self, session: Any) -> None:
        try:

            @session.on("user_state_changed")
            def _on_user_state(ev: Any) -> None:
                try:
                    if getattr(ev, "new_state", None) == "speaking":
                        self._maybe_open()
                except Exception:
                    pass

            @session.on("conversation_item_added")
            def _on_item(ev: Any) -> None:
                try:
                    item = getattr(ev, "item", None)
                    if getattr(item, "role", None) == "user":
                        # 真正的 final 到了 -> 本显示轮次收尾(前端用已有 message 定稿)
                        self._close("final")
                except Exception:
                    pass
        except Exception as exc:  # 安装失败绝不影响主流程
            logger.debug("live transcript attach skipped: %s", exc)

    # ── 在线 2pass 增量喂入(由 web 的扇出调用;内部全兜底,best-effort)──────
    def feed_online(self, piece: str, segment_end: bool) -> None:
        try:
            now = time.monotonic()
            self._maybe_open(now)
            if segment_end:
                # 段落收尾:把本段在线增量并入前缀(沿用打断逻辑"online=增量"的假设)
                self._prefix += self._seg
                self._seg = ""
            else:
                self._seg += piece or ""
            self._last_ts = now
            text = (self._prefix + self._seg).strip()
            if text:
                self._emit({"type": "user_partial", "text": text})
                self._debug("partial", {"len": len(text)})
        except Exception:
            pass

    # ── 主 STT 原生 interim(全量文本)喂入:气泡与内容/上下文同源 ────────────
    def feed_full(self, text: str) -> None:
        """流式主 STT 的 interim 给的是当前轮**全量**文本(非增量),直接置换显示。

        轮边界由主STT 的真 final 决定(conversation_item_added→_close),故这里**只在未开时
        开一次**、不按 gap 中途重开——避免 FunASR 说话中途 >gap 不吐字时被误判"新轮"(气泡中途
        消失/分裂)。gap 重开仅旧在线2pass 路径(feed_online)需要,保留不动。
        """
        try:
            now = time.monotonic()
            if not self._open:
                self._open = True
                self._emit({"type": "user_speaking", "state": "start"})
                self._debug("open", {})
            self._prefix = ""
            self._seg = text or ""
            self._last_ts = now
            t = self._seg.strip()
            if t:
                self._emit({"type": "user_partial", "text": t})
                self._debug("partial_full", {"len": len(t)})
        except Exception:
            pass

    # ── 单一判定点:开新轮 vs 续用当前气泡(以后联动判停只改这里)──────────
    def _maybe_open(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if (not self._open) or (now - self._last_ts) > self._cfg.new_turn_gap_s:
            self._open = True
            self._prefix = ""
            self._seg = ""
            self._last_ts = now
            self._emit({"type": "user_speaking", "state": "start"})
            self._debug("open", {})
        else:
            self._last_ts = now  # 连续说话:沿用同一气泡

    def _close(self, reason: str) -> None:
        if not self._open:
            return
        self._open = False
        self._debug("close", {"reason": reason})

    def _emit(self, msg: dict[str, Any]) -> None:
        try:
            self._broadcast_fn(msg)
        except Exception:
            pass

    def _debug(self, kind: str, payload: dict[str, Any]) -> None:
        tl = self._timeline
        if tl is None:
            return
        try:
            tl.emit(f"live_transcript.{kind}", payload, source="ui")
        except Exception:
            pass
