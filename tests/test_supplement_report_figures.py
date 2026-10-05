from datetime import date
from pathlib import Path

import numpy as np
import polars as pl

from ashare_multifactor.supplements.report_figures import (
    plot_correlation_heatmap,
    plot_cost_components,
    plot_drawdowns,
    plot_quintile_panels,
    plot_rolling_ic,
)

SPLIT = date(2017, 1, 1)


def _is_png(path: Path) -> bool:
    return path.is_file() and path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_every_report_figure_renders_from_synthetic_tables(tmp_path: Path) -> None:
    days = [date(2016, month, 28) for month in range(1, 13)] + [date(2017, 1, 26)]

    plot_correlation_heatmap(np.array([[1.0, -0.001], [-0.001, 1.0]]), ["a", "b"], tmp_path / "h.png")
    plot_rolling_ic(
        pl.DataFrame(
            {
                "date": days * 2,
                "method": ["family_equal"] * 13 + ["rolling_ic_family"] * 13,
                "rolling_ic": [None] * 11 + [0.1, 0.12] + [None] * 11 + [0.09, 0.11],
            }
        ),
        tmp_path / "r.png",
        split=SPLIT,
    )
    plot_cost_components(
        pl.DataFrame({"component": ["commission", "impact_cost"], "bps": [3.0, 22.0], "share": [0.12, 0.88]}),
        tmp_path / "c.png",
    )
    plot_drawdowns(
        pl.DataFrame(
            {
                "date": days,
                "strategy_net": [-0.01 * index for index in range(13)],
                "equal_weight": [-0.02 * index for index in range(13)],
                "cap_weight": [0.0] * 13,
            }
        ),
        tmp_path / "d.png",
        split=SPLIT,
    )
    rows = [
        (period, name, quantile, 0.001 * quantile, 10)
        for period in ("research_2005_2016", "validation_2017_2021")
        for name in ("x", "y")
        for quantile in range(1, 6)
    ]
    plot_quintile_panels(
        pl.DataFrame(rows, schema=["period", "object_name", "quantile", "mean_excess", "months"], orient="row"),
        ("x", "y"),
        tmp_path / "q.png",
    )

    assert all(_is_png(tmp_path / f"{name}.png") for name in "hrcdq")
