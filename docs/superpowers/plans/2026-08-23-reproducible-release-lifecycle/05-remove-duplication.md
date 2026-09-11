# 任务 05：删除已被替代的共享编排

**状态：** 已完成
**服务总目标：** 在两阶段都已证明兼容后，删除真正重复的生命周期实现，使安全修复只需修改一个共同位置。

## 前置条件

- 用户明确授权“开始任务 05”。
- 任务 04 `HANDOFF.status=complete`。
- Stage 7/8 兼容测试、适配器测试和生命周期测试全部通过。
- 已生成重构前后 pipeline 职责对照表。

## 输入

- 两个 pipeline、两个 release adapter 和共享 lifecycle
- 任务 01 的入口清单与固定字节快照
- 任务 03/04 的 `HANDOFF`
- 全仓库对相关名称的引用搜索结果

## 允许修改

- `src/ashare_multifactor/validation/pipeline.py`
- `src/ashare_multifactor/validation/release_adapter.py`
- `src/ashare_multifactor/robustness/pipeline.py`
- `src/ashare_multifactor/robustness/release_adapter.py`
- `src/ashare_multifactor/audit/reproducible_release.py`
- 仅删除或收缩已被等价接口测试覆盖的内部耦合测试

## 本任务不修改

- 现有外观函数名、签名、CLI 和返回合同；
- 研究计算、阶段门禁、血缘语义、seal 和 publication；
- 配置、数据、历史产物、指针；
- Stage 6、Stage 9。

## 删除判据

只有同时满足以下条件的代码才允许删除：

1. 它实现的是双运行、清单、比较、资格、证书或源运行复验等共享编排；
2. 新生命周期已经执行同一职责；
3. Stage 7/8 特征测试证明外部行为等价；
4. 全仓库引用搜索证明没有未迁移调用者；
5. 删除后阶段适配器仍独立拥有阶段门禁和领域语义。

## 执行步骤

- [x] 为两个 pipeline 画出“保留外观 / 移入适配器 / 由生命周期取代 / 保留领域函数”四类清单。
- [x] 逐段删除重复的 run-directory 创建与失败清理，只保留生命周期实现。
- [x] 逐段删除重复的两运行调度、代码/输入比较、核心哈希比较和发布资格判定。
- [x] 逐段删除重复的证书落盘与认证源运行复验。
- [x] 保留 pipeline 中所有兼容外观；薄外观只做参数转发和必要错误翻译。
- [x] 保留 Stage 7/8 阶段门禁、核心路径声明、输入身份、文件映射、lineage 和 seal 的单一阶段归属。
- [x] 删除只断言旧私有调用顺序的测试之前，指出替代它的生命周期或外观测试。
- [x] 用全仓库搜索确认没有调用已删除私有名称；文档中的历史名称无需改写，除非它声称当前结构。
- [x] 运行相关测试、完整 pytest 和 Ruff。
- [x] 比较任务 01 固定快照，确认字节和错误合同未漂移。

## Pipeline 职责四分类

| 分类 | Stage 7 | Stage 8 |
|---|---|---|
| 保留外观 | `finalize_validation_run`、`compare_validation_outputs`、`certify_full_validation_runs`、`execute_full_validation_run`、`execute_validation_reproducibility`、`stage_validation_release`、`run_validation_release`、`validate_run_id` | `finalize_robustness_run`、`compare_robustness_runs`、`execute_robustness_run`、`execute_robustness_reproducibility`、`publish_robustness_release`、`verify_reproducible_source` |
| 移入适配器 | 研究执行、上游/输入门禁、运行时审计、98 文件映射、lineage、predecessor 复查 | 稳健性实验、协议/市场门禁、3 文件映射、seal、successor lineage/audit |
| 由生命周期取代 | run-directory 创建与失败清理、清单编码、代码/输入漂移复查、双运行、核心哈希比较、资格与证书、认证源复验、发布调用 | 同左 |
| 保留领域函数 | 最终测试日期封存、Stage 4/5/6 与 validation daily-panel 身份、运行时 gate、发布血缘构造 | 分析期封存、实验/报告、validation market scope、协议 gate、successor contract |

核心路径只在各阶段 pipeline 的既有常量中声明；生命周期兼容配置惰性读取这些常量，不维护第二份清单。

## 旧测试替代映射

| 删除或收缩的旧断言 | 等价覆盖 |
|---|---|
| Stage 7 adapter 内部自行检查 code/input drift 的两个参数用例 | `test_run_once_rejects_binding_drift_and_cleans_the_run` 与公开外观 `test_stage7_execution_drift_cleans_new_run` |
| 直接调用 `_assert_validation_run_tree` 的私有树顺序断言 | `finalize_validation_run` 公开外观的 root symlink、nested symlink、extra-file、missing-file 固定错误测试，以及生命周期 strict-tree 测试 |
| 通过 `_certify_recorded_runs` monkeypatch 固定 pipeline 内部预认证顺序 | Stage 7 release 外观委托测试、生命周期 release 重建证书测试和固定证书字节测试 |
| pipeline 在 adapter bind 前自行检查 duplicate run-directory | 两阶段公开 execute 外观的 `FileExistsError` 兼容测试与生命周期 duplicate-directory 测试 |
| pipeline 自行 dirty 判定 | 两阶段 dirty+adapter 组合失败顺序回归与 lifecycle direct-caller dirty 回归；判定规则仅在生命周期 helper 中实现 |

## 验收

- [x] 共享双运行—认证—发布编排只有 `audit.reproducible_release` 一个实现来源。
- [x] 两个 pipeline 仍保留全部已观察兼容入口。
- [x] 两个适配器各自只拥有阶段差异，不复制生命周期。
- [x] 删除的每个旧测试都有明确等价覆盖映射。
- [x] `audit.publication` 零变化。
- [x] 完整 pytest 和 Ruff 通过。
- [x] 固定字节、错误合同和 CLI 回归通过。
- [x] 真实发布、指针和最终测试封存状态零变化。

## 失败关闭

- 若无法证明某段代码没有调用者，保留为兼容外观并在 `HANDOFF` 标记残余，不冒险删除。
- 若去重导致适配器出现第四项能力或任意回调集合，恢复该删除并回到边界评审。
- 若全量测试失败且无法定位为本任务局部问题，停止并保留最小失败证据，不进入任务 06。

# 强制停点

去重和全量代码测试完成后停止。不得进行真实产物影子复制、暂存或任何发布动作。

## HANDOFF

```yaml
task: 05-remove-duplication
status: complete
baseline_handoff: 04-migrate-stage8
files_changed:
  - src/ashare_multifactor/audit/reproducible_release.py
  - src/ashare_multifactor/validation/pipeline.py
  - src/ashare_multifactor/validation/release_adapter.py
  - src/ashare_multifactor/robustness/pipeline.py
  - tests/test_reproducible_release_lifecycle.py
  - tests/test_validation_release_adapter.py
  - tests/test_validation_release_compatibility.py
  - tests/test_robustness_release_compatibility.py
removed_choreography:
  - pipeline run-directory creation, duplicate checks and failure cleanup
  - pipeline run-manifest encoding and core-hash comparison
  - pipeline two-run scheduling, eligibility and certificate writing
  - pipeline certified-source selection and revalidation
  - pipeline release pre-certification and publication invocation
  - Stage-7 adapter duplicate code/input drift checks
retained_facades:
  - all Task-01 observed Stage-7 and Stage-8 pipeline call surfaces
  - legacy signatures, return contracts, CLI paths and error translations
retained_stage_differences:
  - Stage-7 98-file closed tree, safe-slug, resource usage, date and runtime gates
  - Stage-8 3-file loose tree, legacy nested IDs, no resource usage, protocol and successor gates
  - stage-owned core paths, input identities, mappings, lineage and seal
test_replacement_map:
  - adapter drift order -> lifecycle drift cleanup plus public Stage-7 execute drift
  - private validation tree -> public finalize facade plus lifecycle strict-tree coverage
  - pipeline recorded certification -> lifecycle release rebuild plus fixed-byte coverage
  - pipeline dirty eligibility -> shared lifecycle helper plus two public ordering regressions
full_pytest: 1596 passed, 1 skipped
ruff: passed
current_pointers_unchanged: true
next_task: 06-verify-and-handoff
next_task_authorized: false
```
