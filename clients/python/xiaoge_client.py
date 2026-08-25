"""Xiaoge R5.2.2 full-duplex client SDK.

Main path:
  HTTPS create_session -> WSS /ws/session + Authorization Bearer -> ctrl.hello
  -> ctrl/data JSON frames + binary PCM.

The SDK never connects to a real robot action module. data.cmd is delivered to
the application through on_command; applications should explicitly send cmd_ack and
cmd_result when they choose to execute or reject a command.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import logging
import os
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlparse

import websockets

logger = logging.getLogger("xiaoge.client")

SAMPLE_RATE = 16_000
NUM_CHANNELS = 1
SAMPLE_WIDTH = 2
SAMPLE_FORMAT = "int16le"
PROTO_VERSION = 2
JSON_TEXT_FRAME_MAX_BYTES = 8192
BINARY_FRAME_MAX_BYTES = 32768
WSS_SESSION_PATH = "/ws/session"
CONTRACT_VERSION = "xiaoge-duplex-protocol-r5.2.2"
MANIFEST_SHA256 = "845F0F4125061FF37A7F4DA20E0C88BC089200A08B319F1035D6522C80B56559"
BUNDLED_CA_CERT = Path(__file__).resolve().parents[1] / "certs" / "cloud-ca.pem"
CLOUD_API_KEY = os.getenv("XIAOGE_CLOUD_API_KEY", "")
VALID_CAPS = {"audio", "text", "cmd", "state"}
VALID_TRUST_LEVELS = {"authoritative", "hint", "observe"}
VALID_INTENT_TYPES = frozenset({"control_cmd", "info_query", "knowledge_qa", "chat", "config", "system"})
VALID_SPEAK_POLICIES = frozenset({"silent", "ack", "ack_then_result", "final_only"})
VALID_RISK_LEVELS = frozenset({"low", "medium", "high"})
VALID_ERROR_CODES = frozenset(
    {
        "auth_failed",
        "permission_denied",
        "busy",
        "protocol_error",
        "capability_unsupported",
        "token_expired",
        "duplicate_connection",
        "resource_exhausted",
        "unknown_cmd_id",
    }
)
VALID_CLEAR_REASONS = frozenset({"barge_in", "user_stop", "system_cancel", "sleep"})
VALID_LINK_STATES = frozenset({"connecting", "connected", "reconnecting", "closed"})
VALID_INTERACTION_MODES = frozenset({"sleeping", "dialogue", "listening"})
VALID_ENGINE_GATES = frozenset({"closed", "open", "kws_only"})
VALID_RESOURCE_STATES = frozenset(
    {"SleepingHot", "SleepingWarm", "ActiveAgent", "ReleasedIdle", "PendingReconnect"}
)
EXECUTABLE_DELIVERIES = {"data.cmd", "data.cmd after confirmation"}
COMMAND_MAX_AGE_GRACE_MS = 1000

CMD_ACK_STATUS_ACCEPTED = "accepted"
CMD_ACK_STATUS_REJECTED = "rejected"
CMD_ACK_STATUS_DUPLICATE = "duplicate"
CMD_RESULT_STATUS_RUNNING = "running"
CMD_RESULT_STATUS_SUCCEEDED = "succeeded"
CMD_RESULT_STATUS_FAILED = "failed"
CMD_RESULT_STATUS_CANCELED = "canceled"
CMD_RESULT_STATUS_TIMEOUT = "timeout"
VALID_CMD_ACK_STATUSES = frozenset(
    {CMD_ACK_STATUS_ACCEPTED, CMD_ACK_STATUS_REJECTED, CMD_ACK_STATUS_DUPLICATE}
)
VALID_CMD_RESULT_STATUSES = frozenset(
    {
        CMD_RESULT_STATUS_RUNNING,
        CMD_RESULT_STATUS_SUCCEEDED,
        CMD_RESULT_STATUS_FAILED,
        CMD_RESULT_STATUS_CANCELED,
        CMD_RESULT_STATUS_TIMEOUT,
    }
)


class CmdAckStatus(str, Enum):
    ACCEPTED = CMD_ACK_STATUS_ACCEPTED
    REJECTED = CMD_ACK_STATUS_REJECTED
    DUPLICATE = CMD_ACK_STATUS_DUPLICATE


class CmdResultStatus(str, Enum):
    RUNNING = CMD_RESULT_STATUS_RUNNING
    SUCCEEDED = CMD_RESULT_STATUS_SUCCEEDED
    FAILED = CMD_RESULT_STATUS_FAILED
    CANCELED = CMD_RESULT_STATUS_CANCELED
    TIMEOUT = CMD_RESULT_STATUS_TIMEOUT


StatusEnumT = TypeVar("StatusEnumT", bound=Enum)


JsonObject = dict[str, Any]
SyncCallback = Callable[..., None]
AsyncJsonSender = Callable[[JsonObject], Awaitable[None]]


class XiaogeProtocolError(ValueError):
    """Raised when an outbound client frame would violate R5.2.2."""


@dataclass(frozen=True, slots=True)
class ProtocolErrorEvent:
    message: str
    message_type: str
    raw: JsonObject | None = None


@dataclass(frozen=True, slots=True)
class ReadyEvent:
    sample_rate: int
    granted_caps: tuple[str, ...]
    config_version: str
    trace_id: str
    session_id: str
    raw: JsonObject


@dataclass(frozen=True, slots=True)
class ClearEvent:
    trace_id: str
    session_id: str
    raw: JsonObject
    reason: str | None = None
    utterance_id: str | None = None


@dataclass(frozen=True, slots=True)
class StateEvent:
    link_state: str
    interaction_mode: str
    engine_gate: str
    resource_state: str
    ts_ms: int
    trace_id: str
    session_id: str
    raw: JsonObject
    pending_confirmation: JsonObject | None = None


@dataclass(frozen=True, slots=True)
class SttEvent:
    text: str
    is_final: bool
    utterance_id: str
    trace_id: str
    session_id: str
    ts_ms: int
    raw: JsonObject

    @property
    def final(self) -> bool:
        return self.is_final


@dataclass(frozen=True, slots=True)
class ReplyEvent:
    text: str
    is_final: bool
    utterance_id: str
    intent_type: str
    trace_id: str
    session_id: str
    ts_ms: int
    raw: JsonObject
    speak_policy: str | None = None


@dataclass(frozen=True, slots=True)
class CommandEvent:
    cmd_id: str
    capability_id: str
    action: str
    params: JsonObject
    risk_level: str
    ack_timeout_ms: int
    result_timeout_ms: int
    issued_at_ms: int
    utterance_id: str
    trace_id: str
    session_id: str
    raw: JsonObject


@dataclass(frozen=True, slots=True)
class ErrorEvent:
    code: str
    message: str
    retryable: bool
    ts_ms: int
    trace_id: str
    session_id: str
    raw: JsonObject


def now_ms() -> int:
    return int(time.time() * 1000)


def json_compact(payload: JsonObject) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def json_utf8_size(payload: JsonObject) -> int:
    return len(json_compact(payload).encode("utf-8"))


def validate_caps(caps: list[str]) -> None:
    if not caps:
        raise XiaogeProtocolError("caps must not be empty")
    if len(set(caps)) != len(caps):
        raise XiaogeProtocolError("caps must be unique")
    unknown = [cap for cap in caps if cap not in VALID_CAPS]
    if unknown:
        raise XiaogeProtocolError(f"unknown caps: {unknown}")


def _coerce_status(
    status: str | StatusEnumT,
    enum_type: type[StatusEnumT],
    valid_values: frozenset[str],
    field_name: str,
) -> str:
    status_value = status.value if isinstance(status, enum_type) else status
    if not isinstance(status_value, str) or status_value not in valid_values:
        expected = "/".join(sorted(valid_values))
        raise XiaogeProtocolError(f"{field_name} must be {expected}")
    return status_value


def default_ssl_context(*, ca_cert: str | Path | None = None, insecure: bool = False) -> object | None:
    if insecure:
        import ssl

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    cert = Path(ca_cert) if ca_cert else BUNDLED_CA_CERT
    if not cert.exists():
        return None

    import ssl

    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cafile=str(cert))
    return ctx


@dataclass(slots=True)
class SessionInfo:
    trace_id: str
    session_id: str
    access_token: str
    expires_in_ms: int
    ws_url: str
    granted_caps: list[str]
    config_snapshot: JsonObject

    @property
    def config_version(self) -> str:
        return str(self.config_snapshot.get("config_version", "unknown"))


@dataclass(slots=True)
class ClientConfig:
    create_session_url: str
    device_id: str
    credential: JsonObject | str
    api_key: str = CLOUD_API_KEY
    caps: list[str] = field(default_factory=lambda: ["audio", "text", "cmd", "state"])
    client_version: str = "xiaoge-python-sdk-r5.2.2"
    role: str = "device"
    prefs: JsonObject = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_caps(self.caps)
        if self.role not in {"device", "panel"}:
            raise XiaogeProtocolError("role must be device or panel")


class TraceLogger:
    """JSONL trace logger with the G3 required common fields."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None

    def emit(
        self,
        *,
        trace_id: str = "",
        session_id: str = "",
        utterance_id: str = "",
        cmd_id: str = "",
        message_type: str = "",
        direction: str = "",
        schema_result: str = "not_applicable",
        semantic_result: str = "not_applicable",
        transport_result: str = "not_applicable",
        final_result: str = "pass",
        failure_reason: str = "",
        payload: JsonObject | None = None,
    ) -> None:
        record: JsonObject = {
            "timestamp_ms": now_ms(),
            "side": "client",
            "manifest_sha256": MANIFEST_SHA256,
            "trace_id": trace_id,
            "session_id": session_id,
            "utterance_id": utterance_id,
            "cmd_id": cmd_id,
            "message_type": message_type,
            "direction": direction,
            "schema_result": schema_result,
            "semantic_result": semantic_result,
            "transport_result": transport_result,
            "final_result": final_result,
            "failure_reason": failure_reason,
        }
        if payload is not None:
            record["payload"] = payload
        line = json_compact(record)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        else:
            logger.debug("trace %s", line)


class ProtocolCodec:
    """R5.2.2 frame builder and transport guard."""

    @staticmethod
    def create_session_request(config: ClientConfig) -> JsonObject:
        return {
            "device_id": config.device_id,
            "credential": config.credential,
            "caps": config.caps,
            "prefs": config.prefs,
            "audio_format": {
                "sample_rate": SAMPLE_RATE,
                "channels": NUM_CHANNELS,
                "sample_format": SAMPLE_FORMAT,
            },
            "client_version": config.client_version,
        }

    @staticmethod
    def parse_session_response(payload: JsonObject) -> SessionInfo:
        required = {
            "type",
            "trace_id",
            "session_id",
            "access_token",
            "expires_in_ms",
            "ws_url",
            "granted_caps",
            "config_snapshot",
        }
        missing = required - payload.keys()
        if missing:
            raise XiaogeProtocolError(f"session.created missing fields: {sorted(missing)}")
        if payload["type"] != "session.created":
            raise XiaogeProtocolError("create_session response type must be session.created")
        ws_url = str(payload["ws_url"])
        parsed_ws_url = urlparse(ws_url)
        if (
            parsed_ws_url.path != WSS_SESSION_PATH
            or parsed_ws_url.query
            or parsed_ws_url.fragment
        ):
            raise XiaogeProtocolError(
                "session.created ws_url must point exactly to /ws/session without query or fragment"
            )
        granted_caps = list(payload["granted_caps"])
        validate_caps(granted_caps)
        return SessionInfo(
            str(payload["trace_id"]),
            str(payload["session_id"]),
            str(payload["access_token"]),
            int(payload["expires_in_ms"]),
            ws_url,
            granted_caps,
            dict(payload["config_snapshot"]),
        )

    @staticmethod
    def ctrl_hello(config: ClientConfig, session: SessionInfo) -> JsonObject:
        payload: JsonObject = {
            "type": "ctrl.hello",
            "trace_id": session.trace_id,
            "session_id": session.session_id,
            "proto": PROTO_VERSION,
            "role": config.role,
            "device_id": config.device_id,
            "caps": session.granted_caps,
        }
        if config.prefs:
            payload["prefs"] = config.prefs
        return payload

    @staticmethod
    def ctrl_frontend_state(
        session: SessionInfo,
        *,
        seq: int,
        ttl_ms: int,
        trust_level: str,
        wake_event: str | None = None,
        wake_state: str | None = None,
        vad: str | None = None,
        doa: float | None = None,
        lock_mode: bool | None = None,
        ts_ms: int | None = None,
    ) -> JsonObject:
        if trust_level not in VALID_TRUST_LEVELS:
            raise XiaogeProtocolError("trust_level must be authoritative, hint, or observe")
        if seq < 0:
            raise XiaogeProtocolError("frontend_state seq must be >= 0")
        if ttl_ms <= 0:
            raise XiaogeProtocolError("frontend_state ttl_ms must be > 0")
        payload: JsonObject = {
            "type": "ctrl.frontend_state",
            "trace_id": session.trace_id,
            "session_id": session.session_id,
            "seq": seq,
            "ts_ms": ts_ms if ts_ms is not None else now_ms(),
            "ttl_ms": ttl_ms,
            "trust_level": trust_level,
        }
        optional = {
            "wake_event": wake_event,
            "wake_state": wake_state,
            "vad": vad,
            "doa": doa,
            "lock_mode": lock_mode,
        }
        payload.update({k: v for k, v in optional.items() if v is not None})
        return payload

    @staticmethod
    def cmd_ack(
        command: JsonObject,
        status: str | CmdAckStatus,
        code: str,
        message: str = "",
    ) -> JsonObject:
        status_value = _coerce_status(
            status,
            CmdAckStatus,
            VALID_CMD_ACK_STATUSES,
            "cmd_ack status",
        )
        payload: JsonObject = {
            "type": "data.cmd_ack",
            "trace_id": command["trace_id"],
            "session_id": command["session_id"],
            "utterance_id": command["utterance_id"],
            "cmd_id": command["cmd_id"],
            "status": status_value,
            "code": code,
            "received_at_ms": now_ms(),
        }
        if message:
            payload["message"] = message
        return payload

    @staticmethod
    def cmd_result(
        command: JsonObject,
        status: str | CmdResultStatus,
        code: str,
        message: str = "",
        *,
        retryable: bool | None = None,
        started_at_ms: int | None = None,
        finished_at_ms: int | None = None,
        duration_ms: int | None = None,
    ) -> JsonObject:
        status_value = _coerce_status(
            status,
            CmdResultStatus,
            VALID_CMD_RESULT_STATUSES,
            "cmd_result status",
        )
        payload: JsonObject = {
            "type": "data.cmd_result",
            "trace_id": command["trace_id"],
            "session_id": command["session_id"],
            "utterance_id": command["utterance_id"],
            "cmd_id": command["cmd_id"],
            "status": status_value,
            "code": code,
        }
        optional = {
            "message": message,
            "retryable": retryable,
            "started_at_ms": started_at_ms,
            "finished_at_ms": finished_at_ms,
            "duration_ms": duration_ms,
        }
        payload.update({k: v for k, v in optional.items() if v is not None and v != ""})
        return payload

    @staticmethod
    def data_error(
        session: SessionInfo | None,
        *,
        code: str,
        message: str,
        retryable: bool,
        trace_id: str = "",
        session_id: str = "",
    ) -> JsonObject:
        return {
            "type": "data.error",
            "trace_id": session.trace_id if session else trace_id,
            "session_id": session.session_id if session else session_id,
            "code": code,
            "message": message,
            "retryable": retryable,
            "ts_ms": now_ms(),
        }

    @staticmethod
    def encode_json(payload: JsonObject) -> str:
        size = json_utf8_size(payload)
        if size > JSON_TEXT_FRAME_MAX_BYTES:
            raise XiaogeProtocolError(f"JSON text frame is {size} bytes, max is 8192")
        return json_compact(payload)


def _raw_copy(payload: JsonObject) -> JsonObject:
    return copy.deepcopy(payload)


def _required_string(payload: JsonObject, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise XiaogeProtocolError(f"{key} must be a non-empty string")
    return value


def _optional_string(payload: JsonObject, key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise XiaogeProtocolError(f"{key} must be a non-empty string")
    return value


def _required_bool(payload: JsonObject, key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise XiaogeProtocolError(f"{key} must be a boolean")
    return value


def _required_int(payload: JsonObject, key: str, *, minimum: int = 0) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise XiaogeProtocolError(f"{key} must be an integer >= {minimum}")
    return value


def _required_enum(payload: JsonObject, key: str, valid: frozenset[str]) -> str:
    value = _required_string(payload, key)
    if value not in valid:
        expected = "/".join(sorted(valid))
        raise XiaogeProtocolError(f"{key} must be {expected}")
    return value


def _optional_enum(payload: JsonObject, key: str, valid: frozenset[str]) -> str | None:
    value = _optional_string(payload, key)
    if value is None:
        return None
    if value not in valid:
        expected = "/".join(sorted(valid))
        raise XiaogeProtocolError(f"{key} must be {expected}")
    return value


def _require_type(payload: JsonObject, kind: str) -> None:
    if payload.get("type") != kind:
        raise XiaogeProtocolError(f"type must be {kind}")


def _ready_event_from_payload(payload: JsonObject) -> ReadyEvent:
    _require_type(payload, "ctrl.ready")
    granted_caps_raw = payload.get("granted_caps")
    if not isinstance(granted_caps_raw, list) or not all(isinstance(cap, str) for cap in granted_caps_raw):
        raise XiaogeProtocolError("granted_caps must be a string array")
    validate_caps(list(granted_caps_raw))
    return ReadyEvent(
        sample_rate=_required_int(payload, "sample_rate"),
        granted_caps=tuple(granted_caps_raw),
        config_version=_required_string(payload, "config_version"),
        trace_id=_required_string(payload, "trace_id"),
        session_id=_required_string(payload, "session_id"),
        raw=_raw_copy(payload),
    )


def _clear_event_from_payload(payload: JsonObject) -> ClearEvent:
    _require_type(payload, "ctrl.clear")
    return ClearEvent(
        trace_id=_required_string(payload, "trace_id"),
        session_id=_required_string(payload, "session_id"),
        raw=_raw_copy(payload),
        reason=_optional_enum(payload, "reason", VALID_CLEAR_REASONS),
        utterance_id=_optional_string(payload, "utterance_id"),
    )


def _state_event_from_payload(payload: JsonObject) -> StateEvent:
    _require_type(payload, "ctrl.state")
    pending = payload.get("pending_confirmation")
    if pending is not None and not isinstance(pending, dict):
        raise XiaogeProtocolError("pending_confirmation must be an object")
    return StateEvent(
        link_state=_required_enum(payload, "link_state", VALID_LINK_STATES),
        interaction_mode=_required_enum(payload, "interaction_mode", VALID_INTERACTION_MODES),
        engine_gate=_required_enum(payload, "engine_gate", VALID_ENGINE_GATES),
        resource_state=_required_enum(payload, "resource_state", VALID_RESOURCE_STATES),
        ts_ms=_required_int(payload, "ts_ms"),
        trace_id=_required_string(payload, "trace_id"),
        session_id=_required_string(payload, "session_id"),
        raw=_raw_copy(payload),
        pending_confirmation=_raw_copy(pending) if pending is not None else None,
    )


def _stt_event_from_payload(payload: JsonObject) -> SttEvent:
    _require_type(payload, "data.stt")
    return SttEvent(
        text=_required_string(payload, "text"),
        is_final=_required_bool(payload, "final"),
        utterance_id=_required_string(payload, "utterance_id"),
        trace_id=_required_string(payload, "trace_id"),
        session_id=_required_string(payload, "session_id"),
        ts_ms=_required_int(payload, "ts_ms"),
        raw=_raw_copy(payload),
    )


def _reply_event_from_payload(payload: JsonObject) -> ReplyEvent:
    _require_type(payload, "data.reply")
    return ReplyEvent(
        text=_required_string(payload, "text"),
        is_final=True,
        utterance_id=_required_string(payload, "utterance_id"),
        intent_type=_required_enum(payload, "intent_type", VALID_INTENT_TYPES),
        trace_id=_required_string(payload, "trace_id"),
        session_id=_required_string(payload, "session_id"),
        ts_ms=_required_int(payload, "ts_ms"),
        raw=_raw_copy(payload),
        speak_policy=_optional_enum(payload, "speak_policy", VALID_SPEAK_POLICIES),
    )


def _command_event_from_payload(payload: JsonObject) -> CommandEvent:
    _require_type(payload, "data.cmd")
    params = payload.get("params")
    if not isinstance(params, dict):
        raise XiaogeProtocolError("params must be an object")
    return CommandEvent(
        cmd_id=_required_string(payload, "cmd_id"),
        capability_id=_required_string(payload, "capability_id"),
        action=_required_string(payload, "action"),
        params=_raw_copy(params),
        risk_level=_required_enum(payload, "risk_level", VALID_RISK_LEVELS),
        ack_timeout_ms=_required_int(payload, "ack_timeout_ms", minimum=1),
        result_timeout_ms=_required_int(payload, "result_timeout_ms", minimum=1),
        issued_at_ms=_required_int(payload, "issued_at_ms"),
        utterance_id=_required_string(payload, "utterance_id"),
        trace_id=_required_string(payload, "trace_id"),
        session_id=_required_string(payload, "session_id"),
        raw=_raw_copy(payload),
    )


def _error_event_from_payload(payload: JsonObject) -> ErrorEvent:
    _require_type(payload, "data.error")
    return ErrorEvent(
        code=_required_enum(payload, "code", VALID_ERROR_CODES),
        message=_required_string(payload, "message"),
        retryable=_required_bool(payload, "retryable"),
        ts_ms=_required_int(payload, "ts_ms"),
        trace_id=_required_string(payload, "trace_id"),
        session_id=_required_string(payload, "session_id"),
        raw=_raw_copy(payload),
    )


def _command_context_from_event(event: CommandEvent) -> JsonObject:
    return {
        "type": "data.cmd",
        "trace_id": event.trace_id,
        "session_id": event.session_id,
        "utterance_id": event.utterance_id,
        "cmd_id": event.cmd_id,
    }


class FakeExecutor:
    """No-real-action command executor used by SDK demos and local replay."""

    def __init__(self, supported_capabilities: set[str] | None = None) -> None:
        self.supported_capabilities = supported_capabilities or {
            "motion.move",
            "robot.motion",
            "robot.light",
            "robot.speaker",
            "device.control",
            "cmd",
            "state",
        }
        self.supported_actions: dict[str, set[str]] = {
            "motion.move": {"navigation.move"},
            "robot.motion": {"navigation.move", "gesture.perform"},
            "robot.light": {"light.set"},
            "robot.speaker": {"speaker.set_volume"},
            "device.control": {"device.set"},
            "cmd": {"cmd.execute"},
            "state": {"state.set"},
        }
        self._seen_cmd_ids: set[str] = set()

    def handle_registry_delivery(self, delivery: str) -> bool:
        return delivery in EXECUTABLE_DELIVERIES

    def execute(self, command: JsonObject) -> tuple[JsonObject | None, list[JsonObject], JsonObject | None]:
        cmd_id = command.get("cmd_id")
        if not cmd_id:
            return None, [], self._unknown_cmd_error(command, "missing cmd_id")
        if cmd_id in self._seen_cmd_ids:
            ack = ProtocolCodec.cmd_ack(
                command,
                CmdAckStatus.DUPLICATE,
                "duplicate_cmd_id",
                "duplicate",
            )
            return ack, [], {"event": "duplicate_cmd_id", "cmd_id": cmd_id, "executed": False}

        validation_error = self._validate_command(command)
        if validation_error is not None:
            code, message = validation_error
            self._seen_cmd_ids.add(str(cmd_id))
            ack = ProtocolCodec.cmd_ack(command, CmdAckStatus.REJECTED, code, message)
            return ack, [], {"event": code, "cmd_id": cmd_id, "executed": False}

        self._seen_cmd_ids.add(str(cmd_id))
        start = now_ms()
        ack = ProtocolCodec.cmd_ack(command, CmdAckStatus.ACCEPTED, "ok", "accepted by SDK")
        running = ProtocolCodec.cmd_result(
            command,
            CmdResultStatus.RUNNING,
            "ok",
            "fake executor started",
            started_at_ms=start,
            retryable=False,
        )
        succeeded = ProtocolCodec.cmd_result(
            command,
            CmdResultStatus.SUCCEEDED,
            "ok",
            "fake executor completed",
            started_at_ms=start,
            finished_at_ms=start + 1,
            duration_ms=1,
            retryable=False,
        )
        return ack, [running, succeeded], None

    def _validate_command(self, command: JsonObject) -> tuple[str, str] | None:
        required_strings = ["type", "trace_id", "session_id", "utterance_id", "cmd_id", "capability_id", "action"]
        for key in required_strings:
            if not isinstance(command.get(key), str) or not command[key]:
                return "invalid_cmd_schema", f"{key} must be a non-empty string"
        if command["type"] != "data.cmd":
            return "invalid_cmd_schema", "type must be data.cmd"
        if not isinstance(command.get("params"), dict):
            return "invalid_cmd_schema", "params must be an object"
        if command.get("risk_level") not in {"low", "medium", "high"}:
            return "invalid_cmd_schema", "risk_level is invalid"
        for key in ("ack_timeout_ms", "result_timeout_ms", "issued_at_ms"):
            minimum = 0 if key == "issued_at_ms" else 1
            if not isinstance(command.get(key), int) or command[key] < minimum:
                return "invalid_cmd_schema", f"{key} is invalid"
        expires_at = int(command["issued_at_ms"]) + int(command["ack_timeout_ms"]) + int(command["result_timeout_ms"])
        if now_ms() > expires_at + COMMAND_MAX_AGE_GRACE_MS:
            return "late_cmd", "command exceeded ack/result timeout"
        capability = str(command["capability_id"])
        action = str(command["action"])
        if capability not in self.supported_capabilities:
            return "capability_unsupported", "unsupported capability"
        if action not in self.supported_actions.get(capability, {action}):
            return "action_unsupported", "unsupported action for capability"
        params = command["params"]
        if (capability, action) == ("motion.move", "navigation.move"):
            direction = params.get("direction")
            distance = params.get("distance_cm")
            if direction not in {"forward", "backward", "left", "right"}:
                return "invalid_params", "direction is invalid"
            if not isinstance(distance, int) or not 1 <= distance <= 10_000:
                return "invalid_params", "distance_cm is invalid"
        return None

    @staticmethod
    def _unknown_cmd_error(command: JsonObject, reason: str) -> JsonObject:
        return {
            "type": "data.error",
            "trace_id": command.get("trace_id", ""),
            "session_id": command.get("session_id", ""),
            "code": "unknown_cmd_id",
            "message": reason,
            "retryable": False,
            "ts_ms": now_ms(),
        }


class CommandDispatcher:
    """Consumes downlink data.cmd and emits data.cmd_ack/data.cmd_result."""

    def __init__(self, send_json: AsyncJsonSender, executor: FakeExecutor | None = None) -> None:
        self._send_json = send_json
        self.executor = executor or FakeExecutor()

    async def handle_cmd(self, command: JsonObject) -> None:
        ack, results, audit = self.executor.execute(command)
        if ack is not None:
            await self._send_json(ack)
        for result in results:
            await self._send_json(result)
        if audit is not None and audit.get("type") == "data.error":
            await self._send_json(audit)


class FrontendStateReporter:
    """Builds monotonic ctrl.frontend_state frames."""

    def __init__(self, session: SessionInfo, send_json: AsyncJsonSender, ttl_ms: int = 1000) -> None:
        self.session = session
        self._send_json = send_json
        self.ttl_ms = ttl_ms
        self._seq = 0
        self._last_emit_ms = 0

    async def report(
        self,
        *,
        trust_level: str = "hint",
        wake_event: str | None = None,
        wake_state: str | None = None,
        vad: str | None = None,
        doa: float | None = None,
        lock_mode: bool | None = None,
    ) -> JsonObject:
        self._seq += 1
        self._last_emit_ms = now_ms()
        payload = ProtocolCodec.ctrl_frontend_state(
            self.session,
            seq=self._seq,
            ttl_ms=self.ttl_ms,
            trust_level=trust_level,
            wake_event=wake_event,
            wake_state=wake_state,
            vad=vad,
            doa=doa,
            lock_mode=lock_mode,
            ts_ms=self._last_emit_ms,
        )
        await self._send_json(payload)
        return payload

    def is_stale(self, ts_ms: int | None = None) -> bool:
        if not self._last_emit_ms:
            return False
        return (ts_ms if ts_ms is not None else now_ms()) > self._last_emit_ms + self.ttl_ms


class XiaogeClient:
    """R5.2.2 session client.

    Callbacks are optional and run on the asyncio event-loop thread. Business
    callbacks expose typed high-level event objects; on_json is the single raw
    payload observer for logging, debugging, and protocol audits.
    """

    def __init__(
        self,
        create_session_url: str,
        device_id: str,
        credential: JsonObject | str,
        *,
        caps: list[str] | None = None,
        client_version: str = "xiaoge-python-sdk-r5.2.2",
        role: str = "device",
        prefs: JsonObject | None = None,
        api_key: str = CLOUD_API_KEY,
        ssl: object | None = None,
        trace_log_path: str | Path | None = None,
        executor: FakeExecutor | None = None,
    ) -> None:
        self.config = ClientConfig(
            create_session_url=create_session_url,
            device_id=device_id,
            credential=credential,
            api_key=api_key,
            caps=caps or ["audio", "text", "cmd", "state"],
            client_version=client_version,
            role=role,
            prefs=prefs or {},
        )
        self._ssl = ssl
        self._ws: Any | None = None
        self.session: SessionInfo | None = None
        self.frontend_state: FrontendStateReporter | None = None
        self.dispatcher = CommandDispatcher(self.send_json, executor)
        self.trace_logger = TraceLogger(trace_log_path)

        self.on_ready: SyncCallback | None = None
        self.on_audio: SyncCallback | None = None
        self.on_clear: SyncCallback | None = None
        self.on_state: SyncCallback | None = None
        self.on_stt: SyncCallback | None = None
        self.on_reply: SyncCallback | None = None
        self.on_command: SyncCallback | None = None
        self.on_error: SyncCallback | None = None
        self.on_json: SyncCallback | None = None
        self.on_protocol_error: SyncCallback | None = None
        self.on_failure: SyncCallback | None = None
        self.on_ready_event: SyncCallback | None = None
        self.on_stt_text: SyncCallback | None = None
        self.on_reply_text: SyncCallback | None = None

    async def create_session(self) -> SessionInfo:
        payload = ProtocolCodec.create_session_request(self.config)
        data = ProtocolCodec.encode_json(payload).encode("utf-8")

        def _post() -> JsonObject:
            headers = {"Content-Type": "application/json"}
            if self.config.api_key:
                headers["x-api-key"] = self.config.api_key
            req = urllib.request.Request(
                self.config.create_session_url,
                data=data,
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=10, context=self._ssl) as resp:  # type: ignore[arg-type]
                    body = resp.read().decode("utf-8")
            except TypeError:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    body = resp.read().decode("utf-8")
            except urllib.error.HTTPError as exc:
                raise XiaogeProtocolError(f"create_session failed HTTP {exc.code}") from exc
            return json.loads(body)

        response = await asyncio.to_thread(_post)
        self.session = ProtocolCodec.parse_session_response(response)
        self.trace_logger.emit(
            trace_id=self.session.trace_id,
            session_id=self.session.session_id,
            message_type="session.created",
            direction="down",
            schema_result="pass",
            payload={"ws_url": self.session.ws_url, "granted_caps": self.session.granted_caps},
        )
        return self.session

    async def run(self) -> None:
        session = await self.create_session()
        headers = {"Authorization": f"Bearer {session.access_token}"}
        async with await self._connect_ws(session.ws_url, headers) as ws:
            self._ws = ws
            logger.info("connected %s", session.ws_url)
            try:
                await self.send_json(ProtocolCodec.ctrl_hello(self.config, session))
                self.frontend_state = FrontendStateReporter(session, self.send_json)
                async for message in ws:
                    await self._dispatch(message)
            finally:
                self.frontend_state = None
                self._ws = None
                logger.info("disconnected")

    async def send_pcm(self, pcm: bytes) -> None:
        if not pcm:
            return
        if len(pcm) > BINARY_FRAME_MAX_BYTES:
            raise XiaogeProtocolError("binary PCM frame exceeds 32768 bytes")
        ws = self._ws
        if ws is not None:
            await ws.send(pcm)
            self._log_frame("binary.pcm", "up", transport_result="pass")

    async def send_json(self, payload: JsonObject) -> None:
        text = ProtocolCodec.encode_json(payload)
        ws = self._ws
        if ws is None:
            raise XiaogeProtocolError("websocket is not connected")
        await ws.send(text)
        self._log_payload(payload, "up", transport_result="pass")

    async def send_frontend_state(self, **kwargs: Any) -> JsonObject:
        if self.frontend_state is None:
            raise XiaogeProtocolError("frontend_state reporter is not ready")
        return await self.frontend_state.report(**kwargs)

    async def send_command_ack(
        self,
        event: CommandEvent,
        status: str | CmdAckStatus,
        code: str,
        message: str = "",
    ) -> None:
        await self.send_json(ProtocolCodec.cmd_ack(_command_context_from_event(event), status, code, message))

    async def send_command_result(
        self,
        event: CommandEvent,
        status: str | CmdResultStatus,
        code: str,
        message: str = "",
        *,
        retryable: bool | None = None,
        started_at_ms: int | None = None,
        finished_at_ms: int | None = None,
        duration_ms: int | None = None,
    ) -> None:
        await self.send_json(
            ProtocolCodec.cmd_result(
                _command_context_from_event(event),
                status,
                code,
                message,
                retryable=retryable,
                started_at_ms=started_at_ms,
                finished_at_ms=finished_at_ms,
                duration_ms=duration_ms,
            )
        )

    async def close(self) -> None:
        ws = self._ws
        if ws is not None:
            await ws.close()

    async def _connect_ws(self, ws_url: str, headers: dict[str, str]) -> Any:
        kwargs = {"max_size": None}
        if urlparse(ws_url).scheme == "wss" and self._ssl is not None:
            kwargs["ssl"] = self._ssl
        params = inspect.signature(websockets.connect).parameters
        if "additional_headers" in params:
            return websockets.connect(ws_url, additional_headers=headers, **kwargs)
        return websockets.connect(ws_url, extra_headers=headers, **kwargs)

    async def _dispatch(self, message: str | bytes) -> None:
        if isinstance(message, (bytes, bytearray)):
            self._log_frame("binary.pcm", "down", transport_result="pass")
            self._call(self.on_audio, bytes(message))
            return
        try:
            payload = json.loads(message)
        except (ValueError, TypeError) as exc:
            logger.warning("non-JSON text frame: %r", message)
            self._emit_protocol_error("invalid JSON text frame", "unknown", None, exc)
            return
        if not isinstance(payload, dict):
            self._emit_protocol_error("JSON text frame must be an object", "unknown", None)
            return
        await self._handle_json(payload)

    async def _handle_json(self, payload: JsonObject) -> None:
        kind = str(payload.get("type", "unknown"))
        self._log_payload(payload, "down", schema_result="pass")
        if kind == "ctrl.ready":
            event = self._build_event(payload, kind, _ready_event_from_payload)
            if event is not None:
                self._call(self.on_ready_event, event)
                self._call(self.on_ready, event.sample_rate)
        elif kind == "ctrl.clear":
            event = self._build_event(payload, kind, _clear_event_from_payload)
            if event is not None:
                self._call(self.on_clear, event)
        elif kind == "ctrl.state":
            event = self._build_event(payload, kind, _state_event_from_payload)
            if event is not None:
                self._call(self.on_state, event)
        elif kind == "data.stt":
            event = self._build_event(payload, kind, _stt_event_from_payload)
            if event is not None:
                self._call(self.on_stt, event)
                self._call(self.on_stt_text, event.text, event.is_final)
        elif kind == "data.reply":
            event = self._build_event(payload, kind, _reply_event_from_payload)
            if event is not None:
                self._call(self.on_reply, event)
                self._call(self.on_reply_text, event.text)
        elif kind == "data.cmd":
            event = self._build_event(payload, kind, _command_event_from_payload)
            if event is not None:
                self._call(self.on_command, event)
        elif kind == "data.error":
            event = self._build_event(payload, kind, _error_event_from_payload)
            if event is not None:
                self._call(self.on_error, event)
        else:
            self._call(self.on_json, payload)
            logger.debug("ignored message type: %s", kind)

    def _build_event(
        self,
        payload: JsonObject,
        kind: str,
        builder: Callable[[JsonObject], object],
    ) -> object | None:
        try:
            event = builder(payload)
        except XiaogeProtocolError as exc:
            self._call(self.on_json, payload)
            self._emit_protocol_error(str(exc), kind, payload, exc)
            return None
        self._call(self.on_json, payload)
        return event

    def _emit_protocol_error(
        self,
        message: str,
        message_type: str,
        raw: JsonObject | None,
        exc: Exception | None = None,
    ) -> None:
        event = ProtocolErrorEvent(message=message, message_type=message_type, raw=_raw_copy(raw) if raw else None)
        self._call(self.on_protocol_error, event)
        if self.on_failure is not None:
            self._call(self.on_failure, exc or XiaogeProtocolError(message))

    def _log_payload(
        self,
        payload: JsonObject,
        direction: str,
        *,
        schema_result: str = "not_applicable",
        semantic_result: str = "not_applicable",
        transport_result: str = "not_applicable",
        final_result: str = "pass",
        failure_reason: str = "",
    ) -> None:
        self.trace_logger.emit(
            trace_id=str(payload.get("trace_id", self.session.trace_id if self.session else "")),
            session_id=str(payload.get("session_id", self.session.session_id if self.session else "")),
            utterance_id=str(payload.get("utterance_id", "")),
            cmd_id=str(payload.get("cmd_id", "")),
            message_type=str(payload.get("type", "unknown")),
            direction=direction,
            schema_result=schema_result,
            semantic_result=semantic_result,
            transport_result=transport_result,
            final_result=final_result,
            failure_reason=failure_reason,
        )

    def _log_frame(self, message_type: str, direction: str, *, transport_result: str) -> None:
        session = self.session
        self.trace_logger.emit(
            trace_id=session.trace_id if session else "",
            session_id=session.session_id if session else "",
            message_type=message_type,
            direction=direction,
            transport_result=transport_result,
        )

    @staticmethod
    def _call(cb: SyncCallback | None, *args: object) -> None:
        if cb is None:
            return
        try:
            cb(*args)
        except Exception:
            logger.exception("client callback error")
