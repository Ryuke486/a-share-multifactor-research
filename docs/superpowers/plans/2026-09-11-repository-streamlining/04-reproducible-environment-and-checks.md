# 步骤 4：固定复现环境与自动质量检查

## 目标与前置条件

让新环境能够安装经过验证的依赖、运行无真实数据测试，并自动发现环境漂移、接口破坏和交付不一致。

前置：[步骤 3](03-v1-boundaries.md)通过并记录验收目标树，用户明确启动本步骤。如用户调整顺序允许先做本步骤，须记录实际基线和未完成依赖，不擅自假定步骤 3 已通过。

## 输入与范围

- 完整阅读 AGENTS.md、pyproject.toml、README.md、docs/reproduction.md、现行交付 manifest 和前序 HANDOFF。
- 使用实际已验证环境确定依赖，不把历史记录中的版本直接当作当前版本。
- 2026-09-11 初查未发现统一锁定环境和 .github 工作流；执行前重查，已有实现优先复用。
- 改动限于环境约束、安装验证、无真实数据自动检查及必要文档。Data/、公告缓存、归档和权威发布不加入 Git 或 CI。

## 执行流程

1. 记录已验收树、解释器、全部依赖、测试命令及可复现发布中的历史环境信息。检查最低 Python 支持声明与实际支持范围。
2. 保留合理的库兼容范围，另提供精确的研究复现环境约束（含必要传递依赖）。选择现有工具可维护的最简单方案；避免为了锁定环境升级依赖或引入重复运行时。清除导出中的本机绝对路径、私有地址和凭据。
3. 使用 Homebrew 已安装解释器在新临时环境安装验证，保留工作环境。说明平台/版本边界；单平台约束不冒称跨平台锁文件。缺少匹配运行时则记录未验证范围，不覆盖旧环境。
4. 新环境依次运行安装/导入检查、相关人工测试、完整 pytest 和 Ruff。验证时显式记录实际导入文件，避免 editable install 或 PYTHONPATH 指向旧工作区。
5. 新增或完善 GitHub Actions 配置：检出、安装精确依赖、运行人工夹具与静态检查。至少验证冻结环境；若继续声明 Python 3.12+，安排最低版本兼容测试或明确未验证状态。限制工作流权限，不启用发布、采集或研究重跑。
6. 增加有实际价值的交付检查：本地 Markdown 链接、报告关键数字与已跟踪机器结果的对应、交付 manifest 的既有哈希口径。真实 release 核验保留为本地层，CI 不依赖本地大数据。
7. 对数字不符、链接缺失或哈希不匹配使用人工反例确认检查确实失败。冻结 v1.0 manifest 不为消除失败而改写；区分历史发布验证与新工作树验证，新增交付版本需要单独授权。
8. 同步 README、复现说明、依赖文件和自动检查命令。完整研究重建、无数据快速验证、历史发布回读分别写清。
9. 本地验证 CI 各步骤；未经推送授权只交付工作流文件，远端状态记录为 not_run。存在远端运行授权且确实触发后，才记录实际结果链接与状态。

## 做完必须检查的问题

- [x] 新建干净环境是否能仅凭版本化文件完成安装，没有依赖旧 .venv？——`requirements-reproducible.txt` 加上 `pip install --no-deps -e ".[dev]"` 在两个全新解释器安装成功，导入路径来自当前工作树而非旧 `.venv`。
- [x] 解释器与直接/传递依赖版本是否可追溯，平台范围是否清楚？——CPython 3.14.7 / macOS arm64 基线，23 个包精确版本，明确声明不含下载哈希、非跨平台锁文件。
- [x] 是否未自动升级研究依赖，未泄露绝对用户路径、令牌或私有源信息？——锁定值等于现有 `.venv` 已装版本；导出剔除了 editable 的 GitHub 行与 pip，仅保留 Homebrew 工具路径。
- [x] 当前 Python 最低支持声明是否有测试依据，未验证范围是否披露？——在 CPython 3.12.14 上按同一锁定文件安装并运行完整 pytest；未验证平台已在文档中披露。
- [x] 安装导入、相关测试、完整 pytest 和 Ruff 是否在新环境通过？——两个新环境的 pytest 与 Ruff 结果见验收报告第 3 节。
- [x] CI 是否只使用人工/可公开数据，不读取 Data、真实归档或最终测试结果？——工作流只运行合成测试、静态检查与交付检查，检出中不存在 `Data/`、`processed/`、`artifacts/`。
- [x] 工作流是否没有采集、发布、CURRENT 切换和凭据写入权限？——`permissions: contents: read`，无发布、采集、研究重跑或密钥写入步骤。
- [x] 数字、链接和 manifest 检查是否能捕获人工制造的真实错误？——20 项测试含 7 类人工反例，全部按预期失败，见验收报告第 6 节。
- [x] 历史 v1.0 manifest 是否保持原字节，未被更新成新源码哈希？——`releases/` 无改动，差异按提示报告，未改写冻结 manifest。
- [x] 文档命令与实际执行是否一致，本地通过与远端 CI 通过是否分开报告？——README 与复现说明的命令均实际执行；远端状态记为 `not_run`。

## 输出、总验收与强制停点

输出环境约束文件、工作流、必要检查实现/反例、复现说明和 docs/audits/ 验收报告。不要将临时环境和大日志加入 Git。

收口报告汇总四步：存储与恢复证据、候选集成状态、模块边界结果、环境与检查结果。注明每步真实状态、实际目标树、未解决项及逻辑/物理空间口径。所有代码改进未进入主仓库时，不能宣称主仓库已全部完成优化；远端未运行也不能写 CI 已通过。

本步骤完成后停止。后续提交、合并、推送、部署、发布、清理工作区和 v2.0 均按各自授权执行。

## HANDOFF（执行后更新）

```yaml
task: 04-reproducible-environment-and-checks
status: complete
predecessor_handoff: 03-v1-boundaries.md
accepted_tree_identity: main local working tree based on 1a9feb196ac659edecbe8a271e9e5abe168d00b3 with the step-03 result integrated byte-identically; step-04 file list and hashes in the verification manifest
environment_constraints: requirements-reproducible.txt (23 third-party pins, CPython 3.14.7 / macOS arm64, no download hashes); requirements-verified.txt keeps the direct pins; pyproject.toml keeps the compatible ranges with requires-python >=3.12
fresh_environment_install: passed on temporary CPython 3.14.7 and CPython 3.12.14 environments outside the repository; installed from the versioned files only and imported from the current working tree
full_pytest: temporary CPython 3.14.7 environment 1649 passed, 1 skipped in 466.44s (exit 0); temporary CPython 3.12.14 environment 1649 passed, 1 skipped in 466.45s (exit 0); main worktree 1629 passed, 1 skipped in 447.91s (run before the 20 delivery tests were added)
ruff: passed in the main worktree and in both temporary environments
delivery_checks: passed; markdown 72 links in 68 documents; 51 documented results with 118 citations; published manifest verified against commit 1e7eddbe8a23; 51 values re-read from the local releases
ci_local_steps: passed (lock install, pytest, ruff, git diff --check, delivery check); the same commands are configured in .github/workflows/ci.yml
ci_remote_run: not_run
verification_report: docs/audits/2026-09-11-v1-reproducible-environment.md
four_step_summary: docs/audits/2026-09-11-v1-reproducible-environment.md section 10
integration_status: integrated_locally_uncommitted (steps 03 and 04)
current_pointers_unchanged: true
final_test_remains_sealed: true
unresolved_items:
  - remote CI has not been dispatched; only local step execution is verified
  - steps 03 and 04 are integrated but uncommitted; the new versioned files must be committed before the workflow can run
  - a non-editable install cannot run the test suite (161 failures from repository-root resolution); CI and the documented install stay editable
  - the frozen v1.0 manifest still describes its publication commit; a new delivery version needs separate authorization
  - three step-03 documents deviate from the step-03 manifest after integration and correction, with reasons and hashes recorded in the verification report
  - delivery checks do not detect newly added, unrecorded numbers
  - candidate worktrees and the stale artifacts temporary directory are not cleaned up
next_authorized_task: null
```
