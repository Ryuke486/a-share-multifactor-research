"""Small plotting helpers for machine-derived factor-card figures."""

from __future__ import annotations

from pathlib import Path

import polars as pl
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from ashare_multifactor.config import FactorResearchSettings
from ashare_multifactor.factors.definitions import FactorDefinition
from ashare_multifactor.research.factor_evaluation import FactorEvaluationBundle


def write_factor_figures(
    evaluation: FactorEvaluationBundle,
    definition: FactorDefinition,
    settings: FactorResearchSettings,
    output_dir: Path,
) -> None:
    """Write the four fixed card figures without reading raw factor observations."""
    output_dir.mkdir(parents=True, exist_ok=True)
    variant = _primary_variant(definition)
    _line_figure(
        evaluation.rank_ic.filter(
            (pl.col("factor_name") == definition.name)
            & (pl.col("score_variant") == variant)
            & (pl.col("horizon") == settings.primary_horizon)
            & pl.col("rank_ic").is_not_null()
            & pl.col("rank_ic").is_finite()
        ).sort("date"),
        "date",
        "rank_ic",
        "Monthly Rank IC",
        "Rank IC",
        output_dir / "ic.png",
    )
    quantiles = (
        evaluation.quantile_returns.filter(
            (pl.col("factor_name") == definition.name) & (pl.col("score_variant") == variant)
        )
        .group_by("quantile")
        .agg(pl.col("mean_forward_return_20").mean())
        .sort("quantile")
    )
    _bar_figure(
        quantiles,
        "quantile",
        "mean_forward_return_20",
        "Mean Quintile Return",
        "Return",
        output_dir / "quantiles.png",
    )
    decay = evaluation.factor_summary.filter(
        (pl.col("factor_name") == definition.name)
        & (pl.col("score_variant") == variant)
        & pl.col("horizon").is_in(settings.forward_horizons)
        & pl.col("mean_ic").is_not_null()
        & pl.col("mean_ic").is_finite()
    ).sort("horizon")
    _line_figure(
        decay,
        "horizon",
        "mean_ic",
        "Rank IC Decay",
        "Mean Rank IC",
        output_dir / "decay.png",
    )
    subperiods = evaluation.subperiod_metrics.filter(
        (pl.col("factor_name") == definition.name)
        & (pl.col("score_variant") == variant)
        & pl.col("mean_ic").is_not_null()
        & pl.col("mean_ic").is_finite()
    ).sort("start")
    _bar_figure(
        subperiods,
        "subperiod",
        "mean_ic",
        "Subperiod Mean Rank IC",
        "Mean Rank IC",
        output_dir / "subperiods.png",
    )


def _line_figure(
    frame: pl.DataFrame,
    x_column: str,
    y_column: str,
    title: str,
    ylabel: str,
    path: Path,
) -> None:
    figure, axis = _figure(title, ylabel)
    if frame.is_empty():
        _show_no_data(axis)
    else:
        axis.plot(frame.get_column(x_column).to_list(), frame.get_column(y_column).to_list())
        axis.axhline(0.0, color="black", linewidth=0.8, alpha=0.5)
    _save_and_clear(figure, path)


def _bar_figure(
    frame: pl.DataFrame,
    x_column: str,
    y_column: str,
    title: str,
    ylabel: str,
    path: Path,
) -> None:
    figure, axis = _figure(title, ylabel)
    if frame.is_empty():
        _show_no_data(axis)
    else:
        axis.bar(
            [str(value) for value in frame.get_column(x_column).to_list()],
            frame.get_column(y_column).to_list(),
        )
        axis.axhline(0.0, color="black", linewidth=0.8, alpha=0.5)
    _save_and_clear(figure, path)


def _figure(title: str, ylabel: str) -> tuple[Figure, object]:
    figure = Figure(figsize=(7, 4), constrained_layout=True)
    FigureCanvasAgg(figure)
    axis = figure.subplots()
    axis.set(title=title, ylabel=ylabel)
    axis.grid(axis="y", alpha=0.25)
    return figure, axis


def _show_no_data(axis: object) -> None:
    axis.text(0.5, 0.5, "No valid machine-readable data", ha="center", va="center")
    axis.set_xticks([])
    axis.set_yticks([])


def _save_and_clear(figure: Figure, path: Path) -> None:
    figure.savefig(path, dpi=120)
    figure.clear()


def _primary_variant(definition: FactorDefinition) -> str:
    return "score_size_neutral" if definition.size_neutralize else "score"
