"""行为锁定测试:并发改造 PR-B 进程池管理器(v4 §7/§4.2)。

覆盖:spawn→ready→alloc→release→recycle 状态机、繁忙返 None、healthz 连败判亡回收、
SPAWNING 冷启动不被误杀、B-2 kill 锁外、B-3 死后入队、B-4 死后同端口重起、B-4b SIGKILL 兜底、
env 注入表、就绪告警。多数用假 I/O(时序);另有**真端口/真进程集成用例**(default_kill 真杀、
recycle 同端口重 bind)堵"假 I/O 逃过"——无云依赖。
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

_AGENT_DIR = Path(__file__).resolve().parents[1] / "examples" / "voice_agents"
sys.path.insert(0, str(_AGENT_DIR))

from poolmgr.manager import (  # noqa: E402
    PoolIO,
    PoolManager,
    PoolTuning,
    default_agent_env,
    default_kill,
)


def _wait_until(pred, timeout: float = 12.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.1)
    return False


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# 极小假 agent:绑端口 + 应答 /healthz{ready:true}(无模型,秒起)。SIGTERM 后**延迟 ~1.5s
# 才退**(仿真 agent cli.py 优雅收尾数秒持端口;仅 POSIX 生效)——使"立即同端口 spawn"的旧
# bug 能被端到端用例稳定判红(回归护栏,评审组 §四建议)。Windows terminate 硬杀不走 handler,
# 即刻死、测试仍正向通过。
_FAKE_AGENT = (
    "import sys,json,signal,time\n"
    "from http.server import HTTPServer,BaseHTTPRequestHandler\n"
    "def _bye(*a):\n"
    " time.sleep(1.5); sys.exit(0)\n"
    "try:\n"
    " signal.signal(signal.SIGTERM,_bye)\n"
    "except Exception:\n"
    " pass\n"
    "class H(BaseHTTPRequestHandler):\n"
    " def do_GET(s):\n"
    "  s.send_response(200);s.send_header('Content-Type','application/json');s.end_headers()\n"
    "  s.wfile.write(json.dumps({'ready':True}).encode())\n"
    " def log_message(s,*a):pass\n"
    "HTTPServer(('127.0.0.1',int(sys.argv[1])),H).serve_forever()\n"
)


class _FakeHandle:
    def __init__(self, proc_id: str, port: int) -> None:
        self.proc_id, self.port, self.killed = proc_id, port, False

    def terminate(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> None:
        pass


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _make(size: int = 2, transcoder: object = None, reap=None, kill=None) -> tuple:
    spawned: list[_FakeHandle] = []
    ready: dict[int, bool] = {}
    clock = _Clock()

    def spawn(pid: str, port: int) -> _FakeHandle:
        h = _FakeHandle(pid, port)
        spawned.append(h)
        return h

    m = PoolManager(
        size=size,
        io=PoolIO(
            spawn=spawn,
            healthz=lambda port: ready.get(port, False),
            kill=kill or (lambda h: h.terminate()),
            reap=reap or (lambda work: work()),  # 默认同步执行,断言确定;B-2 用捕获式
        ),
        tuning=PoolTuning(base_port=19100, poll_interval_s=3600, clock=clock),  # 后台线程休眠
        transcoder=transcoder,
    )
    return m, spawned, ready, clock


def test_spawn_ready_alloc(tmp_path: Path) -> None:
    m, spawned, ready, _ = _make(size=2)
    m.start()
    assert len(spawned) == 2 and m.status()["spawning"] == 2 and m.status()["ready"] == 0
    for h in spawned:
        ready[h.port] = True
    m.poll_once()
    assert m.status()["ready"] == 2
    a = m.alloc()
    assert a is not None and a["port"] in (19100, 19101)
    assert m.status()["ready"] == 1 and m.status()["assigned"] == 1
    m.stop()


def test_alloc_none_when_busy() -> None:
    m, spawned, ready, _ = _make(size=1)
    m.start()  # 1 进程 spawning、未就绪
    assert m.alloc() is None
    ready[spawned[0].port] = True
    m.poll_once()
    assert m.alloc() is not None  # 就绪后可分配
    assert m.alloc() is None  # 唯一进程已占,再分配=繁忙
    m.stop()


def test_release_recycles_and_enqueues(tmp_path: Path) -> None:
    class _FakeTx:
        def __init__(self) -> None:
            self.enq: list[Path] = []

        def start(self) -> None: ...
        def scan_leftovers(self) -> int:
            return 0

        def enqueue_dir(self, d: Path) -> None:
            self.enq.append(Path(d))

        def metrics(self) -> dict:
            return {"queue_depth": len(self.enq)}

        def stop(self) -> None: ...

    tx = _FakeTx()
    root = tmp_path / "recordings"
    m, spawned, ready, _ = _make(size=1, transcoder=tx)
    m._recordings_root = root
    m.start()
    ready[spawned[0].port] = True
    m.poll_once()
    a = m.alloc()
    sid = a["session_id"]
    (root / f"20260707_100000_{sid}").mkdir(parents=True)  # agent 侧录音目录
    assert m.release(sid, "done") is True
    assert any(sid in str(p) for p in tx.enq)  # 录音入队转码
    assert spawned[0].killed is True  # 旧进程被杀
    assert len(spawned) == 2  # 同端口重启补位
    assert m.release("nonexistent", "x") is False
    m.stop()


def test_healthz_dead_recycles_ready_proc() -> None:
    m, spawned, ready, _ = _make(size=1)
    m.start()
    ready[spawned[0].port] = True
    m.poll_once()  # → ready
    assert m.status()["ready"] == 1
    ready[spawned[0].port] = False  # 进程死了
    for _ in range(3):  # 连败 3 次判亡
        m.poll_once()
    assert spawned[0].killed is True and len(spawned) == 2  # 回收+重启
    m.stop()


def test_spawning_not_killed_before_timeout() -> None:
    """冷启动中的 SPAWNING 进程连续 healthz 失败**不得**被判亡(否则起来前被误杀)。"""
    m, spawned, ready, clock = _make(size=1)
    m.start()  # spawning,healthz 一直 false(还没起来)
    for _ in range(10):  # 远超 fail_limit
        m.poll_once()
    assert spawned[0].killed is False and len(spawned) == 1  # 仍在等,没被杀
    clock.t = 31.0  # 越过 spawn_timeout(30s)
    m.poll_once()
    assert spawned[0].killed is True and len(spawned) == 2  # 冷启动超时才判失败重起
    m.stop()


def test_ready_below_threshold_alert() -> None:
    m, spawned, ready, _ = _make(size=4)  # 阈值默认 = size//2 = 2
    m.start()
    assert m.status()["ready_below_threshold"] is True  # 0 ready < 2
    for h in spawned:
        ready[h.port] = True
    m.poll_once()
    assert m.status()["ready_below_threshold"] is False  # 4 ready ≥ 2
    m.stop()


def test_release_defers_kill_off_lock_b2(tmp_path: Path) -> None:
    """B-2:kill(含 wait)不得在锁内阻塞——release 立即返回、status 不冻结;kill 推迟到 reaper。"""
    captured: list = []
    m, spawned, ready, _ = _make(size=1, reap=captured.append)  # 捕获 reaper work,不执行
    m.start()
    ready[spawned[0].port] = True
    m.poll_once()
    a = m.alloc()
    assert m.release(a["session_id"], "done") is True
    assert spawned[0].killed is False  # kill 尚未发生(未在锁内阻塞)
    assert m.status()["size"] == 1  # status 未被 kill 的 wait 冻结
    assert len(spawned) == 1  # B-4:同端口重起推迟到 reaper(kill 确认死后),此刻未补位
    captured[0]()  # 执行 reaper → kill → 同端口重起
    assert spawned[0].killed is True and len(spawned) == 2
    m.stop()


def test_recycle_spawns_after_kill_b4(tmp_path: Path) -> None:
    """B-4:同端口重起必须在 kill 确认死之后(旧进程收尾释放端口后),不得立即 spawn。"""
    order: list[str] = []

    def _kill(h: _FakeHandle) -> None:
        h.killed = True
        order.append("kill")

    spawned2: list[_FakeHandle] = []

    def _spawn(pid: str, port: int) -> _FakeHandle:
        order.append(f"spawn:{port}")
        h = _FakeHandle(pid, port)
        spawned2.append(h)
        return h

    m = PoolManager(
        size=1,
        io=PoolIO(spawn=_spawn, healthz=lambda p: True, kill=_kill, reap=lambda w: w()),
        tuning=PoolTuning(base_port=19100, poll_interval_s=3600, clock=_Clock()),
    )
    m.start()  # order: ["spawn:19100"]
    m.poll_once()
    a = m.alloc()
    order.clear()  # 只看 recycle 期
    m.release(a["session_id"], "done")
    assert order == ["kill", "spawn:19100"]  # 先杀(端口释放)后同端口重起
    m.stop()


def test_kill_before_enqueue_b3(tmp_path: Path) -> None:
    """B-3:reaper 内必须**先 kill(确认进程死)再入队录音转码**,免与录音收尾竞态。"""
    order: list[str] = []

    class _Tx:
        def start(self) -> None: ...
        def scan_leftovers(self) -> int:
            return 0

        def enqueue_dir(self, d: object) -> None:
            order.append("enqueue")

        def metrics(self) -> dict:
            return {}

        def stop(self) -> None: ...

    root = tmp_path / "recordings"
    m, spawned, ready, _ = _make(size=1, transcoder=_Tx(), kill=lambda h: order.append("kill"))
    m._recordings_root = root
    m.start()
    ready[spawned[0].port] = True
    m.poll_once()
    a = m.alloc()
    sid = a["session_id"]
    (root / f"20260707_100000_{sid}").mkdir(parents=True)
    m.release(sid, "done")
    assert order == ["kill", "enqueue"]  # 先杀后转码
    m.stop()


def test_default_kill_sigkill_fallback() -> None:
    """B-4b:terminate 后 5s 仍未退出 → 走 SIGKILL 兜底(否则端口永占、slot 永久死)。"""

    class _Stubborn:
        def __init__(self) -> None:
            self.terminated = self.killed = False

        def terminate(self) -> None:
            self.terminated = True  # 模拟优雅关闭卡住,不退出

        def wait(self, timeout: float | None = None) -> None:
            if not self.killed:
                raise subprocess.TimeoutExpired("agent", timeout or 0)  # terminate 后仍不退

        def kill(self) -> None:
            self.killed = True

    h = _Stubborn()
    default_kill(h)
    assert h.terminated and h.killed  # 先 SIGTERM、超时后 SIGKILL


def test_default_kill_terminates_real_process() -> None:
    """真进程:default_kill 必须真把它杀死(假 handle 逃过的路径,用真 subprocess 兜底)。"""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    assert proc.poll() is None  # 活着
    default_kill(proc)
    assert proc.poll() is not None  # 已死


def test_recycle_rebinds_same_port_real_process() -> None:
    """**真端口 + 真进程集成**(堵"假 I/O 逃过"):recycle 后新进程在旧进程死后于**同端口**
    成功 bind 并 ready——B-4 修复的端到端证明(立即同端口 spawn 会 EADDRINUSE)。"""
    port = _free_port()
    m = PoolManager(
        size=1,
        io=PoolIO(
            spawn=lambda pid, p: subprocess.Popen([sys.executable, "-c", _FAKE_AGENT, str(p)])
        ),
        tuning=PoolTuning(base_port=port, poll_interval_s=0.2),  # 真 healthz/kill,后台轮询
    )
    try:
        m.start()
        assert _wait_until(lambda: m.status()["ready"] == 1), "初始进程未就绪"
        a = m.alloc()
        assert a is not None and a["port"] == port
        assert m.release(a["session_id"], "done") is True
        # reaper 线程:kill 旧进程(确认死、释放端口)→ 同端口重起 → 后台轮询转 ready
        assert _wait_until(lambda: m.status()["ready"] == 1, timeout=15), (
            "同端口重起未就绪(疑 B-4 抢端口)"
        )
    finally:
        m.stop()


def test_env_injection_table() -> None:
    env = default_agent_env("abc123", 19105, metrics_dir=Path("/tmp/run"))
    assert env["WEB_UI_HOST"] == "127.0.0.1"  # M3 内网绑定
    assert env["WEB_UI_PORT"] == "19105"
    assert env["XIAOGE_KWS_ENABLE_NATIVE"] == "0"  # D-06
    assert env["XIAOGE_SESSION_ID"] == "abc123"
    assert env["XIAOGE_RECORD_MODE"] == "full" and env["XIAOGE_RECORD_CODEC"] == "opus"
    assert env["XIAOGE_TIMELINE_LEVEL"] == "audit"  # D-14 近期组合
    assert env["XIAOGE_ADMIN_ROUTES"] == "0"  # M5/D-19 显式隐藏 asr/tts
    assert "abc123" in env["TURN_METRICS_LOG"]


def test_env_injection_respects_recording_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XIAOGE_RECORD_MODE", "off")
    monkeypatch.setenv("XIAOGE_TIMELINE_LEVEL", "off")

    env = default_agent_env("abc123", 19105)

    assert env["XIAOGE_RECORD_MODE"] == "off"
    assert env["XIAOGE_TIMELINE_LEVEL"] == "off"
