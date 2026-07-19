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

- [x] 验证代码、配置、上游release、成本、指标和模板哈希。
- [x] 测试无授权、哈希变化、日期越界和已有成功权威run时必须拒绝。
- [x] 在读取首个2022文件之前写入尝试ID、时间、Git提交和协议哈希。

## Task 2：构建2022–2025规范数据和质量报告

**Files:**
- Create: `src/ashare_multifactor/final_test/data_extension.py`
- Test: `tests/test_final_test_data.py`

**Interfaces:**
- Produces: final-test daily panel、manifest、quality issues。

- [x] 完全复用板块7数据延伸schema和错误等级，不因测试期异常改口径。
- [x] 明确2025-12-31后不读取，2026年数据不属于最终测试。
- [x] 记录数据文件清单、哈希、日期、行数和质量异常，不修改原始Data。

## Task 3：按封存协议外推信号和目标权重

**Files:**
- Create: `src/ashare_multifactor/final_test/signals.py`
- Test: `tests/test_final_test_signals.py`

**Interfaces:**
- Produces: test factor panel、composite scores、target weights。

- [x] 使用封存因子、方向、预处理、合成和组合规则。
- [x] 滚动统计只使用当时已实现标签，测试末端不为获得标签越过2025-12-31。
- [x] 将缺失和覆盖下降按冻结规则处理，不临时替换因子。

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

## 当前执行状态（2026-07-16）

- Task 1–3及Task 4–6的代码路径、合成反例和任务级审查已完成；最新实现验证为945项测试通过且ruff通过。这只证明实现就绪，不代表真实最终测试完成。
- 唯一生产CLI已拆分为显式`prepare`与`resume`两阶段：`prepare`只消费一次开启令牌并停在`awaiting_official_evidence`，`resume`不接受开启令牌，只能用同一attempt的精确官方coverage继续发布；无子命令或跨阶段参数会直接拒绝。
- 研究市场已冻结为沪深市场（`sh`、`sz`），明确排除北交所；该范围已绑定配置、阶段交接内容校验、lineage和最终发布门禁。
- 最终入口已移除可替换授权步骤的生产旁路；最终release只允许发布2022–2025切片，并在发布前扫描全部Parquet日期或终态关联。
- 公司行动必须提供覆盖全部最终执行证券的官方查询、缓存证据、候选差异解释和文件哈希；正式公司行动只从通过校验的官方标准化行生成。
- 研究期和验证期对照必须沿封印的板块8→板块7→板块5具体manifest文件记录解析并复核哈希，不读取任何可漂移的`CURRENT.json`。
- 因市场范围、代码身份及两阶段CLI均已变化，旧板块7/8封印及旧开启确认已失效且不得复用。下一步先双run重建板块7/8不可变successor和新精确封印，再取得用户对新封印的单独开启批准。
- 尚未消费新的开启令牌，未读取真实2022–2025数据，未生成final-test权威release或切换`processed/final_test/CURRENT.json`，也未进入检查点C；因此Task 4–6和验收清单继续保持未勾选。

## 首次开启事故与修复记录（2026-07-18）

- 用户批准后，尝试`stage9-final-20260718-f3bfe4c`消费了绑定旧封印`cff84a47cdd2594bf7047d0ea6f13b7fbb5033be372a7685a940120ea79d8fad`的一次性令牌，并读取2022–2025原始日文件构建规范面板。
- 准备阶段在发布任何symbol scope、信号、目标权重、回测、指标、报告或权威release之前，因面板含`920xxx`北交所证券而失败。该尝试没有产生策略表现结果，也没有切换`processed/final_test/CURRENT.json`；观察到的信息仅限于原始数据包含已冻结范围之外的北交所记录。
- 根因是最终测试数据构建器复用全市场Stage-2管道时未传入已冻结的`supported_markets: [sh, sz]`，后续准备门禁正确拒绝了北交所证券。这是市场范围执行缺陷，不是基于测试收益进行的模型、因子、参数或口径调整。
- 经用户批准，提交`2607513a6eb4f04723fab893de54d3f64250fc96`在Parquet与manifest发布前执行沪深过滤，并逐日记录范围外行数；共享Stage-2默认行为保持不变。修复及事故归档通过994项全量测试、ruff和独立复核。
- 失败现场已整体、无覆盖地归档到`processed/final_test_incidents/stage9-final-20260718-f3bfe4c/`。`incident_manifest.json`绑定16个原始证据文件，其SHA-256为`fca62861dc51af3735a346f49d79238b23062b5e74639932159b38f14deb334f`；旧开启账本继续保持`consumed`，不得复用。
- 再次开启前必须以修复后的代码身份重建板块7/8 successor与新封印，并使用新的密钥内容签发只绑定该新封印的一次性令牌。首次失败尝试永久保持非权威失败记录。

## 官方证据采集器缺失事故与归档（2026-07-19）

- 用户授权后，`stage9-final-20260719-ce7c729` 在封印`766e94837ddcc32d648847863926ed6186a85ad7c96d3aea214ae6a43c3c87b3`和代码提交`ce7c72919bdd2c5a37c0e745ed7c9483e793ed6e`上完成了两阶段`prepare`，并稳定停在`awaiting_official_evidence`。
- 该 attempt 仅发布2022–2025规范面板与沪深证券范围；没有`CURRENT.json`、信号、目标权重、订单、成交、NAV、指标、报告或权威release。它未运行`resume`，也未产生策略表现结果。
- 根因不是官方证据被判定为空或不合格，而是仓库只有严格验证/核销合同，没有可恢复的生产采集器来构建完整的逐证券CNInfo查询包及官方原文证据工作区。静默伪造零事件或以查询标题替代事实证据均被拒绝。
- 经用户批准，该 attempt 被记录为非权威失败，原因固定为`official evidence collector unavailable before evidence acquisition`，并整体、无覆盖地归档到`processed/final_test_incidents/stage9-final-20260719-ce7c729/`。
- 归档`incident_manifest.json`绑定20个证据文件；清单SHA-256为`57a31e7f22dd61fa0fe3c284ca07dbc73e175250b39e560629263e7f60ca8f8d`。复核确认所有文件哈希一致、活动`processed/final_test`根已不存在、归档中也没有`CURRENT.json`、信号或回测产物。
- 该 token和opening ledger保持已消费、不得复用。后续必须先实现并验证完整两链官方证据采集器，重新双run和发布Stage7/8 successor/new seal，之后才能请求新的独立开启授权。
