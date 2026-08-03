# v1.0 结果字典

本文档定义报告中核心数字的含义、单位和机器来源。`processed/` 与 `artifacts/` 不进入 Git；下列路径在完整本地复现环境中可用，并由 v1.0 manifest 绑定其上层 manifest 哈希。

## 通用口径

| 字段 | 含义 | 单位/范围 |
|---|---|---|
| `rank_ic` | 月度截面因子分数与未来收益的 Spearman 相关系数均值 | 小数 |
| `icir` | 月度 Rank IC 均值相对波动的年化比率 | 比率 |
| `net_annual_return` / `annual_return` | 扣除对应情景交易成本后的年化收益 | 小数；文档展示为百分比 |
| `annual_volatility` | 日收益年化波动率 | 小数；文档展示为百分比 |
| `maximum_drawdown` | NAV 相对历史高点的最大跌幅 | 负小数；文档展示为百分比 |
| `turnover` | 方案定义下的平均单边换手或累计年化成交额口径 | 需结合具体 release |
| `cost_erosion` | 零成本与完整成本收益差 | 小数；文档展示为百分点 |
| `implementation_shortfall_rate` | 总交易成本除以成交额 | 小数；文档展示为百分比 |

## Stage 4：单因子研究

机器来源：

- `artifacts/factor_research/factor_classifications.csv`
- `artifacts/factor_research/factor_summary.csv`
- `artifacts/factor_research/manifest.json`

| 报告项 | 值 | 来源字段 |
|---|---:|---|
| 预注册因子数 | 14 | 分类表行数 |
| candidate / watch / reject | 6 / 5 / 3 | `classification` 分组计数 |
| candidate 因子 | `amihud_20`、`reversal_20`、`reversal_5`、`turnover_20`、`volatility_20`、`volatility_60` | `factor_name` where `classification=candidate` |

Stage 4 报告 manifest SHA-256：`d1987c69b46ae931cbc21cdf07b4c0661569c56c589d4f79a3fc17a420c92242`。

## Stage 5：因子合成

机器来源：

- `processed/factor_combination/CURRENT.json`
- `processed/factor_combination/releases/667e0ee00d8941e9a1f5db9a963ae9c3/artifacts/composite_summary.csv`
- 同 release 下的 `portfolio_summary.csv` 与 `report.md`

| 报告项 | 值 | 来源字段 |
|---|---:|---|
| `family_equal` 20 日平均 Rank IC | 0.102201 | `mean_rank_ic` |
| `family_equal` 20 日 ICIR | 2.735685 | `icir` |
| 规模分层缓冲平均单边目标换手 | 0.679583 | `average_one_way_turnover` |

Stage 5 run id 为 `667e0ee00d8941e9a1f5db9a963ae9c3`，manifest SHA-256 为 `d846d866f1766c04f755774a6a750999aa16eef35612a350fbe1797447482fb2`。

## Stage 6：研究期正式执行回测

机器来源：`processed/formal_backtest/releases/b995878_stage6_authoritative/artifacts/summary.json`。

| JSON 字段 | 原始值 | 文档显示 |
|---|---:|---:|
| `total_return` | 5.983307426490563 | 598.3307% |
| `annual_return` | 0.17603097829253445 | 17.6031% |
| `annual_volatility` | 0.3027490664985296 | 30.2749% |
| `sharpe_zero_rate` | 0.7078534734714144 | 0.7079 |
| `maximum_drawdown` | -0.6696382491083639 | -66.9638% |
| `trade_count` | 105271 | 105,271 |
| `filled_order_rate` | 0.4942666987410793 | 49.43% |
| `annualized_traded_value_ratio` | 16.109088864309605 | 16.11 倍 |
| `implementation_shortfall_rate` | 0.0038052023509093746 | 0.3805% |
| `average_target_deviation_l1` | 0.3234100480450629 | 32.34% |
| `maximum_scenario_reconciliation_difference` | 2.384185791015625e-07 | 0.00000024 元 |

Stage 6 run id 为 `b995878_stage6_authoritative`，manifest SHA-256 为 `1a6f3cea593fa14cd7e687fae26b33e1da5f2df86c5616742aff0eb22697128e`。

## Stage 7：独立验证期

机器来源：

- `processed/validation_evaluation/releases/65b19e1_stage7_validation_controlled_collector_successor/artifacts/research/validation_metrics.parquet`
- 同目录的 `selection_decision.json`

| 候选 | `rank_ic` | `icir` | `net_annual_return` | `maximum_drawdown` | `turnover` | `cost_erosion` |
|---|---:|---:|---:|---:|---:|---:|
| `family_equal_size_stratified_buffered` | 0.08551673896 | 2.49678280604 | -0.08384776449 | -0.55420748823 | 0.74983333333 | 0.05126997665 |
| `family_equal_top100_equal` | 0.08551673896 | 2.49678280604 | -0.10242196415 | -0.59221464500 | 0.84883333333 | 0.05551657120 |
| `rolling_ic_family_size_stratified_buffered` | 0.08886394849 | 2.63494916068 | -0.06708285725 | -0.53782987205 | 0.72783333333 | 0.05159205698 |

`selection_decision.json` 的 `status` 为 `selected_by_frozen_rules`，`selected_candidate` 为 `rolling_ic_family_size_stratified_buffered`。选择是候选间相对决策，不改变三者净年化收益均为负的事实。

Stage 7 run id 为 `65b19e1_stage7_validation_controlled_collector_successor`，manifest SHA-256 为 `6ee12a10b25a53d8a48c596faa76c1833d92c10c511e570d301a5f96c10836fd`。

## Stage 8：稳健性

机器来源：`processed/robustness/releases/a1076c2_stage8_robustness_szse_statistics_successor/datasets/robustness_results.parquet`。

按唯一 `(experiment_id, status, interpretation)` 汇总：

| 状态 | 实验数 |
|---|---:|
| `ready / stable` | 25 |
| `ready / sensitive` | 9 |
| `descriptive_only / failed` | 1 |
| `unavailable / failed` | 1 |

关键成本情景：

| `experiment_id` | `metric` | `value` | `baseline_delta` |
|---|---|---:|---:|
| `execution_full_cost` | `annual_return` | 0.09846707290 | 0.00000000000 |
| `execution_explicit_only` | `annual_return` | 0.14792430019 | 0.04945722729 |
| `execution_impact_high` | `annual_return` | 0.07081059340 | -0.02765647950 |
| `execution_zero_cost` | `annual_return` | 0.16806380216 | 0.06959672927 |

Stage 8 run id 为 `a1076c2_stage8_robustness_szse_statistics_successor`，manifest SHA-256 为 `c75948b4c47007c6f5690451afbebdff02eb5d3c793e6285eb799af898ea1fba`。

## 最终测试与 v1.0

- `processed/final_test/CURRENT.json`：不存在；
- 最终测试策略结果：不进入 v1.0 报告；
- v1.0 声明：研究与验证完成，不声明最终样本外有效；
- 交付文件哈希：见 `releases/v1.0.0-research-validation.json`。
