# A股60日动量小闭环 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 基于2012–2015统一Parquet，在不触碰最终测试期的前提下跑通动态股票池、60日动量、20日未来收益、月度Rank IC、前20等权、t+1开盘执行、10bp成本和自动报告。

**Architecture:** 研究层只读取数据底座Parquet。2012–2013仅用于预热，2014–2015用于MVP分析；因子、标签、目标权重、回测和报告逐层输出独立文件，以便单独审计。

**Tech Stack:** Python 3.12+、Polars、NumPy、SciPy、Matplotlib、pytest。

## Dependencies

- 先完成 2026-07-13-01-research-protocol.md。
- 再完成 2026-07-13-02-data-foundation.md。
- 输入manifest最大日期必须不晚于2015-12-31。

## Global Constraints

- 股票池和动量只使用t日及以前信息。
- forward_return_20只作标签，不进入因子或股票池。
- 月末日期来自真实交易日。
- 信号在t日收盘形成，t+1真实交易日开盘执行。
- 小闭环允许分数持仓并采用统一单边10bp成本。
- 不实现机器学习、优化器、涨跌停撮合、整手和历史税费变化。
- 真实数据缺行时采用最小可交易性规则：持仓用最近已知收盘价估值；执行日缺少开盘价的调整不得成交并写入blocked_orders；冻结缺价持仓后，所有可交易目标共用一个同时考虑冻结市值和全部可执行交易成本的最大可行资金基数，再先卖后买；拒单不替换、不追单，下一月重新生成目标。
- 报告必须明确这是管道验证，不是最终投资结论。

## Approved Execution Adjustment

- Task 1 Step 6和Task 2 Step 6所需的分阶段命令入口由Task 4创建，因此这两次真实数据验证延后到Task 4，在命令入口完成后按原验收标准补跑。
- 此调整仅修正实施依赖顺序，不改变研究日期、股票池、因子、标签、组合、成本或输出口径。
- Task 4 Step 6的真实`all`运行与Task 5 Step 4的从空目录重建合并为Task 5的一次最终真实验收，避免对约4GB的2012–2015原始CSV重复扫描；Task 4完成后仍先基于阶段2已审计Parquet补跑`signals`、`targets`、`backtest`和`report`分阶段验证。
- 合并运行仍从空的工作树`processed/`和`artifacts/`开始，不复用生成结果，因此不降低一键复现、日期封存或产物验收标准。
- 早期按朴素目标持仓作出的诊断为：2014–2015共201个持仓缺价证券日，23次调仓中13次受缺少开盘价影响。当前最终实际回测产物的口径为711个陈旧估值证券日和38条拒单，拒单覆盖20个不同execution_date。经用户批准，阶段3增加上述最小缺价估值和拒单审计，但仍不实现阶段6的涨跌停、整手、历史费率和挂单追踪。
- 当前macOS文件系统大小写不敏感，`data/`与只读原始目录`Data/`冲突。经用户批准，数据说明文件从`data/README.md`调整为`docs/data/README.md`，禁止为满足计划而写入`Data/`。

## Files Covered

~~~text
src/ashare_multifactor/research/__init__.py
src/ashare_multifactor/research/universe.py
src/ashare_multifactor/research/momentum.py
src/ashare_multifactor/research/evaluation.py
src/ashare_multifactor/research/portfolio.py
src/ashare_multifactor/research/execution.py
src/ashare_multifactor/research/backtest.py
src/ashare_multifactor/research/report.py
src/ashare_multifactor/research/pipeline_io.py
src/ashare_multifactor/research/lineage.py
src/ashare_multifactor/research/pipeline_audit.py
src/ashare_multifactor/research/mvp.py
src/ashare_multifactor/__main__.py
tests/test_universe_momentum.py
tests/test_evaluation_portfolio.py
tests/test_backtest.py
tests/test_mvp_integration.py
artifacts/mvp/                 # Git ignored
~~~

## Stable Data Flow

~~~text
yearly parquet
  -> dynamic universe + momentum_60 + forward_return_20
  -> monthly signals + rank_ic
  -> target weights
  -> trades + holdings + nav
  -> summary + charts + report.md
~~~

---

### Task 1: 构造动态股票池、60日动量和20日未来收益

**Files:**
- Create: src/ashare_multifactor/research/__init__.py
- Create: src/ashare_multifactor/research/universe.py
- Create: src/ashare_multifactor/research/momentum.py
- Create: tests/test_universe_momentum.py

**Interfaces:**
- Consumes: 2012–2015统一面板和ResearchConfig。
- Produces: date、symbol、is_eligible、momentum_60、momentum_z、forward_return_20。

- [x] **Step 1: 写无前视股票池测试**

测试使用两只股票：

- A有252个历史观测且流动性高；
- B只有20个历史观测；
- 在第252日，A可进入，B不可进入；
- 修改未来成交额不得改变较早日期的股票池。

- [x] **Step 2: 实现股票池字段**

build_universe必须按symbol/date排序并生成：

~~~text
history_observations
amount_mean_20
liquidity_rank
is_eligible
~~~

is_eligible精确定义：

~~~text
history_observations >= 252
AND is_st == false
AND open_raw/high_raw/low_raw/close_raw均有效
AND amount_mean_20 > 0
AND liquidity_rank <= universe_size
~~~

liquidity_rank必须在每个日期内按过去20日平均成交额降序计算，不能使用全样本平均成交额。

- [x] **Step 3: 写动量和标签测试**

对一条人工价格序列断言：

~~~python
expected_momentum = close_adj[t] / close_adj[t - 60] - 1
expected_forward = close_adj[t + 20] / close_adj[t] - 1
~~~

并断言修改t+21之后价格不影响t日标签。

- [x] **Step 4: 实现因子和标签**

add_momentum_and_forward_return必须：

1. 计算momentum_60；
2. 计算forward_return_20；
3. 在每个日期、仅is_eligible股票中计算momentum_z；
4. 标准差为0时momentum_z置空；
5. 最终过滤到smoke_analysis日期2014-01-01至2015-12-31。

- [x] **Step 5: 运行测试**

~~~bash
.venv/bin/pytest tests/test_universe_momentum.py -v
~~~

- [x] **Step 6: 写小闭环信号文件（延后至Task 4命令入口完成后验证）**

Run:

~~~bash
.venv/bin/python -m ashare_multifactor.research.mvp --config configs/research_protocol.yaml --stage signals
~~~

Expected: processed/mvp/signals.parquet存在，日期仅在2014–2015，单日可投资证券不超过200。

- [x] **Step 7: 提交股票池与因子**

~~~bash
git add src/ashare_multifactor/research tests/test_universe_momentum.py
git commit -m "feat: build point-in-time mvp momentum panel"
~~~

**Task 7 Acceptance Checklist**

- [x] 股票池按日动态形成。
- [x] 上市历史、流动性和ST条件均无未来数据。
- [x] momentum只使用t及以前数据。
- [x] forward_return只作为标签，不进入因子计算。

---

### Task 2: 月末截面、Rank IC与前20等权目标组合

**Files:**
- Create: src/ashare_multifactor/research/evaluation.py
- Create: src/ashare_multifactor/research/portfolio.py
- Create: tests/test_evaluation_portfolio.py

**Interfaces:**
- Consumes: Task 1信号面板。
- Produces: monthly_signals.parquet、rank_ic.csv、target_weights.parquet。

- [x] **Step 1: 写月末选择测试**

给定一个自然月的三个交易日，monthly_signal_panel只能保留最后一个实际交易日；不得假定自然月最后一天一定开市。

- [x] **Step 2: 写Rank IC测试**

构造因子排序和未来收益排序完全一致的截面：

~~~python
ic = rank_ic_by_date(frame)
assert ic["rank_ic"][0] == pytest.approx(1.0)
~~~

样本少于20只或存在常量因子时，IC应为空并记录原因。

- [x] **Step 3: 实现月末面板和IC**

monthly_signal_panel按year-month分组取最大交易日；rank_ic_by_date对momentum_z和forward_return_20计算Spearman相关，并输出：

~~~text
date
n_stocks
rank_ic
reason
~~~

- [x] **Step 4: 写目标权重测试**

给定25只股票：

- 仅因子最高的20只权重大于0；
- 权重总和为1；
- 每只入选股票权重为0.05；
- symbol相同时排序稳定；
- target记录signal_date和下一交易日execution_date。

- [x] **Step 5: 实现目标权重**

build_target_weights按momentum_z降序、symbol升序打破并列，取portfolio_size只并等权。execution_date必须通过真实交易日表寻找signal_date之后的第一个交易日，不得简单加一天。

- [x] **Step 6: 运行测试；真实MVP阶段延后至Task 4命令入口完成后验证**

~~~bash
.venv/bin/pytest tests/test_evaluation_portfolio.py -v
.venv/bin/python -m ashare_multifactor.research.mvp --config configs/research_protocol.yaml --stage targets
~~~

Expected: 2014–2015每个月最多一个信号日；每个信号日权重和为1。

- [x] **Step 7: 提交评价与组合模块**

~~~bash
git add src/ashare_multifactor/research/evaluation.py src/ashare_multifactor/research/portfolio.py tests/test_evaluation_portfolio.py
git commit -m "feat: evaluate rank ic and create monthly targets"
~~~

**Task 8 Acceptance Checklist**

- [x] 月末日期来自真实交易日。
- [x] IC使用截面Spearman相关。
- [x] 目标权重稳定、可复现且总和为1。
- [x] execution_date严格晚于signal_date。

---

### Task 3: t+1开盘执行的10bp成本回测

**Files:**
- Create: src/ashare_multifactor/research/backtest.py
- Create: tests/test_backtest.py

**Interfaces:**
- Consumes: 日度open_adj/close_adj与target_weights。
- Produces: BacktestResult(nav, trades, holdings, blocked_orders, summary)。

- [x] **Step 1: 写无同日成交测试**

信号日期为2014-01-02，下一交易日为2014-01-03。断言：

- 2014-01-02没有持仓和交易；
- 第一笔成交日期是2014-01-03；
- 成交价格是2014-01-03的open_adj。

- [x] **Step 2: 写成本测试**

初始资金1,000,000，统一10bp成本率为0.001时，首次全额建仓的数学最大可行资金基数为`1,000,000 / (1 + 0.001)`，成本为该金额乘以0.001，期末现金在浮点容差内为0。断言成本从现金扣除，且零成本回测的净值不低于有成本回测。

- [x] **Step 3: 定义输出对象**

~~~python
@dataclass(frozen=True)
class BacktestResult:
    nav: pl.DataFrame
    trades: pl.DataFrame
    holdings: pl.DataFrame
    blocked_orders: pl.DataFrame
    summary: dict[str, float]
~~~

- [x] **Step 4: 实现事件循环**

逐交易日执行顺序固定为：

1. 用当日open_adj给旧持仓估值；
2. 如果当日是execution_date，计算目标名义金额；
3. 计算总交易金额和10bp成本；
4. 从可投资资金中扣除成本；
5. 按当日open_adj生成分数单位持仓；
6. 用当日close_adj计算收盘净值；
7. 保存交易、持仓和净值。

小闭环仅对缺失行情实施最近收盘估值和拒单，不自动追单；不实现整手、涨跌停和分段税费。summary和报告必须披露这些限制，blocked_orders必须保留拒单原因。

- [x] **Step 5: 定义核心汇总指标**

summary至少包含：

~~~text
initial_cash
final_nav
total_return
annualized_return
annualized_volatility
sharpe_zero_rf
max_drawdown
average_turnover
total_cost
~~~

- [x] **Step 6: 运行测试**

~~~bash
.venv/bin/pytest tests/test_backtest.py -v
~~~

- [x] **Step 7: 提交回测模块**

~~~bash
git add src/ashare_multifactor/research/backtest.py tests/test_backtest.py
git commit -m "feat: backtest monthly targets with next-open costs"
~~~

**Task 9 Acceptance Checklist**

- [x] t日信号不会在t日成交。
- [x] 成本计入现金和净值。
- [x] 交易、持仓和净值能够对账。
- [x] 分数持仓和简化交易规则被明确披露。
- [x] 缺价持仓只用过去最近收盘估值，缺少开盘价的调整进入blocked_orders且不追单。

---

### Task 4: 一键MVP管道与研究报告

**Files:**
- Create: src/ashare_multifactor/research/report.py
- Create: src/ashare_multifactor/research/mvp.py
- Create: src/ashare_multifactor/__main__.py
- Create: tests/test_mvp_integration.py

**Interfaces:**
- Consumes: research_protocol.yaml。
- Produces: artifacts/mvp下的全部可审计结果。

- [x] **Step 1: 写端到端集成测试**

使用tmp_path中至少70个交易日、25只证券的合成数据，执行run_mvp后断言以下文件存在：

~~~text
manifest.json
data_quality.json
rank_ic.csv
target_weights.parquet
trades.csv
holdings.parquet
blocked_orders.csv
nav.csv
nav.png
drawdown.png
summary.json
report.md
~~~

并断言所有输出日期不超过配置的smoke_analysis.end。

- [x] **Step 2: 实现报告写出**

report.md必须包含：

1. 研究问题和日期范围；
2. 股票池和因子定义；
3. Rank IC均值、标准差和有效月份数；
4. 毛收益、净收益、回撤、换手和总成本；
5. 2012–2013预热期不计入绩效；
6. 分数持仓、统一10bp、缺价持仓最近收盘估值、缺少开盘价拒单且不追单，以及未处理涨跌停/整手/税费变化等限制；
7. 明确声明该结果仅为管道验证。

- [x] **Step 3: 实现run_mvp**

run_mvp(config_path)按固定顺序调用：

~~~text
load_config
build_parquet_dataset
build_universe
add_momentum_and_forward_return
monthly_signal_panel
rank_ic_by_date
build_target_weights
run_backtest
write_mvp_report
~~~

每一步开始和结束都记录输入行数、输出行数和日期范围。

- [x] **Step 4: 实现命令行**

~~~bash
.venv/bin/ashare-mvp --config configs/research_protocol.yaml --stage all
~~~

允许的stage仅为build、signals、targets、backtest、report、all；非法值必须非零退出。

- [x] **Step 5: 运行集成测试**

~~~bash
.venv/bin/pytest tests/test_mvp_integration.py -v
~~~

- [x] **Step 6: 在真实数据上运行完整小闭环**

~~~bash
.venv/bin/ashare-mvp --config configs/research_protocol.yaml --stage all
~~~

Expected: artifacts/mvp全部输出存在；日志显示最大读取日期2015-12-31；最终测试期未被扫描。

- [x] **Step 7: 提交管道和报告模块**

~~~bash
git add src/ashare_multifactor/research/report.py src/ashare_multifactor/research/mvp.py src/ashare_multifactor/__main__.py tests/test_mvp_integration.py
git commit -m "feat: run reproducible multifactor mvp pipeline"
~~~

**Task 10 Acceptance Checklist**

- [x] 一条命令可以从配置生成全部MVP结果。
- [x] 报告数字来自生成文件，不由人工复制。
- [x] 报告明确区分管道验证和研究结论。
- [x] 输出日期没有越过2015-12-31。

---

### Task 5: 全量验收、文档和Git安全检查

**Files:**
- Modify: README.md
- Create: docs/data/README.md

**Interfaces:**
- Consumes: 研究协议、数据底座及本计划Task 1–4的代码和MVP产物。
- Produces: 可交付的第一阶段仓库和通过记录。

- [x] **Step 1: 完成README**

README必须包含：

- 项目目标和当前范围；
- 数据不随仓库发布的原因；
- 环境安装；
- 一键运行命令；
- 输出目录说明；
- 研究/验证/测试日期；
- 小闭环日期和预热期；
- 数据泄漏防护；
- 当前限制；
- 下一阶段只扩展因子库，不改动最终测试期。

- [x] **Step 2: 完成docs/data/README.md**

必须记录：

- 当前数据目录布局；
- 实测覆盖2000-01-04至2026-05-14；
- 6,382个日文件、5,841个证券文件；
- 37/38列schema变化；
- 2026年5月日期缺失；
- 数据来源、授权和字段口径尚需用户补全；
- GitHub只提交小型人工合成夹具，不提交原始CSV。

- [x] **Step 3: 运行完整测试与静态检查**

Run:

~~~bash
.venv/bin/pytest -v
.venv/bin/ruff check src tests
~~~

Expected: 0 failed；ruff 0 errors。

- [x] **Step 4: 运行真实MVP并核对产物**

Run:

~~~bash
rm -rf processed artifacts
.venv/bin/ashare-mvp --config configs/research_protocol.yaml --stage all
~~~

Expected:

- 流程从空的processed/artifacts重新生成成功；
- manifest最大日期为2015-12-31；
- rank_ic.csv、target_weights.parquet、nav.csv和report.md非空；
- 目标权重每个信号日合计为1；
- 第一笔成交晚于第一笔信号；
- total_cost大于0；
- nav无负值和空值。

- [x] **Step 5: 验证Git没有原始或生成数据**

Run:

~~~bash
git status --short
git ls-files Data processed artifacts
git check-ignore Data processed artifacts
~~~

Expected: git ls-files不输出Data/processed/artifacts文件；check-ignore确认三者被忽略。

- [x] **Step 6: 最终提交**

> 用户完成阶段3验收后明确批准发布。由于实施期间按总指令保持未提交状态，上述分任务提交项最终合并为一个经完整测试和独立审查的阶段3综合提交。

~~~bash
git add README.md docs/data/README.md
git commit -m "docs: document mvp reproduction and data limitations"
~~~

## Final Acceptance Checklist

### 研究协议

- [x] 研究、验证、测试日期固定且互不重叠。
- [x] 小闭环使用2012–2013预热、2014–2015分析。
- [x] 最终测试期2022–2025没有被小闭环读取。
- [x] 信号、标签、成交和成本时间顺序写清楚。

### 数据底座

- [x] Data目录只读且不被Git追踪。
- [x] 不复权和后复权文件按交易日、证券代码一对一连接。
- [x] 代码保留六位字符串。
- [x] 37/38列schema变化被显式处理。
- [x] Parquet按年份分区，并带manifest和质量报告。
- [x] 严重质量问题会阻断构建。

### 小闭环

- [x] 逐日股票池只使用当时信息。
- [x] 60日动量只依赖t及以前价格。
- [x] 20日未来收益只作为标签。
- [x] 月末日期来自真实交易日。
- [x] Rank IC按月输出。
- [x] 前20只等权，权重和为1。
- [x] t+1开盘执行，成本从净值扣除。
- [x] 一条命令能从配置重新生成结果。
- [x] 报告明确说明结果不是最终投资结论。

### 工程与复现

- [x] pytest全部通过。
- [x] ruff检查通过。
- [x] 重跑结果一致。
- [x] 原始和生成数据均未进入Git。
- [x] README和docs/data/README足以让陌生人理解数据边界和运行方法。

## Execution Order

严格按Task 1至Task 5顺序执行。Task 1–4构成“60日动量小闭环”里程碑，Task 5是阶段3的验收与交付门。

## MVP Acceptance Checklist

- [x] 2012–2013只作为预热，不计入绩效。
- [x] 2014–2015逐日动态股票池最多200只。
- [x] 60日动量没有未来信息。
- [x] 20日未来收益只作为评价标签。
- [x] 每月最多一个真实月末信号日。
- [x] 每个信号日目标组合前20只等权且权重和为1。
- [x] 第一笔成交晚于第一笔信号，并使用t+1 open_adj。
- [x] 10bp成本进入现金和净值。
- [x] rank_ic.csv、target_weights.parquet、trades.csv、blocked_orders.csv、nav.csv和report.md均非空。
- [x] 一条命令可从配置重建全部MVP结果。
- [x] 报告披露分数持仓和简化交易规则。
- [x] 所有输出日期不晚于2015-12-31。

## Estimated Effort

3至5个专注工作日；完成后再扩展完整因子库与正式交易规则。
