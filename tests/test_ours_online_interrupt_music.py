from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "voice_agents"))

providers_config = types.ModuleType("providers.config")
providers_config.funasr_hotwords = lambda: {}
sys.modules.setdefault("providers.config", providers_config)

from app import online_interrupt_host as host  # noqa: E402


class _FakeSession:
    agent_state = "idle"

    def __init__(self) -> None:
        self.interrupts = 0

    def interrupt(self, force: bool = False) -> None:
        self.interrupts += 1


class _FakeWiring:
    def __init__(self) -> None:
        self.online_state = {
            "accum": "",
            "fired_at": 0.0,
            "vad_speaking": True,
            "vad_off_ts": 0.0,
        }
        self.session = _FakeSession()
        self.timeline = None


class _FakeOutput:
    def __init__(self) -> None:
        self.clears = 0
        self._pushed_duration = 1.0

    def clear_buffer(self) -> None:
        self.clears += 1


class _FakeMusicPlayer:
    is_playing = True

    def __init__(self) -> None:
        self.stops = 0

    async def stop(self) -> str:
        self.stops += 1
        return "stopped"


def test_online_interrupt_clears_tts_without_stopping_music(monkeypatch: Any) -> None:
    output = _FakeOutput()
    player = _FakeMusicPlayer()
    monkeypatch.setattr(host.runtime, "ws_audio_output", output)
    monkeypatch.setattr(host.runtime, "music_player", player)

    host._clear_browser_playout()

    assert output.clears == 1
    assert player.stops == 0


def test_online_interrupt_ignores_chat_while_music_playing(monkeypatch: Any) -> None:
    output = _FakeOutput()
    player = _FakeMusicPlayer()
    wiring = _FakeWiring()
    monkeypatch.setattr(host.runtime, "ws_audio_output", output)
    monkeypatch.setattr(host.runtime, "music_player", player)

    accum = host._accumulate_online_text(wiring, "讲一个故事", False)

    assert accum is None
    assert wiring.online_state["accum"] == ""
    assert output.clears == 0
    assert wiring.session.interrupts == 0
