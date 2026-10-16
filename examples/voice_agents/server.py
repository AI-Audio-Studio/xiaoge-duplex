"""Web 控制面板服务(独立线程 + 独立事件循环,aiohttp)。

路由:GET /(static/index.html,后端 tab 按注册表注入)、GET /ws(实时日志/状态)、
POST /api/{mic,asr,tts,feedback};WEB_AUDIO=1 时另有 GET /ws/audio(浏览器 PCM 双向)。

web→agent 的一切控制都经 runtime.agent_loop 的 *_threadsafe marshal,绝不直接 await。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
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
from app.config_store import (
    INSTRUCTIONS_MAX_CHARS,
    PERSONA_KEYS,
    PERSONA_NAME_MAX_CHARS,
    PERSONA_TRAITS_MAX_CHARS,
    VOICES,
    default_instructions,
    load_agent_config,
    personas_public,
    sanitize_config,
    save_agent_config,
)
from app.listening_host import listen_on_mic_toggle
from app.session_state import (
    COMMAND_FAILURE,
    runtime,
    say_command_result,
    say_command_status,
    say_voice_welcome,
)
from app.standby_host import exit_standby, standby_cancel_all
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
_FEEDBACK_DIR = Path(os.getenv("XIAOGE_FEEDBACK_DIR", "data/feedback")).resolve()
_FEEDBACK_TEXT_MAX_CHARS = 2000
_FEEDBACK_CONTACT_MAX_CHARS = 100
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
    # The gateway strips client copies and injects this private header only after
    # API-key authorization. Keep it in the short-lived token record; never echo
    # it in responses or logs.
    trusted_api_key = request.headers.get("X-XG-API-Key", "").strip()
    panel.issued_tokens[token] = {
        "session_id": session_id,
        "trace_id": trace_id,
        "granted_caps": list(caps),
        "expires_at_ms": int(time.time() * 1000) + _TOKEN_TTL_MS,
        "api_key": trusted_api_key,
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
        runtime.project_api_key = None
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
    # This value was injected by the authenticated gateway into /create_session.
    # It is session-scoped and is never accepted from ctrl frames.
    runtime.project_api_key = str(session_info.get("api_key") or "") or None
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
        # 会话 attach 重置(2026-09-15 幽灵待命修复):entrypoint 只在 worker
        # 进程启动跑一次,ctrl.hello 才是池化 worker 复用的真实会话起点——
        # marshal 回 agent 循环热应用配置 + 清 standby/遗留定时器 + 重臂 idle。
        _schedule_session_attach_reset()
    elif typ == "ctrl.frontend_state":
        # 待命唤醒(退出机制 2026-09-15):端侧唤醒帧(wake_state=awake 且
        # trust_level=authoritative,不限 button)→ marshal 回 agent 循环退待命。
        # runtime.standby 只是 web 线程的咨询性读,真正的状态变更串行在 agent 循环。
        if (
            runtime.standby
            and payload.get("wake_state") == "awake"
            and payload.get("trust_level") == "authoritative"
        ):
            aloop = runtime.agent_loop
            if aloop is not None and aloop.is_running():
                aloop.call_soon_threadsafe(exit_standby, "frontend_state")
        if _is_frontend_call_started(payload) and not getattr(ws, "_xg_voice_welcome_sent", False):
            ws._xg_voice_welcome_sent = True  # type: ignore[attr-defined]
            _schedule_voice_welcome()
    elif typ == "ctrl.duplex":
        enabled = bool(payload.get("enabled"))
        if _schedule_session_duplex(enabled):
            await ws.send_str(
                json.dumps(
                    {
                        "type": "ctrl.duplex_state",
                        "trace_id": session_info["trace_id"],
                        "session_id": session_id,
                        "enabled": enabled,
                        "ts_ms": int(time.time() * 1000),
                    },
                    ensure_ascii=False,
                )
            )
    elif typ == "ctrl.persona":
        # 会话性格选择(2026-09-22,仅 -97):面板下拉只发三档 key(三档化后
        # 不再有空串选项);空串仍接受=None=隐藏回落全局人设路径,兼容直发帧
        # 的旧缓存页。ack 帧 ctrl.persona_state 原样回显 key(空串照回空串),
        # 面板据此对齐下拉。非法 key 正常到不了这里(P0 校验 4400 先拒),
        # 分支再兜一层:只调度/回显合法值,防 ack 与实际存储(=None)不一致。
        raw_persona = payload.get("persona")
        persona = "" if raw_persona is None else raw_persona
        if (persona == "" or persona in PERSONA_KEYS) and _schedule_session_persona(persona):
            await ws.send_str(
                json.dumps(
                    {
                        "type": "ctrl.persona_state",
                        "trace_id": session_info["trace_id"],
                        "session_id": session_id,
                        "persona": persona,
                        "ts_ms": int(time.time() * 1000),
                    },
                    ensure_ascii=False,
                )
            )
    elif typ == "data.text":
        _push_manual_text_to_agent(str(payload.get("text") or ""))


_BATTERY_SPEECH_SPEC = {
    "cap": ("电量", "%"),
    "vol": ("电池电压", "伏"),
    "cur": ("电池电流", "安"),
}


def _battery_result_speech(payload: dict) -> str:
    """把 check_battery 的 data.cmd_result 载荷转成一句口播; 认不出返回 ''。

    设备侧结果契约未定稿, 这里容错读取: result/data/payload 任一层下的
    {cap/vol/cur: 值} 或 {type,value,unit} 都认; 认不出就不播(维持现状)。
    充电状态已从交互协议移除; 即使旧端侧仍带 chg/charging, 也会忽略。
    """
    body = payload
    for key in ("result", "data", "payload"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            body = nested
            break

    def _fmt(key: str, value: object) -> str:
        spec = _BATTERY_SPEECH_SPEC.get(key)
        if spec is None or isinstance(value, bool) or value is None:
            return ""
        noun, unit = spec
        text = str(value).strip().rstrip("%").rstrip()
        return f"{noun}{text}{unit}" if text else ""

    lines = [line for line in (_fmt(k, body.get(k)) for k in ("cap", "vol", "cur")) if line]
    if not lines:
        single = _fmt(str(body.get("type") or ""), body.get("value"))
        if single:
            lines.append(single)
    if not lines:
        return ""
    gauge = body.get("gauge_id", payload.get("gauge_id"))
    return ("2号" if str(gauge) == "2" else "") + "，".join(lines)


async def _process_command_lifecycle(
    ws: aiohttp.web.WebSocketResponse,
    payload: dict,
    session_info: dict[str, object],
    session_id: str,
) -> None:
    update = panel.command_lifecycle.accept_update(payload)
    append_turn_log(f"R522_CMD_LIFECYCLE event={update.lifecycle} cmd_id={payload['cmd_id']}")
    # 执行成功不再播报(2026-09-04): 下发时已把动作说进确认里, 成功无需再报;
    # 失败仍要出声, 用户才知道命令没生效。
    if update.outcome == "failure":
        _schedule_command_status(COMMAND_FAILURE)
    elif str(payload.get("type") or "") == "data.cmd_result" and update.outcome == "success":
        # 查询类命令(check_battery)成功后把数值说出口; 控制类命令维持静默。
        cmd_id = str(payload.get("cmd_id") or "")
        if panel.command_lifecycle.action_of(cmd_id) == "check_battery":
            speech = _battery_result_speech(payload)
            if speech:
                _schedule_command_result(speech)
    if update.lifecycle != "unknown":
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
    trusted_api_key = request.headers.get("X-XG-API-Key", "").strip()
    if trusted_api_key:
        # This endpoint is loopback-only behind the gateway. The gateway strips
        # client copies and injects the authenticated key; bind it for audio-mode
        # workers that do not use the merged /create_session flow.
        runtime.project_api_key = trusted_api_key
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
    _schedule_voice_welcome()

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
    if typ == "ctrl.duplex":
        return isinstance(payload.get("enabled"), bool)
    if typ == "ctrl.persona":
        # key 固定三档;空串/None = 回默认人设;其余一律拒(4400 关连接)。
        persona = payload.get("persona")
        return persona is None or (
            isinstance(persona, str) and (persona == "" or persona in PERSONA_KEYS)
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
                if event.get("outcome") == "failure":
                    _schedule_command_status(COMMAND_FAILURE)

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


def _schedule_voice_welcome(tag: str = "voice welcome") -> None:
    """欢迎语统一入口:优先走 agent 侧异步包装(先热应用面板配置再说,修池化
    worker 复连后第一句仍是旧音色);handler 未注册(旧代码/单测)回落同步直说。"""
    aloop = runtime.agent_loop
    if aloop is None or not aloop.is_running():
        logger.info("%s skipped: agent loop not ready", tag)
        return
    handler = runtime.voice_welcome_handler
    if handler is not None:
        logger.info("%s scheduled (config pre-apply)", tag)
        asyncio.run_coroutine_threadsafe(handler(), aloop)
        return
    logger.info("%s scheduled", tag)
    aloop.call_soon_threadsafe(say_voice_welcome)


def _schedule_session_attach_reset() -> None:
    """会话 attach 重置 marshal(web 线程 → agent 循环)。

    agent 注册了 session_attach_handler 就走它(热应用面板配置 + 清脏
    standby + 撤遗留定时器 + 重臂满额 idle);旧代码/单测没注册则回落
    只撤定时器——无论哪路,池化 worker 都不再带幽灵定时器进新会话。
    """
    aloop = runtime.agent_loop
    if aloop is None or not aloop.is_running():
        return
    handler = runtime.session_attach_handler
    if handler is not None:
        asyncio.run_coroutine_threadsafe(handler(), aloop)
        return
    aloop.call_soon_threadsafe(standby_cancel_all)


def _set_session_duplex(enabled: bool) -> None:
    """当前会话内全双工开关:不落 agent_config.json,下次 ctrl.hello 重置为默认开。"""
    runtime.duplex_enabled = enabled
    append_turn_log(f"SESSION_DUPLEX_SET duplex={'on' if enabled else 'off'}")


def _schedule_session_duplex(enabled: bool) -> bool:
    aloop = runtime.agent_loop
    if aloop is None or not aloop.is_running():
        return False
    aloop.call_soon_threadsafe(_set_session_duplex, enabled)
    return True


def _set_session_persona(persona: str) -> None:
    """当前会话内性格选择:存 key(热引用,预设内容 POST 后下一轮生效)。

    非法/空 → none(双保险:P0 校验已拦非法 key;空=隐藏回落全局人设)。
    显式选择置 session_persona_explicit——只有它才 mask 全局自定义完整提示词
    (instructions_override 让位);attach 落的默认档不算显式。
    生效时机 = 下一轮次起点(_maybe_apply_agent_config 按生效配置对比应用);
    新会话 attach 落回默认档(config_store.DEFAULT_PERSONA_KEY)。
    """
    key = persona if persona in PERSONA_KEYS else None
    runtime.session_persona = key
    runtime.session_persona_explicit = key is not None
    append_turn_log(f"SESSION_PERSONA_SET persona={key or 'none'}")


def _schedule_session_persona(persona: str) -> bool:
    aloop = runtime.agent_loop
    if aloop is None or not aloop.is_running():
        return False
    aloop.call_soon_threadsafe(_set_session_persona, persona)
    return True


def _schedule_command_status(text: str) -> None:
    aloop = runtime.agent_loop
    if aloop is not None and aloop.is_running():
        aloop.call_soon_threadsafe(say_command_status, text)


def _schedule_command_result(text: str) -> None:
    aloop = runtime.agent_loop
    if aloop is not None and aloop.is_running():
        aloop.call_soon_threadsafe(say_command_result, text)


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
        _schedule_voice_welcome(tag="local welcome")

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


async def _handle_feedback(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """POST /api/feedback {text, contact?}:问题与反馈 → data/feedback/ 逐条落盘(.md)。

    产品功能路由(同 /api/mic 始终注册,不随 ADMIN_ROUTES 隐藏);鉴权在网关层
    (apikey 准入 + 无亲和反代,同 /api/knows/*):任意 READY agent 都能处理,
    每条反馈独立文件 + 文件名随机后缀,两 worker 并发写也不撞名。
    """
    try:
        data = await request.json()
    except Exception:
        return aiohttp.web.json_response({"error": "invalid json"}, status=400)
    text = str(data.get("text", "")).strip()
    contact = str(data.get("contact", "")).strip()
    if not text:
        return aiohttp.web.json_response({"error": "text is empty"}, status=400)
    if len(text) > _FEEDBACK_TEXT_MAX_CHARS:
        return aiohttp.web.json_response(
            {"error": f"text too long (max {_FEEDBACK_TEXT_MAX_CHARS} chars)"}, status=400
        )
    if len(contact) > _FEEDBACK_CONTACT_MAX_CHARS:
        return aiohttp.web.json_response(
            {"error": f"contact too long (max {_FEEDBACK_CONTACT_MAX_CHARS} chars)"}, status=400
        )
    now = time.time()
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    name = f"feedback-{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}-{secrets.token_hex(2)}.md"
    body = (
        f"# 问题与反馈\n\n"
        f"- 提交时间：{ts}\n"
        f"- 联系方式：{contact or '（未填写）'}\n"
        f"- 来源：webpanel\n\n"
        f"## 内容\n\n{text}\n"
    )
    try:
        _FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
        (_FEEDBACK_DIR / name).write_text(body, encoding="utf-8")
    except Exception as exc:
        logger.exception("failed to save feedback")
        return aiohttp.web.json_response({"error": f"save failed: {exc}"}, status=500)
    logger.info("feedback saved: %s (%d chars)", name, len(text))
    append_turn_log(f"FEEDBACK_SAVED file={name} chars={len(text)}")
    return aiohttp.web.json_response({"ok": True, "file": name})


async def _handle_agent_config_get(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """GET /api/agentconfig:当前配置 + 音色目录/输入上限(面板"小歌设置"渲染)。

    instructions = 当前完整提示词自定义(None=默认拼装);default_instructions =
    按当前人设拼装的默认全文——弹框预填与「恢复默认」的权威来源(与网关侧逐字一致)。
    """
    cfg = load_agent_config()
    return aiohttp.web.json_response({
        "persona_name": cfg.persona_name,
        "persona_traits": cfg.persona_traits,
        "tts_voice": cfg.tts_voice,
        "duplex_enabled": True,
        "duplex_scope": "session",
        "instructions": cfg.instructions_override,
        "default_instructions": default_instructions(cfg),
        # 会话性格预设目录(2026-09-22,仅 -97):选择是会话态(ctrl.persona 帧),
        # 编辑走 POST 的 personas 参数——GET 只读目录,POST 响应不带(与旧契约
        # 最小差:面板保存后重新 GET 刷新)。
        "personas": personas_public(cfg),
        "voices": VOICES,
        "limits": {
            "persona_name": PERSONA_NAME_MAX_CHARS,
            "persona_traits": PERSONA_TRAITS_MAX_CHARS,
            "instructions": INSTRUCTIONS_MAX_CHARS,
        },
    })


async def _handle_agent_config_post(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Persistent Xiaoge configuration is managed by the backend, not this panel."""
    return aiohttp.web.json_response(
        {"error": "agent configuration is managed by the backend"}, status=403
    )


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
    # 问题与反馈(产品功能,始终注册):网关层 apikey 准入 + 无亲和反代,逐条落盘 data/feedback/
    app.router.add_post("/api/feedback", _handle_feedback)
    # 项目配置由 RuoYi 后台管理；面板保留路径仅为旧客户端返回只读拒绝。
    app.router.add_route("GET", "/api/agentconfig", _handle_agent_config_post)
    app.router.add_post("/api/agentconfig", _handle_agent_config_post)
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
