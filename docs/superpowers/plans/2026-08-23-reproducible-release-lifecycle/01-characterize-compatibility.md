# 任务 01：锁定 Stage 7–8 兼容行为

**状态：** 已完成
**服务总目标：** 在重构前把现有可观察行为变成失败即阻断的证据，防止“看似等价”的实现悄悄改变研究审计口径。

## 前置条件

- 用户明确授权“开始任务 01”。
- 已完成 [执行地图](00-execution-map.md) 的启动前公共检查。
- 已阅读[权威设计](../../specs/2026-08-23-reproducible-release-lifecycle-design.md)。
- 主仓库保持只读基线；实现位于候选 1 专用隔离 worktree。

## 输入

- `src/ashare_multifactor/validation/pipeline.py`
- `src/ashare_multifactor/robustness/pipeline.py`
- `src/ashare_multifactor/cli/validation.py`
- `src/ashare_multifactor/cli/robustness.py`
- `tests/test_validation_pipeline.py`
- `tests/test_robustness_publication.py`
- 当前 Stage 7/8 release manifest、lineage 和复现证书，只读使用

## 允许修改

- 新增 Stage 7/8 兼容性测试文件；
- 新增 `tests/fixtures/reproducible_release/` 下的最小固定快照；
- 为测试复用新增仅位于 `tests/` 的辅助函数。

## 本任务不修改

- `src/` 中任何生产代码；
- CLI、配置、研究结果和报告；
- `processed/`、`artifacts/`、`Data/`；
- 任一 `CURRENT.json` 或历史 release；
- Stage 6、Stage 9 与 `audit.publication`。

## 产出

- `tests/test_validation_release_compatibility.py`
- `tests/test_robustness_release_compatibility.py`
- 必要时新增 `tests/fixtures/reproducible_release/` 固定字节快照
- 本任务验证记录和 `HANDOFF`

## 执行步骤

- [x] 列出两个 pipeline 中全部被 CLI、测试或仓库其他模块直接引用的名称、签名和返回类型；形成测试内的明确兼容清单。
- [x] 用固定 `code_identity`、冻结输入、运行 ID、时间和资源值，记录 Stage 7 运行清单与 `full_reproducibility.json` 的精确字节。
- [x] 用固定身份和输入记录 Stage 8 运行清单与 `reproducibility.json` 的精确字节。
- [x] 覆盖 Stage 7 的 98 文件、封闭树、符号链接拒绝、额外文件拒绝、safe-slug 和 `resource_usage`。
- [x] 覆盖 Stage 8 的 3 文件、现有目录/运行 ID 宽松行为以及不含 `resource_usage`。
- [x] 固定两阶段源运行漂移、输入漂移、代码漂移、dirty identity、缺文件和重复目标目录的异常类型与消息。
- [x] 固定 Stage 7 `publish=false` 暂存和 Stage 8 发布准备的文件映射、lineage/证书关键结构；只使用人工夹具或只读历史字节，不调用真实发布根。
- [x] 固定现有 CLI 的命令、参数要求和打印返回口径。
- [x] 先运行新增测试，再运行现有 validation/robustness/publication 相关测试。
- [x] 确认所有新增测试在未修改生产代码的基线上通过；若无法通过，修正测试对现状的误解，不修改生产行为。

## 验收

- [x] `src/` 零改动。
- [x] 两阶段现有入口和已观察内部调用面有清单化测试。
- [x] 固定身份下的两类运行清单和两类复现证书有字节级断言。
- [x] Q6 所列历史差异均有正例或反例测试。
- [x] 当前错误类型和关键错误消息被固定。
- [x] 相关测试全部通过。
- [x] Stage 7/8 `CURRENT.json` 字节与任务开始前一致。
- [x] `processed/final_test/CURRENT.json` 仍不存在。

## 失败关闭

- 若现有行为无法由人工夹具稳定复现，记录不稳定字段及原因，任务状态设为 `blocked`；不得在本任务修生产代码。
- 若发现当前历史发布已无法读取或指针/manifest/lineage 不一致，立即停止并报告独立缺陷；不得把它纳入候选 1 重构。
- 若测试必须读取最终测试期才能通过，判定测试设计越界并停止。

# 强制停点

满足验收后只提交任务 01 报告和下方 `HANDOFF`，停止。不得开始生命周期模块、适配器或 pipeline 重构。

## HANDOFF

```yaml
task: 01-characterize-compatibility
status: complete
baseline_commit: 1e7eddbe8a2319110f275d254b1a7702e41d1aea
worktree: /Users/mikasa/本科时期/Projects/repository1/.worktrees/reproducible-release-lifecycle-candidate1
production_files_changed: []
characterization_tests:
  - tests/test_validation_release_compatibility.py
  - tests/test_robustness_release_compatibility.py
fixed_contracts:
  - Stage 7 observed call surface, 98-file closed tree, safe-slug, resource_usage, manifest and certificate bytes
  - Stage 8 observed call surface, 3-file loose tree, legacy run IDs, manifest and certificate bytes
  - source/input/code drift, dirty identity, missing files, duplicate targets, staging mappings, lineage and CLI contracts
verification:
  - related validation/robustness/publication suite: 157 passed
  - ruff check .: passed
  - git diff --check: passed
current_pointers_unchanged: true
final_test_current_absent: true
next_task: 02-build-lifecycle-core
next_task_authorized: true
```
