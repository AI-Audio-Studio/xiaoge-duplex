# G3 云侧 V4 鉴权与 Demo 边界

## API Key

- `index.html` 只提供 `type=password` 的用户输入框。
- API Key 仅用于当前 `/create_session` 请求的 `X-API-Key` header。
- 页面不提供默认值，不从 URL 参数读取，不写入 `localStorage`。
- 本轮未在材料中复写任何完整 key/token。

## Access Token

| 路径 | 默认注册 | token 来源 | 是否属于 G3 产品协议 |
| --- | --- | --- | --- |
| `/ws/session` | 是 | `Authorization: Bearer` | 是 |
| `/debug/ws/session` | 否 | query，由 BFF 消费后改写为上游 Authorization | 否，仅内部 Demo |

正式路径出现 `access_token` query 时强制拒绝，包括“正确 Authorization + query”组合。
`ws_url` 仍为不带 token 的 `/ws/session`。
Gateway access logger 只记录 `request.path`，不记录 query string；直连 WebPanel 的
aiohttp access log 保持关闭。测试使用哨兵 token 断言日志中不出现参数名或参数值。

## Debug 开关

- Gateway：`XG_WEBPANEL_DEBUG_QUERY_TOKEN`，默认 `false`。
- Direct WebPanel：`XIAOGE_WEBPANEL_DEBUG_QUERY_TOKEN`，默认 `false`。
- 生产、灰度、G3 签收和任何对外环境必须保持关闭。
- 调试环境的 access log 也不应记录 query；本端点只用于短期内部 Demo，测试完成后关闭。

## 未关闭的凭据事件

删除硬编码只阻止继续分发，不能让已经暴露的 key 自动失效。必须由 owner 完成轮换/作废，
并以不含完整凭据的回执关闭 P0-01。
