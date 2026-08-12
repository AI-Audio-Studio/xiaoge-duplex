# SEC-02B 历史 Access Token 失效确认回执

日期：2026-08-11  
目标包：`cloud_g3_review_v4_correction_candidate`  
目标环境：`60.205.197.165:10097`

禁止记录完整 access token、Authorization 值或带 token 的 URL。

| 字段 | 内容 |
| --- | --- |
| 目标环境 | 10097 |
| 失效边界 | access token TTL 600 秒；V4 正式 `/ws/session` 仅接受 Header Bearer，query token 正式路径拒绝 |
| 最晚失效时间（UTC） | 2026-08-10T11:20:53Z 技术验证边界；owner 于 2026-08-11 确认历史 access token 已失效 |
| 验证 token 指纹 | `0a530c81e82f` |
| 正式 `/ws/session` 旧 token 结果 | `4401 auth_failed`，见 610 秒后 Bearer 重连验证 |
| query-only 结果 | `4401 auth_failed`，见最终 smoke `formal_query_only_rejected` |
| Authorization+query 结果 | `4401 auth_failed`，见最终 smoke `formal_header_plus_query_rejected` |
| access log 敏感值检查 | 14 个运行日志文件 0 命中，见 `10097_V4_DEPLOYMENT_AND_ACCEPTANCE_20260810.md` |
| Deployment/identity owner | allen.wangmh（本会话声明为 owner） |
| 结论 | PASS |
| 签字时间（UTC） | 2026-08-11 |

## Owner 声明

Deployment/identity owner 确认：整改前或测试过程中签发的历史 access token 已全部失效；当前正式产品端点 `/ws/session` 只接受 WebSocket Upgrade 的 `Authorization: Bearer`，不接受 query token，也不接受 Authorization+query 混合请求。

## 支撑证据

- `10097_CREDENTIAL_ROTATION_DIAGNOSTIC_20260810.md`：token 指纹 `0a530c81e82f`，签发后等待 610 秒，Bearer 重连返回 `4401`。
- `10097_SMOKE_SUMMARY_FINAL_20260810T1155Z.json`：query-only 与 Authorization+query 均返回 `4401`，Header Bearer 正例通过。
- `10097_V4_DEPLOYMENT_AND_ACCEPTANCE_20260810.md`：运行日志扫描 `query_access_token_hits=0`、`literal_bearer_hits=0`、`session_token_hits=0`。

## 边界

本回执关闭 10097/G3 V4 范围内历史 access token 生命周期确认，不代表其他未列环境的 token 审计，也不授权生产上线或真实机器人动作。
