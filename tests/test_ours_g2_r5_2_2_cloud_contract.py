from __future__ import annotations

import json
from typing import Any

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from tests._g2_contract_r5_2_2 import (
    CLOSE_CODES_PATH,
    EXAMPLES_PATH,
    MANIFEST_PATH,
    MANIFEST_SHA256,
    PROTOCOL_SCHEMA_PATH,
    REGISTRY_SCHEMA_PATH,
    CloudContractReplayer,
    CloudFakeState,
    MiniJsonSchema,
    build_high_risk_confirmation_outputs,
    evaluate_frontend_state,
    evaluate_pcm_gate,
    json_frame_size,
    load_json,
    load_jsonl,
    make_cloud_fake_app,
    make_reply_with_size,
)


def test_r5_2_2_manifest_and_package_counts_are_the_g2_baseline() -> None:
    manifest = load_json(MANIFEST_PATH)

    assert manifest["version"] == "xiaoge-duplex-protocol-r5.2.2"
    assert MANIFEST_SHA256 == "845F0F4125061FF37A7F4DA20E0C88BC089200A08B319F1035D6522C80B56559"
    assert manifest["validation"]["result"] == "PASS"
    assert manifest["validation"]["positive_examples"] == 17
    assert manifest["validation"]["negative_schema_fail_examples"] == 12
    assert manifest["validation"]["negative_semantic_or_transport_examples"] == 3
    assert manifest["validation"]["source_reconciliation"]["close_code_cases_checked"] == 11
    assert 4001 not in manifest["validation"]["source_reconciliation"]["close_codes_covered"]
    assert manifest["gate"]["g2"] == "blocked until G1 review; only mock/test code when approved"


def test_r5_2_2_examples_replay_schema_semantic_and_transport_contract() -> None:
    protocol_schema = load_json(PROTOCOL_SCHEMA_PATH)
    replayer = CloudContractReplayer(protocol_schema)
    examples = load_jsonl(EXAMPLES_PATH)

    positive_count = 0
    schema_fail_count = 0
    semantic_or_transport_fail_count = 0

    for example in examples:
        expect = example["expect"]
        result = replayer.replay_example(example)
        if expect["schema"] == "pass":
            assert result.schema_pass, example["id"]
            positive_count += 1
        else:
            assert not result.schema_pass, example["id"]
            schema_fail_count += 1
            continue

        if expect.get("semantic") == "fail":
            assert result.semantic_pass is False, example["id"]
            semantic_or_transport_fail_count += 1
        elif expect.get("transport") == "fail":
            assert result.transport_pass is False, example["id"]
            semantic_or_transport_fail_count += 1
        else:
            assert result.semantic_pass is True, example["id"]

    assert positive_count == 20
    assert schema_fail_count == 12
    assert semantic_or_transport_fail_count == 3


def test_multi_command_blocked_example_is_reply_only_without_cmd_or_side_effect() -> None:
    protocol_schema = load_json(PROTOCOL_SCHEMA_PATH)
    replayer = CloudContractReplayer(protocol_schema)
    examples = {row["id"]: row for row in load_jsonl(EXAMPLES_PATH)}

    example = examples["data.reply.multi_command_blocked.ask_split"]
    result = replayer.replay_example(example)

    assert result.schema_pass is True
    assert result.semantic_pass is True
    assert result.output_types == ["data.reply"]
    assert result.generated_cmd_ids == []
    assert result.side_effects == []
    assert example["expect"]["forbidden_types"] == ["data.cmd"]
    assert example["expect"]["no_cmd_id"] is True
    assert example["expect"]["no_side_effects"] is True


def test_json_text_frame_boundary_8192_pass_8193_fail() -> None:
    protocol_schema = load_json(PROTOCOL_SCHEMA_PATH)
    validator = MiniJsonSchema(protocol_schema)
    max_bytes = int(protocol_schema["x-frame-json-max-bytes"])

    pass_payload = make_reply_with_size(max_bytes)
    fail_payload = make_reply_with_size(max_bytes + 1)

    assert json_frame_size(pass_payload) == 8192
    assert validator.is_valid_ref("#/$defs/dataReply", pass_payload)
    assert json_frame_size(pass_payload) <= max_bytes

    assert json_frame_size(fail_payload) == 8193
    assert validator.is_valid_ref("#/$defs/dataReply", fail_payload)
    assert json_frame_size(fail_payload) > max_bytes


def test_registry_schema_freezes_cloud_and_endpoint_delivery_boundaries() -> None:
    registry_schema = load_json(REGISTRY_SCHEMA_PATH)
    executable_deliveries = registry_schema["x-client-executable-deliveries"]
    reply_only_deliveries = registry_schema["x-cloud-reply-only-deliveries"]
    param_type_enum = registry_schema["$defs"]["paramSpec"]["properties"]["type"]["enum"]

    assert executable_deliveries == ["data.cmd", "data.cmd after confirmation"]
    assert "cloud_tool + data.reply" in reply_only_deliveries
    assert "cloud_knowledge + data.reply" in reply_only_deliveries
    assert "ask_split only" in reply_only_deliveries
    assert param_type_enum == ["enum", "int"]


def test_registry_delivery_mapping_is_explicit_for_g2_executor_and_cloud_paths() -> None:
    registry_schema = load_json(REGISTRY_SCHEMA_PATH)
    delivery_enum = registry_schema["$defs"]["commandEntry"]["properties"]["delivery"]["enum"]

    executable = set(registry_schema["x-client-executable-deliveries"])
    cloud_reply_only = set(registry_schema["x-cloud-reply-only-deliveries"])
    owner_split = {"ctrl.set/config API", "data.cmd or ctrl.set by owner"}

    assert set(delivery_enum) == executable | cloud_reply_only | owner_split
    assert executable == {"data.cmd", "data.cmd after confirmation"}
    assert cloud_reply_only == {
        "cloud_tool + data.reply",
        "cloud_knowledge + data.reply",
        "ask_split only",
    }
    assert "ctrl.set/config API" not in executable
    assert "data.cmd or ctrl.set by owner" not in executable


def test_high_risk_confirmation_blocks_cmd_before_user_confirms() -> None:
    protocol_schema = load_json(PROTOCOL_SCHEMA_PATH)
    validator = MiniJsonSchema(protocol_schema)

    before_confirmation = build_high_risk_confirmation_outputs(state="awaiting_confirmation")
    assert [frame["type"] for frame in before_confirmation] == ["data.reply"]
    assert all("cmd_id" not in frame for frame in before_confirmation)
    assert all(frame["type"] != "data.cmd" for frame in before_confirmation)
    validator.assert_valid_ref("#/$defs/dataReply", before_confirmation[0])


def test_high_risk_confirmation_cancel_clears_pending_without_cmd() -> None:
    protocol_schema = load_json(PROTOCOL_SCHEMA_PATH)
    validator = MiniJsonSchema(protocol_schema)

    canceled = build_high_risk_confirmation_outputs(state="canceled")
    assert [frame["type"] for frame in canceled] == ["data.reply"]
    assert all("cmd_id" not in frame for frame in canceled)
    assert all(frame["type"] != "data.cmd" for frame in canceled)
    assert "取消" in canceled[0]["text"]
    validator.assert_valid_ref("#/$defs/dataReply", canceled[0])


def test_high_risk_confirmation_timeout_clears_pending_without_cmd() -> None:
    protocol_schema = load_json(PROTOCOL_SCHEMA_PATH)
    validator = MiniJsonSchema(protocol_schema)

    timed_out = build_high_risk_confirmation_outputs(state="timeout")
    assert [frame["type"] for frame in timed_out] == ["data.reply"]
    assert all("cmd_id" not in frame for frame in timed_out)
    assert all(frame["type"] != "data.cmd" for frame in timed_out)
    assert "超时" in timed_out[0]["text"]
    validator.assert_valid_ref("#/$defs/dataReply", timed_out[0])


def test_high_risk_confirmation_dispatches_cmd_after_user_confirms() -> None:
    protocol_schema = load_json(PROTOCOL_SCHEMA_PATH)
    validator = MiniJsonSchema(protocol_schema)

    after_confirmation = build_high_risk_confirmation_outputs(state="confirmed")
    assert [frame["type"] for frame in after_confirmation] == ["data.cmd"]
    assert after_confirmation[0]["risk_level"] == "high"
    assert after_confirmation[0]["action"] == "system.reboot"
    validator.assert_valid_ref("#/$defs/dataCmd", after_confirmation[0])


def test_frontend_state_receive_strategy_trust_ttl_and_seq() -> None:
    protocol_schema = load_json(PROTOCOL_SCHEMA_PATH)
    validator = MiniJsonSchema(protocol_schema)
    base = {
        "type": "ctrl.frontend_state",
        "trace_id": "trace-g2-frontend",
        "session_id": "sess-g2-0001",
        "seq": 10,
        "ts_ms": 1789000000000,
        "ttl_ms": 1000,
        "trust_level": "authoritative",
        "wake_event": "local_kws",
        "wake_state": "awake",
        "vad": "speech",
        "doa": 15,
        "lock_mode": False,
    }

    authoritative = evaluate_frontend_state(
        base, last_seq=9, now_ms=1789000000500, validator=validator
    )
    assert authoritative.accepted is True
    assert authoritative.handling == "authoritative_state"

    hint_payload = dict(base, seq=11, trust_level="hint")
    hint = evaluate_frontend_state(
        hint_payload, last_seq=10, now_ms=1789000000500, validator=validator
    )
    assert hint.accepted is True
    assert hint.handling == "hint_only"

    observe_payload = dict(base, seq=12, trust_level="observe")
    observe = evaluate_frontend_state(
        observe_payload, last_seq=11, now_ms=1789000000500, validator=validator
    )
    assert observe.accepted is True
    assert observe.handling == "observation_only"

    repeated_seq = evaluate_frontend_state(
        dict(base, seq=10), last_seq=10, now_ms=1789000000500, validator=validator
    )
    assert repeated_seq.accepted is False
    assert repeated_seq.reason == "seq_not_increasing"

    expired = evaluate_frontend_state(
        dict(base, seq=13), last_seq=12, now_ms=1789000001001, validator=validator
    )
    assert expired.accepted is False
    assert expired.reason == "ttl_expired"


def test_sleeping_pcm_gate_drops_audio_before_asr_llm_or_context() -> None:
    sleeping = evaluate_pcm_gate(interaction_mode="sleeping", engine_gate="closed")
    assert sleeping.accepted is False
    assert sleeping.enters_asr is False
    assert sleeping.enters_llm is False
    assert sleeping.enters_context is False

    awake = evaluate_pcm_gate(interaction_mode="active", engine_gate="open")
    assert awake.accepted is True
    assert awake.enters_asr is True
    assert awake.enters_llm is True
    assert awake.enters_context is True


def test_close_code_replay_contract_excludes_legacy_4001() -> None:
    cases = load_jsonl(CLOSE_CODES_PATH)

    assert len(cases) == 11
    for case in cases:
        expect = case["expect"]
        assert expect.get("ws_close_code") != 4001, case["id"]
        assert case["source_code"] != "affinity_lost", case["id"]

    by_id = {case["id"]: case for case in cases}
    assert by_id["wss.auth.no_token"]["expect"]["ws_close_code"] == 4401
    assert by_id["wss.auth.permission_denied"]["expect"]["ws_close_code"] == 4403
    assert by_id["wss.auth.duplicate_connection"]["expect"]["ws_close_code"] == 4009
    assert by_id["wss.message.protocol_error"]["expect"]["ws_close_code"] == 4400
    assert by_id["wss.runtime.busy"]["expect"]["ws_close_code"] == 1013
    assert by_id["https.create_session.auth_failed"]["expect"]["http_status"] == 401
    assert by_id["https.create_session.permission_denied"]["expect"]["http_status"] == 403
    assert by_id["https.create_session.resource_exhausted"]["expect"]["http_status"] == 503


async def _create_session(base_url: str, payload: dict[str, Any]) -> dict[str, Any]:
    async with aiohttp.ClientSession() as client:
        async with client.post(f"{base_url}/create_session", json=payload) as response:
            assert response.status == 200
            body = await response.json()
    assert isinstance(body, dict)
    return body


def _valid_create_session_request() -> dict[str, Any]:
    return {
        "device_id": "robot-x3-001",
        "credential": {"key_id": "dev-key", "signature": "hmac-signature"},
        "caps": ["audio", "text", "cmd", "state"],
        "prefs": {"welcome.enabled": True},
        "audio_format": {"sample_rate": 16000, "channels": 1, "sample_format": "int16le"},
        "client_version": "x3-sdk-r5.2.2",
    }


def test_cloud_fake_server_create_session_http_contracts() -> None:
    async def _main() -> None:
        state = CloudFakeState()
        server = TestServer(make_cloud_fake_app(state))
        await server.start_server()
        base_url = f"http://127.0.0.1:{server.port}"
        try:
            async with aiohttp.ClientSession() as client:
                async with client.post(
                    f"{base_url}/create_session", json=_valid_create_session_request()
                ) as response:
                    assert response.status == 200
                    body = await response.json()
                    assert body["type"] == "session.created"
                    assert body["access_token"].startswith("tok-")
                    assert body["granted_caps"] == ["audio", "text", "cmd", "state"]

                bad_auth = _valid_create_session_request()
                bad_auth["credential"] = {"key_id": "bad", "signature": "hmac-signature"}
                async with client.post(f"{base_url}/create_session", json=bad_auth) as response:
                    assert response.status == 401

                denied = _valid_create_session_request()
                denied["caps"] = ["audio", "text", "state"]
                async with client.post(f"{base_url}/create_session", json=denied) as response:
                    assert response.status == 403

                state.force_busy = True
                async with client.post(
                    f"{base_url}/create_session", json=_valid_create_session_request()
                ) as response:
                    assert response.status == 503
        finally:
            await server.close()

    import asyncio

    asyncio.run(_main())


def test_cloud_fake_server_wss_bearer_auth_and_duplicate_close_codes() -> None:
    async def _main() -> None:
        state = CloudFakeState()
        server = TestServer(make_cloud_fake_app(state))
        await server.start_server()
        base_url = f"http://127.0.0.1:{server.port}"
        try:
            session = await _create_session(base_url, _valid_create_session_request())
            token = session["access_token"]
            ws_url = f"{base_url}/ws/session"

            async with aiohttp.ClientSession() as client:
                no_token = await client.ws_connect(ws_url)
                assert (await no_token.receive()).type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                )
                assert no_token.close_code == 4401

                invalid = await client.ws_connect(ws_url, headers={"Authorization": "Bearer bad"})
                assert (await invalid.receive()).type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                )
                assert invalid.close_code == 4401

                first = await client.ws_connect(
                    ws_url, headers={"Authorization": f"Bearer {token}"}
                )
                duplicate = await client.ws_connect(
                    ws_url, headers={"Authorization": f"Bearer {token}"}
                )
                assert (await duplicate.receive()).type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                )
                assert duplicate.close_code == 4009
                await first.close()
        finally:
            await server.close()

    import asyncio

    asyncio.run(_main())


def test_cloud_fake_server_command_loop_ack_and_result_contract() -> None:
    async def _main() -> None:
        state = CloudFakeState()
        server = TestServer(make_cloud_fake_app(state))
        await server.start_server()
        base_url = f"http://127.0.0.1:{server.port}"
        try:
            session = await _create_session(base_url, _valid_create_session_request())
            async with aiohttp.ClientSession() as client:
                ws = await client.ws_connect(
                    f"{base_url}/ws/session",
                    headers={"Authorization": f"Bearer {session['access_token']}"},
                )
                hello = {
                    "type": "ctrl.hello",
                    "trace_id": session["trace_id"],
                    "session_id": session["session_id"],
                    "proto": 2,
                    "role": "device",
                    "device_id": "robot-x3-001",
                    "caps": ["audio", "text", "cmd", "state"],
                    "prefs": {"welcome.enabled": True},
                }
                await ws.send_str(json.dumps(hello, ensure_ascii=False))

                ready = json.loads((await ws.receive()).data)
                assert ready["type"] == "ctrl.ready"
                cmd = json.loads((await ws.receive()).data)
                assert cmd["type"] == "data.cmd"
                assert cmd["cmd_id"] == "cmd-g2-0001"

                ack = {
                    "type": "data.cmd_ack",
                    "trace_id": cmd["trace_id"],
                    "session_id": cmd["session_id"],
                    "utterance_id": cmd["utterance_id"],
                    "cmd_id": cmd["cmd_id"],
                    "status": "accepted",
                    "code": "sdk_received",
                    "message": "accepted by SDK",
                    "received_at_ms": 1789000001120,
                }
                running = {
                    "type": "data.cmd_result",
                    "trace_id": cmd["trace_id"],
                    "session_id": cmd["session_id"],
                    "utterance_id": cmd["utterance_id"],
                    "cmd_id": cmd["cmd_id"],
                    "status": "running",
                    "code": "executor_started",
                    "message": "executing",
                    "started_at_ms": 1789000001200,
                }
                succeeded = {
                    "type": "data.cmd_result",
                    "trace_id": cmd["trace_id"],
                    "session_id": cmd["session_id"],
                    "utterance_id": cmd["utterance_id"],
                    "cmd_id": cmd["cmd_id"],
                    "status": "succeeded",
                    "code": "done",
                    "message": "completed",
                    "started_at_ms": 1789000001200,
                    "finished_at_ms": 1789000002400,
                    "duration_ms": 1200,
                }
                await ws.send_str(json.dumps(ack, ensure_ascii=False))
                await ws.send_str(json.dumps(running, ensure_ascii=False))
                await ws.send_str(json.dumps(succeeded, ensure_ascii=False))
                await ws.close()

            event_types = [event["type"] for event in state.events]
            assert event_types == [
                "ctrl.hello",
                "data.cmd_ack",
                "data.cmd_result",
                "data.cmd_result",
            ]
            assert state.events[1]["status"] == "accepted"
            assert state.events[2]["status"] == "running"
            assert state.events[3]["status"] == "succeeded"
        finally:
            await server.close()

    import asyncio

    asyncio.run(_main())


def test_cloud_fake_server_rejects_8193_byte_text_frame_with_protocol_error() -> None:
    async def _main() -> None:
        state = CloudFakeState()
        server = TestServer(make_cloud_fake_app(state))
        await server.start_server()
        base_url = f"http://127.0.0.1:{server.port}"
        try:
            session = await _create_session(base_url, _valid_create_session_request())
            async with aiohttp.ClientSession() as client:
                ws = await client.ws_connect(
                    f"{base_url}/ws/session",
                    headers={"Authorization": f"Bearer {session['access_token']}"},
                )
                await ws.send_str(json.dumps(make_reply_with_size(8193), ensure_ascii=False))
                message = await ws.receive()
                assert message.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                )
                assert ws.close_code == 4400
        finally:
            await server.close()

    import asyncio

    asyncio.run(_main())


@pytest.mark.parametrize(
    ("payload", "close_code"),
    [
        (
            {
                "type": "ctrl.hello",
                "trace_id": "trace-1",
                "session_id": "sess-1",
                "proto": 2,
                "role": "device",
                "device_id": "robot-x3-001",
                "caps": ["audio", "vision"],
            },
            4400,
        ),
        (
            {
                "type": "ctrl.set",
                "req_id": "r-3",
                "set": {"cmd.ack": "off"},
            },
            4400,
        ),
        # P1-01: data.cmd_ack invalid cases — must be rejected with 4400 protocol_error.
        (
            {
                "type": "data.cmd_ack",
                "trace_id": "trace-ack-bad-status",
                "session_id": "sess-1",
                "utterance_id": "utt-1",
                "cmd_id": "cmd-1",
                "status": "ok",  # not in {accepted, rejected, duplicate}
                "code": "OK",
                "received_at_ms": 1,
            },
            4400,
        ),
        (
            {
                "type": "data.cmd_ack",
                "trace_id": "trace-ack-missing-received-at",
                "session_id": "sess-1",
                "utterance_id": "utt-1",
                "cmd_id": "cmd-1",
                "status": "accepted",
                "code": "OK",
                # received_at_ms missing
            },
            4400,
        ),
        (
            {
                "type": "data.cmd_ack",
                "trace_id": "trace-ack-empty-cmd-id",
                "session_id": "sess-1",
                "utterance_id": "utt-1",
                "cmd_id": "",  # empty cmd_id violates minLength:1
                "status": "accepted",
                "code": "OK",
                "received_at_ms": 1,
            },
            4400,
        ),
        (
            {
                "type": "data.cmd_ack",
                "trace_id": "trace-ack-missing-code",
                "session_id": "sess-1",
                "utterance_id": "utt-1",
                "cmd_id": "cmd-1",
                "status": "rejected",
                # code missing
                "received_at_ms": 1,
            },
            4400,
        ),
        (
            {
                "type": "data.cmd_ack",
                "trace_id": "trace-ack-bool-ms",
                "session_id": "sess-1",
                "utterance_id": "utt-1",
                "cmd_id": "cmd-1",
                "status": "accepted",
                "code": "OK",
                "received_at_ms": True,  # bool must not be accepted as int
            },
            4400,
        ),
        # P1-01: data.cmd_result invalid cases — must be rejected with 4400 protocol_error.
        (
            {
                "type": "data.cmd_result",
                "trace_id": "trace-result-bad-status",
                "session_id": "sess-1",
                "utterance_id": "utt-1",
                "cmd_id": "cmd-1",
                "status": "success",  # not in {running, succeeded, failed, canceled, timeout}
                "code": "OK",
            },
            4400,
        ),
        (
            {
                "type": "data.cmd_result",
                "trace_id": "trace-result-missing-code",
                "session_id": "sess-1",
                "utterance_id": "utt-1",
                "cmd_id": "cmd-1",
                "status": "succeeded",
                # code missing
            },
            4400,
        ),
        (
            {
                "type": "data.cmd_result",
                "trace_id": "trace-result-empty-cmd-id",
                "session_id": "sess-1",
                "utterance_id": "utt-1",
                "cmd_id": "",  # empty cmd_id violates minLength:1
                "status": "failed",
                "code": "ERR",
            },
            4400,
        ),
        (
            {
                "type": "data.cmd_result",
                "trace_id": "trace-result-missing-utterance",
                "session_id": "sess-1",
                # utterance_id missing
                "cmd_id": "cmd-1",
                "status": "running",
                "code": "OK",
            },
            4400,
        ),
    ],
)
def test_cloud_fake_server_rejects_non_p0_or_invalid_frames(
    payload: dict[str, Any], close_code: int
) -> None:
    async def _main() -> None:
        state = CloudFakeState()
        server = TestServer(make_cloud_fake_app(state))
        await server.start_server()
        base_url = f"http://127.0.0.1:{server.port}"
        try:
            session = await _create_session(base_url, _valid_create_session_request())
            async with aiohttp.ClientSession() as client:
                ws = await client.ws_connect(
                    f"{base_url}/ws/session",
                    headers={"Authorization": f"Bearer {session['access_token']}"},
                )
                payload["trace_id"] = payload.get("trace_id", session["trace_id"])
                payload["session_id"] = payload.get("session_id", session["session_id"])
                await ws.send_str(json.dumps(payload, ensure_ascii=False))
                message = await ws.receive()
                assert message.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                )
                assert ws.close_code == close_code
        finally:
            await server.close()

    import asyncio

    asyncio.run(_main())
