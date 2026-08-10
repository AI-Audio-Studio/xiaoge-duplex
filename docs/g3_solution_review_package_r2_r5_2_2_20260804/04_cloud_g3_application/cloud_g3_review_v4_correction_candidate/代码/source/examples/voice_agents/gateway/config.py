"""网关配置(PR-C,§3.5)。全部 env 私有,前缀 `XG_` 与 agent 的 `XIAOGE_` 区分。

内部端口仅绑 127.0.0.1(M3);对外单端口 TLS 终结。HMAC secret 空=进程内随机(重启失效=R4
既定语义,全员回页);准入口令 = Q6 最低准入。
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field


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


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class GatewayConfig:
    listen_host: str = "0.0.0.0"  # 对外(公网);内部进程仍绑 127.0.0.1(池管理器注入)
    listen_port: int = 10099
    ssl_cert: str = ""
    ssl_key: str = ""
    pool_api: str = "http://127.0.0.1:19000"  # 池管理器控制 API(P-2)
    grace_seconds: float = 12.0  # 宽限窗 T(D-16:浏览器刷新 10~15s)
    access_code: str = ""  # Q6 准入口令(空=不启用准入)
    hmac_secret: str = field(default="")  # 空 → 进程内随机(重启失效,R4)
    msg_rate_per_s: float = 200.0  # 每连接消息速率上限(R6)
    max_frame_bytes: int = 32_768  # 单帧大小上限(R6)
    # apikey 准入(协议客户端/模式A):有效集合 = DB(sys_api_key,status='0') ∪ 静态列表。
    # required=1 强制(缺/错拒);=0 兼容/观察(恒放行,仅日志)。DB 空各字段=不查库。
    api_key_required: bool = False
    api_keys_static: str = ""  # 逗号分隔的静态补充/兜底 key(可空)
    api_key_db_host: str = ""
    api_key_db_port: int = 3306
    api_key_db_user: str = ""
    api_key_db_password: str = ""
    api_key_db_name: str = ""
    api_key_refresh_sec: float = 60.0  # 后台刷新有效集合的周期
    # 浏览器原生 WebSocket 不能设置 Authorization。仅测试环境可显式打开隔离 debug 端点；
    # 正式 /ws/session 始终 Bearer-only。
    webpanel_debug_query_token: bool = False

    @classmethod
    def from_env(cls) -> GatewayConfig:
        return cls(
            listen_host=os.getenv("XG_LISTEN_HOST", "0.0.0.0").strip(),
            listen_port=_env_int("XG_LISTEN_PORT", 10099),
            ssl_cert=os.getenv("XG_SSL_CERT", "").strip(),
            ssl_key=os.getenv("XG_SSL_KEY", "").strip(),
            pool_api=os.getenv("XG_POOL_API", "http://127.0.0.1:19000").strip().rstrip("/"),
            grace_seconds=_env_float("XG_GRACE_SECONDS", 12.0),
            access_code=os.getenv("XG_ACCESS_CODE", "").strip(),
            hmac_secret=os.getenv("XG_HMAC_SECRET", "").strip() or secrets.token_hex(16),
            msg_rate_per_s=_env_float("XG_MSG_RATE", 200.0),
            max_frame_bytes=_env_int("XG_MAX_FRAME_BYTES", 32_768),
            api_key_required=_env_bool("XG_API_KEY_REQUIRED", False),
            api_keys_static=os.getenv("XG_API_KEYS", "").strip(),
            api_key_db_host=os.getenv("XG_API_KEY_DB_HOST", "").strip(),
            api_key_db_port=_env_int("XG_API_KEY_DB_PORT", 3306),
            api_key_db_user=os.getenv("XG_API_KEY_DB_USER", "").strip(),
            api_key_db_password=os.getenv("XG_API_KEY_DB_PASSWORD", ""),
            api_key_db_name=os.getenv("XG_API_KEY_DB_NAME", "").strip(),
            api_key_refresh_sec=_env_float("XG_API_KEY_REFRESH_SEC", 60.0),
            webpanel_debug_query_token=_env_bool("XG_WEBPANEL_DEBUG_QUERY_TOKEN", False),
        )

    @property
    def tls_enabled(self) -> bool:
        return bool(self.ssl_cert and self.ssl_key)

    @property
    def access_required(self) -> bool:
        return bool(self.access_code)

    @property
    def api_key_db_enabled(self) -> bool:
        return bool(self.api_key_db_host and self.api_key_db_name and self.api_key_db_user)

    @property
    def api_keys_static_set(self) -> frozenset[str]:
        return frozenset(k.strip() for k in self.api_keys_static.split(",") if k.strip())
