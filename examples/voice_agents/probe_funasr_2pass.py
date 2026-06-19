"""Standalone probe: does the remote FunASR server support 2pass/online (interim) mode?

Connects to FUNASR_WS_URL with mode="2pass", streams a short speech wav as 16k PCM,
and reports whether any `2pass-online` interim messages come back. If they do, option C
(streaming FunASR with interim transcripts -> functional min_words gate) is viable.

Run:
    .venv\\Scripts\\python.exe examples\\voice_agents\\probe_funasr_2pass.py
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import sys
import time
import wave

import aiohttp
from livekit import rtc

WS_URL = os.getenv("FUNASR_WS_URL", "wss://60.205.197.165:10090")
WAV_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "tests", "test_realtime", "weather_question.wav")
TARGET_SR = 16000
CHUNK_BYTES = 3200  # 100ms @ 16k mono 16-bit
SEND_INTERVAL = 0.01


def load_pcm_16k(path: str) -> bytes:
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(w.getnframes())
    print(f"[wav] sr={sr} ch={ch} sampwidth={sw} bytes={len(raw)}")
    if sw != 2:
        raise SystemExit("expected 16-bit PCM wav")
    if sr == TARGET_SR and ch == 1:
        return raw

    resampler = rtc.AudioResampler(
        input_rate=sr,
        output_rate=TARGET_SR,
        num_channels=ch,
        quality=rtc.AudioResamplerQuality.HIGH,
    )
    frame = rtc.AudioFrame(
        data=raw,
        sample_rate=sr,
        num_channels=ch,
        samples_per_channel=len(raw) // (2 * ch),
    )
    frames = resampler.push(frame)
    frames.extend(resampler.flush())
    combined = rtc.combine_audio_frames(frames)
    return bytes(combined.data)


async def main() -> int:
    pcm = load_pcm_16k(WAV_PATH)
    print(f"[pcm] resampled 16k mono bytes={len(pcm)} (~{len(pcm)/2/TARGET_SR:.2f}s)")

    ssl_ctx = None
    if WS_URL.startswith("wss://"):
        ssl_ctx = ssl._create_unverified_context()

    init_payload = {
        "mode": "2pass",
        "chunk_size": [5, 10, 5],
        "chunk_interval": 10,
        "wav_name": "probe-2pass",
        "wav_format": "pcm",
        "audio_fs": TARGET_SR,
        "is_speaking": True,
        "itn": False,
    }

    online_msgs: list[str] = []
    offline_msgs: list[str] = []
    other_modes: set[str] = set()
    all_count = 0

    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        print(f"[ws] connecting {WS_URL} ...")
        async with session.ws_connect(WS_URL, ssl=ssl_ctx, heartbeat=30) as ws:
            print("[ws] connected; sending init (mode=2pass)")
            await ws.send_str(json.dumps(init_payload, ensure_ascii=False))

            async def feed() -> None:
                for i in range(0, len(pcm), CHUNK_BYTES):
                    await ws.send_bytes(pcm[i : i + CHUNK_BYTES])
                    await asyncio.sleep(SEND_INTERVAL)
                await ws.send_str(json.dumps({"is_speaking": False}, ensure_ascii=False))
                print("[ws] finished sending audio + is_speaking:false")

            feeder = asyncio.create_task(feed())

            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline:
                try:
                    msg = await ws.receive(timeout=max(0.1, deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    break
                if msg.type == aiohttp.WSMsgType.TEXT:
                    all_count += 1
                    payload = json.loads(msg.data)
                    mode = payload.get("mode", "<none>")
                    text = payload.get("text", "")
                    is_final = payload.get("is_final")
                    print(f"[recv] mode={mode!r} is_final={is_final} text={text!r}")
                    if mode == "2pass-online":
                        online_msgs.append(text)
                    elif mode == "2pass-offline":
                        offline_msgs.append(text)
                    else:
                        other_modes.add(str(mode))
                    if is_final is True:
                        break
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE):
                    print("[ws] closed by server")
                    break
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    print("[ws] error frame")
                    break

            if not feeder.done():
                feeder.cancel()

    print("\n==== PROBE RESULT ====")
    print(f"total text messages: {all_count}")
    print(f"2pass-online (interim) messages: {len(online_msgs)} -> {online_msgs}")
    print(f"2pass-offline (final)  messages: {len(offline_msgs)} -> {offline_msgs}")
    if other_modes:
        print(f"other modes seen: {sorted(other_modes)}")

    if online_msgs:
        print("\nVERDICT: SUPPORTED. Server emits 2pass-online interim -> option C is viable.")
        return 0
    print("\nVERDICT: NO INTERIM. Server returned no 2pass-online messages.")
    print("Either online models aren't loaded, or server only does offline. Option C blocked.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
