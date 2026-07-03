"""判停 KPI 仪表盘(**仅测试模式挂载**,正式版不存在)。

旁路只读地订阅 AgentSession 事件,算出判停调优关心的 5 类 KPI,写到
runs/<ts>/turn_kpis.json,并(若有 timeline)实时 emit 调试事件。用于把"判停好不好"
从听感变成可量化、可对照的数字,服务后续扫参。

硬约束(同测试工具):opt-in(只在 timeline 激活=`-Test` 时创建)、非阻塞(纯内存 +
收尾时 to_thread 写盘)、解耦(只依赖 session/timeline,不碰 web 内部、不改判停逻辑)、
稳定(全路径 try/except,异常不外抛)。

可拓展:每个 KPI = 一个 KpiDetector;加指标只需往 _detectors 里加一个,核心不动。
当前实现 5 条:① 过度切分率 ② 双回复/残片 ③ felt 延迟 ④ EOT/转写延迟 ⑤ 误打断。
注入阶段(P1)可把"过度切分"从启发式换成场景真值——detector 内部切数据源即可。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("turn-metrics")


def _f(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or not v.strip():
        return default
    try:
        return float(v)
    except ValueError:
        return default


def _lcs_len(a: str, b: str) -> int:
    """最长公共子序列长度(滚动数组,O(len(a)*len(b)))。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    for ca in a:
        cur = [0] * (len(b) + 1)
        for j, cb in enumerate(b, 1):
            cur[j] = prev[j - 1] + 1 if ca == cb else max(prev[j], cur[j - 1])
        prev = cur
    return prev[len(b)]


def _pctl(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return round(s[k], 1)


@dataclass
class TurnRecord:
    role: str
    text: str
    started_at: float | None  # started_speaking_at(秒),None=无独立语音窗(切分征兆)
    stopped_at: float | None
    metrics: dict[str, Any] = field(default_factory=dict)


# ── KPI 检测器基类(可拓展接口)────────────────────────────────────────────────
class KpiDetector:
    key = "base"

    def feed_user_turn(self, rec: TurnRecord, prev_user: TurnRecord | None) -> None: ...
    def feed_assistant_turn(self, rec: TurnRecord) -> None: ...
    def feed_felt(self, ms: float) -> None: ...
    def feed_interrupt(self, kind: str) -> None: ...
    def summary(self) -> dict[str, Any]:
        return {}


class OverSegmentationDetector(KpiDetector):
    """① 过度切分:一段连续话被切成多轮。

    手测启发式(无真值):某 user 轮没有独立语音窗(started/stopped 缺失,即日志里的
    `speech=-->-`)→ 几乎可断定是从上一段切出的续话;或与上一 user 轮间隔 < gap。
    注入阶段改为场景真值(实际轮数 - 声明轮数)。
    """

    key = "over_segmentation"

    def __init__(self, gap_s: float) -> None:
        self._gap = gap_s
        self.user_turns = 0
        self.suspected = 0
        self.by_missing_window = 0
        self.by_small_gap = 0

    def feed_user_turn(self, rec: TurnRecord, prev_user: TurnRecord | None) -> None:
        self.user_turns += 1
        missing = rec.started_at is None or rec.stopped_at is None
        small_gap = False
        if (
            prev_user is not None
            and rec.started_at is not None
            and prev_user.stopped_at is not None
        ):
            small_gap = (rec.started_at - prev_user.stopped_at) < self._gap
        if missing:
            self.by_missing_window += 1
        if small_gap:
            self.by_small_gap += 1
        if missing or small_gap:
            self.suspected += 1

    def summary(self) -> dict[str, Any]:
        rate = round(self.suspected / self.user_turns, 3) if self.user_turns else 0.0
        return {
            "user_turns": self.user_turns,
            "suspected_splits": self.suspected,
            "by_missing_speech_window": self.by_missing_window,
            "by_small_gap": self.by_small_gap,
            "suspected_split_rate": rate,
        }


class DoubleReplyDetector(KpiDetector):
    """② 双回复/残片:一段意图引出多次回复,典型是被续话打断后留下的极短残片(如"啊,")。"""

    key = "double_reply"

    def __init__(self, fragment_chars: int = 3) -> None:
        self._frag = fragment_chars
        self.assistant_turns = 0
        self.fragments = 0

    def feed_assistant_turn(self, rec: TurnRecord) -> None:
        self.assistant_turns += 1
        if len((rec.text or "").strip()) <= self._frag:
            self.fragments += 1

    def summary(self) -> dict[str, Any]:
        return {"assistant_turns": self.assistant_turns, "fragment_replies": self.fragments}


class FeltLatencyDetector(KpiDetector):
    """③ felt 延迟:user_stop -> agent_speak。给中位/p90 + 超预算计数。"""

    key = "felt_latency"

    def __init__(self, budget_ms: float) -> None:
        self._budget = budget_ms
        self.samples: list[float] = []

    def feed_felt(self, ms: float) -> None:
        if ms >= 0:
            self.samples.append(ms)

    def summary(self) -> dict[str, Any]:
        over = sum(1 for x in self.samples if x > self._budget)
        return {
            "count": len(self.samples),
            "median_ms": _pctl(self.samples, 50),
            "p90_ms": _pctl(self.samples, 90),
            "budget_ms": self._budget,
            "over_budget": over,
        }


class EotDelayDetector(KpiDetector):
    """④ EOT / 转写延迟:从 user 轮 metrics 聚合(中位/p90,毫秒)。"""

    key = "eot_delay"

    def __init__(self) -> None:
        self.eot: list[float] = []
        self.tr: list[float] = []

    def feed_user_turn(self, rec: TurnRecord, prev_user: TurnRecord | None) -> None:
        eot = rec.metrics.get("end_of_turn_delay")
        tr = rec.metrics.get("transcription_delay")
        if isinstance(eot, (int, float)):
            self.eot.append(eot * 1000)
        if isinstance(tr, (int, float)):
            self.tr.append(tr * 1000)

    def summary(self) -> dict[str, Any]:
        return {
            "end_of_turn_delay_median_ms": _pctl(self.eot, 50),
            "end_of_turn_delay_p90_ms": _pctl(self.eot, 90),
            "transcription_delay_median_ms": _pctl(self.tr, 50),
            "transcription_delay_p90_ms": _pctl(self.tr, 90),
        }


class InterruptDetector(KpiDetector):
    """⑤ 误打断:统计 agent_false_interruption(框架误打断)等。

    kws/online 打断的原始计数已在 timeline.jsonl(emit 的 interrupt.kws/online),需要时
    再加一个 detector 从那里统计——这就是"可拓展接口"的用法。
    """

    key = "interrupt"

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    def feed_interrupt(self, kind: str) -> None:
        self.counts[kind] = self.counts.get(kind, 0) + 1

    def summary(self) -> dict[str, Any]:
        return {"counts": dict(self.counts)}


class CoverageDetector(KpiDetector):
    """⑥ 识别覆盖率(注入模式真值):LCS(应识别文本, 实际合并文本)/len(应识别)。

    ≈ 1 - 丢字率。仅当场景声明了 expect(由 TurnMetrics.set_expected 注入)才计算;
    手测模式无 expect → {enabled: false},不影响其他 KPI。
    """

    key = "coverage"

    def __init__(self) -> None:
        self._expect: str | None = None
        self._got = ""

    def set_expected(self, text: str | None) -> None:
        self._expect = (text or "").strip() or None

    def feed_user_turn(self, rec: TurnRecord, prev_user: TurnRecord | None) -> None:
        self._got += rec.text or ""

    def summary(self) -> dict[str, Any]:
        if not self._expect:
            return {"enabled": False}
        exp = "".join(self._expect.split())  # 去空白(中文无空格,英文也对齐)
        got = "".join(self._got.split())
        cov = (_lcs_len(exp, got) / len(exp)) if exp else 0.0
        return {
            "enabled": True,
            "expected_chars": len(exp),
            "recognized_chars": len(got),
            "coverage": round(cov, 3),
            "dropped_estimate": round(1 - cov, 3),
        }


class TurnMetrics:
    """挂到 AgentSession,旁路统计判停 KPI,收尾写 turn_kpis.json。"""

    def __init__(self, run_dir: str | Path, *, timeline: Any = None) -> None:
        self._dir = Path(run_dir)
        self._timeline = timeline
        gap = _f("TURN_OVERSEG_GAP", 1.5)
        budget = _f("TURN_FELT_BUDGET_MS", 1500.0)
        self._detectors: list[KpiDetector] = [
            OverSegmentationDetector(gap),
            DoubleReplyDetector(),
            FeltLatencyDetector(budget),
            EotDelayDetector(),
            InterruptDetector(),
            CoverageDetector(),
        ]
        self._prev_user: TurnRecord | None = None
        self._last_user_stop_at: float | None = None

    # ── 安装:自注册 session 监听(只读旁路)──────────────────────────────────
    def attach(self, session: Any) -> None:
        try:

            @session.on("conversation_item_added")
            def _on_item(ev: Any) -> None:
                self._on_item(ev)

            @session.on("user_state_changed")
            def _on_user_state(ev: Any) -> None:
                self._on_user_state_ev(ev)

            @session.on("agent_state_changed")
            def _on_agent_state(ev: Any) -> None:
                self._on_agent_state_ev(ev)

            @session.on("agent_false_interruption")
            def _on_false(ev: Any) -> None:
                self._feed_interrupt("false_interruption")
        except Exception as exc:
            logger.debug("turn metrics attach skipped: %s", exc)

    def _on_user_state_ev(self, ev: Any) -> None:
        try:
            if getattr(ev, "old_state", None) == "speaking" and (
                getattr(ev, "new_state", None) != "speaking"
            ):
                self._last_user_stop_at = getattr(ev, "created_at", None)
        except Exception:
            pass

    def _on_agent_state_ev(self, ev: Any) -> None:
        try:
            if getattr(ev, "new_state", None) == "speaking":
                start = getattr(ev, "created_at", None)
                if start is not None and self._last_user_stop_at is not None:
                    self._feed_felt((start - self._last_user_stop_at) * 1000.0)
                    self._last_user_stop_at = None
        except Exception:
            pass

    def _on_item(self, ev: Any) -> None:
        try:
            item = getattr(ev, "item", None)
            role = getattr(item, "role", None)
            if role not in ("user", "assistant"):
                return
            m = {}
            try:
                m = dict(getattr(item, "metrics", {}) or {})
            except Exception:
                m = {}
            rec = TurnRecord(
                role=role,
                text=getattr(item, "text_content", "") or "",
                started_at=m.get("started_speaking_at"),
                stopped_at=m.get("stopped_speaking_at"),
                metrics=m,
            )
            if role == "user":
                for d in self._detectors:
                    try:
                        d.feed_user_turn(rec, self._prev_user)
                    except Exception:
                        pass
                self._prev_user = rec
                self._debug(
                    "user_turn",
                    {"has_window": rec.started_at is not None, "len": len(rec.text)},
                )
            else:
                for d in self._detectors:
                    try:
                        d.feed_assistant_turn(rec)
                    except Exception:
                        pass
            # 每轮增量写一次:stop_agent.cmd 是强杀进程(shutdown 回调可能不跑),
            # 增量写保证强杀也留得下最新 KPI。写盘丢到线程,事件循环不阻塞。
            self._schedule_write()
        except Exception:
            pass

    def _schedule_write(self) -> None:
        import asyncio

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(asyncio.to_thread(self._write_summary))
        except Exception:
            try:
                self._write_summary()  # 不在事件循环时退化为同步(文件极小,可接受)
            except Exception:
                pass

    def _feed_felt(self, ms: float) -> None:
        for d in self._detectors:
            try:
                d.feed_felt(ms)
            except Exception:
                pass

    def set_expected(self, text: str | None) -> None:
        """注入模式:把场景声明的应识别文本交给 CoverageDetector(算覆盖率/丢字)。"""
        for d in self._detectors:
            if isinstance(d, CoverageDetector):
                d.set_expected(text)

    def _feed_interrupt(self, kind: str) -> None:
        for d in self._detectors:
            try:
                d.feed_interrupt(kind)
            except Exception:
                pass

    def _debug(self, kind: str, payload: dict[str, Any]) -> None:
        tl = self._timeline
        if tl is None:
            return
        try:
            tl.emit(f"turn_metrics.{kind}", payload, source="kpi")
        except Exception:
            pass

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for d in self._detectors:
            try:
                out[d.key] = d.summary()
            except Exception:
                out[d.key] = {"error": True}
        return out

    async def aclose(self) -> None:
        import asyncio

        try:
            await asyncio.to_thread(self._write_summary)
        except Exception:
            pass

    def _write_summary(self) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            path = self._dir / "turn_kpis.json"
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self.summary(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, path)
            logger.info("turn KPIs -> %s", path)
        except Exception:
            logger.warning("turn metrics write failed", exc_info=True)
