# G3 云侧 V4 整改候选包

日期：2026-08-10

本包回应 `docs/REVIEW_GROUP_G3_CLOUD_V3_REVIEW_20260810.md`。定位为
`correction_candidate`，不是签收包，也不是上线、灰度、合入 `main` 或真实机器人动作放行依据。

## 本轮已落实

- Demo 页面不再硬编码 API Key；用户在密码输入框中输入，页面不从 URL 或
  `localStorage` 读取或持久化 API Key。
- 正式 `/ws/session` 只接受 `Authorization: Bearer <access_token>`；query-only 以及
  Authorization+query 均关闭为 `4401`。
- 浏览器原生 WebSocket 无法设置自定义 Authorization header，因此 Demo query token
  仅位于独立 `/debug/ws/session`，且默认不注册。网关开关为
  `XG_WEBPANEL_DEBUG_QUERY_TOKEN=1`，直连 WebPanel 开关为
  `XIAOGE_WEBPANEL_DEBUG_QUERY_TOKEN=1`。该端点不属于 G3 产品协议和签收范围。
- 调试 BFF 在收到 query token 后，向内部 `/ws/session` 改写为 Authorization header，
  不把 query string 转发到 agent。
- `ctrl.hello.token` 拒绝；hello caps 不得扩大 `create_session.granted_caps`，未授权的
  `state` 不下发。
- 增加 `cmd_id` 生命周期跟踪：unknown 返回 `data.error.unknown_cmd_id`，duplicate/late
  只审计，ack/result 分别产生 delivery/execution timeout。
- 增加等效 WS 主路径、Bearer-only 重连和 4 个 X3 action 独立正向测试。
- 包内 `代码/source/` 为 30 个实际部署源码、测试、数据与锁文件的完整快照，哈希见 manifest。
- V4 已部署到 10097；最终远端 smoke 12/12 PASS，远端 126 tests 与 Ruff PASS，运行日志
  敏感值扫描 0 命中。

## 仍未关闭

- `P0-01`：新 Key 已在 10097 返回 HTTP 200，缺失/随机错误 Key 均返回 401；旧 Key
  已删除的最终关闭仍需 Security owner 签署平台作废回执。历史 token 已验证在 610 秒后
  Bearer 重连返回 4401，技术结果 PASS，仍需部署/身份 owner 签字。
- `P1-01`：整改源码已形成独立提交
  `0710254d11d0ee84b0ab09e46d644fd283752461`（分支
  `g3-cloud-v4-signoff-20260810`，提交时间 2026-08-10 20:01:40 +08:00）；
  已附 30 个文件的 source snapshot 与 SHA-256 manifest，待 Release owner 确认可读取位置并签字。
- `P1-02`：当前工作区执行 `uv sync --all-extras --dev` 成功，但不是 clean checkout，
  仍需在可定位 commit 上做一次干净复现。
- 10097 已部署本快照并使用精确证书 SHA-256 pin 采证；生产放行前仍须换成受信证书。
- 远端 `uv sync --all-extras --dev` 在 7 分钟采证上限内未完成；这不影响当前运行态 PASS，
  但 clean checkout 一次装配仍需在不可变 commit 上关闭。

## 索引

- `00_G3_CLOUD_REVIEW_SUBMISSION_SUMMARY_20260810.md`：一页式提交结论和证据入口。
- `00_FORMAL_SIGNOFF_GATE_STATUS.md`：正式签收闸口和判定规则。
- `01_OWNER_ACTION_QUEUE.md`：从安全回执到四方签收的有序责任队列。
- `SIGNOFF_EVIDENCE_INDEX.md`：证据 ID、责任人和回填位置。
- `G3_CLOUD_V3_REVIEW_RESPONSE_20260810.md`：逐项回应和状态。
- `测试/G3_CLOUD_V4_LOCAL_VERIFICATION_20260810.md`：本地命令与结果。
- `测试/G3_CLOUD_SIGNOFF_TOOL_SELF_CHECK_20260810.md`：签收采证工具自检结果。
- `证据/G3_CLOUD_V4_SECURITY_BOUNDARY_20260810.md`：API Key、Bearer 和调试边界。
- `证据/G3_CLOUD_V4_SANITIZATION_CHECK_20260810.md`：候选包脱敏扫描摘要。
- `证据/签收/10097_CREDENTIAL_ROTATION_DIAGNOSTIC_20260810.md`：新 Key 准入、远端版本和 TLS 诊断。
- `证据/签收/10097_V4_DEPLOYMENT_AND_ACCEPTANCE_20260810.md`：部署、回滚、远端回归和日志扫描。
- `证据/签收/10097_SMOKE_SUMMARY_FINAL_20260810T1155Z.json`：最终 12/12 PASS 运行态证据。
- `代码/SOURCE_SNAPSHOT_MANIFEST_20260810.md`：源码快照清单和 SHA-256。
- `待签字/`：安全、部署、release 和端云共同签收模板。
- `待执行/`：clean checkout 与 10097 采证操作单。
- `待执行/SOURCE_AND_EVIDENCE_COMMIT_RUNBOOK.md`：实现 commit 与证据 commit 的发布模型。
- `待执行/FINAL_PACKAGE_PROMOTION_CHECKLIST.md`：全绿后生成正式 signoff candidate 的规则。
- `工具/`：clean checkout 采证脚本和脱敏远端冒烟工具。
