# 历史 Access Token 失效确认模板

禁止填写任何完整 token。

| 字段 | 待填写 |
| --- | --- |
| 目标环境 | `<10097/其他>` |
| 失效边界 | `<服务重启、签名密钥轮换、服务端 token store 清空或 TTL 边界>` |
| 最晚失效时间（UTC） | `<YYYY-MM-DDTHH:MM:SSZ>` |
| 验证 token 指纹 | `<sha256-prefix-12，禁止明文>` |
| 正式 `/ws/session` 旧 token 结果 | `<4401 auth_failed>` |
| query-only 结果 | `<4401 auth_failed>` |
| Authorization+query 结果 | `<4401 auth_failed>` |
| access log 敏感值检查 | `<0 命中，附日志文件>` |
| Deployment/identity owner | `<姓名/账号>` |
| 结论 | `<PASS/FAIL>` |
| 签字时间（UTC） | `<YYYY-MM-DDTHH:MM:SSZ>` |

Owner 声明：确认整改前签发的 access token 已全部失效，且正式产品端点仅接受 Header Bearer。

