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

- [ ] 新建干净环境是否能仅凭版本化文件完成安装，没有依赖旧 .venv？
- [ ] 解释器与直接/传递依赖版本是否可追溯，平台范围是否清楚？
- [ ] 是否未自动升级研究依赖，未泄露绝对用户路径、令牌或私有源信息？
- [ ] 当前 Python 最低支持声明是否有测试依据，未验证范围是否披露？
- [ ] 安装导入、相关测试、完整 pytest 和 Ruff 是否在新环境通过？
- [ ] CI 是否只使用人工/可公开数据，不读取 Data、真实归档或最终测试结果？
- [ ] 工作流是否没有采集、发布、CURRENT 切换和凭据写入权限？
- [ ] 数字、链接和 manifest 检查是否能捕获人工制造的真实错误？
- [ ] 历史 v1.0 manifest 是否保持原字节，未被更新成新源码哈希？
- [ ] 文档命令与实际执行是否一致，本地通过与远端 CI 通过是否分开报告？

## 输出、总验收与强制停点

输出环境约束文件、工作流、必要检查实现/反例、复现说明和 docs/audits/ 验收报告。不要将临时环境和大日志加入 Git。

收口报告汇总四步：存储与恢复证据、候选集成状态、模块边界结果、环境与检查结果。注明每步真实状态、实际目标树、未解决项及逻辑/物理空间口径。所有代码改进未进入主仓库时，不能宣称主仓库已全部完成优化；远端未运行也不能写 CI 已通过。

本步骤完成后停止。后续提交、合并、推送、部署、发布、清理工作区和 v2.0 均按各自授权执行。

## HANDOFF（执行后更新）

```yaml
task: 04-reproducible-environment-and-checks
status: not_started
predecessor_handoff: 03-v1-boundaries.md
accepted_tree_identity: null
environment_constraints: null
fresh_environment_install: pending
full_pytest: pending
ruff: pending
delivery_checks: pending
ci_local_steps: pending
ci_remote_run: not_run
verification_report: null
four_step_summary: null
integration_status: pending
current_pointers_unchanged: null
final_test_remains_sealed: null
unresolved_items: []
next_authorized_task: null
```
