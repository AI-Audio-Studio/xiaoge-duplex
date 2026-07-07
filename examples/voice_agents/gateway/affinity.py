"""会话亲和:HMAC cookie(P-4)+ 会话状态机 + 宽限窗(P-3 / T2 / D-16)。

**cookie(P-4)**:`<proc_id>.<session_id>.<hmac(secret, "proc_id|session_id")>`——HMAC 防伪造
跳进程,常数时间比对。

**会话状态机(P-3)**:`IDLE →(音频连)→ ACTIVE →(音频断)→ PENDING_DISCONNECT(宽限窗 T)
→(同 cookie 重连)→ ACTIVE / (超时)→ CLOSED`。
**上游连接由网关持有**(ACTIVE + PENDING 期间不断),重连接回既有上游、帧续接(T2:agent 无感)。
双标签页(同 cookie 已有活跃音频连接)拒绝(R3)。
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
from dataclasses import dataclass
from typing import Any

IDLE, ACTIVE, PENDING_DISCONNECT, CLOSED = "idle", "active", "pending_disconnect", "closed"

# on_audio_connect 结果
CONNECT_FRESH = "fresh"  # IDLE → ACTIVE:新建上游
CONNECT_REATTACH = "reattach"  # PENDING → ACTIVE:接回既有上游(T2 帧续接)
CONNECT_REJECT_BUSY = "reject_busy"  # 同 cookie 已有活跃音频 = 双标签页(R3)
CONNECT_REJECT_GONE = "reject_gone"  # 会话已亡/CLOSED


def sign_affinity(secret: str, proc_id: str, session_id: str) -> str:
    """签发亲和 cookie 值。"""
    mac = hmac.new(secret.encode(), f"{proc_id}|{session_id}".encode(), hashlib.sha256).hexdigest()
    return f"{proc_id}.{session_id}.{mac[:32]}"


def verify_affinity(secret: str, cookie_value: str) -> tuple[str, str] | None:
    """校验并解析 cookie。返回 (proc_id, session_id);格式错/HMAC 不符 → None。"""
    if not cookie_value:
        return None
    parts = cookie_value.split(".")
    if len(parts) != 3:
        return None
    proc_id, session_id, mac = parts
    expect = hmac.new(
        secret.encode(), f"{proc_id}|{session_id}".encode(), hashlib.sha256
    ).hexdigest()[:32]
    if not hmac.compare_digest(mac, expect):  # 常数时间比对,防时序旁路
        return None
    return proc_id, session_id


@dataclass
class Session:
    session_id: str
    proc_id: str
    port: int
    state: str = IDLE
    upstream: Any = None  # 网关持有的内部上游连接(T2:ACTIVE+PENDING 期不断)
    grace_deadline: float = 0.0
    audio_conns: int = 0  # 当前活跃 /ws/audio 连接数(双标签页检测,R3)


class AffinityTable:
    """会话表 + 宽限窗。I/O(实际关上游/调 /release)由网关做;本表只管状态机与 cookie。"""

    def __init__(self, *, grace_seconds: float, secret: str, clock: Any = time.monotonic) -> None:
        self._grace = float(grace_seconds)
        self._secret = secret
        self._clock = clock
        self._sessions: dict[str, Session] = {}
        self._lock = threading.RLock()

    def register(self, session_id: str, proc_id: str, port: int) -> Session:
        """池 alloc 后登记新会话(IDLE)。"""
        s = Session(session_id=session_id, proc_id=proc_id, port=port, state=IDLE)
        with self._lock:
            self._sessions[session_id] = s
        return s

    def cookie_for(self, session_id: str) -> str | None:
        with self._lock:
            s = self._sessions.get(session_id)
        return sign_affinity(self._secret, s.proc_id, session_id) if s else None

    def resolve(self, cookie_value: str) -> Session | None:
        """校验 cookie → 活跃 session。cookie 无效/篡改/会话已亡/proc 不符 → None(规则 2 拒绝回页)。"""
        parsed = verify_affinity(self._secret, cookie_value)
        if parsed is None:
            return None
        proc_id, session_id = parsed
        with self._lock:
            s = self._sessions.get(session_id)
        if s is None or s.state == CLOSED or s.proc_id != proc_id:
            return None
        return s

    def on_audio_connect(self, session_id: str) -> tuple[str, Session | None]:
        """/ws/audio 连接。返回 (结果, session):
        FRESH(IDLE→ACTIVE,新建上游)/ REATTACH(PENDING→ACTIVE,接回既有上游 T2)/
        REJECT_BUSY(已有活跃音频=双标签页 R3)/ REJECT_GONE(会话亡)。"""
        with self._lock:
            s = self._sessions.get(session_id)
            if s is None or s.state == CLOSED:
                return CONNECT_REJECT_GONE, None
            if s.audio_conns > 0:
                return CONNECT_REJECT_BUSY, s
            reattach = s.state == PENDING_DISCONNECT
            s.state = ACTIVE
            s.audio_conns += 1
            s.grace_deadline = 0.0
            return (CONNECT_REATTACH if reattach else CONNECT_FRESH), s

    def on_audio_disconnect(self, session_id: str) -> None:
        """/ws/audio 断开:最后一条音频连接断 → ACTIVE 转 PENDING_DISCONNECT(宽限窗计时)。"""
        with self._lock:
            s = self._sessions.get(session_id)
            if s is None:
                return
            s.audio_conns = max(0, s.audio_conns - 1)
            if s.audio_conns == 0 and s.state == ACTIVE:
                s.state = PENDING_DISCONNECT
                s.grace_deadline = self._clock() + self._grace

    def sweep_expired(self) -> list[Session]:
        """宽限窗超时的 PENDING → CLOSED 并移出表,返回需网关 release+关上游的会话。"""
        now = self._clock()
        with self._lock:
            expired = [
                s
                for s in self._sessions.values()
                if s.state == PENDING_DISCONNECT and now >= s.grace_deadline
            ]
            for s in expired:
                s.state = CLOSED
                self._sessions.pop(s.session_id, None)
        return expired

    def close(self, session_id: str) -> Session | None:
        with self._lock:
            s = self._sessions.pop(session_id, None)
        if s is not None:
            s.state = CLOSED
        return s

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            return self._sessions.get(session_id)

    def counts(self) -> dict[str, int]:
        with self._lock:
            out: dict[str, int] = {}
            for s in self._sessions.values():
                out[s.state] = out.get(s.state, 0) + 1
        return out
