# Release Owner 源码发布确认模板

| 字段 | 待填写 |
| --- | --- |
| 分支 | `<branch>` |
| Commit SHA（40 位） | `<commit>` |
| 提交时间（UTC） | `<YYYY-MM-DDTHH:MM:SSZ>` |
| 代码评审/PR | `<URL 或 ID>` |
| clean checkout 证据目录 | `<relative-path>` |
| source manifest SHA-256 | `<sha256>` |
| 10097 部署 commit | `<commit，必须一致>` |
| 10097 manifest SHA-256 | `<sha256，必须一致>` |
| 工作区状态 | `<clean>` |
| Release owner | `<姓名/账号>` |
| 结论 | `<PASS/FAIL>` |
| 签字时间（UTC） | `<YYYY-MM-DDTHH:MM:SSZ>` |

Release owner 声明：评审源码、clean checkout 测试源码和 10097 部署源码均来自同一不可变 commit；
没有在目标机手工覆盖源码或手工补装未声明依赖。

