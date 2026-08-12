# G3 V4 Web-only Candidate Owner 签收记录

日期：2026-08-11  
目标包：`cloud_g3_review_v4_correction_candidate`  
目标环境：`60.205.197.165:10097`  
源码锚点：`0710254d11d0ee84b0ab09e46d644fd283752461`

## 0. 签收结论

Owner 确认当前材料可作为 `G3_CLOUD_WEB_ONLY_CANDIDATE_APPROVED`：覆盖 G3 云侧技术整改候选包、Web 端测试条件、10097 smoke、测试阶段端云联合签收、fake executor 协议行为、安全回执和源码可追溯确认。

本签收不声明最终 `SIGNOFF`，因为 `REP-01 clean checkout` 仍未完成；不授权生产上线、灰度发布或真实机器人动作。

当前处于测试阶段，本次申请只覆盖 G3 云侧 Web-only 技术整改候选包评审：不申请正式生产上线，
不申请真实端侧联调完成，不申请生产 TLS 验收。

## 1. Owner 签收表

| 签收方 | 责任人 | 结论 | 时间（UTC） | 备注/例外批准 |
| --- | --- | --- | --- | --- |
| 云侧 owner | allen.wangmh | PASS（Web-only/G3 candidate） | 2026-08-11 | 10097 smoke 12/12 PASS，远端 126 tests 与 Ruff PASS |
| Release owner | allen.wangmh | PASS（source traceability only） | 2026-08-11 | commit/source snapshot/manifest 可追溯；clean checkout 单独 pending |
| Security owner | allen.wangmh | PASS | 2026-08-11 | 旧 API Key 平台侧作废，历史 token 已失效 |
| Test owner | allen.wangmh | PASS（测试阶段端云联合签收） | 2026-08-11 | 当前 G3 测试阶段端云协议闭环已签收；真实动作不放行 |
| 端侧 owner | allen.wangmh | PASS（测试阶段端云协议签收） | 2026-08-11 | 见 `JOINT-01_END_CLOUD_SIGNOFF_10097_20260811.md` |

## 2. 共同确认

- 正式 `/ws/session` 唯一 token 承载方式是 WebSocket Upgrade `Authorization: Bearer`。
- `/debug/ws/session` 默认关闭且不属于产品协议、生产路径或签收范围。
- `data.cmd` 当前按 Web 端 + fake executor 论证云侧协议行为。
- `ROBOT_ACTION_ENABLED=0` 保持关闭，本次签收不授权真实机器人动作。
- Web-only 测试不等同于真实机器人端侧联合验收。
- 生产 TLS 可信 CA/hostname 校验不在当前 G3 pin 签收范围。
- `REP-01 clean checkout` 未完成前，不复制为 `cloud_g3_review_v4_signoff_candidate`。

最终结论：`G3_CLOUD_WEB_ONLY_CANDIDATE_APPROVED`
