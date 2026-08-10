# G3 云侧 v3 更新包评审组复审意见

日期：2026-08-10

评审对象：`outputs/xiaoge_full_duplex_20260731/端云回应/cloud_g3_review_v3`

评审方式：只读取本包材料、R5.2.2/G3 设计合同与当前工作区可见文件；未修改工程代码，未运行远端 10097 测试。本轮没有复写任何完整 key/token。

## 1. 评审结论

结论：`G3_CLOUD_V3_CORRECTION_ACCEPTED_BUT_NOT_SIGNOFF`

v3 包作为“v2 复审纠错响应包”可以采信：它撤回了“7 项全部关闭”的过满结论，材料已脱敏，端口说明已更正，并把未关闭项列清楚。  
但它不是云侧 G3 签收包，不能作为端云共同签收、合入 `main`、上线/灰度或真实机器人动作放行依据。

| 闸口 | 评审判断 |
| --- | --- |
| 当前目录作为纠错材料传给云侧整改责任人 | 可以 |
| 当前目录扩大分发给非必要人员 | 谨慎；需凭据 owner 确认轮换/失效后再扩大 |
| 作为 v3 签收包 | 不可以 |
| 端云共同签收/合入 `main` | 不可以 |
| 上线/灰度/真实机器人动作 | 不可以 |

## 2. 阻断问题

| ID | 级别 | 责任方 | 问题 | 事实依据 | 影响 | 关闭条件 |
| --- | --- | --- | --- | --- | --- | --- |
| REVIEW-G3-CLOUD-V3-P0-01 | P0 | 云侧/安全 owner | 凭据安全事件仍未关闭。v3 只证明当前材料目录脱敏，不证明已泄漏 key 被轮换、token 已失效，也不证明源码默认 key 已移除 | v3 脱敏检查称本目录完整 key/token 命中数为 0：`证据/G3_CLOUD_V2_SANITIZATION_CHECK_20260810.md:19-26`；同文档承认仓库级剩余风险和源码默认 key 未整改：`证据/G3_CLOUD_V2_SANITIZATION_CHECK_20260810.md:28-37`；更正回应明确 P0-01 当前状态为未关闭：`G3_CLOUD_V2_REVIEW_CORRECTION_RESPONSE_20260810.md:23-26`；README 也把 key 轮换/失效确认列为 v3 最小前置：`README.md:32-35` | 当前包可作为云侧整改沟通材料，但不能作为安全事件关闭证明；也不能支持更大范围分发、合入或上线 | 凭据 owner 出具已泄漏 API key 作废/轮换回执；确认历史 access token 失效；生产/可分发构建移除默认 key；重新生成脱敏包并附扫描命令与 0 命中结果 |
| REVIEW-G3-CLOUD-V3-P0-02 | P0 | 云侧 | Bearer-only 产品协议仍未实现关闭，query token 仍被当前 10097 产品路径接受 | 设计要求 WSS Upgrade 必须携带 `Authorization: Bearer <access_token>`，`ws_url` 不带 token：`docs/design/protocol-v2/PROTOCOL_V2_DESIGN.md:80-81`；权威口径明确 URL query 不得携带 token：`docs/design/protocol-v2/PROTOCOL_V2_DESIGN.md:131`；合同正例只含 Authorization header：`outputs/xiaoge_full_duplex_20260731/g3_solution_review_package_r1_r5_2_2_20260804/02_contracts/xiaoge-duplex-protocol-r5.2.2.examples.jsonl:3`。v3 页面冒烟把 query token 101 标为协议 FAIL：`测试/G3_CLOUD_PAGE_SMOKE_LOG_20260810.md:23-30`、`测试/G3_CLOUD_PAGE_SMOKE_LOG_20260810.md:207-214`；更正回应承认 gateway/webpanel 仍有 query fallback：`G3_CLOUD_V2_REVIEW_CORRECTION_RESPONSE_20260810.md:80-91` | 仍违反 G1/G2 冻结合同；端侧按 Bearer-only 开发时存在互通和安全口径偏差；不能签收 | 移除正式 `/ws/session` query fallback；补 Bearer 成功、query-only 失败、Authorization+query 失败、`ctrl.hello.token` 失败的测试和运行日志；access log 不得记录敏感 URL token |

## 3. 重要问题

| ID | 级别 | 责任方 | 问题 | 事实依据 | 影响 | 关闭条件 |
| --- | --- | --- | --- | --- | --- | --- |
| REVIEW-G3-CLOUD-V3-P1-01 | P1 | 云侧 | 源码可追溯仍未关闭，本包没有完整 diff/source snapshot，也没有包含改动的 commit | 源码状态文档显示相关生产文件为 `M`、测试文件为 `??`：`整改说明/G3_CLOUD_SOURCE_TRACEABILITY_STATUS_V2.md:12-19`；同文档承认 sha256 不能证明测试参数、完整上下文或部署过程：`整改说明/G3_CLOUD_SOURCE_TRACEABILITY_STATUS_V2.md:21-32`；主响应也写明当前仅有 sha256，无完整 diff/source snapshot：`G3_CLOUD_FEEDBACK_FIX_RESPONSE_20260810.md:18`、`G3_CLOUD_FEEDBACK_FIX_RESPONSE_20260810.md:206-211` | 评审组仍无法仅凭本包做源码级复核；远端 56 passed 只能作为当前环境结果采信 | 形成独立分支/commit；归档完整脱敏 diff 或 source snapshot 及 manifest；clean checkout 后复跑；远端部署同一 commit/snapshot 并比对 sha256 |
| REVIEW-G3-CLOUD-V3-P1-02 | P1 | 云侧 | clean 环境复现仍未关闭，10097 复跑前依赖靠手工补包 | 页面冒烟记录说明 10097 缺 `dashscope`、`pymysql`，通过手工 `uv pip install` 后复跑：`测试/G3_CLOUD_PAGE_SMOKE_LOG_20260810.md:32-54`；结论处明确没有 clean checkout 执行 `uv sync --all-extras --dev` 后一次复现证据：`测试/G3_CLOUD_PAGE_SMOKE_LOG_20260810.md:227-238`；复跑说明也承认 P0-04 仍为部分关闭：`整改说明/G3_CLOUD_REPRODUCIBLE_EVIDENCE_INDEX_V2.md:124-130` | 换机、重建 venv 或交接给其他同事时仍可能不可复现 | 在 clean checkout 中记录 `uv sync --all-extras --dev` 完整输出、pytest/ruff、页面/WS 主路径冒烟；归档命令、时间、commit、环境 |
| REVIEW-G3-CLOUD-V3-P1-03 | P1 | 云侧/端云共同 | 页面/WS 主路径验收仍不完整，控制 dry-run、多命令、Bearer-only 重连、端回执/fake 回执未覆盖 | 页面冒烟自己列出断开重连只是 query token 旧路径，单条控制无证据：`测试/G3_CLOUD_PAGE_SMOKE_LOG_20260810.md:216-225`；结论明确单条控制 dry-run、多命令主路径和 Bearer-only 断开重连未覆盖：`测试/G3_CLOUD_PAGE_SMOKE_LOG_20260810.md:227-231` | 只能认可普通文本、基础 HTTP 鉴权、无 token 关闭、非法 frame 等局部检查；不能证明 G3 命令链路可用 | 补真实或等效 WS 主路径：单条控制生成 `data.cmd` dry-run；端侧或 fake executor 回 `data.cmd_ack/result`；多命令只 `data.reply.ask_split`；Bearer-only 断开重连；全链路帧日志归档 |
| REVIEW-G3-CLOUD-V3-P1-04 | P1 | 云侧/端云共同 | `cmd_id` lifecycle 仍未关闭，schema 校验不能替代 unknown/duplicate/late/timeout 语义 | 设计要求 unknown `cmd_id` 走 `data.error.code=unknown_cmd_id`，重复和 late 只审计、不污染当前轮：`docs/design/protocol-v2/PROTOCOL_V2_DESIGN.md:448-454`；合同负例包含 unknown 与 duplicate 语义：`outputs/xiaoge_full_duplex_20260731/g3_solution_review_package_r1_r5_2_2_20260804/02_contracts/xiaoge-duplex-protocol-r5.2.2.examples.jsonl:29-30`；v3 代码说明承认 P1-01 只请求确认 schema 子项，lifecycle 未关闭：`整改说明/G3_CLOUD_CODE_CHANGES_ACK_RESULT_V2.md:230-242` | 单侧 schema pass 不能证明云侧命令生命周期安全；端云联调可能出现错误 ack/result 污染当前轮 | 补同一 `cmd_id` 的云下发、端 ack、running/succeeded、unknown、duplicate、late、timeout 审计日志和测试断言 |
| REVIEW-G3-CLOUD-V3-P1-05 | P1 | 云侧 | caps 授权口径仍未完整验收，尤其 `state` 和 hello 不得扩大 create_session 授权 | R5.2.2 caps 枚举包含 `audio/text/cmd/state`，`granted_caps` 为服务端白名单与请求集交集：`docs/design/protocol-v2/PROTOCOL_V2_DESIGN.md:120-124`；v3 更正回应承认当前样例未验收 `state`，且 `ctrl.ready` 未证明不超出 create_session granted_caps：`G3_CLOUD_V2_REVIEW_CORRECTION_RESPONSE_20260810.md:93-99`；页面冒烟也说明该样例不能作为 `state` cap 验收证据：`测试/G3_CLOUD_PAGE_SMOKE_LOG_20260810.md:117-122` | 可能出现端侧 hello 扩大授权面的实现偏差 | 增加含 `state` 的 create_session/hello 正例；增加 hello caps 超出 session granted_caps 的反例，要求拒绝或裁剪并记录 |
| REVIEW-G3-CLOUD-V3-P1-06 | P1 | 云侧 | X3 矩阵已改为更准确，但仍不是逐 action 独立可验收 | 矩阵中 `motion.turn`、`face.eyebrow`、`system.shutdown`、`power.charge` 标为共享分支间接覆盖、待独立用例：`覆盖矩阵/X3_OFFLINE_SKILL_COVERAGE_MATRIX_20260810.md:14-25`；状态汇总列 4 项待独立正例：`覆盖矩阵/X3_OFFLINE_SKILL_COVERAGE_MATRIX_20260810.md:27-36`；后续计划仍要求补齐：`覆盖矩阵/X3_OFFLINE_SKILL_COVERAGE_MATRIX_20260810.md:60-64` | 目录级矩阵可采信，但不能按逐 action 粒度签收 | 补 4 个独立正向 case，或明确这些 action 不纳入本轮签收范围 |

## 4. 次要问题

| ID | 级别 | 责任方 | 问题 | 事实依据 | 建议 |
| --- | --- | --- | --- | --- | --- |
| REVIEW-G3-CLOUD-V3-P2-01 | P2 | 云侧 | 包名与内容定位仍容易误导。目录名是 `cloud_g3_review_v3`，但 README 和主响应仍强调只是 v2 纠错，不能作为 v3/签收包 | `README.md:5-9`；`G3_CLOUD_V2_REVIEW_CORRECTION_RESPONSE_20260810.md:101-111` | 下轮请命名为 `cloud_g3_review_v3_correction_only` 或提交真正的 `cloud_g3_review_v4_signoff_candidate`，避免项目同事误判为签收包 |
| REVIEW-G3-CLOUD-V3-P2-02 | P2 | 云侧 | 本包内无 `.py/.diff/.patch` 可审文件，容易让评审只停留在材料自述 | 本轮 `rg --files ... | rg "\\.(py|js|ts|diff|patch)$"` 无结果；源码追溯文档也承认可追溯不足：`整改说明/G3_CLOUD_SOURCE_TRACEABILITY_STATUS_V2.md:21-32` | 下一包把 source snapshot 或脱敏 diff 放入 `代码/` 或 `diff/` 目录，并列 manifest |

## 5. 可认可进展

- v3 已撤回 v2 的“7 项全部关闭”结论，闸口口径准确：`README.md:5-9`、`G3_CLOUD_FEEDBACK_FIX_RESPONSE_20260810.md:9-12`、`G3_CLOUD_FEEDBACK_FIX_RESPONSE_20260810.md:346-353`。
- 当前 v3 目录材料已做脱敏扫描，未发现完整 key/token 形态；但安全事件本身仍未关闭：`证据/G3_CLOUD_V2_SANITIZATION_CHECK_20260810.md:19-26`。
- 10097 单侧 pytest/ruff 输出可作为当前环境回归通过证据采信：`证据/10097_pytest_v2.log:8-67`、`证据/10097_ruff_v2.log:1`。
- 同机 10097/10099 双池端口段更正为“同时运行但端口不重叠”，比 v2 准确：`证据/10097_PORT_PROCESS_DIRECTORY_MAPPING_20260810.md:6-16`。
- ack/result schema 字段与枚举修正方向符合合同；但 lifecycle 仍待端云共同闭环：`整改说明/G3_CLOUD_CODE_CHANGES_ACK_RESULT_V2.md:56-83`、`整改说明/G3_CLOUD_CODE_CHANGES_ACK_RESULT_V2.md:230-242`。
- 真实机器人动作 gate 继续保持关闭，当前材料未请求放行：`G3_CLOUD_FEEDBACK_FIX_RESPONSE_20260810.md:74-92`。

## 6. 对云侧的下一轮提交要求

1. 安全先行：提交 key 轮换/作废回执、token 失效确认、生产/可分发构建移除默认 key 的代码证据与扫描结果。
2. 协议先行：正式 `/ws/session` Bearer-only，移除 query fallback；如浏览器 demo 需要兼容，必须拆到不参与 G3 签收的隔离 debug endpoint 或 BFF。
3. 源码先行：提交可定位 commit，附完整脱敏 diff/source snapshot、manifest、clean checkout 复跑记录。
4. 验收补齐：补控制 dry-run、多命令 ask_split、Bearer-only 重连、cmd_id lifecycle、caps 授权、4 个 X3 独立正向用例。

## 7. 最终判断

评审组认可 v3 对上一轮问题的事实纠偏，不认可其作为签收候选。云侧可以继续整改；在 P0-01 和 P0-02 未关闭前，不进入端云共同签收、合入、上线或真实机器人动作讨论。
