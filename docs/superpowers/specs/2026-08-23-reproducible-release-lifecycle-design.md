# Stage 7–8 可复现发布生命周期设计

**状态：** 已批准设计；候选 1 已完成实施，2026-09-11 已同步到主仓库本地工作树。历史失败与收口证据见执行地图及主仓库整改报告。
**范围：** 仅 Stage 7 验证期评价与 Stage 8 稳健性分析
**实施入口：** [任务 00：执行地图](../plans/2026-08-23-reproducible-release-lifecycle/00-execution-map.md)

## 1. 问题

Stage 7 与 Stage 8 都实现了同一类高价值编排：

1. 解析冻结输入和代码身份；
2. 在隔离目录执行一次完整阶段计算；
3. 生成运行清单；
4. 运行两次并比较核心产物；
5. 判定干净 Git 身份下的发布资格；
6. 写入复现证书；
7. 发布前重新核验第二个认证运行；
8. 构造暂存树、血缘和阶段专属封印；
9. 调用既有不可变发布原语。

这些步骤目前分别位于 `validation.pipeline` 与 `robustness.pipeline`。两份实现的领域门禁不同，但编排骨架重复。继续复制会让后续安全修复必须在两个位置同步，也会增加证书、源运行复验和发布前身份重检逐渐漂移的风险。

本设计把共同编排收拢为一个**可复现发布生命周期**，同时把 Stage 7 与 Stage 8 的历史差异保留在各自适配器和内置兼容配置中。

## 2. 目标

- 让双运行、认证、源运行复验和发布调用只有一个编排实现。
- 保持现有 Stage 7/8 调用路径、函数签名、CLI、返回值、文件字节和错误契约。
- 让阶段专属研究计算、门禁、血缘和封印继续由阶段包拥有。
- 让未来修改能够分别证明“共同生命周期正确”和“阶段翻译正确”。
- 在不运行真实全量研究计算的情况下，用历史发布读取和临时根目录影子验证完成第一轮验收。

## 3. 非目标

- 不纳入 Stage 6、Stage 9 或其他阶段。
- 不打开、读取、统计或绘制 2022–2025 最终测试结果。
- 不改变研究配置、候选方案、成本、组合规则或评价指标。
- 不修改 `ashare_multifactor.audit.publication`。
- 不新增 CLI、适配器注册表、插件发现、远程存储或文件系统抽象。
- 不从 `ashare_multifactor.audit.__init__` 导出新生命周期。
- 不建立新的公共异常层级。
- 不在本设计实施中切换任何真实 `CURRENT.json`。

## 4. 设计原则

### 4.1 一个深模块，三个入口

新内部模块：

`src/ashare_multifactor/audit/reproducible_release.py`

它只提供三个生命周期概念，名称可在实现时按现有类型标注细化，但不得扩展为更多外部阶段：

```python
run_once(adapter, code_root, *, run_id=None)
reproduce(adapter, code_root, *, run_id_prefix=None)
release(
    adapter,
    code_root,
    *,
    run_id,
    publish,
    options=None,
    certificate=None,
)
```

- `run_once`：绑定身份、创建隔离运行目录、执行阶段计算、检测运行中漂移并写入兼容运行清单。
- `reproduce`：运行两次、重读两份运行清单、比较核心产物、判定发布资格并写入兼容复现证书。
- `release`：重读或重建证书、重新核验认证源运行、调用阶段发布准备；`publish=false` 返回暂存结果，`publish=true` 才调用既有发布原语。

`reproduce` 永远不自动发布。认证源运行固定为同组第二次运行，与当前行为一致。

### 4.2 阶段适配器只有三个能力

文件位置：

- `src/ashare_multifactor/validation/release_adapter.py`
- `src/ashare_multifactor/robustness/release_adapter.py`

适配器边界：

```python
bind(...)
execute(...)
prepare_release(...)
```

- `bind`：解析规范根目录和配置，执行阶段前置门禁，捕获代码与冻结输入身份，选择内置兼容配置，并返回发布保护条件。
- `execute`：调用现有阶段领域计算，返回阶段需要追加到旧运行清单的字段。
- `prepare_release`：执行阶段专属发布门禁、文件映射、血缘构造以及 Stage 8 封印，返回既有发布原语所需的暂存目录和元数据。

适配器不提供任意生命周期钩子。新增阶段差异时，优先判断它属于 `bind`、`execute` 或 `prepare_release` 中的哪一项；无法归类意味着边界需要重新设计，而不是继续增加回调。

### 4.3 两个内置兼容配置

兼容配置由生命周期模块拥有，不由适配器任意拼装：

| 口径 | 必须保留的行为 |
|---|---|
| Stage 7 v1 | 98 个核心路径；封闭运行树；拒绝符号链接和额外文件；safe-slug 运行 ID；`resource_usage`；`full_reproducibility.json` 既有 schema 与字节格式 |
| Stage 8 v1 | 3 个核心路径；现有较宽松目录与运行 ID 行为；不新增 `resource_usage`；`reproducibility.json` 既有 schema 与字节格式 |

不得以“顺便加固”为由让 Stage 8 获得 Stage 7 的目录或 ID 规则，也不得删除 Stage 7 的现有规则。

### 4.4 现有入口是兼容外观

以下现有路径继续可导入，函数名和参数不变：

- `ashare_multifactor.validation.pipeline`
- `ashare_multifactor.robustness.pipeline`
- `ashare_multifactor.cli.validation`
- `ashare_multifactor.cli.robustness`

至少以下已观察入口必须保持兼容：

| Stage 7 | Stage 8 |
|---|---|
| `finalize_validation_run` | `finalize_robustness_run` |
| `certify_full_validation_runs` | `compare_robustness_runs` |
| `execute_full_validation_run` | `execute_robustness_run` |
| `execute_validation_reproducibility` | `execute_robustness_reproducibility` |
| `stage_validation_release` | `verify_reproducible_source` |
| `run_validation_release` | `publish_robustness_release` |

任何当前测试、CLI 或仓库代码直接引用的其他 pipeline 名称，也必须留在原模块，直到证明没有调用者且已有等价覆盖。兼容外观可以委托给生命周期或阶段适配器，但不得要求调用方迁移。

### 4.5 发布原语保持独立

`ashare_multifactor.audit.publication` 已负责冻结发布树、原子安装、清单生成与 `CURRENT.json` 切换。生命周期只调用它，不包裹为端口、不复制其逻辑，也不改变其锁、恢复或安全树语义。

## 5. 职责分配

| 职责 | 生命周期 | Stage 7 适配器 | Stage 8 适配器 | publication |
|---|---:|---:|---:|---:|
| 两次独立运行编排 | ✓ |  |  |  |
| 运行失败后的新目录清理 | ✓ |  |  |  |
| 代码/输入漂移复查顺序 | ✓ |  |  |  |
| 兼容运行清单编码 | ✓ | 提供数据 | 提供数据 |  |
| 两运行核心哈希比较 | ✓ | 提供核心路径 | 提供核心路径 |  |
| 发布资格和复现证书编码 | ✓ | 提供阶段口径 | 提供阶段口径 |  |
| 研究计算 |  | ✓ | ✓ |  |
| 日期、协议、审计和市场门禁 |  | ✓ | ✓ |  |
| 阶段文件映射与血缘 |  | ✓ | ✓ |  |
| Stage 8 最终测试协议封印 |  |  | ✓ |  |
| 冻结发布树和指针切换 |  |  |  | ✓ |

## 6. 必须保留的阶段差异

### 6.1 Stage 7

- 对约定日期列执行 2022 起始日封存检查。
- 核验 98 个核心文件及封闭运行树；拒绝符号链接、额外文件和路径逃逸。
- 运行 ID 保持 safe-slug 规则与原错误消息。
- 冻结输入继续绑定 Stage 4、5、6、验证日面板和官方证据文件。
- `shadow_nav_audit`、`stale_audit` 和上游发布门禁保持原样。
- 运行清单继续包含 `resource_usage`。
- 复现证书继续写入 `processed/validation_evaluation/full_reproducibility.json`。
- 发布暂存继续执行 98 文件映射、前任漂移复查和既有血缘生成。

### 6.2 Stage 8

- 继续执行 2005–2021 分析期、预注册实验矩阵、主候选和指标门禁。
- 核心比较仍只覆盖当前 3 个核心文件。
- 市场范围、协议门禁、`sealed_test_protocol`、successor audit 与 collector readiness 逻辑保持原样。
- 运行清单不增加 `resource_usage`。
- 复现证书继续写入 `processed/robustness/reproducibility.json`。
- 不新增 Stage 7 的封闭树或 safe-slug 规则。
- 发布暂存继续生成 Stage 8 lineage、seal 和可选 successor audit 树。

## 7. 兼容性合同

### 7.1 固定身份下的字节兼容

在固定或模拟的代码身份、输入身份、时间和资源值下：

- 运行清单 JSON 字节完全相同；
- 复现证书 JSON 字节完全相同；
- 暂存文件映射和 lineage 字节完全相同；
- 异常类型和已覆盖错误消息完全相同；
- 既有函数返回类型和路径语义完全相同。

### 7.2 新真实运行

真实新运行允许代码身份和由它派生的血缘哈希变化。除这些合法身份差异外，阶段领域结果、核心文件集合、证书语义和发布包结构必须等价。

### 7.3 历史发布

- 当前 Stage 7 与 Stage 8 发布必须仍能由既有解析器完整读取和核验。
- 历史 release 文件不得改写。
- 影子验证不得切换真实 `processed/validation_evaluation/CURRENT.json` 或 `processed/robustness/CURRENT.json`。
- `processed/final_test/CURRENT.json` 必须继续不存在。

### 7.4 错误合同

不建立新异常层级。兼容外观继续向调用方暴露当前 `ValueError`、`FileNotFoundError`、`FileExistsError` 等类型和已测试消息。生命周期内部若需要结构化失败，只能在外观返回前翻译回现有合同。

## 8. 验证策略

第一轮实施采用以下证据，不做昂贵的真实数据全量重跑：

1. 通过现有公开入口建立 Stage 7/8 特征测试；
2. 用固定身份快照证明清单、证书和错误字节兼容；
3. 用两个简单适配器直接测试生命周期成功与失败路径；
4. 运行相关测试、完整 pytest 和 Ruff；
5. 只读解析当前 Stage 7/8 发布，核对 `CURRENT.json`、manifest 和 lineage；
6. 把所需历史运行/发布文件复制到临时根目录，执行非发布影子认证与暂存；
7. 前后比较真实 `CURRENT.json` 字节和历史发布清单哈希；
8. 确认没有生成最终测试期权威指针。

Stage 7 当前发布约 360 MiB，影子验证使用独立临时副本，不使用可能产生写时别名的硬链接。临时空间不足时失败关闭并保留检查记录，不退化为在真实发布根上试运行。

## 9. 实施顺序

| 顺序 | 任务 | 完成后的唯一新增能力 |
|---:|---|---|
| 01 | [锁定兼容行为](../plans/2026-08-23-reproducible-release-lifecycle/01-characterize-compatibility.md) | 可证明旧行为的测试基线 |
| 02 | [建立共享生命周期](../plans/2026-08-23-reproducible-release-lifecycle/02-build-lifecycle-core.md) | 由简单适配器验证的三入口深模块 |
| 03 | [迁移 Stage 7](../plans/2026-08-23-reproducible-release-lifecycle/03-migrate-stage7.md) | Stage 7 通过适配器委托且保持兼容 |
| 04 | [迁移 Stage 8](../plans/2026-08-23-reproducible-release-lifecycle/04-migrate-stage8.md) | Stage 8 通过适配器委托且保持差异 |
| 05 | [删除重复编排](../plans/2026-08-23-reproducible-release-lifecycle/05-remove-duplication.md) | 单一共享编排来源 |
| 06 | [最终与影子验证](../plans/2026-08-23-reproducible-release-lifecycle/06-verify-and-handoff.md) | 候选 1 的完整验收证据 |

每项任务必须单独获得开始授权。一个任务的完成不授权下一任务，不授权提交、合并、推送、真实发布或最终测试期操作。

## 10. 完成定义

候选 1 只有同时满足以下条件才完成：

- 三个生命周期入口和两个三能力适配器已实现；
- 现有 Stage 7/8 入口、参数、CLI、返回值和错误合同未改变；
- 所有列出的历史差异均有自动测试；
- 两个 pipeline 不再各自拥有双运行—认证—发布的共享编排；
- `audit.publication` 未被修改；
- 全量 pytest 与 Ruff 通过；
- 历史发布读取和临时根影子认证/暂存通过；
- 真实 `CURRENT.json`、历史 release 和最终测试封存状态未改变；
- 没有实施 Stage 6、Stage 9、远程存储、插件系统或新的公共 API；
- 形成可供后续审查的变更清单、验证日志和限制说明。

