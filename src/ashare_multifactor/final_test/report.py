from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
from pathlib import Path

import polars as pl


_REQUIRED_HEADINGS = (
    "## 1. 冻结身份",
    "## 2. 一次性测试结果",
    "## 3. 预注册稳健性对照",
    "## 4. 失败运行与异常披露",
    "## 5. 结论与限制",
)
_LABELS = {
    "rank_ic": "Rank IC",
    "group_spread": "Q5−Q1分组收益差",
    "group_monotonicity": "分组单调性",
    "annual_return": "年化收益",
    "annual_volatility": "年化波动率",
    "sharpe_zero_rate": "夏普比率（无风险利率0）",
    "maximum_drawdown": "最大回撤",
    "turnover": "平均单边换手",
    "cost_erosion": "成本侵蚀",
    "target_deviation": "平均目标偏离",
    "unfilled_rate": "未成交率",
}
_PERCENT_METRICS = {
    "group_spread",
    "annual_return",
    "annual_volatility",
    "maximum_drawdown",
    "turnover",
    "cost_erosion",
    "target_deviation",
    "unfilled_rate",
}


def render_final_test_report(
    template_path: Path,
    *,
    expected_template_sha256: str,
    comparison: pl.DataFrame,
    identity: Mapping[str, object],
    failed_runs: Sequence[Mapping[str, object]],
) -> str:
    """Render numbers only after the pre-number template identity is verified."""
    payload = template_path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != expected_template_sha256:
        raise ValueError("frozen report template hash mismatch")
    template = payload.decode("utf-8")
    if any(heading not in template for heading in _REQUIRED_HEADINGS):
        raise ValueError("frozen report template structure is incomplete")
    _validate_comparison(comparison)

    lines = [
        template.splitlines()[0],
        "",
        _REQUIRED_HEADINGS[0],
        "",
        f"- sealed protocol SHA-256: `{identity.get('sealed_protocol_sha256', '')}`",
        f"- opening approval ID: `{identity.get('approval_id', '')}`",
        f"- authoritative run ID: `{identity.get('attempt_id', '')}`",
        f"- code commit: `{identity.get('git_commit', '')}`",
        f"- upstream robustness release: `{identity.get('robustness_release', '')}`",
        "",
        _REQUIRED_HEADINGS[1],
        "",
        "| 范围 | 对象 | 指标 | 2005–2016研究期 | 2017–2021验证期 | 2022–2025最终测试期 | 机器结果键 |",
        "|---|---|---|---:|---:|---:|---|",
    ]
    for row in comparison.iter_rows(named=True):
        machine_key = f"{row['scope']}/{row['entity']}/{row['metric']}"
        lines.append(
            "| {scope} | {entity} | {label} | {research} | {validation} | "
            "{test} | `machine_result:{key}` |".format(
                scope=row["scope"],
                entity=row["entity"],
                label=_LABELS[row["metric"]],
                research=_format_value(row["metric"], row["research"]),
                validation=_format_value(row["metric"], row["validation"]),
                test=_format_value(row["metric"], row["test"]),
                key=machine_key,
            )
        )
    lines.extend(
        [
            "",
            _REQUIRED_HEADINGS[2],
            "",
            "仅并列封存时点和指标，不合并期间重新选模，不新增参数。",
            "",
            _REQUIRED_HEADINGS[3],
            "",
        ]
    )
    if failed_runs:
        for failure in failed_runs:
            lines.append(
                f"- `{failure.get('attempt_id', '')}`: "
                f"`{failure.get('status', '')}`；{failure.get('reason', '')}"
            )
    else:
        lines.append("- 无（`machine_result:failed_runs/count=0`）")
    lines.extend(
        [
            "",
            _REQUIRED_HEADINGS[4],
            "",
            "三个固定期间的证据已分列报告；不得根据最终测试结果返回修改模型。",
            "",
        ]
    )
    return "\n".join(lines)


def _validate_comparison(comparison: pl.DataFrame) -> None:
    required = {"scope", "entity", "metric", "research", "validation", "test"}
    if comparison.is_empty() or not required.issubset(comparison.columns):
        raise ValueError("machine-readable period comparison is invalid")
    unknown = set(comparison.get_column("metric")) - set(_LABELS)
    if unknown:
        raise ValueError("report contains a metric outside the frozen vocabulary")
    if comparison.select(
        pl.struct("scope", "entity", "metric").is_duplicated().any()
    ).item():
        raise ValueError("report comparison contains duplicate machine result keys")


def _format_value(metric: str, value: float) -> str:
    if metric in _PERCENT_METRICS:
        return f"{value:.2%}"
    return f"{value:.4f}"
