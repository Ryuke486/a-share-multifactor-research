"""Tables behind the report figures, kept separate from plotting so they can be tested."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np
import polars as pl


COST_COMPONENTS = ("commission", "stamp_duty", "transfer_fee", "slippage_cost", "impact_cost")


def quintile_excess(quantiles: pl.DataFrame) -> pl.DataFrame:
    """Mean monthly return of each quantile minus that month's count-weighted universe mean."""
    universe = quantiles.group_by("date", "object_name").agg(
        ((pl.col("mean_return") * pl.col("n_obs")).sum() / pl.col("n_obs").sum()).alias("universe")
    )
    return (
        quantiles.join(universe, on=["date", "object_name"], how="inner")
        .with_columns((pl.col("mean_return") - pl.col("universe")).alias("excess"))
        .group_by("object_name", "quantile")
        .agg(pl.col("excess").mean().alias("mean_excess"), pl.len().alias("months"))
        .sort("object_name", "quantile")
    )


def rolling_mean_ic(series: pl.DataFrame, *, window: int = 12) -> pl.DataFrame:
    """Trailing mean of monthly Rank IC per method; empty until a full window exists."""
    return series.sort("method", "date").with_columns(
        pl.col("rank_ic")
        .rolling_mean(window_size=window, min_samples=window)
        .over("method")
        .alias("rolling_ic")
    )


def drawdowns(levels: pl.DataFrame, *, columns: Sequence[str]) -> pl.DataFrame:
    """Decline of each level path from its running peak."""
    return levels.sort("date").select(
        "date", *[(pl.col(name) / pl.col(name).cum_max() - 1.0).alias(name) for name in columns]
    )


def cost_components(breakdown: Mapping[str, float], *, traded_amount: float) -> pl.DataFrame:
    """Each recorded cost component in basis points of traded amount and as a share of cost."""
    components = [name for name in COST_COMPONENTS if name in breakdown]
    total = float(breakdown["total_cost"])
    if not np.isclose(sum(float(breakdown[name]) for name in components), total, rtol=1e-9):
        raise ValueError("cost components do not add up to the recorded total")
    return pl.DataFrame(
        {
            "component": components,
            "bps": [float(breakdown[name]) / traded_amount * 1e4 for name in components],
            "share": [float(breakdown[name]) / total for name in components],
        }
    )


def correlation_matrix(pairs: pl.DataFrame, *, order: Sequence[str]) -> np.ndarray:
    """Square matrix of mean cross-sectional correlations in the requested factor order."""
    lookup = {
        (row["factor_a"], row["factor_b"]): row["mean_correlation"]
        for row in pairs.iter_rows(named=True)
    }
    matrix = np.empty((len(order), len(order)))
    for i, left in enumerate(order):
        for j, right in enumerate(order):
            value = lookup.get((left, right), lookup.get((right, left)))
            if value is None:
                raise ValueError(f"correlation for {left} and {right} is missing")
            matrix[i, j] = value
    return matrix
