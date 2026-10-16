#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local proxy for the Xiaoge C demo.

Some Windows/firewall setups block the C demo process from connecting directly to
remote HTTPS/WSS endpoints, while allowing Python to connect out. Run this proxy,
then point the demo at the local create_session URL:

    python clients/c/local_proxy.py
    ./build/xiaoge_demo_file http://127.0.0.1:10097/create_session \
        robot-x3-001 '{"key_id":"dev","signature":"mock"}' in.wav out.wav --insecure

The demo connects locally over HTTP/WS; this proxy connects upstream over
HTTPS/WSS and rewrites create_session.ws_url back to the local WS endpoint.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import ssl
import sys
from pathlib import Path
from typing import Any

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_DEFAULTS: dict[str, Any] = {
    "server_host": "60.205.197.165",
    "server_port": 10099,
    "local_host": "127.0.0.1",
    "local_port": 10097,
    "verify_ssl": False,
}

_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

_WS_PASS_HEADERS = {
    "authorization",
    "x-api-key",
}


def _log(message: str) -> None:
    print(f"[{_dt.datetime.now():%H:%M:%S}] {message}", flush=True)


def _load_config() -> dict[str, Any]:
    cfg = dict(_DEFAULTS)
    cfg_path = Path(__file__).with_name("local_proxy_config.json")
    if cfg_path.exists():
        try:
            loaded = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg.update({k: v for k, v in loaded.items() if v not in (None, "")})
        except Exception as exc:  # noqa: BLE001 - keep startup resilient and user-readable.
            _log(f"local_proxy_config.json read failed, using defaults: {type(exc).__name__}: {exc}")

    cfg["server_host"] = str(cfg["server_host"])
    cfg["server_port"] = int(cfg["server_port"])
    cfg["local_host"] = str(cfg["local_host"])
    cfg["local_port"] = int(cfg["local_port"])
    cfg["verify_ssl"] = bool(cfg["verify_ssl"])
    return cfg


_CFG = _load_config()
_LOCAL_HTTP_BASE = f"http://{_CFG['local_host']}:{_CFG['local_port']}"
_LOCAL_WS_BASE = f"ws://{_CFG['local_host']}:{_CFG['local_port']}"
_UPSTREAM_HTTP_BASE = f"https://{_CFG['server_host']}:{_CFG['server_port']}"
_UPSTREAM_WS_BASE = f"wss://{_CFG['server_host']}:{_CFG['server_port']}"


def _ssl_context() -> ssl.SSLContext | bool:
    if _CFG["verify_ssl"]:
        return ssl.create_default_context()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


_SSL = _ssl_context()


def _filtered_http_headers(request: web.Request) -> dict[str, str]:
    return {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _HOP_BY_HOP_HEADERS | {"host", "content-length", "accept-encoding"}
    }


def _filtered_response_headers(headers: Any) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in _HOP_BY_HOP_HEADERS | {"content-length", "content-encoding"}
    }


def _filtered_ws_headers(request: web.Request) -> dict[str, str]:
    return {key: value for key, value in request.headers.items() if key.lower() in _WS_PASS_HEADERS}


def _rewrite_ws_url(body: bytes, content_type: str) -> bytes:
    if "json" not in content_type.lower():
        return body
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return body
    if isinstance(payload, dict) and isinstance(payload.get("ws_url"), str):
        payload["ws_url"] = f"{_LOCAL_WS_BASE}/ws/session"
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return body


async def handle_http(request: web.Request) -> web.Response:
    upstream_url = f"{_UPSTREAM_HTTP_BASE}{request.rel_url}"
    body = await request.read()
    try:
        async with request.app["client"].request(
            request.method,
            upstream_url,
            data=body if body else None,
            headers=_filtered_http_headers(request),
            ssl=_SSL,
        ) as resp:
            resp_body = await resp.read()
            if request.path == "/create_session":
                resp_body = _rewrite_ws_url(resp_body, resp.headers.get("Content-Type", ""))
            headers = _filtered_response_headers(resp.headers)
            headers["Connection"] = "close"
            _log(f"HTTP {request.method} {request.rel_url} -> {resp.status}")
            return web.Response(status=resp.status, headers=headers, body=resp_body)
    except Exception as exc:  # noqa: BLE001 - proxy should return a clear 502 on relay failures.
        _log(f"HTTP relay error {request.method} {request.rel_url}: {type(exc).__name__}: {exc}")
        return web.Response(status=502, text="proxy upstream error")


async def _pipe_ws(src: web.WebSocketResponse, dst: Any) -> None:
    async for msg in src:
        if msg.type == WSMsgType.TEXT:
            await dst.send_str(msg.data)
        elif msg.type == WSMsgType.BINARY:
            await dst.send_bytes(msg.data)
        elif msg.type == WSMsgType.PING:
            await dst.ping(msg.data)
        elif msg.type == WSMsgType.PONG:
            await dst.pong(msg.data)
        elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
            await dst.close()
            break
        elif msg.type == WSMsgType.ERROR:
            break


async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    local_ws = web.WebSocketResponse(max_msg_size=0, heartbeat=None)
    await local_ws.prepare(request)

    upstream_url = f"{_UPSTREAM_WS_BASE}{request.rel_url}"
    peer = request.remote or "?"
    _log(f"WS client connected {peer} {request.rel_url} -> {upstream_url}")
    try:
        async with request.app["client"].ws_connect(
            upstream_url,
            headers=_filtered_ws_headers(request),
            ssl=_SSL,
            max_msg_size=0,
            heartbeat=None,
        ) as upstream_ws:
            client_to_up = asyncio.create_task(_pipe_ws(local_ws, upstream_ws))
            up_to_client = asyncio.create_task(_pipe_ws(upstream_ws, local_ws))
            done, pending = await asyncio.wait(
                {client_to_up, up_to_client}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            for task in done:
                task.result()
    except Exception as exc:  # noqa: BLE001 - keep proxy alive after a failed session.
        _log(f"WS relay error {request.rel_url}: {type(exc).__name__}: {exc}")
    finally:
        await local_ws.close()
        _log("WS client closed")
    return local_ws


async def _client_session_ctx(app: web.Application):
    app["client"] = ClientSession(timeout=ClientTimeout(total=None, sock_connect=30))
    try:
        yield
    finally:
        await app["client"].close()


def create_app() -> web.Application:
    app = web.Application()
    app.cleanup_ctx.append(_client_session_ctx)
    app.router.add_route("*", "/ws/session", handle_ws)
    app.router.add_route("*", "/{tail:.*}", handle_http)
    return app


def main() -> None:
    _log("Xiaoge C demo local proxy starting...")
    _log(f"  HTTP: {_LOCAL_HTTP_BASE}/create_session -> {_UPSTREAM_HTTP_BASE}/create_session")
    _log(f"  WS  : {_LOCAL_WS_BASE}/ws/session     -> {_UPSTREAM_WS_BASE}/ws/session")
    _log("  Demo create_session URL must use local http://, not https://.")
    if not _CFG["verify_ssl"]:
        _log("  Upstream TLS verification is disabled.")
    web.run_app(create_app(), host=_CFG["local_host"], port=_CFG["local_port"], print=None)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _log("stopped")
