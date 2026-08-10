"""R5.2.2 G3 cloud intent routing and X3 command validation.

The command catalog is derived from docs/智元X3语音交互需求0723-已讨论答复.xlsx,
sheet "离线技能清单（待更新）". This module remains deterministic: every
command-like turn is routed, slot-filled, validated, and only then converted into
protocol-shaped frames. Real robot execution is still gated outside this module.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

Delivery = Literal[
    "data.cmd",
    "data.cmd after confirmation",
    "cloud_tool + data.reply",
    "cloud_knowledge + data.reply",
    "ask_split only",
]
IntentType = Literal[
    "control_cmd",
    "control_cmd_multi",
    "info_query",
    "knowledge_qa",
    "chat",
    "config",
]
Decision = Literal[
    "accept",
    "ask_missing",
    "ask_clarify",
    "ask_confirm",
    "reject_capability",
    "reject_policy",
    "cancel",
    "timeout",
]

EXECUTABLE_DELIVERIES = {"data.cmd", "data.cmd after confirmation"}
_MULTI_MARKERS = ("然后", "同时", "并且", "接着", "顺便")
_AFFIRMATIVE_CONFIRM = ("确认", "执行", "可以", "是的", "好的", "同意")
_CANCEL_CONFIRM = ("取消", "算了", "不要", "停止")
_ASK_WORDS = ("什么", "怎么", "为什么", "多少", "哪里", "在哪", "介绍", "查询", "查一下")
_STEP_DISTANCE_CM = 50


@dataclass(frozen=True)
class ParamSpec:
    name: str
    type: Literal["enum", "int", "bool", "str"]
    required: bool = True
    enum: tuple[Any, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    unit: str = ""


@dataclass(frozen=True)
class RegistryEntry:
    action: str
    capability_id: str
    intent_type: IntentType
    delivery: Delivery
    params: tuple[ParamSpec, ...]
    risk_level: Literal["low", "medium", "high"]
    owner: str
    unsupported_behavior: str
    source_seed: str
    positive_examples: tuple[str, ...] = ()
    match: Callable[[str], bool] | None = None
    extract: Callable[[str], dict[str, Any]] | None = None


@dataclass(frozen=True)
class SessionState:
    trace_id: str = "trace-g3-local"
    session_id: str = "sess-g3-local"
    caps: frozenset[str] = field(
        default_factory=lambda: frozenset({"audio", "text", "cmd", "state"})
    )
    interaction_mode: Literal["active", "sleeping", "closed"] = "active"
    engine_gate: Literal["open", "closed"] = "open"
    command_dry_run: bool = True
    robot_action_enabled: bool = False
    pending_high_risk: dict[str, Any] | None = None


@dataclass(frozen=True)
class IntentResult:
    intent_type: IntentType
    confidence: float
    route_reason: str
    raw_text: str
    delivery: Delivery | None = None
    candidate_action: str | None = None
    slots: dict[str, Any] = field(default_factory=dict)
    detected_actions: tuple[str, ...] = ()
    needs_validator: bool = False
    source_seed: str = ""


@dataclass(frozen=True)
class ValidationResult:
    decision: Decision
    intent: IntentResult
    entry: RegistryEntry | None = None
    reason: str = ""


def default_registry() -> tuple[RegistryEntry, ...]:
    """Executable X3 command registry view approved for cloud-side dry-run output."""

    owner = "Cloud owner"
    return (
        RegistryEntry(
            action="navigation.move",
            capability_id="motion.move",
            intent_type="control_cmd",
            delivery="data.cmd",
            risk_level="medium",
            owner=owner,
            unsupported_behavior="reply unsupported",
            source_seed="X3-SHEET-ROWS-18-26",
            params=(
                ParamSpec("direction", "enum", enum=("forward", "backward", "left", "right")),
                ParamSpec("distance_cm", "int", minimum=1, maximum=10000, unit="cm"),
            ),
            positive_examples=(
                "往前走一米",
                "往后走50公分",
                "往左走30公分",
                "往右走10米",
                "再走10cm",
            ),
            match=_match_move,
            extract=_extract_move,
        ),
        RegistryEntry(
            action="motion.turn",
            capability_id="motion.turn",
            intent_type="control_cmd",
            delivery="data.cmd",
            risk_level="medium",
            owner=owner,
            unsupported_behavior="reply unsupported",
            source_seed="X3-SHEET-ROWS-38-44",
            params=(
                ParamSpec("direction", "enum", enum=("left", "right", "back")),
                ParamSpec("angle_deg", "int", required=False, minimum=1, maximum=360, unit="deg"),
            ),
            positive_examples=("转身", "往左转身", "往右转一点", "向后转", "再转一点"),
            match=_match_turn,
            extract=_extract_turn,
        ),
        RegistryEntry(
            action="head.motion",
            capability_id="head.motion",
            intent_type="control_cmd",
            delivery="data.cmd",
            risk_level="low",
            owner=owner,
            unsupported_behavior="reply unsupported",
            source_seed="X3-SHEET-ROWS-2-7",
            params=(ParamSpec("gesture", "enum", enum=("shake", "nod", "look")),),
            positive_examples=("摇摇头", "点点头", "往左边看一下", "往右边看一下"),
            match=_match_head,
            extract=_extract_head,
        ),
        RegistryEntry(
            action="face.eye",
            capability_id="face.eye",
            intent_type="control_cmd",
            delivery="data.cmd",
            risk_level="low",
            owner=owner,
            unsupported_behavior="reply unsupported",
            source_seed="X3-SHEET-ROWS-8-37",
            params=(
                ParamSpec("operation", "str"),
                ParamSpec("side", "enum", required=False, enum=("left", "right", "both")),
            ),
            positive_examples=("闭上眼睛", "闭左眼", "睁开双眼", "眨眨眼", "翻白眼", "抛媚眼"),
            match=_match_eye,
            extract=_extract_eye,
        ),
        RegistryEntry(
            action="face.eyebrow",
            capability_id="face.eyebrow",
            intent_type="control_cmd",
            delivery="data.cmd",
            risk_level="low",
            owner=owner,
            unsupported_behavior="reply unsupported",
            source_seed="X3-SHEET-ROWS-45-49",
            params=(
                ParamSpec("operation", "enum", enum=("raise", "frown")),
                ParamSpec("side", "enum", required=False, enum=("left", "right", "both")),
            ),
            positive_examples=("挑眉毛", "挑左边眉毛", "皱眉"),
            match=_match_eyebrow,
            extract=_extract_eyebrow,
        ),
        RegistryEntry(
            action="gesture.perform",
            capability_id="gesture.perform",
            intent_type="control_cmd",
            delivery="data.cmd",
            risk_level="low",
            owner=owner,
            unsupported_behavior="reply unsupported",
            source_seed="X3-SHEET-ROWS-50-112",
            params=(
                ParamSpec("gesture", "str"),
                ParamSpec("side", "enum", required=False, enum=("left", "right", "both")),
                ParamSpec("count", "int", required=False, minimum=1, maximum=99),
            ),
            positive_examples=("挥手", "左手挥手", "举手", "握手", "点赞", "比心", "拿两瓶水"),
            match=_match_gesture,
            extract=_extract_gesture,
        ),
        RegistryEntry(
            action="tour.control",
            capability_id="tour.control",
            intent_type="control_cmd",
            delivery="data.cmd",
            risk_level="low",
            owner=owner,
            unsupported_behavior="reply unsupported",
            source_seed="X3-SHEET-ROWS-113-149",
            params=(
                ParamSpec("operation", "str"),
                ParamSpec("language", "str", required=False),
                ParamSpec("mode", "enum", required=False, enum=("auto", "manual")),
                ParamSpec("point_index", "int", required=False, minimum=1, maximum=999),
            ),
            positive_examples=(
                "开始讲解",
                "暂停讲解",
                "继续讲解",
                "结束讲解",
                "下一个点",
                "英文讲解",
            ),
            match=_match_tour,
            extract=_extract_tour,
        ),
        RegistryEntry(
            action="audio.volume.set",
            capability_id="audio.volume",
            intent_type="control_cmd",
            delivery="data.cmd",
            risk_level="low",
            owner=owner,
            unsupported_behavior="reply unsupported",
            source_seed="X3-SHEET-ROWS-150-151",
            params=(
                ParamSpec("operation", "enum", enum=("set", "increase", "decrease", "max", "min")),
                ParamSpec("volume_percent", "int", required=False, minimum=0, maximum=100),
            ),
            positive_examples=("音量调到60%", "声音调大一点", "音量调到最大"),
            match=_match_volume,
            extract=_extract_volume,
        ),
        RegistryEntry(
            action="connection.set",
            capability_id="connection.set",
            intent_type="control_cmd",
            delivery="data.cmd",
            risk_level="low",
            owner=owner,
            unsupported_behavior="reply unsupported",
            source_seed="X3-SHEET-ROWS-159-160",
            params=(
                ParamSpec("target", "enum", enum=("wifi", "bluetooth")),
                ParamSpec("enabled", "bool"),
            ),
            positive_examples=("打开WiFi", "关闭蓝牙"),
            match=_match_connection,
            extract=_extract_connection,
        ),
        RegistryEntry(
            action="system.reboot",
            capability_id="system.reboot",
            intent_type="control_cmd",
            delivery="data.cmd after confirmation",
            risk_level="high",
            owner=owner,
            unsupported_behavior="ask confirmation",
            source_seed="X3-SHEET-ROWS-161-162",
            params=(),
            positive_examples=("重启系统", "重启机器人"),
            match=lambda text: "重启" in text,
            extract=lambda text: {"operation": "reboot"},
        ),
        RegistryEntry(
            action="system.shutdown",
            capability_id="system.shutdown",
            intent_type="control_cmd",
            delivery="data.cmd after confirmation",
            risk_level="high",
            owner=owner,
            unsupported_behavior="ask confirmation",
            source_seed="X3-SHEET-ROWS-161-162",
            params=(),
            positive_examples=("关机", "关闭系统"),
            match=lambda text: "关机" in text or "关闭系统" in text,
            extract=lambda text: {"operation": "shutdown"},
        ),
        RegistryEntry(
            action="power.query",
            capability_id="power.query",
            intent_type="info_query",
            delivery="cloud_tool + data.reply",
            risk_level="low",
            owner=owner,
            unsupported_behavior="reply only",
            source_seed="X3-SHEET-ROWS-163-164",
            params=(),
            positive_examples=("查电量", "还有多少电"),
            match=lambda text: "电量" in text or "多少电" in text,
            extract=lambda text: {"topic": "battery"},
        ),
        RegistryEntry(
            action="power.charge",
            capability_id="power.charge",
            intent_type="control_cmd",
            delivery="data.cmd",
            risk_level="medium",
            owner=owner,
            unsupported_behavior="reply unsupported",
            source_seed="X3-SHEET-ROWS-163-164",
            params=(ParamSpec("operation", "enum", enum=("return_to_charge",)),),
            positive_examples=("回去充电", "去充电"),
            match=lambda text: "充电" in text and not any(word in text for word in _ASK_WORDS),
            extract=lambda text: {"operation": "return_to_charge"},
        ),
    )


class G3IntentEngine:
    def __init__(self, registry: Iterable[RegistryEntry] | None = None) -> None:
        self.registry = tuple(registry or default_registry())

    def handle_text(
        self,
        text: str,
        state: SessionState | None = None,
        *,
        utterance_id: str = "utt-g3-local",
        now_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        state = state or SessionState()
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        intent = self.route(text, state)
        validation = self.validate(intent, state)
        return self.build_outputs(validation, state, utterance_id=utterance_id, now_ms=now_ms)

    def route(self, text: str, state: SessionState | None = None) -> IntentResult:
        state = state or SessionState()
        normalized = _normalize_text(text)
        if state.interaction_mode in {"sleeping", "closed"} or state.engine_gate != "open":
            return IntentResult(
                intent_type="chat",
                confidence=1.0,
                route_reason="engine_gate_closed",
                raw_text=text,
                delivery="cloud_tool + data.reply",
            )

        if _is_confirmation_cancel(normalized):
            return IntentResult(
                intent_type="control_cmd",
                confidence=1.0,
                route_reason="high_risk_cancel",
                raw_text=text,
                delivery="data.cmd after confirmation",
            )
        if _is_confirmation_accept(normalized):
            return IntentResult(
                intent_type="control_cmd",
                confidence=1.0,
                route_reason="high_risk_confirm",
                raw_text=text,
                delivery="data.cmd after confirmation",
            )

        matched = self._matched_entries(normalized)
        control = [entry for entry in matched if entry.intent_type == "control_cmd"]
        if len(control) >= 2 or (len(control) == 1 and _has_multi_marker(normalized)):
            return IntentResult(
                intent_type="control_cmd_multi",
                confidence=0.98,
                route_reason="multi_command_gate",
                raw_text=text,
                delivery="ask_split only",
                detected_actions=tuple(entry.action for entry in control),
                source_seed="X3-SHEET-MULTI",
            )
        if len(control) == 1:
            entry = control[0]
            slots = entry.extract(normalized) if entry.extract else {}
            return IntentResult(
                intent_type="control_cmd",
                confidence=0.92,
                route_reason="registry_exact",
                raw_text=text,
                delivery=entry.delivery,
                candidate_action=entry.action,
                slots=slots,
                needs_validator=True,
                source_seed=entry.source_seed,
            )

        reply_only = [
            entry for entry in matched if entry.intent_type in {"info_query", "knowledge_qa"}
        ]
        if reply_only:
            entry = reply_only[0]
            return IntentResult(
                intent_type=entry.intent_type,
                confidence=0.86,
                route_reason="x3_reply_only",
                raw_text=text,
                delivery=entry.delivery,
                candidate_action=entry.action,
                slots=entry.extract(normalized) if entry.extract else {},
                source_seed=entry.source_seed,
            )

        if _looks_like_knowledge(normalized):
            return IntentResult(
                intent_type="knowledge_qa",
                confidence=0.86,
                route_reason="knowledge_route",
                raw_text=text,
                delivery="cloud_knowledge + data.reply",
            )
        if _looks_like_info(normalized):
            return IntentResult(
                intent_type="info_query",
                confidence=0.83,
                route_reason="cloud_tool_route",
                raw_text=text,
                delivery="cloud_tool + data.reply",
            )
        if _looks_like_incomplete_move(normalized):
            return IntentResult(
                intent_type="control_cmd",
                confidence=0.62,
                route_reason="generic_command_missing_slots",
                raw_text=text,
                delivery="data.cmd",
                candidate_action="navigation.move",
                slots=_extract_move(normalized),
                needs_validator=True,
                source_seed="X3-GENERIC-MOVE",
            )
        return IntentResult(
            intent_type="chat",
            confidence=0.5,
            route_reason="chat_fallback",
            raw_text=text,
            delivery="cloud_tool + data.reply",
        )

    def validate(self, intent: IntentResult, state: SessionState | None = None) -> ValidationResult:
        state = state or SessionState()
        if intent.route_reason == "engine_gate_closed":
            return ValidationResult("reject_policy", intent, reason="engine gate closed")
        if intent.intent_type == "control_cmd_multi":
            return ValidationResult("ask_clarify", intent, reason="multi command blocked")
        if intent.intent_type in {"chat", "info_query", "knowledge_qa"}:
            return ValidationResult(
                "accept", intent, self._entry(intent.candidate_action or ""), "reply only"
            )
        if intent.route_reason == "high_risk_cancel":
            return ValidationResult("cancel", intent, reason="high risk canceled")
        if intent.route_reason == "high_risk_confirm":
            if state.pending_high_risk is None:
                return ValidationResult(
                    "ask_clarify", intent, reason="no pending high risk command"
                )
            pending_action = str(state.pending_high_risk.get("action", ""))
            return ValidationResult("accept", intent, self._entry(pending_action), "confirmed")

        entry = self._entry(intent.candidate_action or "")
        if entry is None:
            return ValidationResult("ask_clarify", intent, reason="unknown action")
        if entry.delivery not in EXECUTABLE_DELIVERIES:
            return ValidationResult("accept", intent, entry, "reply-only delivery")
        if "cmd" not in state.caps:
            return ValidationResult("reject_capability", intent, entry, "cmd cap not granted")

        missing = [
            spec.name for spec in entry.params if spec.required and spec.name not in intent.slots
        ]
        if missing:
            return ValidationResult("ask_missing", intent, entry, f"missing: {','.join(missing)}")
        invalid = _invalid_param_reason(intent.slots, entry)
        if invalid:
            return ValidationResult("reject_policy", intent, entry, invalid)
        if entry.risk_level == "high" and state.pending_high_risk is None:
            return ValidationResult("ask_confirm", intent, entry, "high risk requires confirmation")
        return ValidationResult("accept", intent, entry, "validated")

    def build_outputs(
        self,
        result: ValidationResult,
        state: SessionState | None = None,
        *,
        utterance_id: str,
        now_ms: int,
    ) -> list[dict[str, Any]]:
        state = state or SessionState()
        base = {
            "trace_id": state.trace_id,
            "session_id": state.session_id,
            "utterance_id": utterance_id,
            "ts_ms": now_ms,
        }
        intent = result.intent

        if (
            result.decision == "accept"
            and result.entry
            and result.entry.delivery in EXECUTABLE_DELIVERIES
        ):
            return [
                {
                    "type": "data.cmd",
                    "trace_id": state.trace_id,
                    "session_id": state.session_id,
                    "utterance_id": utterance_id,
                    "cmd_id": _cmd_id(state.trace_id, utterance_id, now_ms),
                    "capability_id": result.entry.capability_id,
                    "action": result.entry.action,
                    "params": dict(intent.slots),
                    "risk_level": result.entry.risk_level,
                    "ack_timeout_ms": 800,
                    "result_timeout_ms": 5000,
                    "issued_at_ms": now_ms,
                }
            ]
        if result.decision == "ask_confirm":
            return [_reply(base, "control_cmd", "这个操作风险较高，请先确认是否执行。", "ack")]
        if result.decision == "ask_missing":
            return [_reply(base, "control_cmd", "我还缺少必要参数，请补充后再说一遍。", "ack")]
        if result.decision == "reject_capability":
            return [_reply(base, "control_cmd", "当前设备没有授权这个控制能力。", "ack")]
        if result.decision == "reject_policy":
            return [_reply(base, "system", "当前状态不允许执行这个请求。", "ack")]
        if result.decision == "cancel":
            return [_reply(base, "control_cmd", "已取消，不会下发控制指令。", "ack")]
        if intent.intent_type == "control_cmd_multi":
            return [
                _reply(
                    base,
                    "control_cmd",
                    "我听到了多个操作，请拆成两句，或告诉我先执行哪一个。",
                    "ack",
                )
            ]
        if intent.intent_type == "knowledge_qa":
            return [_reply(base, "knowledge_qa", _rag_answer(intent.raw_text), "final_only")]
        if intent.intent_type == "info_query":
            return [
                _reply(
                    base,
                    "info_query",
                    _info_answer(intent.raw_text, intent.slots),
                    "final_only",
                )
            ]
        return [
            _reply(base, "chat", "我可以聊天，也可以在授权范围内处理单条控制指令。", "final_only")
        ]

    def function_call_output(
        self, name: str, payload: dict[str, Any], state: SessionState | None = None
    ) -> ValidationResult:
        """Validate an internal function-call result before any WSS frame is built."""

        state = state or SessionState()
        if name in {"execute_command", "send_data_cmd", "set_robot_state"}:
            intent = IntentResult(
                intent_type="control_cmd",
                confidence=0.0,
                route_reason="forbidden_function_call",
                raw_text=str(payload),
            )
            return ValidationResult("reject_policy", intent, reason="function cannot send command")
        if name == "parse_command_slots":
            intent = IntentResult(
                intent_type="control_cmd",
                confidence=float(payload.get("confidence", 0.0)),
                route_reason="function_parse_command_slots",
                raw_text=str(payload.get("raw_text", "")),
                delivery=str(payload.get("delivery", "data.cmd")),  # type: ignore[arg-type]
                candidate_action=str(payload.get("action", "")),
                slots=dict(payload.get("slots", {})),
                needs_validator=True,
                source_seed=str(payload.get("source_seed", "")),
            )
            return self.validate(intent, state)
        intent = IntentResult(
            intent_type="chat",
            confidence=0.0,
            route_reason="reply_only_function_call",
            raw_text=str(payload),
            delivery="cloud_tool + data.reply",
        )
        return ValidationResult("accept", intent, reason="reply-only function")

    def _entry(self, action: str) -> RegistryEntry | None:
        return next((entry for entry in self.registry if entry.action == action), None)

    def _matched_entries(self, normalized: str) -> list[RegistryEntry]:
        return [
            entry
            for entry in self.registry
            if (entry.match and entry.match(normalized))
            or any(_normalize_text(example) in normalized for example in entry.positive_examples)
        ]


def _match_move(text: str) -> bool:
    if _is_question(text):
        return False
    if "再走" in text:
        return _parse_distance_cm(text) is not None or "一点" in text
    return any(
        k in text
        for k in (
            "往前走",
            "前进",
            "向前走",
            "往后走",
            "后退",
            "往左走",
            "向左走",
            "往右走",
            "向右走",
        )
    )


def _extract_move(text: str) -> dict[str, Any]:
    slots: dict[str, Any] = {}
    if any(k in text for k in ("往前", "向前", "前进")) or "再走" in text:
        slots["direction"] = "forward"
    elif any(k in text for k in ("往后", "向后", "后退")):
        slots["direction"] = "backward"
    elif any(k in text for k in ("往左走", "向左走", "左移")):
        slots["direction"] = "left"
    elif any(k in text for k in ("往右走", "向右走", "右移")):
        slots["direction"] = "right"
    distance = _parse_distance_cm(text)
    if distance is not None:
        slots["distance_cm"] = distance
    elif "一点" in text:
        slots["distance_cm"] = 10
        slots["distance_hint"] = "a_little"
    elif "direction" in slots:
        slots["distance_cm"] = _STEP_DISTANCE_CM
        slots["distance_hint"] = "default_step"
    return slots


def _match_turn(text: str) -> bool:
    if _is_question(text):
        return False
    return (
        "转身" in text or "向后转" in text or "往左转" in text or "往右转" in text or "再转" in text
    )


def _extract_turn(text: str) -> dict[str, Any]:
    if "向后转" in text or "后转" in text:
        direction = "back"
    elif "右" in text:
        direction = "right"
    elif "左" in text:
        direction = "left"
    else:
        direction = "back"
    slots: dict[str, Any] = {"direction": direction}
    angle = _parse_angle_deg(text)
    if angle is not None:
        slots["angle_deg"] = angle
    elif "一点" in text:
        slots["angle_deg"] = 15
        slots["angle_hint"] = "a_little"
    return slots


def _match_head(text: str) -> bool:
    if _is_question(text):
        return False
    return (
        "摇头" in text
        or "摇摇头" in text
        or "点头" in text
        or any(
            token in text
            for token in (
                "往左边看",
                "往右边看",
                "往前边看",
                "往后边看",
                "往左看",
                "往右看",
                "往前看",
                "往后看",
            )
        )
    )


def _extract_head(text: str) -> dict[str, Any]:
    if "摇" in text and "头" in text:
        return {"gesture": "shake"}
    if "点" in text and "头" in text:
        return {"gesture": "nod"}
    direction = "front"
    if "左" in text:
        direction = "left"
    elif "右" in text:
        direction = "right"
    elif "后" in text:
        direction = "back"
    return {"gesture": "look", "direction": direction}


def _match_eye(text: str) -> bool:
    if _is_question(text):
        return False
    return "眼" in text and any(
        token in text
        for token in ("闭", "睁", "眯", "瞪", "眨", "放电", "翻白眼", "转转", "抛媚眼")
    )


def _extract_eye(text: str) -> dict[str, Any]:
    operation = "blink"
    if "闭" in text:
        operation = "close"
    elif "睁" in text:
        operation = "open"
    elif "眯" in text:
        operation = "squint"
    elif "瞪" in text:
        operation = "wide_open"
    elif "放电" in text:
        operation = "wink_light"
    elif "翻白眼" in text:
        operation = "roll"
    elif "转转" in text:
        operation = "rotate"
    elif "抛媚眼" in text:
        operation = "wink"
    return {"operation": operation, "side": _side(text, default="both")}


def _match_eyebrow(text: str) -> bool:
    if _is_question(text):
        return False
    return "眉" in text and ("挑" in text or "皱" in text)


def _extract_eyebrow(text: str) -> dict[str, Any]:
    return {
        "operation": "frown" if "皱" in text else "raise",
        "side": _side(text, default="both"),
    }


_GESTURES: tuple[tuple[str, str], ...] = (
    ("打招呼", "greet"),
    ("招手", "wave"),
    ("挥手", "wave"),
    ("动动手", "hand_move"),
    ("动手", "hand_move"),
    ("举手", "raise_hand"),
    ("握手", "handshake"),
    ("敬礼", "salute"),
    ("跑步", "run"),
    ("站起", "stand_up"),
    ("蹲下", "squat"),
    ("坐下", "sit_down"),
    ("上下台阶", "stairs"),
    ("上下坡道", "slope"),
    ("摔倒爬起", "fall_recover"),
    ("躺下", "lie_down"),
    ("拍照", "photo_pose"),
    ("跳舞", "dance"),
    ("打太极", "tai_chi"),
    ("做个动作", "random_action"),
    ("大哭", "cry_loudly"),
    ("哭", "cry"),
    ("大笑", "laugh_loudly"),
    ("微笑", "smile"),
    ("奸笑", "smirk"),
    ("笑", "laugh"),
    ("点赞", "thumbs_up"),
    ("碰拳", "fist_bump"),
    ("比心", "heart"),
    ("比耶", "victory"),
    ("拿宣传册", "fetch_brochure"),
    ("拿饮料", "fetch_drink"),
    ("拿水", "fetch_water"),
    ("迎宾欢迎", "welcome_guest"),
    ("开启导览", "start_guide"),
    ("拍照区带领", "lead_photo_area"),
    ("回到原点", "return_origin"),
    ("告别", "farewell"),
)


def _match_gesture(text: str) -> bool:
    if _is_question(text):
        return False
    return (
        any(keyword in text for keyword, _ in _GESTURES) or re.search(r"拿.+水", text) is not None
    )


def _extract_gesture(text: str) -> dict[str, Any]:
    if re.search(r"拿.+水", text):
        slots: dict[str, Any] = {"gesture": "fetch_water"}
        count = _parse_count_before(text, "水")
        if count is not None:
            slots["count"] = count
        return slots
    for keyword, gesture in _GESTURES:
        if keyword in text:
            slots: dict[str, Any] = {"gesture": gesture}
            side = _side(text, default="")
            if side:
                slots["side"] = side
            count = _parse_count_before(text, keyword)
            if count is not None:
                slots["count"] = count
            return slots
    return {}


def _match_tour(text: str) -> bool:
    if _is_question(text):
        return False
    return any(
        token in text
        for token in (
            "讲解",
            "导览",
            "上一个点",
            "下一个点",
            "跳过",
            "第",
            "英文",
            "中文",
            "日文",
            "韩文",
            "自动模式",
            "手动模式",
            "播报模式",
            "播报风格",
            "tts",
        )
    )


def _extract_tour(text: str) -> dict[str, Any]:
    slots: dict[str, Any] = {"operation": "configure"}
    language = _extract_language(text)
    if language:
        slots["operation"] = "set_language"
        slots["language"] = language
    if "开始讲解" in text or "开启导览" in text:
        slots["operation"] = "start"
    elif "暂停讲解" in text:
        slots["operation"] = "pause"
    elif "继续讲解" in text or "继续导览" in text:
        slots["operation"] = "resume"
    elif "结束讲解" in text or "停止讲解" in text:
        slots["operation"] = "stop"
    elif "上一个点" in text:
        slots["operation"] = "previous_point"
    elif "下一个点" in text:
        slots["operation"] = "next_point"
    elif "跳过" in text:
        slots["operation"] = "skip_point"
    elif "自动模式" in text:
        slots["operation"] = "set_mode"
        slots["mode"] = "auto"
    elif "手动模式" in text:
        slots["operation"] = "set_mode"
        slots["mode"] = "manual"
    point_index = _parse_point_index(text)
    if point_index is not None:
        slots["operation"] = "goto_point"
        slots["point_index"] = point_index
    return slots


def _match_volume(text: str) -> bool:
    return ("音量" in text or "声音" in text) and any(
        token in text for token in ("调", "大", "小", "最大", "最小", "%", "百分")
    )


def _extract_volume(text: str) -> dict[str, Any]:
    percent = _parse_percent(text)
    if percent is not None:
        return {"operation": "set", "volume_percent": percent}
    if "最大" in text:
        return {"operation": "max", "volume_percent": 100}
    if "最小" in text:
        return {"operation": "min", "volume_percent": 0}
    if "大" in text or "高" in text:
        return {"operation": "increase"}
    if "小" in text or "低" in text:
        return {"operation": "decrease"}
    return {"operation": "set"}


def _match_connection(text: str) -> bool:
    if _is_question(text):
        return False
    return ("wifi" in text or "wi-fi" in text or "蓝牙" in text) and (
        "打开" in text or "开启" in text or "关闭" in text or "关掉" in text
    )


def _extract_connection(text: str) -> dict[str, Any]:
    return {
        "target": "bluetooth" if "蓝牙" in text else "wifi",
        "enabled": not ("关闭" in text or "关掉" in text),
    }


def _reply(base: dict[str, Any], intent_type: str, text: str, speak_policy: str) -> dict[str, Any]:
    return {
        "type": "data.reply",
        **base,
        "intent_type": intent_type,
        "text": text,
        "speak_policy": speak_policy,
    }


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", text.strip().lower())


def _is_question(text: str) -> bool:
    return any(word in text for word in _ASK_WORDS) or text.endswith(("吗", "呢", "？", "?"))


def _parse_distance_cm(text: str) -> int | None:
    match = re.search(
        r"([0-9]+(?:\.[0-9]+)?|[一二两三四五六七八九十百千万半点]+)(米|厘米|公分|cm|m)", text
    )
    if not match:
        step_match = re.search(r"([0-9]+|[一二两三四五六七八九十百]+)步", text)
        if not step_match:
            return None
        value = _parse_number(step_match.group(1))
        if value is None:
            return None
        return max(1, int(round(value * _STEP_DISTANCE_CM)))
    value = _parse_number(match.group(1))
    if value is None:
        return None
    unit = match.group(2)
    cm = value * 100 if unit in {"米", "m"} else value
    return max(1, int(round(cm)))


def _parse_angle_deg(text: str) -> int | None:
    match = re.search(r"([0-9]+|[一二两三四五六七八九十百]+)度", text)
    if not match:
        return None
    value = _parse_number(match.group(1))
    return int(value) if value is not None else None


def _parse_percent(text: str) -> int | None:
    match = re.search(r"([0-9]+|[一二两三四五六七八九十百]+)\s*(?:%|百分之)", text)
    if not match:
        match = re.search(r"(?:调到|调至|设置为)([0-9]+|[一二两三四五六七八九十百]+)", text)
    if not match:
        return None
    value = _parse_number(match.group(1))
    return int(value) if value is not None else None


def _parse_count_before(text: str, keyword: str) -> int | None:
    index = text.find(keyword)
    if index <= 0:
        return None
    prefix = text[max(0, index - 5) : index]
    match = re.search(r"([0-9]+|[一二两三四五六七八九十百]+)(?:个|瓶|次|张)?$", prefix)
    if not match:
        return None
    value = _parse_number(match.group(1))
    return int(value) if value is not None else None


def _parse_point_index(text: str) -> int | None:
    match = re.search(r"第([0-9]+|[一二两三四五六七八九十百]+)(?:个)?(?:讲解)?点", text)
    if not match:
        return None
    value = _parse_number(match.group(1))
    return int(value) if value is not None else None


def _parse_number(raw: str) -> float | None:
    raw = raw.strip()
    if not raw:
        return None
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", raw):
        return float(raw)
    if raw == "半":
        return 0.5
    table = {
        "零": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    if raw in table:
        return float(table[raw])
    if raw == "十":
        return 10.0
    if "百" in raw:
        left, _, right = raw.partition("百")
        base = table.get(left, 1) * 100
        return float(base + int(_parse_number(right) or 0))
    if "十" in raw:
        left, _, right = raw.partition("十")
        tens = table.get(left, 1) * 10 if left else 10
        ones = table.get(right, 0) if right else 0
        return float(tens + ones)
    return None


def _side(text: str, *, default: str) -> str:
    if "左" in text:
        return "left"
    if "右" in text:
        return "right"
    if "双" in text or "两" in text:
        return "both"
    return default


def _extract_language(text: str) -> str:
    if "英文" in text or "英语" in text:
        return "en"
    if "中文" in text or "汉语" in text:
        return "zh"
    if "日文" in text or "日语" in text:
        return "ja"
    if "韩文" in text or "韩语" in text:
        return "ko"
    return ""


def _invalid_param_reason(slots: dict[str, Any], entry: RegistryEntry) -> str:
    for spec in entry.params:
        if spec.name not in slots:
            continue
        value = slots[spec.name]
        if spec.type == "enum" and value not in spec.enum:
            return f"{spec.name} enum invalid"
        if spec.type == "bool" and not isinstance(value, bool):
            return f"{spec.name} type invalid"
        if spec.type == "str" and not isinstance(value, str):
            return f"{spec.name} type invalid"
        if spec.type == "int":
            if not isinstance(value, int):
                return f"{spec.name} type invalid"
            if spec.minimum is not None and value < spec.minimum:
                return f"{spec.name} lower than minimum"
            if spec.maximum is not None and value > spec.maximum:
                return f"{spec.name} greater than maximum"
    return ""


def _has_multi_marker(text: str) -> bool:
    return any(marker in text for marker in _MULTI_MARKERS)


def _looks_like_incomplete_move(text: str) -> bool:
    return any(
        k in text
        for k in ("移动", "走一下", "往前走", "往后走", "往左走", "往右走", "前进", "后退")
    )


def _looks_like_knowledge(text: str) -> bool:
    return any(
        k in text
        for k in (
            "什么是",
            "介绍",
            "知识",
            "为什么",
            "怎么理解",
            "企业",
            "公司",
            "商品",
            "产品",
            "参数",
            "价格",
            "地址",
        )
    )


def _looks_like_info(text: str) -> bool:
    return any(
        k in text
        for k in (
            "状态",
            "能力",
            "配置",
            "还能做什么",
            "天气",
            "时间",
            "日历",
            "单位换算",
            "百科",
            "健康",
            "菜谱",
        )
    )


def _is_confirmation_accept(text: str) -> bool:
    return any(token in text for token in _AFFIRMATIVE_CONFIRM)


def _is_confirmation_cancel(text: str) -> bool:
    return any(token in text for token in _CANCEL_CONFIRM)


def _rag_answer(text: str) -> str:
    if "全双工" in text:
        return "全双工表示听和说可以并行处理，但知识问答路径只返回回答，不触发端侧执行。"
    if "小歌" in text:
        return "小歌是当前语音交互助手；知识查询只走 data.reply，不生成控制命令。"
    if "不存在" in text or "这个" in text:
        return "暂未找到高置信度知识结果，请换个问法。"
    return "这个问题属于知识库或商品信息查询，我会走 data.reply 返回答案，不生成控制命令。"


def _info_answer(text: str, slots: dict[str, Any]) -> str:
    if slots.get("topic") == "battery" or "电量" in text or "多少电" in text:
        return "电量查询属于状态信息路径，只返回 data.reply，不下发控制指令。"
    return "当前云侧链路正常，真实机器人动作保持关闭。"


def _cmd_id(trace_id: str, utterance_id: str, now_ms: int) -> str:
    suffix = abs(hash((trace_id, utterance_id, now_ms))) % 1_000_000
    return f"cmd-g3-{suffix:06d}"
