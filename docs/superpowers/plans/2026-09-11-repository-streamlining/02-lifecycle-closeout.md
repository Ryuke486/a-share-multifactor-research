# 步骤 2：收口已有可复现发布生命周期改进

## 目标与前置条件

复核并交付候选 1 已实现的共享生命周期，减少 Stage 7/8 重复编排，消除主仓库与候选工作区状态不一致造成的维护风险。

前置：[步骤 1](01-evidence-retention-and-archive.md)验收完成，且用户明确启动本步骤。后续为[步骤 3](03-v1-boundaries.md)。本文件不授权提交、合并、推送、发布或切换 CURRENT。

## 输入与范围

- 完整阅读 AGENTS.md、CONTEXT.md、候选 1 权威设计，以及候选工作区中 01–06、06R、06S 任务及 HANDOFF。
- 候选工作区：.worktrees/reproducible-release-lifecycle-candidate1/。
- 阅读该工作区 docs/audits/ 的最终验收、外部来源修订和豁免证据；旧主仓库中的待授权状态不能覆盖较新的候选收口事实。
- 以实际文件为准盘点 tracked 和 untracked 成果。历史“1606 passed, 1 skipped”只作为定位线索，不能代替本次测试。
- 范围仅为已实现的共享生命周期、Stage 7/8 适配器、兼容外观、既有公司行动来源修复及配套测试文档；不追加新架构。

## 执行流程

1. 记录主仓库、候选工作区的 HEAD、分支、dirty 文件和依赖环境；明确本次集成基线。完成标准：所有待交付文件被列入清单，特别是 untracked 新模块和测试。
2. 对照原任务验收链确认候选完成状态，检查 06R 的失败事实和 06S 的后续修复均保留。完成标准：没有仅凭最终一句 complete 掩盖前置证据缺口。
3. 建立候选改动与目标基线的差异清单。复用安全的隔离工作区；存在无关修改时保留并排除，不覆盖。需要兼容修复时先添加失败反例。
4. 检查共享层职责：run_once、reproduce、release 统一编排；适配器保留 bind、execute、prepare_release；计算和阶段门禁仍由阶段模块负责。
5. 运行相关生命周期、适配器、兼容及公司行动测试，再运行完整 pytest 和 Ruff。显式指定隔离工作区的 src 与已验证项目解释器，防止导入主仓库旧代码；报告实际导入路径、版本和命令。
6. 用只读发布读取器复验历史 Stage 7/8 release。影子验证先检查已有回执与代码、输入身份：可以复用的说明依据；身份变化影响验收时，在隔离输出根执行必要影子认证，publish=false，保持在线发布不变。
7. 公司行动修复保留已批准来源修订及零消费层差异要求；原始历史字节不能靠重新下载假定恢复。需要网络数据时另行确定必要性，BaoStock 完全串行，不借本步骤重新采集。
8. 形成完整可审查交付清单、测试结果和集成说明。若用户未授权提交/合并，状态写为“实现验收完成，集成待授权”；若有明确授权，则按授权集成，并复验集成后的实际目标树。工作区清理只有成果已安全保存且另有授权时执行。

## 做完必须检查的问题

- [x] 新模块、新测试和审计文档是否全部纳入交付清单，没有漏掉 untracked 文件？
- [x] 公共函数、CLI、参数、返回值、旧错误和固定字节合同是否兼容？
- [x] Stage 7/8 核心文件集合、目录规则、资源记录等历史差异是否保留？
- [x] 独立双运行、输入/代码漂移检测、证书和发布资格是否仍有反例覆盖？
- [x] 共享编排是否只有一个实现来源，没有新增无用包装层？
- [x] 公司行动来源修复是否与已批准豁免一致，消费层差异是否为零？
- [x] 测试是否真正运行了候选/集成代码，完整 pytest、Ruff 是否通过？
- [x] CURRENT、历史 release 和归档是否保持完整，最终测试是否仍封存？
- [x] 旧失败记录是否保留，历史测试与本次测试是否清楚区分？
- [x] 报告是否准确区分“候选验收完成”和“已进入主仓库”？

原复核证据见 [步骤 02 收口报告](../../../../docs/audits/2026-09-11-candidate1-lifecycle-closeout.md)；06S 验证入口、步骤 01 报告、主仓库同步和环境决定的后续处理见 [整改报告](../../../../docs/audits/2026-09-11-candidate1-remediation.md)。

## 输出、验收与强制停点

新增 docs/audits/ 下的收口报告，引用原 HANDOFF，不改写历史失败事实。代码验收与实际集成分别记录。未经集成授权可以完成候选验收，但不得宣称主仓库已优化。

下一步必须使用明确记录的已验收目标树；若主仓库仍是旧实现，则在获得相应授权前不以旧主仓库启动后续重构。完成后停止，不自动开始步骤 3。

## HANDOFF（执行后更新）

2026-09-11 用户在步骤 02 收口后授权解决剩余问题并指定新环境。原 HANDOFF 对应的历史状态见原收口报告和清单；以下记录本次整改状态。

```yaml
task: 02-lifecycle-closeout
status: complete
predecessor_handoff: 01-evidence-retention-and-archive.md
accepted_tree_identity: main local working tree based on 1e7eddbe8a2319110f275d254b1a7702e41d1aea; exact files in remediation manifest
delivery_file_list: docs/audits/2026-09-11-candidate1-remediation-manifest.json
verification_report: docs/audits/2026-09-11-candidate1-remediation.md
full_pytest: 1622 passed, 1 skipped in 424.31s (0:07:04); exit 0
ruff: passed
historical_release_readback: passed; Stage 5-8 183 entries; Stage 7/8 certified run pairs and second sources verified read-only
shadow_verification: historical 06S receipt retained; mapping replay passed; no new real-input shadow staging or consumer regeneration
runtime: Python 3.14.7 / polars 1.43.2
runtime_identity_policy: retain historical certificates; new real runs require new identities and certificates
integration_status: integrated_locally_uncommitted
current_pointers_unchanged: true
final_test_remains_sealed: true
unresolved_items: []
limitations:
  - historical consumer equivalence is cited from 06S, not independently regenerated in this turn
next_authorized_task: null
```
