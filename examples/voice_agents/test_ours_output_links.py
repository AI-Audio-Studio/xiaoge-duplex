"""Output capability and per-generation music schema regressions, no external IO."""

import asyncio
from types import SimpleNamespace

import pytest
from app.gate_turns import confirmed
from app.reply_policy import generation_tools
from app.semantic_gate import Decision, SemanticGate

from livekit.agents import StopResponse, function_tool, llm
from livekit.agents.voice.speech_handle import SpeechHandle


def setup():
    gate = SemanticGate(None)
    parent = SpeechHandle.create()
    message = llm.ChatMessage(role="user", content=["拍照"])
    gate.bind(message, Decision(True), parent)
    child = SpeechHandle.create()
    gate.outputs.register(child, parent, "好的，拍照。", "command")
    return gate, parent, child


def test_output_link_is_not_action_grant_and_reset_interrupts():
    async def scenario():
        gate, parent, child = setup()
        gate.require_output(child, "好的", 0)
        with pytest.raises(StopResponse):
            gate.require(child)
        gate.reset()
        assert child.interrupted and parent.interrupted and not gate.outputs.links

    asyncio.run(scenario())


def test_fixed_text_and_source_cancellation_fail_closed():
    async def scenario():
        gate, parent, child = setup()
        with pytest.raises(StopResponse):
            gate.require_output(child, "我还会播放音乐", 0)
        assert child.interrupted
        gate, parent, child = setup()
        parent.interrupt()
        with pytest.raises(StopResponse):
            gate.outputs.require(child)

    asyncio.run(scenario())


def test_history_only_sdk_items_is_idempotent_and_preserves_parent():
    async def scenario():
        gate, parent, child = setup()
        first = llm.ChatMessage(role="assistant", content=["父轮已输出"])
        parent._item_added([first])
        second = llm.ChatMessage(role="assistant", content=["好的，拍照。"])
        child._item_added([second])
        gate.record_output(parent, child)
        gate.record_output(parent, child)
        turn = next(iter(gate.turns.values()))
        turn.collect()
        turn.collect()
        assert turn.items == [first, second]
        assert len(turn.output_speeches) == 1
        assert len(confirmed(gate.turns, gate.epoch)) == 3
        gate.reset()
        child._item_added([llm.ChatMessage(role="assistant", content=["迟到"])])
        assert not confirmed(gate.turns, gate.epoch)

    asyncio.run(scenario())


def test_truncated_fixed_text_cannot_finish_synthesis():
    async def scenario():
        gate, parent, child = setup()

        async def partial():
            yield "好的"

        with pytest.raises(StopResponse):
            async for _ in gate.outputs.text_stream(child, partial()):
                pass
        assert not gate.outputs.require(child).synthesized
        gate.reset()

    asyncio.run(scenario())


def test_random_schema_wrapper_keeps_original_authorization_entry():
    from livekit.agents import RunContext

    calls = []

    @function_tool
    async def play_music(context: RunContext, music_id: str = "") -> str:
        """Play a song under strict authorization."""
        calls.append(music_id)
        context.gate.authorize(context, "play", music_id)
        return music_id

    async def scenario():
        gate = SemanticGate(None)
        parent = SpeechHandle.create()
        gate.bind(llm.ChatMessage(role="user", content=["放首歌"]), Decision(True, "play"), parent)
        context = SimpleNamespace(speech_handle=parent, gate=gate)
        generated = generation_tools([play_music], Decision(True, "play"))[0]
        assert not hasattr(play_music.info, "raw_schema")
        assert await generated(context, {"music_id": "random"}) == "random"
        with pytest.raises(StopResponse):
            await generated(context, {"music_id": "random"})
        assert calls == ["random", "random"]

    asyncio.run(scenario())


def test_random_play_schema_is_local_and_original_authorization_unchanged():
    @function_tool
    async def play_music(music_id: str = "") -> str:
        """Play a song."""
        return music_id

    filtered = generation_tools([play_music], Decision(True, "play"))
    assert filtered[0] is not play_music
    assert filtered[0].info.raw_schema["parameters"]["properties"]["music_id"]["enum"] == ["random"]
    assert generation_tools([play_music], Decision(True, "play", "指定歌")) == [play_music]
    assert not generation_tools([play_music], Decision(True))

    async def scenario():
        gate = SemanticGate(None)
        source = SpeechHandle.create()
        gate.bind(
            llm.ChatMessage(role="user", content=["放轻松音乐"]), Decision(True, "play"), source
        )
        with pytest.raises(StopResponse):
            gate.authorize(SimpleNamespace(speech_handle=source), "play", "轻松")

    asyncio.run(scenario())
