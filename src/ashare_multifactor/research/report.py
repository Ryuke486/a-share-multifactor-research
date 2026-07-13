"""Machine-derived charts and report for the momentum MVP."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Mapping

import polars as pl
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from ashare_multifactor.config import ResearchConfig
from ashare_multifactor.research.pipeline_io import read_json, write_json, write_text


def _rank_ic_summary(rank_ic: pl.DataFrame) -> dict[str, float | int | None]:
    valid = rank_ic.filter(pl.col("rank_ic").is_not_null() & pl.col("rank_ic").is_finite())
    mean = valid.get_column("rank_ic").mean() if valid.height else None
    standard_deviation = valid.get_column("rank_ic").std() if valid.height > 1 else 0.0
    return {
        "mean": float(mean) if mean is not None else None,
        "standard_deviation": (
            float(standard_deviation) if standard_deviation is not None else 0.0
        ),
        "valid_months": valid.height,
        "total_months": rank_ic.height,
    }


def _plot_nav(net_nav: pl.DataFrame, gross_nav: pl.DataFrame, path: Path) -> None:
    figure = Figure(figsize=(9, 5), constrained_layout=True)
    FigureCanvasAgg(figure)
    axis = figure.subplots()
    net_initial = float(net_nav.get_column("nav").item(0))
    gross_initial = float(gross_nav.get_column("nav").item(0))
    axis.plot(
        gross_nav.get_column("date").to_list(),
        (gross_nav.get_column("nav") / gross_initial).to_list(),
        label="Gross (0 bp)",
    )
    axis.plot(
        net_nav.get_column("date").to_list(),
        (net_nav.get_column("nav") / net_initial).to_list(),
        label="Net",
    )
    axis.set(title="MVP NAV", xlabel="Date", ylabel="Normalized NAV")
    axis.grid(alpha=0.3)
    axis.legend()
    figure.savefig(path, dpi=150)


def _plot_drawdown(net_nav: pl.DataFrame, path: Path) -> None:
    figure = Figure(figsize=(9, 4), constrained_layout=True)
    FigureCanvasAgg(figure)
    axis = figure.subplots()
    dates = net_nav.get_column("date").to_list()
    drawdown = net_nav.get_column("drawdown").to_list()
    axis.plot(dates, drawdown, color="tab:red")
    axis.fill_between(dates, drawdown, 0.0, color="tab:red", alpha=0.25)
    axis.set(title="Net Drawdown", xlabel="Date", ylabel="Drawdown")
    axis.grid(alpha=0.3)
    figure.savefig(path, dpi=150)


def _warmup_years(config: ResearchConfig) -> str:
    warmup_end = min(config.smoke_data.end, config.smoke_analysis.start - timedelta(days=1))
    if warmup_end < config.smoke_data.start:
        return "无独立"
    if config.smoke_data.start.year == warmup_end.year:
        return str(warmup_end.year)
    return f"{config.smoke_data.start.year}–{warmup_end.year}"


def _render_report(config: ResearchConfig, summary: Mapping[str, object]) -> str:
    rank_ic = summary["rank_ic"]
    gross = summary["gross"]
    net = summary["net"]
    assert isinstance(rank_ic, dict)
    assert isinstance(gross, dict)
    assert isinstance(net, dict)
    ic_mean = rank_ic["mean"]
    ic_mean_text = "N/A" if ic_mean is None else f"{ic_mean:.6f}"
    warmup_years = _warmup_years(config)
    momentum_lookback = config.mvp.momentum_lookback
    forward_horizon = config.mvp.forward_horizon
    return f"""# A股横截面{momentum_lookback}日动量MVP报告

> **结论边界：本结果仅为管道验证，不是最终投资结论。**

## 研究问题与日期范围

本小闭环检验动态A股截面股票池中的{momentum_lookback}日动量是否能被无泄漏地转换为月度信号、
t+1开盘目标组合和可对账净值。分析期为 {config.smoke_analysis.start.isoformat()} 至
{config.smoke_analysis.end.isoformat()}。{warmup_years}预热期只用于形成历史窗口，不进入NAV绩效。

## 股票池与因子

- 股票池：逐日要求至少252个历史观测、非ST、原始OHLC有效且为正、过20个观测的平均成交额为正，再取流动性前{config.mvp.universe_size}只。
- 因子：`momentum_{momentum_lookback} = close_adj[t] / close_adj[t-{momentum_lookback}] - 1`，仅在当日合格股票中做截面z标准化。
- 标签：{forward_horizon}个交易观测后的后复权收益，只用于Rank IC评价，不进入选股或权重。

## Rank IC

- 均值：{ic_mean_text}
- 标准差：{rank_ic["standard_deviation"]:.6f}
- 有效月数：{rank_ic["valid_months"]} / {rank_ic["total_months"]}

## 组合与绩效

- 毛收益（0bp、同一目标组合）：{gross["total_return"]:.6%}
- 净收益（配置成本）：{net["total_return"]:.6%}
- 最大回撤（净值、正损失幅度）：{net["max_drawdown"]:.6%}
- 平均调仓换手：{net["average_turnover"]:.6%}
- 总成本：{net["total_cost"]:.6f}
- 净回测拒单行数：{net["blocked_order_count"]:.0f}
- 陈旧估值证券日数：{net["stale_valuation_count"]:.0f}

## 限制

当前MVP允许分数持仓，使用统一{config.mvp.transaction_cost_bps:g}bp成本。最小缺价处理规则为：
持仓缺少当日价格时使用最近已知收盘价估值；调仓证券缺少开盘价时记录拒单，且拒单不追单，
冻结缺价持仓后，所有可交易目标共用同一个兼顾冻结市值和全部可执行成本的统一最大可行资金基数，
再先卖后买并保持可交易目标间原权重比例；等待下一次调仓重新生成目标。这不是停牌制度的完整模拟；
仍未处理涨跌停、整手、历史费率和
挂单追踪。因此该报告只证明数据—信号—目标—账本—报告链路可运行、可复现、可审计，不应
解读为策略稳健性或未来收益证据。
"""


def write_mvp_report(
    config: ResearchConfig,
    rank_ic: pl.DataFrame,
    net_nav: pl.DataFrame,
    gross_nav: pl.DataFrame,
    net_summary: Mapping[str, float],
    gross_summary: Mapping[str, float],
    output_dir: Path,
) -> dict[str, object]:
    """Write a machine summary first, then derive charts and prose from it."""
    if net_nav.is_empty() or gross_nav.is_empty():
        raise ValueError("report requires non-empty net and gross NAV")
    output_dir.mkdir(parents=True, exist_ok=True)
    summary: dict[str, object] = {
        "analysis": {
            "start": config.smoke_analysis.start.isoformat(),
            "end": config.smoke_analysis.end.isoformat(),
            "performance_min_date": net_nav.get_column("date").min().isoformat(),
            "performance_max_date": net_nav.get_column("date").max().isoformat(),
            "warmup_excluded": True,
        },
        "rank_ic": _rank_ic_summary(rank_ic),
        "gross": dict(gross_summary),
        "net": dict(net_summary),
        "cost_return_impact": (
            float(gross_summary["total_return"]) - float(net_summary["total_return"])
        ),
        "pipeline_validation_only": True,
    }
    summary_path = output_dir / "summary.json"
    write_json(summary_path, summary)
    machine_summary = read_json(summary_path)
    _plot_nav(net_nav, gross_nav, output_dir / "nav.png")
    _plot_drawdown(net_nav, output_dir / "drawdown.png")
    write_text(output_dir / "report.md", _render_report(config, machine_summary))
    return machine_summary
