"""apikey 准入(协议客户端/模式A)。

有效集合 = DB `sys_api_key`(status='0' 的 api_key)∪ 配置里的静态列表。启动载入一次、
后台按周期刷新;WS 建连只做内存集合判定(O(1)),不在热路径查库、不阻塞 asyncio。DB 查询
用线程池执行(pymysql 为同步驱动),失败保留上一份快照(容忍 DB 抖动,不误杀已有 key)。

授权语义(与 config 对齐):
- required=1:presented 命中有效集合才放行,否则拒(缺/错都拒)。
- required=0:恒放行,仅记录命中与否(灰度/观察态,不打断现网客户端)。
"""

from __future__ import annotations

import asyncio
import logging

from gateway.config import GatewayConfig

logger = logging.getLogger("gateway")

# RuoYi 约定:status '0'=正常/启用,'1'=停用。只取启用的 key。
_SQL = "SELECT api_key FROM sys_api_key WHERE status = '0'"


def _load_from_db(cfg: GatewayConfig) -> set[str]:
    """同步查库,返回启用中的 api_key 集合。异常上抛由调用方兜。"""
    import pymysql  # 惰性导入:未装驱动/未配 DB 时不影响网关启动  # type: ignore[import-untyped]

    conn = pymysql.connect(
        host=cfg.api_key_db_host,
        port=cfg.api_key_db_port,
        user=cfg.api_key_db_user,
        password=cfg.api_key_db_password,
        database=cfg.api_key_db_name,
        connect_timeout=8,
        read_timeout=8,
    )
    try:
        cur = conn.cursor()
        cur.execute(_SQL)
        return {row[0] for row in cur.fetchall() if row[0]}
    finally:
        conn.close()


class ApiKeyStore:
    """持有有效 apikey 集合,提供后台刷新与授权判定。"""

    def __init__(self, cfg: GatewayConfig) -> None:
        self._cfg = cfg
        self._static = cfg.api_keys_static_set
        self._db_keys: frozenset[str] = frozenset()

    @property
    def required(self) -> bool:
        return self._cfg.api_key_required

    def _effective(self) -> frozenset[str]:
        return self._static | self._db_keys

    def authorize(self, presented: str | None) -> bool:
        """True=放行。required=0 恒放行(仅日志);required=1 须命中有效集合。"""
        hit = bool(presented) and presented in self._effective()
        if not self._cfg.api_key_required:
            if not hit:
                logger.info(
                    "apikey compat-mode pass (key %s)", "present" if presented else "absent"
                )
            return True
        return hit

    async def refresh(self) -> None:
        """从 DB 刷新一次;未配 DB 则跳过,失败保留旧快照。"""
        if not self._cfg.api_key_db_enabled:
            return
        try:
            keys = await asyncio.get_running_loop().run_in_executor(None, _load_from_db, self._cfg)
        except Exception:
            logger.exception("apikey db refresh failed; keeping %d cached", len(self._db_keys))
            return
        self._db_keys = frozenset(keys)
        logger.info("apikey db refreshed: %d active keys", len(self._db_keys))

    async def run_refresh_loop(self) -> None:
        """常驻:按 refresh_sec 周期刷新;单次异常不杀循环。"""
        interval = max(5.0, self._cfg.api_key_refresh_sec)
        while True:
            await asyncio.sleep(interval)
            await self.refresh()
