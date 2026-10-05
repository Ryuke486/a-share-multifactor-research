# v1.0 结果字典

本文档定义报告中核心数字的含义、单位和机器来源。`processed/` 与 `artifacts/` 不进入 Git；下列路径在完整本地复现环境中可用，并由 v1.0 manifest 绑定其上层 manifest 哈希。

## 通用口径

| 字段 | 含义 | 单位/范围 |
|---|---|---|
| `rank_ic` | 月度截面因子分数与未来收益的 Spearman 相关系数均值 | 小数 |
| `icir` | 年化 ICIR：月度 Rank IC 均值 ÷ 月度 Rank IC 标准差 × √12；按月度口径需除以 √12 | 比率 |
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
| `family_equal` 20 日年化 ICIR | 2.735685 | `icir` |
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

执行指标口径：

- `filled_order_rate`：`status = filled`（剩余数量为零）的订单数 ÷ 全部订单数。部分成交后被新信号取消的订单不计入，因此它是按笔数的"完全成交率"，不是按股数或金额的成交比例。
- `average_target_deviation_l1`：每次新信号第一个执行日收盘时，实际权重与目标权重的 L1 距离（双边求和），再对全部执行日取平均；不是持有期内的平均偏离。

按股数的未成交比例从同一 release 的 `datasets/orders.parquet` 计算（`sum(remaining_quantity) / sum(quantity)`，按 `side` 过滤）：

| 订单方向 | 原始值 | 文档显示 |
|---|---:|---:|
| 买单 | 0.037243336468061765 | 3.72% |
| 卖单 | 0.029608056339574165 | 2.96% |

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

实验覆盖区间：执行成本类实验在 2005–2021 全区间重跑执行器；组合、因子删除和滚动窗口类实验只重建 2017–2021 目标，2005–2016 段沿用 Stage 5 研究期目标（`validation/backtest_inputs.py` 中 `_RESEARCH_PORTFOLIO` 的映射），因此其 2005–2021 指标中研究期部分对所有变体相同。`regime_pre_registered` 的 `market_size:*` 样本只覆盖 2017–2021，取值为股票池大/小盘股票的平均 20 日未来收益，不是策略收益。

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

## 描述性补充：基准对比与多空两条腿

机器来源：`artifacts/v1_supplements/`（由 `ashare_multifactor.cli.supplements` 生成，`manifest.json` 记录全部输入输出哈希）。数字本身由 `docs/results/v1.0-key-results.json` 绑定，下面只定义字段。

| 文件 | 字段 | 含义 |
|---|---|---|
| `relative_performance.csv` | `benchmark_annual_return` | 零成本股票池基准年化收益；`benchmark = equal_weight` 为等权，`cap_weight` 为总市值加权 |
| 同上 | `annual_relative_return` | 几何超额年化：(策略累计增长 ÷ 基准累计增长)^(1/年数) − 1；年数口径与 Stage 6/7 一致 |
| 同上 | `information_ratio` | 日超额收益均值 × 252 ÷ 跟踪误差；`tracking_error` 为日超额收益标准差 × √252 |
| 同上 | `beta` / `correlation` | 策略日收益对基准日收益的 beta 与相关系数 |
| 同上 | `cost_mode` | `net` 为完整成本 NAV，`zero_cost` 为同一执行路径的零成本影子 NAV |
| `calendar_year_returns.csv` | `strategy_net` 等 | 选定方案与两个基准的自然年收益，以上一年最后一个交易日为基点 |
| `leg_decomposition.csv` | `long_leg` / `short_leg` | 多头腿 = 最高五分组 − 股票池均值；空头腿 = 股票池均值 − 最低五分组；按月计算后取均值，单位为月收益小数 |
| 同上 | `long_leg_t` / `short_leg_t` | Newey–West t 值，滞后阶数与因子评价相同（20 日标签为 0） |
| `benchmark_daily.parquet` | — | 选定方案 NAV 与两个基准的逐日增长序列，供图表使用 |
| `executable_ic.csv` | `close_mean_ic` / `open_mean_ic` | 同一共同样本上，收盘到收盘标签与“下一交易日开盘买入、h 个有效交易日后开盘卖出”标签的平均月度 Rank IC |
| 同上 | `mean_ic_retained` / `label_coverage` | 可成交口径 IC ÷ 收盘口径 IC；共同样本行数 ÷ 收盘标签有效行数 |
| `newey_west_lags.csv` | `t_published_lag` / `t_automatic_lag` | 已发布口径（20 日标签滞后 0）与 Newey–West（1994）自动滞后 floor(4·(T/100)^(2/9)) 下的 t 值；前者与已发布 `nw_t` 逐项一致 |
| 同上 | `q_published_lag` / `q_automatic_lag` | 14 个因子主口径作为一个检验族的 BH q 值；前者与已发布 `bh_q` 逐项一致 |

## 最终测试与 v1.0

- `processed/final_test/CURRENT.json`：不存在；
- 最终测试策略结果：不进入 v1.0 报告；
- v1.0 声明：研究与验证完成，不声明最终样本外有效；
- 交付文件哈希：见 `releases/v1.0.0-research-validation.json`。
