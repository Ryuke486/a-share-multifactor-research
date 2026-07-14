# 检查点A与阶段五审计修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 在不改变阶段五冻结研究决策的前提下，关闭审计A-01至A-09并形成可供阶段六安全读取的版本化发布接口。

**Architecture:** 新增与金融逻辑无关的`audit`基础包，阶段五通过严格输入门禁消费阶段四产物，通过不可变release与单一`CURRENT.json`发布。合成、组合、评价和诊断继续保持领域分离，编排层只连接对象。

**Tech Stack:** Python 3.14.6、Polars、NumPy、SciPy、PyYAML、Matplotlib、pytest、ruff。

## Global Constraints

- 2005-01-01至2016-12-31是唯一评价区间，2017–2025继续封存。
- candidate名单、方向、`family_equal`主方案和`size_stratified_buffered`主组合不得改变。
- 不实现阶段六交易、税费、滑点、订单、持仓或NAV。
- 测试驱动：失败反例、最小实现、相关测试、全量验证。
- 未经用户明确请求，不提交、不推送、不创建PR。

---

### Task 1: 文件记录与数据集契约（A-07基础）

**Files:**
- Create: `src/ashare_multifactor/audit/__init__.py`
- Create: `src/ashare_multifactor/audit/records.py`
- Create: `src/ashare_multifactor/audit/contracts.py`
- Create: `tests/test_audit_contracts.py`

**Interfaces:**
- Produces: `FileRecord`, `file_record()`, `verify_file_record()`；`DatasetContract`、`validate_dataset()`、`dataset_manifest_entry()`。

- [x] **Step 1: 写文件篡改、重复键、越界日期和非有限值失败测试**

测试必须构造临时文件和Polars DataFrame，断言文件哈希变化、重复主键、2017年日期及非有限权重分别抛出`ValueError`。

- [x] **Step 2: 运行失败测试**

Run: `.venv/bin/pytest -q tests/test_audit_contracts.py`
Expected: FAIL，模块尚不存在。

- [x] **Step 3: 实现最小通用对象**

`FileRecord`固定字段为`path`、`role`、`sha256`、`size_bytes`。`DatasetContract`固定字段为`name`、`primary_key`、`date_column`、`minimum_date`、`maximum_date`、`finite_columns`、`non_negative_columns`。manifest条目必须包含schema、主键、行数、日期范围与文件记录。

- [x] **Step 4: 运行相关测试与ruff**

Run: `.venv/bin/pytest -q tests/test_audit_contracts.py && .venv/bin/ruff check src/ashare_multifactor/audit tests/test_audit_contracts.py`
Expected: PASS。

### Task 2: 阶段四实际输入门禁（A-01）

**Files:**
- Create: `src/ashare_multifactor/research/combination_inputs.py`
- Modify: `src/ashare_multifactor/research/combination_pipeline.py`
- Modify: `tests/test_combination_evaluation.py`

**Interfaces:**
- Produces: `StageFiveInputs`和`validate_stage_five_inputs(root: Path) -> StageFiveInputs`。
- Consumes: Task 1的文件记录校验。

- [x] **Step 1: 写日面板manifest、质量报告、任一年度分区被替换时失败的测试**

夹具中的阶段四lineage必须明确绑定日面板记录；分别修改文件后断言在读取因子面板前失败。另写candidate族映射错误和score口径错误测试。

- [x] **Step 2: 运行失败测试**

Run: `.venv/bin/pytest -q tests/test_combination_evaluation.py -k 'upstream or candidate_family'`
Expected: FAIL。

- [x] **Step 3: 实现输入门禁并替换旧`_validate_upstream`**

返回对象包含6个阶段四核心文件、日面板manifest、质量报告、年度分区及实际消费记录。绝对路径不进入身份哈希。

- [x] **Step 4: 运行相关测试**

Run: `.venv/bin/pytest -q tests/test_combination_evaluation.py tests/test_factor_pipeline_security.py`
Expected: PASS。

### Task 3: 配置驱动与候选注册表一致性（A-05）

**Files:**
- Modify: `src/ashare_multifactor/combination/eligibility.py`
- Modify: `src/ashare_multifactor/combination/panel.py`
- Modify: `src/ashare_multifactor/research/combination_pipeline.py`
- Modify: `tests/test_combination_core.py`
- Modify: `tests/test_combination_config.py`

**Interfaces:**
- `build_composite_scores(..., minimum_families: int, analysis_start: date, analysis_end: date)`。
- `build_rolling_composite_scores(..., minimum_families: int, analysis_start: date, analysis_end: date)`。

- [x] **Step 1: 写最低族数、日期配置和错误族映射反例**

断言`minimum_families=3`会排除仅两族证券，合法自定义研究子区间会生效，classification或panel中的family与冻结注册表不一致时失败。

- [x] **Step 2: 运行失败测试**

Run: `.venv/bin/pytest -q tests/test_combination_core.py tests/test_combination_config.py`
Expected: 新测试FAIL。

- [x] **Step 3: 用不可变配置替换核心硬编码**

方法白名单仍由冻结注册表控制；pipeline从`FactorCombinationSettings`传递日期、方法和最低族数。

- [x] **Step 4: 运行相关测试**

Run: `.venv/bin/pytest -q tests/test_combination_core.py tests/test_combination_config.py`
Expected: PASS。

### Task 4: 月度截面相关性（A-04）

**Files:**
- Modify: `src/ashare_multifactor/research/combination_evaluation.py`
- Modify: `tests/test_combination_evaluation.py`

**Interfaces:**
- `CombinationEvaluation`新增`correlation_monthly`。
- 汇总相关表字段固定为`left_name`、`right_name`、`right_type`、`mean_correlation`、`common_months`、`invalid_months`、`primary_reason`。

- [x] **Step 1: 写池化相关与月度等权结果不同的反例**

构造两个截面大小显著不同且相关方向相反的月份，断言汇总值等于两个月Spearman的算术平均，而非池化相关。

- [x] **Step 2: 运行失败测试**

Run: `.venv/bin/pytest -q tests/test_combination_evaluation.py -k correlation`
Expected: FAIL。

- [x] **Step 3: 实现规范化因子对和逐月明细**

逐月主键为`(date, left_name, right_name)`；共同证券少于3、常数序列或非有限相关标记无效并保存原因。

- [x] **Step 4: 运行相关测试**

Run: `.venv/bin/pytest -q tests/test_combination_evaluation.py`
Expected: PASS。

### Task 5: 完整组合诊断（A-03）

**Files:**
- Modify: `src/ashare_multifactor/portfolio/diagnostics.py`
- Modify: `src/ashare_multifactor/research/combination_pipeline.py`
- Modify: `tests/test_portfolio_construction.py`

**Interfaces:**
- Produces: `compare_portfolios()`、`size_group_diagnostics()`、`quality_warnings()`。

- [x] **Step 1: 写重合率、换手差、规模组不足、覆盖下降和异常换手手算测试**

测试使用两个小型目标组合，明确断言重合率、换手差及warning规则名。

- [x] **Step 2: 运行失败测试**

Run: `.venv/bin/pytest -q tests/test_portfolio_construction.py`
Expected: FAIL。

- [x] **Step 3: 实现聚合诊断**

不输出逐证券未入选日志；输出排除原因计数。异常换手阈值和覆盖下降阈值进入配置并经过校验。

- [x] **Step 4: 运行相关测试**

Run: `.venv/bin/pytest -q tests/test_portfolio_construction.py tests/test_combination_config.py`
Expected: PASS。

### Task 6: 代码身份、lineage与版本化发布（A-02、A-07、A-09）

**Files:**
- Create: `src/ashare_multifactor/audit/identity.py`
- Create: `src/ashare_multifactor/audit/lineage.py`
- Create: `src/ashare_multifactor/audit/publication.py`
- Create: `tests/test_audit_publication.py`
- Modify: `src/ashare_multifactor/research/combination_paths.py`
- Modify: `src/ashare_multifactor/research/combination_pipeline.py`

**Interfaces:**
- Produces: `code_identity(root: Path) -> dict`、`publish_release(...) -> PublishedRelease`、`resolve_current(root: Path) -> PublishedRelease`。

- [x] **Step 1: 写dirty身份、CURRENT切换前失败、当前run解析和输出篡改测试**

断言仅commit不足以标识dirty工作树；失败发布不改变CURRENT；解析器校验CURRENT记录的manifest哈希；篡改当前run文件时读取失败。

- [x] **Step 2: 运行失败测试**

Run: `.venv/bin/pytest -q tests/test_audit_publication.py`
Expected: FAIL。

- [x] **Step 3: 实现不可变release发布**

布局固定为`processed/factor_combination/releases/<run_id>/{datasets,artifacts,manifest.json,lineage.json}`及`CURRENT.json`。正式冻结接口拒绝dirty身份；修复验证允许dirty但明确记录diff和源码哈希。

- [x] **Step 4: 运行相关测试**

Run: `.venv/bin/pytest -q tests/test_audit_publication.py tests/test_combination_evaluation.py`
Expected: PASS。

### Task 7: 编排拆分、CLI与文档一致性（A-06、A-08）

**Files:**
- Create: `src/ashare_multifactor/research/combination_outputs.py`
- Create: `src/ashare_multifactor/research/combination_report.py`
- Modify: `src/ashare_multifactor/research/combination_pipeline.py`
- Modify: `src/ashare_multifactor/cli/combinations.py`
- Modify: `README.md`
- Modify: `docs/superpowers/plans/2026-07-14-05-factor-combination-and-portfolio.md`
- Test: `tests/test_combination_evaluation.py`

**Interfaces:**
- CLI固定为`ashare-combine --config PATH --upstream-root PATH`，不暴露子阶段参数。

- [x] **Step 1: 写CLI只接受完整发布路径的测试**

断言旧`scores/evaluate/weights/report`参数被拒绝，默认调用完整发布。

- [x] **Step 2: 运行失败测试**

Run: `.venv/bin/pytest -q tests/test_combination_evaluation.py -k cli`
Expected: FAIL。

- [x] **Step 3: 拆分输出与报告职责并精简编排层**

pipeline不得再定义文件哈希、契约、发布替换、报告正文或绘图实现。同步修正计划中逐证券排除原因及子阶段CLI的错误勾选描述。

- [x] **Step 4: 运行阶段五测试与ruff**

Run: `.venv/bin/pytest -q tests/test_combination_config.py tests/test_combination_core.py tests/test_combination_evaluation.py tests/test_portfolio_construction.py tests/test_audit_contracts.py tests/test_audit_publication.py && .venv/bin/ruff check src tests`
Expected: PASS。

### Task 8: 真实研究期复现与检查点A复查

**Files:**
- Modify: `AGENTS.md`
- Modify: `docs/audits/2026-07-14-checkpoint-a-audit.md`（主工作树中的审计报告）
- Create: `docs/audits/2026-07-14-checkpoint-a-reaudit.md`

**Interfaces:**
- Consumes: Task 1至7全部接口。

- [x] **Step 1: 运行完整测试和静态检查**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check src tests && git diff --check`
Expected: 0 failures、0 lint errors。

- [x] **Step 2: 在dirty验证身份下连续运行两次2005–2016阶段五**

Run: `.venv/bin/ashare-combine --config configs/research_protocol.yaml --upstream-root /Users/mikasa/本科时期/Projects/repository1/processed/factor_research`
Expected: 两个独立run，研究表逐文件哈希一致；run_id、时间等运行元数据允许不同；PNG只核对输入哈希和尺寸。

- [x] **Step 3: 复核日期、主键、权重、血缘和封存边界**

必须确认2017年以后零读取、所有目标权重和为1、每月100只且每规模组20只、所有实际上游输入均已绑定。

- [x] **Step 4: 更新文档事实并形成复查报告**

逐项记录A-01至A-09状态、测试证据、真实数据证据和剩余限制。`AGENTS.md`只更新实际状态，不重写项目规划。

- [x] **Step 5: 停在提交授权门禁**

不提交。向用户报告dirty验证结果并请求是否授权提交；只有获授权后才能生成正式冻结run并关闭A-02。
