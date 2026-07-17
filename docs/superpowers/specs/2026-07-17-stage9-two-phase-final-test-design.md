# 板块9两阶段一次性最终测试设计

## 目标

在不浪费唯一开启授权的前提下，解决“最终面板证券集合只有开启后才能确定，而官方公司行动和证券事件证据又必须精确覆盖该集合”的顺序依赖。

流程仍只允许一个用户批准、一个 `attempt_id` 和一个最终权威release。`prepare` 仅打开最终期数据并冻结证券范围；`resume` 在官方证据齐备后继续同一attempt，执行信号、回测、指标和发布。

## 不变约束

- 研究市场仅为上交所和深交所，排除北交所。
- 2022-01-01至2025-12-31只能在用户批准、令牌验证和attempt登记后读取。
- 2026年及以后数据不得读取。
- 不改变因子、方向、候选、权重、组合、成本、参数、指标或样本划分。
- 不因最终结果修改模型或报告口径。
- 原始数据只读；所有生成物写入`processed/final_test`或`artifacts/final_test`。
- 任一身份、哈希、日期或官方证据门禁失败时关闭执行。

## 方案选择

采用同一attempt的两阶段工作流：

1. `prepare`消费唯一开启令牌并登记attempt，然后构建最终期规范面板。
2. `prepare`从已验证面板生成精确沪深证券清单，并发布不可变准备清单。
3. 外部官方证据采集只以该证券清单为范围，不读取收益、信号或回测结果。
4. `resume`验证原始令牌快照、Stage8四元组、Git身份、准备清单和官方证据后，原子领取执行权。
5. `resume`继续同一attempt，生成信号、执行回测、计算预注册指标并发布一次性release。

不采用两个attempt，因为它会破坏一次性登记语义；不采用全市场证券全集预覆盖，因为它不能保证与最终面板精确一致。

## 状态机

允许的状态转换为：

```text
registered
  -> preparing
  -> awaiting_official_evidence
  -> executing
  -> published | failed
```

补充规则：

- `prepare`成功后必须停在`awaiting_official_evidence`，不得生成因子、目标权重、成交、NAV或指标。
- 官方证据缺失或尚未就绪不记为失败，也不创建新attempt。
- `resume`通过排他领取文件将状态从`awaiting_official_evidence`原子切换为`executing`；第二次领取必须拒绝。
- 进程崩溃只能恢复同一attempt。恢复前必须复核已发布准备清单和所有输入哈希，禁止重新扫描后静默替换证券范围。
- 不可恢复的身份漂移、数据漂移、证据冲突或账务门禁失败写入append-only失败结果。

## 准备阶段

### 输入

- Stage8 `run_id`、manifest SHA-256、lineage SHA-256和sealed protocol SHA-256；
- 用户批准令牌及外部批准密钥；
- 当前Git commit和tree；
- 封存的2022–2025日期范围与沪深市场范围。

### 执行顺序

1. 一次性读取并验证开启令牌。
2. 在读取首个2022文件前写入append-only attempt登记和令牌快照。
3. 将attempt状态原子切换为`preparing`。
4. 复用现有数据延伸模块构建并验证最终期规范面板。
5. 只从已验证面板提取沪深证券代码，稳定排序并去重。
6. 写入`symbol_scope.parquet`和`prepare_manifest.json`。
7. 复核准备目录全部文件哈希后，将状态切换为`awaiting_official_evidence`。

### 准备清单

`prepare_manifest.json`必须绑定：

- `attempt_id`和`approval_id`；
- Stage8四元组；
- Git commit和tree；
- 最终测试日期范围；
- `supported_markets: [sh, sz]`；
- 最终数据manifest文件记录；
- `symbol_scope.parquet`文件记录、证券数量和证券集合SHA-256；
- 创建时间、状态和schema版本。

证券集合SHA-256使用六位代码升序、每行一个代码并以换行结尾的UTF-8字节计算。

## 官方证据阶段

官方公司行动和证券事件证据必须精确覆盖`symbol_scope.parquet`中的证券：

- 每只证券都有成功查询记录；零事件也必须有官方查询成功证据。
- 公司行动正式输入只来自通过哈希校验的官方标准化行。
- BaoStock仅作为候选清单，所有候选必须由官方证据标记为`accepted`、`corrected`或`rejected`。
- 证券事件必须有逐证券覆盖、官方证据索引和缓存原文哈希。
- 证据目录不得使用符号链接、路径逃逸或未登记文件。
- 证据未齐备时attempt保持`awaiting_official_evidence`。

证据采集过程不得读取因子、目标权重、成交、NAV或最终指标。

## 恢复与执行阶段

`resume`接受`attempt_id`、批准密钥、公司行动覆盖目录、证券事件覆盖manifest和目标release ID。

恢复验证顺序：

1. attempt必须唯一且状态为`awaiting_official_evidence`。
2. 令牌快照和开启消费ledger必须存在并匹配。
3. 当前Stage8四元组、Git commit/tree和封存配置必须与准备清单一致。
4. 最终数据claim、数据manifest和证券清单文件必须逐哈希一致。
5. 从准备清单加载证券范围，不重新扫描面板来改变范围；可重新扫描仅用于证明两者仍相等。
6. 两类官方覆盖必须精确匹配准备清单证券集合并通过内容级核销。
7. 通过排他文件原子领取执行权，并将状态写为`executing`。

领取成功后复用现有信号、回测、指标、报告和发布模块。最终release继续只包含2022–2025切片，并在发布前扫描全部Parquet日期或终态关联。

## 命令接口

CLI改为显式子命令：

```text
python -m ashare_multifactor.cli.final_test prepare \
  --root <code-root> \
  --data-root <data-root> \
  --opening-token <token.json> \
  --approval-key-file <key-file> \
  --attempt-id <attempt-id>

python -m ashare_multifactor.cli.final_test resume \
  --root <code-root> \
  --data-root <data-root> \
  --approval-key-file <key-file> \
  --attempt-id <attempt-id> \
  --security-event-coverage <coverage.json> \
  --corporate-action-coverage <coverage-root> \
  --run-id <release-id>
```

旧的无子命令一体化入口必须拒绝，避免再次形成“令牌已消费但证据尚未准备”的路径。

## 模块边界

- `final_test/preparation.py`：准备状态、证券清单和准备manifest。
- `final_test/resume.py`：恢复验证、排他执行领取和授权重建。
- `final_test/pipeline.py`：只负责已准备attempt的信号、回测、指标和发布编排；不得重新授权或改变证券范围。
- `cli/final_test.py`：只解析`prepare`与`resume`命令，不包含研究逻辑。
- `final_test/registry.py`：append-only attempt事件和合法状态转换。

不在现有大型`pipeline.py`中继续堆叠准备状态机细节。

## 错误与恢复语义

- 令牌或Stage8身份在登记前无效：不创建attempt。
- 登记后授权复验失败：追加`failed`结果，令牌不得再次使用。
- 数据构建崩溃：沿现有数据claim恢复协议恢复同一attempt；数据身份不一致则失败关闭。
- 官方证据缺失：保持`awaiting_official_evidence`，不写失败结果。
- resume排他领取冲突：拒绝后续调用，不改变首个执行者状态。
- 执行或账务门禁失败：保留完整失败attempt，不发布权威release。
- 发布中断：仅允许现有prepared-publication恢复协议完成同一release。

## 测试策略

所有行为使用TDD实现，至少覆盖：

- 首个2022扫描发生前attempt已经登记；
- prepare不调用信号、回测、指标或发布；
- 准备清单稳定绑定数据和证券集合；
- 北交所代码无法进入证券清单；
- 官方证据缺失保持等待状态；
- 数据、证券清单、Stage8身份、Git身份或令牌快照漂移均拒绝resume；
- resume不重新消费令牌且只能原子领取一次；
- 崩溃恢复只能继续同一attempt；
- 旧一体化CLI入口拒绝；
- 完整合成流程可从prepare进入resume并发布一个权威release；
- 全量pytest、ruff和独立代码审查通过后才能重新双跑Stage7/8并封印。

## 完成标准

- 同一`attempt_id`贯穿prepare、官方证据、resume和最终release。
- 用户批准只消费一次，resume不创建第二个批准或attempt。
- 精确证券清单在官方证据采集前已不可变发布。
- 最终期研究结果在官方证据准备期间不可见且未计算。
- 所有恢复路径都由机器可读哈希和append-only状态证明。
- 新代码经全量验证、独立审查、Stage7/8双跑和新封印后，才再次请求用户开启。
