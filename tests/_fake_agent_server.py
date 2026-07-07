"""PR-D 集成/浸泡 harness 用的**假 agent 子进程**(非测试用例,以 `_` 前缀避开 pytest 采集)。

由池管理器像真 agent 一样 spawn(读 `WEB_UI_PORT`/`XIAOGE_SESSION_ID`,绑 127.0.0.1),暴露
真 agent 的最小对外面:`GET /healthz`(池探活 `ready` 字段)、`GET /ws/audio`(音频通道,开场
发身份帧 + 回声)、`GET /ws`(状态通道回声)、`POST /api/mic`。**不含任何云/模型依赖**——只做
路由/连接层的全链集成验证(不复活已暂停的音频注入课题)。

/healthz 额外回 `pid` 与 `audio_total`(累计被连过的 /ws/audio 次数):供集成测证"宽限窗内
上游被网关持有、agent 全程只被连一次"(audio_total 不变)与"超时回收后同端口换了新进程"
(pid 变)。生命周期由池管理器持有(kill 回收),本进程不自退。
"""

from __future__ import annotations

import os

import aiohttp.web

_SID = os.getenv("XIAOGE_SESSION_ID", "unknown")
_PORT = int(os.getenv("WEB_UI_PORT", "0"))
_HOST = os.getenv("WEB_UI_HOST", "127.0.0.1")

_state = {"audio_conns": 0, "audio_total": 0}  # 当前活跃 / 累计


async def _healthz(_: aiohttp.web.Request) -> aiohttp.web.Response:
    return aiohttp.web.json_response(
        {
            "ready": True,
            "sid": _SID,
            "pid": os.getpid(),
            "audio_conns": _state["audio_conns"],
            "audio_total": _state["audio_total"],
        }
    )


async def _ws_audio(request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
    ws = aiohttp.web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    _state["audio_conns"] += 1
    _state["audio_total"] += 1
    # 开场身份帧:sid:pid:第几次被连(网关持有上游时,重连不会再触发本帧)。
    await ws.send_str(f"agent:{_SID}:{os.getpid()}:{_state['audio_total']}")
    try:
        async for msg in ws:
            if msg.type == aiohttp.web.WSMsgType.BINARY:
                await ws.send_bytes(b"echo:" + msg.data)
            elif msg.type == aiohttp.web.WSMsgType.TEXT:
                await ws.send_str("echo:" + msg.data)
            elif msg.type in (aiohttp.web.WSMsgType.ERROR, aiohttp.web.WSMsgType.CLOSE):
                break
    finally:
        _state["audio_conns"] -= 1
    return ws


async def _ws_state(request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
    ws = aiohttp.web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    async for msg in ws:
        if msg.type == aiohttp.web.WSMsgType.TEXT:
            await ws.send_str("state:" + msg.data)
        elif msg.type in (aiohttp.web.WSMsgType.ERROR, aiohttp.web.WSMsgType.CLOSE):
            break
    return ws


async def _api_mic(_: aiohttp.web.Request) -> aiohttp.web.Response:
    return aiohttp.web.json_response({"ok": True, "sid": _SID})


def _build() -> aiohttp.web.Application:
    app = aiohttp.web.Application()
    app.router.add_get("/healthz", _healthz)
    app.router.add_get("/ws/audio", _ws_audio)
    app.router.add_get("/ws", _ws_state)
    app.router.add_post("/api/mic", _api_mic)
    return app


if __name__ == "__main__":
    aiohttp.web.run_app(_build(), host=_HOST, port=_PORT, print=None)
