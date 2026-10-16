"""Project-scoped database configuration store tests."""

from __future__ import annotations

import time

from app.config_store import DEFAULT_CONFIG, effective_instructions
from app.project_config_store import ProjectConfigStore, ProjectConfigSnapshot


def _row(project: str, *, voice: str = "longxiaochun_v3", duplex: int = 1, prompt=None):
    return {
        "project_name": project,
        "persona_name": project,
        "persona_traits": f"traits-{project}",
        "tts_voice": voice,
        "duplex_enabled": duplex,
        "instructions_override": prompt,
        "update_time": "2026-09-24 15:00:00",
    }


def test_active_keys_are_project_scoped_and_cached():
    rows = {"key-a": _row("alpha"), "key-b": _row("beta", duplex=0)}
    calls = []

    def query(key, params):
        calls.append((key, params))
        return rows.get(key)

    store = ProjectConfigStore(query, ttl_seconds=60)
    alpha = store.get_for_api_key("key-a")
    beta = store.get_for_api_key("key-b")
    assert alpha is not None and alpha.project_name == "alpha"
    assert beta is not None and beta.project_name == "beta"
    assert alpha.config.persona_name != beta.config.persona_name
    assert beta.config.duplex_enabled is False
    assert store.get_for_api_key("key-a") == alpha
    assert [item[0] for item in calls] == ["key-a", "key-b"]


def test_invalid_or_unknown_key_never_uses_global_config():
    queried = []

    def query(key, params):
        queried.append(key)
        return None

    store = ProjectConfigStore(query, ttl_seconds=60)
    assert store.get_for_api_key("") is None
    assert store.get_for_api_key("unknown") is None
    from app.project_config_store import load_project_config

    # A supplied key must not cross the project boundary into agent_config.json.
    assert load_project_config("unknown") == DEFAULT_CONFIG
    assert queried == ["unknown"]


def test_expired_entry_is_refreshed():
    rows = {"key": _row("old")}
    calls = []

    def query(key, params):
        calls.append(key)
        return rows[key]

    store = ProjectConfigStore(query, ttl_seconds=0.1)
    first = store.get_for_api_key("key")
    rows["key"] = _row("new")
    time.sleep(0.12)
    second = store.get_for_api_key("key")
    assert first is not None and second is not None
    assert first.project_name == "old"
    assert second.project_name == "new"
    assert calls == ["key", "key"]


def test_query_failure_returns_last_successful_snapshot():
    state = {"row": _row("alpha")}

    def query(key, params):
        if state["row"] == "error":
            raise RuntimeError("database unavailable")
        return state["row"]

    store = ProjectConfigStore(query, ttl_seconds=0.1)
    snapshot = store.get_for_api_key("key")
    state["row"] = "error"
    time.sleep(0.12)
    assert store.get_for_api_key("key") == snapshot


def test_invalid_fields_use_safe_defaults_and_guard_remains():
    store = ProjectConfigStore(
        lambda key, params: _row(
            "alpha", voice="not-a-voice", duplex=7,
            prompt="custom instructions",
        ),
        ttl_seconds=60,
    )
    snapshot = store.get_for_api_key("key")
    assert snapshot is not None
    assert snapshot.config.tts_voice == DEFAULT_CONFIG.tts_voice
    assert snapshot.config.duplex_enabled is True
    assert "custom instructions" in effective_instructions(snapshot.config)
    assert "真实可用工具只有" in effective_instructions(snapshot.config)
