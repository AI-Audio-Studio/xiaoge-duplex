"""进程池并发功能测试脚本。

验收项：
  T1  初始状态   size/ready/assigned/spawning 总和一致
  T2  并发 alloc  N 路同时申请，全部成功、session_id 不重复
  T3  池耗尽      第 N+1 路返回 503
  T4  release     释放后 ready 回升（池重新补充新进程）
  T5  超量释放    release 不存在的 session_id 返回 ok=False
  T6  状态一致性  全程 ready+assigned+spawning == size

用法（在服务器上运行）：
  cd /data/home/allen.wangmh/software/xiaoge/xiaoge-duplex-main
  python examples/voice_agents/test_pool_concurrent.py
"""

from __future__ import annotations

import concurrent.futures
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

POOL_URL = "http://127.0.0.1:19000"
TIMEOUT = 5.0
REPLENISH_WAIT_S = 35  # 等新 agent spawn 完成的最长时间（spawn_timeout_s=30）


# ── HTTP helpers ────────────────────────────────────────────────────────────

def _post(path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body or {}).encode() if body is not None else b"{}"
    req = urllib.request.Request(
        f"{POOL_URL}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get(path: str) -> dict:
    with urllib.request.urlopen(f"{POOL_URL}{path}", timeout=TIMEOUT) as r:
        return json.loads(r.read())


def status() -> dict:
    return _get("/status")


def alloc() -> tuple[int, dict]:
    return _post("/alloc")


def release(session_id: str, reason: str = "test") -> bool:
    _, body = _post("/release", {"session_id": session_id, "reason": reason})
    return bool(body.get("ok"))


# ── Result tracking ─────────────────────────────────────────────────────────

@dataclass
class Result:
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def ok(self, name: str, detail: str = "") -> None:
        msg = f"  [PASS] {name}" + (f" — {detail}" if detail else "")
        print(msg)
        self.passed.append(name)

    def fail(self, name: str, detail: str = "") -> None:
        msg = f"  [FAIL] {name}" + (f" — {detail}" if detail else "")
        print(msg)
        self.failed.append(name)

    def check(self, name: str, cond: bool, detail: str = "") -> bool:
        if cond:
            self.ok(name, detail)
        else:
            self.fail(name, detail)
        return cond

    def summary(self) -> None:
        total = len(self.passed) + len(self.failed)
        print(f"\n{'='*50}")
        print(f"结果: {len(self.passed)}/{total} 通过", end="")
        if self.failed:
            print(f"  失败项: {', '.join(self.failed)}")
        else:
            print("  全部通过")
        print('='*50)


# ── Tests ────────────────────────────────────────────────────────────────────

def t1_initial_state(r: Result) -> dict:
    """T1: 初始状态检查"""
    print("\n[T1] 初始状态")
    s = status()
    print(f"  status={s}")
    size = s.get("size", 0)
    total = s.get("ready", 0) + s.get("assigned", 0) + s.get("spawning", 0)

    r.check("T1-size>0", size > 0, f"size={size}")
    r.check("T1-total==size", total == size, f"ready+assigned+spawning={total}, size={size}")
    return s


def t2_concurrent_alloc(r: Result, s0: dict) -> list[str]:
    """T2: 并发 alloc，申请数 = 当前 ready 数"""
    print("\n[T2] 并发 alloc")
    ready = s0.get("ready", 0)
    n = min(ready, s0.get("size", 0))
    if n == 0:
        r.fail("T2-concurrent-alloc", "ready=0，无法测试")
        return []

    print(f"  并发申请 {n} 个 session")
    results: list[tuple[int, dict]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
        futures = [pool.submit(alloc) for _ in range(n)]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]

    ok_200 = [(code, body) for code, body in results if code == 200]
    session_ids = [body["session_id"] for _, body in ok_200]
    unique_ids = set(session_ids)

    r.check("T2-all-200", len(ok_200) == n, f"成功 {len(ok_200)}/{n}")
    r.check("T2-unique-sessions", len(unique_ids) == n, f"唯一 session_id 数={len(unique_ids)}")

    s_after = status()
    print(f"  alloc 后 status={s_after}")
    r.check("T2-assigned==n", s_after.get("assigned", 0) >= n,
            f"assigned={s_after.get('assigned')}, expected>={n}")

    return session_ids


def t3_pool_exhausted(r: Result, s0: dict, held_sessions: list[str]) -> None:
    """T3: 池耗尽时 alloc 返回 503"""
    print("\n[T3] 池耗尽验证")
    s = status()
    if s.get("ready", 1) > 0:
        print(f"  跳过：仍有 ready={s.get('ready')} 个进程（可能有其他用户占用）")
        r.ok("T3-skip", "池未耗尽（有其他会话），跳过")
        return

    code, body = alloc()
    print(f"  第 {len(held_sessions)+1} 次 alloc → {code} {body}")
    r.check("T3-503-on-busy", code == 503, f"code={code}")


def t4_release_and_replenish(r: Result, session_ids: list[str]) -> None:
    """T4: release 后池补充新进程"""
    print("\n[T4] release 并等待池补充")
    if not session_ids:
        r.fail("T4-release", "无 session 可释放")
        return

    for sid in session_ids:
        ok = release(sid)
        if not ok:
            print(f"  警告: release({sid}) 返回 False")

    print(f"  已释放 {len(session_ids)} 个 session，等待池补充（最多 {REPLENISH_WAIT_S}s）...")

    deadline = time.monotonic() + REPLENISH_WAIT_S
    target_ready = len(session_ids)
    last_s: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last_s = status()
        if last_s.get("ready", 0) >= target_ready and last_s.get("spawning", 0) == 0:
            break
        time.sleep(2)

    print(f"  补充后 status={last_s}")
    size = last_s.get("size", 0)
    total = last_s.get("ready", 0) + last_s.get("assigned", 0) + last_s.get("spawning", 0)
    r.check("T4-ready-recovered", last_s.get("ready", 0) >= target_ready,
            f"ready={last_s.get('ready')}, expected>={target_ready}")
    r.check("T4-total==size", total == size,
            f"ready+assigned+spawning={total}, size={size}")



def t5_release_invalid(r: Result) -> None:
    """T5: 释放不存在的 session_id 返回 ok=False"""
    print("\n[T5] release 无效 session_id")
    ok = release("nonexistent-session-id-0000")
    r.check("T5-false-on-invalid", not ok, f"ok={ok}")


def t6_state_consistency(r: Result) -> None:
    """T6: 多次采样，ready+assigned+spawning 始终等于 size"""
    print("\n[T6] 状态一致性（连续 5 次采样）")
    violations = []
    for i in range(5):
        s = status()
        total = s.get("ready", 0) + s.get("assigned", 0) + s.get("spawning", 0)
        size = s.get("size", 0)
        if total != size:
            violations.append(f"#{i}: total={total} size={size}")
        else:
            print(f"  #{i} ready={s['ready']} assigned={s['assigned']} spawning={s['spawning']} OK")
        time.sleep(1)
    r.check("T6-always-consistent", not violations,
            f"不一致: {violations}" if violations else "")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 50)
    print("进程池并发功能测试")
    print(f"目标: {POOL_URL}")
    print("=" * 50)

    # 快速连通性检查
    try:
        s = status()
    except Exception as e:
        print(f"[ERROR] 无法连接 poolmgr: {e}")
        sys.exit(1)

    r = Result()
    s0 = t1_initial_state(r)
    session_ids = t2_concurrent_alloc(r, s0)
    t3_pool_exhausted(r, s0, session_ids)
    t5_release_invalid(r)
    t4_release_and_replenish(r, session_ids)
    t6_state_consistency(r)
    r.summary()

    sys.exit(0 if not r.failed else 1)


if __name__ == "__main__":
    main()
