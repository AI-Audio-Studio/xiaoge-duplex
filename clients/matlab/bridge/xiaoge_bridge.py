"""MATLAB/Simulink TCP bridge for the Xiaoge R5.2.2 client path.

Ports:
  --up      MATLAB -> bridge -> Xiaoge PCM
  --down    Xiaoge TTS PCM -> bridge -> MATLAB
  --events  Xiaoge ctrl/data JSONL + client ack/result JSONL -> MATLAB tools
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))
from xiaoge_client import XiaogeClient, default_ssl_context  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s bridge: %(message)s")
log = logging.getLogger("xiaoge.bridge")


class Bridge:
    def __init__(
        self,
        create_session_url: str,
        device_id: str,
        credential: object,
        *,
        api_key: str = "",
        ssl: object | None = None,
        trace_log_path: str | None = None,
    ) -> None:
        self.client = XiaogeClient(
            create_session_url,
            device_id,
            credential,
            api_key=api_key,
            ssl=ssl,
            trace_log_path=trace_log_path,
        )
        self._down: asyncio.StreamWriter | None = None
        self._events: set[asyncio.StreamWriter] = set()
        self.client.on_ready = lambda sr: self._event({"type": "ctrl.ready.local", "sample_rate": sr})
        self.client.on_audio = self._to_down
        self.client.on_json = self._event

    def _to_down(self, pcm: bytes) -> None:
        w = self._down
        if w is not None and not w.is_closing():
            w.write(pcm)

    def _event(self, payload: dict[str, Any]) -> None:
        line = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        dead = []
        for w in self._events:
            if w.is_closing():
                dead.append(w)
            else:
                w.write(line)
        for w in dead:
            self._events.discard(w)

    async def on_up(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        log.info("up connected %s", writer.get_extra_info("peername"))
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                await self.client.send_pcm(data)
        finally:
            writer.close()
            log.info("up disconnected")

    async def on_down(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        log.info("down connected %s", writer.get_extra_info("peername"))
        self._down = writer
        try:
            await reader.read()
        finally:
            if self._down is writer:
                self._down = None
            writer.close()
            log.info("down disconnected")

    async def on_events(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        log.info("events connected %s", writer.get_extra_info("peername"))
        self._events.add(writer)
        try:
            await reader.read()
        finally:
            self._events.discard(writer)
            writer.close()
            log.info("events disconnected")

    async def serve(
        self,
        up_port: int,
        down_port: int,
        events_port: int,
        *,
        wait_events_client: bool = False,
    ) -> None:
        up = await asyncio.start_server(self.on_up, "0.0.0.0", up_port)
        down = await asyncio.start_server(self.on_down, "0.0.0.0", down_port)
        events = await asyncio.start_server(self.on_events, "0.0.0.0", events_port)
        log.info("bridge ready up=:%d down=:%d events=:%d", up_port, down_port, events_port)
        async with up, down, events:
            while wait_events_client and not self._events:
                await asyncio.sleep(0.01)
            runner = asyncio.create_task(self.client.run())
            while self.client.frontend_state is None:
                await asyncio.sleep(0.01)
            await self.client.send_frontend_state(trust_level="hint", wake_state="awake", vad="unknown")
            await asyncio.gather(runner, up.serve_forever(), down.serve_forever(), events.serve_forever())


def _credential(raw: str) -> object:
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def main() -> None:
    p = argparse.ArgumentParser(description="MATLAB TCP bridge for Xiaoge R5.2.2")
    p.add_argument("create_session_url")
    p.add_argument("device_id")
    p.add_argument("credential", help="JSON object/string credential")
    p.add_argument("--up", type=int, default=5001)
    p.add_argument("--down", type=int, default=5002)
    p.add_argument("--events", type=int, default=5003)
    p.add_argument("--insecure", action="store_true", help="do not verify HTTPS/WSS TLS certs")
    p.add_argument("--ca-cert", default=None, help="PEM CA file for HTTPS/WSS; defaults to ../certs/cloud-ca.pem")
    p.add_argument("--api-key", default="", help="create_session x-api-key header value; defaults to no header")
    p.add_argument("--trace-log", default=None)
    p.add_argument(
        "--wait-events-client",
        action="store_true",
        help="wait until an events TCP client connects before opening WSS; useful for smoke tests",
    )
    a = p.parse_args()
    bridge = Bridge(
        a.create_session_url,
        a.device_id,
        _credential(a.credential),
        api_key=a.api_key,
        ssl=default_ssl_context(ca_cert=a.ca_cert, insecure=a.insecure),
        trace_log_path=a.trace_log,
    )
    try:
        asyncio.run(
            bridge.serve(
                a.up,
                a.down,
                a.events,
                wait_events_client=a.wait_events_client,
            )
        )
    except KeyboardInterrupt:
        print("\nbridge exited")


if __name__ == "__main__":
    main()
