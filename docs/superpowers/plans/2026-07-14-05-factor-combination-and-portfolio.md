# A股因子合成与目标组合构建 Implementation Plan

> **执行说明：** 按复选框逐项实施。阶段五只在研究期内完成因子合成、目标组合与事前诊断，不运行阶段六的正式成交回测。

**Goal：** 基于阶段四已批准的候选因子，建立无前视、可解释、可复现的复合评分与目标权重管道，并冻结进入验证期前的主方案。

**Architecture：** 阶段五直接消费阶段四的 `factor_panel`、分类表、相关矩阵和血缘，不读取原始CSV，也不修改阶段四因子实现。新增独立 `combination` 与 `portfolio` 包：前者只生产 `composite_scores`，后者只生产 `target_weights`；评价、血缘和报告继续由独立研究模块负责。

**Tech Stack：** Python 3.12+、Polars、NumPy、SciPy、PyYAML、Matplotlib、pytest、ruff。

---

## 1. 阶段四证据与阶段五边界

### 1.1 已确认输入

- 阶段四正式面板：2,015,188个唯一 `(date, symbol, factor_name)` 观测；
- 分类结果：6个 `candidate`、5个 `watch`、3个 `reject`；
- 正式候选：
  - 反转：`reversal_5`、`reversal_20`；
  - 流动性：`turnover_20`、`amihud_20`；
  - 低波动：`volatility_20`、`volatility_60`；
- 三个动量因子均为 `reject`，不得翻转方向后重新纳入；
- EP、BP、SP仍缺少历史point-in-time证据，只能为 `watch`；
- 历史行业、ST口径仍未核验，不实施行业中性化或行业约束；
- `volatility_20` 与 `volatility_60`存在冗余，高换手也是组合构建的主要风险。

### 1.2 不可违反的边界

- 只读取2005-01-01至2016-12-31研究期产物；
- 2017–2021验证期和2022–2025最终测试期继续封存；
- 只允许 `candidate` 进入正式合成，`watch/reject` 不进入主方案；
- `log_market_cap`只作控制变量和暴露诊断，不作收益因子；
- 权重日只能使用当日及以前信息；滚动IC权重必须至少滞后一期；
- 未来收益只用于评价，不得参与评分、选股和权重计算；
- 本阶段不模拟成交，不计算正式NAV，不实现税费、滑点、涨跌停、停牌、整手或未成交订单；
- 不以收益最高作为验收条件，负结果和简单基线优于复杂方法也是合法结论。

---

## 2. 冻结研究方案

### 2.1 因子合成方案

所有输入使用阶段四的 `score_size_neutral`，同日仅在有效候选因子的交集/最低覆盖门槛内合成。

| 方案 | 定义 | 定位 |
|---|---|---|
| `candidate_equal` | 6个候选因子各占1/6 | 最简单基线 |
| `family_equal` | 反转、流动性、低波动各占1/3；每族内部等权 | **预注册主方案** |
| `representative_equal` | `reversal_20`、`turnover_20`、`volatility_60`各占1/3 | 去冗余、低换手对照 |
| `rolling_ic_family` | 族权重固定1/3；族内用过去36个月IC加权 | 动态方法对照 |

`rolling_ic_family`固定规则：

1. 权重日 `t` 只能使用截至 `t-1` 已实现的月度20日Rank IC；
2. 回看36个月，至少24个有效月；不足时退回族内等权；
3. 负IC权重截断为0；全为0时退回族内等权；
4. 动态权重向族内等权收缩50%；
5. 每族权重始终为1/3，避免短期IC令单一因子族支配组合；
6. 任何参数调整必须发生在查看验证期之前，并同步修改配置、测试和本计划。

缺失处理：单只股票当日必须至少覆盖三个因子族中的两个；族内按可用因子重新归一；不满足门槛则不给出复合分数，并记录原因。复合分数完成后再做一次同日截面标准化。

### 2.2 复合分数评价

四种合成方案统一报告：

- 5、20、60日Rank IC，主期限为20日；
- IC均值、ICIR、Newey-West检验和有效月份；
- 五分组收益、Q5-Q1和单调性；
- 2005–2008、2009–2012、2013–2016子区间；
- 分数覆盖率、月度排名稳定性和Top 10%成员换手；
- 与6个单因子及其他复合方案的相关性；
- 相比 `candidate_equal` 的增量，不只报告绝对表现。

不根据上述结果临时增加新因子、新窗口或改变方向。阶段五完成后冻结一个主复合分数，默认仍为预注册的 `family_equal`；若改选，必须有预先定义的门禁和书面理由。

### 2.3 目标组合方案

只对主复合分数生成两套月末目标权重：

1. `top100_equal`：复合分数最高100只，等权，作为最简单股票等权基线；
2. `size_stratified_buffered`：**预注册主目标组合**。

主目标组合固定规则：

- 在当月动态股票池内，按 `log_market_cap` 分成5组；
- 每个规模组目标持有20只，共100只；
- 已持有股票只要仍位于本规模组前30名则优先保留；
- 空缺按复合分数降序、`symbol`升序补足；
- 组合等权，正常情况下单股目标权重1%；
- 目标权重非负、总和为1；不足100只时明确降级并记录原因，不静默改变规则；
- 缓冲规则只依赖上期目标持仓，不依赖阶段六的成交状态。

该设计用规模分层限制规模暴露，用排名缓冲降低不必要换手；由于行业口径未核验，本阶段只报告行业字段限制，不实施行业约束。

### 2.4 目标组合诊断

- 单边目标换手：`0.5 * sum(abs(w_t - w_{t-1}))`；
- 持仓数量、最大权重、HHI和有效持仓数；
- 规模组数量与组合加权 `log_market_cap` 暴露；
- 6个候选因子和3个因子族的组合暴露；
- 与 `top100_equal` 的持仓重合率和换手差异；
- 20日平均成交额、目标权重/成交额容量代理；
- 缺失分数、规模组不足、权重未归一等异常清单。

这些是目标组合的事前诊断，不代表真实可成交收益。成本后表现必须留给阶段六账本验证。

---

## 3. 文件与模块边界

### 3.1 修改现有文件

- `configs/research_protocol.yaml`：新增 `factor_combination` 与 `portfolio_construction` 配置；
- `src/ashare_multifactor/config.py`：新增对应不可变配置对象和日期/参数校验；
- `pyproject.toml`：新增独立CLI入口，例如 `ashare-combine`；
- `README.md`：阶段五完成后补运行方式与研究边界；
- `AGENTS.md`：阶段五完成后记录实际结果，不预写收益结论；
- 阶段四计划：仅修正与实际完成状态不一致的遗留复选框，不改变研究结果。

### 3.2 新建源码文件

建议保持以下职责边界，单文件不承载完整管道：

```text
src/ashare_multifactor/
  combination/
    definitions.py       # 合成方案定义与固定注册表
    eligibility.py       # candidate准入、覆盖和输入门禁
    equal_weight.py      # candidate_equal / family_equal / representative_equal
    rolling_ic.py        # 严格滞后的滚动IC权重
    panel.py             # 统一输出composite_scores
  portfolio/
    ranking.py           # 稳定排序与Top N选择
    size_stratified.py   # 规模分层选择
    buffer.py            # 上期目标持仓缓冲
    weights.py           # 等权、归一化与不变量检查
    diagnostics.py       # 换手、集中度、暴露和容量代理
  research/
    combination_paths.py
    combination_evaluation.py
    combination_lineage.py
    combination_outputs.py
    combination_pipeline.py
  cli/
    combinations.py
```

禁止把阶段五逻辑继续堆入现有 `factor_pipeline.py`、MVP回测脚本或Notebook。

### 3.3 机器可读产物

```text
processed/factor_combination/
  CURRENT.json
  releases/<run_id>/
    datasets/
      composite_scores.parquet
      composite_weights.parquet
      target_weights.parquet
      target_weight_diagnostics.parquet
    artifacts/
      composite_ic.parquet
      composite_quantiles.parquet
      composite_subperiods.parquet
      composite_correlations.parquet
      composite_correlation_monthly.parquet
      portfolio_summary.csv
      portfolio_turnover.csv
      portfolio_exposures.csv
      quality_issues.csv
      figures/
      report.md
    lineage.json
    manifest.json
```

核心主键：

- `composite_scores`：`(date, symbol, method)`；
- `composite_weights`：`(date, method, factor_name)`；
- `target_weights`：`(date, portfolio_name, symbol)`；
- 所有表必须拒绝重复主键、非有限值和日期越界。

---

## 4. 实施任务流程与验收清单

### Task 0：阶段门禁与文档一致性

- [x] 完整读取AGENTS、阶段四计划、阶段四报告和manifest；
- [x] 核对6个candidate及其因子族、score口径和相关矩阵；
- [x] 核对阶段四plan遗留复选框与真实产物，纠正文档状态；
- [x] 对阶段四输入文件、manifest和血缘重新验哈希；
- [x] 测试拒绝2017年及以后任何输入行；
- [x] 记录阶段五研究身份和配置哈希。

**验收：** 输入身份唯一；候选清单与阶段四分类完全一致；验证期和测试期零读取。

### Task 1：配置与架构骨架

- [x] 先写非法日期、非法权重、非法窗口和未知方法的失败测试；
- [x] 增加合成与组合配置对象；
- [x] 建立独立路径、临时目录和原子发布机制；
- [x] 新建只执行完整发布的单一CLI入口；
- [x] 保持阶段一至四配置与命令兼容。

**验收：** 配置错误可尽早失败；输出不能覆盖 `factor_research`、MVP或Data。

### Task 2：候选准入与合成基线

- [x] 测试 `watch/reject` 被拒绝进入正式合成；
- [x] 测试候选因子、因子族和主score列完整；
- [x] 实现 `candidate_equal`；
- [x] 实现 `family_equal`；
- [x] 实现 `representative_equal`；
- [x] 实现缺失覆盖、族内重归一和最终截面标准化；
- [x] 测试并列时 `symbol` 升序及输入行序不影响结果。

**验收：** 人工夹具能手算复核；每个日期/方法的因子权重和为1；复合分数无标签列依赖。

### Task 3：严格滞后的滚动IC合成

- [x] 先写“当月IC意外进入当月权重”的泄漏反例；
- [x] 实现36个月窗口、24个月门槛、负值截断和50%收缩；
- [x] 实现历史不足及全零权重退回等权；
- [x] 保存每月每因子的实际权重和可用历史区间；
- [x] 测试未来IC发生变化不会改变过去权重。

**验收：** 每个权重可追溯到严格早于权重日的IC；无可用历史时结果确定且可解释。

### Task 4：复合分数统一评价

- [x] 复用阶段四标签和统计口径，不复制另一套forward return；
- [x] 输出IC、分组、子区间、覆盖、稳定性、换手和相关性；
- [x] 与单因子和 `candidate_equal` 做同口径比较；
- [x] 生成机器可读汇总和图表；
- [x] 明确区分预注册主方案与对照方案。

**验收：** 同一输入重复运行结果一致；所有报告数字均来自机器可读产物。

### Task 5：股票等权基线

- [x] 实现 `top100_equal`；
- [x] 测试少于100只、并列分数、缺失分数和重复证券；
- [x] 检查非负、权重和为1、单股权重及持仓数量；
- [x] 保存未入选原因和异常原因。

**验收：** 人工小截面可精确复核；不存在隐含杠杆、空头或未来信息。

### Task 6：规模分层与排名缓冲主组合

- [x] 实现同日规模五分组；
- [x] 每组选择20只，并按前30名缓冲上期目标持仓；
- [x] 测试股票跨规模组、退池、新增、并列和组内不足；
- [x] 实现确定性补位与等权归一；
- [x] 证明缓冲只读取上期目标权重，不读取真实成交或未来持仓。

**验收：** 正常月份100只、每规模组20只、权重和为1；同样输入不受行顺序影响。

### Task 7：目标组合诊断与质量门禁

- [x] 实现单边目标换手、HHI、有效持仓数和最大权重；
- [x] 实现规模、候选因子和因子族暴露；
- [x] 实现成交额容量代理与基线重合率；
- [x] 对重复主键、日期越界、非有限权重、权重不守恒生成error；
- [x] 对规模组不足、覆盖下降和异常换手生成warning；
- [x] 不把warning静默删除或改成有利样本。

**验收：** 所有月份通过权重账面不变量；异常均有日期、证券、规则和严重度。

### Task 8：血缘、原子发布与CLI闭环

- [x] 绑定阶段四manifest、factor panel、分类表、相关矩阵和配置哈希；
- [x] 每个阶段验证上游文件未被替换；
- [x] 写入不可变release，全部成功后单次切换`CURRENT.json`；
- [x] 失败时保留上一版有效结果；
- [x] 单一CLI命令从阶段四产物一键生成阶段五全部产物。

**验收：** 篡改任一上游文件会失败；中断运行不会留下“半新半旧”的正式结果。

### Task 9：真实研究期运行与报告

- [x] 先在人工夹具和少量月份运行；
- [x] 再运行2005–2016完整研究期；
- [x] 核对日期边界、主键、权重守恒和产物哈希；
- [x] 报告简单基线、主方案、动态方案及失败结果；
- [x] 说明价值、行业、ST及真实成交约束仍未解决；
- [x] 冻结阶段六的主复合分数和主目标组合配置。

**验收：** 报告可回答“为什么这样合成、如何防泄漏、如何控制冗余与换手、为什么尚不能称为真实回测”。

### Task 10：全量验证与文档更新

- [x] 运行阶段五相关测试；
- [x] 运行完整 `pytest -q`；
- [x] 运行 `ruff check src tests`；
- [x] 检查Git中未出现Data、processed或artifacts；
- [x] 更新README、AGENTS和本计划复选框；
- [x] 不提交、不推送、不创建PR，除非用户另行要求。

**验收：** 全量测试和ruff通过；文档、配置、代码与真实产物一致。

---

## 5. 预计目标

阶段五完成后，应得到：

1. 一套从阶段四候选因子到复合分数的统一、无泄漏接口；
2. 三种静态合成和一种严格滞后的滚动IC对照；
3. 一个透明的Top100等权基线；
4. 一个规模分层、带排名缓冲的100只股票主目标组合；
5. 可审计的每月因子权重、目标权重、换手、集中度、暴露和容量代理；
6. 冻结的阶段六输入：`composite_scores` 与 `target_weights`；
7. 一份诚实说明简单方法、动态方法、失败结果和数据限制的研究期报告。

### 阶段成功不等于

- 不要求复合因子一定优于最佳单因子；
- 不要求滚动IC一定优于等权；
- 不要求目标组合产生正收益；
- 不代表组合已经可真实成交；
- 不代表验证期或最终测试期表现已经得到证明。

### 进入阶段六前的硬门禁

- 主复合方案、组合规则和全部参数已冻结；
- 研究期结果可从阶段四产物一键复现；
- 滚动权重无当期或未来IC泄漏；
- 目标权重逐月守恒且排序确定；
- 阶段五产物血缘和哈希完整；
- 2017年及以后数据仍未读取；
- 用户明确批准进入正式A股回测阶段。
