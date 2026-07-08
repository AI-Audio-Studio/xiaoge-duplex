"""并发浸泡 harness(§7 / §12.1 浸泡):churn 压 gateway+poolmgr 全栈,采样 RSS/句柄/池态/
持有上游,查泄漏。

**默认用假 agent 子进程**(`tests/_fake_agent_server.py`,零云依赖)——查网关/池的连接·宽限窗·
回收在长时高频 churn 下的**泄漏**(进程树 RSS、文件句柄、会话表、池槽、proxy 持有的上游)。
`--agent-cmd` 可换真 agent,配真 env/录音在目标机做 §7 全量 4 路×2h 浸泡(含 recordings 磁盘
增速 / 转码积压——需真录音,假 agent 恒 0)。报告落 `docs/reports/`。

用法:`cd examples/voice_agents && python -m harness.soak --sessions 4 --duration 120`
(短时冒烟由 `tests/test_ours_concurrency_soak_smoke.py` 驱动)。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
import aiohttp.web as web
import psutil

_VOICE_AGENTS = Path(__file__).resolve().parents[1]
_REPO = _VOICE_AGENTS.parents[1]
if str(_VOICE_AGENTS) not in sys.path:
    sys.path.insert(0, str(_VOICE_AGENTS))
_FAKE_AGENT = _REPO / "tests" / "_fake_agent_server.py"

from gateway import affinity as af, main as gwmain  # noqa: E402
from gateway.config import GatewayConfig  # noqa: E402
from gateway.pool_client import PoolClient  # noqa: E402
from gateway.proxy import Proxy  # noqa: E402
from poolmgr.control_api import build_control_app  # noqa: E402
from poolmgr.manager import (  # noqa: E402
    PoolIO,
    PoolManager,
    PoolTuning,
    default_healthz,
    default_kill,
)


@dataclass
class SoakConfig:
    sessions: int = 4
    duration_s: float = 120.0
    sample_interval_s: float = 5.0
    grace_s: float = 0.5
    agent_cmd: list[str] | None = None  # None=假 agent;真 agent 传启动命令
    report_path: str = ""  # 空=docs/reports/concurrency_soak_<ts>.md
    # 主进程静默基线→静默末态增长上限(泄漏判据)。实测假 agent 短跑 ~2-3MB / ~4 句柄,
    # 取 ~20× 余量既稳(不假阳)又能兜住真泄漏(未释放上游=每漏 1 会话 +1 句柄,长跑累积)。
    rss_growth_limit_mb: float = 60.0
    fd_growth_limit: int = 64


@dataclass
class SoakResult:
    ok: bool
    samples: list[dict[str, Any]] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)
    report_path: str = ""


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def _free_base(size: int, start: int = 22000) -> int:
    port = start
    while port < start + 4000:
        socks: list[socket.socket] = []
        try:
            for i in range(size):
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.bind(("127.0.0.1", port + i))
                socks.append(s)
            return port
        except OSError:
            port += size + 5
        finally:
            for s in socks:
                s.close()
    raise RuntimeError("no free consecutive port range")


def _spawn_agent(cmd: list[str] | None):
    def _spawn(proc_id: str, port: int) -> subprocess.Popen[bytes]:
        env = dict(os.environ)
        env.update(
            {"WEB_UI_HOST": "127.0.0.1", "WEB_UI_PORT": str(port), "XIAOGE_SESSION_ID": proc_id}
        )
        launch = cmd or [sys.executable, str(_FAKE_AGENT)]
        return subprocess.Popen(  # noqa: S603
            launch, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    return _spawn


def _fd_count(p: psutil.Process) -> int:
    with contextlib.suppress(Exception):
        return int(p.num_fds() if hasattr(p, "num_fds") else p.num_handles())
    return 0


def _sample(table: af.AffinityTable, proxy: Proxy, mgr: PoolManager) -> dict[str, Any]:
    """**主进程(常驻网关+池所在)RSS/句柄 = 泄漏判据**;整树 RSS/进程数 = 仅信息。
    SK-1:agent 子进程随池回收增减(含 kill 后残留),整树求和会被"进程数波动"污染而假阳——
    真正要查的常驻泄漏(会话/上游/socket 句柄)都在主进程,故 RSS/FD 只量主进程。"""
    main = psutil.Process()
    tree = [main, *main.children(recursive=True)]
    tree_rss = 0
    for p in tree:
        with contextlib.suppress(Exception):
            tree_rss += p.memory_info().rss
    status = mgr.status()
    return {
        "t": round(time.monotonic(), 2),
        "rss_mb": round(main.memory_info().rss / 1e6, 1),  # 主进程(判据)
        "fds": _fd_count(main),  # 主进程句柄(判据)
        "tree_rss_mb": round(tree_rss / 1e6, 1),  # 整树(信息,含 agent 子树)
        "tree_procs": len(tree),  # 进程数(信息,churn 中波动、不作判据)
        "pool_ready": status.get("ready", 0),
        "pool_assigned": status.get("assigned", 0),
        "held_upstreams": len(proxy._io),  # 宽限窗/活跃上游持有数
        "sessions": len(table._sessions),  # 会话表规模
    }


async def _churn_user(gw: str, deadline: float) -> None:
    """一个虚拟用户:反复 新浏览器→GET/→/ws/audio→帧+回声→断→(半数)窗内重连→断。
    每轮独立捕获异常(回收窗口的瞬时错误不应中断浸泡)。"""
    n = 0
    while asyncio.get_event_loop().time() < deadline:
        n += 1
        jar = aiohttp.CookieJar(unsafe=True)
        try:
            async with aiohttp.ClientSession(cookie_jar=jar) as br:
                r = await br.get(f"{gw}/")
                if r.status != 200:  # 池满 → 退避重试
                    await asyncio.sleep(0.05)
                    continue
                ws = await br.ws_connect(f"{gw}/ws/audio")
                await ws.receive()  # 身份帧
                for _ in range(3):
                    await ws.send_bytes(b"x" * 320)
                    await ws.receive()
                await ws.close()
                if n % 2 == 0:  # 半数窗内重连(REATTACH),半数放任超时回收
                    ws2 = await br.ws_connect(f"{gw}/ws/audio")
                    await ws2.send_bytes(b"y" * 320)
                    await ws2.receive()
                    await ws2.close()
        except Exception:
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.01)


@contextlib.asynccontextmanager
async def _stack(cfg: SoakConfig):  # noqa: ANN201
    base = _free_base(cfg.sessions)
    ctrl_port, gw_port = _free_port(), _free_port()
    tmp = tempfile.mkdtemp(prefix="xg_soak_")
    mgr = PoolManager(
        size=cfg.sessions,
        io=PoolIO(spawn=_spawn_agent(cfg.agent_cmd), healthz=default_healthz, kill=default_kill),
        tuning=PoolTuning(
            base_port=base, recordings_root=tmp, poll_interval_s=0.2, spawn_timeout_s=30.0
        ),
    )
    mgr.start()
    end = time.monotonic() + 40
    while mgr.status()["ready"] < cfg.sessions and time.monotonic() < end:
        await asyncio.sleep(0.1)
    if mgr.status()["ready"] < cfg.sessions:
        mgr.stop()
        raise RuntimeError(f"pool not ready: {mgr.status()}")

    ctrl = web.AppRunner(build_control_app(mgr))
    await ctrl.setup()
    await web.TCPSite(ctrl, "127.0.0.1", ctrl_port).start()
    gcfg = GatewayConfig(
        hmac_secret="soak", pool_api=f"http://127.0.0.1:{ctrl_port}", grace_seconds=cfg.grace_s
    )
    table = af.AffinityTable(grace_seconds=cfg.grace_s, secret=gcfg.hmac_secret)
    proxy = Proxy(gcfg, table)
    pool = PoolClient(gcfg.pool_api)
    gw = web.AppRunner(gwmain.build_gateway_app(gcfg, table, proxy, pool))
    await gw.setup()
    await web.TCPSite(gw, "127.0.0.1", gw_port).start()
    sweep = asyncio.create_task(gwmain._sweep_loop(table, proxy, pool, interval=0.1))
    try:
        yield f"http://127.0.0.1:{gw_port}", table, proxy, mgr
    finally:
        sweep.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sweep
        await proxy.aclose()
        await pool.close()
        await gw.cleanup()
        await ctrl.cleanup()
        mgr.stop()
        await asyncio.sleep(0.3)


async def _cooldown_to_quiescent(
    cfg: SoakConfig, table: af.AffinityTable, proxy: Proxy, mgr: PoolManager
) -> None:
    """等宽限窗全超时 + 池复位 + 无持有上游/会话(静默态),再多留一拍让 socket 句柄释放收敛。"""
    end = time.monotonic() + max(3.0, cfg.grace_s * 4 + 2.0)
    while time.monotonic() < end:
        if (
            mgr.status()["ready"] == cfg.sessions
            and len(proxy._io) == 0
            and len(table._sessions) == 0
        ):
            break
        await asyncio.sleep(0.2)
    await asyncio.sleep(0.6)  # FD 释放收敛(TIME_WAIT/连接池回收)


async def run_soak(cfg: SoakConfig) -> SoakResult:
    async with _stack(cfg) as (gw, table, proxy, mgr):
        loop = asyncio.get_event_loop()
        # SK-1:泄漏判据 = **churn 前静默基线** vs **churn+冷却后静默末态**(同为静默态,可比;
        # 真泄漏 = 资源未释放、末态显著高于基线)。churn 中样本仅作趋势信息、不入判据。
        await asyncio.sleep(1.0)  # 栈进程/连接达稳态
        baseline = _sample(table, proxy, mgr)
        samples: list[dict[str, Any]] = [baseline]

        deadline = loop.time() + cfg.duration_s
        users = [asyncio.create_task(_churn_user(gw, deadline)) for _ in range(cfg.sessions)]
        while loop.time() < deadline:
            await asyncio.sleep(cfg.sample_interval_s)
            samples.append(_sample(table, proxy, mgr))  # churn 中(信息趋势)
        await asyncio.gather(*users, return_exceptions=True)

        await _cooldown_to_quiescent(cfg, table, proxy, mgr)
        final = _sample(table, proxy, mgr)
        samples.append(final)

    checks = _evaluate(cfg, baseline, final)
    result = SoakResult(ok=all(checks.values()), samples=samples, checks=checks)
    result.report_path = _write_report(cfg, result)
    return result


def _evaluate(cfg: SoakConfig, baseline: dict[str, Any], final: dict[str, Any]) -> dict[str, bool]:
    rss_growth = final["rss_mb"] - baseline["rss_mb"]
    fd_growth = final["fds"] - baseline["fds"]
    return {
        "no_session_leak": final["sessions"] == 0,  # 末态会话表空
        "no_held_upstream_leak": final["held_upstreams"] == 0,  # 无宽限窗上游残留
        "pool_recovered": final["pool_ready"] == cfg.sessions,  # 池全回收复位
        "rss_bounded": rss_growth <= cfg.rss_growth_limit_mb,  # RSS 增长受限
        "fd_bounded": fd_growth <= cfg.fd_growth_limit,  # 句柄增长受限
    }


def _write_report(cfg: SoakConfig, result: SoakResult) -> str:
    ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    path = Path(cfg.report_path or (_REPO / "docs" / "reports" / f"concurrency_soak_{ts}.md"))
    path.parent.mkdir(parents=True, exist_ok=True)
    base = result.samples[0] if result.samples else {}
    fin = result.samples[-1] if result.samples else {}
    lines = [
        f"# 并发浸泡报告 {ts}",
        "",
        f"- 配置:sessions={cfg.sessions} duration={cfg.duration_s}s grace={cfg.grace_s}s "
        f"agent={'real' if cfg.agent_cmd else 'fake(子进程)'}",
        f"- 判定:**{'PASS' if result.ok else 'FAIL'}**",
        "",
        "## 泄漏检查",
        "| 检查 | 结果 |",
        "| --- | --- |",
        *[f"| {k} | {'✅' if v else '❌'} |" for k, v in result.checks.items()],
        "",
        "判据取 churn 前静默基线 vs churn+冷却后静默末态(同静默态可比,SK-1);"
        "RSS/句柄只量**主进程**(常驻网关+池),整树数据仅信息。",
        "",
        f"- 主进程 RSS:基线 {base.get('rss_mb')}MB → 末态 {fin.get('rss_mb')}MB "
        f"(增长 {round(fin.get('rss_mb', 0) - base.get('rss_mb', 0), 1)}MB,限 {cfg.rss_growth_limit_mb})",
        f"- 主进程句柄:基线 {base.get('fds')} → 末态 {fin.get('fds')} "
        f"(增长 {fin.get('fds', 0) - base.get('fds', 0)},限 {cfg.fd_growth_limit})",
        f"- 末态:会话表={fin.get('sessions')} 持有上游={fin.get('held_upstreams')} "
        f"池就绪={fin.get('pool_ready')}/{cfg.sessions}",
        f"- 整树(信息):基线 {base.get('tree_procs')} proc/{base.get('tree_rss_mb')}MB → "
        f"末态 {fin.get('tree_procs')} proc/{fin.get('tree_rss_mb')}MB(含 agent 子进程,随回收波动)",
        "",
        "## 采样序列(主进程 RSS/句柄 + 池态/持有上游 + 整树信息)",
        "```json",
        json.dumps(result.samples, ensure_ascii=False, indent=1),
        "```",
        "",
        "> 注:假 agent 浸泡查网关/池(主进程)泄漏(RSS/句柄/会话/槽/上游);recordings 磁盘增速 / "
        "转码积压曲线需真 agent + 真录音,属目标机 4 路×2h 全量浸泡(§7)。",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


def main(argv: Any = None) -> int:
    ap = argparse.ArgumentParser(description="并发浸泡 harness(§7)")
    ap.add_argument("--sessions", type=int, default=4)
    ap.add_argument("--duration", type=float, default=120.0)
    ap.add_argument("--sample-interval", type=float, default=5.0)
    ap.add_argument("--grace", type=float, default=0.5)
    ap.add_argument("--report", default="")
    ap.add_argument("--rss-growth-mb", type=float, default=80.0)
    a = ap.parse_args(argv)
    cfg = SoakConfig(
        sessions=a.sessions,
        duration_s=a.duration,
        sample_interval_s=a.sample_interval,
        grace_s=a.grace,
        report_path=a.report,
        rss_growth_limit_mb=a.rss_growth_mb,
    )
    result = asyncio.run(run_soak(cfg))
    print(json.dumps({"ok": result.ok, "checks": result.checks, "report": result.report_path}))
    print(f"report: {result.report_path}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
