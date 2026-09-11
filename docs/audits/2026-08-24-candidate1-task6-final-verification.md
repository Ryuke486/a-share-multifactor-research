# 候选 1 任务 06 重新验证报告

## 结论

候选 1 **已通过任务 06S 收口**，状态为 `complete`。

2026-08-27 按用户批准顺序，先对本机、系统临时根以及 UUID 匹配的外接迁移盘执行只读历史字节恢复；文件名、大小、哈希与 artifacts/processed 归档均未找到历史文件。路线 1 失败后进入路线 2：公司行动规范化对同一经济事件保留最强官方来源谱系，现金与送转事件均有公开接口回归。用新 BaoStock 原始表重建后，26445 行 `corporate_actions.parquet` 与两个认证运行全字段多重集零差异，`security_events.parquet` 亦零差异。

Stage 7 `publish=false` 影子暂存通过，98 个研究、回测、账务及核心文件无哈希差异。Stage 8 successor 影子暂存通过，三个核心文件、action coverage 和 collector readiness 证据树一致，opening token 仍为 `closed`。`sealed_test_protocol.json` 的字节不同仅来自隔离临时 opening-ledger 路径与后续权威 predecessor 绑定；代码身份相同，核心协议门与证据树已单独精确验证。未执行真实发布、指针切换或 Stage 9。

2026-08-26 的历史 06R 执行曾以单进程、单会话完整查询 2718 只证券，13590 次查询全部成功并正常登出；新原始表为 9516 × 16，SHA-256 为 `77e4fb6e076d87d4d5536fa1d4d292ff2f678ccd45a3d087fa35dd18579d3980`。当时冻结代码因 `000042` 的弱来源去重问题产生 `source` 和派生 `action_id` 各一处差异，因此 06R 当时正确失败关闭。该失败记录保留不改写；后续 06S 已修复根因并重做全部门槛，以上历史状态已被本报告的完成结论取代。

推荐修复方案已按“独立恢复根、严格历史哈希、不得回写生产根”的边界执行。原先缺失的 12 个 Stage 7 历史输入中，11 个已恢复到系统临时根并逐文件命中历史 SHA-256。2026-08-26 再次使用 BaoStock 时，单进程、单会话、完全串行地从检查点续传成功，2112 个目标证券的 10560 次年度查询全部返回成功，联合表形状也恢复为历史的 9516 × 16；但新文件 SHA-256 为 `435741913656864417d3619ff46df5fed587ec1b65515e56d47294cb01060d1e`，不等于历史绑定的 `ffdfdc8dd0c4835009da53a69ad88c5984d34407d7612887867e4a3006048bab`。因此该输入仍未按严格字节身份恢复；这是路线 1 的失败结论。后续 06S 仅在机器可读零差异豁免核验通过后映射该一项身份，再完成 Stage 7 `publish=false` 暂存。

代码侧复核发现并修复了 `reproduce()` 比历史语义多执行一次 `adapter.bind()` 的兼容缺陷；先增加失败断言，再最小修复，并保留外部测试替换公开 `run_once()` 的兼容表面。相关测试、Ruff 和差异检查均通过；最终全量 pytest 的末次结果见下方记录。

## 范围与基线

- worktree：`/Users/mikasa/本科时期/Projects/repository1/.worktrees/reproducible-release-lifecycle-candidate1`
- HEAD：`1e7eddbe8a2319110f275d254b1a7702e41d1aea`
- Stage 7 当前发布：`65b19e1_stage7_validation_controlled_collector_successor`
- Stage 7 指针 SHA-256：`cc73d95fbebcce6de6666f8dade09c1312c06da15bd8b1fb4d2dc7c7ca3973af`
- Stage 7 manifest SHA-256：`6ee12a10b25a53d8a48c596faa76c1833d92c10c511e570d301a5f96c10836fd`
- Stage 8 当前发布：`a1076c2_stage8_robustness_szse_statistics_successor`
- Stage 8 指针 SHA-256：`0e42ea37107707d014c743fef3ef040ab61bd47f025db0cb81aa09577f9cac3b`
- Stage 8 manifest SHA-256：`c75948b4c47007c6f5690451afbebdff02eb5d3c793e6285eb799af898ea1fba`
- 重新验证前后仅检查精确路径 `processed/final_test/CURRENT.json`，结果均为不存在；本轮读取计数为 0。
- 未修改 `Data/`、真实 `processed/`、真实 `artifacts/`、历史 release、真实 Stage 7/8 指针、`audit.publication`、Stage 6 或 Stage 9。
- 未提交、合并、推送或创建 PR。

## 修复实施

### 代码兼容缺陷

双轴审查发现 `reproduce()` 在两个 `run_once()` 之后为获取 profile/data root 又调用了一次 `adapter.bind()`，使每次复现产生五次绑定，而历史语义只需要四次。

- 在 lifecycle 测试适配器中记录 `bind_count`，新增期望值 4；首次运行按预期失败并观测到 5。
- 修复后仍通过公开 `run_once()` 执行两次运行，再从已知 run-root 布局读取 compatibility profile/data root，不新增绑定。
- 初版内部 helper 会绕过既有 monkeypatch 合同，相关兼容测试将其捕获；最终实现恢复公开 `run_once()` 替换表面后，相关测试全部通过。

### 计划状态一致性

任务 01、02 已有完整 HANDOFF，但执行和验收复选框仍为空。已按现有证据补齐，不改变任务内容或完成标准。

### 恢复策略

- 恢复根：`/tmp/repository1-candidate1-task6-recovery.smoam3`
- 只在恢复根中重建/下载；不回写真实数据根。
- daily panel 使用原始 2017–2021 数据重建，再用历史 Polars `1.42.1` 重序列化。
- 官方 PDF 从原来源恢复，BaoStock metadata 按历史输入集合和统计重新生成。
- 每项只有在字节 SHA-256 等于历史 lineage 记录时才计为恢复成功。

## 恢复证据

以下 11 个文件已精确恢复：

| 文件 | SHA-256 |
|---|---|
| `daily_panel/manifest.json` | `926e7ad649915e3c4d322279f86ee804c9c5b8f1cdf23475588df0e9a47bd8d6` |
| `daily_panel/quality_issues.json` | `fb50267ac9760b2b427d1c79ecf61088770e12ed698331944faf9c3bda3f982b` |
| `daily_panel/year=2017/part-000.parquet` | `27d97b6948c9a6235253a666fbe2e2b143a8b3519b9928e34efc408cc072c11c` |
| `daily_panel/year=2018/part-000.parquet` | `fd33a8b5151a26f4eb3d7dbacf16bd6828dc4352b34abcd5723b2f4fa4c1cd23` |
| `daily_panel/year=2019/part-000.parquet` | `d952d094756fe23f60377fa5e20a2eab596bc94f442cf0c4a2f58313ed6b9994` |
| `daily_panel/year=2020/part-000.parquet` | `ce8396b5b384f424d736973e4cb6f0e97051deebb091202a30708244001be740` |
| `daily_panel/year=2021/part-000.parquet` | `5a98c88888e56e2f7ba86e7e1e77541a32e260a850af87fc1c0ebd027f23194a` |
| `baostock_dividends_execution_union.metadata.json` | `2006ea14f28883d890968a7bb075587122f50844749c8a56e59cafde057e8eb9` |
| `000819_1210269787.pdf` | `e796433bda3b35226a8a560834485f6783870788397df60df3d0eeeafb5f698b` |
| `000916_1204242424.pdf` | `e7d80db31de5967db9c1184d9081339dd9252967077235df39fbc50e8793b54e` |
| `000979_c0f86b35.pdf` | `6ad69710152c6d8e8f8d7bba9cba01508b0a662ec549798451486de959e8592e` |

唯一未按历史字节恢复的文件：

- `artifacts/validation_evaluation/source_cache/baostock_dividend/baostock_dividends_execution_union.parquet`
- 历史 SHA-256：`ffdfdc8dd0c4835009da53a69ad88c5984d34407d7612887867e4a3006048bab`
- 历史大小：177,037 字节；历史形状：9,516 × 16。
- 原检查点保留 150 个目标证券、485 行；2026-08-26 从第 151 个目标继续，单会话完成剩余 1962 个证券，目标分片最终为 7738 行。
- 本次登录、全部查询和登出均返回成功；没有并发连接，没有错误后重试，也没有第二次登录探测。
- 新联合表形状为 9516 × 16，大小为 175,512 字节，SHA-256 为 `435741913656864417d3619ff46df5fed587ec1b65515e56d47294cb01060d1e`；历史大小为 177,037 字节。
- 已排除 24 种简单分片拼接顺序以及证券代码—查询年份稳定排序，均未命中历史哈希。当前证据支持 BaoStock 历史响应发生修订或编码细节变化的推断，但无法在缺少历史原文件时定位到具体字段。
- 未伪造该文件，也未以形状相同、metadata、空文件或新哈希文件替代历史字节。

## 自动验证

### 相关回归

- 结果：`122 passed in 1.58s`
- 退出状态：0

### 全量 pytest

- 修复前基线：`1596 passed, 1 skipped in 423.03s`
- 修复后末次运行：`1596 passed, 1 skipped in 439.84s (0:07:19)`，退出状态 0。

### 静态与文档检查

- Ruff：`All checks passed!`
- `git diff --check`：通过。
- 10 个变更 Markdown 文件的本地链接检查：通过，无缺失目标。
- `audit.publication.py` 相对基线未修改。

### 任务 06R 自动验证

- 06R 针对性测试：`8 passed in 7.19s`。
- 06R 完整 pytest：`1604 passed, 1 skipped in 448.27s (0:07:28)`，退出状态 0。
- 06R 验证辅助与测试 Ruff：通过；全仓 Ruff 与最终差异检查见 06R HANDOFF。

### 任务 06S 最终自动验证

- 公司行动、成交账务、豁免映射等聚焦回归：`67 passed in 0.50s`。
- 最后一次行为修复后完整 pytest：`1606 passed, 1 skipped in 532.65s (0:08:52)`，退出状态 0。
- 全仓 Ruff：`All checks passed!`；`git diff --check` 和两份最终 JSON 解析通过。
- Standards/Spec 双轴复审均无剩余可操作问题。

## Stage 7 影子验证

已通过：

- 认证证书重建与历史证书逐字节相同。
- 认证源运行复验通过。
- 7 个 daily panel 文件、3 个官方 PDF 和 BaoStock metadata 已在恢复根精确恢复。
- BaoStock 查询覆盖和历史形状已恢复，但原始联合表的严格 SHA-256 未恢复。
- 恢复根中精确路径 `processed/final_test/CURRENT.json` 不存在。

历史严格字节路线仍未通过（已由 06S 替代路线收口）：

- 最后一个原始 BaoStock parquet 与历史绑定哈希不一致，因此不能按“原始字节等同”单一路线完成 binding。
- 06S 后续通过零差异豁免映射完成了 Stage 7 核心文件映射、阶段语义比较和 `publish=false` 暂存。

### 任务 06R 消费层复验

- 两个认证运行的 `corporate_actions.parquet` 与 `security_events.parquet` 彼此均全字段一致。
- 新数据重建的两张表与历史表列名、类型和行数一致；`security_events.parquet` 的 5 行全字段多重集一致。
- `corporate_actions.parquet` 的 26445 行经济键多重集一致，但完整行多重集有 1 条历史独有和 1 条新生成独有。
- 历史行：`action_id=10bf9b4859c85e944e2a35bf`，`source=baostock_dividend_operate_year+cninfo_action_correction`。
- 新生成行：`action_id=c50a57248ff45e5cff9271d3`，`source=baostock_dividend_operate_year`。
- 两行均为 `000042`、除权日/生效日 `2018-06-22`、每股现金 `0.2`；差异由新响应同时包含 6 月 22 日与历史异常的 6 月 28 日记录、冻结代码去重时优先保留已修正供应商记录所致。
- 机器可读失败记录见 `docs/audits/2026-08-26-candidate1-task6r-external-source-revision-assessment.json`。
- 该结果不能被表述为消费层零差异，不能支持外部修订豁免。

## Stage 8 影子验证

- 证书重建与源运行复验通过。
- 使用证书绑定的固定干净身份 `763d489527efaa7ccb05d8890fe871e684fe65ee` 执行 `publish=false` 暂存通过。
- 三个核心文件、action coverage 和 collector readiness 目录树与历史发布逐字节相同。
- lineage、前任绑定和关闭的 opening token 语义正确。
- 未调用发布，影子最终测试指针不存在。

## 双轴审查

- Standards：原发现为任务 01/02 已完成但复选框未同步；已修复，复审无剩余 Standards 问题。
- Spec：额外 `bind()` 兼容缺陷已以失败测试修复；`CONTEXT.md` 属于 `docs/agents/domain.md` 明确要求的 single-context 领域文档，不判为越界。
- 06S 复审先后关闭了送转事件官方谱系覆盖和影子证据持久化缺口；最终 Standards/Spec 均无剩余可操作问题。

## 最终边界核对

- Stage 7/8 指针与当前 manifest 在末次重读中仍等于基线哈希。
- 本轮只检查最终测试 `CURRENT.json` 的精确存在性，没有枚举或读取最终测试目录/内容。
- 先前失败尝试的 1 次最终测试目录元数据枚举保留在审计历史中，不被本轮结果覆盖。
- 所有恢复和影子写入只发生在系统临时根；真实根无写入。

## 收工结果

任务 06S 已完成上述必要门槛，历史 06R 失败记录保留不覆盖。机器可读终局证据为 `docs/audits/2026-08-27-candidate1-task6s-external-source-revision-waiver.json`，路线顺序和外盘搜索结果为 `docs/audits/2026-08-27-candidate1-task6s-route-assessment.json`。

# 停止边界

本报告只收口候选 1 任务 06 的本轮自动修复与重新验证。不提交、合并、推送、切换真实指针、开始其他候选或进入 Stage 9。
