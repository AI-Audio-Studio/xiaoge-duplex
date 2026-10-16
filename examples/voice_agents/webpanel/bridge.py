"""agent→web 的线程安全广播(任何线程可调;web 循环未起时静默 no-op)。"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid

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
# V2(2026-10-12)三态状态帧:listening/standby/state 要派生 R522 会话帧
# (ctrl.state)发给纯 /ws/audio 设备客户端(只连音频通道、无 merged WS,
# session_ws_clients 为空时拿不到 _to_session_frames 的产物)。
# V2.1(2026-10-13):设备固件已只认 ctrl.state.interaction_mode、不再解析
# legacy listening/standby 帧——_to_session_frames 对这三类只产 ctrl.state;
# 原样 JSON 仅走 _ws_broadcast 给浏览器面板(不经过本函数)。
_DEVICE_STATE_TYPES = frozenset({"listening", "standby", "state"})
_fallback_turn_lock = threading.Lock()
_fallback_turn_id: str | None = None


def _correlate_user_turn(msg: dict, *, final: bool) -> str:
    global _fallback_turn_id
    explicit = msg.get("utterance_id")
    if explicit:
        if final:
            with _fallback_turn_lock:
                _fallback_turn_id = None
        return str(explicit)
    with _fallback_turn_lock:
        if _fallback_turn_id is None:
            _fallback_turn_id = f"utt-{uuid.uuid4().hex}"
        utterance_id = _fallback_turn_id
        if final:
            _fallback_turn_id = None
    msg["utterance_id"] = utterance_id
    return utterance_id


def broadcast(msg: dict) -> bool:
    """Thread-safe broadcast and return whether a real command was newly issued."""
    loop = panel.web_loop
    if loop is None or not loop.is_running():
        return False
    typ = msg.get("type")
    if typ == "user_partial":
        _correlate_user_turn(msg, final=False)
    elif typ == "message" and msg.get("role") == "user":
        _correlate_user_turn(msg, final=True)
    frames = _to_session_frames(msg) if panel.session_ws_clients else []
    data = json.dumps(msg, ensure_ascii=False)
    asyncio.run_coroutine_threadsafe(_ws_broadcast(data), loop)
    session_messages = [json.dumps(frame, ensure_ascii=False) for frame in frames]
    if session_messages and panel.session_ws_clients:
        asyncio.run_coroutine_threadsafe(_ws_session_broadcast(session_messages), loop)
    if msg.get("type") in _AUDIO_FORWARD_TYPES and panel.audio_ws_clients:
        asyncio.run_coroutine_threadsafe(_ws_audio_ctrl_broadcast(data), loop)
    elif _v2_mode_on() and msg.get("type") in _DEVICE_STATE_TYPES and panel.audio_ws_clients:
        # V2:纯设备会话(session_ws_clients 为空 → 上面 frames 没算没发)也要拿到
        # 三态。独立重算 _to_session_frames(状态帧只读 mode,无轮次关联副作用)。
        device_frames = _to_session_frames(msg)
        if device_frames:
            asyncio.run_coroutine_threadsafe(
                _ws_audio_ctrl_broadcast(
                    [json.dumps(frame, ensure_ascii=False) for frame in device_frames]
                ),
                loop,
            )
    return any(frame.get("type") == "data.cmd" for frame in frames)


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


def audio_client_count() -> int:
    """当前注册的音频侧 WS 客户端数(/ws/audio 与 R522 会话连接都算), 静音泵门控依据。"""
    return len(panel.audio_ws_clients)


def session_client_count() -> int:
    """当前注册的会话侧 WS 客户端数(/ws R5.2.2 合并连接)。

    idle 定时器无会话停表依据:0 = 池化待命/会话已走(区别于纯文字面板
    ——后者 session_ws 有 audio 无),退出机制 2026-09-15 幽灵待命修复。
    """
    return len(panel.session_ws_clients)


async def _ws_audio_ctrl_broadcast(msgs: str | list[str]) -> None:
    """发给纯 /ws/audio 客户端(不在 merged 会话连接里的);str 单帧或帧列表。"""
    if isinstance(msgs, str):
        msgs = [msgs]
    dead: list[aiohttp.web.WebSocketResponse] = []
    legacy_audio_clients = panel.audio_ws_clients - panel.session_ws_clients
    for ws in list(legacy_audio_clients):
        for data in msgs:
            try:
                await ws.send_str(data)
            except Exception:
                dead.append(ws)
                break
    for ws in dead:
        panel.audio_ws_clients.discard(ws)


def broadcast_audio_ctrl(data: dict) -> None:
    loop = panel.web_loop
    if loop is None or not loop.is_running():
        return
    typ = data.get("type")
    if typ == "user_partial":
        _correlate_user_turn(data, final=False)
    elif typ == "message" and data.get("role") == "user":
        _correlate_user_turn(data, final=True)
    legacy = json.dumps(data, ensure_ascii=False)
    asyncio.run_coroutine_threadsafe(_ws_audio_ctrl_broadcast(legacy), loop)
    session_messages = _to_session_messages(data)
    if session_messages and panel.session_ws_clients:
        asyncio.run_coroutine_threadsafe(_ws_session_broadcast(session_messages), loop)


def _to_session_messages(msg: dict) -> list[str]:
    frames = _to_session_frames(msg)
    return [json.dumps(frame, ensure_ascii=False) for frame in frames]


def _v2_mode_on() -> bool:
    """V2 三态开关(晚导入 interaction_host 避免环:它顶层 import 本模块)。"""
    from app import interaction_host

    return interaction_host.mode_v2_enabled()


def _v2_ctrl_state_frame(trace_id: str, session_id: str, now_ms: int) -> dict:
    """V2:按 runtime.interaction_mode 派生单条 ctrl.state 帧(设备三态协议)。

    sleeping → (sleeping, open, SleepingHot);listening → (listening, open,
    ActiveAgent);dialogue/V1 → (dialogue, open, ActiveAgent)。调用方须已置好
    mode 再广播(状态设置函数的既定顺序),本函数只读不写。
    sleeping 的 gate 2026-10-13 起为 open(按键死锁修复 B):固件对中途
    sleeping(kws_only) 帧会停发音频,而按钮又不重开音频门 → 死锁;gate=open
    让音频照流,唤醒交由 voice_turn/kws_hit 判定(资源仍 SleepingHot)。
    固件按钮本地恢复收音(C)落地后可改回 kws_only。
    """
    from app import interaction_host
    from app.session_state import runtime

    mode = runtime.interaction_mode
    if mode == interaction_host.MODE_SLEEPING:
        gate, resource = "open", "SleepingHot"
    elif mode == interaction_host.MODE_LISTENING:
        gate, resource = "open", "ActiveAgent"
    else:
        mode, gate, resource = "dialogue", "open", "ActiveAgent"
    return {
        "type": "ctrl.state",
        "trace_id": trace_id,
        "session_id": session_id,
        "link_state": "connected",
        "interaction_mode": mode,
        "engine_gate": gate,
        "resource_state": resource,
        "ts_ms": now_ms,
    }


def _filter_protocol_frames(msg: dict) -> list[dict]:
    frames = [frame for frame in msg.get("frames", []) if isinstance(frame, dict)]
    dry_run = bool(msg.get("dry_run", False))
    accepted: list[dict] = []
    for frame in frames:
        if frame.get("type") != "data.cmd":
            accepted.append(frame)
        elif not dry_run and panel.command_lifecycle.issue(frame):
            accepted.append(frame)
    return accepted


def _to_session_frames(msg: dict) -> list[dict]:
    typ = msg.get("type")
    if typ == "g3_protocol":
        frames = _filter_protocol_frames(msg)
    elif isinstance(typ, str) and (typ.startswith("data.") or typ.startswith("ctrl.")):
        if typ != "data.cmd" or panel.command_lifecycle.issue(msg):
            frames = [msg]
        else:
            frames = []
    elif typ == "user_partial":
        trace_id, session_id, now_ms = _frame_context(msg)
        frames = [
            {
                "type": "data.stt",
                "trace_id": trace_id,
                "session_id": session_id,
                "utterance_id": _correlate_user_turn(msg, final=False),
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
                    "utterance_id": _correlate_user_turn(msg, final=True),
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
        if _v2_mode_on():
            # V2:按当前三态派生(设备协议);V1 关闭时保持原字面量不变。
            frames = [_v2_ctrl_state_frame(trace_id, session_id, now_ms)]
        else:
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
    elif typ == "listening":
        trace_id, session_id, now_ms = _frame_context(msg)
        if _v2_mode_on():
            # V2.1(2026-10-13):设备固件只认 ctrl.state.interaction_mode、不再
            # 解析 legacy listening 帧——只发按当前 mode 派生的三态帧
            # (浏览器面板走 _ws_broadcast 原始帧,不受影响);V1 分支字节不变。
            frames = [_v2_ctrl_state_frame(trace_id, session_id, now_ms)]
        else:
            frames = [
                {
                    "type": "listening",
                    "trace_id": trace_id,
                    "session_id": session_id,
                    "on": bool(msg.get("on")),
                    "hint": str(msg.get("hint") or ""),
                    "ts_ms": now_ms,
                }
            ]
    elif typ == "standby":
        # 退出机制(2026-09-15):待命态下发端侧。on=true → 客户端停止发音频;
        # 唤醒=真实用户输入退待命(ctrl.frontend_state 帧已退役 2026-10-13)
        # 后 on=false。reason 记触发来源。
        trace_id, session_id, now_ms = _frame_context(msg)
        if _v2_mode_on():
            # V2.1(2026-10-13):设备不再解析 standby on/off——休眠/唤醒全走
            # ctrl.state(sleeping/dialogue);V1 分支输出字节不变。
            frames = [_v2_ctrl_state_frame(trace_id, session_id, now_ms)]
        else:
            frames = [
                {
                    "type": "standby",
                    "trace_id": trace_id,
                    "session_id": session_id,
                    "on": bool(msg.get("on")),
                    "reason": str(msg.get("reason") or ""),
                    "ts_ms": now_ms,
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
        # V2(2026-10-12):纯设备会话只有 /ws/audio,回落取音频侧主连接
        # 的 X-XG-Session(server 在 /ws/audio 注册时也会写 _xg_session_id)。
        ws = panel.audio_ws_primary_client
    if ws is None:
        return "sess-webpanel"
    session_id = getattr(ws, "_xg_session_id", "")
    return str(session_id or "sess-webpanel")
