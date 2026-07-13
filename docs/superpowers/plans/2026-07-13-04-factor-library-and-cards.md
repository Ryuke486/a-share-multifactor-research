# A股完整单因子库与因子卡片 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 在不读取验证期和最终测试期的前提下，建立覆盖2005–2016完整研究期的、预注册且可审计的A股单因子实验室，为阶段五因子合成提供候选清单、淘汰理由和稳定的数据接口。

**Architecture:** 阶段四不继续扩展MVP大脚本，也不为每个因子复制一套评价代码。工程上新增独立的 factors 包、研究期股票池、标签、预处理、统计评价、冗余分析和因子卡片模块；各因子族分文件实现，通过注册表输出统一的月度长表 factor_panel。研究数据写入独立的 processed/factor_research，不能覆盖阶段三的 processed/daily_panel 和 processed/mvp。

**Tech Stack:** Python 3.12+、Polars、NumPy、SciPy、PyYAML、Matplotlib、pytest、ruff。

## Global Constraints

- 阶段三分支 phase3-mvp-closed-loop 必须先通过用户批准的方式合并到执行分支；本计划不擅自合并分支。
- 只读取2003-01-01至2016-12-31：2003–2004仅作为最长252观测窗口的预热，正式统计仅使用2005-01-01至2016-12-31。
- 不读取、统计或绘制2017–2021验证期和2022–2025最终测试期。
- 2014–2015的MVP结果只用于发现管道问题，不得用于翻转因子方向或挑选最优参数。
- 信号日为每个自然月最后一个真实交易日；标签为信号日之后第5、20、60个证券有效交易观测的后复权收益。
- 因子、股票池和中性化只允许使用信号日及以前信息；forward return只能进入评价模块。
- Data/只读；processed/和artifacts/继续被Git忽略。
- 价值因子在估值字段point-in-time口径未核验前最多标记为 watch，不得进入阶段五正式候选。
- 行业字段未通过历史口径核验前，不运行行业中性化；市值中性化仍需完成。
- 因子没有正收益不代表阶段失败；隐瞒负结果、修改方向追逐结果或打开验证/测试期才是失败。
- 新增文件保持单一职责；因子族、预处理、统计、报告和管道编排不得合并成超大文件。

---

## 1. 阶段三证据与方案选择

阶段三已在 phase3-mvp-closed-loop 分支完成，117项测试和ruff检查均通过。真实MVP结果为：

- 2014–2015月度60日动量平均Rank IC：-0.147142；
- 有效月份：23/24；
- 毛收益：-7.708140%；
- 扣除统一10bp后的净收益：-10.169736%；
- 平均月度调仓换手：120.197589%；
- 净值最大回撤：61.453079%；
- 38行拒单、711个陈旧估值证券日；
- 2012–2015共有4,905个估值字段缺失证券日，占2,272,248行面板约0.216%；
- 数据质量没有error，但估值来源、历史行业和point-in-time口径仍未证明。

比较过的方案：

1. 直接把60日动量翻转为反转并继续回测：最快，但明显利用两年小样本调参，拒绝。
2. 一次实现全部因子、组合和正式交易规则：范围过大，会混淆因子证据与组合/执行问题，拒绝。
3. 先冻结因子定义和方向，再用完整研究期生成统一因子卡片：研究边界清晰、能诚实解释负结果，并为阶段五提供可审计输入，采用。

因此，阶段四是“单因子研究与筛选阶段”，不是“继续优化净值阶段”。

## 2. 预注册研究设计

### 2.1 研究股票池

- 至少252个历史有效观测；
- 当日非ST；
- 当日原始OHLC均为有限正数；
- 过去20个观测平均成交额为有限正数；
- 每日按过去20个观测平均成交额降序，保留前1,000只；不足1,000只时保留全部合格股票；
- 不使用当前股票列表、未来退市时间和未来停牌状态；
- symbol并列排序时按六位代码升序。

阶段三的前200只仅是MVP计算上限。阶段四扩大到前1,000只，以增加截面样本、保留中小市值差异，并仍排除极端不流动尾部。

### 2.2 预注册因子

所有方向在读取完整研究期评价结果前冻结。direction=+1表示原值越大，理论预期收益越高；direction=-1表示原值越小，理论预期收益越高。

| 因子 | 原始定义 | direction | 数据状态 |
|---|---|---:|---|
| ep_ttm | 1 / pe_ttm，仅pe_ttm>0 | +1 | 估值口径待核验 |
| bp | 1 / pb，仅pb>0 | +1 | 估值口径待核验 |
| sp_ttm | 1 / ps_ttm，仅ps_ttm>0 | +1 | 估值口径待核验 |
| momentum_60 | close_adj[t] / close_adj[t-60] - 1 | +1 | 可用 |
| momentum_120 | close_adj[t] / close_adj[t-120] - 1 | +1 | 可用 |
| momentum_12_1 | close_adj[t-21] / close_adj[t-252] - 1 | +1 | 可用 |
| reversal_5 | close_adj[t] / close_adj[t-5] - 1 | -1 | 可用 |
| reversal_20 | close_adj[t] / close_adj[t-20] - 1 | -1 | 可用 |
| turnover_20 | 过去20个观测换手率均值 | -1 | 可用 |
| amihud_20 | 过去20个观测均值：abs(adjusted_return) / amount | +1 | 可用 |
| volatility_20 | 过去20个后复权日收益标准差 × sqrt(252) | -1 | 可用 |
| volatility_60 | 过去60个后复权日收益标准差 × sqrt(252) | -1 | 可用 |
| downside_volatility_60 | sqrt(mean(min(adjusted_return, 0)^2)) × sqrt(252)，窗口60 | -1 | 可用 |
| log_market_cap | ln(total_market_cap)，仅市值>0 | -1 | 可用并作为控制变量 |

不加入质量、盈利、成长、情绪、机器学习衍生因子。阶段四结束前不得看到结果后临时增加窗口。

### 2.3 截面处理

对每个信号日、每个因子独立执行：

1. 非有限值转为空；
2. 在合格股票池内按1%和99%分位数去极值；
3. 截面z标准化；
4. 乘以预注册direction，使score越大统一表示理论预期越高；
5. 除log_market_cap外，对score回归同日标准化log_market_cap，残差再次标准化，得到score_size_neutral；
6. 只有历史行业口径通过数据门禁后，才额外生成score_industry_size_neutral。

不能使用全样本均值、全样本分位数或未来日期的数据做截面处理。

### 2.4 评价口径

- 主评价期限：20个交易观测；
- 衰减期限：5、20、60个交易观测；
- 月度Rank IC、IC均值、IC标准差、年化ICIR；
- 对IC均值计算Newey-West t值；滞后阶数固定为 max(0, ceil(horizon / 21) - 1)；
- 对14个主评价检验使用Benjamini-Hochberg校正，输出q值；
- 五分组等权未来收益、Q5-Q1、分组单调性；
- 因子覆盖率、有效月份、正IC月份占比；
- Top 20%成员月度换手；
- 2005–2008、2009–2012、2013–2016三个预注册子区间；
- 同时报告原始定向score和市值中性score；
- 月度截面因子Spearman相关矩阵及abs(correlation)>=0.70冗余标记。

### 2.5 研究期分类规则

以20日score_size_neutral为主；log_market_cap使用定向score。

- candidate：覆盖率>=80%、有效月份>=120、平均IC>0、BH q<=0.10、Q5-Q1>0，且三个子区间至少两个平均IC>0；
- watch：覆盖率和有效月份达标、平均IC>0，但统计显著性、分组收益或稳定性至少一项未达candidate；
- reject：平均IC<=0，或覆盖率<80%，或有效月份<120；
- unverified数据字段对应的因子最多为watch，即使统计结果达到candidate；
- 分类是阶段五的输入建议，不是自动下单或最终投资结论。

不设置“至少保留几个因子”的业绩配额。零个candidate也是合法、可答辩的研究结果。

## 3. 文件与模块边界

### 修改现有文件

- configs/research_protocol.yaml：增加factor_research配置，不改MVP参数。
- configs/data_field_evidence.yaml：记录估值、行业和ST字段的证据状态、来源说明和核验日期。
- src/ashare_multifactor/config.py：增加FactorResearchSettings和日期隔离校验。
- src/ashare_multifactor/data/build.py：允许显式output_root，默认行为保持兼容。
- src/ashare_multifactor/research/lineage.py：只保留MVP兼容包装，不继续堆叠通用数据验证。
- pyproject.toml：新增ashare-factors命令，不新增统计依赖。
- README.md：阶段四完成后补运行命令和结果边界。
- docs/data/README.md：记录阶段四数据门禁结论。
- AGENTS.md：阶段四完成后只更新实际状态和已批准变更。

### 新建源码文件

- src/ashare_multifactor/data/manifest.py：通用Parquet清单、哈希和日期边界验证。
- src/ashare_multifactor/data/field_audit.py：历史字段覆盖与可得性门禁。
- src/ashare_multifactor/factors/__init__.py：因子包公共出口。
- src/ashare_multifactor/factors/definitions.py：FactorDefinition与固定注册表。
- src/ashare_multifactor/factors/transforms.py：安全收益、滚动统计和安全倒数。
- src/ashare_multifactor/factors/value.py：EP、BP、SP。
- src/ashare_multifactor/factors/momentum.py：60、120、12-1动量。
- src/ashare_multifactor/factors/reversal.py：5、20日反转。
- src/ashare_multifactor/factors/liquidity.py：换手和Amihud。
- src/ashare_multifactor/factors/low_volatility.py：20/60日波动和下行波动。
- src/ashare_multifactor/factors/size.py：对数总市值。
- src/ashare_multifactor/factors/panel.py：按因子族生成月度长表，不承载具体公式。
- src/ashare_multifactor/research/research_universe.py：阶段四股票池。
- src/ashare_multifactor/research/labels.py：5/20/60日未来收益标签。
- src/ashare_multifactor/research/factor_preprocessing.py：去极值、标准化和中性化。
- src/ashare_multifactor/research/factor_statistics.py：Newey-West和BH校正。
- src/ashare_multifactor/research/factor_evaluation.py：IC、分组、衰减、换手和子区间。
- src/ashare_multifactor/research/factor_redundancy.py：相关矩阵和冗余标记。
- src/ashare_multifactor/research/factor_selection.py：candidate/watch/reject规则。
- src/ashare_multifactor/research/factor_cards.py：卡片和图表。
- src/ashare_multifactor/research/factor_pipeline.py：阶段编排，不实现因子公式。
- src/ashare_multifactor/cli/factors.py：命令行参数解析。
- docs/factor_research_protocol.md：固定公式、方向、数据门禁和分类规则。

### 新建测试文件

- tests/test_factor_config.py
- tests/test_data_manifest.py
- tests/test_field_audit.py
- tests/test_factor_definitions.py
- tests/test_factor_transforms.py
- tests/test_price_factors.py
- tests/test_cross_sectional_factors.py
- tests/test_research_universe_labels.py
- tests/test_factor_preprocessing.py
- tests/test_factor_statistics.py
- tests/test_factor_evaluation.py
- tests/test_factor_redundancy_selection.py
- tests/test_factor_pipeline_integration.py

### 可再生产物

~~~text
processed/factor_research/
  daily_panel/year=YYYY/part-000.parquet
  daily_panel/manifest.json
  data_readiness.json
  monthly_raw/family=FAMILY/part-000.parquet
  factor_panel.parquet
  forward_returns.parquet
  rank_ic.parquet
  quantile_returns.parquet
  subperiod_metrics.parquet
  factor_turnover.parquet
  factor_correlations.parquet
  lineage.json

artifacts/factor_research/
  manifest.json
  data_readiness.json
  factor_summary.csv
  rank_ic.csv
  quantile_returns.csv
  subperiod_metrics.csv
  factor_turnover.csv
  factor_correlations.csv
  redundancy_flags.csv
  cards/FACTOR.md
  figures/FACTOR/ic.png
  figures/FACTOR/quantiles.png
  figures/FACTOR/decay.png
  figures/FACTOR/subperiods.png
  report.md
~~~

## 4. 稳定接口

~~~text
@dataclass(frozen=True)
class FactorResearchSettings:
    data_start: date
    analysis_start: date
    analysis_end: date
    universe_size: int
    minimum_history: int
    liquidity_lookback: int
    signal_frequency: str
    forward_horizons: Sequence[int]
    primary_horizon: int
    winsor_lower: float
    winsor_upper: float
    quantile_count: int
    minimum_coverage: float
    minimum_valid_months: int
    fdr_q_threshold: float
    redundancy_threshold: float

@dataclass(frozen=True)
class FactorDefinition:
    name: str
    family: str
    source_columns: Sequence[str]
    lookback: int
    direction: int
    requires_verified_pit: bool
    size_neutralize: bool

@dataclass(frozen=True)
class FieldReadiness:
    factor_status: Mapping[str, str]
    industry_neutralization_enabled: bool
    field_metrics: pl.DataFrame

@dataclass(frozen=True)
class FactorEvaluationBundle:
    rank_ic: pl.DataFrame
    quantile_returns: pl.DataFrame
    subperiod_metrics: pl.DataFrame
    factor_turnover: pl.DataFrame
    factor_summary: pl.DataFrame

build_research_universe(frame: pl.DataFrame, settings: FactorResearchSettings) -> pl.DataFrame
add_forward_returns(frame: pl.DataFrame, horizons: Sequence[int]) -> pl.DataFrame
build_monthly_factor_panel(frame: pl.DataFrame, definitions: Sequence[FactorDefinition], settings: FactorResearchSettings) -> pl.DataFrame
preprocess_factor_panel(panel: pl.DataFrame, readiness: FieldReadiness, settings: FactorResearchSettings) -> pl.DataFrame
evaluate_factors(panel: pl.DataFrame, definitions: Sequence[FactorDefinition], settings: FactorResearchSettings) -> FactorEvaluationBundle
~~~

factor_panel固定长表字段：

~~~text
date: Date
symbol: String
factor_name: String
family: String
raw_value: Float64
winsorized_value: Float64
score: Float64
score_size_neutral: Float64
score_industry_size_neutral: Float64 nullable
forward_return_5: Float64 nullable
forward_return_20: Float64 nullable
forward_return_60: Float64 nullable
point_in_time_status: String
~~~

---

### Task 1: 冻结阶段四配置和研究协议

**Files:**
- Modify: configs/research_protocol.yaml
- Modify: src/ashare_multifactor/config.py
- Create: docs/factor_research_protocol.md
- Test: tests/test_factor_config.py

**Interfaces:**
- Consumes: 现有ResearchConfig、Period和MvpSettings。
- Produces: ResearchConfig.factor_research: FactorResearchSettings。

- [x] **Step 1: 写失败测试，拒绝验证期和测试期**

测试必须使用临时YAML覆盖非法值，避免修改正式配置：

~~~python
def _write_factor_config(tmp_path: Path, **overrides: object) -> Path:
    raw = yaml.safe_load(Path("configs/research_protocol.yaml").read_text(encoding="utf-8"))
    raw["paths"] = {
        "raw_unadjusted": str(tmp_path / "raw"),
        "raw_backward_adjusted": str(tmp_path / "adj"),
        "processed": str(tmp_path / "processed"),
        "artifacts": str(tmp_path / "artifacts"),
    }
    raw["factor_research"].update(overrides)
    path = tmp_path / "research_protocol.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return path

def test_factor_research_is_confined_to_research_period(tmp_path: Path) -> None:
    config = _write_factor_config(tmp_path, analysis_end="2017-01-03")
    with pytest.raises(ValueError, match="factor research analysis must stay inside research"):
        load_config(config)

def test_factor_research_warmup_cannot_reach_validation_or_test(tmp_path: Path) -> None:
    config = _write_factor_config(tmp_path, data_start="2017-01-01")
    with pytest.raises(ValueError, match="factor research data"):
        load_config(config)
~~~

- [x] **Step 2: 运行配置测试并确认失败**

Run: .venv/bin/pytest tests/test_factor_config.py -v

Expected: FAIL，因为FactorResearchSettings尚不存在。

- [x] **Step 3: 实现固定配置**

YAML固定值：

~~~yaml
factor_research:
  data_start: 2003-01-01
  analysis_start: 2005-01-01
  analysis_end: 2016-12-31
  universe_size: 1000
  minimum_history: 252
  liquidity_lookback: 20
  signal_frequency: month_end
  forward_horizons: [5, 20, 60]
  primary_horizon: 20
  winsor_lower: 0.01
  winsor_upper: 0.99
  quantile_count: 5
  minimum_coverage: 0.80
  minimum_valid_months: 120
  fdr_q_threshold: 0.10
  redundancy_threshold: 0.70
~~~

校验analysis区间完全位于research；data_start早于analysis_start；所有日期早于validation.start；分位数、覆盖率和阈值在合法区间；primary_horizon属于forward_horizons。

- [x] **Step 4: 写研究协议文档**

文档必须逐项写明本计划第2节的股票池、14个因子、方向、标签、预处理、统计校正、分类规则和禁止事项。

- [x] **Step 5: 运行配置与现有回归测试**

Run: .venv/bin/pytest tests/test_config.py tests/test_factor_config.py -v

Expected: PASS，且现有MVP配置仍可加载。

- [x] **Step 6: 提交本任务**

~~~bash
git add configs/research_protocol.yaml src/ashare_multifactor/config.py docs/factor_research_protocol.md tests/test_factor_config.py
git commit -m "feat: freeze factor research protocol"
~~~

**验收：**

- [x] 所有阶段四参数只在YAML定义一次。
- [x] 代码拒绝任何2017年及以后日期。
- [x] 因子方向和分类阈值在结果生成前冻结。

---

### Task 2: 建立因子注册表和数据可用性门禁

**Files:**
- Create: configs/data_field_evidence.yaml
- Create: src/ashare_multifactor/factors/__init__.py
- Create: src/ashare_multifactor/factors/definitions.py
- Create: src/ashare_multifactor/data/field_audit.py
- Test: tests/test_factor_definitions.py
- Test: tests/test_field_audit.py

**Interfaces:**
- Produces: FACTOR_DEFINITIONS: Sequence[FactorDefinition]。
- Produces: FieldReadiness、audit_factor_fields(frame, definitions)。

- [ ] **Step 1: 写注册表失败测试**

断言正好包含第2.2节14个唯一名称，direction只能为-1或+1，价值因子requires_verified_pit=True，log_market_cap.size_neutralize=False。

- [ ] **Step 2: 实现FactorDefinition和固定注册表**

注册表是因子元数据唯一来源。评价、报告和因子卡片不得各自维护名称或方向。

- [ ] **Step 3: 写数据门禁失败测试**

至少覆盖：

~~~python
def test_unverified_valuation_caps_factor_at_watch() -> None:
    readiness = audit_factor_fields(frame_with_valuation(), valuation_verified=False)
    assert readiness.factor_status["ep_ttm"] == "unverified"

def test_industry_neutralization_is_disabled_without_historical_evidence() -> None:
    readiness = audit_factor_fields(frame_with_current_industry(), industry_verified=False)
    assert readiness.industry_neutralization_enabled is False
~~~

- [ ] **Step 4: 实现门禁报告**

data_readiness.json至少包含每个字段的非空率、有限值率、正值率、最早/最晚日期、point-in-time状态、证据说明和受影响因子。外部核验材料不存在时必须写unverified，不能默认通过。

configs/data_field_evidence.yaml初始明确写valuation、industry和historical_st的状态。交叉核验只记录来源、抓取日期、样本规则和结果摘要；不能把AKShare或BaoStock网络请求变成核心研究管道的隐式步骤。

- [ ] **Step 5: 运行测试**

Run: .venv/bin/pytest tests/test_factor_definitions.py tests/test_field_audit.py -v

Expected: PASS。

- [ ] **Step 6: 提交本任务**

~~~bash
git add configs/data_field_evidence.yaml src/ashare_multifactor/factors src/ashare_multifactor/data/field_audit.py tests/test_factor_definitions.py tests/test_field_audit.py
git commit -m "feat: register factors and data readiness gates"
~~~

**验收：**

- [ ] 因子列表、方向和数据要求只有一个权威来源。
- [ ] 未核验价值字段不能产出candidate。
- [ ] 未核验行业字段不会静默进入中性化。

---

### Task 3: 隔离构建2003–2016研究数据集

**Files:**
- Create: src/ashare_multifactor/data/manifest.py
- Modify: src/ashare_multifactor/data/build.py
- Modify: src/ashare_multifactor/research/lineage.py
- Test: tests/test_data_manifest.py
- Modify: tests/test_build.py

**Interfaces:**
- Produces: validate_panel_source(root: Path, allowed: Period) -> DailyPanelSource。
- Extends: build_parquet_dataset(config: ResearchConfig, start: date, end: date, output_root: Path | None = None) -> BuildManifest。

- [ ] **Step 1: 写隔离输出测试**

测试显式output_root时写入临时research目录，并断言原MVP daily_panel目录内容和哈希不变。

- [ ] **Step 2: 写通用manifest验证反例**

覆盖缺分区、多分区、路径穿越、年份错配、行数不符、哈希不符、日期越界和包含2017年日期。

- [ ] **Step 3: 提取通用清单验证**

将research/lineage.py中的通用DailyPanelSource和分区验证移到data/manifest.py；MVP原函数保留兼容包装，现有调用签名不变。

- [ ] **Step 4: 增加显式output_root**

默认None仍写processed/daily_panel；阶段四明确写processed/factor_research/daily_panel。失败时原已发布数据集不被破坏。

- [ ] **Step 5: 在人工数据和小年份范围验证**

Run: .venv/bin/pytest tests/test_build.py tests/test_data_manifest.py tests/test_mvp_integration.py -v

Expected: PASS。

- [ ] **Step 6: 只读构建2003–2005试运行**

先计时、记录峰值内存和产物大小；只有试运行通过后，才允许Task 10构建完整2003–2016。

- [ ] **Step 7: 提交本任务**

~~~bash
git add src/ashare_multifactor/data/manifest.py src/ashare_multifactor/data/build.py src/ashare_multifactor/research/lineage.py tests/test_data_manifest.py tests/test_build.py
git commit -m "refactor: isolate research panel manifests"
~~~

**验收：**

- [ ] 阶段三数据和血缘不被覆盖。
- [ ] 研究数据最多到2016-12-31。
- [ ] 通用清单逻辑不继续扩大现有lineage.py。

---

### Task 4: 构建研究股票池和多期限标签

**Files:**
- Create: src/ashare_multifactor/research/research_universe.py
- Create: src/ashare_multifactor/research/labels.py
- Test: tests/test_research_universe_labels.py

**Interfaces:**
- Consumes: 规范daily panel和FactorResearchSettings。
- Produces: is_research_eligible、liquidity_rank、forward_return_5/20/60。

- [ ] **Step 1: 写无未来信息股票池测试**

改变目标日之后的成交额、ST或价格，不得改变目标日股票池；当日并列成交额按symbol升序稳定选择。

- [ ] **Step 2: 实现前1,000只研究股票池**

不能修改阶段三build_universe的MVP语义；新逻辑放在research_universe.py。

- [ ] **Step 3: 写标签偏移测试**

对缺交易日和个股停牌人工序列，断言标签使用每只证券之后第5、20、60个有效观测，而不是自然日。

- [ ] **Step 4: 实现标签**

标签列只能由labels.py生成；factors包不得导入或访问forward_return列。

- [ ] **Step 5: 运行测试**

Run: .venv/bin/pytest tests/test_research_universe_labels.py tests/test_universe_momentum.py -v

Expected: PASS，MVP逻辑无回归。

- [ ] **Step 6: 提交本任务**

~~~bash
git add src/ashare_multifactor/research/research_universe.py src/ashare_multifactor/research/labels.py tests/test_research_universe_labels.py
git commit -m "feat: add factor research universe and labels"
~~~

**验收：**

- [ ] 研究股票池不再受MVP前200限制。
- [ ] 未来收益与因子模块物理隔离。
- [ ] 2003–2004不进入评价输出。

---

### Task 5: 实现价格型因子族

**Files:**
- Create: src/ashare_multifactor/factors/transforms.py
- Create: src/ashare_multifactor/factors/momentum.py
- Create: src/ashare_multifactor/factors/reversal.py
- Create: src/ashare_multifactor/factors/low_volatility.py
- Test: tests/test_factor_transforms.py
- Test: tests/test_price_factors.py

**Interfaces:**
- Produces: compute_momentum_factors、compute_reversal_factors、compute_low_volatility_factors。

- [ ] **Step 1: 写安全收益和滚动窗口测试**

覆盖零价格、负价格、NaN、Inf、跨年窗口、缺观测、精确t-21/t-252和不足样本时返回空值。

- [ ] **Step 2: 实现transforms.py**

只提供可复用表达式和小函数，不出现具体因子名称、配置路径或评价逻辑。

- [ ] **Step 3: 写手算因子测试**

使用单调增长、单调下降、混合正负收益三类人工序列，逐项验证5个收益路径因子与3个波动因子。

- [ ] **Step 4: 实现三个因子族文件**

每个函数只读取注册表声明的列，返回date、symbol和本因子族原始值。

- [ ] **Step 5: 运行测试**

Run: .venv/bin/pytest tests/test_factor_transforms.py tests/test_price_factors.py -v

Expected: PASS。

- [ ] **Step 6: 提交本任务**

~~~bash
git add src/ashare_multifactor/factors/transforms.py src/ashare_multifactor/factors/momentum.py src/ashare_multifactor/factors/reversal.py src/ashare_multifactor/factors/low_volatility.py tests/test_factor_transforms.py tests/test_price_factors.py
git commit -m "feat: add price based factor families"
~~~

**验收：**

- [ ] 60日动量direction保持+1，没有因MVP负IC翻转。
- [ ] 12-1动量明确跳过最近21个观测。
- [ ] 波动率只基于历史后复权收益。

---

### Task 6: 实现价值、流动性和规模因子

**Files:**
- Create: src/ashare_multifactor/factors/value.py
- Create: src/ashare_multifactor/factors/liquidity.py
- Create: src/ashare_multifactor/factors/size.py
- Test: tests/test_cross_sectional_factors.py

**Interfaces:**
- Produces: compute_value_factors、compute_liquidity_factors、compute_size_factor。

- [ ] **Step 1: 写估值有效域测试**

PE、PB、PS为零、负值、空值或非有限值时对应倒数必须为空；不能截断成有利数值。

- [ ] **Step 2: 实现价值因子**

输出原始EP、BP、SP，但携带unverified point-in-time状态；代码不得因为状态未核验而伪造数值。

- [ ] **Step 3: 写Amihud和换手测试**

成交额为零或非有限值时单日Amihud为空；20观测不足时为空；收益使用close_adj和prev_close_adj，成交额仍使用原始amount。

- [ ] **Step 4: 实现流动性与规模因子**

log_market_cap只接受有限正市值。禁止使用close_adj乘股本重算市值。

- [ ] **Step 5: 运行测试**

Run: .venv/bin/pytest tests/test_cross_sectional_factors.py -v

Expected: PASS。

- [ ] **Step 6: 提交本任务**

~~~bash
git add src/ashare_multifactor/factors/value.py src/ashare_multifactor/factors/liquidity.py src/ashare_multifactor/factors/size.py tests/test_cross_sectional_factors.py
git commit -m "feat: add value liquidity and size factors"
~~~

**验收：**

- [ ] 价值字段缺失只降低覆盖率，不被填充为0。
- [ ] Amihud不发生除零或Inf污染。
- [ ] 市值口径与后复权价格完全解耦。

---

### Task 7: 生成统一月度因子面板并做截面处理

**Files:**
- Create: src/ashare_multifactor/factors/panel.py
- Create: src/ashare_multifactor/research/factor_preprocessing.py
- Test: tests/test_factor_preprocessing.py

**Interfaces:**
- Produces: build_monthly_factor_panel、preprocess_factor_panel。
- Output contract: 本计划第4节factor_panel长表。

- [ ] **Step 1: 写月末和长表schema测试**

每月只保留最后真实交易日；每个date-symbol-factor_name最多一行；字段和类型固定；2003–2004不输出。

- [ ] **Step 2: 实现按因子族分批计算**

每个因子族只扫描需要列并写monthly_raw/family=FAMILY；禁止一次保留全部日频因子宽表。

- [ ] **Step 3: 写去极值与定向测试**

人工截面验证1%/99%分位数只使用同日合格股票；direction=-1后排序相反；未来日期极值不影响当前日期。

- [ ] **Step 4: 写市值中性化测试**

构造与log_market_cap线性相关的因子，残差与市值截面相关接近0；奇异截面、样本不足和常数因子必须记录reason而不是崩溃。

- [ ] **Step 5: 实现预处理**

行业门禁未通过时score_industry_size_neutral整列为空，并在data_readiness和报告中解释。

- [ ] **Step 6: 运行测试**

Run: .venv/bin/pytest tests/test_factor_preprocessing.py tests/test_price_factors.py tests/test_cross_sectional_factors.py -v

Expected: PASS。

- [ ] **Step 7: 提交本任务**

~~~bash
git add src/ashare_multifactor/factors/panel.py src/ashare_multifactor/research/factor_preprocessing.py tests/test_factor_preprocessing.py
git commit -m "feat: build monthly factor panel"
~~~

**验收：**

- [ ] 因子面板结构统一且无重复主键。
- [ ] 预处理没有跨日期估计。
- [ ] 计算分批进行，不制造超大中间宽表。

---

### Task 8: 实现统计评价、FDR和因子分类

**Files:**
- Create: src/ashare_multifactor/research/factor_statistics.py
- Create: src/ashare_multifactor/research/factor_evaluation.py
- Create: src/ashare_multifactor/research/factor_selection.py
- Test: tests/test_factor_statistics.py
- Test: tests/test_factor_evaluation.py

**Interfaces:**
- Produces: newey_west_mean_test、benjamini_hochberg、evaluate_factors、classify_factors。

- [ ] **Step 1: 写Newey-West和BH手算测试**

使用可手算序列验证均值、标准误、t值和单调q值；空序列、单观测、零方差必须返回明确reason。

- [ ] **Step 2: 实现统计工具**

只依赖NumPy/SciPy，不新增statsmodels。BH校正只对同一预注册主评价族的有效p值执行。

- [ ] **Step 3: 写IC、五分组和换手测试**

覆盖完全同序IC=1、完全反序IC=-1、少于20只、并列分数、五组数量不整除、Top 20%成员变化和标签缺失。

- [ ] **Step 4: 实现通用评价**

评价函数只按factor_name和score_variant分组，不能包含ep_ttm、momentum_60等特例。

- [ ] **Step 5: 写分类规则测试**

逐项验证candidate、watch、reject和unverified上限；不允许根据因子名称给予豁免。

- [ ] **Step 6: 实现分类**

输出每项门槛的布尔值和最终reason，不能只输出一个不透明标签。

- [ ] **Step 7: 运行测试**

Run: .venv/bin/pytest tests/test_factor_statistics.py tests/test_factor_evaluation.py -v

Expected: PASS。

- [ ] **Step 8: 提交本任务**

~~~bash
git add src/ashare_multifactor/research/factor_statistics.py src/ashare_multifactor/research/factor_evaluation.py src/ashare_multifactor/research/factor_selection.py tests/test_factor_statistics.py tests/test_factor_evaluation.py
git commit -m "feat: evaluate and classify single factors"
~~~

**验收：**

- [ ] IC、ICIR、Newey-West、BH q值、分组、衰减、覆盖率和换手均可复算。
- [ ] 负结果保留并带原因。
- [ ] 阶段验收不以candidate数量为条件。

---

### Task 9: 分析因子冗余并生成因子卡片

**Files:**
- Create: src/ashare_multifactor/research/factor_redundancy.py
- Create: src/ashare_multifactor/research/factor_cards.py
- Test: tests/test_factor_redundancy_selection.py

**Interfaces:**
- Produces: factor_correlations、redundancy_flags、cards/FACTOR.md和标准图表。

- [ ] **Step 1: 写相关矩阵测试**

按月计算截面Spearman后再跨月平均；不能把所有date-symbol行直接堆叠求一次相关。缺共同样本时记录reason。

- [ ] **Step 2: 实现冗余标记**

abs(correlation)>=0.70时列出因子对、共同月份和相关方向，但不自动删除因子。

- [ ] **Step 3: 写因子卡片快照测试**

卡片必须含公式、方向、数据状态、经济解释、覆盖率、IC、q值、五分组、衰减、换手、子区间、冗余关系、分类和限制。

- [ ] **Step 4: 实现卡片和图表**

每个因子单独目录，报告模块只读机器可读评价产物，不重新计算指标。

- [ ] **Step 5: 运行测试**

Run: .venv/bin/pytest tests/test_factor_redundancy_selection.py -v

Expected: PASS。

- [ ] **Step 6: 提交本任务**

~~~bash
git add src/ashare_multifactor/research/factor_redundancy.py src/ashare_multifactor/research/factor_cards.py tests/test_factor_redundancy_selection.py
git commit -m "feat: generate factor cards and redundancy report"
~~~

**验收：**

- [ ] 14个因子各有独立卡片。
- [ ] 高相关因子有证据但不被静默删除。
- [ ] 图表数字与CSV/Parquet一致。

---

### Task 10: 一键管道、真实研究期运行与最终验收

**Files:**
- Create: src/ashare_multifactor/research/factor_pipeline.py
- Create: src/ashare_multifactor/cli/__init__.py
- Create: src/ashare_multifactor/cli/factors.py
- Modify: pyproject.toml
- Modify: README.md
- Modify: docs/data/README.md
- Modify: AGENTS.md
- Test: tests/test_factor_pipeline_integration.py

**Interfaces:**
- Produces: ashare-factors --config configs/research_protocol.yaml --stage {build,audit,factors,evaluate,report,all}。
- Produces: 本计划第3节全部机器可读产物和报告。

- [ ] **Step 1: 写端到端人工数据测试**

生成至少320个交易观测、25只股票和多个因子方向；运行all后检查清单、血缘、14个因子、三种标签、评价表、卡片和报告。

- [ ] **Step 2: 写泄漏和断点续跑测试**

人为把manifest最大日期改为2017或2022，必须在扫描数据前失败；替换上游文件后下游stage必须拒绝复用。

- [ ] **Step 3: 实现薄编排层和CLI**

factor_pipeline.py只调用各模块和记录输入输出统计；命令行解析放在cli/factors.py，避免形成第二个超大mvp.py。

- [ ] **Step 4: 运行集成测试和全部检查**

Run: .venv/bin/pytest -v

Expected: 全部PASS。

Run: .venv/bin/ruff check src tests

Expected: All checks passed。

- [ ] **Step 5: 构建完整2003–2016研究数据**

Run: .venv/bin/ashare-factors --config configs/research_protocol.yaml --stage build

检查manifest最小日期不早于2003-01-01、最大日期不晚于2016-12-31；研究期外预热不进入评价。

- [ ] **Step 6: 运行完整阶段四**

Run: .venv/bin/ashare-factors --config configs/research_protocol.yaml --stage all

检查14个因子全部出现在factor_summary.csv；每个因子有分类或明确失败原因；所有产物最大日期不超过2016-12-31。

- [ ] **Step 7: 重跑并核对可复现性**

再次运行all；比较机器可读产物SHA-256。除manifest中明确允许变化的运行时间外，研究结果哈希必须一致。

- [ ] **Step 8: 更新README、数据说明和AGENTS**

README写运行方法和阶段边界；docs/data记录完整研究期字段覆盖；AGENTS只写已经证实的阶段四结果，不预先宣称因子有效。

- [ ] **Step 9: Git安全检查**

Run: git status --short

Run: git ls-files Data processed artifacts

Expected: Data、processed和artifacts没有被追踪。

- [ ] **Step 10: 提交本任务**

~~~bash
git add pyproject.toml README.md AGENTS.md docs/data/README.md src/ashare_multifactor/research/factor_pipeline.py src/ashare_multifactor/cli tests/test_factor_pipeline_integration.py
git commit -m "feat: complete single factor research stage"
~~~

**验收：**

- [ ] 一条命令可从只读原始数据生成全部阶段四结果。
- [ ] 验证期和最终测试期未被读取。
- [ ] 结果可重复、可审计，并保留负结果。

---

## 5. 阶段四最终验收清单

### 研究边界

- [ ] phase3-mvp-closed-loop已先合并到执行分支。
- [ ] 2003–2004只作预热，2005–2016才进入统计。
- [ ] 2017–2021和2022–2025没有被读取、统计或绘图。
- [ ] 因子定义、方向、窗口和门槛在完整结果生成前冻结。
- [ ] 60日动量没有因MVP负IC被翻转。

### 数据与工程

- [ ] 阶段四研究面板不覆盖阶段三面板和MVP产物。
- [ ] 价值、行业和ST字段的数据状态有机器可读门禁。
- [ ] 未核验行业不参与中性化。
- [ ] 14个因子分族实现，没有超大因子文件。
- [ ] factor_panel主键(date, symbol, factor_name)唯一。
- [ ] forward return与因子计算模块隔离。
- [ ] 所有中间产物有清单、哈希和血缘。

### 单因子证据

- [ ] 每个因子都有原始值、定向score和适用时的市值中性score。
- [ ] 每个因子报告5/20/60日Rank IC。
- [ ] 主评价报告IC均值、年化ICIR、Newey-West t值、p值和BH q值。
- [ ] 报告五分组、Q5-Q1、单调性、覆盖率、正IC占比和Top 20%换手。
- [ ] 报告三个预注册子区间。
- [ ] 报告平均截面相关矩阵和冗余标记。
- [ ] 价值因子在数据口径未核验前不会成为candidate。
- [ ] 没有因子因表现差而从报告中消失。

### 交付

- [ ] factor_summary.csv包含全部14个因子。
- [ ] 每个因子有独立Markdown卡片和四张标准图。
- [ ] report.md总结candidate、watch、reject和数据限制。
- [ ] README提供阶段四复现命令。
- [ ] pytest和ruff全部通过。
- [ ] 重跑机器可读结果一致。
- [ ] Git不含Data、processed或artifacts。

## 6. 预计目标与工作量

### 可承诺目标

- 建立14个预注册单因子及统一评价框架；
- 完成2005–2016完整研究期、5/20/60期限评价；
- 输出14张因子卡片、汇总表、相关矩阵和阶段报告；
- 给阶段五提供有明确证据和淘汰理由的候选清单；
- 证明负结果也能被完整保留和解释。

### 不承诺目标

- 不承诺一定出现candidate；
- 不承诺收益为正或超过基准；
- 不用2014–2015结果调方向；
- 不在本阶段合成多因子、优化权重或升级正式交易撮合；
- 不打开验证期和最终测试期。

### 预计工作量

| 部分 | 预计专注开发时间 |
|---|---:|
| 配置、数据门禁与研究协议 | 0.5–1天 |
| 研究数据集隔离与扩展 | 1–1.5天 |
| 14个因子与单元测试 | 2–3天 |
| 预处理、统计评价与FDR | 2–2.5天 |
| 因子卡片、管道和集成测试 | 1.5–2天 |
| 真实数据运行、审计和文档 | 1天 |
| 合计 | 8–11个专注开发日 |

全量运行耗时和峰值内存不能预先保证；Task 3先用2003–2005试运行测量，再决定是否需要按因子族或年份进一步分批。

## 7. 阶段五入口条件

只有以下条件全部满足，才开始因子合成：

1. 阶段四验收清单全部完成；
2. 因子候选状态和冗余关系已冻结；
3. 价值因子若入选，point-in-time口径已有证据；
4. 组合方法只读取candidate和经用户批准的watch；
5. 阶段五仍只使用研究期，不提前打开验证或测试；
6. 阶段五另写独立计划，不在factor_pipeline.py内直接增加组合和回测逻辑。
