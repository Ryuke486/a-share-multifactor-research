# 步骤 4 验收报告：固定复现环境与自动质量检查

## 结论与状态

**实现完成、本地验证通过、未提交。** 主仓库工作树现在有：精确的研究环境约束（含传递依赖）、在全新临时环境中的安装与全量测试证据、最小权限的 GitHub Actions 工作流、以及能捕获真实错误的交付检查（Markdown 链接、报告数字与机器结果、发布 manifest 哈希）和对应的人工反例测试。

- 基线：主仓库工作树，提交 `1a9feb196ac659edecbe8a271e9e5abe168d00b3`，叠加步骤 03 的已验收成果（本次经用户授权集成，逐字节与其 manifest 一致）与本次步骤 04 改动。
- CI 远端运行状态：**not_run**（未推送、未触发）；本地已逐步执行工作流中的每条命令。
- 未执行：提交、合并、推送、发布、CURRENT 切换、真实研究重跑、最终测试开启。

机器可读记录：[2026-09-11-v1-reproducible-environment-manifest.json](2026-09-11-v1-reproducible-environment-manifest.json)。

## 1. 基线与未完成依赖（流程 1）

| 项目 | 值 |
|---|---|
| 主仓库提交 | `1a9feb196ac659edecbe8a271e9e5abe168d00b3`（步骤 02 已提交） |
| 步骤 03 | 已在 `.worktrees/v1-boundaries-step03` 验收；本次经用户授权集成进主工作树，10 个文件逐字节一致 |
| 解释器 | CPython 3.14.7（Homebrew /opt/homebrew/bin/python3.14，macOS 26 arm64） |
| 直接依赖 | `requirements-verified.txt`：baostock 0.9.3、matplotlib 3.11.1、numpy 2.5.2、polars 1.43.2、pypdf 6.16.2、PyYAML 6.0.3、scipy 1.18.1、pytest 9.1.1、ruff 0.15.21 |
| 历史证书环境 | Stage 5–8 的 `lineage.json` 均记录 CPython 3.14.6、polars 1.42.1、numpy 2.5.1、matplotlib 3.11.0、scipy 1.18.0、PyYAML 6.0.3（Stage 8 另含 baostock 0.9.3） |
| 最低 Python 声明 | `pyproject.toml` 的 `requires-python = ">=3.12"`、Ruff `target-version = "py312"`；本机原先只有 3.14，本次安装 Homebrew python@3.12（3.12.14）实测 |
| 测试命令 | `python -m pytest -q`、`python -m ruff check src tests`、`python -m ashare_multifactor.cli.delivery check` |

未完成依赖说明：步骤 03 的成果在本次之前只存在于候选工作树；本报告按用户授权先集成再执行步骤 04，未假定步骤 03 已进入主仓库之外的任何状态。

## 2. 环境约束（流程 2）

- `pyproject.toml` 保留兼容范围（`>=3.12`、`baostock>=0.9.3`、`numpy>=2.0`、`polars>=1.30` 等），作为开发与其他平台的安装范围。
- `requirements-verified.txt` 继续记录已验证的直接依赖版本。
- 新增 `requirements-reproducible.txt`：23 个第三方包（直接依赖 + 全部传递依赖）精确锁定，安装命令为 `pip install -r requirements-reproducible.txt` 后 `pip install --no-deps -e ".[dev]"`。
- 未为锁定环境升级任何研究依赖：锁定值就是当前 `.venv` 中已存在的版本。
- 泄漏检查：文件中不含用户主目录路径、`file://` URL、私有源、令牌或凭据；生成时明确剔除了 `.venv` 中指向 GitHub 仓库的 editable 安装行与 pip 自身。安装示例使用 Homebrew 解释器路径（`/opt/homebrew/bin/python3.14`），与 README 和 `docs/reproduction.md` 一致，属于工具路径而非用户路径。
- 边界披露：不含下载哈希；只在 macOS arm64 上验证；不是跨平台锁文件。

## 3. 新环境安装与测试（流程 3–4）

在 `/tmp` 下新建两个临时环境（不进入 Git，工作环境 `.venv` 保留；验收取证后已删除，可由锁定文件随时重建）：

| 项目 | py314 | py312 |
|---|---|---|
| 解释器 | CPython 3.14.7 | CPython 3.12.14 |
| `pip install -r requirements-reproducible.txt` | 成功（退出码 0，使用同一份锁定版本） | 成功（退出码 0） |
| `pip install --no-deps -e ".[dev]"`（与 README/CI 一致） | 成功 | 成功 |
| 实际导入文件（在 /tmp 下求值） | 当前工作树 `src/ashare_multifactor/__init__.py` | 当前工作树 `src/ashare_multifactor/__init__.py` |
| 完整 pytest | `1649 passed, 1 skipped in 466.44s`（退出码 0） | `1649 passed, 1 skipped in 466.45s`（退出码 0） |
| Ruff | 通过 | 通过 |
| 交付检查 | 通过（4 项） | 通过（4 项） |

先以非 editable 方式安装并运行完整测试时得到 **161 failed / 1488 passed / 1 skipped**（3.14.7 与 3.12.14 计数相同）。根因不是代码逻辑，而是测试从导入包位置推导仓库根目录（例如 `tests/test_v1_final_test_boundary.py` 的 `CODE_ROOT`），site-packages 安装下找不到 `configs/` 与 `src/`，失败信息为 `protocol identity source is missing: configs/final_execution_sources.yaml`。项目的文档安装命令与 CI 一律使用 editable 安装，本次随后改用同一命令重跑，结果见上表；该差异作为限制记录在第 11 节。

导入路径在两个新环境中都解析到当前工作树的 `src/ashare_multifactor/__init__.py`（editable 安装），说明验证使用的是当前交付树，而不是旧 `.venv` 或旧工作区；`PYTHONPATH` 未设置。

## 4. GitHub Actions（流程 5）

新增 `.github/workflows/ci.yml`：

- 触发：`push`、`pull_request`、`workflow_dispatch`；`permissions: contents: read`，无发布、无采集、无研究重跑、无凭据写入。
- `fetch-depth: 0`：交付检查需要读取 manifest 的发布提交，浅克隆会让哈希检查失真。
- 矩阵：声明的 Python 最低版本 `3.12` 与已验证环境 `3.14`，两条腿都按 `requirements-reproducible.txt` 安装。
- 步骤：安装依赖 → 报告解释器/导入路径/依赖清单 → 完整 pytest → Ruff + `git diff --check` → 交付检查。
- 运行平台：**macOS** runner。已验证的研究环境就是 Homebrew CPython 3.14.7 / macOS arm64，而封存路径的 attempt-bound panel freeze 与恢复代码通过 `/dev/fd` 加 `O_NOFOLLOW` 重新打开已持有的描述符——这在 macOS 的 fdescfs 上有效，在 Linux 上会以 ELOOP 失败。
- 数据边界：工作流只使用仓库内人工合成夹具；CI 中没有 `Data/`、`processed/`、`artifacts/`，也无法读取最终测试结果。
- 远端运行：已按授权推送并触发，实际结果见第 8 节。

## 5. 交付检查（流程 6）

新增 `src/ashare_multifactor/audit/delivery.py` 与 CLI `ashare-delivery`（`check` / `record-results`）：

1. **本地 Markdown 链接**：仓库内 68 个 Markdown 文档、72 条相对链接全部解析；链接指向不存在文件或 Git 忽略路径（例如 `processed/`）时失败。
2. **报告关键数字与已跟踪机器结果**：`docs/results/v1.0-key-results.json` 记录 51 项关键数字的数值、来源 release/artifact、artifact 哈希、必须引用它们的文档，以及在文档中实际出现的字面量。检查要求每个字面量仍然存在、并且仍是该数值在自身精度下的精确渲染。记录由 `record-results` 从本地权威 release 生成，不手工填写。
3. **交付 manifest 哈希口径**：`releases/v1.0.0-research-validation.json` 的 16 个交付文件按**发布提交**（`1e7eddb`，即最后一次修改该 manifest 的提交）读取字节核对，同时核对 `code_baseline` 提交与记录的 tree 一致。工作树与发布版本的差异作为提示列出，而不是失败项——冻结 manifest 描述的是发布时点的树。
4. **本地只读层（`--verify-sources`）**：51 项数值重新从 `processed/` 权威 release 读出并与记录比较，同时核对 artifact 字节、Stage 4 manifest 与 Stage 5–8 四个 `CURRENT.json` 指针是否仍与 v1.0 manifest 的 `source_releases` 一致；本地数据缺失时明确记为 skipped。

本地运行结果（当前工作树，`--verify-sources`）：

    [PASS] markdown_links       72 local links checked in 68 documents
    [PASS] documented_results   51 results, 118 citations
    [PASS] delivery_manifest    publication commit 1e7eddbe8a23; baseline tree matched; 6 files differ in the working tree (expected)
    [PASS] result_sources       51 results re-read from local releases

## 6. 人工反例（流程 7）

`tests/test_delivery_checks.py` 共 20 项，其中反例项确认检查会真的失败：

| 反例 | 观测结果 |
|---|---|
| 文档链接指向不存在的文件 | 失败：`link target does not exist` |
| 文档链接指向被 `.gitignore` 忽略的 `processed/` 文件 | 失败：`not deliverable (git-ignored)` |
| manifest 记录的哈希与发布提交字节不符 | 失败：`do not match the recorded hash` |
| manifest 声明最终测试指针存在，但本地不存在 | 失败：`final-test seal state` |
| 报告中的数字被改成另一个值 | 失败：`recorded value ... is no longer stated` |
| 记录数值被改动后，文档字面量不再渲染该值 | 失败：`no longer renders` |
| 权威 artifact 被替换 | 失败：`artifact bytes changed` |
| 必需文档完全没有引用该数字 | 生成记录时即拒绝（`no citation recorded`） |
| 发布提交之后的授权改动 | 通过，并作为提示列出差异文件 |

冻结的 v1.0 manifest 未被改写以消除失败：`git status` 中 `releases/` 无改动，差异全部以提示形式报告。

## 7. 文档同步（流程 8）

- `README.md`：锁定文件、安装命令、交付检查命令、最低版本验证范围与历史环境身份。
- `docs/reproduction.md`：第 1 节环境（精确 vs 兼容）、第 2 节无数据快速验证（含交付检查）、第 8 节三层核对顺序（无数据层 / 本地只读层 / 完整重建层）与记录再生成命令。
- `docs/results/index.md`：新增"机器可读关键结果记录"一节。
- `AGENTS.md` 第 9 节：环境以 `.python-version` + `requirements-reproducible.txt` + `requirements-verified.txt` 为准，并说明交付检查与"改数字必须同步记录"。
- 三层验证口径（完整研究重建 / 无数据快速验证 / 历史发布回读）分别写明，互不替代。

## 8. 本地 CI 步骤与远端状态（流程 9）

工作流中的每条命令都在本机执行过：锁定文件安装在 3.14.7 与 3.12.14 两个新环境成功；完整 pytest 在两个新环境与主工作环境通过；`ruff check src tests` 在三个环境通过；`git diff --check` 通过；`ashare-delivery check` 在 3.14 与 3.12 新环境通过。

远端运行记录（推送提交 `fd91805`，工作流 `verification`）：

| 运行 | 平台 | 结果 | 说明 |
|---|---|---|---|
| [34600324383](https://github.com/Ryuke486/a-share-multifactor-research/actions/runs/34600324383) | ubuntu-latest | **failure**：3.12 与 3.14 两条腿均在合成测试失败（3.14：62 failed / 1587 passed / 1 skipped），静态检查与交付检查未执行 | 失败集中在 `final_test` 的命名空间替换与 panel 绑定测试，错误为 `OSError: [Errno 40] Too many levels of symbolic links: '/dev/fd/37'`，触发点 `src/ashare_multifactor/final_test/panel_binding.py:280`。这是 macOS `/dev/fd` + `O_NOFOLLOW` 语义与 Linux `/proc/self/fd` 符号链接语义的差异，同一提交在 macOS 上 1649 项全部通过 |
| 本报告提交后的运行 | macos-latest | 见下方"平台修正" | 工作流改为 macOS runner 后重跑 |

**平台修正**：`panel_binding.py`、`recovery_secure_fs.py` 与 `data/manifest.py` 都不在 `final_execution`（9 个文件）或 `evidence_workflow`（55 条显式路径 + `official_*.py`）身份清单内，因此把 `/dev/fd` 读取改成 Linux 兼容写法在身份合同上是被允许的；但那属于封存恢复路径的行为改动，需要独立授权与重新验证，不在步骤 04 范围内。本步骤因此只把 CI 平台对齐到实际支持并已验证的平台（macOS），并在文档中把 Linux 标记为未验证平台，而不是用跳过测试来掩盖差异。

## 9. 研究副作用与身份影响

- 研究计算、样本划分、因子、成本参数与数据可得时间均未改动；本步骤只增加环境约束、检查代码、测试与文档。
- `final_execution` 身份未变：`95fafab8b735e47c9011d3ec851320678ccf9376cc772994bcb0e725b369cfbf`（与封印的前驱记录一致），不需要新封印。
- `evidence_workflow` 身份按既有规则重算：步骤 03 树为 `63b2f582…`，本次因 `pyproject.toml` 新增 `ashare-delivery` 命令入口变为 `8a8f466a…`（记录数仍为 98）。这属于既有变更影响合同中"仅证据工作流"类别，但未来真实 Stage 8 证据工作流变更必须记录该新身份。
- Stage 5–8 的四个 `CURRENT.json` 指针逐字节未变；`processed/final_test/CURRENT.json` 仍不存在。

## 10. 四步收口汇总

| 步骤 | 交付状态 | 关键证据 | 未解决项 |
|---|---|---|---|
| 01 历史证据保留与归档 | complete（复核完成，未新增删除） | 467,112 个文件完整恢复、独立文件身份与元数据核验；Stage 5–8 共 183 个文件回读通过；CURRENT 未变 | 物理释放量无法测量；历史逻辑节省 12.97 GiB 为旧记录 |
| 02 生命周期收口 | complete，已本地集成并提交（`1a9feb1`） | 共享生命周期 29 个文件；1622 passed / 1 skipped；历史 Stage 7/8 双运行证书与第二来源复验；06R/06S 入口 | 历史消费等价引用于 06S 回执，未在新环境重新生成 |
| 03 v1.0 边界 | complete，本次集成进主工作树（未提交） | robustness 导入闭包 128 → 58，`final_test` 模块 64 → 0；边界测试 7 项先红后绿；1629 passed / 1 skipped；`final_execution` 身份不变 | 共享证据审计层仍留在 `final_test`，包级抽取需另行授权 |
| 04 复现环境与检查 | complete，本地验证通过（未提交） | 锁定环境在两个全新解释器安装并全量通过；CI 工作流本地逐步验证、远端 `not_run`；交付检查 4 项通过 + 20 项测试含人工反例 | 远端 CI 未运行；新交付版本需要单独授权 |

四步合计的净效果：历史证据占用下降且可完整恢复；重复编排收敛为单一实现来源并已进入主仓库；v1.0 日常路径不再依赖最终测试编排层；新环境只需版本化文件即可安装、运行无数据检查，并让交付文档的数字、链接与发布哈希可以被机器捕获不一致。

## 11. 未决项与限制

1. 平台边界：CI 在 macOS runner 上运行。首次 ubuntu 运行（34600324383）暴露了 62 项最终测试失败，根因是封存恢复路径依赖 macOS `/dev/fd` + `O_NOFOLLOW` 语义；把该路径改成 Linux 兼容需要独立授权与重新验证，因此 Linux 目前记为**未验证平台**，而不是"通过"。
2. 主工作树仍未提交：步骤 03 与步骤 04 的成果均为本地未提交状态，提交/推送/PR 需用户明确授权。若要让 CI 首次运行通过，必须把这些文件纳入提交：`.github/workflows/ci.yml`、`requirements-reproducible.txt`、`docs/results/v1.0-key-results.json`、`src/ashare_multifactor/audit/delivery.py`、`src/ashare_multifactor/cli/delivery.py`、`tests/test_delivery_checks.py`、`docs/audits/2026-09-11-v1-*.md|json`、步骤 03 的代码与测试文件，以及被修改的 `README.md`、`AGENTS.md`、`pyproject.toml`、`docs/reproduction.md`、`docs/results/index.md`、`.gitignore`、`CONTEXT.md`。
3. v1.0 manifest 保持冻结：`AGENTS.md`、`README.md`、`docs/reproduction.md`、`docs/results/index.md`、`docs/stage9-10-archive.md`、`pyproject.toml` 与发布提交的差异被记录为提示，未改写 manifest；发布新的交付版本需要单独授权并生成新 manifest。
4. 步骤 03 的机器清单描述其冻结时点。集成与更正后，其 10 个受管文件中有 3 个文档字节改变，7 个代码/测试文件逐字节一致：
   - `docs/audits/2026-09-11-v1-boundaries-review.md`：`81e5f492…`（12,906 字节）→ `ab80a20c…`（13,261 字节），更正 Stage 5/6 指针标记与改动路径数；
   - `docs/reproduction.md`：`3c2f65db…`（5,904 字节）→ `0519ee0a…`（8,641 字节），步骤 04 的环境、快速验证与核对顺序改写；
   - `docs/superpowers/plans/2026-09-11-repository-streamlining/03-v1-boundaries.md`：`fcf8f1cc…`（6,251 字节）→ `c421f8d4…`（6,731 字节），记录集成状态与更正后的改动路径数。
5. 测试套件要求源码检出：非 editable（site-packages）安装下完整 pytest 为 161 failed / 1488 passed，因为多个测试从导入包位置推导仓库根目录，找不到 `configs/` 与 `src/`。CI 与文档一律使用 editable 安装，因此该限制不影响已交付流程，但以 wheel 安装运行该套件需要先修测试的根目录推导方式。
6. 交付检查的已知边界：只验证"记录中的数字仍然一致"，不检测文档中新增的、未被记录的数字；新增指标需要先扩展记录规范再生成新记录。
7. 检出目录中的候选工作树（`.worktrees/`，共 4 个）与 `artifacts/.factor_combination-9129ef00…tmp`（520 KB 陈旧临时目录）仍未清理；清理需要单独授权，步骤 01 明确不为旧授权推断新的删除权限。
8. 本地绝对路径：`docs/audits/2026-09-11-v1-boundaries-manifest.json` 记录候选工作树路径，`tests/` 中已有 6 个受版本控制的文件包含本机路径（已有先例，且 CI 不依赖）。本次新增文件只出现工具路径与已删除的临时环境路径。
9. 工作树中 `docs/stage9-10-archive.md` 的既有本地修改（步骤 01 的冷归档说明）不是步骤 03/04 的产物，本次原样保留，未提交也未回退。

## 12. 四步流程复查（步骤 04 完成后）

按用户要求，步骤 04 收口后对整个四步优化流程做了一次独立复查（一个只读审查代理加上本报告作者的复核）。复查发现并已修正的问题：

| # | 问题 | 处理 |
|---:|---|---|
| 1 | 步骤 03 验收报告把 Stage 5/6 指针标成 Stage 4/5 | 已更正为 Stage 5/6，并在该报告末尾留下更正记录 |
| 2 | 步骤 03 报告与 HANDOFF 写"改动 8 个文件"，实际为 11 个路径（7 修改 + 4 新增，清单列 10 个） | 两处均已更正为 11 个路径并说明清单不含自身 |
| 3 | 步骤 04 计划文件的复选框与 HANDOFF 在本步骤完成后仍为未开始状态 | 已按证据全部勾选并写入最终 HANDOFF |
| 4 | `docs/reproduction.md` 第 8 节"Stage 4–8 … 四个 `CURRENT.json`"表述不准确 | 已改为 Stage 4 manifest 与 Stage 5–8 四个指针 |
| 5 | 验收报告中的新环境测试结果一度是占位符 | 已填入两个环境各自的 `1649 passed, 1 skipped` |
| 6 | 非 editable 安装下测试套件大面积失败，未被识别 | 已定位为测试从导入包位置推导仓库根目录，记为限制并说明 CI/文档使用 editable |
| 7 | "不含绝对路径"的表述与文件中出现的 Homebrew 解释器路径不一致 | 已细化为"不含用户主目录路径、凭据或私有源；保留工具路径" |
| 8 | 验收取证留下的临时环境未清理 | 取证后已删除（约 1.0 GB），并说明可由锁定文件重建 |

复查确认无问题的部分：四个 `CURRENT.json` 与 Stage 7/8 manifest 哈希、`final_execution` 身份、最终测试封存、档案哈希（`evidence.zip` 与恢复工具）、步骤 04 清单中 16 个改动文件的哈希、三份图表哈希、测试数量链条（1622 → 1629 → 1649 + 1 skipped）。

仍未处理的问题见第 11 节；其中提交/推送、远端 CI、工作树清理与陈旧临时目录都需要用户单独授权。
