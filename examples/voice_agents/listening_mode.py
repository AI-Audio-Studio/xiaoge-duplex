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

_DEFAULT_COMMAND = "小歌聆听模式"  # 主进入命令词(显示用)
_DEFAULT_COMMAND_ALIASES = ("小歌进入聆听模式", "聆听模式")  # 其他进入命令词(同样进入聆听)
_DEFAULT_WAKE = "小歌干活了"
_DEFAULT_NOTICE = "好,我先听着。需要我就说『小歌干活了』。"
# "要整理吗"的回答:肯定/整理类(先判否定,避免"不要"误判为肯定)
_AFFIRMATIVE = ("要", "好", "行", "可以", "嗯", "整理", "总结", "汇总", "归纳", "理一下", "整一下")
_NEGATIVE = ("不用", "不要", "不想", "别", "算了", "没必要", "不必")
# 退出尾巴切分:定位命令词的归一化相似度阈值(容忍 STT 把命令词听岔,如 小歌干活了→小郭/小哥干活了)
_CMD_LOCATE_RATIO = 0.6
_SPLIT_STRIP = " ,，。、!！?？:：;；…—-"


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
    command_keyword: str = _DEFAULT_COMMAND  # 主进入命令词
    command_aliases: tuple[str, ...] = _DEFAULT_COMMAND_ALIASES  # 其他进入命令词(任一命中即进入)
    wake_keyword: str = _DEFAULT_WAKE
    auto_enabled: bool = True
    auto_turns: int = 3
    auto_min_chars: int = 20
    temp_ttl_s: float = 120.0
    min_organize_chars: int = 15
    organize_enabled: bool = False  # 退出后"问是否整理 + 整理动作"总开关;先关,定时删除(TTL)不受影响
    drain_s: float = 2.5  # 退出排空窗(秒):窗内滞后到达的"聆听尾巴"仍按聆听吞掉(host 用)
    enter_notice: str = _DEFAULT_NOTICE
    # 运行状态(只在 agent 循环线程被改,见模块 docstring)
    active: bool = False
    awaiting_organize_answer: bool = False
    temp_transcript: list[str] = field(default_factory=list)  # 退出后待整理
    _buffer: list[str] = field(default_factory=list)  # 聆听期工作缓冲
    _auto_count: int = 0  # 连续自说自话计数

    @classmethod
    def from_environment(cls) -> ListeningController:
        cmd_env = _env_str("XIAOGE_LISTEN_COMMAND", "").strip()  # 多个进入命令词用 | 分隔
        if cmd_env:
            cmds = [c.strip() for c in cmd_env.split("|") if c.strip()] or [_DEFAULT_COMMAND]
            command_keyword, command_aliases = cmds[0], tuple(cmds[1:])
        else:
            command_keyword, command_aliases = _DEFAULT_COMMAND, _DEFAULT_COMMAND_ALIASES
        return cls(
            enabled=_env_bool("XIAOGE_LISTEN_ENABLE", False),
            command_keyword=command_keyword,
            command_aliases=command_aliases,
            wake_keyword=_env_str("XIAOGE_LISTEN_WAKE", _DEFAULT_WAKE).strip() or _DEFAULT_WAKE,
            auto_enabled=_env_bool("XIAOGE_LISTEN_AUTO_ENABLE", True),
            auto_turns=_env_int("XIAOGE_LISTEN_AUTO_TURNS", 3),
            auto_min_chars=_env_int("XIAOGE_LISTEN_AUTO_MINCHARS", 20),
            temp_ttl_s=_env_float("XIAOGE_LISTEN_TEMP_TTL", 120.0),
            min_organize_chars=_env_int("XIAOGE_LISTEN_MIN_ORGANIZE_CHARS", 15),
            organize_enabled=_env_bool("XIAOGE_LISTEN_ORGANIZE", False),
            drain_s=_env_float("XIAOGE_LISTEN_DRAIN", 2.5),
            enter_notice=_env_str("XIAOGE_LISTEN_ENTER_NOTICE", _DEFAULT_NOTICE),
        )

    # ── 供 KWS 词表合并 ──────────────────────────────────────────────────────
    @property
    def enter_keywords(self) -> tuple[str, ...]:
        """所有进入命令词(主词 + 别名)。"""
        return tuple(k for k in ((self.command_keyword, *self.command_aliases)) if k)

    @property
    def keywords(self) -> tuple[str, ...]:
        if not self.enabled:
            return ()
        return tuple(k for k in (*self.enter_keywords, self.wake_keyword) if k)

    # ── 控制:KWS 命中 ───────────────────────────────────────────────────────
    def observe_keyword(self, keyword: str) -> ListeningEvent:
        if not self.enabled:
            return ListeningEvent.NONE
        hit = _norm(keyword)
        if not hit:
            return ListeningEvent.NONE
        if not self.active:
            if hit in {_norm(k) for k in self.enter_keywords}:  # 任一进入命令词命中
                self._enter()
                return ListeningEvent.ENTERED
            return ListeningEvent.NONE
        if hit == _norm(self.wake_keyword):
            self._exit()
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
                self._enter()  # 自动进入
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

    # ── 退出尾巴切分:丢命令词及之前(聆听内容),留之后(退出后接着说的真话)──────
    def split_after_command(self, text: str, keyword: str) -> str | None:
        """在 text 中定位命令词,返回其**之后**的内容:

        - 命令词后有内容("小歌干活了今天天气"→"今天天气")→ 返回该内容;
        - 命令词在结尾/整条就是命令词 → 返回 "";
        - 定位不到(被 STT 听得太离谱)→ 返回 None(host 据此兜底整条吞)。
        命令词及其之前的内容(聆听尾巴)一律丢弃。先精确匹配,失败用归一化滑窗模糊定位。
        """
        raw = (text or "").strip()
        kw = (keyword or "").strip()
        if not raw or not kw:
            return None
        idx = raw.find(kw)  # 1) 精确(连说/准确转写)
        if idx >= 0:
            return raw[idx + len(kw) :].strip(_SPLIT_STRIP)
        nkw = _norm(kw)  # 2) 归一化滑窗模糊定位(容忍听岔/夹标点)
        if not nkw:
            return None
        best_ratio, best_end = 0.0, -1
        n = len(raw)
        for start in range(n):
            for width in range(len(nkw) - 1, len(nkw) + 4):
                end = start + width
                if end > n:
                    break
                ratio = difflib.SequenceMatcher(None, nkw, _norm(raw[start:end])).ratio()
                if ratio > best_ratio:
                    best_ratio, best_end = ratio, end
        if best_ratio >= _CMD_LOCATE_RATIO and best_end >= 0:
            return raw[best_end:].strip(_SPLIT_STRIP)
        return None

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
