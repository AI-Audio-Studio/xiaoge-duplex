"""实时 demo:麦克风 ↔ 小歌 ↔ 扬声器(全双工)。

用法::  python demo_mic.py <host> <port>
依赖:websockets + sounddevice(见 requirements.txt)。Ctrl-C 退出。
收到 clear → 立即清空待播缓冲(自然的 barge-in)。
"""

from __future__ import annotations

import asyncio
import sys
import threading

import sounddevice as sd
from xiaoge_client import NUM_CHANNELS, SAMPLE_RATE, XiaogeClient

BLOCK = 320  # 20ms @ 16k(单声道,样本数)


class _Playback:
    """线程安全的播放字节缓冲:on_audio 追加,扬声器回调消费,clear 清空。"""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._lock = threading.Lock()

    def push(self, pcm: bytes) -> None:
        with self._lock:
            self._buf.extend(pcm)

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()

    def take(self, n: int) -> bytes:
        with self._lock:
            out = bytes(self._buf[:n])
            del self._buf[:n]
        return out.ljust(n, b"\x00")  # 不足补静音


async def _run(host: str, port: int) -> None:
    loop = asyncio.get_running_loop()
    up_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=50)
    play = _Playback()

    def mic_cb(indata, frames, time_info, status) -> None:  # sounddevice 线程
        loop.call_soon_threadsafe(_offer, up_q, bytes(indata))

    def spk_cb(outdata, frames, time_info, status) -> None:  # sounddevice 线程
        outdata[:] = play.take(len(outdata))

    client = XiaogeClient(host, port)
    client.on_audio = play.push
    client.on_clear = play.clear
    client.on_busy = lambda m: print("服务器忙:", m)

    mic = sd.RawInputStream(
        samplerate=SAMPLE_RATE,
        channels=NUM_CHANNELS,
        dtype="int16",
        blocksize=BLOCK,
        callback=mic_cb,
    )
    spk = sd.RawOutputStream(
        samplerate=SAMPLE_RATE,
        channels=NUM_CHANNELS,
        dtype="int16",
        blocksize=BLOCK,
        callback=spk_cb,
    )
    with mic, spk:
        print(f"通话中(16k 单声道)→ {host}:{port};Ctrl-C 退出")
        runner = asyncio.create_task(client.run())
        while not runner.done():
            await client.send_pcm(await up_q.get())


def _offer(q: asyncio.Queue, data: bytes) -> None:
    if not q.full():
        q.put_nowait(data)


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit("用法: python demo_mic.py <host> <port>")
    try:
        asyncio.run(_run(sys.argv[1], int(sys.argv[2])))
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
