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
