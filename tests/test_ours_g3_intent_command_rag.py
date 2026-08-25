from __future__ import annotations

import pytest

from examples.voice_agents.common.g3_intent import G3IntentEngine, SessionState
from tests._g2_contract_r5_2_2 import MiniJsonSchema, load_json


def _validator() -> MiniJsonSchema:
    from tests._g2_contract_r5_2_2 import PROTOCOL_SCHEMA_PATH

    return MiniJsonSchema(load_json(PROTOCOL_SCHEMA_PATH))


def _state(**overrides):
    values = {
        "trace_id": "trace-g3-test",
        "session_id": "sess-g3-test",
        "caps": frozenset({"audio", "text", "cmd", "state"}),
        "command_dry_run": True,
        "robot_action_enabled": False,
    }
    values.update(overrides)
    return SessionState(**values)


def test_intent_control_cmd_single_valid_enters_validator_and_builds_cmd() -> None:
    engine = G3IntentEngine()
    intent = engine.route("往前走一米", _state())
    result = engine.validate(intent, _state())
    frames = engine.build_outputs(result, _state(), utterance_id="utt-1", now_ms=1789000000000)

    assert intent.intent_type == "control_cmd"
    assert intent.route_reason == "registry_exact"
    assert result.decision == "accept"
    assert result.reason == "validated"
    assert frames[0]["type"] == "data.cmd"
    assert frames[0]["action"] == "navigation.move"
    assert frames[0]["params"] == {"direction": "forward", "distance_cm": 100}
    assert frames[0]["ack_timeout_ms"] == 3000
    assert frames[0]["result_timeout_ms"] == 3000
    _validator().assert_valid_ref("#/$defs/dataCmd", frames[0])


def test_intent_control_cmd_multi_ask_split_reply_only() -> None:
    frames = G3IntentEngine().handle_text(
        "往前走一米再挥手", _state(), utterance_id="utt-2", now_ms=1789000000001
    )

    assert [frame["type"] for frame in frames] == ["data.reply"]
    assert "cmd_id" not in frames[0]
    assert frames[0]["intent_type"] == "control_cmd"
    assert "多个操作" in frames[0]["text"]
    _validator().assert_valid_ref("#/$defs/dataReply", frames[0])


def test_intent_chat_reply_only() -> None:
    frames = G3IntentEngine().handle_text(
        "今天心情不错", _state(), utterance_id="utt-3", now_ms=1789000000002
    )

    assert [frame["type"] for frame in frames] == ["data.reply"]
    assert frames[0]["intent_type"] == "chat"
    assert "cmd_id" not in frames[0]
    _validator().assert_valid_ref("#/$defs/dataReply", frames[0])


def test_product_knowledge_qa_reply_only() -> None:
    frames = G3IntentEngine().handle_text(
        "小歌有哪些功能", _state(), utterance_id="utt-4", now_ms=1789000000003
    )

    assert [frame["type"] for frame in frames] == ["data.reply"]
    assert frames[0]["intent_type"] == "knowledge_qa"
    assert "cmd_id" not in frames[0]
    _validator().assert_valid_ref("#/$defs/dataReply", frames[0])


@pytest.mark.parametrize("text", ["什么是情绪价值", "为什么天空是蓝的", "介绍一下杭州"])
def test_open_domain_knowledge_phrases_fall_back_to_chat(text: str) -> None:
    intent = G3IntentEngine().route(text, _state())

    assert intent.intent_type == "chat"
    assert intent.route_reason == "chat_fallback"


def test_function_call_no_direct_cmd() -> None:
    result = G3IntentEngine().function_call_output(
        "send_data_cmd", {"action": "navigation.move"}, _state()
    )

    assert result.decision == "reject_policy"
    assert result.intent.route_reason == "forbidden_function_call"


def test_function_call_validator_required() -> None:
    result = G3IntentEngine().function_call_output(
        "parse_command_slots",
        {
            "raw_text": "往前走一米",
            "action": "navigation.move",
            "delivery": "data.cmd",
            "slots": {"direction": "forward", "distance_cm": 100},
            "confidence": 0.9,
        },
        _state(),
    )

    assert result.decision == "accept"
    assert result.entry is not None
    assert result.entry.action == "navigation.move"


def test_rag_timeout_fallback_reply() -> None:
    frames = G3IntentEngine().handle_text(
        "小歌有不存在的知识点吗", _state(), utterance_id="utt-5", now_ms=1789000000004
    )

    assert frames[0]["type"] == "data.reply"
    assert frames[0]["intent_type"] == "knowledge_qa"
    assert "暂未找到" in frames[0]["text"]
    assert "cmd_id" not in frames[0]


def test_rag_low_confidence_ask_clarify() -> None:
    frames = G3IntentEngine().handle_text(
        "小歌这个知识点是什么", _state(), utterance_id="utt-6", now_ms=1789000000005
    )

    assert frames[0]["type"] == "data.reply"
    assert frames[0]["intent_type"] == "knowledge_qa"
    assert "换个问法" in frames[0]["text"]
    assert "cmd_id" not in frames[0]


def test_generic_move_without_distance_defaults_to_one_step() -> None:
    frames = G3IntentEngine().handle_text(
        "往前走", _state(), utterance_id="utt-7", now_ms=1789000000006
    )

    assert frames[0]["type"] == "data.cmd"
    assert frames[0]["action"] == "navigation.move"
    assert frames[0]["params"] == {
        "direction": "forward",
        "distance_cm": 50,
        "distance_hint": "default_step",
    }


def test_generic_command_unsupported_without_cmd_cap() -> None:
    frames = G3IntentEngine().handle_text(
        "往前走一米",
        _state(caps=frozenset({"audio", "text", "state"})),
        utterance_id="utt-8",
        now_ms=1789000000007,
    )

    assert [frame["type"] for frame in frames] == ["data.reply"]
    assert frames[0]["intent_type"] == "control_cmd"
    assert "没有授权" in frames[0]["text"]
    assert "cmd_id" not in frames[0]


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("你能向前走吗", "capability_query"),
        ("你会后退吗", "capability_query"),
        ("支持转头吗", "capability_query"),
        ("请向前走一米吗", "capability_query"),
        ("重启系统吗", "capability_query"),
        ("不要向前走", "prohibit"),
        ("如果前面有人你会走吗", "hypothetical"),
        ("他说‘向前走一米’", "quotation"),
        ("等会儿向前走", "future_plan"),
    ],
)
def test_non_execution_speech_acts_never_build_commands(text: str, reason: str) -> None:
    engine = G3IntentEngine()
    intent = engine.route(text, _state())
    frames = engine.handle_text(
        text,
        _state(),
        utterance_id="utt-safe",
        now_ms=1789000000010,
    )

    assert intent.route_reason == reason
    assert [frame["type"] for frame in frames] == ["data.reply"]
    assert "cmd_id" not in frames[0]


@pytest.mark.parametrize("text", ["向前走一米", "请向前走一米", "帮我向前走一米"])
def test_explicit_and_polite_imperatives_remain_commands(text: str) -> None:
    frame = G3IntentEngine().handle_text(
        text,
        _state(),
        utterance_id="utt-positive",
        now_ms=1789000000011,
    )[0]

    assert frame["type"] == "data.cmd"
    assert frame["action"] == "navigation.move"


@pytest.mark.parametrize("text", ["可以", "确认", "不要了"])
def test_confirmation_words_without_pending_do_not_execute(text: str) -> None:
    frame = G3IntentEngine().handle_text(
        text,
        _state(),
        utterance_id="utt-no-pending",
        now_ms=1789000000012,
    )[0]

    assert frame["type"] == "data.reply"
    assert "cmd_id" not in frame


def test_high_risk_confirmation_uses_pending_command_slots() -> None:
    engine = G3IntentEngine()
    first_intent = engine.route("重启系统", _state())
    first_validation = engine.validate(first_intent, _state())
    first_frame = engine.build_outputs(
        first_validation,
        _state(),
        utterance_id="utt-risk-1",
        now_ms=1789000000013,
    )[0]

    assert first_validation.decision == "ask_confirm"
    assert first_frame["type"] == "data.reply"
    assert "确认" in first_frame["text"]

    pending = {
        "action": first_validation.entry.action,
        "slots": dict(first_intent.slots),
        "raw_text": first_intent.raw_text,
        "confidence": first_intent.confidence,
        "source_seed": first_intent.source_seed,
    }
    confirm_state = _state(pending_high_risk=pending)
    confirm_intent = engine.route("确认", confirm_state)
    confirm_validation = engine.validate(confirm_intent, confirm_state)
    confirm_frame = engine.build_outputs(
        confirm_validation,
        confirm_state,
        utterance_id="utt-risk-2",
        now_ms=1789000000014,
    )[0]

    assert confirm_validation.decision == "accept"
    assert confirm_frame["type"] == "data.cmd"
    assert confirm_frame["action"] == "system.reboot"
    assert confirm_frame["params"] == {"operation": "reboot"}


def test_high_risk_cancel_with_pending_does_not_execute() -> None:
    engine = G3IntentEngine()
    state = _state(pending_high_risk={"action": "system.shutdown", "slots": {"operation": "shutdown"}})
    frame = engine.handle_text("取消", state, utterance_id="utt-risk-cancel", now_ms=1789000000015)[0]

    assert frame["type"] == "data.reply"
    assert "cmd_id" not in frame
    assert "已取消" in frame["text"]
