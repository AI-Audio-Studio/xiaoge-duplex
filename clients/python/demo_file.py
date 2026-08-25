"""R5.2.2 file demo.

Sends a 16 kHz mono int16 WAV through the R5.2.2 client path and writes
downlink TTS PCM to a WAV file. The create_session URL is provided by the cloud
fake server or test Gateway; this demo doesn't hard-code any production host.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import wave

from xiaoge_client import (
    CommandEvent,
    NUM_CHANNELS,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    CmdAckStatus,
    CmdResultStatus,
    XiaogeClient,
    default_ssl_context,
)

FRAME_MS = 20
FRAME_BYTES = SAMPLE_RATE * SAMPLE_WIDTH * FRAME_MS // 1000


def _read_wav(path: str) -> bytes:
    with wave.open(path, "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (
            SAMPLE_RATE,
            NUM_CHANNELS,
            SAMPLE_WIDTH,
        ):
            raise SystemExit(f"in.wav must be {SAMPLE_RATE}Hz/mono/16-bit PCM: {path}")
        return w.readframes(w.getnframes())


def _write_wav(path: str, pcm: bytes) -> None:
    with wave.open(path, "wb") as w:
        w.setframerate(SAMPLE_RATE)
        w.setnchannels(NUM_CHANNELS)
        w.setsampwidth(SAMPLE_WIDTH)
        w.writeframes(pcm)


def _credential(raw: str) -> object:
    try:
        return json.loads(raw)
    except ValueError:
        return raw


async def _handle_cmd(client: XiaogeClient, event: CommandEvent) -> None:
    print("cmd:", event.cmd_id, event.capability_id, event.action)
    await client.send_command_ack(event, CmdAckStatus.ACCEPTED, "ok")
    await client.send_command_result(event, CmdResultStatus.RUNNING, "ok")
    await client.send_command_result(event, CmdResultStatus.SUCCEEDED, "ok")


async def _run(client: XiaogeClient, in_wav: str, out_wav: str) -> None:
    pcm = _read_wav(in_wav)
    received = bytearray()
    client.on_ready = lambda sr: print("ready", sr)
    client.on_audio = received.extend
    client.on_clear = lambda event: received.clear()
    client.on_reply = lambda event: print("reply:", event.text)
    client.on_command = lambda event: asyncio.create_task(_handle_cmd(client, event))
    client.on_error = lambda event: print("error:", event.code, event.message)

    runner = asyncio.create_task(client.run())
    while client.frontend_state is None:
        await asyncio.sleep(0.01)
    await client.send_frontend_state(trust_level="hint", wake_state="awake", vad="speech")
    for i in range(0, len(pcm), FRAME_BYTES):
        await client.send_pcm(pcm[i : i + FRAME_BYTES])
        await asyncio.sleep(FRAME_MS / 1000)
    await asyncio.sleep(5.0)
    await client.close()
    try:
        await runner
    except asyncio.CancelledError:
        pass
    _write_wav(out_wav, bytes(received))
    print(f"sent={len(pcm)} bytes received={len(received)} bytes out={out_wav}")


def main() -> None:
    p = argparse.ArgumentParser(description="Xiaoge R5.2.2 file demo")
    p.add_argument("create_session_url")
    p.add_argument("device_id")
    p.add_argument("credential", help="JSON object/string credential")
    p.add_argument("in_wav")
    p.add_argument("out_wav", nargs="?", default="xiaoge_reply.wav")
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
    asyncio.run(_run(client, a.in_wav, a.out_wav))


if __name__ == "__main__":
    main()
