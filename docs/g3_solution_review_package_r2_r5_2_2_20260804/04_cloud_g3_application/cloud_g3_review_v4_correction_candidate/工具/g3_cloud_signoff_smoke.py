#!/usr/bin/env python3
"""Run a credential-safe G3 smoke against a deployed gateway."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import ssl
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp


@dataclass
class Report:
    base_url: str
    tls_verification: str
    checks: list[dict[str, object]] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)

    def add(self, name: str, passed: bool, detail: str) -> None:
        self.checks.append(
            {"name": name, "status": "PASS" if passed else "FAIL", "detail": self.redact(detail)}
        )

    def redact(self, value: object) -> str:
        text = str(value)
        for secret in sorted((item for item in self.secrets if item), key=len, reverse=True):
            text = text.replace(secret, "<redacted>")
        return text

    def payload(self) -> dict[str, object]:
        return {
            "tool": "g3_cloud_signoff_smoke",
            "version": 1,
            "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "base_url": self.base_url,
            "tls_verification": self.tls_verification,
            "overall": "PASS" if all(item["status"] == "PASS" for item in self.checks) else "FAIL",
            "checks": self.checks,
        }


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Gateway URL, without credentials")
    parser.add_argument("--output", required=True, type=Path, help="Sanitized JSON output path")
    parser.add_argument(
        "--api-key-env",
        default="G3_SMOKE_API_KEY",
        help="Environment variable to read; prompts securely when unset",
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument(
        "--tls-cert-sha256",
        help="Expected server certificate SHA-256 for pinned internal test endpoints",
    )
    return parser.parse_args()


def _ws_url(base_url: str, path: str) -> str:
    parsed = urlsplit(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


def _session_payload(caps: list[str] | None = None) -> dict[str, object]:
    return {
        "device_id": "g3-signoff-smoke",
        "credential": {"key_id": "signoff-smoke", "signature": "ephemeral"},
        "caps": caps or ["audio", "text", "cmd", "state"],
        "prefs": {"welcome.enabled": False},
        "audio_format": {"sample_rate": 16000, "channels": 1, "sample_format": "int16le"},
        "client_version": "g3-signoff-smoke-r5.2.2",
    }


async def _create_session(
    client: aiohttp.ClientSession,
    base_url: str,
    api_key: str,
    report: Report,
    caps: list[str] | None = None,
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + 15.0
    while True:
        async with client.post(
            f"{base_url}/create_session",
            headers={"X-API-Key": api_key},
            json=_session_payload(caps),
        ) as response:
            body = await response.json(content_type=None)
            if response.status == 200:
                token = str(body.get("access_token") or "")
                if not token:
                    raise RuntimeError("create_session response has no access_token")
                report.secrets.append(token)
                return body
            if response.status != 503 or asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError(f"create_session status={response.status} code={body.get('code')}")
        await asyncio.sleep(0.25)


async def _closed_code(ws: aiohttp.ClientWebSocketResponse) -> int | None:
    while not ws.closed:
        message = await ws.receive()
        if message.type in {
            aiohttp.WSMsgType.CLOSE,
            aiohttp.WSMsgType.CLOSED,
            aiohttp.WSMsgType.ERROR,
        }:
            break
    return ws.close_code


async def _receive_json(
    ws: aiohttp.ClientWebSocketResponse,
    wanted: set[str],
    timeout: float,
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        remaining = deadline - asyncio.get_running_loop().time()
        message = await ws.receive(timeout=remaining)
        if message.type == aiohttp.WSMsgType.TEXT:
            payload = json.loads(message.data)
            if payload.get("type") in wanted:
                return payload
        elif message.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED}:
            raise RuntimeError(f"websocket closed code={ws.close_code}")
    raise TimeoutError(f"timed out waiting for {sorted(wanted)}")


def _hello(session: dict[str, Any], caps: list[str] | None = None) -> dict[str, object]:
    return {
        "type": "ctrl.hello",
        "trace_id": session["trace_id"],
        "session_id": session["session_id"],
        "proto": 2,
        "role": "device",
        "device_id": "g3-signoff-smoke",
        "caps": caps or ["audio", "text", "cmd", "state"],
    }


async def _auth_matrix(
    client: aiohttp.ClientSession,
    base_url: str,
    session: dict[str, Any],
    report: Report,
) -> None:
    ws_url = _ws_url(base_url, "/ws/session")
    token = session["access_token"]
    query_ws = await client.ws_connect(f"{ws_url}?access_token={token}")
    query_code = await _closed_code(query_ws)
    report.add("formal_query_only_rejected", query_code == 4401, f"close_code={query_code}")

    mixed_ws = await client.ws_connect(
        f"{ws_url}?access_token={token}", headers={"Authorization": f"Bearer {token}"}
    )
    mixed_code = await _closed_code(mixed_ws)
    report.add("formal_header_plus_query_rejected", mixed_code == 4401, f"close_code={mixed_code}")


async def _command_path(
    client: aiohttp.ClientSession,
    base_url: str,
    session: dict[str, Any],
    report: Report,
    timeout: float,
) -> None:
    token = session["access_token"]
    ws = await client.ws_connect(
        _ws_url(base_url, "/ws/session"), headers={"Authorization": f"Bearer {token}"}
    )
    try:
        await ws.send_json(_hello(session))
        ready = await _receive_json(ws, {"ctrl.ready"}, timeout)
        report.add(
            "formal_bearer_handshake",
            ready.get("granted_caps") == ["audio", "text", "cmd", "state"],
            f"type={ready.get('type')} caps={ready.get('granted_caps')}",
        )

        await ws.send_json(
            {
                "type": "data.text",
                "trace_id": session["trace_id"],
                "session_id": session["session_id"],
                "utterance_id": "utt-signoff-single",
                "text": "往前走一米",
                "final": True,
                "ts_ms": int(time.time() * 1000),
            }
        )
        command = await _receive_json(ws, {"data.cmd"}, timeout)
        report.add(
            "single_command_dry_run",
            command.get("action") == "navigation.move",
            f"type={command.get('type')} action={command.get('action')}",
        )
        common = {
            "trace_id": command["trace_id"],
            "session_id": command["session_id"],
            "utterance_id": command["utterance_id"],
            "cmd_id": command["cmd_id"],
        }
        await ws.send_json(
            {
                **common,
                "type": "data.cmd_ack",
                "status": "accepted",
                "code": "signoff_fake_received",
                "received_at_ms": int(time.time() * 1000),
            }
        )
        await ws.send_json(
            {
                **common,
                "type": "data.cmd_result",
                "status": "running",
                "code": "signoff_fake_running",
            }
        )
        await ws.send_json(
            {
                **common,
                "type": "data.cmd_result",
                "status": "succeeded",
                "code": "signoff_fake_done",
            }
        )
        report.add("fake_ack_running_succeeded", True, "frames_sent=true")

        await ws.send_json(
            {
                **common,
                "type": "data.cmd_ack",
                "cmd_id": "cmd-signoff-unknown",
                "status": "accepted",
                "code": "signoff_unknown",
                "received_at_ms": int(time.time() * 1000),
            }
        )
        error = await _receive_json(ws, {"data.error"}, timeout)
        report.add(
            "unknown_cmd_id",
            error.get("code") == "unknown_cmd_id",
            f"type={error.get('type')} code={error.get('code')}",
        )

        await ws.send_json(
            {
                "type": "data.text",
                "trace_id": session["trace_id"],
                "session_id": session["session_id"],
                "utterance_id": "utt-signoff-multi",
                "text": "往前走一米再挥手",
                "final": True,
                "ts_ms": int(time.time() * 1000),
            }
        )
        reply = await _receive_json(ws, {"data.reply", "data.cmd"}, timeout)
        report.add(
            "multi_command_ask_split",
            reply.get("type") == "data.reply" and "cmd_id" not in reply,
            f"type={reply.get('type')} has_cmd_id={'cmd_id' in reply}",
        )
    finally:
        await ws.close()


async def _hello_token_rejected(
    client: aiohttp.ClientSession,
    base_url: str,
    session: dict[str, Any],
    report: Report,
) -> None:
    token = session["access_token"]
    ws = await client.ws_connect(
        _ws_url(base_url, "/ws/session"), headers={"Authorization": f"Bearer {token}"}
    )
    hello = _hello(session)
    hello["token"] = token
    await ws.send_json(hello)
    close_code = await _closed_code(ws)
    report.add("ctrl_hello_token_rejected", close_code == 4400, f"close_code={close_code}")


async def _caps_trimmed(
    client: aiohttp.ClientSession,
    base_url: str,
    session: dict[str, Any],
    report: Report,
    timeout: float,
) -> None:
    token = session["access_token"]
    ws = await client.ws_connect(
        _ws_url(base_url, "/ws/session"), headers={"Authorization": f"Bearer {token}"}
    )
    try:
        await ws.send_json(_hello(session, ["audio", "text", "cmd", "state"]))
        ready = await _receive_json(ws, {"ctrl.ready"}, timeout)
        report.add(
            "hello_caps_cannot_escalate",
            ready.get("granted_caps") == ["audio", "text", "cmd"],
            f"caps={ready.get('granted_caps')}",
        )
    finally:
        await ws.close()


async def _run(args: argparse.Namespace, api_key: str, report: Report) -> None:
    timeout = aiohttp.ClientTimeout(total=args.timeout)
    ssl_value: bool | aiohttp.Fingerprint = True
    if args.tls_cert_sha256:
        try:
            digest = bytes.fromhex(args.tls_cert_sha256)
        except ValueError as exc:
            raise RuntimeError("tls certificate fingerprint must be hexadecimal") from exc
        if len(digest) != 32:
            raise RuntimeError("tls certificate fingerprint must contain 64 hexadecimal characters")
        ssl_value = aiohttp.Fingerprint(digest)
    connector = aiohttp.TCPConnector(ssl=ssl_value)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as client:
        async with client.get(f"{report.base_url}/") as response:
            page = await response.text()
            page_ok = (
                response.status == 200
                and 'id="apiKeyInput"' in page
                and "var DEMO_QUERY_TOKEN_ENABLED=false;" in page
                and "DEFAULT_RUOYI_API_KEY" not in page
                and "localStorage" not in page
            )
            report.add("demo_requires_user_key", page_ok, f"status={response.status}")

        async with client.get(f"{report.base_url}/debug/ws/session") as response:
            report.add("debug_query_route_disabled", response.status == 404, f"status={response.status}")

        first = await _create_session(client, report.base_url, api_key, report)
        await _auth_matrix(client, report.base_url, first, report)
        await _command_path(client, report.base_url, first, report, args.timeout)

        reconnect = await _create_session(client, report.base_url, api_key, report)
        token = reconnect["access_token"]
        ws = await client.ws_connect(
            _ws_url(report.base_url, "/ws/session"),
            headers={"Authorization": f"Bearer {token}"},
        )
        await ws.send_json(_hello(reconnect))
        ready = await _receive_json(ws, {"ctrl.ready"}, args.timeout)
        report.add("bearer_reconnect_new_session", ready.get("type") == "ctrl.ready", "ready=true")
        await ws.close()

        hello_token_session = await _create_session(client, report.base_url, api_key, report)
        await _hello_token_rejected(client, report.base_url, hello_token_session, report)

        caps_session = await _create_session(
            client, report.base_url, api_key, report, ["audio", "text", "cmd"]
        )
        await _caps_trimmed(client, report.base_url, caps_session, report, args.timeout)


def main() -> int:
    args = _args()
    base_url = args.base_url.rstrip("/")
    if urlsplit(base_url).scheme != "https":
        print("signoff smoke requires an https:// base URL", file=sys.stderr)
        return 2
    api_key = os.getenv(args.api_key_env) or getpass.getpass("API Key (input hidden): ")
    if not api_key:
        print("API Key is required", file=sys.stderr)
        return 2

    tls_verification = "system_ca_and_hostname"
    if args.tls_cert_sha256:
        tls_verification = f"certificate_sha256_pin:{args.tls_cert_sha256[:16]}"
    report = Report(base_url=base_url, tls_verification=tls_verification, secrets=[api_key])
    try:
        asyncio.run(_run(args, api_key, report))
    except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, RuntimeError, ssl.SSLError) as exc:
        report.add("unhandled_smoke_error", False, f"{type(exc).__name__}: {report.redact(exc)}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report.payload(), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"sanitized summary written to {args.output}")
    return 0 if report.payload()["overall"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
