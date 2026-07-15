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

## Task 1：预注册稳健性实验矩阵

**Files:**
- Create: `configs/robustness_protocol.yaml`
- Create: `src/ashare_multifactor/robustness/protocol.py`
- Test: `tests/test_robustness_protocol.py`

- [x] 在运行结果前冻结实验名称、参数、对照组、指标和解释规则。
- [x] 固定实验随机种子和稳定排序，禁止重复实验ID。
- [x] 测试配置不得包含2022年及以后日期，不得改变主方案身份。

## Task 2：成本和执行假设敏感性

**Files:**
- Create: `src/ashare_multifactor/robustness/execution_sensitivity.py`
- Test: `tests/test_execution_sensitivity.py`

- [x] 保留零成本、仅显式费用和完整成本主场景。
- [x] 在预注册的保守范围内改变滑点、冲击和最大参与率，不改变信号。
- [x] 报告收益、换手、成本侵蚀、未成交率和目标偏离的联合变化。

## Task 3：组合规则和因子删除敏感性

**Files:**
- Create: `src/ashare_multifactor/robustness/portfolio_sensitivity.py`
- Create: `src/ashare_multifactor/robustness/factor_ablation.py`
- Test: `tests/test_portfolio_factor_sensitivity.py`

- [x] 变体必须围绕主方案对称设置，不根据结果继续细分参数。
- [x] 每次只改变一类假设，保持其他代码、数据和成本不变。
- [x] 因子删除使用族别级别，不在本板块尝试新因子。

## Task 4：时间、市场和截面分段

**Files:**
- Create: `src/ashare_multifactor/robustness/regime_analysis.py`
- Test: `tests/test_regime_analysis.py`

- [x] 市场状态只用当期可观察市场数据定义，禁止按策略收益事后切段。
- [x] 历史行业口径仍未验证时，行业结果仅作限制性描述，不作为主结论。
- [x] 每个子样本报告观测数、时间范围和不确定性，避免小样本过度解释。

## Task 5：稳健性汇总与失败结果报告

**Files:**
- Create: `src/ashare_multifactor/robustness/summary.py`
- Create: `src/ashare_multifactor/robustness/report.py`
- Test: `tests/test_robustness_summary.py`

- [x] 使用统一长表schema记录experiment ID、唯一改动、样本、指标和基线差异。
- [x] 输出稳定、敏感和失败结论，不只选择有利表格。
- [x] 报告不改写validation主方案；如果发现致命缺陷，以机器状态阻止测试协议封存。

## Task 6：封存最终测试协议

**Files:**
- Create: `src/ashare_multifactor/robustness/test_protocol.py`
- Create: `src/ashare_multifactor/robustness/pipeline.py`
- Create: `src/ashare_multifactor/cli/robustness.py`
- Test: `tests/test_sealed_test_protocol.py`

- [x] 实现封存最终代码提交、配置、上游release、主组合、日期、成本、指标、报告模板和随机种子哈希的机器门禁。
- [x] 明确测试期只允许执行一个权威run，失败运行必须保留记录。
- [x] 实现测试开启令牌验证；无用户明确批准时拒绝扫描2022年及以后文件。
- [x] 在同一干净提交下发布两个独立可复现run后的robustness release，但不打开测试期。

## 验收清单

- [x] 实验矩阵在结果前冻结；
- [x] 所有实验只使用2005–2021；
- [x] 变体没有被用于事后改选主方案；
- [x] 成本、组合、因子删除和市场分段结果已生成；
- [x] 失败变体已报告；
- [x] `sealed_test_protocol.json` 已在干净提交下正式发布并可完整复验；
- [x] 2022–2025仍为零读取；
- [x] 用户批准前测试开启门禁保持关闭。

## 实际执行状态（2026-07-15）

- 板块7权威release：`efc0267_stage7_validation`，manifest SHA-256 `c047f81c62d49b54159bff8765276eed683e541aaa826ad7173ea566206be728`。
- 两个初始dirty开发run：`stage8_dirty_1`、`stage8_dirty_2`；3个核心文件哈希一致，`outputs_identical=true`。
- 加固后真实全链路run：`stage8_hardened_dirty_v2`；242行、27个实验，最大日期`2021-12-31`，机器门禁`ready_to_seal`。首次加固run在规模分段列顺序处失败并自动删除，随后以失败回归测试完成修复。
- 发布与封存加固：权威期间契约在Parquet扫描前校验；实际Stage5/因子研究输入全部绑定哈希；发布前复验双run源文件；最终测试令牌采用外部密钥HMAC、sealed payload完整性重算和唯一绝对账本路径的原子单次消费。
- 独立代码审查最终结论：Critical 0、Important 0；全量`665 passed`及ruff通过。
- 干净实现提交：`58adad4f4b376af4b966dcab68bcb8c1602a4170`。两个独立完整run为`58adad4_stage8_clean_1`、`58adad4_stage8_clean_2`，`outputs_identical=true`、`release_eligible=true`。
- 权威release：`58adad4_stage8_robustness`；manifest SHA-256 `975291896c01cd7fdf7ab2a56247a28ac9f8cbc20d26708f76b5e25bfe620a21`。3个核心哈希为：results `777f47f82c5bbe0b8d395b3779c21b4952554b7fb1d3e87442dfab71b7a28aa5`、report `484e6216f62a96947aa3f6a8f7000597ef5734b8c6df7147bec54eff8016c90f`、gate `6e689301b9945a6767e50249b4b7dab84bca916f56936d733456815aabbe3ccf`。
- `sealed_test_protocol.json` payload哈希、报告模板哈希、`CURRENT.json`和release manifest已完整复验；`opening_token_status=closed`，唯一消费账本不存在，2022–2025未打开。
- 机器门禁：`ready_to_seal`；无致命实现、协议或账务缺陷。
- 失败/限制性证据：1200只股票池超出已冻结上游1000只范围，记为`unavailable`；历史行业口径未验证，记为`descriptive_only`。
- 板块8已正式发布并封存最终测试协议；板块9仍须用户另行明确授权并提供外部开启密钥，当前保持关闭。
