# G3 云侧 V4 本地验证记录

执行时间：2026-08-10 18:00-18:55 +08:00  
工作区基线 HEAD：`8415a102816fc437c902b4fbae9c11e563bcbf79`  
说明：工作区有未提交改动，因此本记录不是 clean checkout 证据。

## 依赖装配

```text
uv sync --all-extras --dev
Resolved 271 packages
Installed 12 packages
exit code: 0
```

## 扩展回归

```text
uv run pytest tests/test_ours_live_transcript.py \
  tests/test_ours_g2_r5_2_2_cloud_contract.py \
  tests/test_ours_g3_intent_command_rag.py \
  tests/test_ours_g3_ws_session_protocol.py \
  tests/test_ours_g3_x3_skill_commands.py \
  tests/test_ours_concurrency_m5_admin_routes.py \
  tests/test_ours_knowledge.py \
  tests/test_ours_music_player.py -q

126 passed in 2.59s
exit code: 0
```

## 本轮定向检查

```text
uv run ruff check <30 个快照中的 26 个 Python 文件>
All checks passed!
exit code: 0
```

定向协议/X3/admin 回归在拆分前为 `32 passed`，最终协议文件复跑为 `16 passed`。
封包校验确认 30 个 source snapshot 与工作区逐文件 SHA-256 一致；Demo 凭据默认值、
URL/localStorage API Key 持久化标记扫描为 0 命中。

## 覆盖的 V3 点名场景

- `/ws/session` Header Bearer 成功。
- query-only `4401`；Authorization+query `4401`；无 token `4401`。
- Header Bearer 断开后重新连接成功。
- `ctrl.hello.token` 为 `4400 protocol_error`。
- 独立 debug 路由默认 `404`，显式开启后 Demo query 连接成功。
- 网关上游收到 Authorization header，query string 为空。
- hello caps 越权被裁剪，未授权 state 不下发。
- 单命令 `data.cmd` dry-run，fake ack/running/succeeded。
- 多命令仅 `data.reply` ask_split，不出现 `cmd_id`。
- unknown、duplicate、late、delivery timeout、execution timeout 生命周期语义。
- 4 个 X3 action 独立正向 case。

## 尚缺的环境证据

- 独立 commit 的 clean checkout 复跑。
- 10097 部署同一 commit/snapshot 后的 pytest、ruff、页面和 WS 帧日志。
- 端侧真实或正式 fake executor 的联合签收记录。
