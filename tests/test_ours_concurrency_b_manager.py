"""行为锁定测试:并发改造 PR-B 进程池管理器(v4 §7/§4.2)。

覆盖:spawn→ready→alloc→release→recycle 状态机、繁忙返 None、healthz 连败判亡回收、
SPAWNING 冷启动不被误杀(spawn_timeout 才判失败)、录音入队转码、env 注入表、就绪告警。
I/O 全用假实现,无真进程、无云依赖。
"""

from __future__ import annotations

import sys
from pathlib import Path

_AGENT_DIR = Path(__file__).resolve().parents[1] / "examples" / "voice_agents"
sys.path.insert(0, str(_AGENT_DIR))

from poolmgr.manager import PoolIO, PoolManager, PoolTuning, default_agent_env  # noqa: E402


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
    """B-2:kill 的 wait 不得在锁内阻塞——release 立即返回、池即时补位、status 不冻结,
    kill 推迟到 reaper 执行。"""
    captured: list = []
    m, spawned, ready, _ = _make(size=1, reap=captured.append)  # 捕获 reaper work,不执行
    m.start()
    ready[spawned[0].port] = True
    m.poll_once()
    a = m.alloc()
    assert m.release(a["session_id"], "done") is True
    assert spawned[0].killed is False  # kill 尚未发生(未在锁内阻塞)
    assert len(spawned) == 2  # 池已即时补位
    assert m.status()["size"] == 1  # status 未被 kill 的 wait 冻结
    captured[0]()  # 执行 reaper → kill 真正发生
    assert spawned[0].killed is True
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


def test_env_injection_table() -> None:
    env = default_agent_env("abc123", 19105, metrics_dir=Path("/tmp/run"))
    assert env["WEB_UI_HOST"] == "127.0.0.1"  # M3 内网绑定
    assert env["WEB_UI_PORT"] == "19105"
    assert env["XIAOGE_KWS_ENABLE_NATIVE"] == "0"  # D-06
    assert env["XIAOGE_SESSION_ID"] == "abc123"
    assert env["XIAOGE_RECORD_MODE"] == "full" and env["XIAOGE_RECORD_CODEC"] == "opus"
    assert env["XIAOGE_TIMELINE_LEVEL"] == "audit"  # D-14 近期组合
    assert "abc123" in env["TURN_METRICS_LOG"]
