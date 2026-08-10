from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
import aiohttp.web

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_ROOT = (
    REPO_ROOT / "docs" / "g1_contract_signoff_package_r21_r5_2_2_20260804" / "02_contracts"
)
MANIFEST_SHA256 = "845F0F4125061FF37A7F4DA20E0C88BC089200A08B319F1035D6522C80B56559"
PROTOCOL_SCHEMA_PATH = CONTRACT_ROOT / "xiaoge-duplex-protocol-r5.2.2.schema.json"
REGISTRY_SCHEMA_PATH = CONTRACT_ROOT / "xiaoge-duplex-voicecmd-registry-r5.2.2.schema.json"
EXAMPLES_PATH = CONTRACT_ROOT / "xiaoge-duplex-protocol-r5.2.2.examples.jsonl"
CLOSE_CODES_PATH = CONTRACT_ROOT / "xiaoge-duplex-protocol-r5.2.2.close-codes.jsonl"
MANIFEST_PATH = CONTRACT_ROOT / "xiaoge-duplex-protocol-r5.2.2.manifest.json"


class SchemaValidationError(AssertionError):
    pass


class MiniJsonSchema:
    """Small validator for the exact JSON Schema keywords used by the R5.2.2 contracts."""

    def __init__(self, schema: dict[str, Any]) -> None:
        self.schema = schema

    def is_valid_ref(self, schema_ref: str, payload: Any) -> bool:
        return not self.errors(schema_ref, payload)

    def assert_valid_ref(self, schema_ref: str, payload: Any) -> None:
        errors = self.errors(schema_ref, payload)
        if errors:
            raise SchemaValidationError("; ".join(errors[:5]))

    def errors(self, schema_ref: str, payload: Any) -> list[str]:
        return self._validate(self._resolve_ref(schema_ref), payload, schema_ref)

    def _resolve_ref(self, ref: str) -> dict[str, Any]:
        if not ref.startswith("#/"):
            raise ValueError(f"unsupported schema ref: {ref}")
        current: Any = self.schema
        for part in ref[2:].split("/"):
            current = current[part]
        if not isinstance(current, dict):
            raise ValueError(f"schema ref does not resolve to object: {ref}")
        return current

    def _validate(self, schema: dict[str, Any], value: Any, path: str) -> list[str]:
        if "$ref" in schema:
            return self._validate(self._resolve_ref(str(schema["$ref"])), value, path)

        errors: list[str] = []

        if "oneOf" in schema:
            matches = [
                option
                for option in schema["oneOf"]
                if not self._validate(option, value, f"{path}.oneOf")
            ]
            if len(matches) != 1:
                errors.append(f"{path}: expected exactly one oneOf match, got {len(matches)}")
            return errors

        if "const" in schema and value != schema["const"]:
            errors.append(f"{path}: expected const {schema['const']!r}, got {value!r}")

        if "enum" in schema and value not in schema["enum"]:
            errors.append(f"{path}: expected one of {schema['enum']!r}, got {value!r}")

        expected_type = schema.get("type")
        if expected_type is not None and not self._type_matches(str(expected_type), value):
            errors.append(f"{path}: expected type {expected_type}, got {type(value).__name__}")
            return errors

        if isinstance(value, str):
            min_length = schema.get("minLength")
            if isinstance(min_length, int) and len(value) < min_length:
                errors.append(f"{path}: string shorter than minLength {min_length}")
            pattern = schema.get("pattern")
            if isinstance(pattern, str) and re.search(pattern, value) is None:
                errors.append(f"{path}: string does not match pattern {pattern!r}")

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            minimum = schema.get("minimum")
            if isinstance(minimum, (int, float)) and value < minimum:
                errors.append(f"{path}: number lower than minimum {minimum}")
            maximum = schema.get("maximum")
            if isinstance(maximum, (int, float)) and value > maximum:
                errors.append(f"{path}: number greater than maximum {maximum}")

        if isinstance(value, list):
            min_items = schema.get("minItems")
            if isinstance(min_items, int) and len(value) < min_items:
                errors.append(f"{path}: array shorter than minItems {min_items}")
            if schema.get("uniqueItems") is True and len(_stable_items(value)) != len(value):
                errors.append(f"{path}: array items are not unique")
            item_schema = schema.get("items")
            if isinstance(item_schema, dict):
                for index, item in enumerate(value):
                    errors.extend(self._validate(item_schema, item, f"{path}[{index}]"))

        if isinstance(value, dict):
            required = schema.get("required", [])
            if isinstance(required, list):
                for key in required:
                    if key not in value:
                        errors.append(f"{path}: missing required property {key!r}")

            properties = schema.get("properties", {})
            if isinstance(properties, dict):
                for key, property_schema in properties.items():
                    if key in value and isinstance(property_schema, dict):
                        errors.extend(self._validate(property_schema, value[key], f"{path}.{key}"))

            additional = schema.get("additionalProperties", True)
            if additional is False and isinstance(properties, dict):
                extra = sorted(set(value) - set(properties))
                if extra:
                    errors.append(f"{path}: additional properties are not allowed: {extra!r}")

        return errors

    @staticmethod
    def _type_matches(expected_type: str, value: Any) -> bool:
        if expected_type == "object":
            return isinstance(value, dict)
        if expected_type == "array":
            return isinstance(value, list)
        if expected_type == "string":
            return isinstance(value, str)
        if expected_type == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if expected_type == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if expected_type == "boolean":
            return isinstance(value, bool)
        raise ValueError(f"unsupported schema type: {expected_type}")


def _stable_items(values: list[Any]) -> set[str]:
    return {json.dumps(value, ensure_ascii=False, sort_keys=True) for value in values}


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise TypeError(f"expected JSON object in {path}")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise TypeError(f"expected JSON object line in {path}")
            rows.append(value)
    return rows


def compact_json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def json_frame_size(payload: Any) -> int:
    return len(compact_json_bytes(payload))


def make_reply_with_size(target_size: int) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "data.reply",
        "trace_id": "trace-size",
        "session_id": "sess-size",
        "utterance_id": "utt-size",
        "intent_type": "chat",
        "text": "",
        "ts_ms": 1789000000700,
    }
    base_size = json_frame_size(payload)
    text_delta = target_size - base_size
    if text_delta < 0:
        raise ValueError(f"target size {target_size} is smaller than base frame {base_size}")
    payload["text"] = "x" * text_delta
    actual_size = json_frame_size(payload)
    if actual_size != target_size:
        raise AssertionError(f"failed to build {target_size} byte frame, got {actual_size}")
    return payload


@dataclass
class ReplayResult:
    schema_pass: bool
    semantic_pass: bool | None = None
    transport_pass: bool | None = None
    output_types: list[str] = field(default_factory=list)
    generated_cmd_ids: list[str] = field(default_factory=list)
    side_effects: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FrontendStateDecision:
    accepted: bool
    handling: str
    reason: str


@dataclass(frozen=True)
class PcmGateDecision:
    accepted: bool
    enters_asr: bool
    enters_llm: bool
    enters_context: bool
    reason: str


class CloudContractReplayer:
    def __init__(self, protocol_schema: dict[str, Any]) -> None:
        self.protocol_schema = protocol_schema
        self.validator = MiniJsonSchema(protocol_schema)
        self.frame_json_max_bytes = int(protocol_schema["x-frame-json-max-bytes"])

    def replay_example(self, example: dict[str, Any]) -> ReplayResult:
        payload = example["payload"]
        schema_pass = self.validator.is_valid_ref(str(example["schema_ref"]), payload)
        result = ReplayResult(schema_pass=schema_pass, output_types=[payload.get("type", "")])

        context = example.get("context")
        expect = example.get("expect")
        if not isinstance(expect, dict):
            return result

        if isinstance(context, dict) and "serialized_bytes" in context:
            result.transport_pass = int(context["serialized_bytes"]) <= self.frame_json_max_bytes
        else:
            result.transport_pass = json_frame_size(payload) <= self.frame_json_max_bytes

        result.semantic_pass = self._semantic_accepts(payload, context)
        if expect.get("contract") == "multi_command_blocked_ask_split_only":
            self._assert_multi_command_blocked_contract(example, result)
        return result

    def _semantic_accepts(self, payload: dict[str, Any], context: Any) -> bool:
        if not isinstance(context, dict):
            return True

        cmd_id = payload.get("cmd_id")
        known_cmd_ids = context.get("known_cmd_ids")
        if isinstance(known_cmd_ids, list) and cmd_id not in known_cmd_ids:
            return False

        seen_ack_cmd_ids = context.get("seen_ack_cmd_ids")
        if isinstance(seen_ack_cmd_ids, list) and cmd_id in seen_ack_cmd_ids:
            return False

        return True

    @staticmethod
    def _assert_multi_command_blocked_contract(
        example: dict[str, Any], result: ReplayResult
    ) -> None:
        payload = example["payload"]
        context = example["context"]
        expect = example["expect"]
        assert context["intent_type"] == "control_cmd_multi"
        assert context["state"] == "multi_command_blocked"
        assert context["reply_style"] == "ask_split"
        assert payload["type"] == "data.reply"
        assert result.output_types == expect["output_types"] == ["data.reply"]
        assert "data.cmd" not in result.output_types
        assert "cmd_id" not in payload
        assert result.generated_cmd_ids == []
        assert result.side_effects == []


def evaluate_frontend_state(
    payload: dict[str, Any],
    *,
    last_seq: int | None,
    now_ms: int,
    validator: MiniJsonSchema,
) -> FrontendStateDecision:
    if not validator.is_valid_ref("#/$defs/ctrlFrontendState", payload):
        return FrontendStateDecision(False, "reject", "schema_invalid")
    seq = int(payload["seq"])
    if last_seq is not None and seq <= last_seq:
        return FrontendStateDecision(False, "audit_drop", "seq_not_increasing")
    expires_at_ms = int(payload["ts_ms"]) + int(payload["ttl_ms"])
    if now_ms > expires_at_ms:
        return FrontendStateDecision(False, "expire_drop", "ttl_expired")

    trust_level = payload["trust_level"]
    if trust_level == "authoritative":
        return FrontendStateDecision(True, "authoritative_state", "trusted_for_state_gate")
    if trust_level == "hint":
        return FrontendStateDecision(True, "hint_only", "not_authorizing_critical_transition")
    if trust_level == "observe":
        return FrontendStateDecision(True, "observation_only", "not_authorizing_actions")
    return FrontendStateDecision(False, "reject", "unknown_trust_level")


def evaluate_pcm_gate(*, interaction_mode: str, engine_gate: str) -> PcmGateDecision:
    if interaction_mode == "sleeping" or engine_gate == "closed":
        return PcmGateDecision(
            accepted=False,
            enters_asr=False,
            enters_llm=False,
            enters_context=False,
            reason="sleeping_or_engine_gate_closed",
        )
    return PcmGateDecision(
        accepted=True,
        enters_asr=True,
        enters_llm=True,
        enters_context=True,
        reason="engine_gate_open",
    )


def build_high_risk_confirmation_outputs(
    *, state: str = "awaiting_confirmation"
) -> list[dict[str, Any]]:
    base = {
        "trace_id": "trace-g2-high-risk",
        "session_id": "sess-g2-0001",
        "utterance_id": "utt-g2-high-risk",
    }
    if state == "awaiting_confirmation":
        return [
            {
                "type": "data.reply",
                **base,
                "intent_type": "control_cmd",
                "text": "这是高危操作，请确认是否继续。",
                "ts_ms": 1789000000500,
                "speak_policy": "ack",
            }
        ]
    if state == "canceled":
        return [
            {
                "type": "data.reply",
                **base,
                "intent_type": "control_cmd",
                "text": "已取消该高危操作。",
                "ts_ms": 1789000000600,
                "speak_policy": "ack",
            }
        ]
    if state == "timeout":
        return [
            {
                "type": "data.reply",
                **base,
                "intent_type": "control_cmd",
                "text": "确认超时，已取消该高危操作。",
                "ts_ms": 1789000030500,
                "speak_policy": "ack",
            }
        ]
    if state != "confirmed":
        raise ValueError(f"unsupported high-risk confirmation state: {state}")
    return [
        {
            "type": "data.cmd",
            **base,
            "cmd_id": "cmd-g2-high-risk-0001",
            "capability_id": "system.power",
            "action": "system.reboot",
            "params": {},
            "risk_level": "high",
            "ack_timeout_ms": 800,
            "result_timeout_ms": 5000,
            "issued_at_ms": 1789000001000,
        }
    ]


@dataclass
class CloudFakeState:
    issued_tokens: dict[str, str] = field(default_factory=dict)
    denied_tokens: set[str] = field(default_factory=set)
    expired_tokens: set[str] = field(default_factory=set)
    active_sessions: set[str] = field(default_factory=set)
    known_cmd_ids: set[str] = field(default_factory=set)
    events: list[dict[str, Any]] = field(default_factory=list)
    next_session: int = 1
    force_busy: bool = False


def make_cloud_fake_app(state: CloudFakeState) -> aiohttp.web.Application:
    protocol = load_json(PROTOCOL_SCHEMA_PATH)
    validator = MiniJsonSchema(protocol)
    frame_json_max_bytes = int(protocol["x-frame-json-max-bytes"])

    async def create_session(request: aiohttp.web.Request) -> aiohttp.web.Response:
        body = await request.json()
        if not validator.is_valid_ref("#/$defs/createSessionRequest", body):
            return aiohttp.web.json_response({"code": "protocol_error"}, status=400)
        credential = body.get("credential")
        if credential == "bad" or (
            isinstance(credential, dict) and credential.get("key_id") == "bad"
        ):
            return aiohttp.web.json_response({"code": "auth_failed"}, status=401)
        if "cmd" not in body.get("caps", []):
            return aiohttp.web.json_response({"code": "permission_denied"}, status=403)
        if state.force_busy:
            return aiohttp.web.json_response({"code": "resource_exhausted"}, status=503)

        session_id = f"sess-g2-{state.next_session:04d}"
        state.next_session += 1
        access_token = f"tok-{session_id}"
        state.issued_tokens[access_token] = session_id
        response = {
            "type": "session.created",
            "trace_id": "trace-g2-0001",
            "session_id": session_id,
            "access_token": access_token,
            "expires_in_ms": 600000,
            "ws_url": f"ws://127.0.0.1:{request.url.port}/ws/session",
            "granted_caps": body["caps"],
            "config_snapshot": {"config_version": "cfg-g2-0001"},
        }
        validator.assert_valid_ref("#/$defs/createSessionResponse", response)
        return aiohttp.web.json_response(response)

    async def ws_session(request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
        ws = aiohttp.web.WebSocketResponse()
        await ws.prepare(request)
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            await ws.close(code=4401, message=b"auth_failed")
            return ws
        token = auth.removeprefix("Bearer ")
        session_id = state.issued_tokens.get(token)
        if session_id is None:
            await ws.close(code=4401, message=b"auth_failed")
            return ws
        if token in state.expired_tokens:
            await ws.close(code=4401, message=b"token_expired")
            return ws
        if token in state.denied_tokens:
            await ws.close(code=4403, message=b"permission_denied")
            return ws
        if session_id in state.active_sessions:
            await ws.close(code=4009, message=b"duplicate_connection")
            return ws

        state.active_sessions.add(session_id)
        try:
            async for message in ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                if len(message.data.encode("utf-8")) > frame_json_max_bytes:
                    await ws.close(code=4400, message=b"protocol_error")
                    break
                try:
                    payload = json.loads(message.data)
                except json.JSONDecodeError:
                    await ws.close(code=4400, message=b"protocol_error")
                    break
                if not validator.is_valid_ref("#/$defs/p0Message", payload):
                    await ws.close(code=4400, message=b"protocol_error")
                    break
                state.events.append(payload)
                if payload["type"] == "ctrl.hello":
                    ready = {
                        "type": "ctrl.ready",
                        "trace_id": payload["trace_id"],
                        "session_id": payload["session_id"],
                        "sample_rate": 16000,
                        "granted_caps": payload["caps"],
                        "config_version": "cfg-g2-0001",
                    }
                    await ws.send_str(json.dumps(ready, ensure_ascii=False))
                    cmd = {
                        "type": "data.cmd",
                        "trace_id": payload["trace_id"],
                        "session_id": payload["session_id"],
                        "utterance_id": "utt-g2-0001",
                        "cmd_id": "cmd-g2-0001",
                        "capability_id": "motion.move",
                        "action": "navigation.move",
                        "params": {"direction": "forward", "distance_cm": 100},
                        "risk_level": "medium",
                        "ack_timeout_ms": 800,
                        "result_timeout_ms": 5000,
                        "issued_at_ms": 1789000001000,
                    }
                    validator.assert_valid_ref("#/$defs/dataCmd", cmd)
                    state.known_cmd_ids.add(cmd["cmd_id"])
                    await ws.send_str(json.dumps(cmd, ensure_ascii=False))
        finally:
            state.active_sessions.discard(session_id)
        return ws

    app = aiohttp.web.Application()
    app.router.add_post("/create_session", create_session)
    app.router.add_get("/ws/session", ws_session)
    return app
