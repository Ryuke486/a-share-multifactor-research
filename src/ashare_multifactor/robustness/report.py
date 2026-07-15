from __future__ import annotations

from typing import Any

import polars as pl

from ashare_multifactor.robustness.protocol import RobustnessProtocol


def render_robustness_report(
    table: pl.DataFrame,
    gate: dict[str, Any],
    protocol: RobustnessProtocol,
) -> str:
    counts = {
        label: table.filter(pl.col("interpretation") == label)
        .get_column("experiment_id")
        .n_unique()
        for label in ("stable", "sensitive", "failed")
    }
    lines = [
        "# 板块8：稳健性分析与最终测试协议",
        "",
        "## 研究边界",
        "",
        f"- 主方案：`{protocol.main_candidate}`（沿用板块7，不重新选择）",
        "- 使用区间：2005-01-01至2021-12-31",
        "- 2022–2025：保持封存、零读取",
        f"- 测试协议门禁：`{gate['status']}`",
        "",
        "## 汇总",
        "",
        f"- 稳定实验：{counts['stable']}",
        f"- 敏感实验：{counts['sensitive']}",
        f"- 失败或未运行实验：{counts['failed']}",
        "",
        "## 敏感结果",
        "",
    ]
    sensitive = table.filter(pl.col("interpretation") == "sensitive")
    if sensitive.is_empty():
        lines.append("- 无")
    else:
        for row in sensitive.iter_rows(named=True):
            lines.append(
                f"- `{row['experiment_id']}` / `{row['metric']}`: "
                f"{row['value']:.6f}，相对基线 {row['baseline_delta']:+.6f}"
            )
    lines.extend(["", "## 失败或未运行", ""])
    failed = table.filter(pl.col("interpretation") == "failed").unique(
        subset=["experiment_id", "status", "reason"], maintain_order=True
    )
    if failed.is_empty():
        lines.append("- 无")
    else:
        for row in failed.iter_rows(named=True):
            lines.append(
                f"- `{row['experiment_id']}`：`{row['status']}`；"
                f"{row['reason'] or '未提供原因'}"
            )
    for event in gate.get("fatal_events", []):
        lines.append(f"- 致命缺陷：{event.get('reason', event.get('status'))}")
    lines.extend(
        [
            "",
            "## 解释限制",
            "",
            "稳健性变体只用于诊断，不用于按收益重新挑选主方案。历史行业口径未完成point-in-time核验，行业结果仅作限制性描述。",
            "",
        ]
    )
    return "\n".join(lines)
