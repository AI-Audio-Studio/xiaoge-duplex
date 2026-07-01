"""xiaoge_client 自测:用本地 mock `/ws/audio` 服务验证握手/收发/clear/busy。

运行::  python selftest.py    (仅依赖 websockets)
退出码 0 = 全部通过。这也可作对接方验证环境的最小冒烟测试。
"""

from __future__ import annotations

import asyncio
import json

import websockets
from xiaoge_client import XiaogeClient


async def _mock_normal(conn: websockets.ServerConnection) -> None:
    """正常会话:发 ready → 收到上行 PCM 后回一帧音频 + clear。"""
    await conn.send(json.dumps({"type": "ready", "sample_rate": 16000}))
    got = 0
    async for msg in conn:
        if isinstance(msg, (bytes, bytearray)):
            got += len(msg)
            if got >= 640:  # 收满两帧后回应
                await conn.send(b"\x01\x02" * 160)  # 一帧假 TTS PCM
                await conn.send(json.dumps({"type": "clear"}))
                return


async def _mock_busy(conn: websockets.ServerConnection) -> None:
    await conn.send(json.dumps({"type": "busy", "message": "server busy"}))
    await conn.close()


async def _scenario(handler, port: int) -> dict:
    events: dict = {"ready": None, "audio": 0, "clear": 0, "busy": None}
    async with websockets.serve(handler, "127.0.0.1", port):
        c = XiaogeClient("127.0.0.1", port)
        c.on_ready = lambda sr: events.__setitem__("ready", sr)
        c.on_audio = lambda pcm: events.__setitem__("audio", events["audio"] + len(pcm))
        c.on_clear = lambda: events.__setitem__("clear", events["clear"] + 1)
        c.on_busy = lambda m: events.__setitem__("busy", m)
        task = asyncio.create_task(c.run())
        for _ in range(5):  # 喂几帧上行 PCM(10ms/帧=320 字节)
            await c.send_pcm(b"\x00" * 320)
            await asyncio.sleep(0.02)
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except (asyncio.TimeoutError, websockets.ConnectionClosed):
            await c.close()
    return events


async def main() -> int:
    normal = await _scenario(_mock_normal, 8799)
    assert normal["ready"] == 16000, normal
    assert normal["audio"] == 320, normal  # 收到一帧 160 样本*2 字节
    assert normal["clear"] == 1, normal
    print("[normal] ready=16000 audio=320B clear=1  OK")

    busy = await _scenario(_mock_busy, 8800)
    assert busy["busy"] == "server busy", busy
    print("[busy]   busy='server busy'  OK")

    print("=== 全部通过 ===")  # 任一断言失败会抛出,走不到这里
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
