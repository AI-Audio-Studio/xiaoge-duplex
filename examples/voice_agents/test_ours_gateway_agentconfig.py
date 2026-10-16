"""Regression tests for legacy agent-config and prompt-export gateway routes."""

from __future__ import annotations

import asyncio

import aiohttp
import gateway.main as gw
from aiohttp import web


class _StubPool:
    async def status(self):
        return {"size": 1, "ready": 0, "assigned": 0}

    async def close(self):
        return None

    async def list_ready(self):
        return []


class _ReadyPool(_StubPool):
    async def list_ready(self):
        return [{"port": 8123}]


class _StubApiKeys:
    def authorize(self, presented: str | None) -> bool:
        return presented == "verified-key"


def _app(pool=None):
    cfg = gw.GatewayConfig(api_key_required=True, api_keys_static="verified-key")
    table = gw.af.AffinityTable(grace_seconds=1.0, secret="test-secret")
    proxy = gw.Proxy(cfg, table)
    app = gw.build_gateway_app(cfg, table, proxy, pool or _StubPool(), _StubApiKeys())
    return app, proxy


async def _run_request(path: str, method: str = "GET", *, body=None, headers=None, pool=None):
    app, proxy = _app(pool)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(
                method, f"http://127.0.0.1:{port}{path}", json=body, headers=headers
            ) as response:
                return response.status, dict(response.headers), await response.read()
    finally:
        await runner.cleanup()
        await proxy.aclose()


def test_get_agent_config_is_not_exposed():
    async def scenario():
        status, _, body = await _run_request(
            "/api/agentconfig", headers={"X-API-Key": "verified-key"}
        )
        assert status == 503
        assert b"resource_exhausted" in body

    asyncio.run(scenario())


def test_post_agent_config_is_rejected():
    async def scenario():
        status, _, body = await _run_request(
            "/api/agentconfig",
            "POST",
            body={"persona_name": "should-not-save"},
            headers={"X-API-Key": "verified-key"},
        )
        assert status == 403
        assert b"managed by the backend" in body

    asyncio.run(scenario())


def test_agent_config_requires_authenticated_key():
    async def scenario():
        status, _, body = await _run_request("/api/agentconfig")
        assert status == 401
        assert b"auth_failed" in body

    asyncio.run(scenario())


def test_prompt_export_requires_authenticated_key():
    async def scenario():
        status, _, body = await _run_request(
            "/api/prompt-export", headers={"X-API-Key": "untrusted"}
        )
        assert status == 401
        assert b"auth_failed" in body

    asyncio.run(scenario())


def test_prompt_export_forwards_verified_key_and_download_headers(monkeypatch):
    async def scenario():
        captured = {}

        async def fake_proxy(self, request, port, *, prefix, trusted_api_key=None):
            captured.update(port=port, prefix=prefix, trusted_api_key=trusted_api_key)
            return web.Response(
                text="prompt 内容",
                headers={"Content-Disposition": 'attachment; filename="test.txt"'},
                content_type="text/plain",
            )

        monkeypatch.setattr(gw._Router, "_proxy_to_port", fake_proxy)
        status, headers, body = await _run_request(
            "/api/prompt-export",
            headers={"X-API-Key": "verified-key", "X-XG-API-Key": "forged-key"},
            pool=_ReadyPool(),
        )
        assert status == 200
        assert headers["Content-Disposition"] == 'attachment; filename="test.txt"'
        assert headers["Content-Type"].startswith("text/plain")
        assert body.decode() == "prompt 内容"
        assert captured == {
            "port": 8123,
            "prefix": "/api/prompt-export",
            "trusted_api_key": "verified-key",
        }

    asyncio.run(scenario())


def test_prompt_export_returns_503_without_ready_worker():
    async def scenario():
        status, _, body = await _run_request(
            "/api/prompt-export", headers={"X-API-Key": "verified-key"}
        )
        assert status == 503
        assert b"resource_exhausted" in body

    asyncio.run(scenario())
