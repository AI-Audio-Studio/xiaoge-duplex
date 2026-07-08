"""池管理器生产启动器(部署入口)。

从 `XG_POOL_*` env 构建 `PoolManager`(默认真 I/O:起 `web_ui_agent.py console`)+ `Transcoder`
(录音旁路转码,D-13)+ 起控制 API(**仅 127.0.0.1**,M3);`start()` 预热 N 个 agent 进程 + 轮询
healthz,`serve()` 阻塞至 SIGINT/SIGTERM → 优雅 `stop()`(kill 所有 agent + 停转码器)。

用法:`cd examples/voice_agents && python -m poolmgr`(网关另起:`python -m gateway.main`,**池先起**)。
env 见 `PoolLaunchConfig.from_env`;缺省即 §7.2/D-14 部署口径。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from poolmgr import control_api
from poolmgr.manager import PoolIO, PoolManager, PoolTuning
from poolmgr.transcoder import Transcoder

logger = logging.getLogger("poolmgr-launcher")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except (ValueError, AttributeError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).strip())
    except (ValueError, AttributeError):
        return default


@dataclass
class PoolLaunchConfig:
    size: int = 4  # 池大小 N(=目标机摸底定;先给保守初值)
    base_port: int = 19100  # agent 端口起点(191xx,内网)
    control_host: str = "127.0.0.1"  # 控制 API 绑定(M3:仅 loopback)
    control_port: int = 19000
    recordings_root: str = "recordings"
    transcode_codec: str = "opus"  # D-14;"off"/"wav" → 转码器停用、保持 WAV
    transcode_workers: int = 1  # D-22:并发 ≤2
    poll_interval_s: float = 2.0
    spawn_timeout_s: float = 30.0  # 冷启动超时(≠连败判亡)
    healthz_fail_limit: int = 3

    @classmethod
    def from_env(cls) -> PoolLaunchConfig:
        return cls(
            size=_env_int("XG_POOL_SIZE", 4),
            base_port=_env_int("XG_POOL_BASE_PORT", 19100),
            control_host=os.getenv("XG_POOL_CONTROL_HOST", "127.0.0.1").strip(),
            control_port=_env_int("XG_POOL_CONTROL_PORT", 19000),
            recordings_root=os.getenv("XG_POOL_RECORDINGS_ROOT", "recordings").strip(),
            transcode_codec=os.getenv("XG_POOL_TRANSCODE_CODEC", "opus").strip(),
            transcode_workers=_env_int("XG_POOL_TRANSCODE_WORKERS", 1),
            poll_interval_s=_env_float("XG_POOL_POLL_INTERVAL_S", 2.0),
            spawn_timeout_s=_env_float("XG_POOL_SPAWN_TIMEOUT_S", 30.0),
            healthz_fail_limit=_env_int("XG_POOL_HEALTHZ_FAIL_LIMIT", 3),
        )


def build_manager(config: PoolLaunchConfig) -> PoolManager:
    """按 config 装配(未 start)。默认 `PoolIO()` = 真 spawn/healthz/kill(见 manager 默认实现)。"""
    transcoder = Transcoder(
        config.recordings_root, codec=config.transcode_codec, workers=config.transcode_workers
    )
    tuning = PoolTuning(
        base_port=config.base_port,
        recordings_root=config.recordings_root,
        poll_interval_s=config.poll_interval_s,
        spawn_timeout_s=config.spawn_timeout_s,
        healthz_fail_limit=config.healthz_fail_limit,
    )
    return PoolManager(size=config.size, io=PoolIO(), tuning=tuning, transcoder=transcoder)


def run(config: PoolLaunchConfig) -> None:
    manager = build_manager(config)
    logger.info(
        "pool manager starting: N=%d base_port=%d control=%s:%d codec=%s recordings=%s",
        config.size,
        config.base_port,
        config.control_host,
        config.control_port,
        config.transcode_codec,
        config.recordings_root,
    )
    manager.start()  # 预热 N 个 agent + 起转码器 + 扫描遗留 + 轮询 healthz
    try:
        # 阻塞至 SIGINT/SIGTERM;serve 强制 loopback(M3),非本地地址将拒绝。
        control_api.serve(manager, host=config.control_host, port=config.control_port)
    finally:
        logger.info("pool manager stopping")
        manager.stop()  # kill 所有 agent + 停转码器


def main(argv: Any = None) -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    run(PoolLaunchConfig.from_env())


if __name__ == "__main__":
    main()
