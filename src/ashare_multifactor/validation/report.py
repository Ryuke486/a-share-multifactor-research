from __future__ import annotations

from typing import Any

import polars as pl


def render_validation_report(
    metrics: pl.DataFrame,
    decision: dict[str, Any],
) -> str:
    rows = []
    for item in metrics.sort("candidate").iter_rows(named=True):
        rows.append(
            (
                "| {candidate} | {ic:.4f} | {icir:.4f} | {monotonicity:.2f} | "
                "{net:.2%} | {drawdown:.2%} | {turnover:.2%} | {cost:.2%} |"
            ).format(
                candidate=item["candidate"],
                ic=item["rank_ic"],
                icir=item["icir"],
                monotonicity=item["monotonicity"],
                net=item["net_annual_return"],
                drawdown=item["maximum_drawdown"],
                turnover=item["turnover"],
                cost=item["cost_erosion"],
            )
        )
    return """# 板块7：验证期评价与候选冻结

## 冻结结果

- 选定方案：`{selected}`
- 预注册默认方案：`{default}`

| 候选方案 | Rank IC | ICIR | 五分组单调性 | 净年化收益 | 最大回撤 | 平均单边换手 | 成本侵蚀 |
|---|---:|---:|---:|---:|---:|---:|---:|
{rows}

## 证据边界

验证期结果不是最终样本外证据。2022–2025仍保持封存，本报告不据此新增因子、翻转方向或修改执行参数。
""".format(
        selected=decision["selected_candidate"],
        default=decision["default_candidate"],
        rows="\n".join(rows),
    )
