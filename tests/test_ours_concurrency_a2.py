"""行为锁定测试:并发改造 PR-A2(录音/审计产物子系统,agent 小改 #5)。

覆盖:RecordSettings 开关解析(默认=现状逐字节不变)、audit 白名单、EventTimeline
audit 档过滤 vs debug 档全量、TestRecorder single 档只出 duplex。纯逻辑/本地文件,无云依赖。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import numpy as np
import pytest

_AGENT_DIR = Path(__file__).resolve().parents[1] / "examples" / "voice_agents"
sys.path.insert(0, str(_AGENT_DIR))

from app.record_settings import RecordSettings, audit_allows  # noqa: E402


def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("XIAOGE_RECORD_MODE", "XIAOGE_TIMELINE_LEVEL", "AGENT_TIMELINE"):
        monkeypatch.delenv(k, raising=False)


# ── 开关解析:默认=现状(逐字节不变的前提)──────────────────────────────────
def test_defaults_are_legacy_off(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    s = RecordSettings.from_env()
    assert s.record_mode == "legacy" and s.timeline_level == "off" and s.is_legacy


def test_agent_timeline_maps_to_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("AGENT_TIMELINE", "1")
    s = RecordSettings.from_env()
    assert s.timeline_level == "debug" and s.record_mode == "legacy"


def test_explicit_timeline_level_overrides_agent_timeline(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("AGENT_TIMELINE", "1")
    monkeypatch.setenv("XIAOGE_TIMELINE_LEVEL", "off")
    assert RecordSettings.from_env().timeline_level == "off"


@pytest.mark.parametrize(
    "mode,expect_mode,mono",
    [
        ("full", "full", True),
        ("single", "single", False),
        ("off", "off", True),
        ("junk", "legacy", True),
    ],
)
def test_record_mode_parse_and_mono(
    monkeypatch: pytest.MonkeyPatch, mode: str, expect_mode: str, mono: bool
) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("XIAOGE_RECORD_MODE", mode)
    s = RecordSettings.from_env()
    assert s.record_mode == expect_mode
    assert s.writes_mono_tracks is mono


def test_target_dir_runs_for_debug_recordings_otherwise(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    root = Path("/repo")
    debug = RecordSettings(record_mode="full", timeline_level="debug").target_dir(root)
    audit = RecordSettings(record_mode="full", timeline_level="audit").target_dir(root)
    assert debug.parent.name == "runs" and audit.parent.name == "recordings"
    assert "_" in debug.name  # 带 session_id 后缀(#1)


# ── audit 白名单 ─────────────────────────────────────────────────────────────
def test_audit_whitelist() -> None:
    for keep in (
        "turn.user",
        "turn.assistant",
        "interrupt.kws",
        "interrupt.online",
        "error",
        "timeline.closed",
    ):
        assert audit_allows(keep), keep
    for drop in (
        "asr.interim",
        "asr.final",
        "agent_state.changed",
        "user_state.changed",
        "live_transcript.partial_full",
    ):
        assert not audit_allows(drop), drop


# ── EventTimeline:audit 档过滤 vs debug 档全量 ──────────────────────────────
def _emit_and_read(tmp_path: Path, level: str) -> list[str]:
    from event_timeline import EventTimeline

    tl = EventTimeline(tmp_path, level=level)
    tl.emit("turn.user", {"text": "你好"})
    tl.emit("asr.interim", {"text": "你"})
    tl.emit("interrupt.kws", {"keyword": "停"})
    tl.emit("live_transcript.partial_full", {"text": "x"})
    asyncio.run(tl.aclose())
    path = tmp_path / "timeline.jsonl"
    return [
        json.loads(ln)["type"] for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()
    ]


def test_timeline_audit_drops_high_freq(tmp_path: Path) -> None:
    types = _emit_and_read(tmp_path, "audit")
    assert "turn.user" in types and "interrupt.kws" in types
    assert "asr.interim" not in types and "live_transcript.partial_full" not in types


def test_timeline_debug_keeps_all(tmp_path: Path) -> None:
    types = _emit_and_read(tmp_path, "debug")
    assert {"turn.user", "asr.interim", "interrupt.kws", "live_transcript.partial_full"} <= set(
        types
    )


# ── TestRecorder:single 档只出 duplex;full 档三文件 ────────────────────────
def _render(tmp_path: Path, *, mono: bool) -> dict:
    from test_recorder import TestRecorder

    rec = TestRecorder(tmp_path, write_mono_tracks=mono)
    user = [(0, (np.ones(1600, dtype=np.int16) * 100), 16000)]
    asst = [(0, (np.ones(1600, dtype=np.int16) * 50), 16000)]
    rec._render_and_write(user, asst)
    return json.loads((tmp_path / "audio_manifest.json").read_text(encoding="utf-8"))


def test_single_writes_only_duplex(tmp_path: Path) -> None:
    manifest = _render(tmp_path, mono=False)
    assert (tmp_path / "duplex.wav").exists()
    assert not (tmp_path / "user.wav").exists()
    assert not (tmp_path / "assistant.wav").exists()
    # 单轨元数据保留、file 置空(数据在 duplex 左/右声道)
    assert manifest["tracks"][0]["file"] is None and manifest["tracks"][1]["file"] is None
    assert manifest["duplex"]["file"] == "duplex.wav"


def test_full_writes_three_files(tmp_path: Path) -> None:
    manifest = _render(tmp_path, mono=True)
    for f in ("user.wav", "assistant.wav", "duplex.wav"):
        assert (tmp_path / f).exists(), f
    assert manifest["tracks"][0]["file"] == "user.wav"


# ── P-6 分段:不分段 manifest 结构不变;分段产 .<seq> 文件 + 拼接时长等价 ─────
def _seg_lists(gap_us: int) -> tuple[list, list]:
    """两段用户音频:一段在 t=0,一段在 t=gap_us(制造跨桶)。"""
    user = [
        (0, np.ones(1600, dtype=np.int16) * 100, 16000),
        (gap_us, np.ones(1600, dtype=np.int16) * 100, 16000),
    ]
    asst = [(0, np.ones(1600, dtype=np.int16) * 50, 16000)]
    return user, asst


def test_no_segmentation_keeps_legacy_manifest(tmp_path: Path) -> None:
    from test_recorder import TestRecorder

    rec = TestRecorder(tmp_path, segment_seconds=None)  # 不分段=现状
    user, asst = _seg_lists(gap_us=100_000)
    rec._render_and_write(user, asst)
    m = json.loads((tmp_path / "audio_manifest.json").read_text(encoding="utf-8"))
    assert "tracks" in m and "duplex" in m and "segments" not in m  # 结构不变
    assert (tmp_path / "duplex.wav").exists()
    assert not (tmp_path / "duplex.0.wav").exists()


def test_segmentation_produces_seq_files(tmp_path: Path) -> None:
    from test_recorder import TestRecorder

    # 段长 1s;两段相隔 2s → 落在桶 0 与桶 2
    rec = TestRecorder(tmp_path, segment_seconds=1.0)
    user, asst = _seg_lists(gap_us=2_000_000)
    rec._render_and_write(user, asst)
    m = json.loads((tmp_path / "audio_manifest.json").read_text(encoding="utf-8"))
    assert "segments" in m and len(m["segments"]) == 2
    seqs = sorted(s["seq"] for s in m["segments"])
    assert seqs == [0, 2]
    for k in seqs:
        assert (tmp_path / f"duplex.{k}.wav").exists()
    assert not (tmp_path / "duplex.wav").exists()  # 分段模式不产无后缀文件


def test_segmentation_total_frames_match_unsegmented(tmp_path: Path) -> None:
    """分段各段总时长 ≈ 不分段整段(误差 ≤ 帧级);此处两段各含 1600 帧,合计应 = 3200。"""
    from test_recorder import TestRecorder

    rec = TestRecorder(tmp_path, segment_seconds=1.0)
    user, asst = _seg_lists(gap_us=2_000_000)
    rec._render_and_write(user, asst)
    m = json.loads((tmp_path / "audio_manifest.json").read_text(encoding="utf-8"))
    user_frames = sum(seg["tracks"][0]["frameCount"] for seg in m["segments"])
    assert user_frames == 3200  # 两段用户音各 1600 帧,分段不丢样本
