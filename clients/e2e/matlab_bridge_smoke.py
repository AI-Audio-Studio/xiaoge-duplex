from __future__ import annotations

import argparse
import asyncio
import json
import sys
import wave
from pathlib import Path


FRAME_SAMPLES = 320
FRAME_BYTES = FRAME_SAMPLES * 2


def read_wav_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as reader:
        if (reader.getframerate(), reader.getnchannels(), reader.getsampwidth()) != (16000, 1, 2):
            raise SystemExit(f"expected 16k/mono/int16 WAV: {path}")
        return reader.readframes(reader.getnframes())


async def read_events(reader: asyncio.StreamReader, seen: list[dict[str, object]]) -> None:
    while True:
        line = await reader.readline()
        if not line:
            return
        try:
            seen.append(json.loads(line.decode("utf-8")))
        except json.JSONDecodeError:
            seen.append({"type": "invalid-json", "raw": line.decode("utf-8", errors="replace")})


def read_trace_types(path: Path) -> list[str]:
    out: list[str] = []
    if not path.exists():
        return out
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            out.append(str(json.loads(line).get("message_type", "")))
    return out


async def run(args: argparse.Namespace) -> int:
    pcm = read_wav_pcm(Path(args.wav))

    events_reader, events_writer = await asyncio.open_connection(args.host, args.events)
    down_reader, down_writer = await asyncio.open_connection(args.host, args.down)
    _up_reader, up_writer = await asyncio.open_connection(args.host, args.up)
    seen_events: list[dict[str, object]] = []
    event_task = asyncio.create_task(read_events(events_reader, seen_events))

    await asyncio.sleep(1.0)
    for offset in range(0, len(pcm), FRAME_BYTES):
        up_writer.write(pcm[offset : offset + FRAME_BYTES])
        await up_writer.drain()
        await asyncio.sleep(0.002)

    received = bytearray()
    deadline = asyncio.get_running_loop().time() + args.timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            chunk = await asyncio.wait_for(down_reader.read(4096), timeout=0.1)
        except asyncio.TimeoutError:
            chunk = b""
        if chunk:
            received.extend(chunk)
        trace_types = read_trace_types(Path(args.trace_log))
        if "data.cmd_ack" in trace_types and "data.cmd_result" in trace_types:
            break

    up_writer.close()
    down_writer.close()
    events_writer.close()
    await asyncio.gather(
        up_writer.wait_closed(),
        down_writer.wait_closed(),
        events_writer.wait_closed(),
        return_exceptions=True,
    )
    event_task.cancel()
    await asyncio.gather(event_task, return_exceptions=True)

    event_types = [str(event.get("type", "")) for event in seen_events]
    trace_types = read_trace_types(Path(args.trace_log))
    required_trace = {"session.created", "ctrl.hello", "ctrl.ready", "data.cmd", "data.cmd_ack", "data.cmd_result"}
    missing = sorted(required_trace.difference(trace_types))
    if missing:
        print(f"missing trace types: {missing}", file=sys.stderr)
        print(f"trace_types={trace_types}", file=sys.stderr)
        return 1
    if "ctrl.ready.local" not in event_types or "data.cmd" not in event_types:
        print(f"missing bridge event types, got {event_types}", file=sys.stderr)
        return 1

    print(
        "records=matlab-bridge-smoke "
        f"sent={len(pcm)} received={len(received)} "
        f"events={','.join(event_types)} "
        f"trace_types={','.join(trace_types)} failures=0"
    )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke test the MATLAB TCP bridge.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--up", type=int, required=True)
    parser.add_argument("--down", type=int, required=True)
    parser.add_argument("--events", type=int, required=True)
    parser.add_argument("--wav", required=True)
    parser.add_argument("--trace-log", required=True)
    parser.add_argument("--timeout", type=float, default=5.0)
    return parser.parse_args()


def main() -> int:
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
