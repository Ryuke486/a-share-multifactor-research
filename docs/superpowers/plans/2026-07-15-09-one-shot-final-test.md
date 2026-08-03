# 板块9：最终测试期一次性评价实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在已封存且经用户批准的协议下，对2022–2025执行一次最终样本外评价，不因结果修改模型、参数或报告口径。

**Architecture:** 新建 `final_test` 包，仅接受板块8的 `sealed_test_protocol.json`。执行前验证全部哈希和开启令牌；执行后立即封存结果、命令、时间和环境，不提供参数搜索入口。

**Tech Stack:** Python 3.12+、Polars、NumPy、SciPy、PyYAML、pytest、Matplotlib、现有execution/audit接口。

## Global Constraints

- 只有用户明确批准后才能读取2022-01-01至2025-12-31。
- 执行前任一封存哈希不匹配都必须停止，不得自动重新冻结。
- 最终结果不得导致因子方向、候选、权重、组合、成本、参数、指标或样本的修改。
- 不将最终测试结果与任何新候选进行搜索式比较。

---

## 预期目标

- 产生一个可证明“只打开一次”的最终测试release。
- 完整报告2022–2025的因子外推、目标组合、真实成交、成本和风险结果。
- 同时呈现正面、负面和不显著证据，不把测试失败视为工程失败。

## Task 1：执行前封存验证和一次性登记

**Files:**
- Create: `src/ashare_multifactor/final_test/gate.py`
- Create: `src/ashare_multifactor/final_test/registry.py`
- Test: `tests/test_final_test_gate.py`

**Interfaces:**
- Consumes: robustness release、`sealed_test_protocol.json`、用户批准记录。
- Produces: `FinalTestAuthorization`、append-only attempt record。

- [ ] 验证代码、配置、上游release、成本、指标和模板哈希。
- [ ] 测试无授权、哈希变化、日期越界和已有成功权威run时必须拒绝。
- [ ] 在读取首个2022文件之前写入尝试ID、时间、Git提交和协议哈希。

## Task 2：构建2022–2025规范数据和质量报告

**Files:**
- Create: `src/ashare_multifactor/final_test/data_extension.py`
- Test: `tests/test_final_test_data.py`

**Interfaces:**
- Produces: final-test daily panel、manifest、quality issues。

- [ ] 完全复用板块7数据延伸schema和错误等级，不因测试期异常改口径。
- [ ] 明确2025-12-31后不读取，2026年数据不属于最终测试。
- [ ] 记录数据文件清单、哈希、日期、行数和质量异常，不修改原始Data。

## Task 3：按封存协议外推信号和目标权重

**Files:**
- Create: `src/ashare_multifactor/final_test/signals.py`
- Test: `tests/test_final_test_signals.py`

**Interfaces:**
- Produces: test factor panel、composite scores、target weights。

- [ ] 使用封存因子、方向、预处理、合成和组合规则。
- [ ] 滚动统计只使用当时已实现标签，测试末端不为获得标签越过2025-12-31。
- [ ] 将缺失和覆盖下降按冻结规则处理，不临时替换因子。

## Task 4：执行最终真实交易回测

**Files:**
- Create: `src/ashare_multifactor/final_test/backtest.py`
- Test: `tests/test_final_test_backtest.py`

**Interfaces:**
- Produces: orders/trades/positions/cash/receivables/NAV和三场景账本。

- [ ] 严格复用阶段六执行器和板块7延伸方法，不为测试期特例修改撮合。
- [ ] 完成t+1、整手、涨跌停、停牌、pending、公司行动、历史费用和逐日对账。
- [ ] 账务、影子NAV或stale门禁失败时不发布权威release，但保留失败尝试记录。

## Task 5：计算预注册最终指标

**Files:**
- Create: `src/ashare_multifactor/final_test/metrics.py`
- Create: `src/ashare_multifactor/final_test/report.py`
- Test: `tests/test_final_test_metrics.py`

- [ ] 只计算封存指标：因子IC/分组、净收益、波动、夏普、回撤、换手、成本侵蚀、目标偏离和可交易性。
- [ ] 用研究期、验证期和测试期并列表格展示衰减或稳定，不合并重新选模。
- [ ] 报告模板在看到数字前已封存，所有文字结论可追溯到机器可读结果。

## Task 6：发布一次性final-test release

**Files:**
- Create: `src/ashare_multifactor/final_test/pipeline.py`
- Create: `src/ashare_multifactor/cli/final_test.py`
- Test: `tests/test_final_test_pipeline.py`

**Interfaces:**
- Produces: `processed/final_test/CURRENT.json`、权威release、attempt registry。

- [ ] 发布前校验日期仅为2022–2025，上游和封存哈希完整。
- [ ] 为结果文件生成manifest、lineage、环境和执行时间记录。
- [ ] 权威run成功后锁定重复发布；任何重跑都必须保留原run和原因。
- [ ] 完成后停止开发，进入检查点C，不立即修改报告口径或开始项目交付。

## 验收清单

- [ ] 用户开启批准和尝试登记存在；
- [ ] 全部封存哈希与板块8一致；
- [ ] 仅读取2022–2025，不读取2026；
- [ ] 没有因测试结果修改任何研究规则；
- [ ] 因子、组合、成交、账务和指标链路完整；
- [ ] 失败和不显著结果已保留；
- [ ] final-test release可通过manifest完整复验；
- [ ] 完成后已触发检查点C，未跳过审计进入板块10。
