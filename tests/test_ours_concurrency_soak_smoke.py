"""行为锁定测试:并发浸泡 harness 冒烟(§7 harness 的短时自检)。

短时(数秒)真栈 churn——证 harness 本体可跑通、泄漏检查生效、报告落盘;泄漏判据在正常栈下
应全 PASS(会话/上游/池槽末态归零、RSS/句柄增长受限)。真 4 路×2h 全量浸泡属目标机活动,不在
CI(时长 + 真 agent)。慢测(真子进程)。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

_AGENT_DIR = Path(__file__).resolve().parents[1] / "examples" / "voice_agents"
sys.path.insert(0, str(_AGENT_DIR))

from harness.soak import SoakConfig, run_soak  # noqa: E402


def test_soak_harness_smoke_no_leak() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        report = str(Path(tmp) / "soak.md")
        cfg = SoakConfig(
            sessions=2, duration_s=5.0, sample_interval_s=1.5, grace_s=0.3, report_path=report
        )
        result = asyncio.run(run_soak(cfg))
        assert result.ok, f"soak leak checks failed: {result.checks}"
        # 关键泄漏项末态归零 + 池复位
        assert result.checks["no_session_leak"] and result.checks["no_held_upstream_leak"]
        assert result.checks["pool_recovered"]
        assert Path(report).is_file()  # 报告已落盘
        assert len(result.samples) >= 2  # 至少基线 + 末态
