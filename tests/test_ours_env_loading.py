"""import 顺序守护测试(评审#1 / 复审 S2-1)。

锁定属性:入口模块加载完成后,`webpanel.state` / `common.runtime` / `app.setup_taps`
的模块级常量必须反映 **.env** 的值——即 `env_bootstrap`(load_dotenv)先于一切自有包
import 执行。若未来有人把自有包 import 挪到 bootstrap 之上,本测试失败。

方法:子进程 + 干净环境(剔除哨兵变量)+ `XIAOGE_DOTENV` 指向仅含哨兵值的临时 .env,
import web_ui_agent 后打印三处常量断言。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_AGENT_DIR = _REPO / "examples" / "voice_agents"
_PY = _REPO / ".venv" / "Scripts" / "python.exe"

_SENTINELS = {
    "WEB_AUDIO": "1",
    "WEB_UI_PORT": "9999",
    "TURN_METRICS_LOG": "",  # 运行时填充为临时路径
    "XIAOGE_ONLINE_VAD_GRACE": "9.9",
}

_PROBE = (
    "import sys; sys.path.insert(0, {agent_dir!r}); "
    "import web_ui_agent; "  # 入口 import(触发 env_bootstrap → 自有包链)
    "import webpanel.state as ws, common.runtime as cr, app.setup_taps as st; "
    "print(ws.WEB_AUDIO); print(ws.WEB_PORT); "
    "print(cr.TURN_METRICS_LOG); print(st.ONLINE_VAD_GRACE)"
)


def test_dotenv_loads_before_our_module_level_env_reads(tmp_path: Path) -> None:
    metrics_log = tmp_path / "sentinel_metrics.log"
    dotenv = tmp_path / ".env"
    values = dict(_SENTINELS, TURN_METRICS_LOG=str(metrics_log))
    dotenv.write_text("\n".join(f"{k}={v}" for k, v in values.items()) + "\n", encoding="utf-8")

    env = {k: v for k, v in os.environ.items() if k not in values}
    env["XIAOGE_DOTENV"] = str(dotenv)
    env["PYTHONUTF8"] = "1"

    python = str(_PY) if _PY.exists() else sys.executable
    proc = subprocess.run(
        [python, "-c", _PROBE.format(agent_dir=str(_AGENT_DIR))],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),  # 远离仓库根,确保只有 XIAOGE_DOTENV 指定的 .env 可见
        timeout=120,
    )
    assert proc.returncode == 0, f"probe failed:\n{proc.stderr[-2000:]}"
    web_audio, web_port, metrics, grace = proc.stdout.strip().splitlines()[-4:]
    assert web_audio == "True", f"WEB_AUDIO 未从 .env 生效: {web_audio}"
    assert web_port == "9999", f"WEB_UI_PORT 未从 .env 生效: {web_port}"
    assert str(metrics_log) in metrics, f"TURN_METRICS_LOG 未从 .env 生效: {metrics}"
    assert grace == "9.9", f"XIAOGE_ONLINE_VAD_GRACE 未从 .env 生效: {grace}"
