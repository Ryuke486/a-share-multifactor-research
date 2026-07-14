from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import polars as pl


def write_combination_figure(ic: pl.DataFrame, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    fig, axis = plt.subplots(figsize=(8, 4))
    for method in sorted(set(ic.get_column("method"))):
        frame = ic.filter((pl.col("method") == method) & (pl.col("horizon") == 20))
        axis.plot(frame["date"], frame["rank_ic"], label=method, linewidth=0.8)
    axis.axhline(0, color="black", linewidth=0.6)
    axis.set_title("Composite 20-day monthly Rank IC")
    axis.legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(root / "composite_ic.png", dpi=140)
    plt.close(fig)


def render_combination_report(
    summary: pl.DataFrame,
    diagnostics: pl.DataFrame,
    issues: pl.DataFrame,
    primary: str,
) -> str:
    primary_row = summary.filter(
        (pl.col("method") == primary) & (pl.col("horizon") == 20)
    ).row(0, named=True)
    avg_turnover = diagnostics.filter(
        pl.col("portfolio_name") == "size_stratified_buffered"
    ).get_column("one_way_turnover").mean()
    return f"""# 阶段五：因子合成与目标组合

本报告只使用2005–2016研究期，验证期和最终测试期仍封存。本阶段不模拟成交，不生成正式NAV。

- 预注册主复合方案：`{primary}`
- 20日平均Rank IC：{primary_row['mean_ic']:.6f}
- 20日ICIR：{primary_row['icir']:.6f}
- 规模分层缓冲组合平均单边目标换手：{avg_turnover:.6f}
- 质量警告：{issues.height}

价值因子point-in-time、历史行业、ST口径和真实成交约束仍未解决。目标权重只是阶段六账本的事前输入，不是可成交收益结论。
"""
