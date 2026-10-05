# v1.0 复现说明

## 1. 环境

- 精确研究环境：`requirements-reproducible.txt`，固定已验证环境中的全部直接与传递依赖版本。
- 兼容范围：`pyproject.toml` 声明 `requires-python = ">=3.12"`，直接依赖版本见 `requirements-verified.txt`。
- 当前验证环境为 CPython 3.14.7（Homebrew，macOS arm64）；权威 Stage 7/8 release 使用 CPython 3.14.6，各自版本记录在对应 release 的 `lineage.json`。
- 锁定文件不含下载哈希，因此是版本锁定而不是供应链锁文件；它在 macOS arm64 上用于真实数据研究运行，CI 也用它在 Linux x86_64 上安装并通过全部合成检查。
- 平台边界：真实数据研究运行只在 macOS arm64 上产生过。此前 Linux 上的 `ELOOP` 失败全部来自延期最终测试的封存恢复代码（见[步骤 04 验收报告](audits/2026-09-11-v1-reproducible-environment.md)）；该代码于 2026-10-05 移出 main 后，CI run 37326943900 在 Linux 上通过全部合成测试、静态检查与交付检查，Linux 腿现为必须通过项。

按锁定文件建立研究环境：

```bash
/opt/homebrew/bin/python3.14 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-reproducible.txt
.venv/bin/python -m pip install --no-deps -e ".[dev]"
```

只想按兼容范围安装（例如在未验证的操作系统或较新的 Python 上）时：

```bash
/opt/homebrew/bin/python3.14 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[dev]"
```

历史证书保留 CPython 3.14.6 / polars 1.42.1 / numpy 2.5.1 / matplotlib 3.11.0 / scipy 1.18.0 身份。重跑已封存阶段必须生成新的代码身份、依赖身份与证书；不得用当前锁定文件冒充历史环境，也不得覆盖历史记录。

## 2. 无真实数据快速验证

以下命令只使用人工合成测试夹具，不读取 `Data/`、验证期 release 或最终测试期：

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests
.venv/bin/python -m ashare_multifactor.cli.delivery check
```

交付检查在无数据环境下核对三件事：仓库内 Markdown 链接是否都指向可交付文件；`README.md`、研究报告和结果字典中的关键数字是否仍是 `docs/results/v1.0-key-results.json` 所记录机器结果的精确渲染；v1.0 manifest 的交付文件哈希是否仍与其发布提交一致。加上 `--verify-sources` 会在本地进一步把记录中的 268 项数值重新读回 `processed/` 中的权威 release（缺少本地数据时该项明确跳过）。

如需查看单个公开接口的命令边界：

```bash
.venv/bin/python -m ashare_multifactor.cli.factors --help
.venv/bin/python -m ashare_multifactor.cli.combinations --help
.venv/bin/python -m ashare_multifactor.cli.formal_backtest --help
.venv/bin/python -m ashare_multifactor.cli.validation --help
.venv/bin/python -m ashare_multifactor.cli.robustness --help
```

## 3. 本地数据布局

原始数据视为只读，不随 Git 仓库分发：

```text
Data/每天一个文件/不复权/
Data/每天一个文件/后复权/
```

- 不复权数据用于成交状态、股票池和原始市值等字段；
- 后复权数据用于跨期收益与影子核验；
- 二者必须按 `(date, symbol)` 严格一对一连接；
- `symbol` 必须保留为六位字符串；
- 不得将 `Data/`、`processed/`、`artifacts/` 或公告缓存加入 Git。

完整字段、schema 变化和来源限制见[data/README.md](data/README.md)。首次全量处理约 28GB 数据前，应先在小日期范围完成字段和连接验证。

## 4. 从原始数据重建研究期

### 单因子研究

```bash
.venv/bin/ashare-factors --config configs/research_protocol.yaml --stage build
.venv/bin/ashare-factors --config configs/research_protocol.yaml --stage all
```

`build` 构建 2003–2016 隔离面板；`all` 运行字段审计、因子、评价和因子卡片。该步骤不应读取 2017 年及以后数据。

### 因子合成与目标权重

```bash
.venv/bin/python -m ashare_multifactor.cli.combinations \
  --config configs/research_protocol.yaml
```

该步骤只消费 `processed/factor_research/`，不直接读取原始 CSV；滚动 IC 权重严格滞后一期。

### 正式执行回测

```bash
.venv/bin/python -m ashare_multifactor.cli.formal_backtest audit --root .
.venv/bin/python -m ashare_multifactor.cli.formal_backtest all --root . --publish
```

权威结果只能从 `processed/formal_backtest/CURRENT.json` 解析到不可变 release。完整成本、仅显性费用和零成本三套账本必须分别通过逐日对账。

## 5. 重建验证与稳健性

验证期重跑会读取 2017–2021，但不应读取 2022–2025：

```bash
.venv/bin/python -m ashare_multifactor.cli.validation reproduce \
  --root . \
  --run-id v1-validation-repro
```

稳健性重跑依赖冻结的验证 release、官方证据索引与协议文件：

```bash
.venv/bin/python -m ashare_multifactor.cli.robustness reproduce \
  --root . \
  --run-id v1-robustness-repro
```

这两项属于大规模本地重跑。v1.0 的日常核验优先使用下一节的只读 release 回读，不应为了查看报告而重新发布 `CURRENT.json`。

### 基准对比、多空两条腿与因子统计口径（描述性补充）

报告第 3.1–3.3 节、第 7、8 节的数字，以及报告中由补充步骤生成的图表，都来自一个只读的补充步骤：

```bash
.venv/bin/python -m ashare_multifactor.cli.supplements
```

它只读取 Stage 4 产物与 Stage 5–7 的权威 release，并通过第二阶段构建器把 2017–2021 日面板重建到隔离目录 `processed/v1_supplements/build/`（不写入 `processed/validation_evaluation/daily_panel`）。重建结果与 Stage 7 执行面板中的全部收盘价逐一比较，必须完全相等；策略年化收益、每个已发布的月度 Rank IC 序列、Newey–West t 值与 BH q 值都必须精确复现，否则整步失败。输出写入 `artifacts/v1_supplements/`，其 `manifest.json` 记录全部输入与输出的 SHA-256 和代码提交。该步骤不修改任何 release、目标权重或选择结果，也不读取 2022 年及以后的数据；本机约 30 秒。

注意：重建的 parquet 与 Stage 7 当时被清理掉的日面板内容一致，但文件字节会因写入库版本不同而不同，因此不能用 Stage 7 血缘中的旧文件哈希核对，只能做上述内容级核对。

## 6. 只读回读权威 release

在保留本地 `processed/` 的仓库根目录执行：

```bash
PYTHONPATH=src .venv/bin/python - <<'PY'
from pathlib import Path
from ashare_multifactor.audit.publication import resolve_current

for name in ("validation_evaluation", "robustness"):
    release = resolve_current(Path("processed") / name)
    print(name, release.run_id, release.manifest_sha256)

assert not Path("processed/final_test/CURRENT.json").exists()
PY
```

v1.0 预期值：

| release | run id | manifest SHA-256 |
|---|---|---|
| Stage 7 | `65b19e1_stage7_validation_controlled_collector_successor` | `6ee12a10b25a53d8a48c596faa76c1833d92c10c511e570d301a5f96c10836fd` |
| Stage 8 | `a1076c2_stage8_robustness_szse_statistics_successor` | `c75948b4c47007c6f5690451afbebdff02eb5d3c793e6285eb799af898ea1fba` |

`resolve_current()` 会逐文件核对 manifest；只比较 `CURRENT.json` 文本不足以证明 release 完整。

## 7. 最终测试封存

v1.0 不运行任何 `ashare-final-test` 命令，不创建 token 或 attempt，不导入最终测试数据，不生成策略结果，也不手工创建 `processed/final_test/CURRENT.json`。Stage 9–10 的未来恢复条件见[延期归档](stage9-10-archive.md)。

延期最终测试的代码（包括原 `ashare-final-test` 命令和 `ashare-robustness rehearse-evidence` 证据演练）已于 2026-10-05 移出 main，完整保存在 Git 标签 `archive/stage9-final-test`。main 上没有任何模块导入或引用它，该边界由 `tests/test_v1_final_test_boundary.py` 固定；该测试同时核对冻结身份清单中的每个路径都能从 main 或存档提交中取回。恢复步骤与开启前提见[延期归档](stage9-10-archive.md)。

## 8. 结果核对顺序

三个层次必须分开执行，也不要互相替代：

1. **无数据层**：运行合成测试、Ruff 与 `ashare-delivery check`；不需要 `Data/` 或 `processed/`。
2. **本地只读层**：回读 Stage 4 的 artifact manifest 与 Stage 5–8 的四个 `CURRENT.json`，确认它们解析到的 run id 和 manifest 哈希与 v1.0 manifest 的 `source_releases` 一致，并确认最终测试 `CURRENT.json` 仍不存在。
3. **完整重建层**：按第 4–5 节重跑研究、验证与稳健性；这会生成新的运行身份和证书，不改写历史 release。

报告数字的核对以[结果记录](../docs/results/v1.0-key-results.json)为机器可读桥梁：

```bash
# 本地层：记录中的数值重新读回权威 release，并核对 artifact 字节
.venv/bin/python -m ashare_multifactor.cli.delivery check --verify-sources

# 本地层：研究结果变化后重新生成记录（需要 processed/ 中的 release）
.venv/bin/python -m ashare_multifactor.cli.delivery record-results
```

`record-results` 会把`README.md`、研究报告与结果字典中被引用的数字重新绑定到 machine-readable 结果；`check` 随后验证这些数字没有被改成别的数值。若报告中的某个关键数字被改动、删除或换成不同精度下不等值的写法，`check` 会把对应文档和字段列为失败项。字段级含义见[结果字典](result_dictionary.md)，发布的交付哈希见[v1.0 manifest](../releases/v1.0.0-research-validation.json)。
