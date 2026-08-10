# G3 端云共同签收记录模板

前置：`SIGNOFF_EVIDENCE_INDEX.md` 中全部 P0/P1 必须为 PASS。

| 签收方 | 责任人 | 结论 | 时间（UTC） | 备注/例外批准 |
| --- | --- | --- | --- | --- |
| 云侧 owner | `<填写>` | `<PASS/FAIL>` | `<填写>` | `<填写>` |
| 端侧 owner | `<填写>` | `<PASS/FAIL>` | `<填写>` | `<填写>` |
| 测试 owner | `<填写>` | `<PASS/FAIL>` | `<填写>` | `<填写>` |
| 安全 owner | `<填写>` | `<PASS/FAIL>` | `<填写>` | `<填写>` |

## 共同确认

- 正式 `/ws/session` 唯一 token 承载方式是 WebSocket Upgrade Authorization Bearer。
- `/debug/ws/session` 默认关闭且不属于产品协议、生产路径或签收范围。
- `data.cmd` 仍为 dry-run；本次签收不授权真实机器人动作。
- unknown/duplicate/late/timeout 不污染当前对话轮次。
- 本次签收只覆盖本记录所列 commit、环境与协议版本 R5.2.2。

最终结论：`<G3_CLOUD_SIGNOFF_APPROVED / G3_CLOUD_SIGNOFF_REJECTED>`

