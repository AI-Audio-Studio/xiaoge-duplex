"""行为锁定测试:并发改造 PR-B 转码器(D-13/D-21/D-22)。

覆盖:opus/flac 转码往返 + D-21 分档校验、成功删源/失败保底留 WAV、wav 不转码、
遗留扫描入队、队列指标。本地 PyAV(livekit-agents 既有依赖),无云依赖。
"""

from __future__ import annotations

import sys
import wave
from pathlib import Path

import numpy as np
import pytest

_AGENT_DIR = Path(__file__).resolve().parents[1] / "examples" / "voice_agents"
sys.path.insert(0, str(_AGENT_DIR))

from poolmgr import transcoder as tc  # noqa: E402


def _make_wav(path: Path, *, seconds: float = 1.0, rate: int = 24000, channels: int = 1) -> int:
    n = int(seconds * rate)
    t = np.arange(n) / rate
    mono = (np.sin(2 * np.pi * 440 * t) * 8000).astype(np.int16)
    data = np.repeat(mono[:, None], channels, axis=1).reshape(-1) if channels > 1 else mono
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data.tobytes())
    return n


def test_opus_roundtrip_deletes_source(tmp_path: Path) -> None:
    wav = tmp_path / "user.wav"
    _make_wav(wav)
    wav_size = wav.stat().st_size
    r = tc.transcode_file(wav, "opus")
    assert r.ok, r.reason
    assert r.dst is not None and r.dst.suffix == ".opus" and r.dst.exists()
    assert not wav.exists()  # 校验通过才删源
    assert r.dst.stat().st_size < wav_size  # opus 比原 WAV 小(省磁盘)


def test_flac_lossless_sample_exact(tmp_path: Path) -> None:
    wav = tmp_path / "user.wav"
    n = _make_wav(wav)
    r = tc.transcode_file(wav, "flac")
    assert r.ok, r.reason
    assert r.dst is not None and r.dst.suffix == ".flac" and r.dst.exists()
    assert not wav.exists()
    # 无损:解码采样数与原 WAV 逐一相等
    _, dst_samples = tc._decode_info(r.dst)
    assert dst_samples == n


def test_stereo_duplex_transcodes(tmp_path: Path) -> None:
    wav = tmp_path / "duplex.wav"
    _make_wav(wav, channels=2)
    r = tc.transcode_file(wav, "opus")
    assert r.ok and r.dst.exists() and not wav.exists()


def test_wav_codec_no_transcode_keeps_source(tmp_path: Path) -> None:
    wav = tmp_path / "user.wav"
    _make_wav(wav)
    r = tc.transcode_file(wav, "wav")
    assert r.ok and r.dst is None and wav.exists()  # 不转码,源保留


def test_validation_failure_keeps_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D-22:校验失败必须保留 WAV、删掉半成品产物(绝不丢审计数据)。"""
    wav = tmp_path / "user.wav"
    _make_wav(wav)
    monkeypatch.setattr(tc, "_validate", lambda *a, **k: (False, "forced-fail"))
    r = tc.transcode_file(wav, "opus")
    assert not r.ok and r.dst is None
    assert wav.exists()  # 源保底留住
    assert not (tmp_path / "user.opus").exists()  # 半成品产物删掉


def test_scan_leftovers_enqueues(tmp_path: Path) -> None:
    root = tmp_path / "recordings"
    for sid in ("20260707_100000_p1", "20260707_100001_p2"):
        d = root / sid
        d.mkdir(parents=True)
        _make_wav(d / "user.wav", seconds=0.2)
        _make_wav(d / "duplex.wav", seconds=0.2, channels=2)
    t = tc.Transcoder(root, codec="opus")
    assert t.scan_leftovers() == 4  # 两会话 × 两 wav
    assert t.metrics()["queue_depth"] == 4


def test_iter_session_wavs_excludes_products(tmp_path: Path) -> None:
    d = tmp_path / "sess"
    d.mkdir()
    _make_wav(d / "user.wav", seconds=0.1)
    (d / "user.opus").write_bytes(b"x")  # 已转码产物不应被列
    (d / "audio_manifest.json").write_text("{}")
    wavs = tc.iter_session_wavs(d)
    assert [p.name for p in wavs] == ["user.wav"]
