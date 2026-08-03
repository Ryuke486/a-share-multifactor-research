# 板块7：验证期评价与候选方案冻结实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不读取2022–2025最终测试期的前提下，完成2017–2021验证期的因子、组合和真实成交评价，按预注册规则选定主方案。

**Architecture:** 新建独立 `validation` 研究包，复用阶段四至六的稳定接口，但不向原管道文件堆叠验证逻辑。产物发布至 `processed/validation_evaluation/`，并将阶段六release、配置和数据manifest纳入血缘。

**Tech Stack:** Python 3.12+、Polars、NumPy、SciPy、PyYAML、pytest、Matplotlib、现有audit publication接口。

## Global Constraints

- 只允许读取2003-01-01至2021-12-31；2022-01-01及以后必须由读取门禁拒绝。
- 2003–2004仅为预热，2005–2016为历史研究期，2017–2021为唯一验证评价期。
- 不得依据验证结果新增因子、翻转因子方向或扩大模型搜索。
- 只比较阶段五已冻结的简单基线和主方案；本板块不引入LightGBM等新模型。
- 不修改 `Data/`，不将processed、artifacts或原始数据纳入Git。

---

## 预期目标

- 建成覆盖2017–2021的point-in-time日面板、因子面板、复合分数、目标权重和正式回测。
- 对研究期与验证期的Rank IC、ICIR、分组单调性、换手、成本侵蚀和组合风险做可比较评价。
- 使用事先写入配置的判定规则，在 `family_equal`、滚动IC方案和股票等权基线间确定最终候选。
- 输出不可变validation release，但仍保持最终测试期封存。

## Task 1：冻结验证协议和读取门禁

**Files:**
- Modify: `configs/research_protocol.yaml`
- Modify: `src/ashare_multifactor/config.py`
- Create: `src/ashare_multifactor/validation/protocol.py`
- Test: `tests/test_validation_protocol.py`

**Interfaces:**
- Consumes: `ResearchConfig.validation`、阶段六 `CURRENT.json`。
- Produces: `ValidationProtocol`、`assert_validation_read_allowed(start, end)`。

- [x] 写入验证期主指标、次指标、比较对象和选择规则，不使用模糊的“综合表现更好”。
- [x] 先写失败测试：任何超过2021-12-31的读取请求必须在扫描文件前失败。
- [x] 实现最小日期门禁，同时验证阶段六release为 `b995878_stage6_authoritative` 或当前已批准的后继release。
- [x] 运行 `pytest tests/test_validation_protocol.py -q`，再运行配置回归测试。

## Task 2：构建2017–2021日面板与数据质量门禁

**Files:**
- Create: `src/ashare_multifactor/validation/data_extension.py`
- Create: `src/ashare_multifactor/validation/paths.py`
- Test: `tests/test_validation_data_extension.py`

**Interfaces:**
- Consumes: 阶段二reader/schema/validation接口、原始不复权与后复权日文件。
- Produces: `validation_daily_panel`、manifest、quality issues。

- [x] 先在人工夹具上测试schema、六位symbol、`(date, symbol)`唯一性和两种口径1:1连接。
- [x] 复用阶段二规范化逻辑，按年生成2017–2021 Parquet，禁止研究模块直接读CSV。
- [x] 产出日期范围、行数、主键、schema、文件哈希和质量问题摘要。
- [x] 先运行小日期样本，再扩大至2017–2021；最终两次完整流水线run manifest记录耗时205–229秒、峰值RSS约8.95–9.02GB。

## Task 3：无重新选择地外推因子和复合分数

**Files:**
- Create: `src/ashare_multifactor/validation/factor_extension.py`
- Create: `src/ashare_multifactor/validation/combination_extension.py`
- Test: `tests/test_validation_factor_extension.py`

**Interfaces:**
- Consumes: 阶段四因子定义与预处理规则、阶段五候选列表和合成规则。
- Produces: 2017–2021 `factor_panel`、`composite_scores`、IC权重血缘。

- [x] 验证因子公式、方向、去极值、标准化和市值中性化与阶段四一致。
- [x] 保持watch/reject不进入主合成，估值因子仍受point-in-time限制。
- [x] 滚动IC在每个权重日只使用已实现的历史标签；修改未来IC不得影响过去权重。
- [x] 输出覆盖率、缺失、权重退化和输入哈希审计表。

## Task 4：生成验证期目标权重和正式回测

**Files:**
- Create: `src/ashare_multifactor/validation/portfolio_extension.py`
- Create: `src/ashare_multifactor/validation/backtest_extension.py`
- Test: `tests/test_validation_backtest.py`

**Interfaces:**
- Consumes: validation composite scores、阶段五组合规则、阶段六execution接口。
- Produces: target weights、orders、trades、positions、cash、receivables、NAV。

- [x] 延续Top100等权和规模分层缓冲主组合，并保持稳定排序。
- [x] 使用阶段六已审计的t+1、整手、涨跌停、pending、公司行动、历史费用和逐日账本。
- [x] 增加连续性测试：2016年末预热与2017年首个信号不得断档或重置持仓。
- [x] 完成三场景账本、影子NAV和stale门禁，不因验证收益调整执行参数。

## Task 5：验证期指标和预注册选择

**Files:**
- Create: `src/ashare_multifactor/validation/metrics.py`
- Create: `src/ashare_multifactor/validation/selection.py`
- Test: `tests/test_validation_selection.py`

**Interfaces:**
- Consumes: 研究期和验证期机器可读结果。
- Produces: `validation_metrics.parquet`、`selection_decision.json`。

- [x] 计算Rank IC、ICIR、分组单调性、年化收益/波动、最大回撤、换手、成本侵蚀、现金比例和目标偏离。
- [x] 同时报告不显著、为负和不稳定结果，不以通过门槛数量为验收条件。
- [x] 选择函数只允许应用冻结规则，输出全部候选的通过/失败原因。
- [x] 若没有动态方案证明增益，默认保留简单 `family_equal` 基线。

## Task 6：发布validation release并保持测试期封存

**Files:**
- Create: `src/ashare_multifactor/validation/pipeline.py`
- Create: `src/ashare_multifactor/validation/report.py`
- Create: `src/ashare_multifactor/cli/validation.py`
- Test: `tests/test_validation_pipeline.py`

**Interfaces:**
- Produces: `processed/validation_evaluation/CURRENT.json`、release manifest/lineage/report。

- [x] 发布前验证所有数据集最大日期不超过2021-12-31。
- [ ] 在同一干净提交、配置和输入上执行两个独立run，比较核心产物哈希。
- [x] 血缘绑定阶段四、五、六release和validation日面板。
- [x] 报告明确“验证期结果不是最终样本外证据”。

## 验收清单

- [x] 2022年及以后零读取；
- [x] 验证数据、因子、合成、目标和回测链路完整；
- [x] 选择规则在结果之前已冻结；
- [x] 主方案及失败候选均有机器可读证据；
- [x] 三场景账本和逐日对账通过；
- [x] 开发工作树下两个独立完整run的98个核心产物哈希一致；正式发布仍需在同一干净提交上复跑；
- [ ] validation release已发布且Git工作区干净。
