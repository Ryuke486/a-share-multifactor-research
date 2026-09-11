# 任务 04：通过适配器迁移 Stage 8

**状态：** 已完成
**服务总目标：** 让 Stage 8 复用同一生命周期，同时证明共享不等于抹平其较小证书、协议封印和 successor 发布差异。

## 前置条件

- 用户明确授权“开始任务 04”。
- 任务 03 `HANDOFF.status=complete`。
- 生命周期、Stage 7 和 Stage 8 基线测试在当前 worktree 通过。

## 输入

- `src/ashare_multifactor/audit/reproducible_release.py`
- `src/ashare_multifactor/robustness/pipeline.py`
- `src/ashare_multifactor/robustness/protocol.py`
- `src/ashare_multifactor/robustness/test_protocol.py`
- `src/ashare_multifactor/robustness/successor_seal.py`
- `src/ashare_multifactor/cli/robustness.py`
- Stage 8 兼容测试与固定快照

## 允许修改

- 新增 `src/ashare_multifactor/robustness/release_adapter.py`
- 局部修改 `src/ashare_multifactor/robustness/pipeline.py`，把已覆盖入口变为兼容外观
- 新增或局部修改 Stage 8 release adapter 测试
- 仅在委托不改变 CLI 时修改 `src/ashare_multifactor/cli/robustness.py`

## 本任务不修改

- 稳健性实验、因子删除、分段、汇总和报告的领域算法；
- Stage 7 已验收语义；
- evidence-workflow successor 的研究范围与门槛；
- `audit.publication`、配置、数据、历史发布和真实指针；
- Stage 6、Stage 9。

## Stage 8 适配器职责

### `bind`

- 解析 robustness 数据根和预注册协议；
- 解析并核验当前 validation release 与冻结主候选；
- 执行输入期间、市场范围和读取门禁；
- 捕获现有 `_robustness_input_identity` 与代码身份；
- 选择 Stage 8 v1 兼容配置；
- 接收并核验成对出现的可选 successor audit 根。

### `execute`

- 调用现有稳健性实验、长表、协议门禁和报告生成；
- 不添加 `resource_usage`；
- 不引入 Stage 7 的封闭树或 safe-slug 行为。

### `prepare_release`

- 重建或重读复现证书并重新核验第二运行；
- 复验 `protocol_gate`、validation market scope 和当前 validation pointer；
- 保持 3 个核心文件映射；
- 保持 successor audit/collector readiness 成对门禁；
- 生成既有 Stage 8 seal、lineage 和 successor lineage；
- 返回既有发布原语参数。

## 执行步骤（TDD）

- [x] 先写 Stage 8 适配器三能力的成功和失败测试。
- [x] 实现适配器，不移动 `_execute_experiments` 等领域计算。
- [x] 让 `execute_robustness_run` 委托 `run_once`，保持签名、返回路径和当前运行 ID 行为。
- [x] 让 `execute_robustness_reproducibility` 委托 `reproduce`，保持证书路径、schema、字节和返回字典。
- [x] 让 `publish_robustness_release` 委托 `release`/适配器准备，保持 successor 参数、返回对象和既有发布原语调用。
- [x] 保留 `finalize_robustness_run`、`compare_robustness_runs`、`verify_reproducible_source` 及其他已观察名称在原导入路径。
- [x] 逐项证明 3 文件核心集合、无 `resource_usage`、现有目录/ID 行为、协议门禁、市场门禁、seal 和 successor audit 语义未变。
- [x] 明确反例：Stage 8 不得因共享生命周期而拒绝当前允许的额外目录行为或套用 Stage 7 safe-slug。
- [x] 在固定身份夹具上比较重构前快照与重构后实际字节。
- [x] 运行 Stage 8 适配器、兼容、pipeline、sealed protocol、successor、CLI、Stage 7 回归和 Ruff。

## 验收

- [x] Stage 8 run/reproduce/publish 入口通过生命周期或适配器委托。
- [x] CLI 命令、参数和 successor 分支零变化。
- [x] 固定身份下运行清单、复现证书、seal 和 lineage 字节一致。
- [x] Stage 8 没有获得 Stage 7 的 `resource_usage`、safe-slug 或封闭树规则。
- [x] 异常类型与已固定消息一致。
- [x] Stage 7 全部测试继续通过。
- [x] 不执行真实全量 Stage 8 run，不封印新协议、不发布、不切指针。
- [x] 相关测试和 Ruff 通过。
- [x] 最终测试指针仍不存在。

## 失败关闭

- 若共享编码器迫使 Stage 8 采用 Stage 7 schema 或规则，停止并修正兼容配置边界，不修改历史快照。
- 若 successor 发布需要第四个适配器能力，先把其逻辑归入 `prepare_release`；仍不成立则返回设计评审。
- 若迁移改变 sealed protocol 字节或 opening ledger 语义，立即停止，不生成新 seal。

# 强制停点

Stage 8 迁移验收后停止。不得删除旧重复编排，不得开始最终影子验证。

## HANDOFF

```yaml
task: 04-migrate-stage8
status: complete
baseline_handoff: 03-migrate-stage7
files_changed:
  - src/ashare_multifactor/robustness/release_adapter.py
  - src/ashare_multifactor/robustness/pipeline.py
  - tests/test_robustness_release_adapter.py
  - tests/test_robustness_release_compatibility.py
facades_preserved:
  - execute_robustness_run
  - execute_robustness_reproducibility
  - publish_robustness_release
  - finalize_robustness_run
  - compare_robustness_runs
  - verify_reproducible_source
stage8_differences_verified:
  - 3 core files, loose run tree and legacy nested run IDs
  - no resource_usage, no safe-slug and no Stage 7 closed-tree rules
  - fixed run manifest, reproducibility certificate, seal and lineage bytes
  - protocol, period, validation candidate and market-scope gates
  - paired successor action-coverage and collector-readiness audit contract
  - legacy exception types, messages, duplicate-run and failure cleanup behavior
byte_compatibility: true
sealed_protocol_changed: false
verification:
  - Stage 8 adapter and compatibility suite: 25 passed
  - lifecycle, Stage 7, Stage 8, protocol, successor and CLI regression: 119 passed
  - full pytest: 1596 passed, 1 skipped
  - ruff check .: passed
  - git diff --check: passed
  - post-change successor adapter assertions: 8 passed
current_pointers_unchanged: true
next_task: 05-remove-duplication
next_task_authorized: false
```
