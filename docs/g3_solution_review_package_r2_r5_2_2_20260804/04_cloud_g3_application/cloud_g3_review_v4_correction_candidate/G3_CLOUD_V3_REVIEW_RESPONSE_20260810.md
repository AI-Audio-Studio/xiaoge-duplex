# G3 云侧 V3 复审意见整改回应

日期：2026-08-10

| 评审项 | 本轮状态 | 整改与证据 |
| --- | --- | --- |
| P0-01 凭据事件 | 技术关闭、待 owner 签字 | 新 Key 在 10097 创建会话为 200，缺失/错误 Key 为 401；历史 token 610 秒后 Bearer 重连为 4401；Demo 默认 key 已移除 |
| P0-02 Bearer-only | 关闭 | 10097 最终 smoke 12/12 PASS：query-only/混合 4401，Header 成功，`ctrl.hello.token` 4400 |
| P1-01 源码追溯 | 技术关闭，待签字 | 已附 30 个实际部署文件完整快照及 SHA-256；整改提交为 `0710254d11d0ee84b0ab09e46d644fd283752461`，远端关键哈希一致 |
| P1-02 clean 复现 | 部分关闭 | 当前环境 `uv sync --all-extras --dev` 成功；尚不是 clean checkout |
| P1-03 WS 主路径 | 关闭 | 10097 覆盖单命令 dry-run、fake ack/running/succeeded、多命令 ask_split、Bearer-only 重连 |
| P1-04 cmd lifecycle | 云侧本地关闭 | issued/ack/running/succeeded、unknown、duplicate、late、delivery timeout、execution timeout 均有断言；端云真联调待共同验收 |
| P1-05 caps | 本地关闭 | 含 state 正例；hello 请求未授权 state 时 `granted_caps` 被裁剪且不下发 `ctrl.state` |
| P1-06 X3 独立正例 | 关闭 | 新增 `motion.turn`、`face.eyebrow`、`system.shutdown`、`power.charge` 四个独立 case |
| P2-01 包定位 | 关闭 | 包名明确为 `v4_correction_candidate`，不冒充 signoff |
| P2-02 可审源码 | 关闭 | `代码/source/` 含 `.py`、`.html` 和测试完整快照，附 manifest |

## 协议决策

正式产品面只有 `/ws/session`，鉴权唯一来源是 WebSocket Upgrade 的 Authorization
header。只要正式请求出现 `access_token` query，即使同时带正确 Authorization，也按
`4401 auth_failed` 拒绝，避免调用方继续依赖 URL token。

浏览器 `WebSocket` 构造器不能注入 Authorization header。为保留内部 Demo 可测性，
query token 被拆到默认关闭的 `/debug/ws/session`。该路由只有显式环境开关开启时才注册；
网关消费 token 后向上游注入 Authorization，且上游 URL 不含 query。生产、对外和 G3
签收环境必须保持两个 debug 开关为 `0` 或未设置。

## 安全闸口

本包不把“源码不再包含默认 key”等同于“泄漏事件已关闭”。进入签收前必须补齐：

1. API Key owner 的作废/轮换回执，回执中不得复写完整 key。
2. 部署 owner 对历史 access token 已失效的确认。
3. Release owner 确认整改提交的 reviewer 可读取位置；clean checkout 复跑并归档日志。
4. 重新执行仓库和发布物敏感信息扫描，并归档 0 命中摘要。

## 2026-08-10 现网补充检查

- 10097 新 Key：HTTP 200；缺失 Key、随机错误 Key：HTTP 401 `auth_failed`。
- token 指纹 `0a530c81e82f`：签发后等待 610 秒，正式 `/ws/session` Bearer 重连为 4401。
- 10097 已部署 V4，最终 smoke 12/12 PASS；远端 126 tests、Ruff 和日志脱敏扫描通过。
- 证书使用 owner 核验的 SHA-256 pin，禁止 `--insecure`；生产放行前仍须换成受信证书。
- 脱敏详情见 `证据/签收/10097_CREDENTIAL_ROTATION_DIAGNOSTIC_20260810.md`。
