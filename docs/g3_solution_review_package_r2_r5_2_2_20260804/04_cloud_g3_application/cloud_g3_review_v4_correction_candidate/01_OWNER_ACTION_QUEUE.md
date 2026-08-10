# G3 云侧正式签收责任队列

按顺序执行；前一阶段未 PASS 时不得跳到最终签收。

| 顺序 | Owner | 动作 | 必须产物 | 完成判据 |
| ---: | --- | --- | --- | --- |
| 1 | Security owner | 作废/轮换已泄漏 API Key | SEC-01 实际回执、工单引用 | 旧 key 调用被拒绝且无明文 |
| 2 | Identity/deployment owner | 确认历史 access token 失效 | SEC-02 实际回执 | 旧 token 与 query 两种方式均 4401 |
| 3 | Release owner | 形成并推送 Commit A | 40 位 SHA、PR/分支、源码 manifest | 评审员可读取且不再改写历史 |
| 4 | Test owner | 从 Commit A clean checkout 采证 | REP-01 evidence 目录 | sync、126 tests、Ruff、scan 全 PASS |
| 5 | Deployment owner | 10097 部署 Commit A | DEP-01 commit/manifest | 与 clean checkout 哈希一致，无手工覆盖 |
| 6 | Cloud + Test owner | 运行 HTTPS signoff smoke | WS-01/02/03、CAPS-01 summary | `overall=PASS`，边缘/应用日志 0 敏感命中 |
| 7 | 端侧 owner | 见证 ack/result 与 lifecycle | JOINT-01 端侧栏 | 无 unknown/duplicate/late 污染 |
| 8 | Release owner | 生成证据 Commit B | evidence commit、整包 manifest | Commit B 不修改 30 个 source path |
| 9 | 四方 owner | 正式签收 | 完整 JOINT-01 | 四方 PASS，无未批准例外 |

当前可以并行启动第 1、2 项；第 3 项需要 Release owner 明确本次 staging 边界，因为工作区还
存在其他用户改动。第 4-9 项依赖 Commit A。
