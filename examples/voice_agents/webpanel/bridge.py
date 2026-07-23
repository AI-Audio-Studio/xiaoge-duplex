"""agent→web 的线程安全广播(任何线程可调;web 循环未起时静默 no-op)。"""

from __future__ import annotations

import asyncio
import json

import aiohttp.web

from webpanel.state import panel


async def _ws_broadcast(data: str) -> None:
    dead: list[aiohttp.web.WebSocketResponse] = []
    for ws in list(panel.ws_clients):
        try:
            await ws.send_str(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        panel.ws_clients.discard(ws)


# 转写类消息同步发给 /ws/audio 客户端(嵌入式/SDK 形态只连音频通道也能拿到字幕)。
_AUDIO_FORWARD_TYPES = frozenset({"user_partial", "message"})


def broadcast(msg: dict) -> None:
    """Thread-safe broadcast from any thread to all WebSocket clients."""
    loop = panel.web_loop
    if loop is None or not loop.is_running():
        return
    data = json.dumps(msg, ensure_ascii=False)
    asyncio.run_coroutine_threadsafe(_ws_broadcast(data), loop)
    if msg.get("type") in _AUDIO_FORWARD_TYPES and panel.audio_ws_clients:
        asyncio.run_coroutine_threadsafe(_ws_audio_ctrl_broadcast(data), loop)


async def _ws_audio_broadcast(data: bytes) -> None:
    dead: list[aiohttp.web.WebSocketResponse] = []
    for ws in list(panel.audio_ws_clients):
        try:
            await ws.send_bytes(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        panel.audio_ws_clients.discard(ws)


def broadcast_audio(data: bytes) -> None:
    loop = panel.web_loop
    if loop is None or not loop.is_running() or not panel.audio_ws_clients:
        return
    asyncio.run_coroutine_threadsafe(_ws_audio_broadcast(data), loop)


async def _ws_audio_ctrl_broadcast(msg: str) -> None:
    dead: list[aiohttp.web.WebSocketResponse] = []
    for ws in list(panel.audio_ws_clients):
        try:
            await ws.send_str(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        panel.audio_ws_clients.discard(ws)


def broadcast_audio_ctrl(data: dict) -> None:
    loop = panel.web_loop
    if loop is None or not loop.is_running():
        return
    asyncio.run_coroutine_threadsafe(
        _ws_audio_ctrl_broadcast(json.dumps(data, ensure_ascii=False)), loop
    )
