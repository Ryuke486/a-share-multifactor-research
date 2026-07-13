# A股数据底座 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 把不复权与后复权日CSV安全地配对、规范化、检查并转换为按年份分区的统一Parquet数据集。

**Architecture:** 原始CSV保持只读。发现层只处理文件路径，读取层只处理单日规范化，验证层返回结构化问题，构建层按年份写Parquet和manifest。研究代码不得绕过此层直接读取Data目录。

**Tech Stack:** Python 3.12+、Polars、pytest、JSON、Parquet。

## Dependencies

- 先完成 2026-07-13-01-research-protocol.md。
- 使用其中定义的ResearchConfig、Data忽略规则和2012–2015小闭环日期。

## Global Constraints

- 只读取Data/每天一个文件/不复权和Data/每天一个文件/后复权。
- 证券代码始终为六位字符串。
- 退市时间不参与历史股票池过滤。
- 37列与38列schema变化必须显式处理。
- 严重质量错误阻止Parquet写入；warning写入质量报告。
- 输出按年份分区，每年写完释放内存。
- manifest最大日期不得超过2015-12-31。

## Files Covered

~~~text
src/ashare_multifactor/data/__init__.py
src/ashare_multifactor/data/schema.py
src/ashare_multifactor/data/discovery.py
src/ashare_multifactor/data/reader.py
src/ashare_multifactor/data/validation.py
src/ashare_multifactor/data/build.py
tests/test_discovery.py
tests/test_reader.py
tests/test_validation.py
tests/test_build.py
processed/daily_panel/          # Git ignored
~~~

## Stable Output Contract

统一面板主键为(date, symbol)，同时包含raw成交口径和backward-adjusted总收益口径OHLC。构建输出必须包含year=YYYY/part-000.parquet、manifest.json和quality_issues.json。

---

### Task 1: 规范字段与发现成对日文件

**Files:**
- Create: src/ashare_multifactor/data/__init__.py
- Create: src/ashare_multifactor/data/schema.py
- Create: src/ashare_multifactor/data/discovery.py
- Create: tests/test_discovery.py

**Interfaces:**
- Consumes: 两个原始日文件根目录和日期范围。
- Produces: 按日期排序的DailyFilePair；每个日期必须同时有不复权和后复权文件。

- [x] **Step 1: 写文件发现测试**

~~~python
# tests/test_discovery.py
from datetime import date
from pathlib import Path

import pytest

from ashare_multifactor.data.discovery import discover_daily_pairs


def test_discovery_requires_matching_dates(tmp_path: Path):
    raw = tmp_path / "raw"
    adj = tmp_path / "adj"
    raw.mkdir()
    adj.mkdir()
    (raw / "2014-01-02_金玥数据.csv").touch()

    with pytest.raises(ValueError, match="unpaired trading dates"):
        discover_daily_pairs(raw, adj, date(2014, 1, 1), date(2014, 12, 31))


def test_discovery_sorts_pairs_by_date(tmp_path: Path):
    raw = tmp_path / "raw"
    adj = tmp_path / "adj"
    raw.mkdir()
    adj.mkdir()
    for day in ("2014-01-03", "2014-01-02"):
        (raw / f"{day}_金玥数据.csv").touch()
        (adj / f"{day}_金玥数据.csv").touch()

    pairs = discover_daily_pairs(raw, adj, date(2014, 1, 1), date(2014, 12, 31))
    assert [pair.trading_date.isoformat() for pair in pairs] == ["2014-01-02", "2014-01-03"]
~~~

- [x] **Step 2: 定义规范字段**

schema.py必须定义：

~~~python
CANONICAL_COLUMNS = (
    "date",
    "symbol",
    "name",
    "industry",
    "open_raw",
    "high_raw",
    "low_raw",
    "close_raw",
    "prev_close_raw",
    "open_adj",
    "high_adj",
    "low_adj",
    "close_adj",
    "prev_close_adj",
    "volume",
    "amount",
    "turnover",
    "is_st",
    "is_limit_up",
    "total_shares",
    "float_shares",
    "total_market_cap",
    "float_market_cap",
    "pe_ttm",
    "pb",
    "ps_ttm",
    "list_date",
    "delist_date",
    "is_margin",
)

SCHEMA_VERSION = "1.0.0"
~~~

- [x] **Step 3: 实现文件配对接口**

discovery.py必须包含：

~~~python
from dataclasses import dataclass
from datetime import date
from pathlib import Path
import re

DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_金玥数据\.csv$")


@dataclass(frozen=True)
class DailyFilePair:
    trading_date: date
    unadjusted: Path
    backward_adjusted: Path


def _index_files(root: Path, start: date, end: date) -> dict[date, Path]:
    indexed = {}
    for path in root.rglob("*.csv"):
        match = DATE_RE.match(path.name)
        if match is None:
            continue
        trading_date = date.fromisoformat(match.group(1))
        if start <= trading_date <= end:
            if trading_date in indexed:
                raise ValueError(f"duplicate file for {trading_date}")
            indexed[trading_date] = path
    return indexed


def discover_daily_pairs(
    unadjusted_root: Path,
    backward_adjusted_root: Path,
    start: date,
    end: date,
) -> list[DailyFilePair]:
    raw = _index_files(unadjusted_root, start, end)
    adj = _index_files(backward_adjusted_root, start, end)
    missing = sorted(set(raw) ^ set(adj))
    if missing:
        raise ValueError(f"unpaired trading dates: {missing[:10]}")
    return [
        DailyFilePair(day, raw[day], adj[day])
        for day in sorted(raw)
    ]
~~~

- [x] **Step 4: 在真实2014年目录上做只读发现检查**

Run:

~~~bash
.venv/bin/pytest tests/test_discovery.py -v
.venv/bin/python -c "from datetime import date; from pathlib import Path; from ashare_multifactor.data.discovery import discover_daily_pairs; print(len(discover_daily_pairs(Path('Data/每天一个文件/不复权'), Path('Data/每天一个文件/后复权'), date(2014,1,1), date(2014,12,31))))"
~~~

Expected: 测试通过；真实文件数为245。

- [x] **Step 5: 提交字段和发现模块**

~~~bash
git add src/ashare_multifactor/data tests/test_discovery.py
git commit -m "feat: discover paired daily market files"
~~~

**Task 3 Acceptance Checklist**

- [x] 同一天的两种复权文件总是成对。
- [x] 文件列表按交易日稳定排序。
- [x] 2014年发现245对文件。
- [x] 发现逻辑不会读取CSV内容。

---

### Task 2: 读取、规范化并连接单日数据

**Files:**
- Create: src/ashare_multifactor/data/reader.py
- Create: tests/test_reader.py

**Interfaces:**
- Consumes: DailyFilePair。
- Produces: 只包含CANONICAL_COLUMNS的单日Polars DataFrame。

- [x] **Step 1: 创建两个最小CSV夹具测试**

test_reader.py必须覆盖：

1. 代码000001保持六位字符串。
2. “是/否”转换为布尔值。
3. 退市时间“-”转换为空值。
4. 不复权文件少“是否融资融券”时，is_margin为空而不是报错。
5. 不复权与后复权按(date, symbol)一对一连接。
6. 两边代码集合不一致时抛出ValueError。

核心断言：

~~~python
frame = read_daily_pair(pair)
row = frame.row(0, named=True)
assert row["symbol"] == "000001"
assert row["is_st"] is False
assert row["delist_date"] is None
assert frame.columns == list(CANONICAL_COLUMNS)
~~~

- [x] **Step 2: 实现不复权读取规则**

reader.py使用pl.read_csv，并明确：

~~~python
RAW_RENAME = {
    "日期": "date",
    "代码": "symbol",
    "名称": "name",
    "所属行业": "industry",
    "开盘价": "open_raw",
    "最高价": "high_raw",
    "最低价": "low_raw",
    "收盘价": "close_raw",
    "前收盘价": "prev_close_raw",
    "成交量（股）": "volume",
    "成交额（元）": "amount",
    "换手率": "turnover",
    "是否ST": "is_st",
    "是否涨停": "is_limit_up",
    "总股本（股）": "total_shares",
    "流通股本（股）": "float_shares",
    "总市值（元）": "total_market_cap",
    "流通市值（元）": "float_market_cap",
    "滚动市盈率": "pe_ttm",
    "市净率": "pb",
    "滚动市销率": "ps_ttm",
    "上市时间": "list_date",
    "退市时间": "delist_date",
    "是否融资融券": "is_margin",
}
~~~

读取代码必须给“代码”指定String类型；连接前拒绝空值、非纯数字或超过6位的symbol，
允许1至6位数字并执行str.zfill(6)；缺少“是否融资融券”时添加空布尔列；日期列转pl.Date。

- [x] **Step 3: 实现后复权读取规则**

后复权只保留并重命名：

~~~python
ADJ_RENAME = {
    "日期": "date",
    "代码": "symbol",
    "开盘价": "open_adj",
    "最高价": "high_adj",
    "最低价": "low_adj",
    "收盘价": "close_adj",
    "前收盘价": "prev_close_adj",
}
~~~

- [x] **Step 4: 实现严格一对一连接**

read_daily_pair必须在连接前比较两侧symbol集合，随后执行1:1连接，并防御性确认
连接行数与两侧输入行数一致：

~~~python
joined = raw.join(adj, on=["date", "symbol"], how="inner", validate="1:1")
return joined.select(CANONICAL_COLUMNS).sort(["date", "symbol"])
~~~

- [x] **Step 5: 运行单元测试和真实单日抽查**

Run:

~~~bash
.venv/bin/pytest tests/test_reader.py -v
.venv/bin/python -c "from datetime import date; from pathlib import Path; from ashare_multifactor.data.discovery import discover_daily_pairs; from ashare_multifactor.data.reader import read_daily_pair; p=discover_daily_pairs(Path('Data/每天一个文件/不复权'),Path('Data/每天一个文件/后复权'),date(2014,1,2),date(2014,1,2))[0]; f=read_daily_pair(p); print(f.shape, f.select('symbol').head(1))"
~~~

Expected: 测试通过；真实单日行数大于1000；首个代码保留前导零。

- [x] **Step 6: 提交读取模块**

~~~bash
git add src/ashare_multifactor/data/reader.py tests/test_reader.py
git commit -m "feat: normalize and join daily market data"
~~~

**Task 4 Acceptance Checklist**

- [x] 37列和38列原始文件都能读取。
- [x] symbol始终为六位字符串。
- [x] 连接后没有丢失证券。
- [x] 输出列名、顺序和类型稳定。

---

### Task 3: 数据质量检查与机器可读报告

**Files:**
- Create: src/ashare_multifactor/data/validation.py
- Create: tests/test_validation.py

**Interfaces:**
- Consumes: 规范单日或多日面板。
- Produces: QualityIssue列表；严重错误阻止Parquet写入。

- [x] **Step 1: 写质量问题测试**

测试必须构造并识别：

- 重复(date, symbol)；
- open/high/low/close非正；
- high低于open或close；
- low高于open或close；
- volume或amount为负；
- volume或amount缺失（包括非法文本解析为空）；
- raw与adj的OHLC价格缺失；
- raw或adj前收盘价缺失（warning，保留空值）；
- date与文件日期不一致。

~~~python
issues = validate_daily_panel(frame, expected_date=date(2014, 1, 2))
codes = {issue.code for issue in issues}
assert "duplicate_key" in codes
assert "invalid_ohlc" in codes
~~~

- [x] **Step 2: 定义问题结构**

~~~python
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class QualityIssue:
    severity: str
    code: str
    count: int
    message: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)
~~~

- [x] **Step 3: 实现逐项检查**

validate_daily_panel(frame, expected_date)必须返回稳定排序的问题列表，严重级别规则为：

- error：重复主键、日期错误、无效OHLC、raw/adj的OHLC缺失、volume/amount缺失；
- warning：前收盘价、行业、估值、两融缺失，成交量额为零。前收盘价缺失使用 `missing_prev_close`，不填补原值。

2026-07-13经用户批准调整：真实 smoke 构建在 2015-09-14、symbol 832317 发现
`prev_close_raw` 和 `prev_close_adj` 均缺失。这两个字段不是当日 OHLC，因此从泛化的
`missing_price` error 拆分为独立 warning；
当日 raw/adj OHLC 任一缺失仍为 error。此变更不使用 `list_date` 或任何未来信息。

- [x] **Step 4: 增加阻断函数**

~~~python
def raise_on_errors(issues: list[QualityIssue]) -> None:
    errors = [issue for issue in issues if issue.severity == "error"]
    if errors:
        detail = "; ".join(f"{issue.code}:{issue.count}" for issue in errors)
        raise ValueError(f"data quality errors: {detail}")
~~~

- [x] **Step 5: 运行测试**

Run:

~~~bash
.venv/bin/pytest tests/test_validation.py -v
~~~

Expected: 所有人工错误均被识别，干净夹具返回零个error。

- [x] **Step 6: 提交质量模块**

~~~bash
git add src/ashare_multifactor/data/validation.py tests/test_validation.py
git commit -m "feat: validate canonical daily panel"
~~~

**Task 5 Acceptance Checklist**

- [x] 严重质量问题会阻止构建。
- [x] warning不会静默消失。
- [x] 报告包含问题代码、数量和解释。
- [x] 所有检查均有人工构造的反例测试。

---

### Task 4: 按年份构建Parquet与清单

**Files:**
- Create: src/ashare_multifactor/data/build.py
- Create: tests/test_build.py

**Interfaces:**
- Consumes: ResearchConfig、日期范围、DailyFilePair列表。
- Produces: processed/daily_panel/year=YYYY/part-000.parquet、manifest.json、quality_issues.json。

- [x] **Step 1: 写两日构建测试**

测试使用tmp_path中的两对CSV，运行build_parquet_dataset后断言：

~~~python
manifest = build_parquet_dataset(config, date(2014, 1, 2), date(2014, 1, 3))
assert manifest.file_pairs == 2
assert manifest.min_date == date(2014, 1, 2)
assert manifest.max_date == date(2014, 1, 3)
assert (config.paths.processed / "daily_panel/year=2014/part-000.parquet").exists()
~~~

- [x] **Step 2: 定义构建清单**

~~~python
@dataclass(frozen=True)
class BuildManifest:
    schema_version: str
    file_pairs: int
    rows: int
    min_date: date
    max_date: date
    years: tuple[int, ...]
~~~

- [x] **Step 3: 实现按年份写入**

构建逻辑必须：

1. 发现日期对；
2. 逐日读取和验证；
3. 同一年内累积DataFrame；
4. 年份结束时concat并按(date, symbol)排序；
5. 写入临时文件 part-000.parquet.tmp；
6. 写成功后原子重命名为part-000.parquet；
7. 写manifest.json和quality_issues.json。

每个年份写完即释放内存，不跨年份累积。

- [x] **Step 4: 提供命令行入口**

build.py必须支持：

~~~bash
python -m ashare_multifactor.data.build --config configs/research_protocol.yaml --mode smoke
~~~

mode=smoke只能读取smoke_data日期；不得提供会隐式读取test日期的默认值。

- [x] **Step 5: 运行单元测试**

~~~bash
.venv/bin/pytest tests/test_build.py -v
~~~

- [x] **Step 6: 构建真实小闭环Parquet**

Run:

~~~bash
.venv/bin/python -m ashare_multifactor.data.build --config configs/research_protocol.yaml --mode smoke
~~~

Expected:

- processed/daily_panel/year=2012至year=2015四个分区存在；
- manifest最大日期不超过2015-12-31；
- manifest不包含2022或之后日期；
- quality_issues.json记录字段缺失warning但没有未解释error。

- [x] **Step 7: 提交构建模块，不提交生成数据**

~~~bash
git add src/ashare_multifactor/data/build.py tests/test_build.py
git commit -m "feat: build audited yearly parquet dataset"
~~~

**Task 6 Acceptance Checklist**

- [x] 每个年份单独写入，内存使用有界。
- [x] 失败不会留下看似完整的正式Parquet。
- [x] manifest能证明小闭环没有读取最终测试期。
- [x] processed目录保持Git忽略。

---

## Data Foundation Acceptance Checklist

- [x] 2014年发现245对不复权/后复权日文件。
- [x] 每日文件严格按(date, symbol)一对一连接。
- [x] symbol保留六位字符串。
- [x] 37/38列文件均可读取。
- [x] OHLC、重复主键、成交量额缺失/负值和非法symbol有自动测试。
- [x] 2012–2015四个年份分区生成成功。
- [x] manifest和quality_issues.json存在且可读。
- [x] manifest最大日期不超过2015-12-31。
- [x] Data与processed均未进入Git。

## Estimated Effort

3至4个专注工作日；完成后进入小闭环计划。
