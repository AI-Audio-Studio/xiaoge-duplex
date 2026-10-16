"""静音泵门控单测: 无客户端不发静音帧; 客户端在/来即恢复旧行为; 排队数据照常排干。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "voice_agents"))

import app.web_audio as web_audio  # noqa: E402
from app.web_audio import WebSocketAudioInput  # noqa: E402
from webpanel import bridge  # noqa: E402
from webpanel.state import panel  # noqa: E402

_SILENCE = b"\x00" * (WebSocketAudioInput.SAMPLES_PER_FRAME * 2)


def _make_input(monkeypatch, clients: int, gate: bool = True) -> WebSocketAudioInput:
    inp = WebSocketAudioInput()
    inp._pump_gate = gate
    monkeypatch.setattr(web_audio, "audio_client_count", lambda: clients)
    return inp


def test_no_clients_no_silence(monkeypatch):
    inp = _make_input(monkeypatch, clients=0)

    async def run() -> None:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(inp.__anext__(), timeout=0.3)

    asyncio.run(run())


def test_clients_present_pumps_silence(monkeypatch):
    inp = _make_input(monkeypatch, clients=1)

    async def collect() -> list:
        return [await inp.__anext__() for _ in range(4)]

    frames = asyncio.run(asyncio.wait_for(collect(), timeout=1.0))
    assert len(frames) == 4
    for frame in frames:
        assert bytes(frame.data) == _SILENCE
        assert frame.sample_rate == WebSocketAudioInput.SAMPLE_RATE


def test_gate_off_legacy_silence(monkeypatch):
    inp = _make_input(monkeypatch, clients=0, gate=False)

    async def collect() -> list:
        return [await inp.__anext__() for _ in range(2)]

    frames = asyncio.run(asyncio.wait_for(collect(), timeout=0.5))
    assert all(bytes(frame.data) == _SILENCE for frame in frames)


def test_queued_data_drains_when_gated(monkeypatch):
    inp = _make_input(monkeypatch, clients=0)
    payload = b"\x11" * (WebSocketAudioInput.SAMPLES_PER_FRAME * 2)
    inp._sync_push(payload)

    async def run():
        return await asyncio.wait_for(inp.__anext__(), timeout=0.3)

    frame = asyncio.run(run())
    assert bytes(frame.data) == payload


def test_pump_stops_after_client_leaves(monkeypatch):
    state = {"clients": 1}
    inp = WebSocketAudioInput()
    inp._pump_gate = True
    monkeypatch.setattr(web_audio, "audio_client_count", lambda: state["clients"])

    async def run() -> None:
        first = await inp.__anext__()
        assert bytes(first.data) == _SILENCE
        state["clients"] = 0
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(inp.__anext__(), timeout=0.3)

    asyncio.run(run())


def test_pump_resumes_when_client_registers(monkeypatch):
    state = {"clients": 0}
    inp = WebSocketAudioInput()
    inp._pump_gate = True
    monkeypatch.setattr(web_audio, "audio_client_count", lambda: state["clients"])
    monkeypatch.setattr(WebSocketAudioInput, "GATED_POLL_INTERVAL_S", 0.05)

    async def run() -> None:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(inp.__anext__(), timeout=0.15)
        state["clients"] = 1
        frame = await asyncio.wait_for(inp.__anext__(), timeout=0.3)
        assert bytes(frame.data) == _SILENCE

    asyncio.run(run())


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, True),
        ("", True),
        ("  ", True),
        ("1", True),
        ("true", True),
        ("YES", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("off", False),
        ("no", False),
    ],
)
def test_env_pump_gate_parsing(raw, expected, monkeypatch):
    if raw is None:
        monkeypatch.delenv("XIAOGE_SILENCE_PUMP_GATE", raising=False)
    else:
        monkeypatch.setenv("XIAOGE_SILENCE_PUMP_GATE", raw)
    assert WebSocketAudioInput._env_pump_gate() is expected


def test_bridge_audio_client_count():
    saved = set(panel.audio_ws_clients)
    panel.audio_ws_clients.clear()
    try:
        assert bridge.audio_client_count() == 0
        fake = object()
        panel.audio_ws_clients.add(fake)
        assert bridge.audio_client_count() == 1
    finally:
        panel.audio_ws_clients.clear()
        panel.audio_ws_clients.update(saved)
