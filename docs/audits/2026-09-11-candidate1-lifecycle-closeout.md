# 步骤 2 收口报告：可复现发布生命周期候选 1 复核与交付清单

## 结论与状态

**候选 1 实现验收完成，集成待授权。** 本次只做复核、测试、只读发布复验和交付清单，没有把候选代码写入主仓库，没有提交、合并、推送、发布或切换任何 `CURRENT.json`。

- 候选 1 的权威设计、六个任务加 06R/06S 的失败与整改事实均由实际文件复核，未被最终一句 `complete` 掩盖。
- 主仓库 `main` 仍是旧实现：`ashare_multifactor.audit.reproducible_release` 在主仓库不存在，两个 pipeline 仍是各自的双运行—认证—发布编排。
- 后续步骤 3 必须使用本报告记录的已验收目标树；在主仓库获得集成授权前，不以旧主仓库启动后续重构。

机器可读交付清单：[2026-09-11-candidate1-lifecycle-closeout-manifest.json](2026-09-11-candidate1-lifecycle-closeout-manifest.json)（29 个交付文件的路径、状态、字节数与 SHA-256，以及指针、发布清单和归档哈希）。

## 服务总项目的方式

本步骤减少 Stage 7/8 重复编排带来的维护风险，并把“双运行—认证—发布”收敛为单一可测试实现，使复现证据链在后续重构中仍可核验；它不改变因子、样本划分、成本、组合规则或最终测试期封存状态。

## 输入与范围

本次输入：

- 根目录 `AGENTS.md`、`CONTEXT.md`、权威设计 `docs/superpowers/specs/2026-08-23-reproducible-release-lifecycle-design.md`、执行地图和任务 01–06、06R、06S；
- 候选工作区 `.worktrees/reproducible-release-lifecycle-candidate1/`；
- 该工作区 `docs/audits/` 的最终验证报告、06R 失败评估和 06S 豁免/路线证据；
- 真实 Stage 7/8 发布、复现证书、认证运行与 `processed/` 指针（只读）。

不触碰范围：`Data/`、真实 `processed/` 与 `artifacts/` 写入、历史 release 字节、Stage 6、Stage 9、`audit.publication`、最终测试期、网络数据采集、提交/合并/推送/发布。

本次不做：不追加新架构、不重新采集 BaoStock、不执行真实发布、不切换指针、不打开或统计最终测试期结果。修订交付物、任务内容和验收标准均未改动。

## 1. 基线与交付清单

| 项目 | 值 |
|---|---|
| 主仓库 HEAD | `1e7eddbe8a2319110f275d254b1a7702e41d1aea`（`main`） |
| 候选工作区 HEAD | `1e7eddbe8a2319110f275d254b1a7702e41d1aea`（分支 `codex/reproducible-release-lifecycle-candidate1`） |
| 集成基线 | 两处 HEAD 相同，候选无基线外提交；集成即以主仓库 HEAD 为目标树 |
| 候选 tracked 补丁 | 40,204 字节，SHA-256 `2fbca783851e742f5be1e6528b135ec39bfabab41a5546378507a3ccba0c8a25` |
| 交付文件数 | 29（4 个已跟踪文件修改，25 个新文件） |

主仓库当前工作树改动均为与本步骤无关的既有内容，予以保留且不纳入交付清单：`docs/stage9-10-archive.md` 的修改、步骤 1 的 `docs/evidence-archive-2026-09-11.md`、`docs/superpowers/plans/2026-09-11-repository-streamlining/`，以及 2026-08-23 计划集的**未执行旧副本**（见第 8 节）。

29 个交付文件按来源分类：

- 共享生命周期：`src/ashare_multifactor/audit/reproducible_release.py`。
- 两个阶段适配器：`src/ashare_multifactor/validation/release_adapter.py`、`src/ashare_multifactor/robustness/release_adapter.py`。
- 两个兼容外观：`src/ashare_multifactor/validation/pipeline.py`、`src/ashare_multifactor/robustness/pipeline.py`。
- 公司行动来源修复：`src/ashare_multifactor/validation/corporate_actions.py`。
- 配套测试 8 个：生命周期、两个适配器、两个兼容、06R 豁免辅助与测试各一，以及 `tests/test_validation_backtest.py`。
- 审计与领域文档 5 个：`CONTEXT.md`、候选 1 最终验证报告、06R 失败评估、06S 豁免与路线评估 JSON。
- 设计、执行地图与任务书 10 个（9 份 `docs/superpowers/plans/2026-08-23-reproducible-release-lifecycle/` 任务书加设计文档，含只在候选存在的 06R、06S）。

以上 1 + 2 + 2 + 1 + 8 + 5 + 10 合计 29 个文件，与机器可读清单一致。

**发现：** 逐任务 `HANDOFF.files_changed` 并集只有 19 个路径，29 个交付文件中有 10 个未被任何任务书列出（`CONTEXT.md`、06R 评估 JSON、`00-execution-map.md`、`03`/`04`/`05` 任务书自身、`06R` 任务书、设计文档、`tests/task6r_revision_waiver.py`、`tests/test_task6r_revision_waiver.py`）。其中 06R 的辅助模块与测试来自一份没有 `files_changed` 字段的 `HANDOFF`，属于本次清单纠正的真实缺口；其余为任务书自身更新或设计文档。清单以本报告附带的机器可读文件为准。

## 2. 候选完成状态与失败事实

复核结论：任务 01–05 `HANDOFF.status=complete`；任务 06 在经历 06R 失败关闭后由 06S 收口为 `complete`。证据链完整，且失败记录保持原样：

- 06R 的 `HANDOFF.status=incomplete`、`external_source_revision_waiver=rejected`、`consumer_semantic_differences=2` 保留在 `06R-external-source-revision-waiver.md` 与 `docs/audits/2026-08-26-candidate1-task6r-external-source-revision-assessment.json`，未被改写。
- 06S 只在“消费层零差异 + 生产契约整改”路线上重新验收，并在任务书正文明确标注“保留 06R 当时失败事实”。
- 06R 的两条硬门槛（影子身份映射、Stage 8 影子验收）在任务书中仍显示为未执行并附原因，没有被 06S 的结果冒充。

历史失败的技术事实：历史 BaoStock 原始联合表（9,516 × 16，177,037 字节，SHA-256 `ffdfdc8d…48bab`）无法按字节恢复；2026-08-26/27 串行重取的新表形状相同但 SHA-256 为 `77e4fb6e…d3980`（175,011 字节）。该字节差异在本次复核中未被重新采集，也未伪造成历史字节。

## 3. 差异清单与隔离工作区

复用候选工作区作为唯一实现与验证工作区，未复制改动到主仓库，未覆盖无关修改。差异清单与交付清单一致（29 个文件，哈希见机器可读清单）。

主仓库方向另有一处**状态不一致**：主仓库当前工作树中 2026-08-23 计划集是执行前副本，而候选工作区版本包含执行后的复选框、`HANDOFF` 和新增的 06R/06S 任务书。逐文件比较结果：

- `00`–`04` 任务书：任务内容逐字相同，仅状态行、复选框和 `HANDOFF` 不同。
- `05`、`06` 任务书：候选版本另有执行证据内容（`05` 新增“Pipeline 职责四分类”和“旧测试替代映射”表；`06` 把验收步骤改写为 06S 豁免路线、并记录先前失败尝试的 1 次最终测试目录元数据枚举）。
- `06R`、`06S` 任务书只存在于候选工作区。
- 设计文档与 `CONTEXT.md` 两侧内容相同（仅末尾空行差异）。

因此集成应取候选版本；主仓库旧副本不得反向覆盖，否则会丢失执行证据。

## 4. 共享层职责检查

- `audit/reproducible_release.py` 只暴露 `run_once`、`reproduce`、`release`（`__all__` 实测为这三项），并内置 `stage7_v1`、`stage8_v1` 两个兼容配置。核心路径由阶段 pipeline 常量惰性读取，没有第二份清单。
- 两个适配器各自只有 `bind`、`execute`、`prepare_release` 三项阶段能力（外加不可变绑定/选项值对象），没有第四个钩子或注册表。
- 两个 pipeline 的全部生命周期调用都指向共享模块；全仓库只有 `audit/reproducible_release.py` 写 `run_manifest.json`，共享编排确为唯一实现来源。
- 兼容外观保留：两阶段公开函数**签名逐字未变**（AST 比较），CLI 模块零改动，`audit/publication.py` 与 `audit/__init__.py` 零改动。仅删除了两个已被等价覆盖的私有函数 `_assert_validation_run_tree`、`_certify_recorded_runs`，无残留引用。
- 阶段差异仍在阶段模块：Stage 7 的 98 文件封闭树、safe-slug、`resource_usage`、封存日期与运行时审计；Stage 8 的 3 文件宽松树、旧式 run ID、无 `resource_usage`、协议/市场门禁与 successor 树。

## 5. 测试与静态检查（本次实际运行）

解释器与导入身份（防止误用主仓库旧代码）：

- `CANDIDATE1_PYTHON` = `/Users/mikasa/本科时期/Projects/repository1/.venv/bin/python`，Python 3.14.7。
- 依赖：polars 1.43.2、numpy 2.5.2、scipy 1.18.1、PyYAML 6.0.3、matplotlib 3.11.1、pypdf 6.16.2、baostock 0.9.3、pytest 9.1.1、ruff 0.15.21。
- 该虚拟环境以 `.pth` 指向主仓库 `src`（可编辑安装）；从候选工作区根显式设置 `PYTHONPATH=src` 后，实测 `ashare_multifactor` 解析到候选工作区 `src/ashare_multifactor/__init__.py`，`audit.reproducible_release` 解析到候选 `src`；同一解释器在主仓库导入该模块抛 `ModuleNotFoundError`，证明测试运行的是候选代码。

命令与结果（工作目录均为候选工作区根）：

| 项目 | 命令 | 结果 |
|---|---|---|
| 生命周期/适配器/兼容/06R | `PYTHONPATH=src $CANDIDATE1_PYTHON -m pytest tests/test_reproducible_release_lifecycle.py tests/test_validation_release_adapter.py tests/test_validation_release_compatibility.py tests/test_robustness_release_adapter.py tests/test_robustness_release_compatibility.py tests/test_task6r_revision_waiver.py -q` | `89 passed in 1.10s`，退出码 0 |
| 公司行动与账务 | `… -m pytest tests/test_corporate_action_coverage.py tests/test_corporate_action_rebase.py tests/test_corporate_action_sources.py tests/test_validation_corporate_action_source.py tests/test_validation_backtest.py tests/test_broker.py tests/test_execution_data.py -q` | `77 passed in 0.65s`，退出码 0 |
| 完整 pytest | `PYTHONPATH=src $CANDIDATE1_PYTHON -m pytest -q` | `1606 passed, 1 skipped in 415.09s`，退出码 0 |
| Ruff | `$CANDIDATE1_PYTHON -m ruff check .` | `All checks passed!`，退出码 0 |
| 空白检查 | `git diff --check` | 通过，退出码 0 |

与历史记录的区别：任务 06/06S 记录的是当时的 `122 passed`、`67 passed`、`1606 passed, 1 skipped in 532.65s`。本次是重新运行的独立结果，测试集合略有差异（本次聚焦集合为 89 + 77），不能与历史数字相互替代。

反例覆盖确认（对应本步骤检查项）：61 个兼容/适配器/生命周期测试覆盖双运行、代码与输入漂移、核心哈希不一致、dirty 身份不可发布、缺文件、目标目录已存在、失败清理、safe-slug、封闭树符号链接与额外文件、Stage 8 宽松目录与旧式 run ID、两阶段 CLI 契约，以及固定清单/证书字节。

## 6. 历史发布只读复验与影子验证判定

用既有只读发布读取器（`opened_verified_current`）复验两个真实发布，前后各读一次指针字节：

| 发布 | run_id | 磁盘文件 / 清单条目 | manifest SHA-256 | 结果 |
|---|---|---:|---|---|
| Stage 7 | `65b19e1_stage7_validation_controlled_collector_successor` | 100 / 99（98 核心 + lineage） | `6ee12a10…836fd` | 全部清单条目哈希核验通过 |
| Stage 8 | `a1076c2_stage8_robustness_szse_statistics_successor` | 34 / 33（3 核心 + lineage + successor 证据） | `c75948b4…1fba` | 全部清单条目哈希核验通过 |

两个发布各自还有 `manifest.json` 本身不计入清单条目；核验由既有读取器在共享读锁下完成，未改动任何发布字节或指针。

Stage 7 lineage SHA-256 `daf7804230c7901d99f0e98adf3eea93608be3eb25568413cec2ac50d363404e`，与 06S 回执中 `supersedes.lineage_sha256` 一致；Stage 8 封印 `status=sealed`、`protocol_version=4`、`opening_token_status=closed`、`test_period=["2022-01-01","2025-12-31"]`、上游验证运行 `65b19e1_…`。

用候选生命周期自身的只读步骤重放证书重建与认证源运行复验（不写证书文件）：

- Stage 7：`stage7_controlled_65b19e1_1/_2` 两运行输出一致，98 个核心文件哈希、代码身份与冻结输入身份**逐项等于**历史证书；认证源运行 `…_2` 复验通过。
- Stage 8：`stage8_tree_763d489_1/_2` 同上，3 个核心文件与证书逐项一致；认证源运行复验通过。

影子验证判定：**复用 06S 已有回执，并说明依据，未重复执行影子暂存。**

1. 代码身份未变：全部代码与测试文件的修改时间不晚于 2026-08-27 10:11，早于 06S 豁免回执（10:17）与最终验证报告（10:18）；`git status` 与任务书 `files_changed` 描述一致；本次完整 pytest 结果与 06S 记录一致（`1606 passed, 1 skipped`）。
2. 输入身份未变：历史证书绑定的认证运行仍在磁盘上且逐文件核验通过；本次未产生任何新输入。
3. 无法就地重放：真实根缺失 12 个 Stage 7 冻结输入——7 个验证日日面板文件、BaoStock 原始联合表与其 metadata、3 份官方 PDF；其中 11 个当时只恢复到已清理的系统临时根，BaoStock 原始字节始终未按历史身份恢复。重做影子暂存需要重新采集 BaoStock 或从冷归档恢复约 23 GiB。按本步骤“不借本步骤重新采集”的约束，未执行重放，也未在真实发布根试运行。
4. 06S 回执本身记录了影子结论：Stage 7 `publish=false` 暂存 98 核心文件零哈希差异；Stage 8 `publish=false` 暂存三核心文件与两棵 successor 证据树一致、opening token 为 `closed`；`sealed_test_protocol.json` 的字节差异被解释为隔离临时 opening-ledger 路径与前驱绑定（本次复核确认当前权威封印的 `protocol_version=4`，影子记录的后继封印为 `3`，与代码中“后继=3、v4=4”的分支一致，不构成新差异）。

## 7. 公司行动来源修复与豁免一致性

修复范围：`validation/corporate_actions.py` 的 `_normalize_baostock_actions`。送转行在命中官方行动修正时也标注 `+cninfo_action_correction`（现金行此前已支持该档，并另有 `+cninfo_payment_date` 档），并在拼接后按经济事件键（symbol、ex_date、effective_date、cash_per_share、share_ratio）保留最强官方来源谱系（`+cninfo_action_correction` > `+cninfo_payment_date` > 供应商原始）。金额、日期、股份比例与成交账务逻辑未改动。

与已批准豁免一致：06S 回执 `remediation.behavior` 描述的正是“按经济事件去重并优先保留最强冻结官方谱系”，并记录现金与送转两条谱系均已覆盖、`consumer_semantic_differences=0`、重新生成 26,445 行且两个认证运行各零差异。

本次可独立复核的部分（只读）：

- 两个认证运行的 `datasets/corporate_actions.parquet` 完全相同，SHA-256 `d5f5b74933562e7ac27d794db7e142d59364af6cadd4be29d49405fef53b9509`、519,893 字节、26,445 行，与 06S 回执记录的历史身份一致。
- 两个认证运行的 `datasets/security_events.parquet` 完全相同，SHA-256 `540f41bd73ae918d0f08d8b7beece7c901319efb3760f09886d514f6b0f6f543`、3,289 字节、5 行，与回执一致。
- 历史争议行确实存在：`000042`、2018-06-22、每股现金 0.2、`action_id=10bf9b4859c85e944e2a35bf`、来源含 `+cninfo_action_correction`。
- 新增参数化测试（现金与送转两种形态）在合成输入下恢复出同一个 `action_id=10bf9b4859c85e944e2a35bf`，即修复后的代码可从公开接口重建历史消费行的身份。

未决证据缺口（本次新发现，记录不改写历史）：`tests/task6r_revision_waiver.py` 的身份映射门槛要求豁免声明 `production_adapter_modified=False`，而 06S 交付的机器可读豁免记录的是 `true`（06S 确实修改了生产代码）。实测调用 `map_verified_baostock_identity(actual, certified, 06S豁免)` 会抛出 `Task6RWaiverError: external source revision waiver is not approved`。因此 06S 的豁免 JSON 无法由交付代码重放，其“影子身份映射通过”的结论目前只由该 JSON 自身与最终验证报告支持，没有对应的可执行验证路径。06R 的门槛语义本身是正确的（06R 授权明确要求生产适配器零修改），问题在于缺少与 06S 授权相匹配的豁免校验入口，或缺少对该 JSON 的显式说明。该缺口不影响历史发布、指针或最终测试封存状态，但应在集成前决定处理方式。

重新生成侧无法离线复核：2026-08-27 重建的消费表保存在已清理的临时根，本次未重新下载，故 `consumer_semantic_differences=0` 只能引用 06S 回执，不能作为本次独立测量。

## 8. 集成说明（待授权）

授权后建议的集成顺序（本次未执行）：

1. 从候选工作区取本报告清单中的 29 个文件写入主仓库目标树；删除或覆盖主仓库中 2026-08-23 计划集的执行前副本，采用候选版本（含 06R/06S）。
2. 保留主仓库既有无关改动：`docs/stage9-10-archive.md` 的修改与步骤 1 新增文档。
3. 集成后复验：两阶段公开入口签名、两个 pipeline 委托关系、`audit/publication.py` 零差异、完整 pytest 与 Ruff，并再次只读核验 Stage 7/8 指针与 `processed/final_test/CURRENT.json` 不存在。
4. 集成动作如需提交，只添加清单内文件，不使用 `git add .`。

## 边界未改变证据

- Stage 7 指针 SHA-256 `cc73d95fbebcce6de6666f8dade09c1312c06da15bd8b1fb4d2dc7c7ca3973af`，Stage 8 指针 SHA-256 `0e42ea37107707d014c743fef3ef040ab61bd47f025db0cb81aa09577f9cac3b`；测试前后两次读取完全相同。
- 两个 release manifest 哈希与基线相同（`6ee12a10…`、`c75948b4…`），历史 release 字节未被改写。
- `processed/final_test/CURRENT.json` 按精确路径检查始终**不存在**；本次只做该精确路径存在性检查，未枚举或读取最终测试目录内容。
- 冷归档 `artifacts/evidence_archive/2026-09-11/evidence.zip` 实测 SHA-256 `2d2f574c319ff4dbb32b8fbd3e82c0d4ce2fbc30027ff15f8141a09a72871d55`，与步骤 1 文档和归档 `HANDOFF.json` 记录一致。
- 未修改 `Data/`、真实 `processed/`、真实 `artifacts/`、历史 release、`audit.publication`、Stage 6、Stage 9，未提交、合并、推送或发布。

## 未决项

1. 06S 豁免无法由交付代码重放（第 7 节）；需决定是补充与 06S 授权匹配的豁免校验入口，还是在豁免 JSON 中显式声明其校验路径，任何改动都应先写失败反例。
2. 本步骤前置条件记录不完整：步骤 1 的机器可读回执（`artifacts/evidence_archive/2026-09-11/HANDOFF.json` 等）显示归档执行 `PASSED` 且本次复核确认归档哈希一致，但 `01-evidence-retention-and-archive.md` 要求新增的 `docs/audits/` 复核报告在主仓库中不存在，其 `HANDOFF` 仍为 `not_started`。用户在本次明确启动步骤 2，故未阻塞执行；该报告不属于本步骤交付范围。
3. 影子暂存未在本次重放（第 6 节），依据为代码与输入身份未变；若后续任何交付文件被修改，现有 06S 影子回执即失效，必须重新验收。
4. 环境差异：历史证书绑定 Python 3.14.6 与 polars 1.42.1，当前项目环境为 Python 3.14.7 与 polars 1.43.2。只读复验与兼容测试不受影响，但**新**的真实 Stage 7/8 运行会产生合法但不同的代码身份与派生血缘哈希，不能声称与历史运行逐字节相同。
5. 主仓库仍是旧实现；在获得集成授权前，步骤 3 只能以本报告记录的候选目标树为基线。

## 下一步

已验收目标树：候选工作区 `.worktrees/reproducible-release-lifecycle-candidate1/`，HEAD `1e7eddbe8a2319110f275d254b1a7702e41d1aea`，29 个交付文件的哈希见 `docs/audits/2026-09-11-candidate1-lifecycle-closeout-manifest.json`。

集成待授权，候选工作区不清理（清理需成果已安全保存且另有授权）。本步骤到此停止，不自动开始[步骤 3](../../docs/superpowers/plans/2026-09-11-repository-streamlining/03-v1-boundaries.md)。
