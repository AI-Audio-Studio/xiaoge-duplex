from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "voice_agents"))

backends = types.ModuleType("app.backends")
backends.build_llm = lambda: None
backends.build_tts = lambda: None
backends.build_stt = lambda: None
backends.STT_BACKENDS = {}
backends.TTS_BACKENDS = {}
backends.backend_tabs_html = lambda *args, **kwargs: ""
backends.make_stt_backend = lambda *args, **kwargs: None
backends.make_tts_backend = lambda *args, **kwargs: None
sys.modules.setdefault("app.backends", backends)
providers_config = types.ModuleType("providers.config")
providers_config.funasr_hotwords = lambda: {}
sys.modules.setdefault("providers.config", providers_config)
silero = types.ModuleType("livekit.plugins.silero")
silero.VAD = types.SimpleNamespace(load=lambda *args, **kwargs: None)
sys.modules.setdefault("livekit.plugins.silero", silero)

import web_ui_agent  # noqa: E402
from livekit.agents import StopResponse  # noqa: E402


class _FakeSession:
    def __init__(self) -> None:
        self.says: list[str] = []
        self.say_options: list[dict[str, bool]] = []
        self.interrupts = 0
        self.replies: list[dict[str, str]] = []

    def say(
        self,
        text: str,
        *,
        add_to_chat_ctx: bool = True,
        allow_interruptions: bool = True,
    ) -> None:
        self.says.append(text)
        self.say_options.append(
            {"add_to_chat_ctx": add_to_chat_ctx, "allow_interruptions": allow_interruptions}
        )

    def interrupt(self, force: bool = False) -> None:
        self.interrupts += 1

    def generate_reply(self, *, user_input: str, input_modality: str) -> None:
        self.replies.append({"user_input": user_input, "input_modality": input_modality})


class _FakeMusicPlayer:
    def __init__(self, *, playing: bool = False, last_name: str = "song_one") -> None:
        self.is_playing = playing
        self.last_name = last_name
        self.resumes = 0
        self.stops = 0

    async def resume_last_for_tool(self) -> str | None:
        self.resumes += 1
        self.is_playing = True
        return self.last_name or None

    async def stop_for_tool(self) -> bool:
        self.stops += 1
        was = self.is_playing
        self.is_playing = False
        return was


def _new_agent() -> tuple[types.SimpleNamespace, _FakeSession]:
    session = _FakeSession()
    agent = types.SimpleNamespace(session=session)
    agent._music_control_intent = web_ui_agent.VoiceAgent._music_control_intent.__get__(agent)
    agent._apply_turn_filters = web_ui_agent.VoiceAgent._apply_turn_filters.__get__(agent)
    agent._maybe_handle_g3_protocol_turn = (
        web_ui_agent.VoiceAgent._maybe_handle_g3_protocol_turn.__get__(agent)
    )
    agent.handle_manual_text = web_ui_agent.VoiceAgent.handle_manual_text.__get__(agent)
    return agent, session


def test_bare_continue_resumes_last_music(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        player = _FakeMusicPlayer(playing=False, last_name="声动未来-聚力同行")
        agent, session = _new_agent()
        monkeypatch.setattr(web_ui_agent.runtime, "music_player", player)

        with pytest.raises(StopResponse):
            await agent._apply_turn_filters("继续", False)
        await asyncio.sleep(0)

        assert player.resumes == 1
        assert session.says == ["好的，继续播放《声动未来-聚力同行》。"]
        assert session.interrupts == 0

    asyncio.run(run())


def test_explicit_stop_music_says_ack(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        player = _FakeMusicPlayer(playing=True, last_name="声动未来-聚力同行")
        agent, session = _new_agent()
        monkeypatch.setattr(web_ui_agent.runtime, "music_player", player)

        with pytest.raises(StopResponse):
            await agent._apply_turn_filters("停止播放", False)
        await asyncio.sleep(0)

        assert player.stops == 1
        assert session.says == ["好的，音乐停了。"]
        assert session.say_options == [
            {"add_to_chat_ctx": False, "allow_interruptions": False}
        ]
        assert session.interrupts == 0

    asyncio.run(run())


def test_manual_chat_while_music_playing_skips_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        player = _FakeMusicPlayer(playing=True, last_name="声动未来-聚力同行")
        agent, session = _new_agent()
        monkeypatch.setattr(web_ui_agent.runtime, "music_player", player)
        monkeypatch.setattr(web_ui_agent, "broadcast", lambda *args, **kwargs: None)

        await agent.handle_manual_text("讲个故事")

        assert player.stops == 0
        assert session.says == []
        assert session.replies == []

    asyncio.run(run())


def test_manual_stop_music_filters_before_g3(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        player = _FakeMusicPlayer(playing=True, last_name="声动未来-聚力同行")
        agent, session = _new_agent()
        monkeypatch.setattr(web_ui_agent.runtime, "music_player", player)
        monkeypatch.setattr(web_ui_agent, "broadcast", lambda *args, **kwargs: None)

        await agent.handle_manual_text("停止播放")
        await asyncio.sleep(0)

        assert player.stops == 1
        assert session.says == ["好的，音乐停了。"]
        assert session.say_options == [
            {"add_to_chat_ctx": False, "allow_interruptions": False}
        ]
        assert session.replies == []

    asyncio.run(run())


def test_manual_bare_continue_resumes_last_music(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        player = _FakeMusicPlayer(playing=False, last_name="声动未来-聚力同行")
        agent, session = _new_agent()
        monkeypatch.setattr(web_ui_agent.runtime, "music_player", player)
        monkeypatch.setattr(web_ui_agent, "broadcast", lambda *args, **kwargs: None)

        await agent.handle_manual_text("继续")
        await asyncio.sleep(0)

        assert player.resumes == 1
        assert session.says == ["好的，继续播放《声动未来-聚力同行》。"]
        assert session.replies == []

    asyncio.run(run())
