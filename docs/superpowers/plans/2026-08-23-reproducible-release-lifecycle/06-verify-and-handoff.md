# 任务 06：完整验证、历史读取与临时根影子验收

**状态：** 任务 06S 整改与两阶段影子验收通过，候选 1 已完成
**服务总目标：** 用代码测试和现有真实证据证明候选 1 完成，同时保持研究发布、权威指针和最终测试期完全不变。

## 前置条件

- 用户明确授权“开始任务 06”。
- 任务 05 `HANDOFF.status=complete`。
- worktree 仅含候选 1 已声明文件改动。
- 有足够临时空间容纳 Stage 7/8 影子副本；预检不足时不复制。

## 输入

- 候选 1 全部实现、测试和前五个 `HANDOFF`
- 当前 Stage 7 `CURRENT.json`、release manifest、lineage、复现证书和认证运行
- 当前 Stage 8 `CURRENT.json`、release manifest、lineage、复现证书和认证运行
- 当前仓库测试配置与可用项目 Python 环境

## 允许修改

- 修复候选 1 范围内由本任务验证发现的缺陷；修复必须先有失败测试
- 新增候选 1 最终验证记录或测试辅助文件
- 在系统临时目录创建可删除的独立影子副本

## 本任务不修改

- `Data/`、真实 `processed/`、真实 `artifacts/` 和历史 release 字节；
- 真实 Stage 7/8 `CURRENT.json`；
- `processed/final_test/`；
- 研究配置、Stage 6、Stage 9、远程仓库；
- 不提交、合并、推送或创建 PR。

## 影子验证原则

- 开始前记录真实 Stage 7/8 指针字节、当前 release manifest SHA-256 和最终测试指针不存在状态。
- 只把认证和暂存所需文件复制到新建临时根；Stage 7 约 360 MiB，使用真实副本，不使用硬链接。
- 测试夹具可以把阶段数据根解析定向到临时根，但不得新增面向生产调用者的数据根覆盖 API。
- 影子流程只执行证书重建、源运行复验和 `publish=false` 暂存；不运行真实 Stage 7/8 领域计算。
- 如需测试 `publish=true`，只能对完全独立的临时 publication 根使用人工运行 ID，并验证其 `CURRENT.json` 不可能指向真实根。
- 临时根验证失败时保留日志和摘要，删除或隔离临时副本；不在真实根重试。

## 执行步骤

- [x] 记录 worktree HEAD、diff 范围、真实指针原始字节、当前 release 清单哈希和 `processed/final_test/CURRENT.json` 不存在状态。
- [x] 运行 lifecycle、Stage 7、Stage 8、CLI、audit publication 和 sealed protocol 的相关测试。
- [x] 使用项目环境运行完整 pytest；记录总数、跳过数、耗时和退出状态。
- [x] 运行 Ruff；记录命令和退出状态。
- [x] 通过既有 `resolve_current`/release verifier 只读核验当前 Stage 7 发布。
- [x] 通过既有 `resolve_current`/release verifier 只读核验当前 Stage 8 发布及关闭的 opening token 状态。
- [x] 按 06S 机器可读豁免仅映射 BaoStock 原始身份，在临时根完成 Stage 7 `publish=false` 暂存。
- [x] 在另一临时根复制 Stage 8 所需真实文件，执行影子证书重建、源运行复验、seal/lineage 暂存；successor 审计路径按当前证书语义完整复制。
- [x] 将影子结果与历史对应字节比较：Stage 7 的 98 个核心文件零哈希差异；Stage 8 核心文件及 successor 证据树一致。
- [x] 再次读取真实指针字节和历史 manifest SHA-256，确认与开始前完全一致。
- [x] 再次确认精确路径 `processed/final_test/CURRENT.json` 不存在，且没有新 opening ledger、最终测试报告或最终测试数据读取证据。
- [x] 检查 `git diff --check`、Markdown 本地链接、任务文件状态和最终变更清单。
- [x] 形成候选 1 完成报告：改动、测试、影子证据、未运行事项、已知限制和下一步选择。

## 最终验收

- [x] 三入口生命周期和两个三能力适配器符合权威设计。
- [x] Stage 7/8 现有入口、参数、CLI、返回值和错误合同通过全部兼容测试。
- [x] Stage 7/8 历史差异逐项通过代码兼容测试。
- [x] 固定身份下清单、证书、lineage/暂存关键字节兼容。
- [x] 共享编排只有一个实现来源。
- [x] 完整 pytest 与 Ruff 通过。
- [x] 两个当前历史发布均可只读解析和核验。
- [x] 两阶段临时根影子认证与暂存通过。
- [x] 真实 Stage 7/8 指针、历史 release 和 manifest 未改变。
- [x] 本轮最终测试期未读取、未开放、未发布；先前失败尝试的 1 次目录元数据枚举保留在审计记录。
- [x] `audit.publication` 未修改，未新增公共 API、CLI、注册表或文件系统 port。
- [x] 没有提交、合并、推送或 PR。

## 失败关闭

- 完整测试、历史读取或任一影子验证失败，候选 1 状态保持 `incomplete`；不得以相关测试通过代替最终验收。
- 若失败揭示历史产物本身已漂移，保留只读证据并拆成独立审计任务；不得修改历史 release。
- 若临时空间不足，记录所需/可用空间并设为 `blocked`，不使用硬链接或真实根暂存绕过。
- 若任何命令触发最终测试期路径访问，立即终止，记录命令和访问边界，候选 1 不得宣称完成。

# 强制停点

输出候选 1 最终报告和下方 `HANDOFF` 后停止。不得提交、合并、推送、切换真实指针、开始其他架构候选或进入 Stage 9。

## HANDOFF

```yaml
task: 06-verify-and-handoff
status: complete
baseline_handoff: 05-remove-duplication
worktree_head: 1e7eddbe8a2319110f275d254b1a7702e41d1aea
files_changed:
  - docs/superpowers/plans/2026-08-23-reproducible-release-lifecycle/01-characterize-compatibility.md
  - docs/superpowers/plans/2026-08-23-reproducible-release-lifecycle/02-build-lifecycle-core.md
  - docs/superpowers/plans/2026-08-23-reproducible-release-lifecycle/06-verify-and-handoff.md
  - docs/superpowers/plans/2026-08-23-reproducible-release-lifecycle/06S-corporate-action-provenance-remediation.md
  - docs/audits/2026-08-24-candidate1-task6-final-verification.md
  - docs/audits/2026-08-27-candidate1-task6s-external-source-revision-waiver.json
  - docs/audits/2026-08-27-candidate1-task6s-route-assessment.json
  - src/ashare_multifactor/audit/reproducible_release.py
  - src/ashare_multifactor/validation/corporate_actions.py
  - tests/test_reproducible_release_lifecycle.py
  - tests/test_validation_backtest.py
related_tests: 122 passed in 1.58s
task6s_focused_tests: 67 passed in 0.50s
full_pytest: 1606 passed, 1 skipped in 532.65s
ruff: passed
stage7_historical_readback: passed
stage8_historical_readback: passed; opening_token_status=closed
stage7_input_recovery: 11 of 12 files exact; BaoStock serial retry completed 10560 successful queries and recovered 9516x16 shape, but raw parquet SHA-256 is 435741913656864417d3619ff46df5fed587ec1b65515e56d47294cb01060d1e instead of historical ffdfdc8dd0c4835009da53a69ad88c5984d34407d7612887867e4a3006048bab
stage7_shadow_certification: passed via task 06S waiver; publish=false; 98 core files exact
stage8_shadow_certification: passed with fixed clean certificate identity fixture
task6r_status: historical failure retained; superseded by completed task 06S
task6r_new_raw: 9516x16; 13590 serial queries; sha256=77e4fb6e076d87d4d5536fa1d4d292ff2f678ccd45a3d087fa35dd18579d3980; size=175011
task6r_consumer_semantic_differences: 2; one 000042 row differs in source and derived action_id
task6s_external_source_revision_waiver: passed; consumer_semantic_differences=0
task6s_shadow_identity_mapping_performed: true; only baostock_dividends mapped
task6s_stage7_shadow: passed; 98 core files; 0 hash mismatches
task6s_stage8_shadow: passed; successor evidence trees exact; opening_token_status=closed
task6r_related_tests: 8 passed in 7.19s
task6r_full_pytest: 1604 passed, 1 skipped in 448.27s
real_current_pointers_unchanged: true; final hashes equal baseline
historical_releases_unchanged: true; no real-root writes and current manifest hashes equal baseline
final_test_current_absent: true at rerun baseline and final exact-path check
prior_failed_attempt_final_test_metadata_reads: 1
current_rerun_final_test_read_count: 0
commit_merge_push_performed: false
candidate_1_complete: true
next_task: null
```
