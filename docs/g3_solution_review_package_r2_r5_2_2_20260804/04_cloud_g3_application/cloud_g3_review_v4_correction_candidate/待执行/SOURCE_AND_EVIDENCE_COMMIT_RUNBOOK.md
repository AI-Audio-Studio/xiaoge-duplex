# 源码 Commit 与证据 Commit 操作单

本操作必须由 Release owner 执行。当前工作区包含其他未提交内容，禁止直接 `git add .`。

## 推荐的双 commit 模型

### Commit A：实现候选

只包含经代码评审确认的 G3 实现、测试、合同依赖和本包中的采证工具/空模板。建议分支：
`review/g3-cloud-v4-signoff`。

提交前用 `代码/SOURCE_SNAPSHOT_MANIFEST_20260810.md` 的 30 个路径作为源码核对基线，逐项
确认实际 staging 内容；不要因为文件位于脏工作区就把无关改动带入。

建议 message：

```text
feat(g3-cloud): enforce bearer-only session auth and close review gaps
```

Commit A 产生后：

1. 推送分支并记录 40 位 SHA。
2. 在 Commit A 的 clean checkout 执行本包 collector。
3. 10097 部署 Commit A，执行远端操作单。
4. API Key/token owner 完成签字。

### Commit B：签收证据

只增加脱敏日志、owner 回执、状态表和最终 manifest，不修改生产源码。建议 message：

```text
docs(g3-cloud): attach v4 signoff evidence
```

Commit B 的材料必须同时记录：

- `source_commit`：Commit A。
- `evidence_commit`：Commit B。
- 10097 部署 commit：必须等于 Commit A。
- Commit A 与 Commit B 中 30 个 source path 的 SHA-256：必须全部一致。

这种拆分避免在源码 commit 内自引用尚未产生的 commit SHA，也能让评审确认 Commit B 没有
夹带生产代码变化。

## 禁止项

- 不得使用 `git add .`、`git commit -a` 或把当前全部脏工作区一次提交。
- 不得 amend/force-push 已用于 clean checkout 或 10097 部署的 Commit A。
- 不得在 Commit A 后手工修改 10097 源码。
- 不得把完整凭据、环境 dump、带 token URL 或未脱敏 access log 放入 Commit B。
- 不得在 owner 未签字时把模板自行改成 PASS。
