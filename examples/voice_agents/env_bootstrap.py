"""环境引导:import 本模块即加载 .env(评审#1 修复)。

必须是入口文件的**第一个自有 import**——`webpanel.state`/`common.runtime`/
`app.online_interrupt_host` 等模块在 import 期读取 `os.getenv`,若晚于
`load_dotenv` 执行,拿到的是进程环境缺省,.env 配置会静默失效
(`start.ps1` 预导出环境变量会掩盖该问题,直接 `python web_ui_agent.py console`
启动必现)。import 位置由 tests/test_ours_env_loading.py 守护。

测试钩子:设 `XIAOGE_DOTENV=<path>` 可显式指定 .env 路径;默认(未设)与原行为
一致——python-dotenv 从调用方目录向上搜索仓库根的 .env。
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv(dotenv_path=os.getenv("XIAOGE_DOTENV") or None, override=True)


def ensure_loaded() -> None:
    """no-op:供入口在 import 块之间显式调用,锚定本模块的 import 顺序
    (非 import 语句会分隔 isort 排序块,防止格式化工具把本模块重排到自有包之后)。"""
