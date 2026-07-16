from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
from pathlib import Path

import polars as pl


_PLACEHOLDERS = ("{{identity}}", "{{comparison_table}}", "{{failed_runs}}")
_IDENTITY_FIELDS = (
    ("sealed_protocol_sha256", "sealed protocol SHA-256"),
    ("approval_id", "opening approval ID"),
    ("attempt_id", "authoritative run ID"),
    ("git_commit", "code commit"),
    ("robustness_release", "upstream robustness release"),
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
    if any(template.count(placeholder) != 1 for placeholder in _PLACEHOLDERS):
        raise ValueError("frozen report template placeholders are incomplete")
    _validate_comparison(comparison)
    missing_identity = [
        key for key, _label in _IDENTITY_FIELDS if not str(identity.get(key, "")).strip()
    ]
    if missing_identity:
        raise ValueError("critical report identity is empty: " + ", ".join(missing_identity))
    identity_lines = [
        f"- {label}: `{identity[key]}` (`machine_identity:{key}`)"
        for key, label in _IDENTITY_FIELDS
    ]
    table_lines = [
        "| 范围 | 对象 | 指标 | 2005–2016研究期 | 2017–2021验证期 | 2022–2025最终测试期 | 机器结果键 |",
        "|---|---|---|---:|---:|---:|---|",
    ]
    for row in comparison.iter_rows(named=True):
        machine_key = f"{row['scope']}/{row['entity']}/{row['metric']}"
        table_lines.append(
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
    failure_lines = []
    if failed_runs:
        for failure in failed_runs:
            attempt_id = str(failure.get("attempt_id", "")).strip()
            status = str(failure.get("status", "")).strip()
            reason = str(failure.get("reason", "")).strip()
            if not attempt_id or not status or not reason:
                raise ValueError("failed run record is missing a machine-readable field")
            failure_lines.append(
                f"- `{failure.get('attempt_id', '')}`: "
                f"`{failure.get('status', '')}`；{failure.get('reason', '')} "
                f"(`machine_failure:{attempt_id}`)"
            )
    else:
        failure_lines.append("- 无（`machine_failure:count=0`）")
    report = (
        template.replace("{{identity}}", "\n".join(identity_lines))
        .replace("{{comparison_table}}", "\n".join(table_lines))
        .replace("{{failed_runs}}", "\n".join(failure_lines))
    )
    if "{{" in report or "}}" in report:
        raise ValueError("frozen report contains an unfilled placeholder")
    return report


def _validate_comparison(comparison: pl.DataFrame) -> None:
    required = {"scope", "entity", "metric", "research", "validation", "test"}
    if comparison.is_empty() or not required.issubset(comparison.columns):
        raise ValueError("machine-readable period comparison is invalid")
    unknown = set(comparison.get_column("metric")) - set(_LABELS)
    if unknown:
        raise ValueError("report contains a metric outside the frozen vocabulary")
    if set(comparison.get_column("metric")) != set(_LABELS):
        raise ValueError("report requires every sealed metric")
    if comparison.select(
        pl.struct("scope", "entity", "metric").is_duplicated().any()
    ).item():
        raise ValueError("report comparison contains duplicate machine result keys")


def _format_value(metric: str, value: float) -> str:
    if metric in _PERCENT_METRICS:
        return f"{value:.2%}"
    return f"{value:.4f}"
