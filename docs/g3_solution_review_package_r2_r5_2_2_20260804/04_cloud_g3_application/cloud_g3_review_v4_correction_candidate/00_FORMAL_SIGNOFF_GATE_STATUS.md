# G3 云侧正式签收闸口状态

更新日期：2026-08-11

结论：`READY_FOR_RELEASE_TRACEABILITY_AND_OWNER_SIGNOFF`。V4 已部署到 10097，远端回归、
Bearer-only、命令主路径、关闭码和日志扫描均通过。当前
不能声明 `SIGNOFF`；下表所有“待回填”项完成并经评审核验后，才能复制为
`cloud_g3_review_v4_signoff_candidate`。

当前处于测试阶段，本次申请只覆盖 G3 云侧 Web-only 技术整改候选包评审：不申请正式生产上线，
不申请真实端侧联调完成，不申请生产 TLS 验收。

| 闸口 | 当前判断 | 已有证据 | 正式关闭还需要 |
| --- | --- | --- | --- |
| P0-01 API Key 泄漏事件 | PASS | 新 Key HTTP 200；缺失/错误 Key 均为 401；默认 key 已从 V4 Demo 删除；候选包扫描 0 命中；owner 确认旧 API Key 已平台侧作废 | 无 |
| P0-01 历史 token | PASS | token 仅驻留测试进程；610 秒后 Bearer 重连为 4401；owner 确认历史 access token 已失效 | 无 |
| P0-02 Bearer-only | PASS | 10097 最终 smoke 12/12 PASS；query-only/混合 4401，`ctrl.hello.token` 4400，日志敏感值 0 命中 | 无 |
| TLS 测试入口 | PASS（证书 pin） | smoke 使用固定 SHA-256 `460e09d5d59b91df...` | 生产放行前换受信证书，不阻塞内部 G3 |
| P1-01 源码可追溯 | PASS | 整改提交 `0710254d11d0ee84b0ab09e46d644fd283752461`，分支 `g3-cloud-v4-signoff-20260810`，完整 source snapshot 与 SHA-256 manifest；Release owner 已确认可读取位置 | 无 |
| P1-02 clean 复现 | 待 release | 本地和 10097 均为 126 tests、Ruff PASS；远端全 extras 同步 7 分钟未完成 | 不可变 commit 的 clean checkout 一次性复跑日志 |
| P1-03 WS 主路径 | PASS | 10097 dry-run、ack/result、多命令、Bearer 重连 12/12 PASS | 无 |
| P1-04 cmd lifecycle | PASS（测试阶段端云签收） | unknown/duplicate/late/两类 timeout 测试；10097 fake ack/running/succeeded；`JOINT-01_END_CLOUD_SIGNOFF_10097_20260811.md` 已回填 owner 签收 | 不阻塞当前 G3 candidate；真实动作放行和生产上线不在本次范围 |
| P1-05 caps | PASS | 10097 state 正例、hello 越权裁剪通过 | 无 |
| P1-06 X3 actions | PASS（运行态） | 4 个独立正向 case，纳入本地/远端 126 tests | clean checkout 复跑 |
| P2 包定位/源码 | 已闭环 | correction_candidate 命名、30 个源码/测试/锁文件快照 | 签收条件全绿后再生成 signoff_candidate |

## 签收判定规则

以下条件必须同时满足：

1. 两份安全回执均由明确 owner 签字，且不包含完整凭据。
2. Release owner 确认整改提交 `0710254d11d0ee84b0ab09e46d644fd283752461` 的 reviewer 可读取位置并签字。
3. clean checkout 采证脚本成功，工作区状态为 clean。
4. 10097 部署同一 commit，源码哈希与 clean checkout 一致。
5. 10097 的正式 `/ws/session` 完成 Bearer-only、控制 dry-run、多命令和回执冒烟。
6. HTTPS/WSS 正式 smoke 使用系统 CA/hostname 校验或 owner 确认的证书 SHA-256 pin，
   不使用 `--insecure` 或关闭证书校验。
7. 端云共同签收记录中云侧、端侧、测试和安全责任人均给出结论。
8. `ROBOT_ACTION_ENABLED` 等真实动作 gate 保持关闭；G3 签收不自动授权真机动作。

任何一项为空、使用“口头确认”或无法定位到工单/commit/日志文件，都按未关闭处理。
