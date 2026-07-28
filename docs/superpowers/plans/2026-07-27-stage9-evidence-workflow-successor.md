# Stage 9 证据工作流继任实施计划

**目标：** 在不降低官方事实门槛、不触碰旧 attempt 的前提下，缩小 Stage 9
局部缺陷的失效范围，并让新 attempt 复用已验证原始证据。

**设计：**
`docs/superpowers/specs/2026-07-27-stage9-evidence-workflow-successor-design.md`

## 不变边界

- 实现和预演不得读取 2022–2025 的策略结果。
- 当前 `stage9-final-20260724-763d489` 保持只读，不执行 `resume`。
- Data/ 不修改；现有 release、seal、ledger 和 evidence 不覆盖。
- 本计划不自动提交、发布 v4 release、创建新 attempt 或补采网络文档。

## Task 1：拆分身份和变更影响

- [x] 将 research result、final execution 和 evidence workflow 身份独立哈希。
- [x] 生成并验证机器可读 change-impact 审计。
- [x] 只有 research result 身份相同时允许复用 Stage 7/8 研究产物。
- [x] v4 seal、lineage 和 Stage 9 gate 绑定三类身份及 readiness 哈希。

## Task 2：PDF 准入和路由反例

- [x] 非 PDF、加密、不可读、零页文档失败关闭并进入 quarantine。
- [x] 有效 PDF 记录页数、媒体类型、字节数和 SHA-256。
- [x] 已知两类可转债误路由标题先于通用关键词排除。
- [x] 文档队列显式区分 `cached`、`quarantined` 和 `not_fetched`。

## Task 3：可恢复候选采集

- [x] BaoStock 按证券/年度发布不可变查询包。
- [x] 中断后只查询缺失包，完整后精确发布快照。
- [x] 拒绝字段、行宽、证券范围和年度语义漂移。
- [x] 候选与执行事实分离，不允许未审核候选直接进入 coverage。

## Task 4：字段级审核和原子覆盖

- [x] 审核提交精确覆盖所有公告队列行和所有结构化候选。
- [x] 修正保留原值、新值和理由，事实必须引用有效官方 PDF。
- [x] 公司行动和证券事件 coverage 在同一 staging 中共同验证。
- [x] 两类 coverage 原子公开，零事件不制造虚假证据。
- [x] attempt 级查询/PDF 只引用、不复制，加载时重复验哈希。

## Task 5：跨 attempt 证据导入

- [x] 新 attempt 以 v4 复用凭证绑定既有不可变最终期日面板，不重新构建。
- [x] 重建目标 attempt 的官方证券身份索引。
- [x] 原字节复用完整官方查询包并最后原子发布索引。
- [x] 逐份重新验证旧文档，只发布合格 PDF。
- [x] 无效或缺失文档进入精确补采清单；重复导入保持幂等。

## Task 6：Stage 8 完整证据预演

- [x] 生成 PDF 准入报告。
- [x] 生成候选恢复报告。
- [x] 生成审核核销报告。
- [x] 生成覆盖原子发布报告。
- [x] 生成证据导入报告。
- [x] 完成 2017–2021、100 只证券、57,616 条真实公告目录重路由，最终测试期
  读取为零；字段审核和双覆盖发布使用端到端人工夹具，不声称 408 份历史候选
  PDF 已逐份人工审核。
- [x] 将六份报告发布为 development evidence-workflow readiness audit；
  最新路径为
  `artifacts/stage8_evidence_workflow_rehearsal/20260727_development_v4_r4/readiness/audit`，
  manifest SHA-256 为
  `8a4f4f7cf7441e8191898202e9a9ddaa008357072c3a70edcc9c4478160680cf`；
  干净提交后的正式 audit 仍须重新生成。

## Task 7：验证、审查和 v4 关闭封印

- [x] 相关测试、全量 `1270 passed`、Ruff 和 `git diff --check` 全部通过。
- [x] 复查身份失效范围、旧 attempt 不变性和共享证据路径安全：旧 attempt
  仍为 `awaiting_official_evidence`，关键 manifest 哈希未变，无
  `CURRENT.json`、复用凭证、新候选、审核提交或 execution coverage。
- [ ] 经用户明确授权后提交干净实现身份。
- [ ] 生成 change-impact audit，证明 Stage 7/8 research result 哈希未变。
- [ ] 复用并重验既有 Stage 7/8 研究文件，发布 v4 protocol-only successor。
- [ ] 核验 v4 seal 为关闭状态，且未创建新 attempt。

## Task 7A：封印前门禁收紧与可恢复人工审核

- [x] protocol-only 发布、Stage 9 授权和最终期日面板复用共同要求
  `changed_domains=["evidence_workflow"]`，且
  `final_execution_rehearsal_required=false`。
- [x] 将完整队列与候选按证券冻结为确定性批次计划；计划绑定两类上游
  manifest，任何分片字节漂移失败关闭。
- [x] 单批审核独立执行字段级门禁并不可变发布；相同批次可幂等恢复，冲突
  提交不得覆盖。
- [x] 只有全部批次齐备、审核者一致且批次时间不晚于汇总时间时才可汇总；
  汇总后再次执行原有全队列、全候选精确核销。
- [x] 将中断恢复、缺批拒绝、冲突拒绝、分片漂移和跨证券证据引用反例纳入
  Stage 8 evidence-workflow readiness 必选测试。
- [x] 完成 Task 7A 全量 `1276 passed`、Ruff、差异审计和旧 attempt
  不变性复核；旧 attempt 仍为 `awaiting_official_evidence`，无批次计划、
  审核提交、execution coverage 或最终测试 `CURRENT.json`。
- [x] 经用户明确确认后提交 Task 7A；提交为
  `b2fd4c18b8467f310cd5d8da8f585925cb415bf8`。正式 readiness 和
  change-impact 尚未生成，旧 development readiness 不作为发布输入。

## Task 7A.1：v4 发布前终审修复

- [x] change-impact 不再信任审计自报的前任身份；从权威 v3 seal 的 Git
  commit/tree 重建 `final_execution` 身份，并同时写入 v4 seal 和 lineage，
  发布、授权及日面板复用重复核对。
- [x] 正式 execution coverage 只接受可从完整批次计划、全部不可变批次
  submission、审核者和时间重新构造的最终审核提交；直接一次性审核不能绕过
  可恢复审核门禁。
- [x] 将 disposition 跨证券引用与无 `candidate_id` 的事实跨证券引用拆成
  两个独立反例，并把两者及直接审核绕过反例纳入 evidence-workflow
  readiness 必选测试。
- [x] 完成 Task 7A.1 专项 `62 passed`、全量 `1280 passed`、Ruff、差异
  审计、身份与旧 attempt 不变性复核。权威 v3 与当前 `final_execution`
  SHA-256 同为
  `95fafab8b735e47c9011d3ec851320678ccf9376cc772994bcb0e725b369cfbf`；
  旧 attempt 仍为 `awaiting_official_evidence`，无最终测试 `CURRENT.json`、
  批次计划、批次提交或 execution coverage。
- [x] 用户已明确确认提交 Task 7A.1；本提交不生成正式 readiness、
  change-impact 或 v4 seal。

## Task 7A.2：BaoStock 缺失支付日失败关闭修复

- [x] 用现金分红 `dividPayDate` 为空的反例复现候选采集失败。
- [x] 候选层保留 `effective_date=null`，候选快照 schema 升至 v2 并记录
  缺失日期候选数量，不伪造支付日期。
- [x] 审核层使用 null-safe 原值/事实对比；缺失日期候选只能由官方 PDF
  `corrected` 为非空日期或 `rejected`，不得直接 `accepted`。
- [x] 最终 execution fact 契约继续拒绝空日期，候选修复不放宽正式执行输入。
- [x] 完成相关 `69 passed`、全量 `1290 passed`、Ruff、差异与身份失效
  范围审计；`final_execution` 身份仍为
  `95fafab8b735e47c9011d3ec851320678ccf9376cc772994bcb0e725b369cfbf`，
  只有 `evidence_workflow` 身份发生变化。
- [x] 用户已明确确认提交 Task 7A.2；本提交不生成正式 readiness、
  change-impact、v4 seal、新授权或新 attempt。失败 attempt 不得继续。

## Task 8：新授权后的最小重采

- [ ] 用户对精确 v4 seal 提供新的一次性授权。
- [ ] 创建唯一新 attempt，不复用旧 token 或旧 attempt 状态。
- [ ] 发布并验证最终期日面板复用凭证，不重新构建共享日面板。
- [ ] 导入并重验旧官方身份、查询包和有效 PDF。
- [ ] 只请求导入报告列出的无效/缺失 URL；预期当前为 2 个。
- [ ] 收集/恢复 BaoStock 候选，重建队列并通过可恢复批次完成字段级人工协调。
- [ ] 原子发布两类 `ready` execution coverage。
- [ ] 停在 `resume` 前，单独报告新 attempt、导入/补采数量和两类 manifest
  哈希，等待用户明确确认。

## 当前完成定义

本轮代码完成不等于 Stage 9 完成。只有 Task 6–8 的实际证据、关闭封印、新授权、
新 attempt 和两类 `ready` coverage 均完成，并在用户再次授权后执行唯一
`resume`，才可能进入最终测试发布和检查点 C。
