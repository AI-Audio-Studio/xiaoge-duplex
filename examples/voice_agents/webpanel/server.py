"""Web 控制面板服务(独立线程 + 独立事件循环,aiohttp)。

路由:GET /(static/index.html,后端 tab 按注册表注入)、GET /ws(实时日志/状态)、
POST /api/{mic,asr,tts};WEB_AUDIO=1 时另有 GET /ws/audio(浏览器 PCM 双向)。

web→agent 的一切控制都经 runtime.agent_loop 的 *_threadsafe marshal,绝不直接 await。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
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
    SSL_CERT,
    SSL_KEY,
    WEB_AUDIO,
    WEB_HOST,
    panel,
)

logger = logging.getLogger("web-ui-agent")


def _build_index_html(admin_routes: bool) -> str:
    """读 static/index.html 注入后端 tab(注册表生成,加后端自动出 tab)。
    M5/D-19:`admin_routes` 关则 `<!--BACKEND_TABS-->` **不注入**——开关同控路由注册与 tab。"""
    html = (Path(__file__).resolve().parent / "static" / "index.html").read_text(encoding="utf-8")
    return html.replace("<!--BACKEND_TABS-->", backend_tabs_html() if admin_routes else "")


# 页面加载一次进内存(依模块 ADMIN_ROUTES 定态)。
_INDEX_HTML = _build_index_html(ADMIN_ROUTES)


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
            return aiohttp.web.Response(text=BUSY_HTML, content_type="text/html", charset="utf-8")
    return aiohttp.web.Response(text=_INDEX_HTML, content_type="text/html", charset="utf-8")


async def _handle_healthz(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """就绪/健康探测(池管理器 B4):专用轻路由,不占 WS primary 槽、无会话副作用。"""
    aloop = runtime.agent_loop
    loop_running = aloop is not None and aloop.is_running()
    ready = loop_running and runtime.session is not None
    return aiohttp.web.json_response({"ready": ready, "agent_loop_running": loop_running})


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


def build_web_app(
    *, admin_routes: bool = ADMIN_ROUTES, web_audio: bool = WEB_AUDIO
) -> aiohttp.web.Application:
    """装配面板路由。M5/D-19:`admin_routes` 关(默认)时**不注册** /api/asr·/api/tts,命中即
    404(隐藏 ≠ 仅前端无 tab);/api/mic 属产品功能始终在。与启动/TLS 解耦,便于单测路由集合。"""
    app = aiohttp.web.Application()
    app.router.add_get("/", _handle_index)
    app.router.add_get("/healthz", _handle_healthz)
    app.router.add_get("/ws", _handle_ws)
    app.router.add_post("/api/mic", _handle_mic)
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
