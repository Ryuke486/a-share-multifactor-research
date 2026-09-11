# 步骤 3：理清 v1.0、共享审计与延期功能的边界

## 目标与前置条件

降低理解和维护成本，使 v1.0 日常流程与最终测试专属编排具有清晰边界，同时保持历史发布可读、证据可追溯及封存门禁有效。

前置：[步骤 2](02-lifecycle-closeout.md)已给出明确验收目标树，用户明确启动本步骤。后续为[步骤 4](04-reproducible-environment-and-checks.md)。代码行数不是删减指标；不能直接删除 final_test/。

## 输入与范围

- 完整阅读 AGENTS.md、CONTEXT.md、docs/stage9-10-archive.md、Stage 8 计划及相关身份/封印合同。
- 检查 robustness/evidence_workflow_rehearsal.py 对 final_test 的引用，以及 robustness/protocol_identities.py 中绑定的源码路径。
- 2026-09-11 初查发现 final_test 占源码约 65%；执行时重新统计。该比例只说明关注点，不证明每个模块都应迁移。
- 本步骤只改代码组织、必要兼容接口、测试和说明；研究样本、因子、方向、成本、组合规则、来源证据和在线 release 保持原样。

## 执行流程

1. 从步骤 2 验收目标树建立隔离基线，记录代码身份、环境和当前发布指针。确认原有改进已包含在基线中。
2. 盘点 import、CLI、动态引用、配置路径、源码身份清单和历史 lineage；区分 v1.0 计算、共享证据审计、最终测试专属编排。完成标准：每个拟移动文件都有调用者和身份绑定清单。
3. 先形成精确改动表：现路径、职责、目标位置、旧接口兼容方式、受影响测试、历史身份处理。只迁移有明确共享需求的能力，不增加通用插件框架。
4. 对旧接口、发布回读和封存边界补充必要特征测试。使用人工夹具证明普通 v1.0 流程不会调用最终测试采集、授权、attempt 创建或执行入口。
5. 在隔离树中进行最小职责调整，旧导入路径通过兼容外观保留；共享层不依赖最终测试编排层。同步调整测试分组与 v1.0 文档导航。
6. 历史身份必须继续按原合同验证：不通过重写旧哈希、lineage 或放宽门禁获得通过。若调整必然需要新封印/新协议，停止该部分实施，交付具体冲突及方案，等待独立授权；不在本步骤发布 successor。
7. 先运行受影响模块、CLI 和边界测试，再完整 pytest、Ruff。无真实数据和无网络也应完成人工夹具检查。复验历史发布回读和封存状态。
8. 对比改动前后的依赖关系、公共入口和行为证据，解释哪些维护成本减少了。只拆文件但耦合没有改善，不视为完成。

## 做完必须检查的问题

- [ ] 每个迁移模块是否有明确理由，是否遗漏动态引用或源码身份绑定？
- [ ] v1.0 必需的证据能力是否仍可用，旧导入和 CLI 是否兼容？
- [ ] 共享层是否摆脱最终测试编排依赖，是否没有新增循环依赖？
- [ ] 人工反例是否证明 v1.0 入口不会创建 token、attempt 或触发最终测试执行？
- [ ] 研究计算、排序、参数和数据可得时间是否保持一致？
- [ ] 历史 release、封印和血缘是否仍按原规则可核验？
- [ ] 是否没有改旧哈希、放宽校验或改写失败记录来适配新结构？
- [ ] 新旧接口测试、相关回归、完整 pytest 和 Ruff 是否通过？
- [ ] 文档是否明确共享能力与延期功能边界，没有宣称 Stage 9 已完成？
- [ ] 改动是否实际减少跨层依赖，而不是仅增加文件、包装或抽象？

## 输出、验收与强制停点

输出精确文件清单、调整前后依赖说明、测试证据和 docs/audits/ 验收报告。把新规则写入最小必要文档，保留历史任务记录。

兼容、依赖边界、历史回读、全量验证均通过才记为 complete。协议身份冲突未解决时记为 incomplete，清楚列出已完成部分。完成后停止；不提交、合并、推送、发布或启动步骤 4，除非会话已有对应明确授权。

## HANDOFF（执行后更新）

```yaml
task: 03-v1-boundaries
status: not_started
predecessor_handoff: 02-lifecycle-closeout.md
accepted_tree_identity: null
dependency_review: null
verification_report: null
compatibility_tests: pending
boundary_tests: pending
full_pytest: pending
ruff: pending
historical_identity_compatibility: pending
current_pointers_unchanged: null
final_test_remains_sealed: null
integration_status: pending
unresolved_items: []
next_authorized_task: null
```
