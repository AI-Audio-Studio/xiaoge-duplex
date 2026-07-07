"""行为锁定测试:并发改造 PR-C 网关之 config + affinity(P-3/P-4/T2/D-16/R3)。

覆盖:env 解析与默认、HMAC cookie 签发/校验/防篡改、会话状态机(IDLE→ACTIVE→
PENDING_DISCONNECT→CLOSED)、宽限窗内重连接回(REATTACH)、超时清除、双标签页拒绝、
resolve 校验。纯逻辑,无云依赖、无真进程。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_AGENT_DIR = Path(__file__).resolve().parents[1] / "examples" / "voice_agents"
sys.path.insert(0, str(_AGENT_DIR))

from gateway import affinity as af  # noqa: E402
from gateway.config import GatewayConfig  # noqa: E402


# ── config ───────────────────────────────────────────────────────────────────
def _clear(mp: pytest.MonkeyPatch) -> None:
    for k in (
        "XG_LISTEN_HOST",
        "XG_LISTEN_PORT",
        "XG_SSL_CERT",
        "XG_SSL_KEY",
        "XG_POOL_API",
        "XG_GRACE_SECONDS",
        "XG_ACCESS_CODE",
        "XG_HMAC_SECRET",
        "XG_MSG_RATE",
        "XG_MAX_FRAME_BYTES",
    ):
        mp.delenv(k, raising=False)


def test_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    c = GatewayConfig.from_env()
    assert c.listen_port == 10099 and c.grace_seconds == 12.0
    assert c.pool_api == "http://127.0.0.1:19000"
    assert c.tls_enabled is False and c.access_required is False
    assert len(c.hmac_secret) == 32  # 未设 → 随机 32 hex


def test_config_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("XG_LISTEN_PORT", "8443")
    monkeypatch.setenv("XG_GRACE_SECONDS", "15")
    monkeypatch.setenv("XG_ACCESS_CODE", "s3cret")
    monkeypatch.setenv("XG_SSL_CERT", "/c.pem")
    monkeypatch.setenv("XG_SSL_KEY", "/k.pem")
    monkeypatch.setenv("XG_POOL_API", "http://127.0.0.1:19999/")
    c = GatewayConfig.from_env()
    assert c.listen_port == 8443 and c.grace_seconds == 15.0
    assert c.access_required is True and c.tls_enabled is True
    assert c.pool_api == "http://127.0.0.1:19999"  # 尾 / 去掉


# ── cookie(P-4)───────────────────────────────────────────────────────────────
def test_cookie_roundtrip() -> None:
    v = af.sign_affinity("secret", "p1", "sess9")
    assert af.verify_affinity("secret", v) == ("p1", "sess9")


def test_cookie_rejects_tamper_and_wrong_secret() -> None:
    v = af.sign_affinity("secret", "p1", "sess9")
    assert af.verify_affinity("secret", v[:-1] + ("0" if v[-1] != "0" else "1")) is None  # 改 HMAC
    assert af.verify_affinity("other", v) is None  # 错 secret
    assert af.verify_affinity("secret", "p1.sess9") is None  # 缺段
    assert af.verify_affinity("secret", "") is None
    # 改 proc_id 但保留旧 HMAC → 不符
    proc, sess, mac = v.split(".")
    assert af.verify_affinity("secret", f"evil.{sess}.{mac}") is None


# ── 会话状态机(P-3 / T2 / 宽限窗)────────────────────────────────────────────
class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _table(grace: float = 10.0):
    clk = _Clock()
    return af.AffinityTable(grace_seconds=grace, secret="secret", clock=clk), clk


def test_connect_fresh_then_disconnect_pending() -> None:
    t, clk = _table()
    t.register("s1", "p1", 19100)
    assert t.get("s1").state == af.IDLE
    res, s = t.on_audio_connect("s1")
    assert res == af.CONNECT_FRESH and s.state == af.ACTIVE and s.audio_conns == 1
    t.on_audio_disconnect("s1")
    assert t.get("s1").state == af.PENDING_DISCONNECT
    assert t.get("s1").grace_deadline == 10.0  # now(0)+grace(10)


def test_reconnect_within_grace_reattaches() -> None:
    t, clk = _table()
    t.register("s1", "p1", 19100)
    t.on_audio_connect("s1")
    t.on_audio_disconnect("s1")  # → PENDING, deadline=10
    clk.t = 5.0  # 窗内
    res, s = t.on_audio_connect("s1")
    assert res == af.CONNECT_REATTACH and s.state == af.ACTIVE  # 接回既有上游(T2)
    assert t.sweep_expired() == []  # 已回 ACTIVE,不再过期


def test_grace_timeout_closes() -> None:
    t, clk = _table()
    t.register("s1", "p1", 19100)
    t.on_audio_connect("s1")
    t.on_audio_disconnect("s1")  # deadline=10
    clk.t = 9.9
    assert t.sweep_expired() == []  # 未到点
    clk.t = 10.0
    expired = t.sweep_expired()
    assert len(expired) == 1 and expired[0].session_id == "s1"
    assert t.get("s1") is None  # 移出表
    # 超时后再连 → 会话已亡
    res, _ = t.on_audio_connect("s1")
    assert res == af.CONNECT_REJECT_GONE


def test_double_tab_rejected() -> None:
    t, _ = _table()
    t.register("s1", "p1", 19100)
    t.on_audio_connect("s1")  # 第一条音频连接
    res, s = t.on_audio_connect("s1")  # 同 cookie 再连 = 双标签页
    assert res == af.CONNECT_REJECT_BUSY and s.state == af.ACTIVE  # 不透传、不改状态


def test_resolve_and_close() -> None:
    t, _ = _table()
    t.register("s1", "p1", 19100)
    cookie = t.cookie_for("s1")
    assert t.resolve(cookie).session_id == "s1"
    # 篡改 → None
    assert t.resolve(cookie[:-1] + "z") is None
    # 关闭后 resolve → None
    t.close("s1")
    assert t.resolve(cookie) is None and t.get("s1") is None
