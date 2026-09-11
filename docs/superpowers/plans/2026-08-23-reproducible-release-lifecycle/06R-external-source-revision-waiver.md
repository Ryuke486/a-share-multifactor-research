# 任务 06R：外部数据修订豁免与消费层等价验证

**状态：** 历史执行已失败关闭；后续任务 06S 已完成整改并收口候选 1
**服务总目标：** 在不伪造 BaoStock 历史原始字节、不修改生产发布逻辑的前提下，判断供应商历史修订是否影响 Stage 7 实际消费数据；仅在消费层零差异时，为任务 06 提供一次验证专用的严格豁免证据。

## 授权与前置条件

- 用户已在 2026-08-26 明确授权“开始任务 06R”，批准此前提出的“外部数据修订豁免 + 消费层等价验证”路线。
- 任务 05 `HANDOFF.status=complete`。
- 任务 06 当前唯一阻塞是 BaoStock 原始联合表的新旧 SHA-256 不同；历史字节仍标记为不可恢复。
- 真实 Stage 7/8 发布、`CURRENT.json`、manifest、lineage 和最终测试封存状态必须保持不变。

## 冻结身份

- 历史 BaoStock 原始联合表：9516 × 16，177037 字节，SHA-256 `ffdfdc8dd0c4835009da53a69ad88c5984d34407d7612887867e4a3006048bab`。
- 2026-08-26 串行重取文件：9516 × 16，175512 字节，SHA-256 `435741913656864417d3619ff46df5fed587ec1b65515e56d47294cb01060d1e`。
- 历史认证运行：`stage7_controlled_65b19e1_1` 与 `stage7_controlled_65b19e1_2`。
- 历史消费表：两运行中的 `datasets/corporate_actions.parquet` 与 `datasets/security_events.parquet`。

上次新文件只存在于已清理的系统临时根，因此本任务必须重新取得真实新输入并重新计算；不得仅凭旧报告中的形状或哈希声称等价。

## 允许修改

- 本任务书、任务 06 HANDOFF 和候选 1 最终审计报告；
- 仅位于 `tests/` 的 06R 验证辅助代码与测试；
- 在系统临时目录创建独立恢复根、下载检查点、影子副本和审计输出。

## 本任务不修改

- 生产适配器、共享生命周期、`audit.publication` 或任何公共 API/CLI；
- `Data/`、真实 `processed/`、真实 `artifacts/` 和历史 release 字节；
- 真实 Stage 7/8 `CURRENT.json`；
- `processed/final_test/`、Stage 9 或远程仓库；
- 不提交、合并、推送、发布或创建 PR。

## 验证缝隙

1. **消费层等价缝隙**：用冻结 Stage 7 代码从新 BaoStock 原始联合表生成公司行动表，并对认证运行中的 `corporate_actions.parquet`、`security_events.parquet` 做列名、数据类型、行数和全部实际消费字段的多重集精确比较。不得只比较形状、哈希、排序后首尾行或抽样。
2. **影子身份缝隙**：验证专用夹具先核验机器可读豁免，再仅把已声明的新 BaoStock 身份映射到证书绑定的历史身份；任何其他输入身份差异都必须失败。生产适配器与生命周期代码保持零变化。

## 执行步骤

- [x] 冻结 06R 授权、范围、历史身份和强制停点。
- [x] 先写失败测试，覆盖消费字段差异、非 BaoStock 输入漂移、原始文件哈希不符和合格豁免。
- [x] 在 `tests/` 内实现最小验证辅助，不新增生产接口。
- [x] 在新临时根中以单进程、单会话、完全串行方式取得 BaoStock 2017–2021 `operate` 数据；13590 次查询全部成功，未重连或重试。
- [x] 记录 BaoStock 版本、下载时间、查询覆盖、登录/登出状态、原始文件大小和 SHA-256。
- [x] 从新原始文件重建 Stage 7 公司行动消费表；安全事件表使用同一冻结官方证据链重新加载。
- [x] 对两个认证运行分别执行全部实际消费字段精确比较。
- [x] 因消费字段非零差异拒绝生成 `external_source_revision_waiver.json`，生成失败关闭评估记录。
- [ ] 用验证专用身份夹具执行 Stage 7 证书重建、源运行复验与 `publish=false` 影子暂存。（硬门槛失败，按规则未执行。）
- [ ] 重做 Stage 8 影子验收。（硬门槛失败，既有通过结果未被本任务冒充为新验收。）
- [x] 运行 06R 相关测试、Ruff、完整 pytest 与 `git diff --check`。
- [x] 重读真实 Stage 7/8 CURRENT、manifest，并确认精确路径 `processed/final_test/CURRENT.json` 不存在。
- [x] 更新任务 06 HANDOFF 和最终审计报告。

## 实际结果

> 以下保留 06R 的历史失败证据。2026-08-27 用户按顺序批准先尝试历史字节恢复，失败后转入 06S 生产契约整改。06S 已以消费层零差异、Stage 7/8 `publish=false` 影子验收通过收口；本文不改写 06R 当时的失败事实。

- 新下载仍为 9516 × 16，SHA-256 `77e4fb6e076d87d4d5536fa1d4d292ff2f678ccd45a3d087fa35dd18579d3980`，大小 175011 字节。
- `security_events.parquet` 对两个认证运行均全字段、多重集零差异。
- `corporate_actions.parquet` 的列、类型、行数和经济键多重集一致，但一个 `000042` 行的 `source` 与派生 `action_id` 不同；完整行多重集为一条历史独有、一条新生成独有。
- 新 BaoStock 响应同时包含 `dividOperateDate=2018-06-22` 和历史异常值 `2018-06-28`。冻结代码优先保留供应商已修正的 6 月 22 日记录，因此不会再把来源标为 `+cninfo_action_correction`。
- `source` 与 `action_id` 均为认证消费表的实际列，故 `consumer_semantic_differences=2`，不满足已批准的零差异硬门槛。
- 机器可读失败记录：`docs/audits/2026-08-26-candidate1-task6r-external-source-revision-assessment.json`。

## 通过门槛

- 新旧 BaoStock 原始身份被分别记录，`historical_raw_bytes_recovered=false`。
- 10560 或重新完整下载对应的全部串行查询均有完整覆盖，且无并发连接。
- 两个认证运行的全部消费列、类型、行数和行多重集均零差异。
- 机器可读豁免只适用于候选 1 任务 06 影子验收，并绑定新旧哈希、下载证据和零差异结果。
- 除 BaoStock 原始身份外，证书输入身份无任何漂移。
- Stage 7/8 影子认证和 `publish=false` 暂存通过；真实发布与指针零变化。
- 完整 pytest、Ruff 和格式检查通过。

## 失败关闭

- 若消费层任一实际字段、类型、行数或重复计数不同，豁免不得生成，任务 06 与候选 1 继续保持 `incomplete`。
- 若 BaoStock 返回黑名单或任一非零错误，立即停止本次网络操作并保留检查点，不发起自动登录探测。
- 若无法证明只有 BaoStock 原始身份不同，验证专用映射必须拒绝。
- 不得用新原始文件覆盖、改名冒充或回写历史原始文件。

# 强制停点

完成 06R 证据并回到任务 06 做最终收口后停止。不得提交、合并、推送、真实发布、切换指针、开始其他架构候选或进入 Stage 9。

## HANDOFF

```yaml
task: 06R-external-source-revision-waiver
status: incomplete
baseline_handoff: 06-verify-and-handoff
historical_raw_bytes_recovered: false
external_source_revision_waiver: rejected
consumer_semantic_differences: 2
consumer_difference_fields:
  - action_id
  - source
assessment: docs/audits/2026-08-26-candidate1-task6r-external-source-revision-assessment.json
shadow_identity_mapping_performed: false
related_tests: 8 passed in 7.19s
full_pytest: 1604 passed, 1 skipped in 448.27s
ruff: passed
publication_performed: false
candidate_1_complete: false
successor_task: 06S-corporate-action-provenance-remediation
candidate_1_complete_after_successor: true
```
