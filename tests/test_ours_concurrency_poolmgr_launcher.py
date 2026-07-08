"""行为锁定测试:并发改造 池管理器生产启动器(poolmgr/launcher.py 部署入口)。

锁 `XG_POOL_*` env 解析与默认(§7.2/D-14 部署口径)+ `build_manager` 装配(未 start 即可验
size/status,不 spawn 真 agent、不依赖云/模型)。真起停(spawn N 个 console agent + serve)属
部署期,不在单测。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_AGENT_DIR = Path(__file__).resolve().parents[1] / "examples" / "voice_agents"
sys.path.insert(0, str(_AGENT_DIR))

from poolmgr.launcher import PoolLaunchConfig, build_manager  # noqa: E402

_ENV = (
    "XG_POOL_SIZE",
    "XG_POOL_BASE_PORT",
    "XG_POOL_CONTROL_HOST",
    "XG_POOL_CONTROL_PORT",
    "XG_POOL_RECORDINGS_ROOT",
    "XG_POOL_TRANSCODE_CODEC",
    "XG_POOL_TRANSCODE_WORKERS",
    "XG_POOL_POLL_INTERVAL_S",
    "XG_POOL_SPAWN_TIMEOUT_S",
    "XG_POOL_HEALTHZ_FAIL_LIMIT",
)


def _clear(mp: pytest.MonkeyPatch) -> None:
    for k in _ENV:
        mp.delenv(k, raising=False)


def test_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    c = PoolLaunchConfig.from_env()
    assert c.size == 4 and c.base_port == 19100
    assert c.control_host == "127.0.0.1" and c.control_port == 19000  # M3 loopback
    assert c.transcode_codec == "opus" and c.transcode_workers == 1  # D-14/D-22
    assert c.recordings_root == "recordings"


def test_config_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("XG_POOL_SIZE", "10")
    monkeypatch.setenv("XG_POOL_BASE_PORT", "19200")
    monkeypatch.setenv("XG_POOL_CONTROL_PORT", "19001")
    monkeypatch.setenv("XG_POOL_TRANSCODE_CODEC", "flac")
    monkeypatch.setenv("XG_POOL_TRANSCODE_WORKERS", "2")
    monkeypatch.setenv("XG_POOL_RECORDINGS_ROOT", "/data/rec")
    c = PoolLaunchConfig.from_env()
    assert c.size == 10 and c.base_port == 19200 and c.control_port == 19001
    assert c.transcode_codec == "flac" and c.transcode_workers == 2
    assert c.recordings_root == "/data/rec"


def test_build_manager_reflects_size_without_spawn() -> None:
    """build_manager 装配(未 start)→ status 反映 N、无进程 spawn(ready/assigned 全 0)。"""
    c = PoolLaunchConfig(size=6, base_port=19300, recordings_root="recordings")
    mgr = build_manager(c)
    st = mgr.status()
    assert st["size"] == 6  # N 生效
    assert st["ready"] == 0 and st["assigned"] == 0 and st["spawning"] == 0  # 未 start,无进程


def test_build_manager_codec_off_builds() -> None:
    """codec=off(保持 WAV)也能装配(转码器内部停用,不报错)。"""
    mgr = build_manager(PoolLaunchConfig(size=2, transcode_codec="off"))
    assert mgr.status()["size"] == 2
