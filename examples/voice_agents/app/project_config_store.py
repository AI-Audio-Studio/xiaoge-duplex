"""Read-only project configuration lookup for Xiaoge sessions.

The backend owns project settings. This module only reads a project config by an
already-authenticated API key, with a short-lived cache and safe fallback.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, replace
from typing import Any, Callable

from app.config_store import (
    AgentConfig,
    DEFAULT_CONFIG,
    DEFAULT_PERSONAS,
    DEFAULT_PERSONA_NAME,
    DEFAULT_PERSONA_TRAITS,
    DEFAULT_VOICE,
    INSTRUCTIONS_MAX_CHARS,
    PERSONA_KEYS,
    PERSONA_NAME_MAX_CHARS,
    PERSONA_TRAITS_MAX_CHARS,
    VOICE_KEYS,
    effective_instructions,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProjectConfigSnapshot:
    """Validated project settings plus a revision marker."""

    project_name: str
    config: AgentConfig
    revision: str = ""
    # Raw API-key selectors are kept separate from effective values.  The panel
    # needs these to re-select the persisted option after a page refresh.
    persona_key: str | None = None
    voice_key: str | None = None


class ProjectConfigStore:
    """Small synchronous DB adapter; callers never receive API-key contents in logs."""

    def __init__(
        self,
        query: Callable[[str, tuple[Any, ...]], dict[str, Any] | None] | None = None,
        *,
        ttl_seconds: float | None = None,
    ) -> None:
        self._query = query or self._query_mysql
        self._ttl = max(0.1, ttl_seconds if ttl_seconds is not None else float(os.getenv("XIAOGE_PROJECT_CONFIG_TTL", "2")))
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, ProjectConfigSnapshot]] = {}
        self._max_cache_entries = max(16, int(os.getenv("XIAOGE_PROJECT_CONFIG_CACHE_SIZE", "256")))

    @staticmethod
    def _query_mysql(api_key: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
        try:
            import pymysql
        except ImportError:
            logger.warning("project config DB disabled: pymysql is unavailable")
            return None
        host = os.getenv("XIAOGE_DB_HOST") or os.getenv("XG_API_KEY_DB_HOST", "")
        user = os.getenv("XIAOGE_DB_USER") or os.getenv("XG_API_KEY_DB_USER", "")
        password = os.getenv("XIAOGE_DB_PASSWORD")
        if password is None:
            password = os.getenv("XG_API_KEY_DB_PASSWORD", "")
        database = (
            os.getenv("XIAOGE_DB_NAME")
            or os.getenv("XG_API_KEY_DB_NAME")
            or "ry-cloud"
        )
        port = os.getenv("XIAOGE_DB_PORT") or os.getenv("XG_API_KEY_DB_PORT", "3306")
        if not host or not user:
            return None
        conn = None
        try:
            conn = pymysql.connect(
                host=host,
                port=int(port),
                user=user,
                password=password,
                database=database,
                connect_timeout=float(os.getenv("XIAOGE_DB_CONNECT_TIMEOUT", "2")),
                read_timeout=float(os.getenv("XIAOGE_DB_READ_TIMEOUT", "2")),
                write_timeout=float(os.getenv("XIAOGE_DB_READ_TIMEOUT", "2")),
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=True,
            )
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT k.project_name, c.persona_name, c.persona_traits,
                              c.tts_voice, k.voice_key, k.persona_key,
                              c.duplex_enabled, c.instructions_override,
                              c.update_time
                         FROM sys_api_key k
                         LEFT JOIN sys_xiaoge_project_config c
                           ON c.project_name = k.project_name
                        WHERE k.api_key = %s AND k.status = '0'
                        LIMIT 1""",
                    params,
                )
                return cur.fetchone()
        except Exception:
            logger.warning("project config DB lookup failed", exc_info=True)
            return None
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _text(value: Any, default: str, limit: int) -> str:
        return value.strip() if isinstance(value, str) and value.strip() and len(value.strip()) <= limit else default

    @classmethod
    def _snapshot(cls, row: dict[str, Any]) -> ProjectConfigSnapshot | None:
        project = cls._text(row.get("project_name"), "default", 100)

        # Start with the project's editable scalar defaults.  A valid preset key
        # is a selector layered on top of those defaults (not a mutation of the
        # global/persona table); an explicit voice_key is the final voice layer.
        persona_name = cls._text(
            row.get("persona_name"), DEFAULT_PERSONA_NAME, PERSONA_NAME_MAX_CHARS
        )
        persona_traits = cls._text(
            row.get("persona_traits"), DEFAULT_PERSONA_TRAITS, PERSONA_TRAITS_MAX_CHARS
        )
        voice = row.get("tts_voice")
        if not isinstance(voice, str) or voice not in VOICE_KEYS:
            voice = DEFAULT_VOICE

        persona_key = row.get("persona_key")
        persona = DEFAULT_PERSONAS.get(persona_key) if isinstance(persona_key, str) else None
        if persona is not None:
            # A persona selector changes prompt traits only.  Voice selection is
            # independent and comes only from the project default or voice_key.
            persona_traits = persona["traits"]

        voice_key = row.get("voice_key")
        if isinstance(voice_key, str) and voice_key in VOICE_KEYS:
            voice = voice_key

        override = row.get("instructions_override")
        if not isinstance(override, str) or not override.strip() or len(override.strip()) > INSTRUCTIONS_MAX_CHARS:
            override = None
        duplex = row.get("duplex_enabled")
        if isinstance(duplex, bool):
            duplex_enabled = duplex
        elif isinstance(duplex, (int, float)) and duplex in (0, 1):
            duplex_enabled = bool(duplex)
        else:
            duplex_enabled = True
        cfg = AgentConfig(
            persona_name=persona_name,
            persona_traits=persona_traits,
            tts_voice=voice,
            duplex_enabled=duplex_enabled,
            instructions_override=override.strip() if override else None,
        )
        revision = str(row.get("update_time") or "")
        persisted_persona = persona_key if persona_key in PERSONA_KEYS else None
        persisted_voice = voice_key if voice_key in VOICE_KEYS else None
        return ProjectConfigSnapshot(
            project, cfg, revision, persisted_persona, persisted_voice
        )

    def get_for_api_key(
        self, api_key: str | None, *, force_refresh: bool = False
    ) -> ProjectConfigSnapshot | None:
        if not isinstance(api_key, str) or not api_key.strip():
            return None
        key = api_key.strip()
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(key)
            if not force_refresh and cached and now - cached[0] < self._ttl:
                return cached[1]
        try:
            row = self._query(key, (key,))
            snapshot = self._snapshot(row) if isinstance(row, dict) else None
        except Exception:
            logger.warning("project config validation failed", exc_info=True)
            snapshot = None
        if snapshot is None:
            with self._lock:
                if cached:
                    return cached[1]
            return None
        with self._lock:
            self._cache[key] = (now, snapshot)
            if len(self._cache) > self._max_cache_entries:
                oldest = min(self._cache, key=lambda item: self._cache[item][0])
                self._cache.pop(oldest, None)
        return snapshot


_store = ProjectConfigStore()


def get_project_snapshot(
    api_key: str | None, *, force_refresh: bool = False
) -> ProjectConfigSnapshot | None:
    """Return the exact-key snapshot for authenticated control-plane reads."""
    return _store.get_for_api_key(api_key, force_refresh=force_refresh)


def load_project_config(api_key: str | None) -> AgentConfig:
    """Return the config for *api_key* without crossing project boundaries.

    A supplied key is an authenticated project selector.  Never fall back to the
    process-wide JSON config for that request: doing so can expose one project's
    persona/instructions to another project when the DB row is missing or
    temporarily unavailable.  The local JSON fallback remains available only for
    keyless offline/development workers.
    """
    snapshot = _store.get_for_api_key(api_key)
    if snapshot:
        return snapshot.config
    if isinstance(api_key, str) and api_key.strip():
        return DEFAULT_CONFIG
    # Offline/dev deployments retain the pre-existing JSON configuration. A DB
    # outage never prevents a keyless worker from starting.
    try:
        from app.config_store import load_agent_config
        return load_agent_config()
    except Exception:
        return DEFAULT_CONFIG


def project_instructions(api_key: str | None) -> str:
    return effective_instructions(load_project_config(api_key))
