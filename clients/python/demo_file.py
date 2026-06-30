"""文件 demo(无需声卡):把一个 wav 发给小歌,把收到的音频(TTS)存成 wav。

用法::  python demo_file.py <host> <port> <in.wav> [out.wav]
要求 in.wav 为 16kHz / 单声道 / 16-bit PCM(与协议一致)。仅依赖标准库 wave + SDK。
"""

from __future__ import annotations

import asyncio
import sys
import wave

from xiaoge_client import NUM_CHANNELS, SAMPLE_RATE, SAMPLE_WIDTH, XiaogeClient

FRAME_MS = 20
FRAME_BYTES = SAMPLE_RATE * SAMPLE_WIDTH * FRAME_MS // 1000  # 20ms = 640 字节


def _read_wav(path: str) -> bytes:
    with wave.open(path, "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (
            SAMPLE_RATE,
            NUM_CHANNELS,
            SAMPLE_WIDTH,
        ):
            raise SystemExit(f"in.wav 必须是 {SAMPLE_RATE}Hz/单声道/16-bit:{path}")
        return w.readframes(w.getnframes())


def _write_wav(path: str, pcm: bytes) -> None:
    with wave.open(path, "wb") as w:
        w.setframerate(SAMPLE_RATE)
        w.setnchannels(NUM_CHANNELS)
        w.setsampwidth(SAMPLE_WIDTH)
        w.writeframes(pcm)


async def _run(host: str, port: int, in_wav: str, out_wav: str) -> None:
    pcm = _read_wav(in_wav)
    received = bytearray()
    client = XiaogeClient(host, port)
    client.on_audio = received.extend
    client.on_clear = received.clear  # 打断:丢弃已收(模拟停止播放)
    client.on_busy = lambda m: print("服务器忙:", m)

    task = asyncio.create_task(client.run())
    await asyncio.sleep(0.3)  # 等握手
    for i in range(0, len(pcm), FRAME_BYTES):
        await client.send_pcm(pcm[i : i + FRAME_BYTES])
        await asyncio.sleep(FRAME_MS / 1000)  # 按实时速率发
    await asyncio.sleep(3.0)  # 等回复尾巴
    await client.close()
    task.cancel()
    _write_wav(out_wav, bytes(received))
    print(f"已发送 {len(pcm)} 字节,收到 {len(received)} 字节 → {out_wav}")


def main() -> None:
    if len(sys.argv) < 4:
        raise SystemExit("用法: python demo_file.py <host> <port> <in.wav> [out.wav]")
    host, port, in_wav = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    out_wav = sys.argv[4] if len(sys.argv) > 4 else "xiaoge_reply.wav"
    asyncio.run(_run(host, port, in_wav, out_wav))


if __name__ == "__main__":
    main()
