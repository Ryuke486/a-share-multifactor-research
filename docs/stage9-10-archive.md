# Stage 9–10 延期归档

## 决策

自 2026-08-03 起，Stage 9–10 不再阻塞“研究与验证完成版 v1.0”，但保留为未来 v2.0 的可选路线。此调整缩小交付范围，不降低既有数据正确性门禁，也不把失败 attempt 改写为成功。

## v1.0 中明确不做的事项

- 不读取、统计或绘制 2022–2025 策略结果；
- 不创建新的最终测试 token、attempt 或 opening-ledger 条目；
- 不恢复官方证据采集、候选审核或 coverage 发布；
- 不执行同 attempt `resume`；
- 不手工创建 `processed/final_test/CURRENT.json`；
- 不执行检查点 C，也不宣称原总项目的完整标准已经满足。

## 保留的审计事实

- Stage 9 曾完成多轮协议、证据工作流、采集器和路由修复，但没有形成权威最终测试 release。
- 最新保留的失败 attempt 之一为 `stage9-final-20260803-a1076c2`；其 base evidence 导入已产生不可变 report/receipt，但控制层重放撞上独占路径，因此按治理失败关闭。
- 该失败 attempt 没有生成候选 snapshot、审核 batch、ready coverage、最终测试 `CURRENT.json` 或 `resume` 结果。
- 平衡空间清理只移除了重复的大型证据副本；attempt registry、authorization、HANDOFF、事故记录、Git 分支和提交历史仍保留。

## 未来恢复条件

如用户未来明确启动 v2.0，应视为新范围，并至少重新核对：

1. 当前 Git commit/tree、Stage 7/8 release 与 sealed protocol；
2. 失败 attempt、opening ledger、token 消耗和证据根身份；
3. 目标证券范围、官方查询/文档缓存、候选快照和人工审核覆盖；
4. 完整 field-level reconciliation 与成对 `ready` execution coverage；
5. 新的精确授权、一次性 token 和 successor attempt；
6. 同 attempt `resume`、权威最终测试 release、`CURRENT.json` 和检查点 C。

任何未来恢复都不得在已失败 attempt 上补写成功 receipt、覆盖不可变缓存、复用已消费 token，或把 v1.0 的范围调整解释为 Stage 9 已通过。

## 2026-09-11 历史证据存储调整

三个历史证据/事故目录已执行去重冷归档，原路径保留 `COLD_ARCHIVE.md` 占位说明。归档不改变失败状态、证据有效性、封存或授权限制。历史事故重入核验及未来 v2.0 工作前，必须先按[冷归档与恢复说明](evidence-archive-2026-09-11.md)恢复完整历史路径。当前 Stage 5–8 权威发布继续在线保留。

## 2026-10-05 代码存档

延期最终测试的代码已从 main 移除，以免占源码约 63% 的延期功能掩盖 v1.0 的研究主体。移除只涉及代码与测试，不改变任何失败状态、证据、release、`CURRENT.json`、attempt、token 或授权限制。

- **存档位置**：Git 标签 `archive/stage9-final-test`，指向 `d7c2828`（移除前最后一个包含该代码的 main 提交），已推送到 GitHub。
- **移除范围**：`src/ashare_multifactor/final_test/`（91 个模块）、`cli/final_test.py`、`cli/final_test_review.py`、`ashare-final-test` 命令；`ashare-robustness` 的 `rehearse-evidence` 与 `publish-evidence-successor` 命令及只为它们服务的 4 个 `robustness` 模块；对应测试；仅被该代码使用的 `pypdf` 依赖。精确清单见移除提交与[任务计划](superpowers/plans/2026-10-05-archive-final-test-code.md)。
- **保留**：Stage 8 流水线、发布适配、封印与审计读取代码；`protocol_identities.py` 中的历史路径清单（冻结合同，不改写）；Stage 8 复现仍读取的 `configs/final_execution_sources.yaml` 与报告模板。

### 恢复步骤

1. 在 main 上对移除提交执行 `git revert`；被移除的文件会逐字节恢复为 `d7c2828` 的版本（移除前已在临时工作树演练，完整测试套件通过）。
2. 重新安装依赖（恢复后的 `pyproject.toml` 与锁定文件重新包含 `pypdf`）。
3. 按本文"未来恢复条件"逐项核对后，才能考虑新的授权与 attempt。

### 封存门禁的现状

`final_test/gate.py` 的开启授权会逐一核对 Stage 8 封印中 227 个源码文件的哈希、Python 版本和依赖版本。2026-10-05 核对时，main 在移除代码之前就已无法通过：

- 6 个被封印源码已在 2026-09-11 的 `1a9feb1`、`cdf4f3d` 中改变；
- 运行环境已从 CPython 3.14.6 / polars 1.42.1 升级到 3.14.7 / 1.43.2，numpy、scipy、matplotlib 也有变化。

2026-09-11 边界验收所说的"不需要新封印"只针对三个域身份的变更影响分类，没有覆盖门禁的整树核对。因此，未来开启最终测试需要以下二者之一，且都需要独立授权：

- 在恢复代码后发布新的 Stage 8 封印（successor seal），再按新封印开启；
- 回到封印提交 `a1076c2`，重建 CPython 3.14.6 与当时的依赖版本后在该树上开启。

不得通过改写旧封印、旧哈希或放宽门禁来绕过这一要求。
