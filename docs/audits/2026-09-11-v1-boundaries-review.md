# 步骤 3 验收报告：v1.0 与延期最终测试的边界

## 结论与状态

**实现完成、验证通过、未提交。** v1.0 库层（`audit`、`research`、`validation`、`robustness`、`data`、`factors`、`combination`、`portfolio`、`execution`）现在**完全不导入** `ashare_multifactor.final_test`；唯一保留的接触点是显式的操作命令桥接。研究样本、因子、方向、成本、组合规则、来源证据、在线 release 与最终测试封存状态均未改动。

机器可读记录：[2026-09-11-v1-boundaries-manifest.json](2026-09-11-v1-boundaries-manifest.json)。

工作位置：`.worktrees/v1-boundaries-step03`，分支 `codex/v1-boundaries-step03`，基线提交 `1a9feb196ac659edecbe8a271e9e5abe168d00b3`（步骤 02 已验收目标树）。本步骤未提交、未合并、未推送、未发布、未启动步骤 4。

## 1. 隔离基线（流程 1）

| 项目 | 值 |
|---|---|
| 基线提交 | `1a9feb196ac659edecbe8a271e9e5abe168d00b3`（含步骤 02 全部候选 1 成果） |
| 环境 | `.venv` Python 3.14.7 / polars 1.43.2，与 `.python-version`、`requirements-verified.txt` 一致 |
| Stage 5 指针 | `processed/factor_combination/CURRENT.json` = `0733c609…` |
| Stage 6 指针 | `processed/formal_backtest/CURRENT.json` = `2f8d2d35…` |
| Stage 7 指针 / manifest | `cc73d95f…` / `6ee12a10…` |
| Stage 8 指针 / manifest | `0e42ea37…` / `c75948b4…` |
| 最终测试 | `processed/final_test/CURRENT.json` 不存在 |

原有改进已包含在基线中：共享生命周期、两个适配器、兼容外观与候选 1 的 1622 项测试全部在基线提交内。

## 2. 盘点结果（流程 2）

### 2.1 规模

源码共 62,392 行；`final_test` 91 个文件、40,795 行，占 **65.4%**（与初查约 65% 一致）。该比例只说明关注点，不构成迁移理由。

### 2.2 三层职责划分（实测）

| 层 | 包 | 对 `final_test` 的依赖（改动前） |
|---|---|---|
| v1.0 计算 | `research`、`validation`、`data`、`factors`、`combination`、`portfolio`、`execution` | 0 |
| v1.0 共享审计 | `audit`、`robustness` | `robustness` → 64 个 `final_test` 模块，其中 **27 个是最终测试专属编排**（授权 `gate`、attempt `registry`、`resume`、`preparation`、`historical_attempt`、`execution_*`、采集与事件归档等） |
| 延期最终测试 | `final_test` | — |

改动前唯一的跨层入口是 `src/ashare_multifactor/robustness/evidence_workflow_rehearsal.py`（487 行）：它导入 `final_test.official_announcement_routing` 与 `final_test.official_candidate_review_admission`，函数级只需要 4 个入口（routing frame、历史准入复算、准入契约常量与类型），但模块级导入把 64 个模块、27 个编排模块一并拉入 v1.0 的 import 闭包。

### 2.3 调用者与动态引用

- 唯一函数调用者：`cli/robustness.py` 的 `rehearse-evidence` 命令（Stage 8 证据工作流演练）。Stage 8 successor 发布只消费产物根 `--evidence-workflow-readiness-root`，不调用演练函数。
- 源码身份绑定：`robustness/protocol_identities.py` 的 `_FINAL_EXECUTION_PATHS`（9 条，含 7 个 `final_test` 执行文件 + 2 个配置）与 `_EVIDENCE_WORKFLOW_EXTRA_PATHS`（54 条）+ `final_test/official_*.py` 通配（43 个文件），改动后证据工作流身份共 98 条记录。
- 其他动态引用：`pyproject.toml` 脚本表（未变）、`configs/`（未引用该模块路径）、`docs/reproduction.md`（本次新增边界说明）。

## 3. 精确改动表（流程 3）

| 现路径 | 职责 | 目标位置 | 旧接口兼容方式 | 受影响测试 | 历史身份处理 |
|---|---|---|---|---|---|
| `robustness/evidence_workflow_rehearsal.py` | 演练延期最终测试的证据工作流（公告路由、候选准入复算、就绪度报告） | `final_test/evidence_workflow_rehearsal.py` | 旧路径保留**惰性兼容外观**（模块级 `__getattr__`）：导入不加载 `final_test`，按需取用时才解析到新位置 | `test_evidence_workflow_successor.py`（2 处导入改到新位置）、`test_robustness_release_compatibility.py`（monkeypatch CLI 属性，无需改动） | 证据工作流身份新增新路径并保留旧路径；`final_execution` 域零变化 |
| `cli/robustness.py` | 操作命令入口 | 不变 | 导入改到新位置；命令名、参数与输出不变 | 同上 CLI 合同测试 | 该文件本就在证据工作流身份清单内 |
| `robustness/protocol_identities.py` | 源码身份清单 | 不变 | 仅新增一行路径 | 无 | 证据工作流身份按既有规则重算 |
| `tests/test_v1_final_test_boundary.py` | 新增边界合同测试（7 项） | 新增 | — | 自身 | 不纳入身份清单，避免每次改测试都改变身份 |

被评估后**不实施**的两条路线（记录理由，不做静默取舍）：

1. **把共享证据审计层整体搬出 `final_test`**：v1.0 两个入口的函数级闭包覆盖 26 个模块，模块级存在 46 处跨界导入，其中 `official_review_contract → execution_contracts`、`official_announcement_catalog → gate/resume/preparation` 等跨越被封印身份绑定的模块。这属于包级重构，不是"最小职责调整"，且对达成 v1.0 库层边界并非必需。
2. **搬迁被冻结的合同模块**（`action_source_contract.py`、`execution_contracts.py`、`resume.py` 等）：它们由封印的 `final_execution_identity` 绑定，任何内容或路径变化都会把变更影响类别从"仅证据工作流"改成包含 `final_execution`，从而要求新封印与真实演练——本步骤不授权。

## 4. 特征测试与人工反例（流程 4）

新增 `tests/test_v1_final_test_boundary.py`，7 项：

1. v1.0 库层任何模块不得以 import 语句引用 `ashare_multifactor.final_test`（AST 检查）；
2. v1.0 库层中唯一提及该包的只能是命名桥接外观；
3. 在子进程中导入全部 v1.0 库模块（含子模块遍历）与验证 CLI 后，`sys.modules` 中不得出现任何 `final_test` 模块；
4. 旧路径导入不加载 `final_test`，取用属性时才解析到新位置；
5. 外观不臆造属性（未知属性抛 `AttributeError`）；
6. **人工反例**：v1.0 稳健性 CLI `run` 在缺失输入时失败关闭，且不在目标根创建任何文件（不创建 token、attempt 或执行产物）；
7. 身份合同完好：`_FINAL_EXECUTION_PATHS` 仍为原 9 条，`build_final_execution_identity` 记录集不变，证据工作流身份同时绑定新旧两个演练路径且所列文件全部存在。

红绿证据：把旧版演练模块放回 `robustness/` 后，第 1、4 项失败（第 4 项观测到 51 个 `final_test` 模块被加载），强化后的第 3 项同样失败；恢复改动后 7 项全部通过。

## 5. 依赖对比：维护成本实际减少了什么（流程 8）

| 指标 | 改动前 | 改动后 |
|---|---:|---:|
| `robustness` import 闭包模块数 | 128 | **58** |
| 闭包内 `final_test` 模块数 | 64 | **0** |
| 闭包内最终测试编排模块数 | 27 | **0** |
| v1.0 库层导入 `final_test` 的模块数 | 1 | **0** |

效果：读 v1.0 稳健性发布路径（`pipeline`、`release_adapter`、`evidence_workflow_readiness`、`change_impact`、`successor_seal`、`test_protocol`）不再需要理解延期最终测试的授权、attempt、恢复与采集代码；跨层依赖收敛为一个有名字、有注释、有测试的桥接（`rehearse-evidence` 命令 + 惰性外观）。这不是"只拆文件"：闭包内 27 个编排模块的依赖被真正移除，剩余耦合只存在于显式调用点。

## 6. 历史身份与封印（流程 6）

| 域 | 封印记录（当前 Stage 8 release） | 改动后当前树 | 判定 |
|---|---|---|---|
| `final_execution`（9 条） | `95fafab8b735e47c9011d3ec851320678ccf9376cc772994bcb0e725b369cfbf` | 同值 | **域未变** |
| `evidence_workflow` | `0cdeb1e4…`（97 条） | `63b2f582…`（98 条，含本步骤全部改动） | 仅本域变化，符合"仅证据工作流变更"类别 |
| `research` | 未涉及 | 未涉及 | 未变 |

（`evidence_workflow` 的具体哈希随本步骤每个被绑定文件的最终字节变化；判定只取决于"是否仅该域变化"，不依赖某一次中间值。）

结论：本次调整**不需要新协议版本或新封印**——它落在既有变更影响合同允许的 `evidence_workflow` 域内；但本步骤不发布 successor、不重封、不切换指针，也不改写任何历史回执。

历史回读（改动后实测）：Stage 7 `65b19e1_…` 99 条清单、Stage 8 `a1076c2_…` 33 条清单逐文件核验通过；两组认证运行对各自证书的 98 / 3 个核心哈希、代码身份与输入身份逐项相同，第二认证源运行复验通过；封印 `status=sealed`、`protocol_version=4`、`opening_token_status=closed`、`test_period=["2022-01-01","2025-12-31"]`；四个 CURRENT 指针前后逐字节不变；`processed/final_test/CURRENT.json` 不存在。

## 7. 验证证据（流程 7）

| 项目 | 命令 | 结果 |
|---|---|---|
| 边界与证据工作流相关 | `PYTHONPATH=src .venv/bin/python -m pytest tests/test_evidence_workflow_successor.py tests/test_robustness_release_compatibility.py tests/test_robustness_release_adapter.py tests/test_robustness_publication.py tests/test_final_test_cli.py tests/test_v1_final_test_boundary.py -q` | `57 passed in 16.14s`，退出码 0 |
| 完整 pytest | `PYTHONPATH=src .venv/bin/python -m pytest -q` | `1629 passed, 1 skipped in 431.39s`，退出码 0（基线 1622 + 新增 7 项；末次运行在冻结后的步骤 03 树上执行） |
| Ruff | `.venv/bin/python -m ruff check .` | `All checks passed!` |
| 空白检查 | `git diff --check` | 通过 |
| 历史回读与封存 | 见第 6 节 | 通过 |

研究计算、排序规则、参数与数据可得时间均未改动：本步骤只改模块归属、一处导入、一份身份路径清单、测试与文档。

## 8. 文档（流程 5）

- `docs/reproduction.md` 第 7 节新增边界规则、`rehearse-evidence` 命令示例与惰性外观说明；未声称 Stage 9 已完成。
- `CONTEXT.md` 新增领域词条"最终测试证据演练"，明确它属于最终测试证据层而非 v1.0 计算层。

## 9. 做完必须检查的问题

- [x] 每个迁移模块是否有明确理由，是否遗漏动态引用或源码身份绑定？——迁移 1 个模块；调用者、身份清单、CLI、测试与文档同步更新，全仓检索无遗留旧引用。
- [x] v1.0 必需的证据能力是否仍可用，旧导入和 CLI 是否兼容？——旧路径惰性可用，新路径为新家；`rehearse-evidence` 命令名、参数与输出未变。
- [x] 共享层是否摆脱最终测试编排依赖，是否没有新增循环依赖？——9 个 v1.0 包对 `final_test` 的闭包模块数均为 0；未新增反向环。
- [x] 人工反例是否证明 v1.0 入口不会创建 token、attempt 或触发最终测试执行？——第 4 节反例 3 与 6。
- [x] 研究计算、排序、参数和数据可得时间是否保持一致？——未触碰这些路径；完整测试通过。
- [x] 历史 release、封印和血缘是否仍按原规则可核验？——第 6 节。
- [x] 是否没有改旧哈希、放宽校验或改写失败记录来适配新结构？——`final_execution` 路径清单与身份值不变；`evidence_workflow` 仅按既有规则重算；历史回执字节未改。
- [x] 新旧接口测试、相关回归、完整 pytest 和 Ruff 是否通过？——第 7 节。
- [x] 文档是否明确共享能力与延期功能边界，没有宣称 Stage 9 已完成？——第 8 节。
- [x] 改动是否实际减少跨层依赖，而不是仅增加文件、包装或抽象？——闭包 128→58、编排 27→0（第 5 节）。

## 10. 未决项与限制

1. 共享证据审计层（公告路由、候选准入、目录校验、文档验证等）仍在 `final_test` 内。v1.0 库层已与其解耦，但若未来要让该层同时服务 v1.0 与 v2.0，需要一次包级抽取（约 26 个模块、46 处跨界导入），应作为独立授权任务评估，并预期改变 `evidence_workflow` 身份。
2. 惰性外观按需解析新位置；这是保持旧导入路径与移除导入期依赖之间的取舍。若未来要求"导入即失败"的强边界，可在外观中改为显式报错，但会破坏既有调用方。
3. 本步骤未重封、未发布 successor；将来真实 Stage 8 证据工作流变更仍需按既有流程生成新的 `evidence_workflow` 身份与证书。
4. 步骤 04（可复现环境与检查）未启动。

## 下一步

已验收目标树：`.worktrees/v1-boundaries-step03`，基线 `1a9feb19…`，本次改动 11 个路径（7 个修改、4 个新增；机器可读清单列出其中 10 个文件，不含清单自身）。集成（提交/合并/推送）与步骤 04 均需用户明确授权后另行执行。

> 2026-09-11 更正记录：步骤 04 集成时更正了本节的两处事实错误——Stage 5/6 指针原先被误标为 Stage 4/5；改动路径数原先写成 8 个文件。更正原因与更正前后的哈希见[步骤 04 验收报告](2026-09-11-v1-reproducible-environment.md)。
