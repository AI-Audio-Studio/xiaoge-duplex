"""agent→web 的线程安全广播(任何线程可调;web 循环未起时静默 no-op)。"""

from __future__ import annotations

import asyncio
import json
import time

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


async def _ws_session_broadcast(messages: list[str]) -> None:
    dead: list[aiohttp.web.WebSocketResponse] = []
    for ws in list(panel.session_ws_clients):
        for data in messages:
            try:
                await ws.send_str(data)
            except Exception:
                dead.append(ws)
                break
    for ws in dead:
        panel.session_ws_clients.discard(ws)


# 转写类消息同步发给 /ws/audio 客户端(嵌入式/SDK 形态只连音频通道也能拿到字幕)。
_AUDIO_FORWARD_TYPES = frozenset({"user_partial", "message"})


def broadcast(msg: dict) -> None:
    """Thread-safe broadcast from any thread to all WebSocket clients."""
    loop = panel.web_loop
    if loop is None or not loop.is_running():
        return
    data = json.dumps(msg, ensure_ascii=False)
    asyncio.run_coroutine_threadsafe(_ws_broadcast(data), loop)
    session_messages = _to_session_messages(msg)
    if session_messages and panel.session_ws_clients:
        asyncio.run_coroutine_threadsafe(_ws_session_broadcast(session_messages), loop)
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
    legacy_audio_clients = panel.audio_ws_clients - panel.session_ws_clients
    for ws in list(legacy_audio_clients):
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
    legacy = json.dumps(data, ensure_ascii=False)
    asyncio.run_coroutine_threadsafe(_ws_audio_ctrl_broadcast(legacy), loop)
    session_messages = _to_session_messages(data)
    if session_messages and panel.session_ws_clients:
        asyncio.run_coroutine_threadsafe(_ws_session_broadcast(session_messages), loop)


def _to_session_messages(msg: dict) -> list[str]:
    frames = _to_session_frames(msg)
    return [json.dumps(frame, ensure_ascii=False) for frame in frames]


def _to_session_frames(msg: dict) -> list[dict]:
    typ = msg.get("type")
    if typ == "g3_protocol":
        frames = [frame for frame in msg.get("frames", []) if isinstance(frame, dict)]
        for frame in frames:
            panel.command_lifecycle.issue(frame)
    elif isinstance(typ, str) and (typ.startswith("data.") or typ.startswith("ctrl.")):
        panel.command_lifecycle.issue(msg)
        frames = [msg]
    elif typ == "user_partial":
        trace_id, session_id, now_ms = _frame_context(msg)
        frames = [
            {
                "type": "data.stt",
                "trace_id": trace_id,
                "session_id": session_id,
                "utterance_id": str(msg.get("utterance_id") or f"utt-{now_ms}"),
                "text": str(msg.get("text") or ""),
                "final": False,
                "ts_ms": now_ms,
            }
        ]
    elif typ == "message":
        trace_id, session_id, now_ms = _frame_context(msg)
        role = msg.get("role")
        if role == "user":
            frames = [
                {
                    "type": "data.stt",
                    "trace_id": trace_id,
                    "session_id": session_id,
                    "utterance_id": str(msg.get("utterance_id") or f"utt-{now_ms}"),
                    "text": str(msg.get("text") or ""),
                    "final": True,
                    "ts_ms": now_ms,
                }
            ]
        elif role == "assistant":
            frames = [
                {
                    "type": "data.reply",
                    "trace_id": trace_id,
                    "session_id": session_id,
                    "utterance_id": str(msg.get("utterance_id") or f"utt-{now_ms}"),
                    "intent_type": str(msg.get("intent_type") or "chat"),
                    "text": str(msg.get("text") or ""),
                    "ts_ms": now_ms,
                    "speak_policy": str(msg.get("speak_policy") or "final_only"),
                }
            ]
        else:
            frames = []
    elif typ == "state":
        trace_id, session_id, now_ms = _frame_context(msg)
        frames = [
            {
                "type": "ctrl.state",
                "trace_id": trace_id,
                "session_id": session_id,
                "link_state": "connected",
                "interaction_mode": "dialogue",
                "engine_gate": "open",
                "resource_state": "ActiveAgent",
                "ts_ms": now_ms,
            }
        ]
    elif typ == "clear":
        trace_id, session_id, _ = _frame_context(msg)
        frames = [
            {
                "type": "ctrl.clear",
                "trace_id": trace_id,
                "session_id": session_id,
                "reason": str(msg.get("reason") or "barge_in"),
            }
        ]
    else:
        frames = []
    return frames


def _frame_context(msg: dict) -> tuple[str, str, int]:
    trace_id = str(msg.get("trace_id") or "trace-webpanel")
    session_id = str(msg.get("session_id") or _primary_session_id())
    now_ms = int(float(msg.get("ts", time.time())) * 1000)
    return trace_id, session_id, now_ms


def _primary_session_id() -> str:
    ws = panel.session_ws_primary_client
    if ws is None:
        return "sess-webpanel"
    session_id = getattr(ws, "_xg_session_id", "")
    return str(session_id or "sess-webpanel")
