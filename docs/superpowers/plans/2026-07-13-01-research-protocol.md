# A股研究协议 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 建立安全的Python/Git项目边界，冻结研究、验证、最终测试和小闭环日期，并让代码自动拒绝数据泄漏配置。

**Architecture:** 本计划先保护28GB原始Data目录，再建立唯一的YAML研究配置和不可变ResearchConfig。后续数据底座与小闭环只能消费该配置，不得在代码中重复硬编码日期和参数。

**Tech Stack:** Python 3.12+（当前机器为3.14.6）、PyYAML、pytest、ruff、Git。

## Global Constraints

- Data目录只读且必须在第一次Git操作前忽略。
- 研究期：2005-01-01至2016-12-31。
- 验证期：2017-01-01至2021-12-31。
- 最终测试期：2022-01-01至2025-12-31，保持封存。
- 小闭环数据期：2012-01-01至2015-12-31；分析期：2014-01-01至2015-12-31。
- 配置代码必须拒绝任何与最终测试期重叠的小闭环区间。
- 所有研究日期和MVP参数只允许在configs/research_protocol.yaml中定义一次。

## Files Covered

~~~text
.gitignore
pyproject.toml
README.md
configs/research_protocol.yaml
docs/research_protocol.md
src/ashare_multifactor/__init__.py
src/ashare_multifactor/config.py
tests/test_package.py
tests/test_config.py
~~~

## Exit Deliverable

一套可安装的Python项目、受保护的Git边界、经过自动验证的研究配置，以及可供后两份计划复用的load_config(path) -> ResearchConfig接口。

---

### Task 1: Git与Python项目护栏

**Files:**
- Create: .gitignore
- Create: pyproject.toml
- Create: README.md
- Create: src/ashare_multifactor/__init__.py
- Create: tests/test_package.py

**Interfaces:**
- Consumes: 当前空项目目录和只读Data目录。
- Produces: 可安装Python包、pytest入口和不会误提交原始数据的Git边界。

- [x] **Step 1: 先写包导入测试**

~~~python
# tests/test_package.py
def test_package_imports():
    import ashare_multifactor

    assert ashare_multifactor.__version__ == "0.1.0"
~~~

- [x] **Step 2: 创建 .gitignore，必须包含以下内容**

~~~gitignore
.DS_Store
.venv/
__pycache__/
.pytest_cache/
.ruff_cache/
*.py[cod]
Data/
data/raw/
processed/
artifacts/
~~~

- [x] **Step 3: 创建 pyproject.toml**

~~~toml
[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[project]
name = "ashare-multifactor"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "matplotlib>=3.9",
  "numpy>=2.0",
  "polars>=1.30",
  "pyyaml>=6.0",
  "scipy>=1.13",
]

[project.optional-dependencies]
dev = [
  "pytest>=8.3",
  "ruff>=0.9",
]

[project.scripts]
ashare-mvp = "ashare_multifactor.research.mvp:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py312"
~~~

- [x] **Step 4: 创建包版本**

~~~python
# src/ashare_multifactor/__init__.py
__version__ = "0.1.0"
~~~

- [x] **Step 5: 创建环境并验证包**

Run:

~~~bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/pytest tests/test_package.py -v
~~~

Expected: 1 passed。

- [x] **Step 6: 初始化Git并验证原始数据被忽略**

Run:

~~~bash
git init
git check-ignore Data/每天一个文件/不复权/2000至2025年/2014/2014-01-02_金玥数据.csv
git status --short
~~~

Expected: check-ignore输出该Data路径；git status不列出Data内部文件。

- [x] **Step 7: 提交项目护栏**

~~~bash
git add .gitignore pyproject.toml README.md src/ashare_multifactor/__init__.py tests/test_package.py
git commit -m "chore: initialize research project safely"
~~~

**Task 1 Acceptance Checklist**

- [x] Data目录未被Git追踪。
- [x] Python包可导入。
- [x] pytest和ruff可运行。
- [x] README明确说明项目处于MVP阶段。

---

### Task 2: 冻结研究协议与配置验证

**Files:**
- Create: configs/research_protocol.yaml
- Create: docs/research_protocol.md
- Create: src/ashare_multifactor/config.py
- Create: tests/test_config.py

**Interfaces:**
- Consumes: YAML配置路径。
- Produces: 不可变ResearchConfig；所有后续模块只通过该对象读取日期、路径和参数。

- [x] **Step 1: 写日期隔离失败测试**

~~~python
# tests/test_config.py
from pathlib import Path

import pytest

from ashare_multifactor.config import load_config


def test_smoke_period_must_not_overlap_final_test(tmp_path: Path):
    config = tmp_path / "bad.yaml"
    config.write_text(
        """
paths:
  raw_unadjusted: Data/每天一个文件/不复权
  raw_backward_adjusted: Data/每天一个文件/后复权
  processed: processed
  artifacts: artifacts
periods:
  research: [2005-01-01, 2016-12-31]
  validation: [2017-01-01, 2021-12-31]
  test: [2022-01-01, 2025-12-31]
  smoke_data: [2020-01-01, 2023-12-31]
  smoke_analysis: [2022-01-01, 2023-12-31]
mvp:
  universe_size: 200
  momentum_lookback: 60
  forward_horizon: 20
  portfolio_size: 20
  transaction_cost_bps: 10.0
  initial_cash: 1000000.0
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="smoke periods must not overlap test period"):
        load_config(config)
~~~

- [x] **Step 2: 创建配置对象和区间检查**

~~~python
# src/ashare_multifactor/config.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Period:
    start: date
    end: date

    def overlaps(self, other: "Period") -> bool:
        return max(self.start, other.start) <= min(self.end, other.end)

    def contains(self, other: "Period") -> bool:
        return self.start <= other.start and other.end <= self.end


@dataclass(frozen=True)
class Paths:
    raw_unadjusted: Path
    raw_backward_adjusted: Path
    processed: Path
    artifacts: Path


@dataclass(frozen=True)
class MvpSettings:
    universe_size: int
    momentum_lookback: int
    forward_horizon: int
    portfolio_size: int
    transaction_cost_bps: float
    initial_cash: float


@dataclass(frozen=True)
class ResearchConfig:
    paths: Paths
    research: Period
    validation: Period
    test: Period
    smoke_data: Period
    smoke_analysis: Period
    mvp: MvpSettings


def _period(value: list[str]) -> Period:
    if len(value) != 2:
        raise ValueError("period must contain exactly two dates")
    period = Period(date.fromisoformat(value[0]), date.fromisoformat(value[1]))
    if period.end < period.start:
        raise ValueError("period end precedes start")
    return period


def load_config(path: Path) -> ResearchConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    periods = raw["periods"]
    config = ResearchConfig(
        paths=Paths(**{key: Path(value) for key, value in raw["paths"].items()}),
        research=_period(periods["research"]),
        validation=_period(periods["validation"]),
        test=_period(periods["test"]),
        smoke_data=_period(periods["smoke_data"]),
        smoke_analysis=_period(periods["smoke_analysis"]),
        mvp=MvpSettings(**raw["mvp"]),
    )
    if config.research.overlaps(config.validation):
        raise ValueError("research and validation periods overlap")
    if config.validation.overlaps(config.test):
        raise ValueError("validation and test periods overlap")
    if config.smoke_data.overlaps(config.test) or config.smoke_analysis.overlaps(config.test):
        raise ValueError("smoke periods must not overlap test period")
    if not config.smoke_data.contains(config.smoke_analysis):
        raise ValueError("smoke data period must contain smoke analysis period")
    if config.mvp.portfolio_size > config.mvp.universe_size:
        raise ValueError("portfolio size exceeds universe size")
    return config
~~~

- [x] **Step 3: 写正式配置**

~~~yaml
# configs/research_protocol.yaml
paths:
  raw_unadjusted: Data/每天一个文件/不复权
  raw_backward_adjusted: Data/每天一个文件/后复权
  processed: processed
  artifacts: artifacts

periods:
  research: [2005-01-01, 2016-12-31]
  validation: [2017-01-01, 2021-12-31]
  test: [2022-01-01, 2025-12-31]
  smoke_data: [2012-01-01, 2015-12-31]
  smoke_analysis: [2014-01-01, 2015-12-31]

mvp:
  universe_size: 200
  momentum_lookback: 60
  forward_horizon: 20
  portfolio_size: 20
  transaction_cost_bps: 10.0
  initial_cash: 1000000.0
~~~

- [x] **Step 4: 写研究协议文档**

docs/research_protocol.md必须逐条写明：

1. 研究问题：60日动量在高流动性A股股票池中的截面预测能力。
2. 数据形成时间和信号形成时间。
3. 研究、验证、测试、小闭环预热和分析日期。
4. 股票池、因子、标签、调仓和成交规则。
5. 小闭环10bp成本与分数持仓的局限。
6. 最终测试期封存规则。
7. 任何改变协议的提交必须在读取最终测试结果之前完成。

- [x] **Step 5: 验证配置**

Run:

~~~bash
.venv/bin/pytest tests/test_config.py -v
.venv/bin/python -c "from pathlib import Path; from ashare_multifactor.config import load_config; print(load_config(Path('configs/research_protocol.yaml')).smoke_analysis)"
~~~

Expected: 测试通过；输出2014-01-01至2015-12-31。

- [x] **Step 6: 提交研究协议**

~~~bash
git add configs/research_protocol.yaml docs/research_protocol.md src/ashare_multifactor/config.py tests/test_config.py
git commit -m "docs: freeze research protocol and sample boundaries"
~~~

**Task 2 Acceptance Checklist**

- [x] 小闭环和最终测试期没有重叠。
- [x] 2012–2013仅用于预热，不进入小闭环绩效报告。
- [x] 所有日期和MVP参数只在YAML中定义一次。
- [x] 代码能拒绝越界配置。

---

## Research Protocol Acceptance Checklist

- [x] Data目录不被Git追踪。
- [x] Python包、pytest和ruff可运行。
- [x] 研究、验证、测试日期互不重叠。
- [x] 小闭环日期完全位于研究期，且不接触最终测试期。
- [x] 所有后续模块只通过ResearchConfig读取日期和参数。
- [x] docs/research_protocol.md说明信号、标签、成交、成本和协议变更规则。

## Estimated Effort

半天至一天；完成后进入数据底座计划。
