"""Web 控制面板服务(独立线程 + 独立事件循环,aiohttp)。

路由:GET /(static/index.html,后端 tab 按注册表注入)、GET /ws(实时日志/状态)、
POST /api/{mic,asr,tts};WEB_AUDIO=1 时另有 GET /ws/audio(浏览器 PCM 双向)。

web→agent 的一切控制都经 runtime.agent_loop 的 *_threadsafe marshal,绝不直接 await。
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp
import aiohttp.web
from app.backends import (
    STT_BACKENDS,
    TTS_BACKENDS,
    backend_tabs_html,
    make_stt_backend,
    make_tts_backend,
)
from app.listening_host import listen_on_mic_toggle
from app.session_state import runtime, say_voice_welcome
from app.web_audio import WebSocketAudioInput
from common.runtime import append_turn_log

from webpanel.state import (
    ADMIN_ROUTES,
    BUSY_HTML,
    BUSY_MESSAGE,
    DEBUG_QUERY_TOKEN,
    SSL_CERT,
    SSL_KEY,
    WEB_AUDIO,
    WEB_HOST,
    panel,
)

logger = logging.getLogger("web-ui-agent")
_CAPS = ("audio", "text", "cmd", "state")
_CONFIG_VERSION = "cfg-r5.2.2-webpanel"
_TOKEN_TTL_MS = 600_000
_FRAME_JSON_MAX_BYTES = 8192
_INDEX_HTML_KEY = aiohttp.web.AppKey("index_html", str)
_HIDDEN_BACKEND_TABS = """
  <button id="tabFunasr" class="asr-tab"></button>
  <button id="tabQwen3" class="asr-tab"></button>
  <button id="tabQwen3Stream" class="asr-tab"></button>
  <button id="tabTtsQwen" class="asr-tab"></button>
  <button id="tabTtsCosy" class="asr-tab"></button>
  <button id="tabTtsHttp" class="asr-tab"></button>
""".strip()


def _build_index_html(admin_routes: bool, debug_query_token: bool = False) -> str:
    """读 static/index.html 注入后端 tab(注册表生成,加后端自动出 tab)。
    M5/D-19:`admin_routes` 关则 `<!--BACKEND_TABS-->` **不注入**——开关同控路由注册与 tab。"""
    html = (Path(__file__).resolve().parent / "static" / "index.html").read_text(encoding="utf-8")
    tabs = backend_tabs_html() if admin_routes else _HIDDEN_BACKEND_TABS
    html = html.replace("<!--BACKEND_TABS-->", tabs)
    return html.replace(
        "var DEMO_QUERY_TOKEN_ENABLED=false;",
        f"var DEMO_QUERY_TOKEN_ENABLED={str(debug_query_token).lower()};",
    )


async def _send_busy_and_close(ws: aiohttp.web.WebSocketResponse) -> None:
    await ws.send_str(json.dumps({"type": "busy", "message": BUSY_MESSAGE}, ensure_ascii=False))
    await ws.close(code=aiohttp.WSCloseCode.TRY_AGAIN_LATER, message=b"busy")


async def _handle_index(request: aiohttp.web.Request) -> aiohttp.web.Response:
    # The gateway already enforces affinity and double-tab exclusion. Direct/local access
    # has no trusted marker, so it retains the original busy guard.
    gateway_session = request.headers.get("X-XG-Session")
    if WEB_AUDIO and not gateway_session:
        primary = panel.ws_primary_client
        audio_primary = panel.audio_ws_primary_client
        if (primary is not None and not primary.closed) or (
            audio_primary is not None and not audio_primary.closed
        ):
            return aiohttp.web.Response(
                text=BUSY_HTML,
                content_type="text/html",
                charset="utf-8",
                headers={"Cache-Control": "no-store"},
            )
    return aiohttp.web.Response(
        text=request.app[_INDEX_HTML_KEY],
        content_type="text/html",
        charset="utf-8",
        headers={"Cache-Control": "no-store"},
    )


async def _handle_healthz(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """就绪/健康探测(池管理器 B4):专用轻路由,不占 WS primary 槽、无会话副作用。"""
    aloop = runtime.agent_loop
    loop_running = aloop is not None and aloop.is_running()
    ready = loop_running and runtime.session is not None
    return aiohttp.web.json_response({"ready": ready, "agent_loop_running": loop_running})


async def _handle_create_session(request: aiohttp.web.Request) -> aiohttp.web.Response:
    try:
        body = await request.json()
    except Exception:
        return aiohttp.web.json_response({"code": "protocol_error"}, status=400)
    caps = body.get("caps")
    audio = body.get("audio_format")
    credential = body.get("credential")
    if (
        not isinstance(body.get("device_id"), str)
        or not body.get("device_id")
        or not isinstance(body.get("client_version"), str)
        or not body.get("client_version")
        or not isinstance(caps, list)
        or not caps
        or len(caps) != len(set(caps))
        or any(cap not in _CAPS for cap in caps)
        or not isinstance(audio, dict)
        or audio.get("sample_rate") != WebSocketAudioInput.SAMPLE_RATE
        or audio.get("channels") != 1
        or audio.get("sample_format") != "int16le"
    ):
        return aiohttp.web.json_response({"code": "protocol_error"}, status=400)
    if credential == "bad" or (isinstance(credential, dict) and credential.get("key_id") == "bad"):
        return aiohttp.web.json_response({"code": "auth_failed"}, status=401)
    if "cmd" not in caps:
        return aiohttp.web.json_response({"code": "permission_denied"}, status=403)

    session_id = f"sess-r522-{int(time.time() * 1000)}-{secrets.token_hex(3)}"
    trace_id = f"trace-r522-{secrets.token_hex(8)}"
    token = f"tok-{secrets.token_urlsafe(24)}"
    panel.issued_tokens[token] = {
        "session_id": session_id,
        "trace_id": trace_id,
        "granted_caps": list(caps),
        "expires_at_ms": int(time.time() * 1000) + _TOKEN_TTL_MS,
    }
    scheme = "wss" if request.secure else "ws"
    return aiohttp.web.json_response(
        {
            "type": "session.created",
            "trace_id": trace_id,
            "session_id": session_id,
            "access_token": token,
            "expires_in_ms": _TOKEN_TTL_MS,
            "ws_url": f"{scheme}://{request.host}/ws/session",
            "granted_caps": list(caps),
            "config_snapshot": {"config_version": _CONFIG_VERSION},
        }
    )


async def _handle_ws_session(request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
    token = "" if "access_token" in request.query else _bearer_token(request)
    return await _serve_ws_session(request, token)


async def _handle_debug_ws_session(
    request: aiohttp.web.Request,
) -> aiohttp.web.WebSocketResponse:
    token = request.query.get("access_token", "").strip()
    return await _serve_ws_session(request, token)


async def _serve_ws_session(
    request: aiohttp.web.Request, token: str
) -> aiohttp.web.WebSocketResponse:
    ws = aiohttp.web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    registered = await _register_session_ws(ws, token)
    if registered is None:
        return ws
    session_info, session_id = registered

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                _push_audio_to_agent(msg.data)
                continue
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            if len(msg.data.encode("utf-8")) > _FRAME_JSON_MAX_BYTES:
                await ws.close(code=4400, message=b"protocol_error")
                break
            try:
                payload = json.loads(msg.data)
            except json.JSONDecodeError:
                await ws.close(code=4400, message=b"protocol_error")
                break
            if not isinstance(payload, dict) or not _is_p0_client_frame(payload):
                await ws.close(code=4400, message=b"protocol_error")
                break
            await _process_session_payload(ws, payload, session_info, session_id)
    finally:
        _unregister_session_ws(ws, session_id)
        logger.info("R5.2.2 merged WS session disconnected: %s", session_id)
        _request_graceful_exit(request.headers.get("X-XG-Session"))
    return ws


async def _register_session_ws(
    ws: aiohttp.web.WebSocketResponse, token: str
) -> tuple[dict[str, object], str] | None:
    session_info = panel.issued_tokens.get(token or "")
    now_ms = int(time.time() * 1000)
    if session_info is None or int(session_info["expires_at_ms"]) < now_ms:
        await ws.close(code=4401, message=b"auth_failed")
        return None
    session_id = str(session_info["session_id"])
    if session_id in panel.active_session_ids:
        await ws.close(code=4009, message=b"duplicate_connection")
        return None

    lock = panel.connection_lock
    if lock is not None:
        async with lock:
            panel.active_session_ids.add(session_id)
            panel.session_ws_primary_client = ws
            panel.session_ws_clients.add(ws)
            panel.audio_ws_primary_client = ws
            panel.audio_ws_clients.add(ws)
    else:
        panel.active_session_ids.add(session_id)
        panel.session_ws_primary_client = ws
        panel.session_ws_clients.add(ws)
        panel.audio_ws_primary_client = ws
        panel.audio_ws_clients.add(ws)
    ws._xg_session_id = session_id  # type: ignore[attr-defined]
    logger.info("R5.2.2 merged WS session connected: %s", session_id)
    return session_info, session_id


def _unregister_session_ws(ws: aiohttp.web.WebSocketResponse, session_id: str) -> None:
    panel.active_session_ids.discard(session_id)
    panel.session_ws_clients.discard(ws)
    panel.audio_ws_clients.discard(ws)
    if panel.session_ws_primary_client is ws:
        panel.session_ws_primary_client = None
    if panel.audio_ws_primary_client is ws:
        panel.audio_ws_primary_client = None


async def _process_session_payload(
    ws: aiohttp.web.WebSocketResponse,
    payload: dict,
    session_info: dict[str, object],
    session_id: str,
) -> None:
    typ = payload.get("type")
    append_turn_log(f"R522_WS_IN type={typ} session={session_id}")
    if typ in {"data.cmd_ack", "data.cmd_result"}:
        await _process_command_lifecycle(ws, payload, session_info, session_id)
    elif typ == "ctrl.hello":
        await _send_ctrl_ready_and_state(ws, payload, session_info)
    elif typ == "ctrl.frontend_state" and _is_frontend_call_started(payload):
        if not getattr(ws, "_xg_voice_welcome_sent", False):
            ws._xg_voice_welcome_sent = True  # type: ignore[attr-defined]
            _schedule_voice_welcome()
    elif typ == "data.text":
        _push_manual_text_to_agent(str(payload.get("text") or ""))


async def _process_command_lifecycle(
    ws: aiohttp.web.WebSocketResponse,
    payload: dict,
    session_info: dict[str, object],
    session_id: str,
) -> None:
    lifecycle = panel.command_lifecycle.accept(payload)
    append_turn_log(f"R522_CMD_LIFECYCLE event={lifecycle} cmd_id={payload['cmd_id']}")
    if lifecycle != "unknown":
        return
    await ws.send_str(
        json.dumps(
            {
                "type": "data.error",
                "trace_id": session_info["trace_id"],
                "session_id": session_id,
                "code": "unknown_cmd_id",
                "message": "ack/result references unknown cmd_id",
                "retryable": False,
                "ts_ms": int(time.time() * 1000),
            },
            ensure_ascii=False,
        )
    )


async def _handle_ws_audio(request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
    ws = aiohttp.web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    # 网关为真实会话连接注入 X-XG-Session 标记;断开时据此优雅退出进程(#3,配合池回收)。
    # 无网关(PC/测试形态)时头缺失 → 退出逻辑天然不触发,行为不变。
    xg_session = request.headers.get("X-XG-Session")
    lock = panel.connection_lock
    if lock is not None:
        async with lock:
            if (
                panel.audio_ws_primary_client is not None
                and not panel.audio_ws_primary_client.closed
            ):
                logger.info("audio WS rejected: server busy")
                await _send_busy_and_close(ws)
                return ws
            panel.audio_ws_primary_client = ws
            panel.audio_ws_clients.add(ws)
    else:
        panel.audio_ws_primary_client = ws
        panel.audio_ws_clients.add(ws)
    logger.info("audio WS client connected (%d total)", len(panel.audio_ws_clients))

    await ws.send_str(json.dumps({"type": "ready", "sample_rate": WebSocketAudioInput.SAMPLE_RATE}))
    aloop = runtime.agent_loop
    if aloop is not None and aloop.is_running():
        logger.info("voice welcome scheduled")
        aloop.call_soon_threadsafe(say_voice_welcome)
    else:
        logger.info("voice welcome skipped: agent loop not ready")

    frame_count = 0
    byte_count = 0
    async for msg in ws:
        if msg.type == aiohttp.WSMsgType.BINARY:
            frame_count += 1
            byte_count += len(msg.data)
            if frame_count in (1, 50, 200):
                logger.info("audio WS received frames=%d bytes=%d", frame_count, byte_count)
            inp = runtime.ws_audio_input
            aloop = runtime.agent_loop
            if inp is not None and aloop is not None and aloop.is_running():
                aloop.call_soon_threadsafe(inp._sync_push, msg.data)
        elif msg.type == aiohttp.WSMsgType.ERROR:
            break

    if panel.audio_ws_primary_client is ws:
        panel.audio_ws_primary_client = None
    panel.audio_ws_clients.discard(ws)
    logger.info("audio WS client disconnected (%d remaining)", len(panel.audio_ws_clients))
    _request_graceful_exit(xg_session)
    return ws


def _bearer_token(request: aiohttp.web.Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth.removeprefix("Bearer ").strip()
    return ""


_CMD_ACK_STATUS = frozenset({"accepted", "rejected", "duplicate"})
_CMD_RESULT_STATUS = frozenset({"running", "succeeded", "failed", "canceled", "timeout"})


def _is_non_empty_str(value: object) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _is_non_negative_int(value: object) -> bool:
    # bool is a subclass of int; reject it explicitly so True/False are not valid ms values.
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_p0_client_frame(payload: dict) -> bool:
    typ = payload.get("type")
    if typ == "ctrl.hello":
        caps = payload.get("caps")
        return (
            payload.get("proto") == 2
            and payload.get("role") in {"device", "panel"}
            and _is_non_empty_str(payload.get("trace_id"))
            and _is_non_empty_str(payload.get("session_id"))
            and _is_non_empty_str(payload.get("device_id"))
            and isinstance(caps, list)
            and bool(caps)
            and len(caps) == len(set(caps))
            and all(cap in _CAPS for cap in caps)
            and "token" not in payload
        )
    if typ == "ctrl.frontend_state":
        return _is_non_negative_int(payload.get("seq")) and _is_non_negative_int(
            payload.get("ttl_ms")
        )
    if typ == "data.text":
        text = payload.get("text")
        return isinstance(text, str) and 0 < len(text.strip()) <= 2000
    if typ == "data.cmd_ack":
        return (
            _is_non_empty_str(payload.get("trace_id"))
            and _is_non_empty_str(payload.get("session_id"))
            and _is_non_empty_str(payload.get("utterance_id"))
            and _is_non_empty_str(payload.get("cmd_id"))
            and payload.get("status") in _CMD_ACK_STATUS
            and _is_non_empty_str(payload.get("code"))
            and _is_non_negative_int(payload.get("received_at_ms"))
        )
    if typ == "data.cmd_result":
        return (
            _is_non_empty_str(payload.get("trace_id"))
            and _is_non_empty_str(payload.get("session_id"))
            and _is_non_empty_str(payload.get("utterance_id"))
            and _is_non_empty_str(payload.get("cmd_id"))
            and payload.get("status") in _CMD_RESULT_STATUS
            and _is_non_empty_str(payload.get("code"))
        )
    return False


async def _send_ctrl_ready_and_state(
    ws: aiohttp.web.WebSocketResponse, hello: dict, session_info: dict[str, object]
) -> None:
    trace_id = str(hello.get("trace_id") or session_info["trace_id"])
    session_id = str(hello.get("session_id") or session_info["session_id"])
    granted = session_info.get("granted_caps")
    granted_caps = set(granted) if isinstance(granted, list) else set()
    caps = [cap for cap in hello.get("caps", []) if cap in granted_caps]
    ready = {
        "type": "ctrl.ready",
        "trace_id": trace_id,
        "session_id": session_id,
        "sample_rate": WebSocketAudioInput.SAMPLE_RATE,
        "granted_caps": caps,
        "config_version": _CONFIG_VERSION,
    }
    state = {
        "type": "ctrl.state",
        "trace_id": trace_id,
        "session_id": session_id,
        "link_state": "connected",
        "interaction_mode": "dialogue",
        "engine_gate": "open",
        "resource_state": "ActiveAgent",
        "ts_ms": int(time.time() * 1000),
    }
    await ws.send_str(json.dumps(ready, ensure_ascii=False))
    if "state" in caps:
        await ws.send_str(json.dumps(state, ensure_ascii=False))


async def _command_timeout_context(_: aiohttp.web.Application) -> AsyncIterator[None]:
    async def _sweep() -> None:
        while True:
            await asyncio.sleep(0.1)
            for event in panel.command_lifecycle.expire():
                append_turn_log(
                    f"R522_CMD_LIFECYCLE event={event['event']} cmd_id={event['cmd_id']}"
                )

    task = asyncio.create_task(_sweep())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def _push_audio_to_agent(data: bytes) -> None:
    inp = runtime.ws_audio_input
    aloop = runtime.agent_loop
    if inp is not None and aloop is not None and aloop.is_running():
        aloop.call_soon_threadsafe(inp._sync_push, data)


def _push_manual_text_to_agent(text: str) -> None:
    handler = runtime.manual_text_handler
    aloop = runtime.agent_loop
    if handler is not None and aloop is not None and aloop.is_running():
        asyncio.run_coroutine_threadsafe(handler(text), aloop)


def _schedule_voice_welcome() -> None:
    aloop = runtime.agent_loop
    if aloop is not None and aloop.is_running():
        aloop.call_soon_threadsafe(say_voice_welcome)


def _is_frontend_call_started(payload: dict[str, object]) -> bool:
    return (
        payload.get("wake_event") == "button"
        and payload.get("wake_state") == "awake"
        and payload.get("trust_level") == "authoritative"
    )


def _clear_agent_audio() -> None:
    output = getattr(runtime, "ws_audio_output", None)
    if output is not None:
        output.clear_buffer()


def _request_graceful_exit(session_tag: str | None) -> None:
    """网关标记的真实会话断开 → 经 agent 循环 marshal ctx.shutdown() 优雅退出进程
    (跑完 shutdown 回调:录音收尾等),池管理器随后重启回收。仅并发部署下触发;
    未标记(session_tag 为空,即无网关/PC 形态)直接返回,行为不变。"""
    if not session_tag:
        return
    ctx = runtime.job_ctx
    aloop = runtime.agent_loop
    if ctx is None or aloop is None or not aloop.is_running():
        return
    logger.info("gateway session %s disconnected -> graceful process shutdown", session_tag)
    aloop.call_soon_threadsafe(lambda: ctx.shutdown(reason="gateway session ended"))


async def _handle_ws(request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
    ws = aiohttp.web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    gateway_session = request.headers.get("X-XG-Session")
    lock = panel.connection_lock
    old_state_ws = None
    if WEB_AUDIO and lock is not None:
        async with lock:
            if (
                not gateway_session
                and panel.audio_ws_primary_client is not None
                and not panel.audio_ws_primary_client.closed
            ):
                logger.info("state WS rejected: audio in progress")
                await _send_busy_and_close(ws)
                return ws
            old_state_ws = (
                panel.ws_primary_client
                if panel.ws_primary_client is not None and not panel.ws_primary_client.closed
                else None
            )
            if old_state_ws is not None:
                panel.ws_clients.discard(old_state_ws)
            panel.ws_primary_client = ws
            panel.ws_clients.add(ws)
    else:
        if WEB_AUDIO:
            panel.ws_primary_client = ws
        panel.ws_clients.add(ws)
    if old_state_ws is not None:
        try:
            await old_state_ws.close(code=aiohttp.WSCloseCode.GOING_AWAY)
        except Exception:
            pass

    # Push current state immediately on connect
    stt = runtime.switchable_stt
    await ws.send_str(
        json.dumps(
            {
                "type": "state",
                "muted": runtime.mute_gate.muted if runtime.mute_gate else False,
                "stt_backend": stt.provider if stt else "FunASR",
                "tts_backend": runtime.tts_backend_key,
                "audio_mode": WEB_AUDIO,
            },
            ensure_ascii=False,
        )
    )
    if not WEB_AUDIO:
        aloop = runtime.agent_loop
        if aloop is not None and aloop.is_running():
            logger.info("local welcome scheduled")
            aloop.call_soon_threadsafe(say_voice_welcome)
        else:
            logger.info("local welcome skipped: agent loop not ready")

    async for _ in ws:
        pass  # keep-alive; messages from browser not used

    if panel.ws_primary_client is ws:
        panel.ws_primary_client = None
    panel.ws_clients.discard(ws)
    return ws


async def _handle_mic(request: aiohttp.web.Request) -> aiohttp.web.Response:
    from webpanel.bridge import broadcast

    # 关麦=真关麦:主机制是输入源头的静音门(对所有 STT 后端统一,关麦时全链路收静音)。
    gate = runtime.mute_gate
    if gate is None:
        return aiohttp.web.json_response({"error": "agent not ready"}, status=503)
    gate.muted = not gate.muted
    muted = gate.muted
    # 上游(SwitchableSTT)路径若在,同步其 muted 以保持状态一致(冗余但无害)。
    if runtime.switchable_stt is not None:
        runtime.switchable_stt.muted = muted
    # 麦克风关闭 -> 暂停录制(用户轨),开启 -> 继续(测试模式下)。
    if runtime.test_recorder is not None:
        runtime.test_recorder.set_paused(muted)
    broadcast({"type": "state", "muted": muted})
    # 聆听模式:通话键退出/补问的控制器变更必须 marshal 回 agent 循环串行(见设计 §5.7)。
    loop = runtime.agent_loop
    ctrl = runtime.listen_ctrl
    if loop is not None and loop.is_running() and ctrl is not None and ctrl.enabled:
        loop.call_soon_threadsafe(listen_on_mic_toggle, muted)
    logger.info("mic %s (mute-gate)", "muted" if muted else "unmuted")
    return aiohttp.web.json_response({"muted": muted})


async def _handle_switch_asr(request: aiohttp.web.Request) -> aiohttp.web.Response:
    from webpanel.bridge import broadcast

    stt = runtime.switchable_stt
    if stt is None:
        return aiohttp.web.json_response({"error": "agent not ready"}, status=503)

    try:
        data = await request.json()
    except Exception:
        return aiohttp.web.json_response({"error": "invalid json"}, status=400)

    backend = data.get("backend", "funasr").strip().lower()
    if backend not in STT_BACKENDS:
        return aiohttp.web.json_response({"error": f"unknown backend: {backend}"}, status=400)

    new_stt = make_stt_backend(backend)

    old_stt = stt.switch_backend(new_stt)
    provider = new_stt.provider
    broadcast({"type": "state", "stt_backend": provider})
    logger.info("ASR backend switched to %s", provider)
    append_turn_log(f"ASR_SWITCH provider={provider}")

    # Close old backend in the agent's event loop (it may have async teardown)
    aloop = runtime.agent_loop
    if aloop is not None and aloop.is_running():
        asyncio.run_coroutine_threadsafe(old_stt.aclose(), aloop)

    return aiohttp.web.json_response({"backend": backend, "provider": provider})


async def _handle_switch_tts(request: aiohttp.web.Request) -> aiohttp.web.Response:
    from webpanel.bridge import broadcast

    tts_engine = runtime.switchable_tts
    if tts_engine is None:
        return aiohttp.web.json_response({"error": "agent not ready"}, status=503)

    try:
        data = await request.json()
    except Exception:
        return aiohttp.web.json_response({"error": "invalid json"}, status=400)

    backend = data.get("backend", "qwen").strip().lower()
    if backend not in TTS_BACKENDS:
        return aiohttp.web.json_response({"error": f"unknown backend: {backend}"}, status=400)

    new_tts = make_tts_backend(backend)

    old_tts = tts_engine.switch_backend(new_tts)
    runtime.tts_backend_key = backend
    provider = new_tts.provider
    broadcast({"type": "state", "tts_backend": backend})
    logger.info("TTS backend switched to %s", provider)

    aloop = runtime.agent_loop
    if aloop is not None and aloop.is_running():
        asyncio.run_coroutine_threadsafe(old_tts.aclose(), aloop)

    return aiohttp.web.json_response({"backend": backend, "provider": provider})


def _knows_html() -> str:
    """读 static/knows.html(独立管理页;Cache-Control: no-store 由处理器设)。"""
    return (Path(__file__).resolve().parent / "static" / "knows.html").read_text(encoding="utf-8")


async def _handle_knows_page(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /knows → 返回独立管理页(无鉴权;网关层负责 apikey 准入)。"""
    return aiohttp.web.Response(
        text=_knows_html(),
        content_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


async def _handle_knows_list(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/knows/list → {chunks:[...], doc_count, ready}。list_chunks 同步读 SQLite。"""
    idx = runtime.knowledge_index
    if idx is None:
        return aiohttp.web.json_response({"error": "knowledge index not initialized"}, status=503)
    try:
        chunks = idx.list_chunks()
    except Exception as exc:
        logger.exception("knowledge list failed")
        return aiohttp.web.json_response({"error": f"list failed: {exc}"}, status=500)
    return aiohttp.web.json_response(
        {
            "chunks": chunks,
            "doc_count": idx.doc_count,
            "ready": idx.is_ready(),
        }
    )


async def _handle_knows_append(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """POST /api/knows/append:写 user_knowledge.md → rebuild → {ok, chunks}。

    与已移除的 _handle_knowledge_append 一致:8000 字上限、agent_loop marshal、120s 超时。
    """
    try:
        data = await request.json()
    except Exception:
        return aiohttp.web.json_response({"error": "invalid json"}, status=400)
    text = str(data.get("text", "")).strip()
    title = str(data.get("title", "")).strip() or None
    if not text:
        return aiohttp.web.json_response({"error": "text is empty"}, status=400)
    if len(text) > 8000:
        return aiohttp.web.json_response({"error": "text too long (max 8000 chars)"}, status=400)
    idx = runtime.knowledge_index
    if idx is None:
        return aiohttp.web.json_response({"error": "knowledge index not initialized"}, status=503)
    try:
        idx.append_user_chunk(text, title=title)
    except Exception as exc:
        logger.exception("failed to append user knowledge")
        return aiohttp.web.json_response({"error": f"write failed: {exc}"}, status=500)
    aloop = runtime.agent_loop
    if aloop is None or not aloop.is_running():
        return aiohttp.web.json_response({"error": "agent loop not running"}, status=503)
    fut = asyncio.run_coroutine_threadsafe(idx.rebuild(), aloop)
    try:
        n = fut.result(timeout=120)
    except Exception as exc:
        logger.exception("knowledge rebuild failed")
        return aiohttp.web.json_response({"error": f"rebuild failed: {exc}"}, status=500)
    return aiohttp.web.json_response({"ok": True, "chunks": n})


async def _handle_knows_query(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """POST /api/knows/query {query, top_k?} → {hits:[{title,text,score,source},...]}。

    **核心新功能**:不依赖 LLM 工具调用即可验证 RAG 是否命中。
    """
    try:
        data = await request.json()
    except Exception:
        return aiohttp.web.json_response({"error": "invalid json"}, status=400)
    query = str(data.get("query", "")).strip()
    if not query:
        return aiohttp.web.json_response({"error": "query is empty"}, status=400)
    top_k = int(data.get("top_k") or 5)
    if top_k < 1 or top_k > 50:
        top_k = 5
    idx = runtime.knowledge_index
    if idx is None:
        return aiohttp.web.json_response({"error": "knowledge index not initialized"}, status=503)
    aloop = runtime.agent_loop
    if aloop is None or not aloop.is_running():
        return aiohttp.web.json_response({"error": "agent loop not running"}, status=503)
    fut = asyncio.run_coroutine_threadsafe(idx.query(query, top_k=top_k), aloop)
    try:
        hits = fut.result(timeout=30)
    except Exception as exc:
        logger.exception("knowledge query failed")
        return aiohttp.web.json_response({"error": f"query failed: {exc}"}, status=500)
    return aiohttp.web.json_response(
        {
            "hits": [
                {
                    "title": h.title,
                    "text": h.text,
                    "score": h.score,
                    "source": h.source,
                }
                for h in hits
            ]
        }
    )


async def _handle_knows_delete(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """DELETE /api/knows/{chunk_id} → {ok: bool}。仅可删 user_knowledge.md 来源的块。"""
    chunk_id_raw = request.match_info.get("chunk_id", "")
    try:
        chunk_id = int(chunk_id_raw)
    except ValueError:
        return aiohttp.web.json_response({"error": "invalid chunk_id"}, status=400)
    idx = runtime.knowledge_index
    if idx is None:
        return aiohttp.web.json_response({"error": "knowledge index not initialized"}, status=503)
    aloop = runtime.agent_loop
    if aloop is None or not aloop.is_running():
        return aiohttp.web.json_response({"error": "agent loop not running"}, status=503)
    fut = asyncio.run_coroutine_threadsafe(idx.delete_chunk(chunk_id), aloop)
    try:
        ok = fut.result(timeout=120)
    except Exception as exc:
        logger.exception("knowledge delete failed")
        return aiohttp.web.json_response({"error": f"delete failed: {exc}"}, status=500)
    return aiohttp.web.json_response({"ok": ok})


def build_web_app(
    *,
    admin_routes: bool = ADMIN_ROUTES,
    web_audio: bool = WEB_AUDIO,
    debug_query_token: bool = DEBUG_QUERY_TOKEN,
) -> aiohttp.web.Application:
    """装配面板路由。M5/D-19:`admin_routes` 关(默认)时**不注册** /api/asr·/api/tts,命中即
    404(隐藏 ≠ 仅前端无 tab);/api/mic 属产品功能始终在。与启动/TLS 解耦,便于单测路由集合。"""
    app = aiohttp.web.Application()
    app.cleanup_ctx.append(_command_timeout_context)
    app[_INDEX_HTML_KEY] = _build_index_html(admin_routes, debug_query_token)
    app.router.add_get("/", _handle_index)
    app.router.add_get("/healthz", _handle_healthz)
    app.router.add_post("/create_session", _handle_create_session)
    app.router.add_get("/ws/session", _handle_ws_session)
    if debug_query_token:
        app.router.add_get("/debug/ws/session", _handle_debug_ws_session)
    app.router.add_get("/ws", _handle_ws)
    app.router.add_post("/api/mic", _handle_mic)
    # 知识库独立管理界面(/knows 页面 + /api/knows/* REST):
    # 网关层 apikey 准入 + 无亲和反代(任意 READY agent 都能处理,文件共享 + meta.json mtime 热更新)
    app.router.add_get("/knows", _handle_knows_page)
    app.router.add_get("/api/knows/list", _handle_knows_list)
    app.router.add_post("/api/knows/append", _handle_knows_append)
    app.router.add_post("/api/knows/query", _handle_knows_query)
    app.router.add_delete("/api/knows/{chunk_id}", _handle_knows_delete)
    if admin_routes:  # M5:隐藏态不注册 → aiohttp 默认 404
        app.router.add_post("/api/asr", _handle_switch_asr)
        app.router.add_post("/api/tts", _handle_switch_tts)
    if web_audio:
        app.router.add_get("/ws/audio", _handle_ws_audio)
    return app


async def _run_web_server(port: int) -> None:
    panel.web_loop = asyncio.get_running_loop()
    panel.connection_lock = asyncio.Lock()

    app = build_web_app()

    ssl_ctx = None
    if SSL_CERT and SSL_KEY:
        import ssl as _ssl

        ssl_ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.load_cert_chain(SSL_CERT, SSL_KEY)
        logger.info("TLS enabled: cert=%s", SSL_CERT)

    runner = aiohttp.web.AppRunner(app, access_log=None)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, WEB_HOST, port, ssl_context=ssl_ctx)
    await site.start()
    scheme = "https" if ssl_ctx else "http"
    logger.info("Web UI available at %s://%s:%d", scheme, WEB_HOST, port)

    await asyncio.Event().wait()  # run forever


def start_web_server_thread(port: int) -> threading.Thread:
    """起 daemon 线程跑面板(独立事件循环)。"""
    t = threading.Thread(
        target=lambda: asyncio.run(_run_web_server(port)),
        args=(),
        daemon=True,
        name="web-ui",
    )
    t.start()
    return t
