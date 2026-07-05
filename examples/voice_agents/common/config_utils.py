"""统一的环境变量解析 helper。

收敛此前散落在 turn_config / listening_mode / kws_interrupt / online_interrupt /
live_transcript / 两个 agent 文件里的 5 套 `_env_*` / `_parse_*`。语义以原实现为准:

- 缺省(未设)→ default;空串对 bool 默认按"假"处理(与原多数实现一致),
  需要"空串=没设"的调用方传 `blank_is_default=True`(原 listening_mode 语义)。
- int/float 解析失败 → 记 warning 并回退 default(原 turn_config 语义;其余模块
  原先静默回退,现多一条日志,无功能差异)。
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("config-utils")

_TRUTHY = {"1", "true", "yes", "on"}


def env_bool(name: str, default: bool, *, blank_is_default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    if blank_is_default and not v.strip():
        return default
    return v.strip().lower() in _TRUTHY


def env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or not v.strip():
        return default
    try:
        return int(v)
    except ValueError:
        logger.warning("bad %s=%r, using default %s", name, v, default)
        return default


def env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or not v.strip():
        return default
    try:
        return float(v)
    except ValueError:
        logger.warning("bad %s=%r, using default %s", name, v, default)
        return default


def env_float_opt(name: str) -> float | None:
    """可选 float:未设/空 → None(调用方用其自身默认,如"用模型默认值")。"""
    v = os.getenv(name)
    if v is None or not v.strip():
        return None
    try:
        return float(v)
    except ValueError:
        logger.warning("bad %s=%r, ignoring", name, v)
        return None


def env_str(name: str, default: str) -> str:
    """字符串:未设 → default;**保留空串**(空串是合法值,如"提示语=不出声")。"""
    v = os.getenv(name)
    return default if v is None else v


def env_pair(name: str, default: tuple[float, float]) -> tuple[float, float]:
    """形如 "a,b" 的 float 二元组。"""
    v = os.getenv(name)
    if v is None or not v.strip():
        return default
    try:
        a, b = (x.strip() for x in v.split(","))
        return (float(a), float(b))
    except Exception:
        logger.warning("bad %s=%r (want 'a,b'), using default %s", name, v, default)
        return default
