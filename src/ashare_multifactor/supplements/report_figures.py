"""Report figures drawn from the supplement tables and frozen release outputs."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import polars as pl  # noqa: E402

from ashare_multifactor.supplements.figure_style import (  # noqa: E402
    BASELINE,
    DIVERGING,
    MUTED_INK,
    PRIMARY_INK,
    SECONDARY_INK,
    SERIES,
    save,
    style_axis,
    title,
)


def plot_correlation_heatmap(matrix: np.ndarray, names: Sequence[str], path: Path) -> None:
    """Mean cross-sectional correlation of direction-adjusted factor scores."""
    colormap = LinearSegmentedColormap.from_list("diverging", DIVERGING)
    figure, axis = plt.subplots(figsize=(8.4, 7.2))
    image = axis.imshow(matrix, cmap=colormap, vmin=-1.0, vmax=1.0)
    axis.set_xticks(range(len(names)), names, rotation=60, ha="right", fontsize=8)
    axis.set_yticks(range(len(names)), names, fontsize=8)
    axis.tick_params(colors=SECONDARY_INK, length=0)
    for spine in axis.spines.values():
        spine.set_visible(False)
    for i in range(len(names)):
        for j in range(len(names)):
            value = matrix[i, j]
            axis.text(
                j, i, f"{round(value, 2) + 0.0:.2f}", ha="center", va="center", fontsize=6.5,
                color="#ffffff" if abs(value) > 0.6 else PRIMARY_INK,
            )
    bar = figure.colorbar(image, ax=axis, fraction=0.04, pad=0.02)
    bar.ax.tick_params(labelsize=7, colors=MUTED_INK)
    bar.outline.set_visible(False)
    title(axis, "Mean monthly cross-sectional correlation, 2005-2016 (direction-adjusted scores)")
    save(figure, path)
    plt.close(figure)


def plot_rolling_ic(rolling: pl.DataFrame, path: Path, *, split: date) -> None:
    """Trailing 12-month mean Rank IC of the two composite methods."""
    figure, axis = plt.subplots(figsize=(9.0, 4.2))
    style_axis(axis)
    labels = {"family_equal": "family_equal (pre-registered)", "rolling_ic_family": "rolling_ic_family"}
    for color, method in zip(SERIES, ("family_equal", "rolling_ic_family"), strict=False):
        frame = rolling.filter(pl.col("method") == method).drop_nulls("rolling_ic").sort("date")
        axis.plot(
            frame["date"].to_list(), frame["rolling_ic"].to_list(),
            color=color, linewidth=1.6, label=labels[method],
        )
    axis.axhline(0.0, color=BASELINE, linewidth=1.0)
    axis.axvline(split, color=MUTED_INK, linewidth=1.0, linestyle=":")
    axis.text(split, axis.get_ylim()[1], " validation starts", va="top", fontsize=8, color=SECONDARY_INK)
    axis.set_ylabel("trailing 12-month mean Rank IC (20-day)", fontsize=9, color=SECONDARY_INK)
    axis.legend(fontsize=8, frameon=False, loc="lower left")
    title(axis, "Composite signal Rank IC, 2005-2021")
    save(figure, path)
    plt.close(figure)


def plot_cost_components(components: pl.DataFrame, path: Path) -> None:
    """Research-period trading cost by component, in bps of traded amount."""
    names = {
        "commission": "Commission",
        "stamp_duty": "Stamp duty",
        "transfer_fee": "Transfer fee",
        "slippage_cost": "Fixed slippage",
        "impact_cost": "Market impact",
    }
    ordered = components.sort("bps")
    figure, axis = plt.subplots(figsize=(8.0, 3.4))
    style_axis(axis, grid_axis="x")
    positions = range(ordered.height)
    axis.barh(list(positions), ordered["bps"].to_list(), height=0.55, color=SERIES[0])
    axis.set_yticks(list(positions), [names[name] for name in ordered["component"]], fontsize=9)
    for position, bps, share in zip(positions, ordered["bps"], ordered["share"], strict=True):
        axis.text(
            bps, position, f"  {bps:.1f} bps ({share:.0%})", va="center", fontsize=8,
            color=SECONDARY_INK,
        )
    axis.set_xlim(0, float(ordered["bps"].max()) * 1.35)
    axis.set_xlabel("bps of traded amount", fontsize=9, color=SECONDARY_INK)
    title(axis, "Research-period trading cost by component (2005-2016, full-cost run)")
    save(figure, path)
    plt.close(figure)


def plot_drawdowns(frame: pl.DataFrame, path: Path, *, split: date) -> None:
    """Drawdowns of the selected strategy and the two zero-cost universe benchmarks."""
    series = (
        ("strategy_net", "Strategy, full cost"),
        ("equal_weight", "Universe equal-weight"),
        ("cap_weight", "Universe cap-weight"),
    )
    figure, axis = plt.subplots(figsize=(9.0, 4.2))
    style_axis(axis)
    dates = frame["date"].to_list()
    for color, (column, label) in zip(SERIES, series, strict=True):
        axis.plot(dates, frame[column].to_list(), color=color, linewidth=1.3, label=label)
    axis.axhline(0.0, color=BASELINE, linewidth=1.0)
    axis.axvline(split, color=MUTED_INK, linewidth=1.0, linestyle=":")
    axis.yaxis.set_major_formatter(plt.FuncFormatter(lambda value, _: f"{value:.0%}"))
    axis.set_ylabel("drawdown from running peak", fontsize=9, color=SECONDARY_INK)
    axis.legend(
        fontsize=8, frameon=False, loc="upper center", bbox_to_anchor=(0.5, -0.08), ncol=3
    )
    title(axis, "Drawdowns, 2005-2021 (selected candidate vs zero-cost universe benchmarks)")
    save(figure, path)
    plt.close(figure)


def plot_quintile_panels(excess: pl.DataFrame, names: Sequence[str], path: Path) -> None:
    """Quintile excess over the universe, research versus validation, one panel per signal."""
    columns = 4
    rows = -(-len(names) // columns)
    figure, axes = plt.subplots(rows, columns, figsize=(10.0, 2.6 * rows), sharey=True)
    flat = np.atleast_1d(axes).ravel()
    periods = (("research_2005_2016", "2005-2016"), ("validation_2017_2021", "2017-2021"))
    width = 0.36
    for axis, name in zip(flat, names, strict=False):
        style_axis(axis)
        for offset, color, (period, label) in zip((-width / 2, width / 2), SERIES, periods, strict=False):
            frame = excess.filter(
                (pl.col("period") == period) & (pl.col("object_name") == name)
            ).sort("quantile")
            axis.bar(
                [quantile + offset for quantile in frame["quantile"].to_list()],
                [value * 100 for value in frame["mean_excess"].to_list()],
                width=width, color=color, label=label,
            )
        axis.axhline(0.0, color=BASELINE, linewidth=1.0)
        axis.set_xticks(range(1, 6), ["Q1", "Q2", "Q3", "Q4", "Q5"], fontsize=7)
        axis.set_title(name, fontsize=9, color=PRIMARY_INK, loc="left")
    for axis in flat[len(names):]:
        axis.axis("off")
    flat[0].set_ylabel("excess over universe, %/month", fontsize=8, color=SECONDARY_INK)
    if len(flat) > len(names):
        handles, labels = flat[0].get_legend_handles_labels()
        flat[len(names)].legend(handles, labels, fontsize=9, frameon=False, loc="center")
    figure.suptitle(
        "Quintile 20-day return minus universe mean (Q5 = highest score, gross, equal-weight)",
        fontsize=10, color=PRIMARY_INK, x=0.01, ha="left",
    )
    save(figure, path)
    plt.close(figure)
