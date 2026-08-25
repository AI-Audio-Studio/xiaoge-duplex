from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
import types
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp.test_utils import TestServer

from livekit import rtc

ROOT = Path(__file__).resolve().parents[1]
VOICE_AGENTS = ROOT / "examples" / "voice_agents"
if str(VOICE_AGENTS) not in sys.path:
    sys.path.insert(0, str(VOICE_AGENTS))

dashscope = types.ModuleType("dashscope")
dashscope.api_key = ""
qwen_module = types.ModuleType("dashscope.audio.qwen_tts_realtime.qwen_tts_realtime")
tts_v2_module = types.ModuleType("dashscope.audio.tts_v2")


class _DummyAudioFormat:
    PCM_24000HZ_MONO_16BIT = "pcm"


class _DummyQwenTtsRealtime:
    def __init__(self, *args, **kwargs) -> None:
        pass


class _DummyQwenTtsRealtimeCallback:
    pass


qwen_module.AudioFormat = _DummyAudioFormat
qwen_module.QwenTtsRealtime = _DummyQwenTtsRealtime
qwen_module.QwenTtsRealtimeCallback = _DummyQwenTtsRealtimeCallback
tts_v2_module.AudioFormat = types.SimpleNamespace(
    PCM_16000HZ_MONO_16BIT="pcm16",
    PCM_22050HZ_MONO_16BIT="pcm22",
    PCM_24000HZ_MONO_16BIT="pcm24",
    PCM_48000HZ_MONO_16BIT="pcm48",
)
tts_v2_module.ResultCallback = object
tts_v2_module.SpeechSynthesizer = object
tts_v2_module.SpeechSynthesizerObjectPool = object
sys.modules.setdefault("dashscope", dashscope)
sys.modules.setdefault("dashscope.audio", types.ModuleType("dashscope.audio"))
sys.modules.setdefault(
    "dashscope.audio.qwen_tts_realtime",
    types.ModuleType("dashscope.audio.qwen_tts_realtime"),
)
sys.modules.setdefault("dashscope.audio.qwen_tts_realtime.qwen_tts_realtime", qwen_module)
sys.modules.setdefault("dashscope.audio.tts_v2", tts_v2_module)
sys.modules.setdefault(
    "websocket",
    types.SimpleNamespace(WebSocketConnectionClosedException=RuntimeError),
)

import app.web_audio as web_audio  # noqa: E402
import webpanel.bridge as web_bridge  # noqa: E402
import webpanel.server as web_server  # noqa: E402
from app.session_state import COMMAND_SUCCESS, runtime  # noqa: E402
from app.web_audio import WebSocketAudioOutput  # noqa: E402
from common.g3_intent import G3IntentEngine, SessionState  # noqa: E402
from gateway import affinity as af, main as gwmain  # noqa: E402
from gateway.apikey import ApiKeyStore  # noqa: E402
from gateway.config import GatewayConfig  # noqa: E402
from gateway.proxy import Proxy  # noqa: E402
from webpanel.bridge import broadcast  # noqa: E402
from webpanel.command_lifecycle import CommandLifecycleTracker  # noqa: E402
from webpanel.server import build_web_app  # noqa: E402
from webpanel.state import panel  # noqa: E402


def _create_session_payload() -> dict[str, Any]:
    return {
        "device_id": "robot-x3-001",
        "credential": {"key_id": "dev-key", "signature": "hmac-signature"},
        "caps": ["audio", "text", "cmd", "state"],
        "prefs": {"welcome.enabled": True},
        "audio_format": {"sample_rate": 16000, "channels": 1, "sample_format": "int16le"},
        "client_version": "x3-sdk-r5.2.2",
    }


async def _open_session(
    base_url: str,
) -> tuple[aiohttp.ClientSession, aiohttp.ClientWebSocketResponse, dict]:
    client = aiohttp.ClientSession()
    response = await client.post(f"{base_url}/create_session", json=_create_session_payload())
    assert response.status == 200
    session = await response.json()
    ws = await client.ws_connect(
        f"{base_url}/ws/session",
        headers={"Authorization": f"Bearer {session['access_token']}"},
    )
    return client, ws, session


def test_webpanel_create_session_and_merged_ws_ctrl_handshake() -> None:
    async def _main() -> None:
        server = TestServer(build_web_app(admin_routes=False, web_audio=True))
        await server.start_server()
        base_url = f"http://127.0.0.1:{server.port}"
        try:
            client, ws, session = await _open_session(base_url)
            try:
                await ws.send_str(
                    json.dumps(
                        {
                            "type": "ctrl.hello",
                            "trace_id": session["trace_id"],
                            "session_id": session["session_id"],
                            "proto": 2,
                            "role": "device",
                            "device_id": "robot-x3-001",
                            "caps": ["audio", "text", "cmd", "state"],
                        },
                        ensure_ascii=False,
                    )
                )
                ready = json.loads((await ws.receive()).data)
                state = json.loads((await ws.receive()).data)
                assert ready["type"] == "ctrl.ready"
                assert ready["granted_caps"] == ["audio", "text", "cmd", "state"]
                assert state["type"] == "ctrl.state"
                assert state["link_state"] == "connected"
            finally:
                await ws.close()
                await client.close()
        finally:
            await server.close()

    asyncio.run(_main())


def test_webpanel_rejects_client_upstream_ctrl_clear() -> None:
    async def _main() -> None:
        server = TestServer(build_web_app(admin_routes=False, web_audio=True))
        await server.start_server()
        base_url = f"http://127.0.0.1:{server.port}"
        try:
            client, ws, session = await _open_session(base_url)
            try:
                await ws.send_str(
                    json.dumps(
                        {
                            "type": "ctrl.clear",
                            "trace_id": session["trace_id"],
                            "session_id": session["session_id"],
                            "reason": "barge_in",
                        },
                        ensure_ascii=False,
                    )
                )
                closed = await ws.receive()
                assert closed.type == aiohttp.WSMsgType.CLOSE
                assert ws.close_code == 4400
            finally:
                await ws.close()
                await client.close()
        finally:
            await server.close()

    asyncio.run(_main())


def test_ws_audio_output_drops_stale_pcm_immediately_after_clear(monkeypatch) -> None:
    sent_audio: list[bytes] = []
    sent_ctrl: list[dict] = []
    monkeypatch.setenv("XIAOGE_CLEAR_SUPPRESS_TAIL_MS", "1000")
    monkeypatch.setattr(web_audio, "broadcast_audio", sent_audio.append)
    monkeypatch.setattr(web_audio, "broadcast_audio_ctrl", sent_ctrl.append)

    async def _main() -> None:
        output = WebSocketAudioOutput(None)
        frame = rtc.AudioFrame(
            data=b"\x01\x02" * 160,
            sample_rate=16_000,
            num_channels=1,
            samples_per_channel=160,
        )

        await output.capture_frame(frame)
        assert sent_audio == [b"\x01\x02" * 160]

        output.clear_buffer()
        await output.capture_frame(frame)
        assert sent_ctrl == [{"type": "clear"}]
        assert sent_audio == [b"\x01\x02" * 160]

        output._drop_audio_until = 0.0
        await output.capture_frame(frame)
        assert sent_audio == [b"\x01\x02" * 160, b"\x01\x02" * 160]

    asyncio.run(_main())


def test_ws_audio_output_allows_fresh_pcm_after_music_clear(monkeypatch) -> None:
    sent_audio: list[bytes] = []
    sent_ctrl: list[dict] = []
    monkeypatch.setenv("XIAOGE_CLEAR_SUPPRESS_TAIL_MS", "1000")
    monkeypatch.setattr(web_audio, "broadcast_audio", sent_audio.append)
    monkeypatch.setattr(web_audio, "broadcast_audio_ctrl", sent_ctrl.append)

    async def _main() -> None:
        output = WebSocketAudioOutput(None)
        frame = rtc.AudioFrame(
            data=b"\x01\x02" * 160,
            sample_rate=16_000,
            num_channels=1,
            samples_per_channel=160,
        )

        await output.capture_frame(frame)
        output.clear_music_buffer()
        await output.capture_frame(frame)

        assert sent_ctrl == [{"type": "clear"}]
        assert sent_audio == [b"\x01\x02" * 160, b"\x01\x02" * 160]

    asyncio.run(_main())


def test_webpanel_ws_session_rejects_missing_bearer() -> None:
    async def _main() -> None:
        server = TestServer(build_web_app(admin_routes=False, web_audio=True))
        await server.start_server()
        try:
            async with aiohttp.ClientSession() as client:
                ws = await client.ws_connect(f"http://127.0.0.1:{server.port}/ws/session")
                assert (await ws.receive()).type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                )
                assert ws.close_code == 4401
        finally:
            await server.close()

    asyncio.run(_main())


def test_webpanel_ws_session_is_bearer_only_and_supports_header_reconnect() -> None:
    async def _main() -> None:
        server = TestServer(build_web_app(admin_routes=False, web_audio=True))
        await server.start_server()
        base_url = f"http://127.0.0.1:{server.port}"
        try:
            async with aiohttp.ClientSession() as client:
                response = await client.post(
                    f"{base_url}/create_session", json=_create_session_payload()
                )
                session = await response.json()
                token = session["access_token"]

                query_only = await client.ws_connect(f"{base_url}/ws/session?access_token={token}")
                assert (await query_only.receive()).type == aiohttp.WSMsgType.CLOSE
                assert query_only.close_code == 4401

                mixed = await client.ws_connect(
                    f"{base_url}/ws/session?access_token={token}",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert (await mixed.receive()).type == aiohttp.WSMsgType.CLOSE
                assert mixed.close_code == 4401

                hello = {
                    "type": "ctrl.hello",
                    "trace_id": session["trace_id"],
                    "session_id": session["session_id"],
                    "proto": 2,
                    "role": "device",
                    "device_id": "robot-x3-001",
                    "caps": ["audio", "text", "cmd", "state"],
                }
                headers = {"Authorization": f"Bearer {token}"}
                first = await client.ws_connect(f"{base_url}/ws/session", headers=headers)
                await first.send_str(json.dumps(hello, ensure_ascii=False))
                assert json.loads((await first.receive()).data)["type"] == "ctrl.ready"
                await first.close()
                await asyncio.sleep(0.01)

                reconnected = await client.ws_connect(f"{base_url}/ws/session", headers=headers)
                await reconnected.send_str(json.dumps(hello, ensure_ascii=False))
                assert json.loads((await reconnected.receive()).data)["type"] == "ctrl.ready"
                await reconnected.close()

                debug_route = await client.get(f"{base_url}/debug/ws/session")
                assert debug_route.status == 404
        finally:
            await server.close()

    asyncio.run(_main())


def test_bridge_preserves_and_correlates_user_turn_identity() -> None:
    web_bridge._fallback_turn_id = None

    explicit_partial = web_bridge._to_session_frames(
        {"type": "user_partial", "text": "one", "utterance_id": "utt-explicit"}
    )[0]
    explicit_final = web_bridge._to_session_frames(
        {
            "type": "message",
            "role": "user",
            "text": "one final",
            "utterance_id": "utt-explicit",
        }
    )[0]
    assert explicit_partial["utterance_id"] == "utt-explicit"
    assert explicit_final["utterance_id"] == "utt-explicit"
    assert explicit_partial["final"] is False
    assert explicit_final["final"] is True

    legacy_partial_msg = {"type": "user_partial", "text": "legacy one"}
    legacy_partial = web_bridge._to_session_frames(legacy_partial_msg)[0]
    legacy_final_msg = {"type": "message", "role": "user", "text": "legacy final"}
    legacy_final = web_bridge._to_session_frames(legacy_final_msg)[0]
    next_final = web_bridge._to_session_frames(
        {"type": "message", "role": "user", "text": "next final"}
    )[0]

    legacy_id = legacy_partial["utterance_id"]
    assert legacy_id
    assert legacy_partial_msg["utterance_id"] == legacy_id
    assert legacy_final_msg["utterance_id"] == legacy_id
    assert legacy_final["utterance_id"] == legacy_id
    assert next_final["utterance_id"] != legacy_id


def test_bridge_explicit_final_clears_stale_legacy_fallback() -> None:
    web_bridge._fallback_turn_id = None
    stale = web_bridge._to_session_frames({"type": "user_partial", "text": "legacy"})[0][
        "utterance_id"
    ]
    explicit = web_bridge._to_session_frames(
        {
            "type": "message",
            "role": "user",
            "text": "authoritative",
            "utterance_id": "utt-authoritative",
        }
    )[0]
    following = web_bridge._to_session_frames(
        {"type": "message", "role": "user", "text": "following"}
    )[0]

    assert explicit["utterance_id"] == "utt-authoritative"
    assert following["utterance_id"] not in {stale, "utt-authoritative"}


def test_bridge_translates_user_turn_once_for_all_session_recipients() -> None:
    class Recipient:
        def __init__(self) -> None:
            self.messages: list[str] = []

        async def send_str(self, data: str) -> None:
            self.messages.append(data)

    async def _main() -> None:
        first = Recipient()
        second = Recipient()
        previous = panel.session_ws_clients
        panel.session_ws_clients = {first, second}  # type: ignore[assignment]
        web_bridge._fallback_turn_id = None
        try:
            message = {"type": "user_partial", "text": "shared"}
            wire_messages = web_bridge._to_session_messages(message)
            await web_bridge._ws_session_broadcast(wire_messages)
        finally:
            panel.session_ws_clients = previous

        assert message["utterance_id"]
        assert first.messages == second.messages
        assert {json.loads(data)["utterance_id"] for data in first.messages} == {
            message["utterance_id"]
        }

    asyncio.run(_main())


def test_webpanel_debug_demo_requires_user_key_and_uses_isolated_route() -> None:
    async def _main() -> None:
        server = TestServer(
            build_web_app(admin_routes=False, web_audio=True, debug_query_token=True)
        )
        await server.start_server()
        base_url = f"http://127.0.0.1:{server.port}"
        try:
            async with aiohttp.ClientSession() as client:
                page = await client.get(f"{base_url}/")
                html = await page.text()
                assert 'id="apiKeyInput"' in html
                assert "var DEMO_QUERY_TOKEN_ENABLED=true;" in html
                assert "DEFAULT_RUOYI_API_KEY" not in html
                assert "xiaoge.webpanel.ruoyi_api_key.v1" in html
                assert "localStorage.getItem(RUOYI_API_KEY_STORE)" in html
                assert "localStorage.setItem(RUOYI_API_KEY_STORE, value)" in html
                assert "localStorage.removeItem(RUOYI_API_KEY_STORE)" in html
                assert "params.get('apikey')" not in html
                assert "location.search" not in html
                assert "location.hash" not in html
                assert "/debug/ws/session?access_token=" in html
                assert "simulateEndpointCommand(m);" in html
                assert "type:'data.cmd_ack'" in html
                assert "status:'accepted'" in html
                assert "type:'data.cmd_result'" in html
                assert "command.action==='gesture.perform'" in html
                assert "gesture==='laugh' || gesture==='cry'" in html
                assert "failed ? 'failed' : 'succeeded'" in html
                assert "}, 1000);" in html
                assert "ws===socket && protoSession===session" in html
                assert "activeAttempt===attempt && !attempt.disposed" in html
                assert "attempt.session=await createProtocolSession()" in html
                assert "var socket=attempt.ws" in html
                assert "socket.readyState===WebSocket.OPEN && !muted" in html
                assert "location.reload" not in html
                assert "setTimeout(conn" not in html
                assert "setTimeout(function(){conn" not in html
                assert "连接失败，点击重试" in html
                assert "finalizedUserTurns[key]" in html
                assert "if(finalizedUserTurns[key]) return" in html
                assert "USER_TURN_ALIAS_LIMIT=384" in html

                response = await client.post(
                    f"{base_url}/create_session", json=_create_session_payload()
                )
                session = await response.json()
                ws = await client.ws_connect(
                    f"{base_url}/debug/ws/session?access_token={session['access_token']}"
                )
                await ws.send_str(
                    json.dumps(
                        {
                            "type": "ctrl.hello",
                            "trace_id": session["trace_id"],
                            "session_id": session["session_id"],
                            "proto": 2,
                            "role": "panel",
                            "device_id": "web-panel-x3",
                            "caps": ["audio", "text", "cmd", "state"],
                        },
                        ensure_ascii=False,
                    )
                )
                assert json.loads((await ws.receive()).data)["type"] == "ctrl.ready"
                await ws.close()
        finally:
            await server.close()

    asyncio.run(_main())


def test_webpanel_rejects_hello_token_and_prevents_caps_escalation() -> None:
    async def _main() -> None:
        server = TestServer(build_web_app(admin_routes=False, web_audio=True))
        await server.start_server()
        base_url = f"http://127.0.0.1:{server.port}"
        try:
            async with aiohttp.ClientSession() as client:
                payload = _create_session_payload()
                payload["caps"] = ["audio", "text", "cmd"]
                response = await client.post(f"{base_url}/create_session", json=payload)
                session = await response.json()
                headers = {"Authorization": f"Bearer {session['access_token']}"}
                ws = await client.ws_connect(f"{base_url}/ws/session", headers=headers)
                hello = {
                    "type": "ctrl.hello",
                    "trace_id": session["trace_id"],
                    "session_id": session["session_id"],
                    "proto": 2,
                    "role": "device",
                    "device_id": "robot-x3-001",
                    "caps": ["audio", "text", "cmd", "state"],
                }
                await ws.send_str(json.dumps(hello, ensure_ascii=False))
                ready = json.loads((await ws.receive()).data)
                assert ready["granted_caps"] == ["audio", "text", "cmd"]
                try:
                    unexpected_state = await ws.receive(timeout=0.1)
                except (TimeoutError, asyncio.TimeoutError):
                    unexpected_state = None
                assert unexpected_state is None
                await ws.close()
                await asyncio.sleep(0.01)

                second_response = await client.post(
                    f"{base_url}/create_session", json=_create_session_payload()
                )
                second = await second_response.json()
                token_ws = await client.ws_connect(
                    f"{base_url}/ws/session",
                    headers={"Authorization": f"Bearer {second['access_token']}"},
                )
                hello.update(
                    {
                        "trace_id": second["trace_id"],
                        "session_id": second["session_id"],
                        "token": second["access_token"],
                    }
                )
                await token_ws.send_str(json.dumps(hello, ensure_ascii=False))
                assert (await token_ws.receive()).type == aiohttp.WSMsgType.CLOSE
                assert token_ws.close_code == 4400
        finally:
            await server.close()

    asyncio.run(_main())


def test_gateway_create_session_exchanges_ruoyi_apikey_for_xiaoge_token() -> None:
    class FakePool:
        def __init__(self, port: int) -> None:
            self._seats: list[dict[str, Any]] = [
                {"session_id": "sess-gw-auth", "proc_id": "proc-gw-auth", "port": port}
            ]
            self.alloc_calls = 0
            self.released: list[str] = []

        async def alloc(self) -> dict[str, Any] | None:
            self.alloc_calls += 1
            return self._seats.pop(0) if self._seats else None

        async def release(self, session_id: str, reason: str = "") -> bool:
            self.released.append(session_id)
            return True

        async def status(self) -> dict[str, Any]:
            return {"released": list(self.released)}

        async def close(self) -> None:
            pass

    async def _main() -> None:
        async def create_session(_: aiohttp.web.Request) -> aiohttp.web.Response:
            return aiohttp.web.json_response(
                {
                    "type": "session.created",
                    "trace_id": "trace-gw-auth",
                    "session_id": "sess-gw-auth",
                    "access_token": "agent-issued-token",
                    "expires_in_ms": 60_000,
                }
            )

        agent_app = aiohttp.web.Application()
        agent_app.router.add_post("/create_session", create_session)
        agent = TestServer(agent_app)
        await agent.start_server()
        cfg = GatewayConfig(hmac_secret="s", api_key_required=True, api_keys_static="ruoyi-api-key")
        table = af.AffinityTable(grace_seconds=10.0, secret=cfg.hmac_secret)
        proxy = Proxy(cfg, table)
        pool = FakePool(agent.port)
        gateway = TestServer(
            gwmain.build_gateway_app(
                cfg,
                table,
                proxy,
                pool,  # type: ignore[arg-type]
                ApiKeyStore(cfg),
            )
        )
        await gateway.start_server()
        base_url = f"http://127.0.0.1:{gateway.port}"
        try:
            async with aiohttp.ClientSession() as client:
                missing = await client.post(
                    f"{base_url}/create_session", json=_create_session_payload()
                )
                assert missing.status == 401
                assert (await missing.json())["code"] == "auth_failed"
                assert pool.alloc_calls == 0

                bad = await client.post(
                    f"{base_url}/create_session",
                    headers={"X-API-Key": "bad-key"},
                    json=_create_session_payload(),
                )
                assert bad.status == 401
                assert (await bad.json())["code"] == "auth_failed"
                assert pool.alloc_calls == 0

                ok = await client.post(
                    f"{base_url}/create_session",
                    headers={"X-API-Key": "ruoyi-api-key"},
                    json=_create_session_payload(),
                )
                assert ok.status == 200
                body = await ok.json()
                assert body["access_token"] == "agent-issued-token"
                assert body["ws_url"] == f"ws://127.0.0.1:{gateway.port}/ws/session"
                assert pool.alloc_calls == 1
        finally:
            await proxy.aclose()
            await gateway.close()
            await agent.close()

    asyncio.run(_main())


def test_gateway_ws_session_rejects_query_and_injects_bearer_upstream() -> None:
    class FakePool:
        def __init__(self, port: int) -> None:
            self._seat = {
                "session_id": "sess-gw-bearer",
                "proc_id": "proc-gw-bearer",
                "port": port,
            }

        async def alloc(self) -> dict[str, Any] | None:
            seat, self._seat = self._seat, None  # type: ignore[assignment]
            return seat

        async def release(self, session_id: str, reason: str = "") -> bool:
            return True

        async def status(self) -> dict[str, Any]:
            return {}

        async def close(self) -> None:
            pass

    async def _main() -> None:
        upstream_requests: list[dict[str, str]] = []

        async def create_session(_: aiohttp.web.Request) -> aiohttp.web.Response:
            return aiohttp.web.json_response(
                {
                    "type": "session.created",
                    "trace_id": "trace-gw-bearer",
                    "session_id": "sess-gw-bearer",
                    "access_token": "agent-token-gw-bearer",
                    "expires_in_ms": 60_000,
                }
            )

        async def ws_session(request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
            upstream_requests.append(
                {
                    "authorization": request.headers.get("Authorization", ""),
                    "query": request.query_string,
                }
            )
            ws = aiohttp.web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_str(
                json.dumps(
                    {
                        "type": "ctrl.ready",
                        "trace_id": "trace-gw-bearer",
                        "session_id": "sess-gw-bearer",
                    }
                )
            )
            async for message in ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                payload = json.loads(message.data)
                if payload.get("token"):
                    await ws.close(code=4400, message=b"protocol_error")
                    break
            return ws

        agent_app = aiohttp.web.Application()
        agent_app.router.add_post("/create_session", create_session)
        agent_app.router.add_get("/ws/session", ws_session)
        agent = TestServer(agent_app)
        await agent.start_server()
        cfg = GatewayConfig(hmac_secret="s")
        table = af.AffinityTable(grace_seconds=10.0, secret=cfg.hmac_secret)
        proxy = Proxy(cfg, table)
        gateway = TestServer(
            gwmain.build_gateway_app(
                cfg,
                table,
                proxy,
                FakePool(agent.port),  # type: ignore[arg-type]
                ApiKeyStore(cfg),
            )
        )
        await gateway.start_server()
        base_url = f"http://127.0.0.1:{gateway.port}"
        try:
            async with aiohttp.ClientSession() as client:
                response = await client.post(
                    f"{base_url}/create_session", json=_create_session_payload()
                )
                session = await response.json()
                token = session["access_token"]
                query_only = await client.ws_connect(f"{base_url}/ws/session?access_token={token}")
                assert (await query_only.receive()).type == aiohttp.WSMsgType.CLOSE
                assert query_only.close_code == 4401

                mixed = await client.ws_connect(
                    f"{base_url}/ws/session?access_token={token}",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert (await mixed.receive()).type == aiohttp.WSMsgType.CLOSE
                assert mixed.close_code == 4401

                ws = await client.ws_connect(
                    f"{base_url}/ws/session",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert json.loads((await ws.receive()).data)["type"] == "ctrl.ready"
                await ws.send_json(
                    {
                        "type": "ctrl.hello",
                        "trace_id": session["trace_id"],
                        "session_id": session["session_id"],
                        "token": token,
                    }
                )
                assert (await ws.receive()).type == aiohttp.WSMsgType.CLOSE
                assert ws.close_code == 4400
                assert upstream_requests == [{"authorization": f"Bearer {token}", "query": ""}]
        finally:
            await proxy.aclose()
            await gateway.close()
            await agent.close()

    asyncio.run(_main())


def test_gateway_access_log_does_not_render_query_string() -> None:
    class CaptureHandler(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.messages: list[str] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.messages.append(record.getMessage())

    logger = logging.getLogger("test-g3-path-only-access")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = CaptureHandler()
    logger.addHandler(handler)
    try:
        access_logger = gwmain._PathOnlyAccessLogger(logger, "%r")
        request = types.SimpleNamespace(
            remote="127.0.0.1",
            method="GET",
            path="/ws/session",
            query_string="access_token=must-not-appear",
        )
        response = types.SimpleNamespace(status=401)
        access_logger.log(request, response, 0.01)
        assert handler.messages == ["127.0.0.1 GET /ws/session 401 0.010s"]
        assert "access_token" not in handler.messages[0]
        assert "must-not-appear" not in handler.messages[0]
    finally:
        logger.removeHandler(handler)


def test_gateway_ws_session_duplicate_closes_4009_without_releasing_owner() -> None:
    class FakePool:
        def __init__(self, port: int) -> None:
            self._seat = {"session_id": "sess-gw-dup", "proc_id": "proc-gw-dup", "port": port}
            self.released: list[str] = []

        async def alloc(self) -> dict[str, Any] | None:
            seat, self._seat = self._seat, None  # type: ignore[assignment]
            return seat

        async def release(self, session_id: str, reason: str = "") -> bool:
            self.released.append(session_id)
            return True

        async def status(self) -> dict[str, Any]:
            return {"released": list(self.released)}

        async def close(self) -> None:
            pass

    async def _main() -> None:
        async def create_session(_: aiohttp.web.Request) -> aiohttp.web.Response:
            return aiohttp.web.json_response(
                {
                    "type": "session.created",
                    "trace_id": "trace-gw-dup",
                    "session_id": "sess-gw-dup",
                    "access_token": "agent-token-gw-dup",
                    "expires_in": 60,
                }
            )

        async def ws_session(request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
            ws = aiohttp.web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_str(
                json.dumps(
                    {
                        "type": "ctrl.ready",
                        "trace_id": "trace-gw-dup",
                        "session_id": "sess-gw-dup",
                    }
                )
            )
            async for _ in ws:
                pass
            return ws

        agent_app = aiohttp.web.Application()
        agent_app.router.add_post("/create_session", create_session)
        agent_app.router.add_get("/ws/session", ws_session)
        agent = TestServer(agent_app)
        await agent.start_server()
        cfg = GatewayConfig(hmac_secret="s")
        table = af.AffinityTable(grace_seconds=10.0, secret=cfg.hmac_secret)
        proxy = Proxy(cfg, table)
        pool = FakePool(agent.port)
        gateway = TestServer(
            gwmain.build_gateway_app(
                cfg,
                table,
                proxy,
                pool,  # type: ignore[arg-type]
                ApiKeyStore(cfg),
            )
        )
        await gateway.start_server()
        base_url = f"http://127.0.0.1:{gateway.port}"
        try:
            async with aiohttp.ClientSession() as client:
                response = await client.post(
                    f"{base_url}/create_session", json=_create_session_payload()
                )
                assert response.status == 200
                session = await response.json()
                headers = {"Authorization": f"Bearer {session['access_token']}"}
                owner = await client.ws_connect(f"{base_url}/ws/session", headers=headers)
                try:
                    assert json.loads((await owner.receive()).data)["type"] == "ctrl.ready"
                    duplicate = await client.ws_connect(f"{base_url}/ws/session", headers=headers)
                    assert (await duplicate.receive()).type in (
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSING,
                    )
                    assert duplicate.close_code == 4009
                    assert pool.released == []
                finally:
                    await owner.close()
                for _ in range(20):
                    if pool.released:
                        break
                    await asyncio.sleep(0.01)
                assert pool.released == ["sess-gw-dup"]
        finally:
            await proxy.aclose()
            await gateway.close()
            await agent.close()

    asyncio.run(_main())


def test_webpanel_welcome_waits_until_frontend_call_started(monkeypatch) -> None:
    async def _main() -> None:
        welcomed: list[str] = []
        monkeypatch.setattr(web_server, "say_voice_welcome", lambda: welcomed.append("welcome"))
        runtime.agent_loop = asyncio.get_running_loop()
        server = TestServer(build_web_app(admin_routes=False, web_audio=True))
        await server.start_server()
        base_url = f"http://127.0.0.1:{server.port}"
        try:
            client, ws, session = await _open_session(base_url)
            try:
                hello = {
                    "type": "ctrl.hello",
                    "trace_id": session["trace_id"],
                    "session_id": session["session_id"],
                    "proto": 2,
                    "role": "panel",
                    "device_id": "web-panel-x3",
                    "caps": ["audio", "text", "cmd", "state"],
                }
                await ws.send_str(json.dumps(hello, ensure_ascii=False))
                assert json.loads((await ws.receive()).data)["type"] == "ctrl.ready"
                assert json.loads((await ws.receive()).data)["type"] == "ctrl.state"
                await asyncio.sleep(0.05)
                assert welcomed == []

                call_started = {
                    "type": "ctrl.frontend_state",
                    "trace_id": session["trace_id"],
                    "session_id": session["session_id"],
                    "seq": 1,
                    "ts_ms": 1789000001000,
                    "ttl_ms": 1000,
                    "trust_level": "authoritative",
                    "wake_event": "button",
                    "wake_state": "awake",
                    "vad": "unknown",
                    "lock_mode": False,
                }
                await ws.send_str(json.dumps(call_started, ensure_ascii=False))
                await asyncio.sleep(0.05)
                assert welcomed == ["welcome"]

                call_started["seq"] = 2
                await ws.send_str(json.dumps(call_started, ensure_ascii=False))
                await asyncio.sleep(0.05)
                assert welcomed == ["welcome"]
            finally:
                await ws.close()
                await client.close()
        finally:
            runtime.agent_loop = None
            await server.close()

    asyncio.run(_main())


def test_webpanel_merged_ws_routes_manual_text_to_agent() -> None:
    async def _main() -> None:
        received: list[str] = []

        async def fake_manual_text_handler(text: str) -> None:
            received.append(text)

        runtime.agent_loop = asyncio.get_running_loop()
        runtime.manual_text_handler = fake_manual_text_handler
        server = TestServer(build_web_app(admin_routes=False, web_audio=True))
        await server.start_server()
        try:
            client, ws, session = await _open_session(f"http://127.0.0.1:{server.port}")
            try:
                await ws.send_str(
                    json.dumps(
                        {
                            "type": "ctrl.hello",
                            "trace_id": session["trace_id"],
                            "session_id": session["session_id"],
                            "proto": 2,
                            "role": "panel",
                            "device_id": "web-panel-x3",
                            "caps": ["audio", "text", "cmd", "state"],
                        },
                        ensure_ascii=False,
                    )
                )
                await ws.receive()
                await ws.receive()
                await ws.send_str(
                    json.dumps(
                        {
                            "type": "data.text",
                            "trace_id": session["trace_id"],
                            "session_id": session["session_id"],
                            "utterance_id": "utt-manual-1",
                            "text": "往前走两步",
                            "final": True,
                            "ts_ms": 1789000001000,
                        },
                        ensure_ascii=False,
                    )
                )
                await asyncio.sleep(0.05)
                assert received == ["往前走两步"]
            finally:
                await ws.close()
                await client.close()
        finally:
            runtime.agent_loop = None
            runtime.manual_text_handler = None
            await server.close()

    asyncio.run(_main())


def test_webpanel_merged_ws_multi_command_is_ask_split_reply_only() -> None:
    async def _main() -> None:
        session_info: dict[str, Any] = {}

        async def fake_manual_text_handler(text: str) -> None:
            state = SessionState(
                trace_id=session_info["trace_id"],
                session_id=session_info["session_id"],
                caps=frozenset({"audio", "text", "cmd", "state"}),
                command_dry_run=True,
                robot_action_enabled=False,
            )
            frames = G3IntentEngine().handle_text(
                text, state, utterance_id="utt-ws-multi", now_ms=1789000001000
            )
            broadcast({"type": "g3_protocol", "frames": frames, "dry_run": True})

        runtime.agent_loop = asyncio.get_running_loop()
        runtime.manual_text_handler = fake_manual_text_handler
        server = TestServer(build_web_app(admin_routes=False, web_audio=True))
        await server.start_server()
        panel.web_loop = asyncio.get_running_loop()
        try:
            client, ws, created = await _open_session(f"http://127.0.0.1:{server.port}")
            session_info.update(created)
            try:
                await ws.send_str(
                    json.dumps(
                        {
                            "type": "ctrl.hello",
                            "trace_id": created["trace_id"],
                            "session_id": created["session_id"],
                            "proto": 2,
                            "role": "device",
                            "device_id": "robot-x3-001",
                            "caps": ["audio", "text", "cmd", "state"],
                        },
                        ensure_ascii=False,
                    )
                )
                assert json.loads((await ws.receive()).data)["type"] == "ctrl.ready"
                assert json.loads((await ws.receive()).data)["type"] == "ctrl.state"
                await ws.send_str(
                    json.dumps(
                        {
                            "type": "data.text",
                            "trace_id": created["trace_id"],
                            "session_id": created["session_id"],
                            "utterance_id": "utt-ws-multi",
                            "text": "往前走一米再挥手",
                            "final": True,
                            "ts_ms": 1789000001000,
                        },
                        ensure_ascii=False,
                    )
                )
                reply = json.loads((await ws.receive()).data)
                assert reply["type"] == "data.reply"
                assert reply["intent_type"] == "control_cmd"
                assert "多个操作" in reply["text"]
                assert "cmd_id" not in reply
                try:
                    unexpected_cmd = await ws.receive(timeout=0.1)
                except (TimeoutError, asyncio.TimeoutError):
                    unexpected_cmd = None
                assert unexpected_cmd is None
            finally:
                await ws.close()
                await client.close()
        finally:
            runtime.agent_loop = None
            runtime.manual_text_handler = None
            panel.web_loop = None
            await server.close()

    asyncio.run(_main())


def test_webpanel_merged_ws_receives_binary_and_data_cmd_frames() -> None:
    class FakeInput:
        def __init__(self) -> None:
            self.frames: list[bytes] = []

        def _sync_push(self, data: bytes) -> None:
            self.frames.append(data)

    async def _main() -> None:
        fake_input = FakeInput()
        runtime.ws_audio_input = fake_input
        runtime.agent_loop = asyncio.get_running_loop()
        server = TestServer(build_web_app(admin_routes=False, web_audio=True))
        await server.start_server()
        panel.web_loop = asyncio.get_running_loop()
        try:
            client, ws, session = await _open_session(f"http://127.0.0.1:{server.port}")
            try:
                await ws.send_str(
                    json.dumps(
                        {
                            "type": "ctrl.hello",
                            "trace_id": session["trace_id"],
                            "session_id": session["session_id"],
                            "proto": 2,
                            "role": "device",
                            "device_id": "robot-x3-001",
                            "caps": ["audio", "text", "cmd", "state"],
                        },
                        ensure_ascii=False,
                    )
                )
                await ws.receive()
                await ws.receive()
                await ws.send_bytes(b"\x01\x02" * 160)
                await asyncio.sleep(0.05)
                assert fake_input.frames == [b"\x01\x02" * 160]

                frame = {
                    "type": "data.cmd",
                    "trace_id": session["trace_id"],
                    "session_id": session["session_id"],
                    "utterance_id": "utt-x3-1",
                    "cmd_id": "cmd-x3-1",
                    "capability_id": "motion.move",
                    "action": "navigation.move",
                    "params": {"direction": "forward", "distance_cm": 100},
                    "risk_level": "medium",
                    "ack_timeout_ms": 3000,
                    "result_timeout_ms": 3000,
                    "issued_at_ms": 1789000001000,
                }
                audit_start = len(panel.command_lifecycle.audit_events)
                assert broadcast({"type": "g3_protocol", "frames": [frame], "dry_run": False})
                received = json.loads((await ws.receive()).data)
                assert received == frame
                assert not broadcast({"type": "g3_protocol", "frames": [frame], "dry_run": False})
                assert not broadcast(
                    {
                        "type": "g3_protocol",
                        "frames": [{**frame, "cmd_id": "cmd-x3-dry-run"}],
                        "dry_run": True,
                    }
                )

                ack = {
                    "type": "data.cmd_ack",
                    "trace_id": frame["trace_id"],
                    "session_id": frame["session_id"],
                    "utterance_id": frame["utterance_id"],
                    "cmd_id": frame["cmd_id"],
                    "status": "accepted",
                    "code": "sdk_received",
                    "received_at_ms": 1789000001100,
                }
                running = {
                    "type": "data.cmd_result",
                    "trace_id": frame["trace_id"],
                    "session_id": frame["session_id"],
                    "utterance_id": frame["utterance_id"],
                    "cmd_id": frame["cmd_id"],
                    "status": "running",
                    "code": "executor_started",
                }
                succeeded = {
                    **running,
                    "status": "succeeded",
                    "code": "done",
                }
                await ws.send_str(json.dumps(ack, ensure_ascii=False))
                await ws.send_str(json.dumps(running, ensure_ascii=False))
                await ws.send_str(json.dumps(succeeded, ensure_ascii=False))
                await ws.send_str(json.dumps(succeeded, ensure_ascii=False))

                unknown = {**ack, "cmd_id": "cmd-x3-unknown"}
                await ws.send_str(json.dumps(unknown, ensure_ascii=False))
                error = json.loads((await ws.receive()).data)
                assert error["type"] == "data.error"
                assert error["code"] == "unknown_cmd_id"
                lifecycle_events = panel.command_lifecycle.audit_events[audit_start:]
                assert [event["event"] for event in lifecycle_events] == [
                    "issued",
                    "duplicate_issue",
                    "ack",
                    "result",
                    "result",
                    "duplicate",
                    "unknown_cmd_id",
                ]

                broadcast(
                    {"type": "message", "role": "assistant", "text": "收到", "ts": 1789000002}
                )
                reply = json.loads((await ws.receive()).data)
                assert reply["type"] == "data.reply"
                assert reply["text"] == "收到"
                try:
                    duplicate = await ws.receive(timeout=0.2)
                except (TimeoutError, asyncio.TimeoutError):
                    duplicate = None
                assert duplicate is None, "merged WS must not receive legacy duplicate message"
            finally:
                await ws.close()
                await client.close()
        finally:
            runtime.ws_audio_input = None
            runtime.agent_loop = None
            panel.web_loop = None
            await server.close()

    asyncio.run(_main())


def _lifecycle_command(*, cmd_id: str = "cmd-lifecycle", result_timeout_ms: int = 3000):
    return {
        "type": "data.cmd",
        "trace_id": "trace-lifecycle",
        "session_id": "sess-lifecycle",
        "utterance_id": "utt-lifecycle",
        "cmd_id": cmd_id,
        "ack_timeout_ms": 3000,
        "result_timeout_ms": result_timeout_ms,
        "issued_at_ms": 1000,
    }


def _lifecycle_frame(command: dict, *, typ: str, status: str) -> dict:
    return {
        "type": typ,
        "trace_id": command["trace_id"],
        "session_id": command["session_id"],
        "utterance_id": command["utterance_id"],
        "cmd_id": command["cmd_id"],
        "status": status,
    }


def test_command_lifecycle_registration_replaces_stale_intent_time() -> None:
    tracker = CommandLifecycleTracker()
    command = _lifecycle_command(cmd_id="cmd-stale-issued-at")
    command["issued_at_ms"] = 1

    assert tracker.issue(command, now_ms=10_000)
    assert command["issued_at_ms"] == 10_000
    accepted = tracker.accept_update(
        _lifecycle_frame(command, typ="data.cmd_ack", status="accepted"), now_ms=12_999
    )
    succeeded = tracker.accept_update(
        _lifecycle_frame(command, typ="data.cmd_result", status="succeeded"), now_ms=13_000
    )

    assert (accepted.lifecycle, accepted.outcome) == ("accepted", None)
    assert (succeeded.lifecycle, succeeded.outcome) == ("accepted", "success")


def test_command_lifecycle_duplicate_issue_does_not_refresh_deadline() -> None:
    tracker = CommandLifecycleTracker()
    command = _lifecycle_command(cmd_id="cmd-no-refresh", result_timeout_ms=500)
    command["ack_timeout_ms"] = 500

    assert tracker.issue(command, now_ms=1000)
    assert not tracker.issue(command, now_ms=1400)
    expired = tracker.expire(now_ms=1501)

    assert [event["event"] for event in expired] == ["delivery_timeout"]


def test_command_lifecycle_outcomes_are_exactly_once_at_three_second_boundary() -> None:
    tracker = CommandLifecycleTracker()
    command = _lifecycle_command()
    assert tracker.issue(command, now_ms=1000)
    accepted = _lifecycle_frame(command, typ="data.cmd_ack", status="accepted")
    running = _lifecycle_frame(command, typ="data.cmd_result", status="running")
    succeeded = _lifecycle_frame(command, typ="data.cmd_result", status="succeeded")

    assert tracker.accept_update(accepted, now_ms=1100).outcome is None
    assert tracker.accept_update(running, now_ms=1200).outcome is None
    success = tracker.accept_update(succeeded, now_ms=4000)
    assert (success.lifecycle, success.outcome) == ("accepted", "success")
    duplicate = tracker.accept_update(succeeded, now_ms=4001)
    assert (duplicate.lifecycle, duplicate.outcome) == ("duplicate", None)
    contradiction = tracker.accept_update({**succeeded, "status": "failed"}, now_ms=4002)
    assert (contradiction.lifecycle, contradiction.outcome) == ("late", None)
    assert tracker.expire(now_ms=5000) == []


def test_command_lifecycle_late_success_yields_one_failure_before_sweep() -> None:
    tracker = CommandLifecycleTracker()
    command = _lifecycle_command()
    tracker.issue(command, now_ms=1000)
    tracker.accept_update(
        _lifecycle_frame(command, typ="data.cmd_ack", status="accepted"), now_ms=1100
    )
    succeeded = _lifecycle_frame(command, typ="data.cmd_result", status="succeeded")

    late = tracker.accept_update(succeeded, now_ms=4001)
    assert (late.lifecycle, late.outcome) == ("late", "failure")
    repeated = tracker.accept_update(succeeded, now_ms=4002)
    assert (repeated.lifecycle, repeated.outcome) == ("late", None)
    assert tracker.expire(now_ms=5000) == []


def test_command_lifecycle_terminal_failures_settle_once() -> None:
    for status in ("failed", "canceled", "timeout"):
        tracker = CommandLifecycleTracker()
        command = _lifecycle_command(cmd_id=f"cmd-{status}")
        tracker.issue(command, now_ms=1000)
        failed = tracker.accept_update(
            _lifecycle_frame(command, typ="data.cmd_result", status=status), now_ms=1200
        )
        assert (failed.lifecycle, failed.outcome) == ("accepted", "failure")
        repeated = tracker.accept_update(
            _lifecycle_frame(command, typ="data.cmd_result", status=status), now_ms=1201
        )
        assert (repeated.lifecycle, repeated.outcome) == ("duplicate", None)

    for status in ("rejected", "duplicate"):
        tracker = CommandLifecycleTracker()
        command = _lifecycle_command(cmd_id=f"cmd-ack-{status}")
        tracker.issue(command, now_ms=1000)
        failed = tracker.accept_update(
            _lifecycle_frame(command, typ="data.cmd_ack", status=status), now_ms=1100
        )
        assert (failed.lifecycle, failed.outcome) == ("accepted", "failure")


def test_server_schedules_exact_command_outcome_speech() -> None:
    async def _main() -> None:
        spoken: list[str] = []
        previous = panel.command_lifecycle
        panel.command_lifecycle = CommandLifecycleTracker()
        command = _lifecycle_command(cmd_id="cmd-server-speech")
        command["issued_at_ms"] = int(time.time() * 1000)
        command["ack_timeout_ms"] = 3000
        command["result_timeout_ms"] = 3000
        panel.command_lifecycle.issue(command)
        runtime.agent_loop = asyncio.get_running_loop()
        original = web_server.say_command_status
        web_server.say_command_status = spoken.append
        ws = types.SimpleNamespace(send_str=lambda _: None)
        session_info = {"trace_id": command["trace_id"]}
        try:
            await web_server._process_command_lifecycle(
                ws,
                _lifecycle_frame(command, typ="data.cmd_result", status="succeeded"),
                session_info,
                command["session_id"],
            )
            await web_server._process_command_lifecycle(
                ws,
                _lifecycle_frame(command, typ="data.cmd_result", status="succeeded"),
                session_info,
                command["session_id"],
            )
            await asyncio.sleep(0)
            assert spoken == [COMMAND_SUCCESS]
        finally:
            web_server.say_command_status = original
            runtime.agent_loop = None
            panel.command_lifecycle = previous

    asyncio.run(_main())


def test_command_lifecycle_audits_delivery_execution_timeout_and_late_frames() -> None:
    command = {
        "type": "data.cmd",
        "trace_id": "trace-lifecycle",
        "session_id": "sess-lifecycle",
        "utterance_id": "utt-lifecycle",
        "cmd_id": "cmd-lifecycle-delivery",
        "ack_timeout_ms": 100,
        "result_timeout_ms": 500,
        "issued_at_ms": 1000,
    }
    delivery = CommandLifecycleTracker()
    delivery.issue(command, now_ms=1000)
    assert delivery.expire(now_ms=1100)[0]["event"] == "delivery_timeout"
    late_ack = {
        "type": "data.cmd_ack",
        "trace_id": command["trace_id"],
        "session_id": command["session_id"],
        "utterance_id": command["utterance_id"],
        "cmd_id": command["cmd_id"],
        "status": "accepted",
    }
    assert delivery.accept(late_ack, now_ms=1110) == "late"

    execution = CommandLifecycleTracker()
    execution_command = {**command, "cmd_id": "cmd-lifecycle-execution"}
    execution.issue(execution_command, now_ms=1000)
    ack = {**late_ack, "cmd_id": execution_command["cmd_id"]}
    assert execution.accept(ack, now_ms=1050) == "accepted"
    assert execution.expire(now_ms=1501)[0]["event"] == "execution_timeout"
    late_result = {
        **ack,
        "type": "data.cmd_result",
        "status": "succeeded",
    }
    assert execution.accept(late_result, now_ms=1510) == "late"
