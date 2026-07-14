# A股横截面多因子研究

本项目研究A股横截面因子的预测能力，并在严格控制前视偏差、幸存者偏差和交易时序的前提下，构建含交易成本的可审计组合。当前已完成60日动量MVP和阶段四完整单因子研究；后者只使用2005–2016研究期，不是正式组合回测，也不构成投资建议。

## 阶段四结果边界

- 2003–2004只用于预热，2005–2016为唯一评价区间；2017年及以后数据未开启。
- 统一 `factor_panel` 含2,015,188个唯一 `(date, symbol, factor_name)` 观测，日期为2005-01-31至2016-12-30，覆盖14个预注册因子。
- 冻结规则下分类为6个candidate、5个watch、3个reject：candidate为 `amihud_20`、`reversal_20`、`reversal_5`、`turnover_20`、`volatility_20`、`volatility_60`；watch为 `bp`、`downside_volatility_60`、`ep_ttm`、`log_market_cap`、`sp_ttm`；reject为 `momentum_120`、`momentum_12_1`、`momentum_60`。
- 价值字段的历史point-in-time口径、历史行业和ST来源仍未核验；本阶段不做行业中性化。candidate只表示通过预注册的研究期门槛，不代表样本外有效或未来收益承诺。
- 产物包含14张独立因子卡片、56张标准图、36个 `processed` 文件和81个 `artifacts` 文件；重跑后合计117个产物文件哈希零变化。数据质量记录的哈希已绑定到清单和血缘，后续阶段不能在质量文件缺失或被替换时继续。

## 当前MVP范围

- 逐日形成最多200只股票的动态股票池：至少252个历史观测、非ST、当日原始OHLC有效，并按过去20个观测的平均成交额筛选流动性。
- 使用后复权收盘价计算60日动量；20日未来收益只作为Rank IC评价标签，不进入股票池、因子或权重。
- 每个真实月末生成信号，按动量取前20只等权；信号在t日收盘后形成，最早在下一真实交易日开盘执行。
- 同时保存毛收益与扣除统一单边10bp成本的净收益，以及目标、成交、持仓、拒单、净值、图表和机器生成报告。

## 数据准备

原始数据应放在配置指定的只读目录：

```text
Data/每天一个文件/不复权/
Data/每天一个文件/后复权/
```

第一版只读取这两类每日文件，并按 `(date, symbol)` 严格一对一连接：不复权数据用于研究和成交状态，后复权数据用于跨期收益。原始CSV不随仓库发布，也不得提交Git，原因是数据量大，且来源、授权和部分字段口径仍待补全；仓库只允许提交极小的人工合成测试夹具。完整数据清单和限制见 [`docs/data/README.md`](docs/data/README.md)。`processed/` 和 `artifacts/` 均为可再生且默认忽略的目录。

## 安装

需要Python 3.12或更高版本。在仓库根目录执行：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

## 运行

从只读原始日文件生成阶段四全部结果：

```bash
.venv/bin/ashare-factors --config configs/research_protocol.yaml --stage build
.venv/bin/ashare-factors --config configs/research_protocol.yaml --stage all
```

`build` 仅构建2003–2016隔离数据集；`all` 依次执行字段审计、因子、评价和报告。阶段四不做因子合成、组合优化或正式回测，也不读取2017年及以后数据。

从阶段四产物生成阶段五复合分数、目标权重和事前诊断：

```bash
.venv/bin/python -m ashare_multifactor.cli.combinations --config configs/research_protocol.yaml
```

阶段五只消费 `processed/factor_research/`，不读取原始CSV；滚动IC权重严格滞后一期。每次完整运行写入不可变release，`CURRENT.json`只在全部校验通过后切换。输出是阶段六的事前目标，不是真实成交回测。

阶段三MVP仍可独立复现：

从原始日文件重建2012–2015数据底座并完成全部MVP步骤：

```bash
.venv/bin/ashare-mvp --config configs/research_protocol.yaml --stage all
```

也可按依赖顺序分阶段运行，便于审计或断点续跑：

```bash
.venv/bin/ashare-mvp --config configs/research_protocol.yaml --stage build
.venv/bin/ashare-mvp --config configs/research_protocol.yaml --stage signals
.venv/bin/ashare-mvp --config configs/research_protocol.yaml --stage targets
.venv/bin/ashare-mvp --config configs/research_protocol.yaml --stage backtest
.venv/bin/ashare-mvp --config configs/research_protocol.yaml --stage report
```

后续阶段会校验配置、源数据清单和上游文件指纹；若上游产物缺失、被替换或与当前配置不一致，流程会拒绝继续。不要跳过依赖阶段复用来源不明的中间结果。

## 输出

- `processed/daily_panel/`：2012–2015按年份分区的统一Parquet、数据清单和质量问题记录；清单保存每个分区的行数、日期范围、大小和SHA-256。
- `processed/mvp/`：信号、月末截面、Rank IC、目标权重、净回测成交/持仓/拒单账本、毛/净NAV与汇总，以及血缘信息等中间产物。
- `artifacts/mvp/`：便于审计的 `rank_ic.csv`、`target_weights.parquet`、`trades.csv`、`holdings.parquet`、`blocked_orders.csv`、`nav.csv`、`gross_nav.csv`、`summary.json`、图表、运行清单、数据质量摘要和 `report.md`。
- `processed/factor_combination/CURRENT.json`：当前权威阶段五release身份及manifest哈希。
- `processed/factor_combination/releases/<run_id>/datasets/`：四种复合分数、每月因子权重、两套目标权重和组合诊断。
- `processed/factor_combination/releases/<run_id>/artifacts/`：复合IC、月度截面相关性、换手、暴露、容量代理、质量问题、图表和机器生成报告。

`report.md`中的指标从机器可读产物生成，不手工抄写回测数字。报告只说明管道可以运行、复现和审计，不应解读为策略赚钱、稳健或具备未来收益能力。

## 日期边界与防泄漏

日期统一由 `configs/research_protocol.yaml` 管理：

| 用途 | 日期 | 当前状态 |
|---|---|---|
| 研究期 | 2005-01-01至2016-12-31 | 研究协议固定 |
| 验证期 | 2017-01-01至2021-12-31 | 与研究期隔离 |
| 最终测试期 | 2022-01-01至2025-12-31 | 封存，不读取、不统计、不绘图 |
| MVP预热数据 | 2012-01-01至2013-12-31 | 只形成历史窗口，不计入绩效 |
| MVP分析期 | 2014-01-01至2015-12-31 | 当前管道验证范围 |
| 阶段四预热 | 2003-01-01至2004-12-31 | 只形成历史窗口 |
| 阶段四评价 | 2005-01-01至2016-12-31 | 完整单因子研究 |

- 股票池、因子和目标权重在t日只使用t日及以前的信息；历史股票池逐日形成，不用当前存续股票列表或未来退市时间回看历史。
- 20日未来收益只作标签；月末和t+1执行日均来自真实交易日序列，禁止用自然日简单加一。
- 构建器只允许MVP预热与分析区间，数据清单和各阶段输出还会校验日期上限与血缘。
- 阶段四已完成但不解封后续区间；阶段五仍只使用研究期，验证期和最终测试期继续封存。

## 当前简化与缺价规则

MVP允许分数持仓，使用统一单边10bp成本。真实行情缺价采用最小、可审计规则：

1. 持仓缺少当日有效价格时，使用该证券最近已知收盘价估值。
2. 调仓证券缺少执行日开盘价时不成交，并写入 `blocked_orders`，原因标记为 `missing_open_price`。
3. 冻结缺价持仓后，为全部可交易目标使用同一个兼顾冻结市值与全部可执行交易成本的最大可行资金基数；再先卖后买，保持可交易目标间原权重比例。
4. 拒单不追单，也不在月内补单；下一月重新生成目标组合。

这些规则不是完整停牌或撮合系统。当前仍未实现涨跌停约束、整手交易、历史佣金与印花税变化、滑点和冲击成本，以及完整挂单/撤单/追单系统；这些属于后续正式A股回测阶段。

## 验证

以下命令只运行人工合成测试和静态检查，不需要读取真实 `Data/`：

```bash
.venv/bin/pytest -v
.venv/bin/ruff check src tests
```
