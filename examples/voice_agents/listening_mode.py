"""聆听模式控制器 —— 纯状态机,零依赖,可单测。设计见 LISTENING_MODE_DESIGN.md。

小歌"只听不插":聆听期 ASR 照常,但文本进临时缓冲、不进上下文、不回复。
进入:命令词 / 自动检测;退出:命令词 / 通话键(host 调 force_exit)。

本模块**只持状态/缓冲/决策,纯同步、无 asyncio / 无 I/O、不 import 工程模块**。
host 喂事件(KWS 命中 / 每轮文本 / 通话键)、按返回值动作;asyncio 定时器、session.say、
broadcast、turn_ctx 注入全在 host。**线程安全靠 host 把所有变更串行到 agent 循环**
(纯状态机 ≠ 线程安全)。
"""

from __future__ import annotations

import difflib
import os
from dataclasses import dataclass, field
from enum import Enum

_DEFAULT_COMMAND = "小歌聆听模式"
_DEFAULT_WAKE = "小歌干活了"
_DEFAULT_NOTICE = "好,我先听着。需要我就说『小歌干活了』。"
# "要整理吗"的回答:肯定/整理类(先判否定,避免"不要"误判为肯定)
_AFFIRMATIVE = ("要", "好", "行", "可以", "嗯", "整理", "总结", "汇总", "归纳", "理一下", "整一下")
_NEGATIVE = ("不用", "不要", "不想", "别", "算了", "没必要", "不必")
# 命令词回声整体近似阈值:KWS 按读音命中,但 STT 可能把命令词听岔(小歌干活了→小郭/小哥干活了)。
# 归一化后整体相似度≥此值即视为同一条命令词的回声(KWS 才是命中真源)。
_ECHO_FUZZY_RATIO = 0.6


class ListeningEvent(Enum):
    NONE = "none"
    ENTERED = "entered"
    EXITED = "exited"


class AutoDecision(Enum):
    NONE = "none"
    ENTER = "enter"


def _norm(s: str) -> str:
    """归一化:仅留字母数字/CJK,去标点空白,casefold。用于命令词/答案的鲁棒匹配。"""
    return "".join(ch for ch in str(s or "") if ch.isalnum()).casefold()


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or not v.strip():
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int((os.getenv(name) or "").strip() or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float((os.getenv(name) or "").strip() or default)
    except ValueError:
        return default


def _env_str(name: str, default: str) -> str:
    v = os.getenv(name)
    return default if v is None else v  # 允许空串(=不出声),故不 strip 掉空


@dataclass
class ListeningController:
    # 配置
    enabled: bool = False
    command_keyword: str = _DEFAULT_COMMAND
    wake_keyword: str = _DEFAULT_WAKE
    auto_enabled: bool = True
    auto_turns: int = 3
    auto_min_chars: int = 20
    temp_ttl_s: float = 120.0
    min_organize_chars: int = 15
    enter_notice: str = _DEFAULT_NOTICE
    # 运行状态(只在 agent 循环线程被改,见模块 docstring)
    active: bool = False
    awaiting_organize_answer: bool = False
    pending_command_echo: str = ""  # 一次性:刚因语音命令进/出,待剥的命令词
    temp_transcript: list[str] = field(default_factory=list)  # 退出后待整理
    _buffer: list[str] = field(default_factory=list)  # 聆听期工作缓冲
    _auto_count: int = 0  # 连续自说自话计数

    @classmethod
    def from_environment(cls) -> ListeningController:
        return cls(
            enabled=_env_bool("XIAOGE_LISTEN_ENABLE", False),
            command_keyword=_env_str("XIAOGE_LISTEN_COMMAND", _DEFAULT_COMMAND).strip()
            or _DEFAULT_COMMAND,
            wake_keyword=_env_str("XIAOGE_LISTEN_WAKE", _DEFAULT_WAKE).strip() or _DEFAULT_WAKE,
            auto_enabled=_env_bool("XIAOGE_LISTEN_AUTO_ENABLE", True),
            auto_turns=_env_int("XIAOGE_LISTEN_AUTO_TURNS", 3),
            auto_min_chars=_env_int("XIAOGE_LISTEN_AUTO_MINCHARS", 20),
            temp_ttl_s=_env_float("XIAOGE_LISTEN_TEMP_TTL", 120.0),
            min_organize_chars=_env_int("XIAOGE_LISTEN_MIN_ORGANIZE_CHARS", 15),
            enter_notice=_env_str("XIAOGE_LISTEN_ENTER_NOTICE", _DEFAULT_NOTICE),
        )

    # ── 供 KWS 词表合并 ──────────────────────────────────────────────────────
    @property
    def keywords(self) -> tuple[str, ...]:
        if not self.enabled:
            return ()
        return tuple(k for k in (self.command_keyword, self.wake_keyword) if k)

    # ── 控制:KWS 命中 ───────────────────────────────────────────────────────
    def observe_keyword(self, keyword: str) -> ListeningEvent:
        if not self.enabled:
            return ListeningEvent.NONE
        hit = _norm(keyword)
        if not hit:
            return ListeningEvent.NONE
        if not self.active:
            if hit == _norm(self.command_keyword):
                self._enter()
                self.pending_command_echo = self.command_keyword
                return ListeningEvent.ENTERED
            return ListeningEvent.NONE
        if hit == _norm(self.wake_keyword):
            self._exit()
            self.pending_command_echo = self.wake_keyword
            return ListeningEvent.EXITED
        return ListeningEvent.NONE

    # ── 自动进入:每轮信号(仅未聆听 + 自动开启时计数)────────────────────────
    def observe_turn(self, text: str, interrupted_agent: bool) -> AutoDecision:
        if not self.enabled or not self.auto_enabled or self.active:
            return AutoDecision.NONE
        n = len((text or "").strip())
        if n < self.auto_min_chars:
            return AutoDecision.NONE  # 短噪声(ack/停顿/backchannel):既不计也不重置连击
        if interrupted_agent:
            self._auto_count += 1  # 长输入且打断了小歌=自说自话信号
            if self._auto_count >= self.auto_turns:
                self._auto_count = 0
                self._enter()  # 自动进入(无 pending_command_echo)
                return AutoDecision.ENTER
        else:
            self._auto_count = 0  # 长输入但没打断小歌=在正常对话→重置
        return AutoDecision.NONE

    @property
    def auto_count(self) -> int:
        return self._auto_count

    # ── 内容:聆听期吞入缓冲 ─────────────────────────────────────────────────
    def capture(self, text: str) -> None:
        t = (text or "").strip()
        if t:
            self._buffer.append(t)

    # ── 命令词回声:内容感知剥词(置位则消费一次)──────────────────────────────
    def strip_command_echo(self, text: str) -> str | None:
        """置位则 disarm + 剥掉命令词,返回剩余(可能空串);未置位返回 None。

        - 返回 None:本轮不是命令词回声轮,host 照常往下。
        - 返回 "":纯回声(整条就是命令词)→ host 吞掉、不动状态。
        - 返回非空:连说("小歌干活了 顺便整理一下")→ host 用剩余继续(capture/当回答)。
        """
        kw = self.pending_command_echo
        if not kw:
            return None
        self.pending_command_echo = ""
        raw = (text or "").strip()
        nkw, nraw = _norm(kw), _norm(raw)
        if not nkw:
            return raw
        # 1) 精确包含(连说 / 准确转写):去掉命令词,返回剩余
        if nkw in nraw:
            idx = raw.find(kw)
            if idx >= 0:
                return (raw[:idx] + raw[idx + len(kw) :]).strip(" ,，。、!！?？")
            return "" if nraw == nkw else raw
        # 2) 整体近似(KWS 按读音命中、STT 把命令词听岔,如 小歌干活了→小郭干活了):
        #    视为纯回声吞掉——KWS 才是命中真源,不被 STT 错字带偏。
        if difflib.SequenceMatcher(None, nkw, nraw).ratio() >= _ECHO_FUZZY_RATIO:
            return ""
        # 3) 回声轮没来、下一轮直接是真答 → 原样返回(不卡死)
        return raw

    # ── 整理回答判定 / 临时内容 ──────────────────────────────────────────────
    def is_affirmative(self, text: str) -> bool:
        n = _norm(text)
        if not n:
            return False
        if any(_norm(w) in n for w in _NEGATIVE):  # 先否定,"不要"不算肯定
            return False
        return any(_norm(w) in n for w in _AFFIRMATIVE)

    def temp_has_substance(self) -> bool:
        return sum(len(s) for s in self.temp_transcript) >= self.min_organize_chars

    def take_temp(self) -> list[str]:
        t = list(self.temp_transcript)
        self.temp_transcript = []
        return t

    def drop_temp(self) -> None:
        self.temp_transcript = []

    def clear_awaiting(self) -> None:
        self.awaiting_organize_answer = False

    # ── 通话键兜底退出(只在 agent 循环调,见模块 docstring)────────────────────
    def force_exit(self) -> bool:
        if self.active:
            self._exit()
            return True
        return False

    # ── 内部状态转移 ─────────────────────────────────────────────────────────
    def _enter(self) -> None:
        self.active = True
        self._buffer = []
        self.temp_transcript = []  # 再次进入:丢弃上次待整理(定时器由 host 取消)
        self.awaiting_organize_answer = False
        self._auto_count = 0

    def _exit(self) -> None:
        self.active = False
        self.temp_transcript = list(self._buffer)
        self._buffer = []
        # awaiting / 主动问 / 定时器:host 按 temp_has_substance 决定
