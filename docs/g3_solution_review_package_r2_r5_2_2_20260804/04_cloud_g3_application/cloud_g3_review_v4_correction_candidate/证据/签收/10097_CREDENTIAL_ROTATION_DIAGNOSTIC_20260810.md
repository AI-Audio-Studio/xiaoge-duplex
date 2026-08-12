# 10097 API Key 轮换与部署状态诊断

> 本文件是部署前诊断快照。V4 已于同日部署，最终状态以
> `10097_V4_DEPLOYMENT_AND_ACCEPTANCE_20260810.md` 和
> `10097_SMOKE_SUMMARY_FINAL_20260810T1155Z.json` 为准。

采证时间：2026-08-10T11:17:42Z  
目标：`https://60.205.197.165:10097`  
执行位置：开发工作站外网直连 10097  

本文件只记录凭据 SHA-256 前缀、HTTP 状态和页面版本标志，不记录完整 API Key、
access token、Authorization 值或带 token 的 URL。

## API Key 准入结果

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 新 Key 指纹 | `8b875d338dbf` | SHA-256 前 12 位 |
| 新 Key 创建会话 | PASS | `POST /create_session` 返回 HTTP 200 |
| 缺失 Key 创建会话 | PASS | HTTP 401，`auth_failed` |
| 随机错误 Key 创建会话 | PASS | HTTP 401，`auth_failed` |
| 旧 Key 服务端删除 | OWNER DECLARED | 操作方声明已删除；完整旧值已从可分发材料清除，当前工作站无法复写旧值做负向调用 |

上述结果证明 10097 已启用强制 API Key 校验且新 Key 生效。采证当时旧 Key 平台侧作废仍待
Security owner 回执；2026-08-11 已由 `SEC-01_API_KEY_CLOSURE_OWNER_DECLARATION_20260811.md`
补充关闭。不得为补测试而从历史日志或材料重新传播完整旧值。

## 历史 Access Token

10097 当前实现签发的 access token TTL 为 600 秒。本次检查只在测试进程内保留 token，
等待 610 秒后以 Bearer 重连；token 未写入磁盘。

| 字段 | 结果 |
| --- | --- |
| token 指纹 | `0a530c81e82f` |
| 签发时间 | `2026-08-10T11:10:43.221761Z` |
| 等待时间 | 610 秒 |
| 验证时间 | `2026-08-10T11:20:53.309296Z` |
| 正式 `/ws/session` Bearer 结果 | `4401` |
| 技术结论 | `PASS` |

旧 Key 已在该测试前由操作方声明删除，10097 同时验证缺失/错误 Key 均为 401；因此在
`2026-08-10T11:20:53Z` 这个边界，删除前签发且未超过 600 秒的历史 token 也已全部跨过
TTL。该结论关闭 token 重放的应用层技术疑问；2026-08-11 已由
`SEC-02B_TOKEN_INVALIDATION_OWNER_DECLARATION_20260811.md` 补充 deployment/identity owner 回执。

本次没有在旧快照上继续发送携带有效 token 的 query 请求，以避免把 token 写入旧版 access
log。query-only、Authorization+query 和 access-log 0 命中必须在部署 V4 后由正式 smoke
共同验证，不能用本次 TTL 结果替代 P0-02。

## 远端部署版本检查

对 10097 根页面执行只读检查：

| V4 页面标志 | 现网结果 |
| --- | --- |
| `apiKeyInput` 存在 | false |
| `DEMO_QUERY_TOKEN_ENABLED=false` | false |
| `DEFAULT_RUOYI_API_KEY` 已移除 | false |
| 不使用 `localStorage` | false |

结论：10097 仍运行 V4 整改前快照。当前不能执行或宣称远端 Bearer-only 签收通过；必须先
部署本包 source snapshot 对应版本、重启 10097 gateway/poolmgr/agents，再执行正式 smoke。

## TLS 诊断

10097 当前证书：

- SHA-256：`460e09d5d59b91df0e2eb6fe2d47d28db1229cdf561b3e2e2623ae8a0ac6fabf`
- Subject/Issuer：`CN=60.205.197.165`（自签名）
- Subject Alternative Name：缺失
- 到期时间：2027-05-28T02:56:13Z

系统默认 TLS 校验失败；即使把该自签证书作为信任锚，现代客户端仍因 IP SAN 缺失拒绝。
本文件中的 API 应用层检查使用诊断性 `ssl=False`，不得作为生产 TLS PASS 证据。后续正式
smoke 已使用证书 SHA-256 pin 验收 10097 G3 内部测试入口；若目标变为生产发布，仍必须换用
受信 CA 签发且 SAN 覆盖生产入口域名的证书，或通过受信域名入口执行 smoke。
