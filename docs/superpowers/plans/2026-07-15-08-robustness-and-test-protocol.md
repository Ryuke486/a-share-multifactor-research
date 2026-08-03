# 板块8：稳健性分析与最终测试协议实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在2005–2021已开放样本内检查最终候选方案对成本、参数、股票池和市场环境的依赖，并封存2022–2025一次性测试协议。

**Architecture:** 新建 `robustness` 包，将“实验定义”、“执行”和“汇总”分离。所有实验从预注册矩阵生成，不在Notebook中临时改参数；最终输出 `sealed_test_protocol.json`。

**Tech Stack:** Python 3.12+、Polars、NumPy、SciPy、PyYAML、pytest、Matplotlib。

## Global Constraints

- 只使用2005-01-01至2021-12-31；严禁读取2022–2025。
- 稳健性变体是诊断，不是新的模型搜索空间；不按最佳收益挑选参数。
- 主结论始终来自板块7冻结的主方案，变体只报告敏感性。
- 如果发现实现bug或研究协议致命缺陷，停止本板块并使板块7冻结失效；修复后重走板块7。

---

## 预期目标

- 得到成本、调仓频率、股票池、因子删除、窗口和市场分段的稳健性证据。
- 明确哪些结论稳定，哪些高度依赖特定假设，不隐藏失败变体。
- 产出带哈希的最终测试协议，封存代码、数据、模型、成本、指标和报告模板。

## Task 1：预注册稳健性实验矩阵

**Files:**
- Create: `configs/robustness_protocol.yaml`
- Create: `src/ashare_multifactor/robustness/protocol.py`
- Test: `tests/test_robustness_protocol.py`

**Interfaces:**
- Consumes: validation release的主方案身份。
- Produces: `RobustnessExperiment`不可变实验列表。

- [ ] 在运行结果前冻结实验名称、参数、对照组、指标和解释规则。
- [ ] 固定实验随机种子和稳定排序，禁止重复实验ID。
- [ ] 测试配置不得包含2022年及以后日期，不得改变主方案身份。

## Task 2：成本和执行假设敏感性

**Files:**
- Create: `src/ashare_multifactor/robustness/execution_sensitivity.py`
- Test: `tests/test_execution_sensitivity.py`

**Interfaces:**
- Produces: 显式费用、滑点/冲击、参与率和最低佣金敏感性表。

- [ ] 保留零成本、仅显式费用和完整成本主场景。
- [ ] 在预注册的保守范围内改变滑点、冲击和最大参与率，不改变信号。
- [ ] 报告收益、换手、成本侵蚀、未成交率和目标偏离的联合变化。

## Task 3：组合规则和因子删除敏感性

**Files:**
- Create: `src/ashare_multifactor/robustness/portfolio_sensitivity.py`
- Create: `src/ashare_multifactor/robustness/factor_ablation.py`
- Test: `tests/test_portfolio_factor_sensitivity.py`

**Interfaces:**
- Produces: 调仓频率、持仓数、缓冲排名、流动性门槛和逐因子删除结果。

- [ ] 变体必须围绕主方案对称设置，不根据结果继续细分参数。
- [ ] 每次只改变一类假设，保持其他代码、数据和成本不变。
- [ ] 因子删除使用族别级别，不在本板块尝试新因子。

## Task 4：时间、市场和截面分段

**Files:**
- Create: `src/ashare_multifactor/robustness/regime_analysis.py`
- Test: `tests/test_regime_analysis.py`

**Interfaces:**
- Produces: 研究/验证子区间、市场涨跌/高低波动、大小市值及可验证行业分组表。

- [ ] 市场状态只用当期可观察市场数据定义，禁止按策略收益事后切段。
- [ ] 历史行业口径仍未验证时，行业结果仅作限制性描述，不作为主结论。
- [ ] 每个子样本报告观测数、时间范围和不确定性，避免小样本过度解释。

## Task 5：稳健性汇总与失败结果报告

**Files:**
- Create: `src/ashare_multifactor/robustness/summary.py`
- Create: `src/ashare_multifactor/robustness/report.py`
- Test: `tests/test_robustness_summary.py`

- [ ] 使用统一长表schema记录experiment ID、唯一改动、样本、指标和基线差异。
- [ ] 输出稳定、敏感和失败结论，不只选择有利表格。
- [ ] 报告不改写validation主方案；如果发现致命缺陷，以机器状态阻止测试协议封存。

## Task 6：封存最终测试协议

**Files:**
- Create: `src/ashare_multifactor/robustness/test_protocol.py`
- Create: `src/ashare_multifactor/robustness/pipeline.py`
- Create: `src/ashare_multifactor/cli/robustness.py`
- Test: `tests/test_sealed_test_protocol.py`

**Interfaces:**
- Produces: `processed/robustness/CURRENT.json`、`sealed_test_protocol.json`。

- [ ] 封存最终代码提交、配置、上游release、主组合、日期、成本、指标、报告模板和随机种子哈希。
- [ ] 明确测试期只允许执行一个权威run，失败运行必须保留记录。
- [ ] 实现测试开启令牌验证；无用户明确批准时拒绝扫描2022年及以后文件。
- [ ] 发布两个独立可复现run后的robustness release，但不打开测试期。

## 验收清单

- [ ] 实验矩阵在结果前冻结；
- [ ] 所有实验只使用2005–2021；
- [ ] 变体没有被用于事后改选主方案；
- [ ] 成本、组合、因子删除和市场分段结果完整；
- [ ] 失败变体已报告；
- [ ] `sealed_test_protocol.json` 可完整复验；
- [ ] 2022–2025仍为零读取；
- [ ] 用户批准前测试开启门禁保持关闭。

