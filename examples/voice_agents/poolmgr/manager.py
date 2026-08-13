"""进程池管理器(PR-B,v4 §7 / §4.2)。

维护 N 个预热的 agent 进程,按状态机流转:
  `SPAWNING →(healthz ready)→ READY →(alloc)→ ASSIGNED →(release/超时/崩溃)→ RECYCLING(kill+重启)→ SPAWNING`
alloc 取一个 READY 进程给网关;release 把该会话录音入队转码 + 回收进程(kill+重启,比状态复位可靠);
healthz 轮询(默认 2s、连败 `fail_limit` 判亡)。就绪数 < 阈值告警(M4)。

I/O(spawn/healthz/kill)经构造注入 → 状态机可用假 agent 独立单测(P-8);默认实现见
`default_spawn`/`default_healthz`/`default_kill`(真起 `web_ui_agent.py console` + 探 /healthz)。
每进程 env 注入见 `default_agent_env`(v4 §7.2 表)。**内部端口仅绑 127.0.0.1(M3)。**
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("poolmgr-manager")

SPAWNING, READY, ASSIGNED, RECYCLING = "spawning", "ready", "assigned", "recycling"


def default_agent_env(
    proc_id: str, port: int, *, metrics_dir: Path | None = None
) -> dict[str, str]:
    """每进程 env 注入表(v4 §7.2)。部署环境可覆盖录音模式和审计级别。"""
    env = {
        "WEB_AUDIO": "1",
        "WEB_UI_HOST": "127.0.0.1",  # M3:显式内网绑定,不依赖代码默认
        "WEB_UI_PORT": str(port),
        "XIAOGE_KWS_ENABLE_NATIVE": "0",  # D-06:服务器形态默认关 KWS
        "XIAOGE_SESSION_ID": proc_id,  # #1/#4:目录/日志会话短 id(进程实例=会话)
        "XIAOGE_RECORD_MODE": os.getenv("XIAOGE_RECORD_MODE", "full"),
        "XIAOGE_RECORD_CODEC": "opus",
        "XIAOGE_TIMELINE_LEVEL": os.getenv("XIAOGE_TIMELINE_LEVEL", "audit"),
        "XIAOGE_ADMIN_ROUTES": "0",  # M5/D-19:asr/tts 显式隐藏(不依赖代码默认,防 shell 环境泄漏)
        # console 默认 DEBUG,会把 livekit 的 tts/llm 帧级 DEBUG 全刷进合流日志;我们自己的
        # 轮次日志都是 INFO,故拉到 INFO 即可保留诊断、砍掉帧噪声。要排障时临时设 DEBUG。
        "LIVEKIT_LOG_LEVEL": os.getenv("LIVEKIT_LOG_LEVEL", "INFO"),
    }
    if metrics_dir is not None:
        env["TURN_METRICS_LOG"] = str(metrics_dir / f"turn_metrics_{proc_id}.log")
    return env


@dataclass
class PoolProcess:
    proc_id: str
    port: int
    state: str = SPAWNING
    session_id: str | None = None
    handle: Any = None
    healthz_fails: int = 0
    spawned_at: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


def _default_reap(work: Callable[[], None]) -> None:
    """默认回收执行器:守护线程跑 kill+入队(不阻塞锁,B-2)。"""
    threading.Thread(target=work, name="poolmgr-reap", daemon=True).start()


@dataclass
class PoolIO:
    """可注入的进程 I/O(默认=真实现;单测传假实现)。"""

    spawn: Callable[[str, int], Any] | None = None  # (proc_id, port) → handle
    healthz: Callable[[int], bool] | None = None  # (port) → ready?
    kill: Callable[[Any], None] | None = None  # (handle) → None
    reap: Callable[[Callable[[], None]], None] | None = None  # 回收执行器(默认线程)


@dataclass
class PoolTuning:
    """池调参(端口/录音根/轮询/超时/阈值/时钟)。"""

    base_port: int = 19100
    recordings_root: str | Path = "recordings"
    poll_interval_s: float = 2.0
    healthz_fail_limit: int = 3
    spawn_timeout_s: float = 30.0  # 冷启动超时才判 spawn 失败(≠连败判亡)
    ready_alert_threshold: int | None = None  # None=size//2
    clock: Callable[[], float] = time.monotonic


class PoolManager:
    def __init__(
        self,
        *,
        size: int,
        io: PoolIO | None = None,
        tuning: PoolTuning | None = None,
        transcoder: Any = None,
    ) -> None:
        io = io or PoolIO()
        t = tuning or PoolTuning()
        self._size = int(size)
        self._base_port = int(t.base_port)
        self._recordings_root = Path(t.recordings_root)
        self._spawn_fn = io.spawn or default_spawn
        self._healthz_fn = io.healthz or default_healthz
        self._kill_fn = io.kill or default_kill
        self._reap = io.reap or _default_reap
        self._transcoder = transcoder
        self._poll_interval_s = float(t.poll_interval_s)
        self._fail_limit = int(t.healthz_fail_limit)
        self._spawn_timeout_s = float(t.spawn_timeout_s)
        # 就绪数低于此阈值告警(M4);默认 = size 的一半(至少 1)
        self._ready_alert = (
            t.ready_alert_threshold
            if t.ready_alert_threshold is not None
            else max(1, self._size // 2)
        )
        self._clock = t.clock
        self._procs: dict[str, PoolProcess] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._poll_thread: threading.Thread | None = None

    # ── 生命周期 ────────────────────────────────────────────────────────────
    def start(self) -> None:
        with self._lock:
            for i in range(self._size):
                self._spawn_one(self._base_port + i)
        if self._transcoder is not None:
            with contextlib.suppress(Exception):
                self._transcoder.start()
                self._transcoder.scan_leftovers()  # P-5:接管遗留录音
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name="poolmgr-poll", daemon=True
        )
        self._poll_thread.start()

    def _spawn_one(self, port: int) -> PoolProcess:
        proc_id = uuid.uuid4().hex[:8]
        handle = self._spawn_fn(proc_id, port)
        p = PoolProcess(proc_id=proc_id, port=port, handle=handle, spawned_at=self._clock())
        self._procs[proc_id] = p
        logger.info("spawn proc=%s port=%d", proc_id, port)
        return p

    def _recycle(self, p: PoolProcess, reason: str, *, enqueue_session: str | None = None) -> None:
        """持锁调用:仅 pop + 调度 reaper。**不在锁内、也不立即同端口 spawn**(B-4:旧进程优雅
        收尾数秒仍占端口,立即同端口 spawn 会 EADDRINUSE)。reaper 锁外:kill 确认死(端口释放)→
        同端口重起 → 入队转码(依次满足 B-2 kill 锁外 / B-4 死后重起 / B-3 死后入队)。"""
        logger.info("recycle proc=%s port=%d reason=%s", p.proc_id, p.port, reason)
        p.state = RECYCLING
        self._procs.pop(p.proc_id, None)
        self._reap(lambda: self._reap_work(p.handle, p.port, enqueue_session))

    def _reap_work(self, handle: Any, port: int, enqueue_session: str | None) -> None:
        """回收(锁外执行):kill 并确认进程真死(端口释放、录音收尾完成)→ **死后**同端口重起 →
        入队转码。slot 在此期空缺 ~kill+冷启(即时同端口补位不可得,除非端口数 > 进程数)。"""
        with contextlib.suppress(Exception):
            self._kill_fn(handle)  # terminate → wait → SIGKILL 兜底:确认死、端口释放
        with self._lock:
            if not self._stop.is_set():
                self._spawn_one(port)  # B-4:确认死后才同端口重起,避免抢端口
        if enqueue_session is not None:
            self._enqueue_recordings(enqueue_session)  # B-3:确认死后再转码

    # ── 网关调用面 ──────────────────────────────────────────────────────────
    def alloc(self) -> dict[str, Any] | None:
        """取一个 READY 进程标记 ASSIGNED,返回 {proc_id, port, session_id};无就绪=None(繁忙)。"""
        with self._lock:
            for p in self._procs.values():
                if p.state == READY:
                    p.state = ASSIGNED
                    p.session_id = p.proc_id  # 进程实例即一次会话(用后回收)
                    logger.info("alloc proc=%s port=%d", p.proc_id, p.port)
                    return {"proc_id": p.proc_id, "port": p.port, "session_id": p.session_id}
            logger.info("alloc failed: no ready process (pool busy)")
            return None

    def release(self, session_id: str, reason: str = "") -> bool:
        """会话结束:该会话录音入队转码 + 回收进程。找不到该会话返回 False。"""
        with self._lock:
            target = next(
                (
                    p
                    for p in self._procs.values()
                    if p.session_id == session_id and p.state == ASSIGNED
                ),
                None,
            )
            if target is None:
                return False
            # 录音入队推迟到 reaper 内 kill 确认死后(B-3);kill 的 wait 也在锁外(B-2)。
            self._recycle(target, reason or "released", enqueue_session=session_id)
            return True

    def _enqueue_recordings(self, session_id: str) -> None:
        """把该会话的录音目录(recordings/*_<sid>/)入队转码器。"""
        if self._transcoder is None or not self._recordings_root.is_dir():
            return
        for d in self._recordings_root.glob(f"*_{session_id}"):
            if d.is_dir():
                with contextlib.suppress(Exception):
                    self._transcoder.enqueue_dir(d)

    def status(self) -> dict[str, Any]:
        with self._lock:
            counts = Counter(p.state for p in self._procs.values())
        ready = counts.get(READY, 0)
        out = {
            "size": self._size,
            "ready": ready,
            "assigned": counts.get(ASSIGNED, 0),
            "spawning": counts.get(SPAWNING, 0),
            "ready_below_threshold": ready < self._ready_alert,  # M4 告警
        }
        if self._transcoder is not None:
            with contextlib.suppress(Exception):
                out["transcoder"] = self._transcoder.metrics()
        return out

    def list_ready(self) -> list[dict[str, Any]]:
        """只读列出所有 READY 进程的端口,供无亲和路由(/knows 等)取可用 agent。

        与 alloc() 的关键区别:**不修改状态机**——不标 ASSIGNED、不占槽位、后续也不需 release
        (release 会 kill 进程)。/knows 是高频运维请求,绝不能每次都杀 agent。
        返回 [{"proc_id", "port", "state"}, ...];pool 全空或全 SPAWNING 时返回 []。
        """
        with self._lock:
            return [
                {"proc_id": p.proc_id, "port": p.port, "state": p.state}
                for p in self._procs.values()
                if p.state == READY
            ]

    # ── healthz 轮询 ─────────────────────────────────────────────────────────
    def poll_once(self) -> None:
        """探测一轮。可单测直调:
        - SPAWNING:healthz ok → READY;否则等到 spawn_timeout 才判 spawn 失败(冷启动 ~10s,
          **不能按连败判亡**,否则起来前就被误杀);
        - READY/ASSIGNED:曾存活现连败 fail_limit → 判亡回收。"""
        now = self._clock()
        with self._lock:
            for p in list(self._procs.values()):
                if p.state not in (SPAWNING, READY, ASSIGNED):
                    continue
                ok = False
                with contextlib.suppress(Exception):
                    ok = bool(self._healthz_fn(p.port))
                if ok:
                    p.healthz_fails = 0
                    if p.state == SPAWNING:
                        p.state = READY
                        logger.info("proc=%s ready port=%d", p.proc_id, p.port)
                elif p.state == SPAWNING:
                    if now - p.spawned_at >= self._spawn_timeout_s:
                        self._recycle(p, "spawn timeout")
                else:  # READY/ASSIGNED 曾就绪现失联 → 连败判亡
                    p.healthz_fails += 1
                    if p.healthz_fails >= self._fail_limit:
                        self._recycle(p, f"healthz dead ({p.healthz_fails} fails)")

    def _poll_loop(self) -> None:
        while not self._stop.wait(self._poll_interval_s):
            with contextlib.suppress(Exception):
                self.poll_once()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            for p in list(self._procs.values()):
                with contextlib.suppress(Exception):
                    self._kill_fn(p.handle)
            self._procs.clear()
        if self._transcoder is not None:
            with contextlib.suppress(Exception):
                self._transcoder.stop()


# ── 默认真实 I/O(部署用;单测注入假实现) ──────────────────────────────────
def default_spawn(proc_id: str, port: int) -> subprocess.Popen[bytes]:
    """起 `web_ui_agent.py console`(注入 §7.2 env);cwd=examples/voice_agents,内网端口。"""
    import os
    import sys

    agent_dir = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env.update(default_agent_env(proc_id, port, metrics_dir=agent_dir.parents[1] / ".run"))
    return subprocess.Popen(  # noqa: S603
        [sys.executable, "web_ui_agent.py", "console"]
        + __import__("shlex").split(os.environ.get("XIAOGE_AGENT_CONSOLE_ARGS", "")),
        cwd=str(agent_dir),
        env=env,
        stdin=subprocess.DEVNULL,  # no TTY in background; prevents EBADF in keyboard thread
    )


def default_healthz(port: int, *, timeout: float = 1.0) -> bool:
    """探 http://127.0.0.1:<port>/healthz 的 ready 字段。"""
    import json
    import urllib.request

    with contextlib.suppress(Exception):
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=timeout) as r:  # noqa: S310
            return bool(json.loads(r.read()).get("ready"))
    return False


def default_kill(handle: Any) -> None:
    """terminate(SIGTERM,触发优雅收尾)→ 等 5s;仍未退出 → **SIGKILL 兜底**(B-4b:优雅关闭
    卡住/>5s 时确保进程死、端口释放,否则同端口 slot 永久无法 bind)。"""
    if handle is None:
        return
    with contextlib.suppress(Exception):
        handle.terminate()
    try:
        handle.wait(timeout=5)
        return  # 优雅收尾完成
    except Exception:
        pass
    with contextlib.suppress(Exception):  # 未按时退出 → 强杀
        handle.kill()
        handle.wait(timeout=5)
