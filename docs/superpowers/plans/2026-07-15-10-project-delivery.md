# 板块10：检查点C与项目交付实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 审计最终测试的一次性和冻结纪律，并将代码、数据说明、研究报告、结果表、图表和复现命令整合为可复现的GitHub项目。

**Architecture:** 先执行独立检查点C，通过后才生成交付层。报告和图表只消费机器可读release，不重新计算因子、交易或NAV。本板块不包含答辩材料。

**Tech Stack:** Python 3.12+、Polars、PyYAML、Matplotlib、pytest、Markdown、现有audit publication接口。

## Global Constraints

- 检查点C未通过前不得将项目标记为完成。
- 报告、README和图表中的数字必须从权威release生成，禁止人工复制计算。
- 不修改因子、组合、交易、成本或最终测试结果。
- GitHub不得包含 `Data/`、processed、artifacts、缓存的公告原文或其他未授权大文件。
- 暂不制作答辩PPT、讲稿、模拟问答或口试材料。

---

## 预期目标

- 检查点C证明最终测试只使用一次，且未触发事后调参。
- 形成一份完整研究报告，同时报告有效、失败和局限性证据。
- 用户能从README和命令清单理解项目、复验小样例，并在有本地原始数据时重建主实验。
- 生成结构清晰、无原始数据泄漏的GitHub交付版本。

## Task 1：执行检查点C

**Files:**
- Create: `docs/audits/checkpoint-c-audit.md`
- Create: `src/ashare_multifactor/delivery/checkpoint_c.py`
- Test: `tests/test_checkpoint_c.py`

**Interfaces:**
- Consumes: sealed test protocol、attempt registry、final-test release、Git历史。
- Produces: `CheckpointCResult`、审计报告。

- [ ] 核对最终测试开启时间、用户批准、协议哈希和权威run数量。
- [ ] 比较打开测试前后的因子、组合、成本、指标、代码和配置；任一未披露变化都阻断。
- [ ] 完整复验final-test manifest、lineage、账务、日期和报告输入。
- [ ] 检查正面、负面和不显著结果均保留；不以策略收益高低作为通过条件。
- [ ] 只有审计状态为 `passed` 时才允许后续交付任务。

## Task 2：建立单一结果源和报告数据集

**Files:**
- Create: `src/ashare_multifactor/delivery/result_catalog.py`
- Create: `src/ashare_multifactor/delivery/tables.py`
- Test: `tests/test_delivery_tables.py`

**Interfaces:**
- Produces: `delivery_tables/`下的统一CSV/Parquet表和数字字典。

- [ ] 为研究、验证、稳健性和测试release建立显式目录，拒绝临时路径。
- [ ] 统一指标名称、单位、年化口径、样本范围和小数格式。
- [ ] 对每个报告数字保存source release、数据集、列名和计算函数。

## Task 3：生成研究报告

**Files:**
- Create: `src/ashare_multifactor/delivery/research_report.py`
- Create: `reports/research_report.md`
- Test: `tests/test_research_report.py`

- [ ] 报告研究问题、数据来源、口径、样本划分、无泄漏措施和动态股票池。
- [ ] 报告因子定义、单因子证据、合成、目标组合和真实交易约束。
- [ ] 并列展示研究期、验证期和最终测试期，不把三者混为同一个调参样本。
- [ ] 报告成本侵蚀、换手、回撤、稳健性、失败结果和数据局限。
- [ ] 估值point-in-time、历史行业和ST口径未完全验证时必须继续披露。

## Task 4：生成标准图表

**Files:**
- Create: `src/ashare_multifactor/delivery/plots.py`
- Test: `tests/test_delivery_plots.py`

- [ ] 生成因子IC、分组收益、净值/回撤、成本侵蚀、换手、目标偏离和稳健性图。
- [ ] 图表标题、轴、样本日期、成本口径和数据源完整；图上不使用“稳赚”等夸大用语。
- [ ] 同一指标在表格、图表和正文中必须来自同一result catalog。

## Task 5：重写README和复现路径

**Files:**
- Modify: `README.md`
- Create: `docs/reproduction.md`
- Create: `docs/result_dictionary.md`
- Test: `tests/test_documentation_links.py`

- [ ] README提供研究问题、主要贡献、仓库结构、数据限制、环境和主命令。
- [ ] 明确GitHub不包含原始数据，给出本地数据的预期路径、字段和哈希复验方式。
- [ ] 提供“人工小样例快速验证”和“本地28GB数据完整复现”两套命令。
- [ ] 文档链接、命令和文件名必须由自动测试验证。

## Task 6：GitHub安全和可复现性审计

**Files:**
- Create: `src/ashare_multifactor/delivery/repository_audit.py`
- Test: `tests/test_repository_delivery_audit.py`

- [ ] 扫描Git跟踪文件，拒绝Data、processed、artifacts、超阈值数据文件和敏感绝对路径。
- [ ] 检查license、数据来源、引用、字段字典和已知限制文档。
- [ ] 在干净环境运行小样例、全量pytest、ruff和所有release manifest复验。
- [ ] 确认失败运行、旧release和不利结果的审计记录没有被删除。

## Task 7：发布项目交付release

**Files:**
- Create: `src/ashare_multifactor/delivery/pipeline.py`
- Create: `src/ashare_multifactor/cli/delivery.py`
- Test: `tests/test_delivery_pipeline.py`

- [ ] 生成 `processed/project_delivery/CURRENT.json`、manifest、lineage和交付摘要。
- [ ] 血缘绑定检查点C、研究/验证/稳健性/测试release、README和报告哈希。
- [ ] 两次独立生成的表格、图表和报告核心文件哈希一致。
- [ ] 更新 `AGENTS.md` 的最终实际状态，但不声称策略保证盈利。

## 验收清单

- [ ] 检查点C通过；
- [ ] 最终测试没有触发事后调参；
- [ ] 报告、README、图表和机器结果数字一致；
- [ ] 正面、负面和不显著证据均已报告；
- [ ] 小样例和本地完整复现路径可用；
- [ ] GitHub不包含原始数据或可再生大文件；
- [ ] 全量pytest、ruff和manifest复验通过；
- [ ] 项目交付release可复现；
- [ ] 本板块未生成任何答辩材料。
