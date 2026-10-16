"""Project-isolated prompt and function-tool download tests."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_VOICE_AGENTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_VOICE_AGENTS))

# The worker module imports optional production integrations during import.
backends = types.ModuleType("app.backends")
backends.build_llm = lambda: None
backends.build_tts = lambda: None
backends.build_stt = lambda: None
backends.STT_BACKENDS = {}
backends.TTS_BACKENDS = {}
backends.backend_tabs_html = lambda *args, **kwargs: ""
backends.make_stt_backend = lambda *args, **kwargs: None
backends.make_tts_backend = lambda *args, **kwargs: None
sys.modules.setdefault("app.backends", backends)
providers_config = types.ModuleType("providers.config")
providers_config.funasr_hotwords = lambda: {}
sys.modules.setdefault("providers.config", providers_config)
silero = types.ModuleType("livekit.plugins.silero")
silero.VAD = types.SimpleNamespace(load=lambda *args, **kwargs: None)
sys.modules.setdefault("livekit.plugins.silero", silero)

import webpanel.server as server  # noqa: E402
from app.config_store import AgentConfig, effective_instructions  # noqa: E402


class _Snapshot:
    def __init__(self, config):
        self.config = config
        self.project_name = "private-project-name"


def test_export_uses_project_effective_prompt_and_runtime_tool_schemas(monkeypatch):
    cfg = AgentConfig(persona_name="项目角色", persona_traits="项目特征")
    monkeypatch.setattr(server, "get_project_snapshot", lambda key, force_refresh: _Snapshot(cfg))
    constructed = []

    class FakeAgent:
        def __init__(self, api_key):
            constructed.append(api_key)
            self.instructions = effective_instructions(cfg)
            self.tools = []

    monkeypatch.setattr("web_ui_agent.VoiceAgent", FakeAgent, raising=False)

    async def scenario():
        req = types.SimpleNamespace(headers={"X-XG-API-Key": "project-key"})
        response = await server._handle_prompt_export(req)
        assert response.status == 200
        assert response.content_type == "text/plain"
        assert response.charset == "utf-8"
        assert response.headers["Content-Disposition"].endswith('filename="xiaoge-prompt-and-tools.txt"')
        assert response.headers["Cache-Control"] == "no-store"
        body = response.text
        assert effective_instructions(cfg) in body
        assert "OpenAI-compatible function tool schemas" in body
        assert "private-project-name" not in body
        assert "project-key" not in body
        assert constructed == ["project-key"]
        assert json.loads(body.split("========== Function tools (OpenAI-compatible JSON) ==========\n", 1)[1]) == []

    asyncio.run(scenario())


def test_export_requires_snapshot_and_never_constructs_default_agent(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "get_project_snapshot", lambda key, force_refresh: None)
    monkeypatch.setattr("web_ui_agent.VoiceAgent", lambda key: calls.append(key))

    async def scenario():
        missing = await server._handle_prompt_export(types.SimpleNamespace(headers={}))
        assert missing.status == 401
        response = await server._handle_prompt_export(
            types.SimpleNamespace(headers={"X-XG-API-Key": "unknown-key"})
        )
        assert response.status == 404
        assert json.loads(response.text) == {"error": "project configuration unavailable"}
        assert calls == []

    asyncio.run(scenario())


def test_panel_exposes_header_only_download_contract():
    html = (_VOICE_AGENTS / "webpanel" / "static" / "index.html").read_text(encoding="utf-8")
    assert "downloadPromptExport()" in html
    assert "fetch('/api/prompt-export',{headers:{'X-API-Key':apiKey}})" in html
    assert "link.download='xiaoge-prompt-and-tools.txt'" in html
    assert "URL.createObjectURL(blob)" in html
    assert "apiKey+'" not in html
