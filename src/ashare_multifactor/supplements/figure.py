"""Strategy-versus-benchmark growth figure for the v1.0 report."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import polars as pl  # noqa: E402

from ashare_multifactor.supplements.figure_style import (  # noqa: E402
    GRID as _GRID,
    MUTED_INK as _MUTED_INK,
    PRIMARY_INK as _PRIMARY_INK,
    SECONDARY_INK as _SECONDARY_INK,
    SERIES,
    SURFACE as _SURFACE,
)


# categorical slots 1-3 of the validated default palette; the zero-cost line is the
# same strategy, so it keeps slot 1 and differs by dash pattern
_SERIES = (
    ("strategy_net", "Strategy, full cost", "net", SERIES[0], "-"),
    ("strategy_zero_cost", "Strategy, zero cost", "zero cost", SERIES[0], "--"),
    ("equal_weight", "Universe equal-weight", "EW", SERIES[1], "-"),
    ("cap_weight", "Universe cap-weight", "CW", SERIES[2], "-"),
)


def plot_benchmark_comparison(levels: pl.DataFrame, path: Path, *, split: date) -> None:
    """Plot growth of 1 on a log axis with the research/validation boundary marked."""
    figure, axis = plt.subplots(figsize=(9.0, 4.8))
    figure.patch.set_facecolor(_SURFACE)
    axis.set_facecolor(_SURFACE)
    dates = levels.get_column("date").to_list()
    for column, label, _, color, style in _SERIES:
        values = levels.get_column(column).to_list()
        axis.plot(dates, values, color=color, linestyle=style, linewidth=1.6, label=label)
    ends = [(levels.get_column(column)[-1], short) for column, _, short, _, _ in _SERIES]
    for value, position, short in _separated_labels(ends):
        axis.annotate(
            f"{short} {value:.2f}",
            xy=(dates[-1], position),
            xytext=(6, 0),
            textcoords="offset points",
            va="center",
            fontsize=8,
            color=_SECONDARY_INK,
        )
    axis.set_yscale("log")
    axis.axvline(split, color=_MUTED_INK, linewidth=1.0, linestyle=":")
    axis.text(
        split,
        axis.get_ylim()[1],
        " validation starts",
        va="top",
        fontsize=8,
        color=_SECONDARY_INK,
    )
    axis.set_title(
        "Growth of 1, 2005-2021 (selected candidate vs zero-cost universe benchmarks)",
        fontsize=10,
        color=_PRIMARY_INK,
        loc="left",
    )
    axis.set_ylabel("growth of 1 (log scale)", fontsize=9, color=_SECONDARY_INK)
    axis.grid(True, which="major", color=_GRID, linewidth=0.6)
    axis.tick_params(colors=_MUTED_INK, labelsize=8)
    for spine in ("top", "right"):
        axis.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        axis.spines[spine].set_color(_GRID)
    axis.legend(fontsize=8, frameon=False, loc="upper left")
    figure.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(path, dpi=140, facecolor=_SURFACE, metadata={"Software": None})
    plt.close(figure)


def _separated_labels(
    ends: list[tuple[float, str]], *, minimum_ratio: float = 1.1
) -> list[tuple[float, float, str]]:
    """Push end labels apart on the log axis so close series stay readable."""
    ordered = sorted(ends)
    placed: list[tuple[float, float, str]] = []
    for value, name in ordered:
        position = value
        if placed and position < placed[-1][1] * minimum_ratio:
            position = placed[-1][1] * minimum_ratio
        placed.append((value, position, name))
    return placed
