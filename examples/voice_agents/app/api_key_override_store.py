"""Direct, per-API-key persistence for persona and voice overrides.

This writer intentionally owns only the two nullable override columns.  It does
not read or write the process-wide agent_config.json file.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class ApiKeyOverrideUnavailable(RuntimeError):
    """The database writer is not configured or cannot complete the operation."""


class ApiKeyOverrideStore:
    """Small synchronous MySQL adapter; call it off the asyncio event loop."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str,
        password: str,
        database: str,
        connect: Callable[..., Any] | None = None,
        connect_timeout: float = 2.0,
        read_timeout: float = 2.0,
        write_timeout: float = 2.0,
    ) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._database = database
        self._connect = connect
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._write_timeout = write_timeout

    @classmethod
    def from_config(cls, config: Any) -> "ApiKeyOverrideStore":
        return cls(
            host=config.api_key_write_host,
            port=config.api_key_write_port,
            user=config.api_key_write_user,
            password=config.api_key_write_password,
            database=config.api_key_write_name,
        )

    def _open(self) -> Any:
        if not self._host or not self._user or not self._database:
            raise ApiKeyOverrideUnavailable("API-key persistence is not configured")
        connect = self._connect
        if connect is None:
            try:
                import pymysql  # type: ignore[import-untyped]
            except ImportError as exc:
                raise ApiKeyOverrideUnavailable("database driver unavailable") from exc
            connect = pymysql.connect
        try:
            return connect(
                host=self._host,
                port=self._port,
                user=self._user,
                password=self._password,
                database=self._database,
                connect_timeout=self._connect_timeout,
                read_timeout=self._read_timeout,
                write_timeout=self._write_timeout,
                autocommit=False,
            )
        except Exception as exc:
            raise ApiKeyOverrideUnavailable("database connection failed") from exc

    def update(
        self,
        api_key: str,
        *,
        persona_key: str | None,
        voice_key: str | None,
    ) -> bool:
        """Update one enabled key, returning False when no enabled key matches."""
        conn = self._open()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """SELECT 1 FROM sys_api_key
                       WHERE api_key = %s AND status = '0'
                       LIMIT 1 FOR UPDATE""",
                    (api_key,),
                )
                if cursor.fetchone() is None:
                    conn.rollback()
                    return False
                cursor.execute(
                    """UPDATE sys_api_key
                          SET persona_key = %s,
                              voice_key = %s,
                              update_time = NOW()
                        WHERE api_key = %s AND status = '0'""",
                    (persona_key, voice_key, api_key),
                )
            conn.commit()
            return True
        except ApiKeyOverrideUnavailable:
            raise
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                logger.debug("API-key override rollback failed", exc_info=True)
            raise ApiKeyOverrideUnavailable("database update failed") from exc
        finally:
            try:
                conn.close()
            except Exception:
                logger.debug("API-key override connection close failed", exc_info=True)
