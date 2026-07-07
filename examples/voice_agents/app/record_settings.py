"""录音/审计产物开关解析(PR-A2,agent 小改 #5)。

两个部署级开关,**默认解析为"现状"**(AGENT_TIMELINE 主导),保证 PC/测试形态逐字节不变:
  - `XIAOGE_RECORD_MODE` = full | single | off | (未设=legacy 现状)
  - `XIAOGE_TIMELINE_LEVEL` = off | audit | debug | (未设 → AGENT_TIMELINE 开=debug 否则 off)

`full`=三文件(user/assistant/duplex)、`single`=仅 duplex(立体声左右分轨,审计能力保留);
`audit` timeline=轮次级白名单(含对话文本),不落 debug.log/KPI。
**`XIAOGE_RECORD_CODEC` 由池管理器/转码器消费,agent 不读**(D-10:agent 永远只写 WAV,
"关=保持 WAV"即转码器不跑)——故本模块不解析 CODEC。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from common.runtime import session_id

_RECORD_MODES = {"full", "single", "off"}
_TIMELINE_LEVELS = {"off", "audit", "debug"}

# audit 档白名单:轮次级(turn.*)+ 打断(interrupt.*)+ 生命周期(timeline.*)+ 错误(error);
# 高频调试事件(asr.* / *_state.changed / live_transcript.* / turn_metrics.*)不落。
_AUDIT_PREFIXES = ("turn.", "interrupt.", "timeline.")
_AUDIT_EXACT = frozenset({"error"})


def agent_timeline_on() -> bool:
    """遗留开关:AGENT_TIMELINE=1 等价于 timeline_level=debug(D-11/K3,测试工作流不变)。"""
    return os.getenv("AGENT_TIMELINE", "0").strip().lower() in {"1", "true", "yes", "on"}


def audit_allows(event_type: str) -> bool:
    """audit 档是否保留该事件类型(白名单:前缀或精确匹配)。"""
    return event_type in _AUDIT_EXACT or event_type.startswith(_AUDIT_PREFIXES)


@dataclass(frozen=True)
class RecordSettings:
    record_mode: str  # full | single | off | legacy(未设=现状)
    timeline_level: str  # off | audit | debug

    @classmethod
    def from_env(cls) -> RecordSettings:
        raw_mode = os.getenv("XIAOGE_RECORD_MODE", "").strip().lower()
        record_mode = raw_mode if raw_mode in _RECORD_MODES else "legacy"
        raw_level = os.getenv("XIAOGE_TIMELINE_LEVEL", "").strip().lower()
        if raw_level in _TIMELINE_LEVELS:
            timeline_level = raw_level
        else:  # 未设 → 现状:AGENT_TIMELINE 决定
            timeline_level = "debug" if agent_timeline_on() else "off"
        return cls(record_mode=record_mode, timeline_level=timeline_level)

    @property
    def is_legacy(self) -> bool:
        """现状路径:未显式设 RECORD_MODE。此时录音沿用原 if(timeline)/else(混音)分支,
        行为逐字节不变。"""
        return self.record_mode == "legacy"

    @property
    def writes_mono_tracks(self) -> bool:
        """full=写 user/assistant/duplex 三文件;single=仅 duplex(mono 轨不写)。"""
        return self.record_mode != "single"

    def target_dir(self, repo_root: Path) -> Path:
        """录音/审计产物落盘目录。debug=runs/(测试资产,与现状同);其余=recordings/(生产)。
        目录名带 session_id 后缀(#1),防同秒多进程撞名。**每会话应只调一次并复用**。"""
        name = f"{time.strftime('%Y%m%d_%H%M%S')}_{session_id()}"
        base = "runs" if self.timeline_level == "debug" else "recordings"
        return repo_root / base / name
