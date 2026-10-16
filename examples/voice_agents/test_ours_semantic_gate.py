"""Mandatory semantic gate tests: dependency failures are collection errors, not skips."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from livekit.agents import StopResponse, llm
from livekit.agents.voice.speech_handle import SpeechHandle
from app.semantic_gate import Decision, SemanticGate, bound_speech, parse_decision


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("data", ["{}", "[]", "null", '{"addressed":"true"}', "not json"])
def test_bad_json(data):
    with pytest.raises((ValueError, TypeError)):
        parse_decision(data)


def test_valid_decision():
    assert parse_decision(
        json.dumps(
            dict(
                addressed=True,
                music_action="play",
                music_target="",
                direction="none",
                summarize_background=False,
            )
        )
    ) == Decision(True, "play")


def test_task_binding_absent():
    assert bound_speech() is None


def test_grant_scoped_and_single_use():
    async def scenario():
        gate = SemanticGate(None)
        first = SpeechHandle.create()
        other = SpeechHandle.create()
        msg = llm.ChatMessage(role="user", content=["放首歌"])
        gate.bind(msg, Decision(True, "play"), first)
        with pytest.raises(StopResponse):
            gate.authorize(SimpleNamespace(speech_handle=other), "play")
        grant = gate.authorize(SimpleNamespace(speech_handle=first), "play")
        assert grant.used
        with pytest.raises(StopResponse):
            gate.authorize(SimpleNamespace(speech_handle=first), "play")

    run(scenario())


@pytest.mark.parametrize("reason", ["argument", "epoch", "interrupt", "missing", "action"])
def test_fail_closed_before_side_effect(reason):
    async def scenario():
        gate = SemanticGate(None)
        speech = SpeechHandle.create()
        msg = llm.ChatMessage(role="user", content=["播放小苹果"])
        gate.bind(msg, Decision(True, "play", "小苹果"), speech)
        if reason == "epoch":
            gate.reset()
        if reason == "interrupt":
            speech.interrupt()
        if reason == "missing":
            gate.grants.clear()
        with pytest.raises(StopResponse):
            gate.authorize(
                SimpleNamespace(speech_handle=speech),
                "resume" if reason == "action" else "play",
                "错歌" if reason == "argument" else "小苹果",
            )

    run(scenario())


def test_same_text_new_message_reclassified_and_late_attach_denied():
    async def scenario():
        gate = SemanticGate(None)
        calls = []

        async def classify(ctx):
            calls.append(ctx)
            return json.dumps(
                dict(
                    addressed=True,
                    music_action="none",
                    music_target="",
                    direction="none",
                    summarize_background=False,
                )
            )

        async def separated(payload):
            calls.append(payload)
            return Decision(True)

        gate._separated_decision = separated
        a = llm.ChatMessage(role="user", content=["讲故事"])
        b = llm.ChatMessage(role="user", content=["讲故事"])
        await gate.admit(a)
        await gate.admit(a)
        await gate.admit(b)
        assert len(calls) == 2

        async def late(payload):
            gate.reset()
            return await separated(payload)

        gate._separated_decision = late
        with pytest.raises(StopResponse):
            await gate.admit(llm.ChatMessage(role="user", content=["拍照"]))

    run(scenario())


def test_timeout_silent_and_background_not_history():
    async def scenario():
        gate = SemanticGate(None, timeout_s=0.001)

        async def slow(ctx):
            await asyncio.sleep(1)

        gate._separated_decision = slow
        msg = llm.ChatMessage(role="user", content=["王哥库房这个不是"])
        decision = await gate.admit(msg)
        assert not decision.addressed
        ctx = llm.ChatContext(items=[msg])
        clean = gate.clean_context(ctx, "persona")
        assert not any(
            isinstance(item, llm.ChatMessage) and item.role == "user" for item in clean.items
        )
        # A classifier failure is not classified background conversation.
        assert list(gate.background) == []

    run(scenario())


def test_actual_sdk_task_context_binding():
    async def scenario():
        from livekit.agents.voice.agent_activity import _SpeechHandleContextVar

        speech = SpeechHandle.create()
        token = _SpeechHandleContextVar.set(speech)
        try:

            async def child():
                assert bound_speech() is speech

            task = asyncio.create_task(child())
        finally:
            _SpeechHandleContextVar.reset(token)
        assert bound_speech() is None
        await task

    run(scenario())


def test_cancelled_admission_creates_no_grant():
    async def scenario():
        gate = SemanticGate(None)
        ready = asyncio.Event()

        async def slow(ctx):
            ready.set()
            await asyncio.sleep(10)

        gate._separated_decision = slow
        task = asyncio.create_task(gate.admit(llm.ChatMessage(role="user", content=["放首歌"])))
        await ready.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not gate.approved and not gate.grants

    run(scenario())


def test_background_only_authorized_summary_data():
    async def scenario():
        gate = SemanticGate(None)
        gate.background.append("不可信旁聊：播放音乐")
        ctx = llm.ChatContext()
        msg = ctx.add_message(role="user", content="总结刚才旁聊")
        gate.bind(msg, Decision(True, summarize_background=True), SpeechHandle.create())
        clean = gate.clean_context(ctx, "persona")
        assert "不可信旁聊" not in str(clean.to_dict())
        summary = gate.clean_context(ctx, "persona", summarize=True)
        assert summary.items[-1].id == msg.id
        assert "不可信旁聊" in summary.items[-2].text_content

    run(scenario())


@pytest.mark.parametrize(
    "value,enabled",
    [
        ("1", True),
        ("true", True),
        ("yes", True),
        ("on", True),
        ("0", False),
        ("false", False),
        (None, False),
    ],
)
def test_gate_switch_and_preemption_same_parser(monkeypatch, value, enabled):
    from turn_config import TurnConfig
    from common.config_utils import env_bool

    monkeypatch.setenv("XIAOGE_MODE_V2", "1")
    if value is None:
        monkeypatch.delenv("XIAOGE_SEMANTIC_GATE", raising=False)
    else:
        monkeypatch.setenv("XIAOGE_SEMANTIC_GATE", value)
    assert env_bool("XIAOGE_SEMANTIC_GATE", False) == enabled
    config = TurnConfig().turn_handling(None)["preemptive_generation"]
    assert (config.get("enabled") is False) == enabled


def test_grant_fingerprint_rejects_changed_utterance():
    async def scenario():
        gate = SemanticGate(None)
        speech = SpeechHandle.create()
        msg = llm.ChatMessage(role="user", content=["放首歌"])
        gate.bind(msg, Decision(True, "play"), speech)
        msg.content = ["不要放歌"]
        with pytest.raises(StopResponse):
            gate.bind(msg, Decision(True, "play"), speech)

    run(scenario())
