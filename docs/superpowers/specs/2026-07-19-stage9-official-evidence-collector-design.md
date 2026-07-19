# 板块9官方证据采集与重封设计

## 背景与范围

当前attempt `stage9-final-20260719-ce7c729` 已在已授权的两阶段 `prepare` 中构建 2022–2025 规范面板和精确沪深证券范围，并停在 `awaiting_official_evidence`。它没有生成信号、目标权重、订单、成交、NAV、指标、报告或权威 final-test release。

当前代码已有官方查询覆盖、公司行动核销和证券事件核销的严格验证器，但没有生产采集器或已准备的最终期证据包。因此该 attempt 无法安全 `resume`。经用户批准，本设计将它作为非权威基础设施失败完整归档，再补齐采集器并重新封印。

本修订不改变样本划分、研究市场（仅沪深）、因子、方向、候选、权重、交易制度、成本、参数、指标或评价口径。它不以任何最终期策略结果为依据；当前 attempt 未计算此类结果。

## 目标与非目标

目标是建立可恢复、可审计的官方证据采集链，使新 attempt 能在同一 `prepare` 冻结证券范围后准备两类独立证据：

1. 每证券、每预注册类别的 CNInfo 查询覆盖；
2. 每条非零公司行动或证券事件的官方公告/PDF 原文、哈希、来源身份和字段级核销。

非目标包括：从公告标题推断经济字段、把查询结果数量当作事件数量、自动把未分类公告视为零事件、读取因子/回测输出、修改已有研究规则，或将原始网络响应提交 Git。

## 选择的架构

### 1. 当前 attempt 的不可变事故归档

先通过 append-only registry 将当前 attempt 记为非权威失败，失败原因固定为“缺少已验证的官方证据采集能力，无法在不伪造覆盖结论的前提下恢复”。随后复用受保护的 `archive_failed_attempt`：它必须复核 token/attempt/prepare/data-claim 身份、完整面板和准备清单，生成含全部文件 SHA-256 的 incident manifest，并以无覆盖原子移动归档整个 `processed/final_test` 根。

不得删除、重写或复用当前 token、opening ledger、prepare 清单或面板。归档成功后正式 `processed/final_test` 根不存在；任何中断或验证失败均保留原根并失败关闭。

### 2. 查询覆盖采集器

新增三个聚焦单元，而不是扩张现有验证器：

- `official_query_client.py`：唯一负责 CNInfo `POST` 请求、超时、指数退避重试、速率限制和无 cookie/无密钥的响应返回；
- `official_query_collector.py`：从已验证的 prepare 清单推导精确 scope，创建/恢复单个查询包并最终写覆盖索引；
- `cli/final_test.py` 的 `collect-queries` 子命令：只做授权与准备身份复核、参数解析和进度输出，不承载采集或研究逻辑。

采集器只接受状态为 `awaiting_official_evidence` 的同一 attempt，使用批准密钥重新验证已消费的授权快照，但绝不创建、消费或刷新 token。它从 `symbol_scope.parquet` 派生稳定排序的证券集合，拒绝任何自行提供的证券列表、符号链接、范围外市场或更改后的 prepare 文件。

每个 `(symbol, market, logical_category)` 写入一个独立、不可覆盖的查询包。当前两个逻辑类别的 CNInfo `category` 参数都为空，仍分别查询和缓存，避免将“同一传输请求”误当成两个独立覆盖结论。每包包含既有验证器要求的 `request.json`、逐页原始响应、`pages.json` 和 `query_manifest.json`。分页必须从 1 连续到服务端声明的页数；完成前不能发布包或索引。

包写入 attempt 专属、Git 忽略的目录：

```text
processed/final_test_evidence/<attempt_id>/official_query_coverage/
  packages/<category>/<market>/<symbol>/query-package/
  official_query_coverage.json
```

单包使用 staging 目录与原子、无覆盖发布。重跑时只接受既有完整且通过验证的包；未完成 staging 包可在确认其请求身份相同后继续，身份不一致则拒绝。索引只能在所有预期 scope 通过验证后原子发布。网络失败、429、超时、JSON 不合法、分页不一致或服务端重复公告均保持未完成，不写“零事件”。默认单线程和固定最小间隔；可配置的最大 scope 数只用于中断后分批继续，不改变预期全集。

缓存文件只保存公开请求字段、响应字节、哈希和时间戳；不得保存批准密钥、token 正文、cookie、认证头或环境变量。

### 3. 官方原文与事件核销采集

查询覆盖只证明已查询，不能产生事件事实。采集器的第二条链从完成的查询包构建一个不可变公告目录，包含公告 ID、证券、市场、标题、发布时间、官方附件 URL 和其来源响应位置。它仅接受现有白名单内的 CNInfo、上交所或深交所 URL。

随后按两类来源任务写入 attempt 专属证据工作区：

- 公司行动：BaoStock 仅形成候选清单。每个候选必须关联官方公告/PDF 原文字节、证据索引和标准化官方行动行；候选与官方行逐字段核销为 accepted、corrected 或 rejected。
- 证券事件：每证券的来源查询、官方原文字节、证据索引和标准化事件行必须一对一可追溯。事件数量按证券/市场/来源逐键核对，不以总数替代。

PDF/公告内容无法确定证券、日期、金额、比例或事件类型时，采集器将其写入明确的 `needs_review` 队列并停止该类证据的 `ready` 发布。不得从标题、候选数据或缺失项猜测字段，也不得把未完成审核解释为零事件。

正式 `corporate_action_coverage` 与 `security_event_coverage` 继续使用现有验证器，并必须引用完成的官方查询索引。只有它们均为 `ready`、范围与 prepare scope 完全相同、所有证据文件哈希正确且逐行核销完成时，`resume` 才能领取执行权。

### 4. 生命周期、恢复与路径安全

证据根与 prepare 清单通过 `attempt_id`、approval ID、Git commit/tree、Stage8 四元组、sealed protocol SHA-256、prepare manifest SHA-256 和 scope SHA-256 绑定。任何一项漂移、目录替换、符号链接、相对路径逃逸、混入其他 attempt 或先前索引替换均拒绝。

采集过程不改变 attempt 状态：完整查询或下载不等于可以执行。它可在网络中断后从已有已验证包继续；没有完整索引时仍保持 `awaiting_official_evidence`。再次调用 `prepare`、更换证券范围或在当前 attempt 下生成第二个 token 一律拒绝。

### 5. 实现、验证和重新封印顺序

1. 用合成 HTTP transport 与文件夹夹具为归档、重试、分页、恢复、范围绑定、无密钥落盘和核销阻断写失败测试。
2. 先实现最小归档调用与查询覆盖采集器，再实现公告目录、下载缓存和需要人工核实队列；每一项都先观察 RED，再最小 GREEN。
3. 在 2021 年及以前的单证券官方查询上仅验证网络连通性；实现和重封期间不再采集 2022–2025。
4. 运行相关测试、全量 pytest、Ruff 和独立安全审查。
5. 以干净提交对 Stage7 执行两次独立完整验证并发布 successor；重新绑定 Stage8，执行两次独立稳健性验证并发布 successor 与新 seal。
6. 复核历史 incident、旧 release 和旧 seal 未改写，正式 final-test 根为空，才请求用户单独批准新 seal。
7. 新授权后才运行新的 `prepare`，再运行 collector、完成正式事件核销，最后同一 attempt `resume` 一次。

## 测试与完成标准

至少覆盖：

- 当前 prepared attempt 只能先写非权威失败 outcome 后归档，归档完整性/崩溃恢复/并发替换均失败关闭；
- collector 拒绝未验证 prepare、错误 attempt、范围漂移、非沪深证券和已有不一致包；
- HTTP 超时、429/5xx、无效 JSON、漏页、总数漂移、重复公告、部分写入和中断恢复均不产生完成索引；
- 不会把密钥、cookie、token 或请求认证信息写入缓存；
- 查询索引仅在精确 `2 × symbol_count` scope 完整时发布；
- 非零事件没有官方原文/证据 ID/字段核销时始终不能 `ready`；歧义进入 review 队列而不生成输入行；
- `resume` 继续拒绝只含查询覆盖或不完整核销的证据；
- 全量测试、Ruff、Stage7/8 双 run 和发布后哈希审计均有新鲜证据。

成功标准不是“下载到很多公告”，而是新 seal 绑定可复验采集能力且不降低任何事件事实、市场范围或一次性最终测试门禁。只有后续新 attempt 完成证据、`resume`、最终 release 和检查点 C，板块9才完成并可判断是否开启板块10。
