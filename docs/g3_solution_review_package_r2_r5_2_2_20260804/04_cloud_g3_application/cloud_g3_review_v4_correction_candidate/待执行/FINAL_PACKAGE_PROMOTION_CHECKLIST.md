# V4 正式签收包晋级清单

目标目录名：`cloud_g3_review_v4_signoff_candidate`。

## 晋级前

- [ ] `SIGNOFF_EVIDENCE_INDEX.md` 中所有 P0/P1 为 PASS。
- [ ] 安全、token、release 和端云共同签收均有实际签字文件，不是未填写模板。
- [ ] API Key 轮换工单可由评审员访问，但正文不含完整 key。
- [ ] Release commit 为 40 位不可变 SHA，已推送到评审员可读取的位置。
- [ ] clean checkout、10097 和 source snapshot 三方 commit/哈希一致。
- [ ] 10097 应用和前置反向代理日志敏感值扫描均为 0。
- [ ] 远端 smoke summary 的 `overall` 为 `PASS`。
- [ ] `ROBOT_ACTION_ENABLED=0` 或等价 gate 关闭证据已归档。

## 生成签收候选包

1. 从当前 correction candidate 复制生成新目录，不覆盖历史包。
2. 将 `待签字/` 模板替换为实际脱敏签字文件；原模板可移到 `模板/`。
3. 将 clean/10097/联合签收证据放入 `证据/签收/`。
4. 把 README 结论改为“申请正式签收”，但不要声明评审已通过。
5. 把状态总表所有关闭项更新为 PASS，并给出实际文件路径和行号。
6. 对源码快照、工具、日志、签字材料和整个目录分别生成 SHA-256 manifest。
7. 执行整包敏感信息扫描，只归档计数和命令，不归档匹配值。
8. 由提交人和第二复核人共同确认 manifest 后再交评审组。

## 评审通过后

评审结论文件由评审组生成或签字，不能由整改责任人预先填写。G3 正式签收仍不等于上线、
灰度或真机动作放行；这些动作需进入独立发布/安全流程。

