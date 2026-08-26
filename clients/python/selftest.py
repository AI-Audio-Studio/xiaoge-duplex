"""R5.2.2 Python SDK self-test.

Starts a local create_session HTTP endpoint and a WSS-session-compatible mock
WebSocket server. No production service and no real robot action are used.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import websockets

from xiaoge_client import (
    CmdAckStatus,
    CmdResultStatus,
    FakeExecutor,
    JSON_TEXT_FRAME_MAX_BYTES,
    ProtocolCodec,
    XiaogeClient,
    XiaogeProtocolError,
    json_utf8_size,
    now_ms,
)


TRACE_ID = "trace-selftest-0001"
SESSION_ID = "sess-selftest-0001"
TOKEN = "jwt-selftest"


class _CreateSessionHandler(BaseHTTPRequestHandler):
    ws_port = 0

    def do_POST(self) -> None:  # noqa: N802
        assert self.headers.get("x-api-key") == "selftest-api-key"
        size = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(size).decode("utf-8"))
        assert request["device_id"] == "robot-selftest"
        assert request["audio_format"] == {
            "sample_rate": 16000,
            "channels": 1,
            "sample_format": "int16le",
        }
        body = json.dumps(
            {
                "type": "session.created",
                "trace_id": TRACE_ID,
                "session_id": SESSION_ID,
                "access_token": TOKEN,
                "expires_in_ms": 600000,
                "ws_url": f"ws://127.0.0.1:{self.ws_port}/ws/session",
                "granted_caps": ["audio", "text", "cmd", "state"],
                "config_snapshot": {"config_version": "cfg-selftest"},
            },
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def _start_http(port: int, ws_port: int) -> ThreadingHTTPServer:
    _CreateSessionHandler.ws_port = ws_port
    server = ThreadingHTTPServer(("127.0.0.1", port), _CreateSessionHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _request_headers(conn: Any) -> Any:
    if hasattr(conn, "request") and hasattr(conn.request, "headers"):
        return conn.request.headers
    return getattr(conn, "request_headers", {})


async def _mock_ws(conn: Any) -> None:
    headers = _request_headers(conn)
    assert headers.get("Authorization") == f"Bearer {TOKEN}"
    assert headers.get("x-api-key") is None
    hello = json.loads(await conn.recv())
    assert hello["type"] == "ctrl.hello"
    assert hello["proto"] == 2
    assert hello["session_id"] == SESSION_ID
    assert "token" not in hello
    assert "api_key" not in hello
    assert "x-api-key" not in hello

    await conn.send(
        json.dumps(
            {
                "type": "ctrl.ready",
                "trace_id": TRACE_ID,
                "session_id": SESSION_ID,
                "sample_rate": 16000,
                "granted_caps": ["audio", "text", "cmd", "state"],
                "config_version": "cfg-selftest",
            },
            separators=(",", ":"),
        )
    )
    await conn.send(
        json.dumps(
            {
                "type": "ctrl.state",
                "trace_id": TRACE_ID,
                "session_id": SESSION_ID,
                "link_state": "connected",
                "interaction_mode": "dialogue",
                "engine_gate": "open",
                "resource_state": "ActiveAgent",
                "ts_ms": 1789000000000,
            },
            separators=(",", ":"),
        )
    )

    got_pcm = 0
    saw_frontend_state = False
    sent_cmd = False
    frontend_deadline = 0

    async for msg in conn:
        if isinstance(msg, (bytes, bytearray)):
            got_pcm += len(msg)
            if got_pcm >= 640 and not sent_cmd:
                await conn.send(b"\x01\x02" * 160)
                await conn.send(
                    json.dumps(
                        {
                            "type": "ctrl.clear",
                            "trace_id": TRACE_ID,
                            "session_id": SESSION_ID,
                            "utterance_id": "utt-selftest",
                            "reason": "barge_in",
                        },
                        separators=(",", ":"),
                    )
                )
                await conn.send(
                    json.dumps(
                        {
                            "type": "data.stt",
                            "trace_id": TRACE_ID,
                            "session_id": SESSION_ID,
                            "utterance_id": "utt-selftest",
                            "text": "往前走一米",
                            "final": True,
                            "ts_ms": 1789000000200,
                        },
                        separators=(",", ":"),
                    )
                )
                await conn.send(
                    json.dumps(
                        {
                            "type": "data.reply",
                            "trace_id": TRACE_ID,
                            "session_id": SESSION_ID,
                            "utterance_id": "utt-selftest",
                            "intent_type": "control_cmd",
                            "speak_policy": "ack_then_result",
                            "text": "好的，正在执行。",
                            "ts_ms": 1789000000300,
                        },
                        separators=(",", ":"),
                    )
                )
                await conn.send(
                    json.dumps(
                        {
                            "type": "data.cmd",
                            "trace_id": TRACE_ID,
                            "session_id": SESSION_ID,
                            "utterance_id": "utt-selftest",
                            "cmd_id": "cmd-selftest",
                            "capability_id": "motion.move",
                            "action": "navigation.move",
                            "params": {"direction": "forward", "distance_cm": 1},
                            "risk_level": "medium",
                            "ack_timeout_ms": 800,
                            "result_timeout_ms": 5000,
                            "issued_at_ms": 1789000000100,
                        },
                        separators=(",", ":"),
                    )
                )
                sent_cmd = True
                if saw_frontend_state:
                    frontend_deadline = got_pcm
                    if got_pcm >= frontend_deadline:
                        await conn.close()
                        return
        else:
            payload = json.loads(msg)
            kind = payload.get("type")
            if kind == "ctrl.frontend_state":
                saw_frontend_state = payload["trust_level"] == "authoritative"
                if saw_frontend_state and sent_cmd:
                    frontend_deadline = got_pcm
            elif kind in {"data.cmd_ack", "data.cmd_result"}:
                raise AssertionError("client must not auto-send command ack/result")
            if frontend_deadline and got_pcm >= frontend_deadline:
                await conn.close()
                return


async def _scenario() -> dict[str, Any]:
    http_port = 8898
    ws_port = 8899
    events: dict[str, Any] = {
        "json": 0,
        "ready": None,
        "ready_event": None,
        "audio": 0,
        "clear": 0,
        "state": 0,
        "stt": None,
        "stt_text": None,
        "reply": None,
        "reply_text": None,
        "command": None,
        "protocol_error": 0,
    }
    http = _start_http(http_port, ws_port)
    try:
        async with websockets.serve(_mock_ws, "127.0.0.1", ws_port):
            client = XiaogeClient(
                f"http://127.0.0.1:{http_port}/create_session",
                "robot-selftest",
                {"type": "mock", "value": "selftest"},
                api_key="selftest-api-key",
            )
            client.on_json = lambda payload: events.__setitem__("json", events["json"] + 1)
            client.on_ready = lambda sr: events.__setitem__("ready", sr)
            client.on_ready_event = lambda event: events.__setitem__("ready_event", event.sample_rate)
            client.on_audio = lambda pcm: events.__setitem__("audio", events["audio"] + len(pcm))
            client.on_clear = lambda event: events.__setitem__("clear", events["clear"] + 1)
            client.on_state = lambda event: events.__setitem__("state", events["state"] + 1)
            client.on_stt = lambda event: events.__setitem__("stt", event)
            client.on_stt_text = lambda text, final: events.__setitem__("stt_text", (text, final))
            client.on_reply = lambda event: events.__setitem__("reply", event)
            client.on_reply_text = lambda text: events.__setitem__("reply_text", text)
            client.on_command = lambda event: events.__setitem__("command", event)
            client.on_protocol_error = lambda event: events.__setitem__(
                "protocol_error", events["protocol_error"] + 1
            )

            runner = asyncio.create_task(client.run())
            while client.frontend_state is None:
                await asyncio.sleep(0.01)
            await client.send_frontend_state(
                trust_level="authoritative",
                wake_event="local_kws",
                wake_state="awake",
                vad="speech",
                lock_mode=False,
            )
            for _ in range(3):
                await client.send_pcm(b"\x00" * 320)
                await asyncio.sleep(0.02)
            await asyncio.wait_for(runner, timeout=3.0)
    finally:
        http.shutdown()
    return events


def _transport_limits() -> None:
    base = {
        "type": "data.reply",
        "trace_id": TRACE_ID,
        "session_id": SESSION_ID,
        "utterance_id": "utt-size",
        "intent_type": "chat",
        "text": "",
        "ts_ms": 1789000000000,
    }
    fixed = json_utf8_size(base)
    base["text"] = "x" * (JSON_TEXT_FRAME_MAX_BYTES - fixed)
    assert json_utf8_size(base) == JSON_TEXT_FRAME_MAX_BYTES
    ProtocolCodec.encode_json(base)
    base["text"] += "x"
    try:
        ProtocolCodec.encode_json(base)
    except XiaogeProtocolError:
        return
    raise AssertionError("8193-byte JSON frame must fail")


def _command_boundaries() -> None:
    base = {
        "type": "data.cmd",
        "trace_id": TRACE_ID,
        "session_id": SESSION_ID,
        "utterance_id": "utt-boundary",
        "cmd_id": "cmd-boundary",
        "capability_id": "motion.move",
        "action": "navigation.move",
        "params": {"direction": "forward", "distance_cm": 1},
        "risk_level": "medium",
        "ack_timeout_ms": 800,
        "result_timeout_ms": 5000,
        "issued_at_ms": now_ms(),
    }
    executor = FakeExecutor()
    ack, results, audit = executor.execute(dict(base))
    assert ack and ack["status"] == "accepted"
    assert any(r["status"] == "running" for r in results)
    assert any(r["status"] == "succeeded" for r in results)
    assert audit is None

    ack, results, audit = executor.execute(dict(base))
    assert ack and ack["status"] == "duplicate"
    assert not results
    assert audit and audit["executed"] is False

    unsupported = dict(base, cmd_id="cmd-unsupported", capability_id="robot.unknown")
    ack, results, audit = executor.execute(unsupported)
    assert ack and ack["status"] == "rejected" and ack["code"] == "capability_unsupported"
    assert not results
    assert audit and audit["executed"] is False

    bad_params = dict(base, cmd_id="cmd-bad-params", params={"direction": "sideways", "distance_cm": 1})
    ack, results, audit = executor.execute(bad_params)
    assert ack and ack["status"] == "rejected" and ack["code"] == "invalid_params"
    assert not results
    assert audit and audit["executed"] is False

    late = dict(base, cmd_id="cmd-late", issued_at_ms=1)
    ack, results, audit = executor.execute(late)
    assert ack and ack["status"] == "rejected" and ack["code"] == "late_cmd"
    assert not results
    assert audit and audit["executed"] is False

    for status in CmdAckStatus:
        payload = ProtocolCodec.cmd_ack(base, status, "ok")
        assert payload["status"] == status.value
    assert ProtocolCodec.cmd_ack(base, "accepted", "ok")["status"] == "accepted"
    try:
        ProtocolCodec.cmd_ack(base, "unknown", "bad")
    except XiaogeProtocolError:
        pass
    else:
        raise AssertionError("invalid cmd_ack status must fail")

    for status in CmdResultStatus:
        payload = ProtocolCodec.cmd_result(base, status, "ok")
        assert payload["status"] == status.value
    assert ProtocolCodec.cmd_result(base, "running", "ok")["status"] == "running"
    try:
        ProtocolCodec.cmd_result(base, "done", "bad")
    except XiaogeProtocolError:
        pass
    else:
        raise AssertionError("invalid cmd_result status must fail")


def _session_payload(ws_url: str) -> dict[str, Any]:
    return {
        "type": "session.created",
        "trace_id": TRACE_ID,
        "session_id": SESSION_ID,
        "access_token": "token",
        "expires_in_ms": 1000,
        "ws_url": ws_url,
        "granted_caps": ["audio", "text", "cmd", "state"],
        "config_snapshot": {"config_version": "cfg"},
    }


def _assert_session_payload_fails(payload: dict[str, Any], message: str) -> None:
    try:
        ProtocolCodec.parse_session_response(payload)
    except XiaogeProtocolError:
        return
    raise AssertionError(message)


def _assert_session_url_fails(ws_url: str, message: str) -> None:
    _assert_session_payload_fails(_session_payload(ws_url), message)


def _no_legacy_session_path() -> None:
    _assert_session_url_fails("ws://127.0.0.1/ws/audio", "legacy /ws/audio session path must fail")
    _assert_session_url_fails(
        "ws://127.0.0.1/ws/session?access_token=token",
        "query token on /ws/session must fail",
    )
    _assert_session_url_fails(
        "ws://127.0.0.1/ws/session#fragment",
        "fragment on /ws/session must fail",
    )
    zero_expiry = _session_payload("ws://127.0.0.1/ws/session")
    zero_expiry["expires_in_ms"] = 0
    _assert_session_payload_fails(zero_expiry, "zero expires_in_ms must fail")
    negative_expiry = _session_payload("ws://127.0.0.1/ws/session")
    negative_expiry["expires_in_ms"] = -1
    _assert_session_payload_fails(negative_expiry, "negative expires_in_ms must fail")
    extra_field = _session_payload("ws://127.0.0.1/ws/session")
    extra_field["token"] = "must-not-be-here"
    _assert_session_payload_fails(extra_field, "extra session.created fields must fail")


async def _event_validation() -> None:
    client = XiaogeClient("http://127.0.0.1/create_session", "robot", {"type": "mock"})
    seen: dict[str, Any] = {"protocol_error": 0, "ready": 0, "stt": 0}
    client.on_protocol_error = lambda event: seen.__setitem__(
        "protocol_error", seen["protocol_error"] + 1
    )
    client.on_ready_event = lambda event: seen.__setitem__("ready", seen["ready"] + 1)
    client.on_stt = lambda event: seen.__setitem__("stt", seen["stt"] + 1)
    await client._handle_json(  # noqa: SLF001 - selftest covers inbound validation behavior.
        {
            "type": "data.stt",
            "trace_id": TRACE_ID,
            "session_id": SESSION_ID,
            "utterance_id": "utt-invalid",
            "text": "bad",
            "final": "true",
            "ts_ms": 1,
        }
    )
    await client._handle_json(  # noqa: SLF001 - selftest covers strict additionalProperties.
        {
            "type": "data.stt",
            "trace_id": TRACE_ID,
            "session_id": SESSION_ID,
            "utterance_id": "utt-extra",
            "text": "bad",
            "final": True,
            "ts_ms": 1,
            "extra": "forbidden",
        }
    )
    await client._handle_json(  # noqa: SLF001 - selftest covers ready sample-rate const.
        {
            "type": "ctrl.ready",
            "trace_id": TRACE_ID,
            "session_id": SESSION_ID,
            "sample_rate": 8000,
            "granted_caps": ["audio", "text"],
            "config_version": "cfg-invalid",
        }
    )
    assert seen == {"protocol_error": 3, "ready": 0, "stt": 0}, seen


async def main() -> int:
    _transport_limits()
    _command_boundaries()
    _no_legacy_session_path()
    await _event_validation()
    events = await _scenario()
    assert events["json"] >= 5, events
    assert events["ready"] == 16000, events
    assert events["ready_event"] == 16000, events
    assert events["audio"] == 320, events
    assert events["clear"] == 1, events
    assert events["state"] == 1, events
    stt = events["stt"]
    assert stt.text == "往前走一米" and stt.is_final is True and stt.final is True, events
    assert stt.raw["text"] == "往前走一米", events
    assert events["stt_text"] == ("往前走一米", True), events
    reply = events["reply"]
    assert reply.text == "好的，正在执行。" and reply.intent_type == "control_cmd", events
    assert reply.speak_policy == "ack_then_result" and reply.is_final is True, events
    assert "final" not in reply.raw, events
    assert events["reply_text"] == "好的，正在执行。", events
    command = events["command"]
    assert command.cmd_id == "cmd-selftest" and command.action == "navigation.move", events
    assert events["protocol_error"] == 0, events
    print("[r5.2.2] create_session/auth/hello/ready/state/pcm/clear/cmd callback OK")
    print("[transport] 8192 pass / 8193 fail OK")
    print("[cmd] accepted/rejected/duplicate/late/result-status OK")
    print("records=local-selftest failures=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
