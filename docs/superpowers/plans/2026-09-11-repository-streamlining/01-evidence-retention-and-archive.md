# 步骤 1：历史证据保留、去重与归档验收

## 目标与当前状态

减少历史证据展开副本的占用，同时保留来源、失败记录、完整恢复能力及现行研究发布的可核验性。

2026-09-11 编写本任务时，[冷归档说明](../../../../docs/evidence-archive-2026-09-11.md)已记载归档执行完成，逻辑净减少约 12.97 GiB。因此本步骤应从**复核现有归档**开始，不重复打包或删除。该数字是已有记录，不是本任务新测量，也不代表 APFS 物理释放量。

本文及后续三份文件只定义任务；生成文件不授权执行。每次仅执行用户点名的一步。后续为[步骤 2](02-lifecycle-closeout.md)、[步骤 3](03-v1-boundaries.md)、[步骤 4](04-reproducible-environment-and-checks.md)。

## 输入与范围

- 完整阅读根目录 AGENTS.md、上述冷归档说明、docs/stage9-10-archive.md。
- 检查实际 Git/worktree 状态，保留其他任务已有修改。
- 现有归档位于 artifacts/evidence_archive/2026-09-11/；输入包括 manifest、directory_metadata、references、retention、verification、full_restore_verification、sources_reverified、retirement、releases_before/after 及恢复工具。
- 只处理原清单中的三个历史证据/事故目录；Data/、研究参数、在线发布、授权和尝试状态保持不变。
- 仅读取存储清单、字节及身份信息；不解析、统计或绘制最终测试期策略结果，不采集数据、不执行 resume。

## 执行流程

1. 核对归档、清单、恢复工具及已有回执是否齐全，记录路径、大小和 SHA-256。完成标准：每份证据可定位，缺失项明确列出。
2. 核对原文件数、唯一对象数、重复组和归档对象之间的对应关系。原路径占位文件只能说明存储位置，不能作为完整历史现场。完成标准：所有原路径都有可恢复对象；无悬空引用或未解释的对象缺失。
3. 复核引用及保留策略：现行发布、候选工作区、历史事故和未来恢复需求分别标明。文本搜索的局限必须保留，不能声称穷尽动态依赖。
4. 小夹具先验证恢复工具，再决定真实恢复复验范围。完整演练需约 25 GiB 以上空闲空间；先测可用空间和耗时预算。已有完整恢复回执若与当前归档和工具身份匹配，可复用并说明日期；身份不一致时必须重新验证，空间不足则停止在未验收状态。
5. 恢复只写入新的独立目录，拒绝覆盖。逐文件核对字节、原路径、权限、mtime、空目录及重复对象恢复后的独立文件身份；明确 ACL 等未纳入契约的元数据。
6. 用现有发布读取器核对 Stage 5–8 CURRENT、manifest、lineage 和全部清单文件；确认最终测试 CURRENT 仍不存在。核验基线与当前记录一致。
7. 若现有归档已合格，直接收口。若仍有待移除副本，先形成精确清单、保留引用判断和恢复证据；删除只有在本次用户明确授权覆盖该清单时执行。不得依据旧清理授权推断新删除权限。
8. 记录前后同口径大小；分别报告逻辑字节、目录占用和可用空间，无法测得物理释放量时直接注明。

## 做完必须检查的问题

- [x] 原清单每个文件和空目录是否都能恢复，哈希是否全部一致？
- [x] 当前归档是否与恢复验证回执绑定的是同一份字节，工具身份是否有记录？
- [x] 重复内容恢复后是否成为独立文件，避免修改一份污染另一份？
- [x] 损坏归档、越界路径、已有目标目录是否被拒绝，失败是否留下明确记录？
- [x] Stage 5–8 发布全部文件是否完好，CURRENT 是否逐字节未变？
- [x] Data/、授权、token、attempt 和最终测试封存状态是否保持原状？
- [x] 已结束事故的失败事实、来源证据和恢复说明是否完整保留？
- [x] 报告是否区分历史验收与本次复验、逻辑节省与物理释放？
- [x] 是否没有清理归档本体、唯一恢复工具或候选工作区未提交成果？

## 输出、验收与强制停点

在 docs/audits/ 中新增简短复核报告；大清单和回执留在 Git 忽略的本地证据目录，不复制历史市场数据到 Git。无研究代码变化时无需重跑全量回测；恢复工具如有 bug，先写人工反例测试，再修复和验证。

检查全部通过且证据路径完整才记为 complete。失败项保留为 incomplete；只依赖尚未核实的旧说明不能记为通过。完成后停止，不自动开始步骤 2，不提交、合并或推送。

## HANDOFF（执行后更新）

```yaml
task: 01-evidence-retention-and-archive
status: complete
existing_archive_record: docs/evidence-archive-2026-09-11.md
current_verification_report: docs/audits/2026-09-11-evidence-archive-review.md
archive_sha256: 2d2f574c319ff4dbb32b8fbd3e82c0d4ce2fbc30027ff15f8141a09a72871d55
restore_verification: passed; full 467112-file restore repeated with tool hash binding
release_integrity: passed; 183 files across Stage 5-8
current_pointers_unchanged: true
final_test_remains_sealed: true
logical_bytes_saved: 13930640831 # historical approximate reduction; no new deletion
physical_space_measurement: unavailable; free-space observations in review JSON
unresolved_items: []
next_authorized_task: null
```
