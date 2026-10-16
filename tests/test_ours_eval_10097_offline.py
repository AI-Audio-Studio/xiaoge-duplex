"""Offline checks; no model, gateway, or production module import."""

import json
import struct
import wave
from pathlib import Path

import pytest

from eval_10097.audio_client import FRAME_BYTES, effective_tail, read_pcm, rms, stream_pcm
from eval_10097.runner import ROOT, blocked_results, write_report


def test_pcm_tail_ignores_silence() -> None:
    voice = struct.pack("<160h", *([1000] * 160))
    pcm = bytes(FRAME_BYTES) + voice + bytes(FRAME_BYTES)
    assert rms(voice) == 1000
    assert effective_tail(pcm) == FRAME_BYTES * 2
    assert effective_tail(bytes(FRAME_BYTES)) is None
    with pytest.raises(ValueError):
        effective_tail(b"\x00")


@pytest.mark.asyncio
async def test_stream_pcm_preserves_payload_and_tail() -> None:
    sent: list[bytes] = []

    async def send(frame: bytes) -> None:
        sent.append(frame)

    pcm = struct.pack("<160h", *([1000] * 160))
    anchor = await stream_pcm(send, pcm, len(pcm), trailing_silence_s=0.01)
    assert anchor > 0
    assert b"".join(sent) == pcm + bytes(FRAME_BYTES)
    with pytest.raises(ValueError):
        await stream_pcm(send, pcm, 1)


def test_wav_format(tmp_path: Path) -> None:
    path = tmp_path / "input.wav"
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16000)
        target.writeframes(bytes(FRAME_BYTES))
    assert read_pcm(path) == bytes(FRAME_BYTES)


def test_case_inventory_and_blocked_report(tmp_path: Path) -> None:
    cases = json.loads((ROOT / "eval_10097/cases.json").read_text(encoding="utf-8"))
    assert len(cases["cases"]) == 36
    assert len({case["id"] for case in cases["cases"]}) == 36
    rows = blocked_results(cases)
    assert all(row["status"] == "blocked" for row in rows)
    output = tmp_path / "report"
    write_report(output, cases)
    data = json.loads((output / "results.json").read_text(encoding="utf-8"))
    assert data["model_requests"] == 0
    assert data["production_sessions"] == 0
    assert all(row["llm_ttft_ms"] is None for row in data["results"])
    with pytest.raises(FileExistsError):
        write_report(output, cases)
