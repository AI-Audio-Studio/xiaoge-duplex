from __future__ import annotations

import pytest

from examples.voice_agents.common.g3_intent import G3IntentEngine, SessionState
from tests._g2_contract_r5_2_2 import MiniJsonSchema, load_json


def _validator() -> MiniJsonSchema:
    from tests._g2_contract_r5_2_2 import PROTOCOL_SCHEMA_PATH

    return MiniJsonSchema(load_json(PROTOCOL_SCHEMA_PATH))


def _state(**overrides):
    values = {
        "trace_id": "trace-x3-test",
        "session_id": "sess-x3-test",
        "caps": frozenset({"audio", "text", "cmd", "state"}),
        "command_dry_run": True,
        "robot_action_enabled": False,
    }
    values.update(overrides)
    return SessionState(**values)


def _frame(text: str):
    return G3IntentEngine().handle_text(
        text, _state(), utterance_id="utt-x3", now_ms=1789000100000
    )[0]


def test_x3_move_distance_params_from_requirement_sheet() -> None:
    frame = _frame("往前走一米")

    assert frame["type"] == "data.cmd"
    assert frame["action"] == "navigation.move"
    assert frame["params"] == {
        "direction": "forward",
        "distance_cm": 100,
    }
    _validator().assert_valid_ref("#/$defs/dataCmd", frame)


def test_x3_move_supports_centimeter_and_ten_meter_examples() -> None:
    back = _frame("往后走50公分")
    right = _frame("往右走10米")

    assert back["params"]["direction"] == "backward"
    assert back["params"]["distance_cm"] == 50
    assert right["params"]["direction"] == "right"
    assert right["params"]["distance_cm"] == 1000
    _validator().assert_valid_ref("#/$defs/dataCmd", back)
    _validator().assert_valid_ref("#/$defs/dataCmd", right)


def test_x3_move_supports_step_count_as_distance() -> None:
    frame = _frame("往前走两步")

    assert frame["type"] == "data.cmd"
    assert frame["action"] == "navigation.move"
    assert frame["params"] == {
        "direction": "forward",
        "distance_cm": 100,
    }
    _validator().assert_valid_ref("#/$defs/dataCmd", frame)


def test_x3_head_and_eye_commands_emit_params() -> None:
    nod = _frame("点点头")
    eye = _frame("闭上你的左眼")

    assert nod["action"] == "head.motion"
    assert nod["params"] == {"gesture": "nod"}
    assert eye["action"] == "face.eye"
    assert eye["params"] == {"operation": "close", "side": "left"}
    _validator().assert_valid_ref("#/$defs/dataCmd", nod)
    _validator().assert_valid_ref("#/$defs/dataCmd", eye)


def test_x3_gesture_side_and_count_params() -> None:
    wave = _frame("左手挥手")
    water = _frame("拿两瓶水")

    assert wave["action"] == "gesture.perform"
    assert wave["params"] == {"gesture": "wave", "side": "left"}
    assert water["action"] == "gesture.perform"
    assert water["params"] == {"gesture": "fetch_water", "count": 2}


def test_x3_laugh_and_cry_emit_simulator_failure_gestures() -> None:
    laugh = _frame("笑一个")
    cry = _frame("哭一个")

    assert laugh["action"] == laugh["capability_id"] == "gesture.perform"
    assert laugh["params"] == {"gesture": "laugh"}
    assert cry["action"] == cry["capability_id"] == "gesture.perform"
    assert cry["params"] == {"gesture": "cry"}
    _validator().assert_valid_ref("#/$defs/dataCmd", laugh)
    _validator().assert_valid_ref("#/$defs/dataCmd", cry)


def test_x3_tour_and_volume_params() -> None:
    lang = _frame("切换到英文讲解")
    volume = _frame("音量调到60%")

    assert lang["action"] == "tour.control"
    assert lang["params"] == {"operation": "set_language", "language": "en"}
    assert volume["action"] == "audio.volume.set"
    assert volume["params"] == {"operation": "set", "volume_percent": 60}


def test_x3_connection_params_and_high_risk_confirmation() -> None:
    wifi = _frame("打开WiFi")
    reboot = _frame("系统重启一下")

    assert wifi["action"] == "connection.set"
    assert wifi["params"] == {"target": "wifi", "enabled": True}
    assert reboot["type"] == "data.reply"
    assert reboot["intent_type"] == "control_cmd"
    assert "确认" in reboot["text"]
    assert "cmd_id" not in reboot


def test_x3_query_rows_reply_only() -> None:
    battery = _frame("查一下现在还有多少电")
    knowledge = _frame("智元公司的地址在哪")

    assert battery["type"] == "data.reply"
    assert battery["intent_type"] == "info_query"
    assert "cmd_id" not in battery
    assert knowledge["type"] == "data.reply"
    assert knowledge["intent_type"] == "knowledge_qa"
    assert "cmd_id" not in knowledge


def test_x3_motion_turn_has_independent_positive_case() -> None:
    frame = _frame("往右转一点")

    assert frame["type"] == "data.cmd"
    assert frame["action"] == "motion.turn"
    assert frame["params"]["direction"] == "right"
    assert frame["params"]["angle_deg"] == 15
    _validator().assert_valid_ref("#/$defs/dataCmd", frame)


@pytest.mark.parametrize(("text", "direction"), [("向左转", "left"), ("向右转", "right")])
def test_x3_common_turn_phrases_use_registry_fast_path(text: str, direction: str) -> None:
    engine = G3IntentEngine()
    intent = engine.route(text, _state())
    frame = _frame(text)

    assert intent.route_reason == "registry_exact"
    assert frame["type"] == "data.cmd"
    assert frame["action"] == "motion.turn"
    assert frame["params"] == {"direction": direction}


@pytest.mark.parametrize("text", ["你能向左转吗", "你会向右转吗"])
def test_x3_turn_capability_questions_remain_reply_only(text: str) -> None:
    frame = _frame(text)

    assert frame["type"] == "data.reply"
    assert frame["intent_type"] == "info_query"
    assert "cmd_id" not in frame


def test_x3_face_eyebrow_has_independent_positive_case() -> None:
    frame = _frame("挑左边眉毛")

    assert frame["type"] == "data.cmd"
    assert frame["action"] == "face.eyebrow"
    assert frame["params"] == {"operation": "raise", "side": "left"}
    _validator().assert_valid_ref("#/$defs/dataCmd", frame)


def test_x3_system_shutdown_has_independent_confirmation_case() -> None:
    engine = G3IntentEngine()
    state = _state()
    intent = engine.route("关闭系统", state)
    frame = engine.handle_text(
        "关闭系统", state, utterance_id="utt-x3-shutdown", now_ms=1789000100000
    )[0]

    assert intent.candidate_action == "system.shutdown"
    assert frame["type"] == "data.reply"
    assert frame["intent_type"] == "control_cmd"
    assert "确认" in frame["text"]
    assert "cmd_id" not in frame
    _validator().assert_valid_ref("#/$defs/dataReply", frame)


def test_x3_power_charge_has_independent_positive_case() -> None:
    frame = _frame("回去充电")

    assert frame["type"] == "data.cmd"
    assert frame["action"] == "power.charge"
    assert frame["params"] == {"operation": "return_to_charge"}
    _validator().assert_valid_ref("#/$defs/dataCmd", frame)
