# 小歌 Duplex · 文档索引

> ← 返回项目首页：[../README.md](../README.md)　|　配置清单：[../.env.example](../.env.example)　|　运行脚本在仓库根目录（`start_agent.cmd` / `stop_agent.cmd` / `setup.ps1`）

本工程的所有文档集中在 `docs/` 下，按用途分四类。

## 上手 & 运维 · [guide/](guide/)
| 文档 | 说明 |
| --- | --- |
| [guide/RUN.md](guide/RUN.md) | 本地构建 / 启动 / 关闭 / 配置管理 |
| [guide/ARCHITECTURE.md](guide/ARCHITECTURE.md) | 系统架构总览与一轮对话生命周期（配 [diagrams/](diagrams/)） |

## 设计 & 评审 · [design/](design/)
按特性分组，每个特性的设计 / 评审 / 重构放在同一子目录。
| 特性 | 文档 |
| --- | --- |
| 判停 & STT（turn-taking / STT） | **[design/turn-stt/TURN_RULES.md](design/turn-stt/TURN_RULES.md)（判停规则速查）** · [design/turn-stt/TURN_STT_DESIGN.md](design/turn-stt/TURN_STT_DESIGN.md) · [TURN_STT_DESIGN_REVIEW.md](design/turn-stt/TURN_STT_DESIGN_REVIEW.md) · [TURN_STT_REFACTOR.md](design/turn-stt/TURN_STT_REFACTOR.md) |
| 聆听模式（listening mode） | [design/listening-mode/LISTENING_MODE_DESIGN.md](design/listening-mode/LISTENING_MODE_DESIGN.md) · [LISTENING_MODE_DESIGN_REVIEW.md](design/listening-mode/LISTENING_MODE_DESIGN_REVIEW.md) |
| 测试 | [design/TESTING_DESIGN.md](design/TESTING_DESIGN.md) |
| 服务器并发改造 | [design/concurrency/CONCURRENCY_DESIGN.md](design/concurrency/CONCURRENCY_DESIGN.md)（分析与设计 v3,定稿候选待抽查;附录含两轮评审+定稿裁定往返） |

## 报告 · [reports/](reports/)
| 文档 | 说明 |
| --- | --- |
| [reports/RESOURCE_REPORT.md](reports/RESOURCE_REPORT.md) | 资源消耗实测报告（总体 + 分模块 + 最低配置 + 降耗路径） |

## 协作规范 · [project/](project/)
| 文档 | 说明 |
| --- | --- |
| [project/CODE_GUIDELINES.md](project/CODE_GUIDELINES.md) | 自有代码规范(文件/函数大小、解耦);`make lint-ours` 强制 |
| [project/CONTRIBUTING.md](project/CONTRIBUTING.md) | 贡献指南 |
| [project/CODE_OF_CONDUCT.md](project/CODE_OF_CONDUCT.md) | 行为准则 |

## 图示 · [diagrams/](diagrams/)
`architecture.svg`（系统架构）、`sequence-turn.svg`（一轮对话时序）——被 [guide/ARCHITECTURE.md](guide/ARCHITECTURE.md) 引用。

---

### 与仓库根目录的关系
- **项目门面**：根 [../README.md](../README.md)（首页、快速开始）。
- **运行脚本**（保留在根，双击/命令行入口）：`start_agent.cmd`、`stop_agent.cmd`、`setup.ps1`、`start.ps1`、`stop.ps1`。
- **配置**：根 [../.env.example](../.env.example)（复制为 `.env` 后修改）。
- **构建/依赖**：根 `pyproject.toml`、`makefile`、`renovate.json`。
- **AI/开发者指引**：根 `AGENTS.md`（构建命令速览）、`CLAUDE.md`。
