# G3 云侧正式签收责任队列

按顺序执行；前一阶段未 PASS 时不得跳到最终签收。

| 顺序 | Owner | 动作 | 必须产物 | 完成判据 |
| ---: | --- | --- | --- | --- |
| 1 | Security owner | 作废/轮换已泄漏 API Key | `证据/签收/SEC-01_API_KEY_CLOSURE_OWNER_DECLARATION_20260811.md` | PASS：owner 确认平台侧作废且无明文 |
| 2 | Identity/deployment owner | 确认历史 access token 失效 | `证据/签收/SEC-02B_TOKEN_INVALIDATION_OWNER_DECLARATION_20260811.md` | PASS：旧 token 与 query 两种方式均 4401 |
| 3 | Release owner | 形成并推送 Commit A | `证据/签收/SRC-02_RELEASE_OWNER_SOURCE_CONFIRMATION_0710254d11d0_20260811.md` | PASS：评审员可读取源码锚点；clean checkout 单独关闭 |
| 4 | Test owner | 从 Commit A clean checkout 采证 | `证据/签收/REP-01_CLEAN_CHECKOUT_EVIDENCE_1b62ad5_e0aac93_20260812.md` | PASS：`e0aac93802afc017eaefc01adf5290ab7d44cdf9` clean checkout，sync、126 tests、Ruff、scan 全 PASS |
| 5 | Deployment owner | 10097 部署 Commit A | DEP-01 commit/manifest | 与 clean checkout 哈希一致，无手工覆盖 |
| 6 | Cloud + Test owner | 运行 HTTPS signoff smoke | WS-01/02/03、CAPS-01 summary | `overall=PASS`，边缘/应用日志 0 敏感命中 |
| 7 | 端侧 owner | 见证 ack/result 与 lifecycle | `证据/签收/JOINT-01_END_CLOUD_SIGNOFF_10097_20260811.md` | PASS：测试阶段端云联合签收已完成，不授权真实动作/生产上线 |
| 8 | Release owner | 生成证据 Commit B | evidence commit、整包 manifest | Commit B 不修改 30 个 source path |
| 9 | 四方 owner | 当前测试阶段 G3 candidate 签收 | `证据/签收/OWNER_WEB_ONLY_G3_CANDIDATE_SIGNOFF_20260811.md` | PASS（测试阶段 G3 candidate），不授权真实动作/生产上线/生产 TLS |

第 1、2 项已由 owner 回执关闭；第 3 项已由 Release owner 回执关闭源码可追溯边界。
第 4 项已由 `REP-01_CLEAN_CHECKOUT_EVIDENCE_1b62ad5_e0aac93_20260812.md` 关闭；第 5-9 项按当前测试阶段 G3 candidate 范围成立。

2026-08-11 评委复审后的逐项补证要求见 `G3_CLOUD_V4_JUDGE_REVIEW_RESPONSE_20260811.md`；
该文件不替代 owner 回执，只定义回填路径、最小字段和 G3/生产 TLS 边界。
