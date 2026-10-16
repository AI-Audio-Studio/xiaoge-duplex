from __future__ import annotations

import asyncio
import types

import pytest

import web_ui_agent


class FakeSession:
    def __init__(self):
        self.replies = []
        self.says = []

    def generate_reply(self, **kwargs):
        self.replies.append(kwargs)

    def say(self, text, **kwargs):
        self.says.append((text, kwargs))


def make_agent():
    session = FakeSession()
    agent = types.SimpleNamespace(
        session=session,
        _wiring=types.SimpleNamespace(live=None),
        _finish_consumed_voice_turn=lambda *args: None,
        _remember_executed=lambda *args: None,
    )
    agent._apply_turn_filters = web_ui_agent.VoiceAgent._apply_turn_filters.__get__(agent)
    agent._music_control_intent = web_ui_agent.VoiceAgent._music_control_intent.__get__(agent)
    return agent, session


@pytest.mark.parametrize(
    "text",
    ["换一个故事", "讲个悬疑故事", "说个笑话", "来一个童话故事", "写首诗", "悬疑的"],
)
def test_creative_input_is_not_consumed_by_turn_filters(text):
    agent, session = make_agent()
    asyncio.run(agent._apply_turn_filters(text, False))
    assert session.replies == []
    # 创作输入应由后续标准 LLM 回复处理，而不是由 turn filter 消费。


def test_creative_detector_and_special_route_are_removed():
    assert not hasattr(web_ui_agent, "_is_creative_request")
    assert not hasattr(web_ui_agent, "_CREATIVE_REPLY_INSTRUCTIONS")
    assert "tool_choice=\"none\"" not in web_ui_agent.VoiceAgent.handle_manual_text.__code__.co_consts
    assert "CREATIVE_REQUEST_NO_TOOLS" not in web_ui_agent.VoiceAgent.on_user_turn_completed.__code__.co_consts


def test_manual_text_uses_standard_reply_path(monkeypatch):
    agent, session = make_agent()
    agent._maybe_apply_agent_config = _async_noop
    agent._duplex_off = lambda: False
    agent._apply_turn_filters = _async_noop
    monkeypatch.setattr(web_ui_agent, "_append_turn_log", lambda *args: None)
    monkeypatch.setattr(web_ui_agent, "exit_reset_idle_timer", lambda: None)
    monkeypatch.setattr(web_ui_agent, "mark_agent_output_started", lambda: None)
    asyncio.run(web_ui_agent.VoiceAgent.handle_manual_text(agent, "换一个故事"))
    assert not any("tool_choice" in call for call in session.replies)


async def _async_noop(*args, **kwargs):
    return None


def test_duplicate_user_turn_is_suppressed_within_window():
    agent = object.__new__(web_ui_agent.VoiceAgent)
    agent._last_submitted_user_turn = None
    assert not agent._is_duplicate_user_turn("讲一个故事")
    assert agent._is_duplicate_user_turn("讲 一个故事")
