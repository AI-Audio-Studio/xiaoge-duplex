# SEC-01 API Key 安全事件关闭回执

日期：2026-08-11  
目标包：`cloud_g3_review_v4_correction_candidate`  
目标环境：`60.205.197.165:10097`

禁止记录完整 API Key、token、Authorization 值或带 token 的 URL。

| 字段 | 内容 |
| --- | --- |
| 事件/工单号 | G3-CLOUD-P0-01 |
| 涉及系统 | G3 云侧 Web Demo / Gateway create_session API |
| 旧 key 指纹 | owner 确认旧 API Key 已平台侧作废；本回执不复写旧 key 值，当前工作站无旧 key 可用于负向复测 |
| 作废时间（UTC） | 2026-08-11 前已完成，owner 于 2026-08-11 确认 |
| 新 key 生效时间（UTC） | 见 `10097_CREDENTIAL_ROTATION_DIAGNOSTIC_20260810.md` 新 key 创建会话 HTTP 200 |
| 验证方式 | owner 平台侧确认 + 10097 Web/API 准入验证 |
| 旧 key 调用验证 | owner 确认已作废；未为补证从历史材料恢复或传播旧 key |
| 缺失 key 调用验证 | HTTP 401，`auth_failed`，见 `10097_CREDENTIAL_ROTATION_DIAGNOSTIC_20260810.md` |
| 随机错误 key 调用验证 | HTTP 401，`auth_failed`，见 `10097_CREDENTIAL_ROTATION_DIAGNOSTIC_20260810.md` |
| 受影响日志排查范围 | 10097 本轮应用、poolmgr、agent `.run` 日志，14 个文件扫描 0 命中，见 `10097_V4_DEPLOYMENT_AND_ACCEPTANCE_20260810.md` |
| 后续密钥存储位置 | 受控部署/运行时凭据通道；不进入源码、评审材料、URL、浏览器存储或日志 |
| Security owner | allen.wangmh（本会话声明为 owner） |
| 结论 | PASS |
| 签字时间（UTC） | 2026-08-11 |

## Owner 声明

Security owner 确认：旧 API Key 已在平台侧作废，当前 G3 V4 Web Demo 不再携带默认 API Key，新凭据未进入源码、评审材料、日志、URL 或浏览器存储。为避免二次泄露，本回执不从历史材料恢复、粘贴或传播旧 API Key 明文。

## 支撑证据

- `10097_CREDENTIAL_ROTATION_DIAGNOSTIC_20260810.md`：新 Key HTTP 200，缺失/随机错误 Key 均 HTTP 401。
- `10097_V4_DEPLOYMENT_AND_ACCEPTANCE_20260810.md`：运行日志 14 文件扫描 `complete_api_key_hits=0`。
- `G3_CLOUD_V4_SANITIZATION_CHECK_20260810.md`：候选包脱敏扫描通过。

## 边界

本回执只关闭 G3 V4 候选包范围内的旧 API Key 生命周期确认，不授权生产上线或真实机器人动作。
