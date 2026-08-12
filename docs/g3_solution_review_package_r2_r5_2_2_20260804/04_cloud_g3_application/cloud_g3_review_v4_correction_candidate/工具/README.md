# 签收采证工具

| 文件 | 用途 |
| --- | --- |
| `collect_clean_signoff_evidence.ps1` | 在 clean checkout 中执行依赖装配、72 项回归、Ruff、源码哈希、凭据脱敏扫描和生成产物检查 |
| `g3_cloud_signoff_smoke.py` | 对 HTTPS 10097 执行不打印凭据的 Bearer/WS/命令链路冒烟 |

工具自身已完成 PowerShell parser、Python bytecode compile、Ruff 和 `--help` 自检。工具只能在
生成正式 commit 后使用；当前工作区运行结果不能替代 clean checkout 或 10097 证据。

运行时 API Key 优先通过隐藏交互输入。若自动化必须使用 `G3_SMOKE_API_KEY`，该环境变量只能在
隔离进程中短时存在，运行后立即清除，且不得采集进程环境。

工具 SHA-256 在最终 commit 后必须重新计算并写入 release evidence；当前 working-tree hash
只用于发现意外改动，不作为不可变发布锚点。
