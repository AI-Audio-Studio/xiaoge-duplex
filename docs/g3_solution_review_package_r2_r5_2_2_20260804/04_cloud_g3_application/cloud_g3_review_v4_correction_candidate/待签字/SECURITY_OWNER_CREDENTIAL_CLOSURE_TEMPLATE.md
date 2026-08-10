# API Key 安全事件关闭回执模板

禁止填写完整 API Key、token、Authorization 值或带 token 的 URL。

| 字段 | 待填写 |
| --- | --- |
| 事件/工单号 | `<ticket-id>` |
| 涉及系统 | `<system>` |
| 旧 key 指纹 | `<sha256-prefix-12 或平台 key-id，禁止明文>` |
| 作废时间（UTC） | `<YYYY-MM-DDTHH:MM:SSZ>` |
| 新 key 生效时间（UTC） | `<YYYY-MM-DDTHH:MM:SSZ>` |
| 验证方式 | `<控制台状态/审计事件/API 验证，禁止粘贴 secret>` |
| 旧 key 调用验证 | `<已拒绝；HTTP/平台错误码>` |
| 受影响日志排查范围 | `<时间范围和系统范围>` |
| 后续密钥存储位置 | `<secret manager 名称和逻辑路径，不含值>` |
| Security owner | `<姓名/账号>` |
| 结论 | `<PASS/FAIL>` |
| 签字时间（UTC） | `<YYYY-MM-DDTHH:MM:SSZ>` |

Security owner 声明：旧 API Key 已不可用于任何生产、测试或 Demo 环境；新凭据未进入源码、
评审材料、日志、URL 或浏览器存储，且只通过受控凭据通道交付。此回执只证明凭据事件关闭，
不授权上线或真机动作。
