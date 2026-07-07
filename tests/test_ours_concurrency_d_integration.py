"""PR-D 全链集成测:真池管理器(spawn 假 agent 子进程)→ 真控制 API → 真网关 → 真 WS/HTTP 客户端。

覆盖单组件测覆盖不到的**跨组件行为**:①浏览器全链往返(GET/ 分配→cookie→/ws/audio 身份帧+回声
→/api/mic 反代);②**宽限窗跨真进程**(断开→网关持有真上游、agent 不感知;窗内重连接回同一
上游、agent 全程只被连一次;超时→网关 release→池同端口回收换新进程);③N+1 繁忙;④断开→回收→
槽复用。承前教训:进程/连接/端口类必配真子进程+真端口,不以假时序代替。慢测(真子进程冷启)。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import aiohttp.web as web

_AGENT_DIR = Path(__file__).resolve().parents[1] / "examples" / "voice_agents"
sys.path.insert(0, str(_AGENT_DIR))

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

_FAKE_AGENT = Path(__file__).resolve().parent / "_fake_agent_server.py"


def _spawn_fake(proc_id: str, port: int) -> subprocess.Popen[bytes]:
    """像池管理器 spawn 真 agent 一样起假 agent 子进程(注入 WEB_UI_PORT/XIAOGE_SESSION_ID)。"""
    env = dict(os.environ)
    env.update({"WEB_UI_HOST": "127.0.0.1", "WEB_UI_PORT": str(port), "XIAOGE_SESSION_ID": proc_id})
    return subprocess.Popen(  # noqa: S603
        [sys.executable, str(_FAKE_AGENT)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def _free_base(size: int, start: int = 21000) -> int:
    """找一段 size 个连续空闲端口的起点(池管理器按 base+i 顺序绑)。"""
    port = start
    while port < start + 3000:
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


async def _await(fn: Any, timeout: float) -> bool:
    end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end:
        if fn():
            return True
        await asyncio.sleep(0.1)
    return False


def _browser() -> aiohttp.ClientSession:
    """浏览器会话:unsafe cookie jar(默认 jar 拒绝 IP 主机 127.0.0.1 的 cookie,亲和 cookie 会丢)。"""
    return aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))


async def _agent_healthz(port: int) -> dict[str, Any] | None:
    """直接探 agent(绕过网关)读 pid/audio_total,证进程身份与连接次数。回收窗口无监听 → None。"""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"http://127.0.0.1:{port}/healthz", timeout=aiohttp.ClientTimeout(total=1)
            ) as r:
                return await r.json()  # type: ignore[no-any-return]
    except Exception:
        return None


@dataclass
class _Stack:
    gw: str  # 网关基址 http://127.0.0.1:<port>
    base: int  # 假 agent 端口起点(size=1 时即该 agent 端口)
    mgr: PoolManager


@contextlib.asynccontextmanager
async def _stack(size: int, grace: float, access_code: str = ""):  # noqa: ANN201
    """拉起完整栈,yield _Stack;退出时拆栈 + 杀所有 agent 子进程。"""
    base = _free_base(size)
    ctrl_port, gw_port = _free_port(), _free_port()
    tmp = tempfile.mkdtemp(prefix="xg_pd_")
    mgr = PoolManager(
        size=size,
        io=PoolIO(spawn=_spawn_fake, healthz=default_healthz, kill=default_kill),
        tuning=PoolTuning(
            base_port=base, recordings_root=tmp, poll_interval_s=0.2, spawn_timeout_s=25.0
        ),
    )
    mgr.start()
    ready = await _await(lambda: mgr.status()["ready"] == size, timeout=30)
    assert ready, f"pool not ready in time: {mgr.status()}"

    ctrl_runner = web.AppRunner(build_control_app(mgr))
    await ctrl_runner.setup()
    await web.TCPSite(ctrl_runner, "127.0.0.1", ctrl_port).start()

    cfg = GatewayConfig(
        hmac_secret="s",
        pool_api=f"http://127.0.0.1:{ctrl_port}",
        grace_seconds=grace,
        access_code=access_code,
    )
    table = af.AffinityTable(grace_seconds=grace, secret=cfg.hmac_secret)
    proxy = Proxy(cfg, table)
    pool = PoolClient(cfg.pool_api)
    gw_runner = web.AppRunner(gwmain.build_gateway_app(cfg, table, proxy, pool))
    await gw_runner.setup()
    await web.TCPSite(gw_runner, "127.0.0.1", gw_port).start()
    sweep = asyncio.create_task(gwmain._sweep_loop(table, proxy, pool, interval=0.1))
    try:
        yield _Stack(gw=f"http://127.0.0.1:{gw_port}", base=base, mgr=mgr)
    finally:
        sweep.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sweep
        await proxy.aclose()
        await pool.close()
        await gw_runner.cleanup()
        await ctrl_runner.cleanup()
        mgr.stop()
        await asyncio.sleep(0.2)


# ── ① 浏览器全链往返 ──────────────────────────────────────────────────────────
def test_full_stack_browser_roundtrip() -> None:
    async def _main() -> None:
        async with _stack(size=1, grace=5.0) as st:
            async with _browser() as br:
                r = await br.get(f"{st.gw}/")
                assert r.status == 200 and gwmain.COOKIE in r.cookies  # 分配 + 种 cookie
                ws = await br.ws_connect(f"{st.gw}/ws/audio")
                assert (await ws.receive()).data.startswith("agent:")  # agent 身份帧透传
                await ws.send_bytes(b"hi")
                assert (await ws.receive()).data == b"echo:hi"  # 音频双向透传
                async with br.post(f"{st.gw}/api/mic", json={}) as pr:  # /api 反代
                    assert pr.status == 200 and (await pr.json())["ok"] is True
                await ws.close()

    asyncio.run(_main())


# ── ② 宽限窗跨真进程:持有上游 / REATTACH 同进程 / 超时回收换新进程 ─────────────
def test_grace_reattach_same_process_then_timeout_recycle() -> None:
    async def _main() -> None:
        async with _stack(size=1, grace=1.5) as st:
            hz0 = await _agent_healthz(st.base)
            pid0 = hz0["pid"]
            async with _browser() as br:
                await br.get(f"{st.gw}/")  # 分配 + cookie(jar 自动携带)
                ws1 = await br.ws_connect(f"{st.gw}/ws/audio")
                assert (await ws1.receive()).data == f"agent:{hz0['sid']}:{pid0}:1"  # 第一次被连
                await ws1.send_bytes(b"a")
                assert (await ws1.receive()).data == b"echo:a"
                hz1 = await _agent_healthz(st.base)
                assert hz1["audio_total"] == 1 and hz1["audio_conns"] == 1
                await ws1.close()  # 断开 → 宽限窗:网关持有真上游
                await asyncio.sleep(0.4)  # < grace
                hzg = await _agent_healthz(st.base)
                assert hzg["audio_conns"] == 1 and hzg["pid"] == pid0  # 上游仍被持有、进程未换
                ws2 = await br.ws_connect(f"{st.gw}/ws/audio")  # 窗内重连 = REATTACH
                await ws2.send_bytes(b"b")
                assert (await ws2.receive()).data == b"echo:b"  # 同上游续接、回声正常
                hz2 = await _agent_healthz(st.base)
                assert (
                    hz2["audio_total"] == 1 and hz2["pid"] == pid0
                )  # ★ agent 全程只被连一次、同进程
                await ws2.close()
            # 超时 → 网关 sweep release → 池同端口回收换新进程(pid 变)
            changed = await _await(
                lambda: (h := _sync_pid(st.base)) is not None and h != pid0, timeout=12
            )
            assert changed, "recycle did not replace process after grace timeout"

    asyncio.run(_main())


def _sync_pid(port: int) -> int | None:
    """同步探 agent pid(供 _await 谓词用);无监听 → None。"""
    import json
    import urllib.request

    with contextlib.suppress(Exception):
        with urllib.request.urlopen(  # noqa: S310
            f"http://127.0.0.1:{port}/healthz", timeout=1
        ) as r:
            return int(json.loads(r.read())["pid"])
    return None


# ── ③ N+1 繁忙 ───────────────────────────────────────────────────────────────
def test_pool_busy_returns_busy_page() -> None:
    async def _main() -> None:
        async with _stack(size=1, grace=5.0) as st:
            async with _browser() as br1, _browser() as br2:
                r1 = await br1.get(f"{st.gw}/")
                assert r1.status == 200  # 占满唯一座位(alloc → ASSIGNED)
                r2 = await br2.get(f"{st.gw}/")  # 新浏览器 → 池满
                assert r2.status == 503 and "繁忙" in await r2.text()

    asyncio.run(_main())


# ── ④ 断开→回收→槽复用 ───────────────────────────────────────────────────────
def test_release_recycles_and_slot_reusable() -> None:
    async def _main() -> None:
        async with _stack(size=1, grace=0.5) as st:
            async with _browser() as br:
                await br.get(f"{st.gw}/")
                ws = await br.ws_connect(f"{st.gw}/ws/audio")
                await ws.receive()  # 身份帧
                await ws.close()  # → PENDING(0.5s)→ sweep release → 池回收
            reused = await _await(lambda: st.mgr.status()["ready"] >= 1, timeout=12)
            assert reused, f"slot not recycled: {st.mgr.status()}"
            async with _browser() as br2:  # 回收后可再分配
                r = await br2.get(f"{st.gw}/")
                assert r.status == 200

    asyncio.run(_main())


# ── ⑤ 批量断开:N 路同断 → 全部 release → 池全部回收复位 ─────────────────────────
def test_batch_disconnect_all_released_and_recovered() -> None:
    async def _main() -> None:
        n = 3
        async with _stack(size=n, grace=0.5) as st:
            sessions = [_browser() for _ in range(n)]
            wss = []
            for br in sessions:
                await br.get(f"{st.gw}/")  # 各占一座
                ws = await br.ws_connect(f"{st.gw}/ws/audio")
                await ws.receive()  # 身份帧
                wss.append(ws)
            assert st.mgr.status()["ready"] == 0  # N 座全占
            await asyncio.gather(*(ws.close() for ws in wss))  # 同刻全断
            for br in sessions:
                await br.close()
            # 全部 → PENDING(0.5s)→ sweep release → 池同端口全回收复位
            recovered = await _await(lambda: st.mgr.status()["ready"] == n, timeout=20)
            assert recovered, f"pool did not recover all {n} slots: {st.mgr.status()}"

    asyncio.run(_main())
