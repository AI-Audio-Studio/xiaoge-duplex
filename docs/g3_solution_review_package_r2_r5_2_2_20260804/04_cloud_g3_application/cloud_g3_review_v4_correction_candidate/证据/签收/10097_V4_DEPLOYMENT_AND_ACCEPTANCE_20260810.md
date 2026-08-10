# 10097 V4 部署与验收回执

目标环境：`60.205.197.165:10097`（cont）  
部署目录：`/data/home/allen.wangmh/software/xiaoge/xiaoge-duplex-main-cont`  
最终验收时间：2026-08-10T11:55:17Z

本回执不记录完整 API Key、access token、Authorization 值或带 token 的 URL。

## 部署与回滚

- 部署文件：30 个，来源为本包 `代码/source/`。
- 上传方式：临时文件上传、SHA-256 校验、原子替换；初次替换后 30/30 哈希一致。
- Linux 启动脚本：`xg.sh` 归一化为 LF 并恢复 `0755`。
- 回滚包：`/data/home/allen.wangmh/software/xiaoge/backups/xiaoge-duplex-main-cont-g3v4-20260810T113114Z.tar.gz`
- 回滚包 SHA-256：`bc42ca0b97a6b309f1126315771ea5e353ad348f6656c9258f89eeb1ddf5aa36`

最终关键源码哈希：

| 文件 | 10097 SHA-256 |
| --- | --- |
| `gateway/main.py` | `b1fa15e527c14a3145eaaf24bee80a6dd16a6814b4c24204e230d1322fc1d5b0` |
| `gateway/proxy.py` | `26a08b5deef0be8abe6df61b7cff7cf689bb1ea23b75f26d8832d8658ccb3bbc` |
| `webpanel/server.py` | `c383edd3b8287f3497ef0f4bfb8b74d305a977ad11b08afdf64f9c06831f5e84` |
| `webpanel/static/index.html` | `f08f958cae062d6b01fa0d2966da3286d0681eb6fe800aa179089c104135016f` |
| `test_ours_g3_ws_session_protocol.py` | `95131fe7a81d4bdca15832ed72a0042bb1011d26e2b2693e043def81dce7b09b` |
| `uv.lock` | `bfba9a46a771882fc14f0e365c9272279b795114c0d5f6c0c1f4de7d8bf73da6` |

## 安全配置和运行状态

| 检查 | 结果 |
| --- | --- |
| `XG_API_KEY_REQUIRED` | `1` |
| `XG_WEBPANEL_DEBUG_QUERY_TOKEN` | `0` |
| `XIAOGE_WEBPANEL_DEBUG_QUERY_TOKEN` | `0` |
| `ROBOT_ACTION_ENABLED` | `0` |
| pool | ready=2, assigned=0, spawning=0 |
| gateway | running，PID 354522 |
| poolmgr | running，PID 354178 |
| agent | 2 个，端口段 19100-19199 |

## 正式运行态 Smoke

最终文件：`10097_SMOKE_SUMMARY_FINAL_20260810T1155Z.json`，总体 `PASS`，12/12 检查通过：

- Demo 必须由用户输入 Key，源码默认 Key 不存在。
- debug query 路由为 404。
- query-only、Authorization+query 均为 4401。
- Header Bearer、重连、caps 正例通过。
- 单命令 `data.cmd` dry-run、fake ack/running/succeeded 通过。
- unknown `cmd_id` 返回 `unknown_cmd_id`。
- 多命令只返回 reply，不产生 `cmd_id`。
- `ctrl.hello.token` 为 4400。
- hello 不得扩大 caps。

首次两轮运行暴露 gateway 未传播上游 4400、外部看到 1006；对应失败文件保留为
`10097_SMOKE_PREFIX_FAIL_20260810T1133Z.json` 和
`10097_SMOKE_POSTFIX1_FAIL_20260810T1136Z.json`。修复为“停止并发 reader 后统一传播 close code”，
新增 gateway 端到端回归后最终通过，未删除失败轨迹。

## 远端回归和日志

2026-08-10T11:54:43Z：

```text
126 passed in 1.80s
All checks passed!
```

运行日志扫描 14 个文件：

```text
complete_api_key_hits=0
query_access_token_hits=0
literal_bearer_hits=0
session_token_hits=0
```

10097 由 Python gateway 直接监听 TLS 端口，本次环境没有单独的前置反向代理日志；应用、
poolmgr 和 agent 的 `.run` 日志均纳入上述扫描。

## 可复现性边界

远端 `.venv` 使用 `uv.lock` 对应依赖，功能回归和 Ruff 均通过。非交互执行
`uv sync --all-extras --dev` 在 7 分钟采证上限内未完成，验证进程已精确终止，未影响运行服务。
因此本文件证明“实际部署快照运行通过”，不冒充 clean checkout 一次装配成功；不可变 commit
与 clean checkout 仍由 `SRC-02`、`REP-01` 单独关闭。

