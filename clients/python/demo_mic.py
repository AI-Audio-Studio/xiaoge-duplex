"""R5.2.2 real-time microphone demo."""

from __future__ import annotations

import argparse
import asyncio
import json
import threading

import sounddevice as sd

from xiaoge_client import (
    CommandEvent,
    NUM_CHANNELS,
    SAMPLE_RATE,
    CmdAckStatus,
    CmdResultStatus,
    XiaogeClient,
    default_ssl_context,
)

BLOCK = 320


class _Playback:
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
        return out.ljust(n, b"\x00")


def _credential(raw: str) -> object:
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _print_final_stt(text: str, is_final: bool) -> None:
    if is_final:
        text = text.strip()
        if text:
            print("stt final:", text)


async def _handle_cmd(client: XiaogeClient, event: CommandEvent) -> None:
    print("cmd:", event.cmd_id, event.capability_id, event.action)
    await client.send_command_ack(event, CmdAckStatus.ACCEPTED, "ok")
    await client.send_command_result(event, CmdResultStatus.RUNNING, "ok")
    await asyncio.sleep(1.0)
    await client.send_command_result(event, CmdResultStatus.SUCCEEDED, "ok")


async def _run(client: XiaogeClient) -> None:
    loop = asyncio.get_running_loop()
    up_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=50)
    play = _Playback()

    def mic_cb(indata, frames, time_info, status) -> None:
        loop.call_soon_threadsafe(_offer, up_q, bytes(indata))

    def spk_cb(outdata, frames, time_info, status) -> None:
        outdata[:] = play.take(len(outdata))

    client.on_ready = lambda sr: print("ready", sr)
    client.on_audio = play.push
    client.on_clear = lambda event: play.clear()
    client.on_stt_text = _print_final_stt
    client.on_reply = lambda event: print("reply:", event.text)
    client.on_command = lambda event: asyncio.create_task(_handle_cmd(client, event))
    client.on_error = lambda event: print("error:", event.code, event.message)

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
        runner = asyncio.create_task(client.run())
        while client.frontend_state is None:
            await asyncio.sleep(0.01)
        await client.send_frontend_state(trust_level="hint", wake_state="awake", vad="speech")
        print("talking; press Ctrl-C to exit")
        while not runner.done():
            await client.send_pcm(await up_q.get())


def _offer(q: asyncio.Queue[bytes], data: bytes) -> None:
    if not q.full():
        q.put_nowait(data)


def main() -> None:
    p = argparse.ArgumentParser(description="Xiaoge R5.2.2 microphone demo")
    p.add_argument("create_session_url")
    p.add_argument("device_id")
    p.add_argument("credential", help="JSON object/string credential")
    p.add_argument("--insecure", action="store_true", help="do not verify TLS certs for HTTPS/WSS test env")
    p.add_argument("--ca-cert", default=None, help="PEM CA file for HTTPS/WSS; defaults to ../certs/cloud-ca.pem")
    p.add_argument("--api-key", default="", help="create_session x-api-key header value; defaults to no header")
    p.add_argument("--trace-log", default=None, help="optional client JSONL trace path")
    a = p.parse_args()
    client = XiaogeClient(
        a.create_session_url,
        a.device_id,
        _credential(a.credential),
        api_key=a.api_key,
        ssl=default_ssl_context(ca_cert=a.ca_cert, insecure=a.insecure),
        trace_log_path=a.trace_log,
    )
    try:
        asyncio.run(_run(client))
    except KeyboardInterrupt:
        print("\nexited")


if __name__ == "__main__":
    main()
