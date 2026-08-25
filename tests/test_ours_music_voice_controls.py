from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "voice_agents"))

backends = types.ModuleType("app.backends")
backends.build_llm = lambda: None
backends.build_semantic_llm = lambda: None
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

import app.setup_taps as setup_taps  # noqa: E402
import web_ui_agent  # noqa: E402
from app.knowledge_index import KnowledgeHit  # noqa: E402
from common.semantic_router import (  # noqa: E402
    SemanticDecision,
    SemanticRoute,
    SemanticRouterConfig,
)

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


class _FakeSemanticRouter:
    def __init__(self, route: SemanticRoute, *, mode: str = "enforce") -> None:
        self.result = route
        self.config = SemanticRouterConfig(mode=mode)  # type: ignore[arg-type]
        self.calls: list[str] = []

    @property
    def enabled(self) -> bool:
        return True

    async def route(self, text: str) -> SemanticRoute:
        self.calls.append(text)
        return self.result


class _FakeKnowledgeIndex:
    def __init__(self, hits: list | None = None) -> None:
        self.hits = hits or []
        self.queries: list[str] = []

    def is_ready(self) -> bool:
        return bool(self.hits)

    async def query(self, query: str) -> list:
        self.queries.append(query)
        return self.hits


class _FakeMusicPlayer:
    def __init__(self, *, playing: bool = False, last_name: str = "song_one") -> None:
        self.is_playing = playing
        self.last_name = last_name
        self.plays: list[str | None] = []
        self.resumes = 0
        self.stops = 0

    async def play_for_tool(self, music_id: str | None) -> str | None:
        self.plays.append(music_id)
        self.is_playing = True
        return self.last_name

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
    agent = types.SimpleNamespace(
        session=session,
        _semantic_router=None,
        _wiring=types.SimpleNamespace(live=None),
    )
    agent._music_control_intent = web_ui_agent.VoiceAgent._music_control_intent.__get__(agent)
    agent._apply_turn_filters = web_ui_agent.VoiceAgent._apply_turn_filters.__get__(agent)
    agent._maybe_handle_g3_protocol_turn = (
        web_ui_agent.VoiceAgent._maybe_handle_g3_protocol_turn.__get__(agent)
    )
    agent._g3_frames = web_ui_agent.VoiceAgent._g3_frames.__get__(agent)
    agent._g3_validation_and_frames = web_ui_agent.VoiceAgent._g3_validation_and_frames.__get__(agent)
    agent._build_g3_state = web_ui_agent.VoiceAgent._build_g3_state.__get__(agent)
    agent._remember_g3_pending_high_risk = (
        web_ui_agent.VoiceAgent._remember_g3_pending_high_risk.__get__(agent)
    )
    agent._say_g3_knowledge_reply = web_ui_agent.VoiceAgent._say_g3_knowledge_reply.__get__(agent)
    agent._semantic_frames = web_ui_agent.VoiceAgent._semantic_frames.__get__(agent)
    agent._finalize_g3_user_message = web_ui_agent.VoiceAgent._finalize_g3_user_message.__get__(agent)
    agent._reset_live_transcript = lambda *args, **kwargs: None
    agent._finish_consumed_voice_turn = (
        web_ui_agent.VoiceAgent._finish_consumed_voice_turn.__get__(agent)
    )
    agent.handle_manual_text = web_ui_agent.VoiceAgent.handle_manual_text.__get__(agent)
    return agent, session


@pytest.mark.parametrize("command", ["唱首歌吧", "唱一首歌吧", "放首歌吧"])
def test_generic_play_with_particle_uses_random_music(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    async def run() -> None:
        player = _FakeMusicPlayer(last_name="声动未来-聚力同行")
        agent, session = _new_agent()
        monkeypatch.setattr(web_ui_agent.runtime, "music_player", player)

        with pytest.raises(StopResponse):
            await agent._apply_turn_filters(command, False)
        await asyncio.sleep(0)

        assert player.plays == [None]
        assert session.says == ["好的，播放《声动未来-聚力同行》。"]

    asyncio.run(run())


def test_voice_music_turn_finalizes_and_resets_once(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        player = _FakeMusicPlayer(last_name="声动未来-聚力同行")
        agent, _ = _new_agent()
        events: list[dict] = []
        resets: list[str] = []
        agent._reset_live_transcript = resets.append
        monkeypatch.setattr(web_ui_agent.runtime, "music_player", player)
        monkeypatch.setattr(web_ui_agent, "broadcast", lambda message: events.append(message) or True)

        with pytest.raises(StopResponse):
            await agent._apply_turn_filters(
                "，播放音乐", False, finalize_user_message=True
            )
        await asyncio.sleep(0)

        assert len(events) == 1
        assert events[0]["type"] == "message"
        assert events[0]["role"] == "user"
        assert events[0]["text"] == "播放音乐"
        assert resets == ["music_play"]

    asyncio.run(run())


@pytest.mark.parametrize(
    ("command", "playing", "reason"),
    [("继续播放", False, "music_resume"), ("暂停播放", True, "music_stop")],
)
def test_voice_resume_and_stop_close_live_turn(
    monkeypatch: pytest.MonkeyPatch, command: str, playing: bool, reason: str
) -> None:
    async def run() -> None:
        player = _FakeMusicPlayer(playing=playing, last_name="声动未来-聚力同行")
        agent, _ = _new_agent()
        resets: list[str] = []
        agent._reset_live_transcript = resets.append
        monkeypatch.setattr(web_ui_agent.runtime, "music_player", player)
        monkeypatch.setattr(web_ui_agent, "broadcast", lambda message: True)

        with pytest.raises(StopResponse):
            await agent._apply_turn_filters(command, False, finalize_user_message=True)
        await asyncio.sleep(0)

        assert resets == [reason]

    asyncio.run(run())


def test_manual_music_turn_broadcasts_user_message_only_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        player = _FakeMusicPlayer(last_name="声动未来-聚力同行")
        agent, _ = _new_agent()
        events: list[dict] = []
        resets: list[str] = []
        agent._reset_live_transcript = resets.append
        monkeypatch.setattr(web_ui_agent.runtime, "music_player", player)
        monkeypatch.setattr(web_ui_agent, "broadcast", lambda message: events.append(message) or True)

        await agent.handle_manual_text("播放音乐")
        await asyncio.sleep(0)

        user_messages = [event for event in events if event.get("role") == "user"]
        assert len(user_messages) == 1
        assert user_messages[0]["text"] == "播放音乐"
        assert resets == []

    asyncio.run(run())


def test_explicit_music_title_strips_sentence_particle(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        player = _FakeMusicPlayer(last_name="声动未来-聚力同行")
        agent, session = _new_agent()
        monkeypatch.setattr(web_ui_agent.runtime, "music_player", player)

        with pytest.raises(StopResponse):
            await agent._apply_turn_filters("播放声动未来吧", False)
        await asyncio.sleep(0)

        assert player.plays == ["声动未来"]
        assert session.says == ["好的，播放《声动未来-聚力同行》。"]

    asyncio.run(run())


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


@pytest.mark.parametrize("command", ["停止播放", "别唱了", "不要唱了", "停"])
def test_explicit_stop_music_says_ack(monkeypatch: pytest.MonkeyPatch, command: str) -> None:
    async def run() -> None:
        player = _FakeMusicPlayer(playing=True, last_name="声动未来-聚力同行")
        agent, session = _new_agent()
        monkeypatch.setattr(web_ui_agent.runtime, "music_player", player)

        with pytest.raises(StopResponse):
            await agent._apply_turn_filters(command, False)
        await asyncio.sleep(0)

        assert player.stops == 1
        assert session.says == ["好的，音乐停了。"]
        assert session.say_options == [{"add_to_chat_ctx": False, "allow_interruptions": False}]
        assert session.interrupts == 0

    asyncio.run(run())


def test_bare_stop_does_not_match_longer_word(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        player = _FakeMusicPlayer(playing=True, last_name="声动未来-聚力同行")
        agent, session = _new_agent()
        monkeypatch.setattr(web_ui_agent.runtime, "music_player", player)

        with pytest.raises(StopResponse):
            await agent._apply_turn_filters("停车", False)

        assert player.stops == 0
        assert session.says == []

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
        assert session.say_options == [{"add_to_chat_ctx": False, "allow_interruptions": False}]
        assert session.replies == []

    asyncio.run(run())


def test_final_bare_stop_defers_to_music_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    player = _FakeMusicPlayer(playing=True)
    session = _FakeSession()
    wiring = types.SimpleNamespace(
        session=session,
        live_from_main=False,
        live=None,
    )
    event = types.SimpleNamespace(is_final=True, transcript="停")
    monkeypatch.setattr(setup_taps.runtime, "music_player", player)
    monkeypatch.setattr(setup_taps.runtime, "listen_ctrl", None)
    monkeypatch.setattr(setup_taps, "_log", lambda *args, **kwargs: None)

    setup_taps._handle_stt_event(wiring, event)

    assert session.interrupts == 0


def test_final_bare_stop_interrupts_without_music(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession()
    wiring = types.SimpleNamespace(
        session=session,
        live_from_main=False,
        live=None,
    )
    event = types.SimpleNamespace(is_final=True, transcript="停")
    monkeypatch.setattr(setup_taps.runtime, "music_player", None)
    monkeypatch.setattr(setup_taps.runtime, "listen_ctrl", None)
    monkeypatch.setattr(setup_taps, "listen_interrupt_blocked", lambda: False)
    monkeypatch.setattr(setup_taps, "_log", lambda *args, **kwargs: None)

    setup_taps._handle_stt_event(wiring, event)

    assert session.interrupts == 1


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


def test_g3_command_dispatch_says_executing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        agent, session = _new_agent()
        broadcasts: list[dict] = []

        def fake_broadcast(message: dict) -> bool:
            broadcasts.append(message)
            return True

        monkeypatch.setattr(web_ui_agent, "broadcast", fake_broadcast)
        with pytest.raises(StopResponse):
            await agent._maybe_handle_g3_protocol_turn("往前走一米")

        assert broadcasts[0]["type"] == "g3_protocol"
        assert broadcasts[0]["dry_run"] is False
        assert broadcasts[0]["frames"][0]["type"] == "data.cmd"
        assert broadcasts[0]["frames"][0]["result_timeout_ms"] == 3000
        assert session.says == ["好的，正在执行"]
        assert session.say_options == [{"add_to_chat_ctx": False, "allow_interruptions": False}]

    asyncio.run(run())


def test_g3_command_without_endpoint_says_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        agent, session = _new_agent()
        monkeypatch.setattr(web_ui_agent, "broadcast", lambda message: False)

        with pytest.raises(StopResponse):
            await agent._maybe_handle_g3_protocol_turn("往前走一米")

        assert session.says == ["执行失败，请稍后再试！"]
        assert session.say_options == [{"add_to_chat_ctx": False, "allow_interruptions": False}]

    asyncio.run(run())


def test_g3_product_knowledge_uses_real_helper_direct_speech(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        hit = KnowledgeHit(
            text="小歌支持全双工语音对话、音乐播放和基础机器人控制。",
            score=0.93,
            source="manual.md",
            title="小歌功能",
        )
        idx = _FakeKnowledgeIndex([hit])
        agent, session = _new_agent()
        monkeypatch.setattr(web_ui_agent.runtime, "knowledge_index", idx)

        with pytest.raises(StopResponse):
            await agent._maybe_handle_g3_protocol_turn("小歌有哪些功能")

        assert idx.queries == ["小歌有哪些功能"]
        assert session.says == ["小歌功能：小歌支持全双工语音对话、音乐播放和基础机器人控制。"]
        assert "知识库命中" not in session.says[0]

    asyncio.run(run())


def test_g3_open_domain_knowledge_delegates_to_agent_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        idx = _FakeKnowledgeIndex()
        agent, session = _new_agent()
        monkeypatch.setattr(web_ui_agent.runtime, "knowledge_index", idx)

        await agent._maybe_handle_g3_protocol_turn("介绍一下杭州")

        assert idx.queries == []
        assert session.says == []

    asyncio.run(run())


def _execute_route() -> SemanticRoute:
    return SemanticRoute(
        SemanticDecision(
            speech_act="execute",
            domain="robot_control",
            action="navigation.move",
            slots={"direction": "forward", "distance_cm": 50},
            confidence=0.97,
            ambiguous=False,
            answer_mode="execute",
        ),
        "accepted",
    )


def test_semantic_command_dispatches_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        agent, session = _new_agent()
        router = _FakeSemanticRouter(_execute_route())
        agent._semantic_router = router
        broadcasts: list[dict] = []

        def fake_broadcast(message: dict) -> bool:
            broadcasts.append(message)
            return True

        monkeypatch.setattr(web_ui_agent, "broadcast", fake_broadcast)
        with pytest.raises(StopResponse):
            await agent._maybe_handle_g3_protocol_turn("朝前挪半米")

        protocol = [item for item in broadcasts if item.get("type") == "g3_protocol"]
        assert len(protocol) == 1
        assert protocol[0]["frames"][0]["action"] == "navigation.move"
        assert router.calls == ["朝前挪半米"]
        assert session.says == ["好的，正在执行"]

    asyncio.run(run())


@pytest.mark.parametrize("text", ["你能向前走吗", "不要向前走", "他说向前走一米"])
def test_non_execution_control_language_never_broadcasts_command(
    monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    async def run() -> None:
        agent, session = _new_agent()
        broadcasts: list[dict] = []
        monkeypatch.setattr(
            web_ui_agent,
            "broadcast",
            lambda message: broadcasts.append(message) or True,
        )

        with pytest.raises(StopResponse):
            await agent._maybe_handle_g3_protocol_turn(text)

        assert not [item for item in broadcasts if item.get("type") == "g3_protocol"]
        assert session.says
        assert "正在执行" not in session.says

    asyncio.run(run())


def test_semantic_non_execute_fails_closed_with_clarification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        agent, session = _new_agent()
        agent._semantic_router = _FakeSemanticRouter(
            SemanticRoute(None, "low_confidence", "ambiguous")
        )
        broadcasts: list[dict] = []
        monkeypatch.setattr(
            web_ui_agent,
            "broadcast",
            lambda message: broadcasts.append(message) or True,
        )

        with pytest.raises(StopResponse):
            await agent._maybe_handle_g3_protocol_turn("往那边挪一下")

        assert not [item for item in broadcasts if item.get("type") == "g3_protocol"]
        assert session.says == ["我不确定你是不是要现在执行，请直接说一个明确的操作。"]

    asyncio.run(run())


def test_semantic_chat_classification_delegates_to_agent_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        agent, session = _new_agent()
        decision = SemanticDecision(
            speech_act="chat",
            domain="chat",
            action=None,
            slots={},
            confidence=0.98,
            ambiguous=False,
            answer_mode="delegate",
        )
        agent._semantic_router = _FakeSemanticRouter(SemanticRoute(decision, "non_execute"))
        broadcasts: list[dict] = []
        monkeypatch.setattr(
            web_ui_agent,
            "broadcast",
            lambda message: broadcasts.append(message) or True,
        )

        await agent._maybe_handle_g3_protocol_turn("咱们出去挪一挪")

        assert broadcasts == []
        assert session.says == []

    asyncio.run(run())


def test_semantic_shadow_mode_falls_through_without_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        agent, session = _new_agent()
        agent._semantic_router = _FakeSemanticRouter(_execute_route(), mode="shadow")
        broadcasts: list[dict] = []
        monkeypatch.setattr(
            web_ui_agent,
            "broadcast",
            lambda message: broadcasts.append(message) or True,
        )

        await agent._maybe_handle_g3_protocol_turn("朝前挪半米")

        assert broadcasts == []
        assert session.says == []

    asyncio.run(run())
