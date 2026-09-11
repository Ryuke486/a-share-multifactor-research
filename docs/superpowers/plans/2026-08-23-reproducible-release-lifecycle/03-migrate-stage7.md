# 任务 03：通过适配器迁移 Stage 7

**状态：** 已完成
**服务总目标：** 让 Stage 7 首先成为共享生命周期的真实纵向切片，同时完整保留其严格运行树和发布语义。

## 前置条件

- 用户明确授权“开始任务 03”。
- 任务 02 `HANDOFF.status=complete`。
- 生命周期测试、任务 01 兼容测试和现有 Stage 7 测试均在当前 worktree 通过。

## 输入

- `src/ashare_multifactor/audit/reproducible_release.py`
- `src/ashare_multifactor/validation/pipeline.py`
- `src/ashare_multifactor/validation/full_run.py`
- `src/ashare_multifactor/validation/protocol.py`
- `src/ashare_multifactor/cli/validation.py`
- Stage 7 兼容测试与固定快照

## 允许修改

- 新增 `src/ashare_multifactor/validation/release_adapter.py`
- 局部修改 `src/ashare_multifactor/validation/pipeline.py`，把已覆盖入口变为兼容外观
- 新增或局部修改 Stage 7 release adapter 测试
- 仅在委托不改变 CLI 时修改 `src/ashare_multifactor/cli/validation.py`

## 本任务不修改

- Stage 7 研究计算、因子、组合、回测和报告逻辑；
- `robustness/`；
- `audit.publication` 与 `audit/__init__.py`；
- 配置、数据、历史运行、历史 release 和真实指针；
- Stage 6、Stage 9。

## Stage 7 适配器职责

### `bind`

- 解析真实数据根；
- 执行 Stage 6 上游发布门禁；
- 捕获现有 `_validation_input_identity` 的完整身份；
- 捕获代码身份和验证配置；
- 选择 Stage 7 v1 兼容配置；
- 保留前任发布和发布中漂移复查所需保护条件。

### `execute`

- 调用现有 `execute_validation_stages`；
- 提供 `resource_usage` 所需的耗时与最大 RSS；
- 不吸收任何 Stage 7 领域计算。

### `prepare_release`

- 重新执行封存日期、runtime audit 和上游发布门禁；
- 重新核验冻结输入和认证源运行；
- 保持 98 文件到发布树的现有映射；
- 生成既有 validation lineage 和前任信息；
- 返回 `audit.publication.publish_release` 所需参数。

## 执行步骤（TDD）

- [x] 先让适配器测试描述 Stage 7 三能力的输入、输出和失败边界。
- [x] 实现适配器，不移动 `full_run.py` 中的研究计算。
- [x] 让 `execute_full_validation_run` 委托 `run_once`，保持签名、返回 `Path`、safe-slug、错误和失败清理。
- [x] 让 `execute_validation_reproducibility` 委托 `reproduce`，保持证书路径、schema、字节和返回字典。
- [x] 让 `stage_validation_release` 与 `run_validation_release` 委托 `release`/适配器准备，保持 `publish` 分支、返回类型和前任漂移门禁。
- [x] 保留 `finalize_validation_run`、`certify_full_validation_runs`、`compare_validation_outputs`、`validate_run_id` 及其他已观察名称在原导入路径；可作为薄外观，不要求调用方迁移。
- [x] 逐项证明 98 文件、封闭树、符号链接、额外文件、日期列、safe-slug、`resource_usage`、runtime audit 和输入身份未变。
- [x] 在固定身份夹具上比较重构前快照与重构后实际字节。
- [x] 运行 Stage 7 适配器、兼容、pipeline、CLI、publication 相关测试和 Ruff。

## 验收

- [x] Stage 7 的 run/reproduce/stage/publish 现有入口均通过生命周期或适配器委托。
- [x] CLI 命令和参数零变化。
- [x] 固定身份下的运行清单、复现证书、暂存映射和 lineage 字节一致。
- [x] 异常类型与已固定消息一致。
- [x] Stage 7 领域模块无无关改动。
- [x] Stage 8 代码零改动且测试继续通过。
- [x] 不执行真实全量 Stage 7 run，不发布、不切指针。
- [x] 相关测试和 Ruff 通过。
- [x] 最终测试指针仍不存在。

## 失败关闭

- 任一固定字节或错误合同变化时，先恢复外观兼容；不得更新快照来适应新行为。
- 若某段逻辑同时包含共同编排和 Stage 7 门禁，优先整体留在适配器，本任务不追求最大删除量。
- 若迁移要求修改研究计算结果，停止并把它登记为候选 1 之外的问题。

# 强制停点

Stage 7 迁移验收后停止。不得开始 Stage 8 适配器或跨阶段去重。

## HANDOFF

```yaml
task: 03-migrate-stage7
status: complete
baseline_handoff: 02-build-lifecycle-core
files_changed:
  - src/ashare_multifactor/validation/release_adapter.py
  - src/ashare_multifactor/validation/pipeline.py
  - tests/test_validation_release_adapter.py
facades_preserved:
  - execute_full_validation_run
  - execute_validation_reproducibility
  - stage_validation_release
  - run_validation_release
  - finalize_validation_run
  - certify_full_validation_runs
  - compare_validation_outputs
  - validate_run_id
stage7_differences_verified:
  - 98 core files and closed-tree, symlink, extra-file and missing-file gates
  - safe-slug, resource_usage, sealed dates, runtime audit and frozen input identity
  - fixed manifest, certificate, staging mapping and lineage bytes
  - legacy exception types, messages, failure cleanup and predecessor drift gate
byte_compatibility: true
verification:
  - Stage 7 adapter suite: 20 passed
  - related lifecycle, Stage 7, Stage 8 and publication suite: 177 passed
  - full pytest: 1588 passed, 1 skipped
  - ruff check .: passed
  - git diff --check: passed
  - two-axis standards and spec review: no remaining findings
stage8_changed: false
current_pointers_unchanged: true
final_test_current_absent: true
next_task: 04-migrate-stage8
next_task_authorized: false
```
