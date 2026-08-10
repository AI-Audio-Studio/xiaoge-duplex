# G3 云侧 V4 评审提交摘要

日期：2026-08-10  
源码提交：`0710254d11d0ee84b0ab09e46d644fd283752461`  
目标环境：`60.205.197.165:10097`（cont）

建议评审结论：`G3_CLOUD_V4_TECHNICAL_ACCEPTED_PENDING_RELEASE_SIGNOFF`。

## 本轮关闭结果

| 评审项 | 结论 | 关键证据 |
| --- | --- | --- |
| API Key 硬编码/泄漏 | 技术关闭 | Demo 改用户输入；新 Key 200，缺失/错误 Key 401；包扫描 0 命中 |
| 历史 access token | 技术关闭 | token 等待 610 秒后 Bearer 重连为 4401 |
| query token | 关闭 | 正式 query-only、Header+query 均为 4401；debug 路由 404 |
| Header Bearer | 关闭 | 正式建连和重新创建会话重连均 PASS |
| `ctrl.hello.token` | 关闭 | 修复 gateway close-code 传播后远端为 4400 |
| 命令主路径 | 关闭 | 单命令 dry-run、ack/running/succeeded、unknown、ask_split 均 PASS |
| caps 授权 | 关闭 | hello 不得扩大 session caps，远端 PASS |
| 远端回归 | PASS | 10097：126 passed；Ruff All checks passed |
| 日志脱敏 | PASS | 14 个运行日志文件，Key/query/Bearer/token 均 0 命中 |
| 真实机器人动作 | 保持关闭 | `ROBOT_ACTION_ENABLED=0` |

最终结构化运行态证据：

- `证据/签收/10097_SMOKE_SUMMARY_FINAL_20260810T1155Z.json`：12/12 PASS。
- `证据/签收/10097_V4_DEPLOYMENT_AND_ACCEPTANCE_20260810.md`：部署、回滚、哈希、
  126 tests、Ruff 和日志扫描。
- `证据/签收/10097_CREDENTIAL_ROTATION_DIAGNOSTIC_20260810.md`：Key 准入和 token TTL。
- `代码/SOURCE_SNAPSHOT_MANIFEST_20260810.md`：30 个部署相关文件快照。

## 部署状态

- gateway、poolmgr、2 个 agent 均运行，pool ready=2。
- V4 已部署到 10097；关键源码哈希与本包快照一致。
- 回滚包：`xiaoge-duplex-main-cont-g3v4-20260810T113114Z.tar.gz`，SHA-256 见部署回执。
- 内部测试入口使用证书 SHA-256 pin；生产放行前更换受信证书。

## 不扩大结论

以下属于 release/组织签字，不影响本轮云侧技术整改事实，但正式归档前仍需完成：

1. Security/deployment owner 在模板中签署旧 Key 作废和 token 失效回执。
2. Release owner 补 clean checkout 一次装配记录；远端全 extras 同步本轮超出 7 分钟采证上限。
3. 端侧、云侧、测试和安全 owner 完成联合签字。

本材料不授权合入 `main`、生产上线、灰度或真实机器人动作。

