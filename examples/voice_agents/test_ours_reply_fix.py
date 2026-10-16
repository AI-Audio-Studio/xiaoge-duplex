"""Deterministic lifecycle and single-authority regression; no real model calls."""

import asyncio
from types import SimpleNamespace

import pytest
from app.gate_turns import Turn, confirmed, fingerprint, user_topics
from app.reply_policy import generation_instructions, generation_tools
from app.semantic_gate import Decision, SemanticGate

from livekit.agents import RunContext, StopResponse, llm
from livekit.agents.voice.speech_handle import SpeechHandle


def run(coro):
    return asyncio.run(coro)


def test_concurrent_hooks_share_decision_and_assistant_revision():
    async def scenario():
        gate = SemanticGate(None)
        calls = []

        async def judge(payload):
            calls.append(payload)
            await asyncio.sleep(0)
            return Decision(True)

        gate._separated_decision = judge
        msg = llm.ChatMessage(role="user", content=["讲故事"])
        await asyncio.gather(gate.admit(msg), gate.admit(msg))
        gate.remember_assistant("继续吗")
        await gate.admit(msg)
        assert len(calls) == 1
        assert not confirmed(gate.turns, gate.epoch)

    run(scenario())


def test_cancel_keeps_user_topic_not_confirmed_output_until_sdk_commits():
    async def scenario():
        gate = SemanticGate(None)
        msg = llm.ChatMessage(role="user", content=["讲故事"])
        speech = SpeechHandle.create()
        gate.bind(msg, Decision(True), speech)
        gate.remember_assistant("虚构追问")
        speech.interrupt()
        assert confirmed(gate.turns, gate.epoch) == []
        assert user_topics(gate.turns, gate.epoch) == [{"role": "user", "text": "讲故事"}]
        speech._item_added([llm.ChatMessage(role="assistant", content=["实际播放片段"])])
        speech._mark_done()
        await asyncio.sleep(0)
        assert [item["text"] for item in confirmed(gate.turns, gate.epoch)] == [
            "讲故事",
            "实际播放片段",
        ]
        gate.reset()
        assert confirmed(gate.turns, gate.epoch) == []

    run(scenario())


def test_epoch_cancel_ignoring_judge_cannot_commit():
    async def scenario():
        gate = SemanticGate(None)
        ready = asyncio.Event()

        async def judge(payload):
            ready.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                return Decision(True)

        gate._separated_decision = judge
        task = asyncio.create_task(gate.admit(llm.ChatMessage(role="user", content=["拍照"])))
        await ready.wait()
        gate.reset()
        with pytest.raises((StopResponse, asyncio.CancelledError)):
            await task
        assert not gate.turns and not gate.approved and not gate.grants
        assert not user_topics(gate.turns, gate.epoch)

    run(scenario())


def test_changed_message_cannot_authorize_existing_grant():
    async def scenario():
        gate = SemanticGate(None)
        speech = SpeechHandle.create()
        msg = llm.ChatMessage(role="user", content=["放首歌"])
        gate.bind(msg, Decision(True, "play"), speech)
        msg.content = ["不要放歌"]
        with pytest.raises(StopResponse):
            gate.authorize(SimpleNamespace(speech_handle=speech), "play")

    run(scenario())


def test_single_gate_tool_and_runtime_prompt_authority():
    tools = [
        SimpleNamespace(id=name)
        for name in ["not_addressed", "play_music", "resume_music", "take_photo", "query_weather"]
    ]
    assert [t.id for t in generation_tools(tools, Decision(True))] == [
        "take_photo",
        "query_weather",
    ]
    prompt = generation_instructions("人格\n- 调用 not_addressed 保持安静\n其他能力")
    assert "调用 not_addressed 保持安静" not in prompt
    assert "当前任务优先" in prompt and "不主动总结" in prompt


def test_raw_assistant_history_cannot_be_paired_to_current():
    async def scenario():
        gate = SemanticGate(None)
        msg = llm.ChatMessage(role="user", content=["电影推荐"])
        rogue = llm.ChatMessage(role="assistant", content=["没播放的追问"])
        speech = SpeechHandle.create()
        gate.bind(msg, Decision(True), speech)
        ctx = gate.clean_context(llm.ChatContext(items=[msg, rogue]), "persona")
        assert [i.id for i in ctx.items[1:]] == [msg.id]

    run(scenario())


@pytest.mark.parametrize("operation", ["play", "resume", "switch"])
def test_music_guard_cancels_after_stop_await_before_start(operation, monkeypatch):
    from pathlib import Path

    from app import music_player

    monkeypatch.setattr(music_player, "_log", lambda text: None)

    async def scenario():
        gate = SemanticGate(None)
        speech = SpeechHandle.create()
        msg = llm.ChatMessage(role="user", content=["放首歌"])
        gate.bind(msg, Decision(True, "play"), speech)
        library = SimpleNamespace(
            resolve=lambda query: Path("fake.wav"), list_names=lambda: ["fake", "other"]
        )
        player = music_player.MusicPlayer(
            SimpleNamespace(ws_audio_output=None), library, loop=asyncio.get_running_loop()
        )
        player._last_path = Path("fake.wav")
        stopped = []

        async def stop(*, guard=None):
            guard()
            stopped.append(True)
            await asyncio.sleep(0)
            speech.interrupt()

        player._stop_internal = stop

        def guard():
            return gate.require(speech)

        with pytest.raises(StopResponse):
            if operation == "play":
                await player.play_for_tool(None, guard=guard)
            elif operation == "resume":
                await player.resume_last_for_tool(guard=guard)
            else:
                await player.switch_for_tool("next", guard=guard)
        assert stopped == [True] and player._task is None and player._current_path is None
        speech._mark_done()

    run(scenario())


def test_epoch_done_callback_cannot_repopulate_history():
    async def scenario():
        gate = SemanticGate(None)
        speech = SpeechHandle.create()
        msg = llm.ChatMessage(role="user", content=["讲故事"])
        gate.bind(msg, Decision(True), speech)
        gate.reset()
        speech._item_added([llm.ChatMessage(role="assistant", content=["旧轮追问"])])
        speech._mark_done()
        await asyncio.sleep(0)
        assert not gate.turns and not gate.approved and not gate.grants
        assert not user_topics(gate.turns, gate.epoch)

    run(scenario())


def test_history_bound_keeps_active_grant_and_cached_decision():
    async def scenario():
        gate = SemanticGate(None)
        calls = []

        async def judge(payload):
            calls.append(payload)
            return Decision(True)

        gate._separated_decision = judge
        current = llm.ChatMessage(role="user", content=["放首歌"])
        await gate.admit(current)
        speech = SpeechHandle.create()
        gate.bind(current, Decision(True), speech)
        for number in range(160):
            await gate.admit(llm.ChatMessage(role="user", content=[str(number)]))
        gate.decisions.clear()
        await gate.admit(current)
        assert len(calls) == 161
        assert len(gate.turns) <= 129
        assert len(user_topics(gate.turns, gate.epoch, current.id)) == 6
        assert all(item["text"] not in {"0", "1"} for item in user_topics(gate.turns, gate.epoch))
        assert gate.require(speech).message_id == current.id
        gate.reset()
        assert speech.interrupted
        speech._mark_done()

    run(scenario())


def test_cancelled_admission_can_retry_without_pending_leak():
    async def scenario():
        gate = SemanticGate(None)
        ready = asyncio.Event()

        async def slow(payload):
            ready.set()
            await asyncio.sleep(100)

        gate._separated_decision = slow
        msg = llm.ChatMessage(role="user", content=["电影推荐"])
        task = asyncio.create_task(gate.admit(msg))
        await ready.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not gate.pending and not gate.turns
        assert not user_topics(gate.turns, gate.epoch)

        async def good(payload):
            return Decision(True)

        gate._separated_decision = good
        assert (await gate.admit(msg)).addressed

    run(scenario())


def test_generation_policy_is_idempotent():
    first = generation_instructions("人格\nnot_addressed重复受话裁决\n能力约束")
    assert generation_instructions(first) == first


@pytest.mark.parametrize(
    ("decision", "names", "callable_name"),
    [
        (Decision(True), ["play_music"], None),
        (Decision(False, "play"), ["play_music"], None),
        (Decision(True, "play"), ["play_music"], "play_music"),
        (Decision(True, "play"), ["resume_music"], None),
        (Decision(True, "play"), [], None),
        (Decision(True, "resume"), ["resume_music", "play_music"], "resume_music"),
        (Decision(True, "stop"), ["stop_music"], "stop_music"),
        (Decision(True, "switch", direction="prev"), ["switch_music"], "switch_music"),
        (None, ["play_music"], None),
        (Decision(True, "play"), None, None),
    ],
)
def test_generation_turn_policy_uses_final_decision_and_actual_tools(
    decision, names, callable_name
):
    tools = None if names is None else [SimpleNamespace(id=name) for name in names]
    prompt = generation_instructions("人格", decision=decision, tools=tools)
    local = prompt.split("# 本轮音乐执行边界", 1)[1]
    assert "未获本轮授权不得调用" in local
    assert "伪造工具标签" in local and "没有真实成功结果" in local
    if callable_name:
        assert f"音乐工具仅有：{callable_name}。" in local
        if decision.music_action == "play":
            assert "music_id只能为random" in local
        if decision.music_action == "switch":
            assert "direction=prev" in local
    else:
        assert "不得声称音乐工具仍可用" in local
        assert "音乐工具仅有：" not in local
    assert generation_instructions(prompt, decision=decision, tools=tools) == prompt


def test_random_wrapper_and_tool_filter_still_enforce_original_grant():
    async def scenario():
        gate = SemanticGate(None)
        speech = SpeechHandle.create()
        message = llm.ChatMessage(role="user", content=["放首歌"])
        decision = Decision(True, "play")
        grant = gate.bind(message, decision, speech)
        calls = []

        @llm.function_tool
        async def play_music(context: RunContext, music_id: str) -> str:
            """Play the requested song."""
            gate.authorize(context, "play", music_id)
            calls.append(music_id)
            return "synthetic success"

        tools = generation_tools(
            [play_music, SimpleNamespace(id="resume_music"), SimpleNamespace(id="not_addressed")],
            decision,
        )
        assert [tool.id for tool in tools] == ["play_music"]
        schema = tools[0].info.raw_schema
        assert schema["parameters"]["properties"]["music_id"]["enum"] == ["random"]
        assert schema["parameters"]["required"] == ["music_id"]
        prompt = generation_instructions("人格", decision=decision, tools=tools)
        assert "music_id只能为random" in prompt
        assert not grant.used
        context = SimpleNamespace(speech_handle=speech)
        assert await tools[0](context=context, raw_arguments={"music_id": "random"})
        assert calls == ["random"] and grant.used
        with pytest.raises(StopResponse):
            await tools[0](context=context, raw_arguments={"music_id": "random"})
        assert calls == ["random"]
        gate.reset()
        speech._mark_done()

    run(scenario())


def test_truncated_tools_are_never_orphaned():
    from app.gate_turns import paired_items

    call = llm.FunctionCall(call_id="a", name="query_weather", arguments="{}")
    output = llm.FunctionCallOutput(call_id="a", name="query_weather", output="sun", is_error=False)
    assert paired_items([output]) == []
    assert paired_items([call]) == []
    assert paired_items([call, output]) == [call, output]


@pytest.mark.parametrize(
    ("text", "action", "direction", "approved"),
    [
        ("小歌，放一首轻松的音乐。", "play", "none", True),
        ("下一首", "switch", "next", True),
        ("上一首", "switch", "prev", True),
        ("现在停音乐", "stop", "none", True),
        ("恢复音乐", "resume", "none", True),
        ("老王说下一首，我只是转述", "switch", "next", False),
        ("不要放音乐", "play", "none", False),
        ("如果我说恢复音乐会怎样，现在不要执行", "resume", "none", False),
        ("继续小狐狸的故事", "resume", "none", False),
    ],
)
def test_music_review_remains_independent_with_unknown_state(text, action, direction, approved):
    """Scripted judgments test wiring, NOT real-model semantic correctness."""
    from app.semantic_gate import ACTION_PROMPT, REVIEW_PROMPT

    async def scenario():
        gate = SemanticGate(None)
        calls = []

        async def judge(prompt, payload, keys):
            calls.append((prompt, payload))
            if keys == {"addressed"}:
                return {"addressed": True}
            if keys == {"summarize_background"}:
                return {"summarize_background": False}
            if keys == {"approved"}:
                assert payload["music"] == "unknown"
                assert payload["candidate"]["music_action"] == action
                return {"approved": approved}
            return {"music_action": action, "music_target": "", "direction": direction}

        gate._judge = judge
        msg = llm.ChatMessage(role="user", content=[text])
        decision = await gate.admit(msg)
        assert decision.music_action == (action if approved else "none")
        assert sum(prompt == REVIEW_PROMPT for prompt, _ in calls) == 1
        assert any(prompt == ACTION_PROMPT for prompt, _ in calls)
        speech = SpeechHandle.create()
        gate.bind(msg, decision, speech)
        context = SimpleNamespace(speech_handle=speech)
        if approved:
            gate.authorize(context, action, direction if action == "switch" else "")
        with pytest.raises(StopResponse):
            gate.authorize(context, action, direction if action == "switch" else "")
        gate.reset()
        with pytest.raises(StopResponse):
            gate.require(speech)

    run(scenario())


def test_completed_task_policy_prohibits_optional_tail_invites():
    from app.reply_policy import GATE_REPLY_RULES

    assert "任务完成即结束" in GATE_REPLY_RULES
    assert "你有兴趣了解一下它的亮点吗" in GATE_REPLY_RULES
    assert "用户明确要求互动" in GATE_REPLY_RULES


def test_explicit_city_question_policy_overrides_default_query():
    """Check policy composition only; no claim about model obedience."""
    from app.reply_policy import GATE_REPLY_RULES

    legacy = "天气必查询；未指定城市使用默认所在地；信息足够不追问。"
    effective = generation_instructions(legacy)
    assert effective.index(legacy) < effective.index(GATE_REPLY_RULES)
    assert "优先于正文中的默认立即执行、天气必查询及少追问规则" in effective
    assert "本轮只问城市，零query_weather调用" in effective
    assert "随后回答城市并承接查询任务时查询该城市" in effective
    assert generation_instructions(effective) == effective


@pytest.mark.parametrize("review_result", [{"approved": False}, {"approved": "true"}])
def test_structured_review_still_fails_closed(review_result):
    """Scripted REVIEW refusal/invalid schema cannot become a music grant."""
    from app.semantic_gate import REVIEW_PROMPT

    async def scenario():
        gate = SemanticGate(None)
        reviews = []

        async def judge(prompt, payload, keys):
            if keys == {"addressed"}:
                return {"addressed": True}
            if keys == {"summarize_background"}:
                return {"summarize_background": False}
            if keys == {"approved"}:
                assert prompt == REVIEW_PROMPT
                assert payload["current"] == "恢复播放音乐"
                assert payload["candidate"]["music_action"] == "resume"
                reviews.append(payload)
                return review_result
            return {"music_action": "resume", "music_target": "", "direction": "none"}

        gate._judge = judge
        msg = llm.ChatMessage(role="user", content=["恢复播放音乐"])
        decision = await gate.admit(msg)
        assert len(reviews) == 1
        assert decision.music_action == "none"
        speech = SpeechHandle.create()
        if decision.addressed:
            gate.bind(msg, decision, speech)
        with pytest.raises(StopResponse):
            gate.authorize(SimpleNamespace(speech_handle=speech), "resume")

    run(scenario())


def test_device_tool_require_validates_real_gate_before_dispatch(monkeypatch):
    import web_ui_agent

    monkeypatch.setattr(web_ui_agent.random, "choice", lambda choices: choices[0])

    async def scenario():
        gate = SemanticGate(None)
        speech = SpeechHandle.create()
        msg = llm.ChatMessage(role="user", content=["拍张照"])
        gate.bind(msg, Decision(True), speech)
        effects = []
        confirmations = []

        def dispatch(frames, **kwargs):
            assert kwargs == {"reason": "llm_tool_take_photo", "say_ack": False}
            effects.append(frames)
            return True

        async def say_linked(context, text, *, purpose, history=False):
            confirmations.append((context, text, purpose, history))

        agent = SimpleNamespace(
            _gate_enabled=lambda: True,
            _gate=lambda: gate,
            _dispatch_g3_cmd_frames=dispatch,
            _say_linked=say_linked,
        )
        agent._require_gate_turn = web_ui_agent.VoiceAgent._require_gate_turn.__get__(agent)
        agent._command_ack = web_ui_agent.VoiceAgent._command_ack.__get__(agent)
        context = SimpleNamespace(speech_handle=speech)
        with pytest.raises(StopResponse):
            await web_ui_agent.VoiceAgent.take_photo(agent, context)
        assert len(effects) == 1
        assert len(effects[0]) == 1
        assert confirmations == [
            (context, web_ui_agent._natural_command_ack(effects[0]), "command", True)
        ]
        speech.interrupt()
        with pytest.raises(StopResponse):
            await web_ui_agent.VoiceAgent.take_photo(agent, context)
        assert len(effects) == len(confirmations) == 1

        fresh_speech = SpeechHandle.create()
        fresh_msg = llm.ChatMessage(role="user", content=["拍张照"])
        gate.bind(fresh_msg, Decision(True), fresh_speech)
        fresh_context = SimpleNamespace(speech_handle=fresh_speech)
        agent._require_gate_turn(fresh_context)
        gate.reset()
        with pytest.raises(StopResponse):
            await web_ui_agent.VoiceAgent.take_photo(agent, fresh_context)
        assert len(effects) == len(confirmations) == 1

    run(scenario())


def test_topic_projection_filters_and_bounds_without_committing():
    turns = {}
    for number in range(10):
        msg = llm.ChatMessage(role="user", content=[str(number) * 600])
        turns[msg.id] = Turn(2, msg, fingerprint(msg), Decision(True))
    current = msg.id
    assert len(user_topics(turns, 2, current)) == 6
    assert all(len(item["text"]) == 500 for item in user_topics(turns, 2, current))
    assert all(item["text"] != "9" * 500 for item in user_topics(turns, 2, current))
    assert not confirmed(turns, 2)
    assert not user_topics(turns, 3)
    for turn in turns.values():
        turn.message.content = ["tampered"]
    assert not user_topics(turns, 2)
    msg = llm.ChatMessage(role="user", content=["旁聊"])
    turns[msg.id] = Turn(2, msg, fingerprint(msg), Decision(False))
    assert not user_topics(turns, 2)


def test_movie_continuation_payload_and_generation_are_data_not_old_commands():
    async def scenario():
        gate = SemanticGate(None)
        old = llm.ChatMessage(role="user", content=["推荐一部科幻电影"])
        speech = SpeechHandle.create()
        gate.bind(old, Decision(True), speech)
        speech.interrupt()
        speech._mark_done()
        await asyncio.sleep(0)
        seen = []

        async def judge(prompt, payload, keys):
            seen.append((keys, payload))
            if keys == {"addressed"}:
                assert payload["user_topics"] == [{"role": "user", "text": old.text_content}]
                assert payload["confirmed"] == []
                return {"addressed": True}
            assert "user_topics" not in payload
            if keys == {"summarize_background"}:
                return {"summarize_background": False}
            if keys == {"approved"}:
                return {"approved": False}
            return {"music_action": "play", "music_target": "", "direction": "none"}

        gate._judge = judge
        current = llm.ChatMessage(role="user", content=["那部电影有什么亮点？"])
        decision = await gate.admit(current)
        assert decision == Decision(True)
        assert len(seen) == 4
        fresh = SpeechHandle.create()
        gate.bind(current, decision, fresh)
        ctx = gate.clean_context(llm.ChatContext(items=[old, current]), "persona")
        assert [i.id for i in ctx.items if i.role == "user"] == [current.id]
        assert not [i for i in ctx.items if i.role == "assistant"]
        block = ctx.items[1].text_content
        assert "历史用户话题数据" in block and old.text_content in block
        assert "不得重执行旧动作" in block and "未确认助手回复" in block
        assert current.text_content not in block
        with pytest.raises(StopResponse):
            gate.authorize(SimpleNamespace(speech_handle=fresh), "play")
        gate.reset()
        assert not user_topics(gate.turns, gate.epoch)

    run(scenario())


@pytest.mark.parametrize("failure", ["denied", "error", "changed"])
def test_unsuccessful_admission_never_becomes_topic(failure):
    async def scenario():
        gate = SemanticGate(None)
        msg = llm.ChatMessage(role="user", content=["电影推荐"])

        async def judge(payload):
            if failure == "error":
                raise ValueError("fake schema failure")
            if failure == "changed":
                msg.content = ["篡改"]
            return Decision(failure != "denied")

        gate._separated_decision = judge
        if failure == "changed":
            with pytest.raises(StopResponse):
                await gate.admit(msg)
        else:
            assert not (await gate.admit(msg)).addressed
        assert not user_topics(gate.turns, gate.epoch)
        assert not confirmed(gate.turns, gate.epoch)

    run(scenario())


@pytest.mark.parametrize("text", ["好", "一二三", "老王，那部电影不错"])
def test_topic_is_not_a_fabricated_assistant_question_or_admission(text):
    async def scenario():
        gate = SemanticGate(None)
        old = llm.ChatMessage(role="user", content=["推荐电影"])
        gate.bind(old, Decision(True), SpeechHandle.create())
        gate.remember_assistant("想看科幻吗")
        seen = []

        async def judge(prompt, payload, keys):
            if keys == {"addressed"}:
                seen.append(payload)
                assert payload["confirmed"] == []
                assert payload["user_topics"] == [{"role": "user", "text": "推荐电影"}]
                return {"addressed": False}
            if keys == {"summarize_background"}:
                return {"summarize_background": False}
            return {"music_action": "none", "music_target": "", "direction": "none"}

        gate._judge = judge
        msg = llm.ChatMessage(role="user", content=[text])
        assert not (await gate.admit(msg)).addressed
        assert len(seen) == 1
        ctx = gate.clean_context(llm.ChatContext(items=[old, msg]), "persona")
        assert not any("历史用户话题数据" in i.text_content for i in ctx.items)
        assert all(i["text"] != text for i in user_topics(gate.turns, gate.epoch))
        gate.reset()

    run(scenario())


@pytest.mark.parametrize("action", ["play", "switch", "resume"])
def test_uncommitted_music_topic_cannot_transfer_grant(action):
    async def scenario():
        gate = SemanticGate(None)
        old = llm.ChatMessage(role="user", content=["放首歌"])
        speech = SpeechHandle.create()
        old_decision = Decision(True, action, direction="next" if action == "switch" else "none")
        grant = gate.bind(old, old_decision, speech)
        argument = "next" if action == "switch" else ""
        gate.authorize(SimpleNamespace(speech_handle=speech), action, argument)
        speech.interrupt()
        assert user_topics(gate.turns, gate.epoch)
        current = llm.ChatMessage(role="user", content=["那部电影有什么亮点？"])
        fresh = SpeechHandle.create()
        gate.bind(current, Decision(True), fresh)
        assert grant.used
        with pytest.raises(StopResponse):
            gate.authorize(SimpleNamespace(speech_handle=fresh), action, argument)
        with pytest.raises(StopResponse):
            gate.require(speech)
        assert grant.used
        gate.reset()

    run(scenario())
