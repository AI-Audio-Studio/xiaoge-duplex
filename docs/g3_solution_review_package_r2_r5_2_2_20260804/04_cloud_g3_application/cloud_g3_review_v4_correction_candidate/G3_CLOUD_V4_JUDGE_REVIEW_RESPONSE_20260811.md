# G3 云侧 V4 评委复审意见响应与补证计划

日期：2026-08-11  
对应评委意见：`docs/REVIEW_JUDGE_OPINION_20260811.md`  
对象包：`cloud_g3_review_v4_correction_candidate`

## 0. 响应结论

接受评委组结论：本包只能作为“技术整改候选包/进入 owner 签收与端云联合验收”的材料，不声明最终 `SIGNOFF`。

2026-08-11 owner 回填后，`SEC-01`、`SEC-02B`、`SRC-02` 和测试阶段端云联合范围的 `JOINT-01` 已形成 owner 回执；`REP-01 clean checkout` 仍为 `PENDING`，因此本包仍不能复制为最终 `cloud_g3_review_v4_signoff_candidate`。

当前处于测试阶段，本次申请只覆盖 G3 云侧 Web-only 技术整改候选包评审：不申请正式生产上线，
不申请真实端侧联调完成，不申请生产 TLS 验收。

本文件保留评委复审后的补证计划与边界；已完成项以 `SIGNOFF_EVIDENCE_INDEX.md` 和 `证据/签收/` 下实际回执为准。

本次补充覆盖以下内容：

1. 将评委提出的 5 个未关闭事项映射到本包证据 ID、责任人、模板和完成判据。
2. 明确 TLS pin 验收只覆盖 G3 内部 10097 环境，不等同于生产可信 CA 证书链。
3. 明确 fake executor / dry-run 的联合验收边界，不冒充真实端侧动作闭环。
4. 明确 clean checkout 和 release commit 锚点的最小采证要求。

## 1. 评委未关闭项逐项响应

| 评委问题 | 本包现状 | 本次补充动作 | 最终关闭条件 |
| --- | --- | --- | --- |
| P0-01 旧 API Key/历史 token 缺 owner 回执 | `SEC-01`、`SEC-02B` 已回填 owner 回执并更新为 `PASS` | 已新增 `SEC-01_API_KEY_CLOSURE_OWNER_DECLARATION_20260811.md` 与 `SEC-02B_TOKEN_INVALIDATION_OWNER_DECLARATION_20260811.md` | 已按 owner 回执关闭；不记录明文凭据 |
| REP-01 clean checkout 未完成 | `REP-01` 仍为 `PENDING` | 保持 `PENDING`；按 `待执行/CLEAN_CHECKOUT_REPRO_RUNBOOK.md` 执行并归档 evidence | 干净 clone 到不可变 commit，`uv sync`、pytest、Ruff、sanitization、source sha256 全部 PASS |
| 源码可追溯需 release owner 确认 | `SRC-01` PASS；`SRC-02` 已回填 release owner 回执并更新为 `PASS` | 已新增 `SRC-02_RELEASE_OWNER_SOURCE_CONFIRMATION_0710254d11d0_20260811.md` | 正式 commit/source snapshot/manifest 可被评审员读取；clean checkout 仍由 `REP-01` 单独关闭 |
| 端云联合验收未关闭 | `JOINT-01` 已按测试阶段端云联合范围回填 owner 签收 | 已新增 `JOINT-01_WEB_ONLY_SCOPE_OWNER_DECLARATION_20260811.md`、`JOINT-01_END_CLOUD_SIGNOFF_10097_20260811.md` 与 `OWNER_WEB_ONLY_G3_CANDIDATE_SIGNOFF_20260811.md` | 当前关闭测试阶段端云联合签收；真实动作放行、生产上线和生产 TLS 不在本次范围 |
| TLS 证据为 pin 验收 | 10097 smoke 使用 certificate SHA-256 pin | 补充 G3/生产 TLS 边界见第 5 节 | G3 内部签收需 owner 接受 pin；生产发布需生产域名、可信 CA 链和 hostname 校验证据 |

## 2. 安全 owner 回执补证要求

### SEC-01 API Key 泄露生命周期

必须回填到新文件，建议路径：

`证据/签收/SEC-01_API_KEY_CLOSURE_<ticket>_<utc>.md`

基于模板：`待签字/SECURITY_OWNER_CREDENTIAL_CLOSURE_TEMPLATE.md`。

最小字段：

- 事件/工单号。
- 旧 key 指纹或平台 key-id，禁止明文 key。
- 作废时间、新 key 生效时间。
- 控制台审计/API 验证方式。
- 旧 key 调用验证结果。
- 受影响日志排查范围。
- 后续 secret manager 逻辑路径，不含 secret 值。
- Security owner 结论与签字时间。

已完成更新：

- `SIGNOFF_EVIDENCE_INDEX.md`：`SEC-01` 已为 `PASS` 并指向实际回执。
- `00_FORMAL_SIGNOFF_GATE_STATUS.md`：P0-01 API Key 泄漏事件已为 `PASS`。

### SEC-02B 历史 access token owner 回执

必须回填到新文件，建议路径：

`证据/签收/SEC-02B_TOKEN_INVALIDATION_<env>_<utc>.md`

基于模板：`待签字/DEPLOYMENT_OWNER_TOKEN_INVALIDATION_TEMPLATE.md`。

最小字段：

- 目标环境和失效边界。
- 最晚失效时间。
- token 指纹 `sha256-prefix-12`，禁止明文 token。
- 正式 `/ws/session` 旧 token 结果：`4401 auth_failed`。
- query-only 结果：`4401 auth_failed`。
- Authorization+query 结果：`4401 auth_failed`。
- access log 敏感值扫描：0 命中，附日志文件名。
- Identity/deployment owner 结论与签字时间。

已完成更新：

- `SIGNOFF_EVIDENCE_INDEX.md`：`SEC-02B` 已为 `PASS`。
- `00_FORMAL_SIGNOFF_GATE_STATUS.md`：P0-01 历史 token 已为 `PASS`。

## 3. Release owner 与 clean checkout 补证要求

### SRC-02 release owner 源码锚点

必须回填到新文件，建议路径：

`证据/签收/SRC-02_RELEASE_SOURCE_CONFIRMATION_<commit12>_<utc>.md`

基于模板：`待签字/RELEASE_OWNER_SOURCE_CONFIRMATION_TEMPLATE.md`。

必须确认：

- 分支、40 位 commit、提交时间、PR/评审入口。
- clean checkout 证据目录。
- source manifest SHA-256。
- 10097 部署 commit 与 manifest SHA-256。
- 工作区状态为 clean。
- 若形成新 commit 或 tag，`代码/SOURCE_SNAPSHOT_MANIFEST_20260810.md` 不得继续作为最终不可变发布锚点，必须从最终 commit 重新生成 manifest 或提供逐项一致性说明。

### REP-01 clean checkout

必须按 `待执行/CLEAN_CHECKOUT_REPRO_RUNBOOK.md` 执行，建议归档路径：

`证据/签收/clean_<commit12>_<YYYYMMDDTHHMMSSZ>/`

最小证据文件：

- `00_git_status_before.txt`：为空。
- `01_commit.txt`：40 位 commit 与 release 模板一致。
- `10_uv_sync.log`：exit code 0。
- `20_pytest.log`：exit code 0，测试数量与本包口径一致或说明差异原因。
- `30_ruff.log`：exit code 0。
- `40_source_sha256.json`：与候选包 source manifest 可解释地一致。
- `50_sanitization.json`：所有计数为 0。
- evidence 目录 SHA-256 manifest。

若 `uv sync --all-extras --dev` 在 clean 环境仍因网络、平台或依赖源超时失败，不能标记 `PASS`；只能由 Release owner 明确签署 `N/A（附批准人）` 或例外接受边界，并写明不影响 G3 内部验收的理由。

## 4. 端云联合验收补证要求

`JOINT-01` 关闭前不得把云侧 fake executor smoke 等同于真实端云闭环。

建议新增实际证据路径：

`证据/签收/JOINT-01_END_CLOUD_SIGNOFF_<env>_<utc>.md`

基于模板：`待签字/END_CLOUD_JOINT_SIGNOFF_TEMPLATE.md`，并至少附以下事件序列之一。

### 4.1 真实端侧见证路径

同一 `session_id` / `cmd_id` 下，脱敏记录以下事件：

1. 云侧发出 `data.cmd`，包含 `cmd_id`、`action`、`args`、`dry_run=true` 或真实动作 gate 关闭证明。
2. 端侧收到该 `cmd_id`。
3. 端侧返回 `ack`。
4. 端侧返回 `running`。
5. 端侧返回 `succeeded` / `result`。
6. 云侧日志确认该 `cmd_id` lifecycle closed。
7. unknown/duplicate/late/timeout case 不污染当前会话。

### 4.2 fake executor 联合见证路径

如果 G3 阶段只能使用 fake executor，必须由端侧 owner、测试 owner 和 release owner 在联合签收中明确接受，并写明：

- fake executor 运行位置、版本、commit。
- fake executor 是否模拟端侧协议栈，而非仅云侧单元测试。
- 覆盖的 action 列表，至少包含评审要求的 X3 核心 action。
- 真实机器人动作 gate 保持关闭，`ROBOT_ACTION_ENABLED=0` 或等价证明。
- 该结论只覆盖 G3 dry-run/协议验收，不授权生产动作。

## 5. TLS 验收边界

### G3 内部 10097 环境

当前 `10097_SMOKE_SUMMARY_FINAL_20260810T1155Z.json` 使用证书 SHA-256 pin：

`460e09d5d59b91df...`

该证据只说明：客户端没有使用 `--insecure` 或关闭校验，而是对 10097 当前证书做 pin 校验。它可作为 G3 内部环境验收方式，前提是 Deployment/security owner 在最终签收中确认该 pin 属于受控验收边界。

### 生产发布环境

若目标变为生产发布，必须新增生产 TLS 证据，建议路径：

`证据/签收/TLS-02_PRODUCTION_CERT_CHAIN_<domain>_<utc>.md`

最小内容：

- 生产入口域名和端口。
- 证书 Subject / SAN 覆盖域名。
- 完整证书链 issuer。
- 系统 CA + hostname 校验通过的客户端命令或工具输出。
- 证书有效期。
- Deployment/security owner 结论。

未提供生产 TLS 证据时，`TLS-01` 只能保留为“G3 内部 pin PASS”，不能作为生产放行证书结论。

## 6. 更新文件检查表

每份实际回执落盘后，必须同步更新：

1. `SIGNOFF_EVIDENCE_INDEX.md`：状态、实际文件路径、责任人。
2. `00_FORMAL_SIGNOFF_GATE_STATUS.md`：对应闸口状态和证据路径。
3. `README.md`：索引新增实际证据入口。
4. `01_OWNER_ACTION_QUEUE.md`：对应顺序项完成判据。
5. 若最终生成 `cloud_g3_review_v4_signoff_candidate`，必须重新生成整包 manifest，并保留本 `correction_candidate` 不覆盖。

## 7. 当前仍保持 PENDING 的证据 ID

截至本响应文件最新回填时，以下事项仍不能由云侧文档自行关闭：

- `REP-01`：clean checkout 复现 evidence。必须在干净 checkout 的不可变 commit 上完成 `uv sync`、pytest、Ruff、sanitization、source sha256 和 evidence manifest 后才能关闭。

以下事项已按当前 G3 测试阶段 candidate 边界形成 owner 回执，不再列为当前 PENDING：

- `SEC-01`：旧 API Key 平台侧失效/轮换 owner 回执已回填。
- `SEC-02B`：历史 access token owner 回执已回填。
- `SRC-02`：release owner 源码可追溯确认已回填；clean checkout 仍由 `REP-01` 单独关闭。
- `JOINT-01`：测试阶段端云联合签收已回填；不扩大为真实动作放行、生产上线或生产 TLS。
- `TLS-02`：仅在生产发布目标下需要；当前 G3 内部验收保留 owner 接受的 TLS pin 边界。
