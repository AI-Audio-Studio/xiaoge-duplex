from __future__ import annotations

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


def test_intent_knowledge_qa_rag_reply_only() -> None:
    frames = G3IntentEngine().handle_text(
        "什么是全双工", _state(), utterance_id="utt-4", now_ms=1789000000003
    )

    assert [frame["type"] for frame in frames] == ["data.reply"]
    assert frames[0]["intent_type"] == "knowledge_qa"
    assert "cmd_id" not in frames[0]
    assert "全双工" in frames[0]["text"]
    _validator().assert_valid_ref("#/$defs/dataReply", frames[0])


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
        "介绍一个不存在的知识点", _state(), utterance_id="utt-5", now_ms=1789000000004
    )

    assert frames[0]["type"] == "data.reply"
    assert frames[0]["intent_type"] == "knowledge_qa"
    assert "暂未找到" in frames[0]["text"]
    assert "cmd_id" not in frames[0]


def test_rag_low_confidence_ask_clarify() -> None:
    frames = G3IntentEngine().handle_text(
        "什么是这个", _state(), utterance_id="utt-6", now_ms=1789000000005
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
