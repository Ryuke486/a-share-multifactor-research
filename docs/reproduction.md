# v1.0 复现说明

## 1. 环境

- Python 3.12 或更高版本；权威 Stage 7/8 release 使用 Python 3.14.6。
- 依赖版本由各 release 的 `lineage.json` 记录。
- 建议在仓库根目录使用项目级 `.venv`。

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[dev]"
```

## 2. 无真实数据快速验证

以下命令只使用人工合成测试夹具，不读取 `Data/`、验证期 release 或最终测试期：

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests
```

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

## 8. 结果核对顺序

1. 运行合成测试与 Ruff；
2. 回读 Stage 4–8 的 machine-readable manifest；
3. 核对 `releases/v1.0.0-research-validation.json` 中的源码基线、release 哈希和文档哈希；
4. 确认最终测试 `CURRENT.json` 不存在；
5. 对照[结果字典](result_dictionary.md)检查报告中的数字来源。
