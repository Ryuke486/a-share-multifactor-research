from __future__ import annotations

import polars as pl

from ashare_multifactor.config import ResearchConfig


def build_target_weights(
    frame: pl.DataFrame,
    trading_dates: pl.DataFrame,
    config: ResearchConfig,
) -> pl.DataFrame:
    """Select high-momentum stocks and map signals to the next observed session."""
    execution_calendar = (
        trading_dates.select("date")
        .unique()
        .sort("date")
        .with_columns(pl.col("date").shift(-1).alias("execution_date"))
        .drop_nulls("execution_date")
        .rename({"date": "signal_date"})
    )
    selected = (
        frame.filter(
            pl.col("momentum_z").is_not_null() & pl.col("momentum_z").is_finite()
        )
        .sort(
            ["date", "momentum_z", "symbol"],
            descending=[False, True, False],
        )
        .with_columns(
            pl.col("symbol").cum_count().over("date").alias("_selection_rank")
        )
        .filter(pl.col("_selection_rank") <= config.mvp.portfolio_size)
        .with_columns((1.0 / pl.len().over("date")).alias("target_weight"))
        .rename({"date": "signal_date"})
    )
    return (
        selected.join(execution_calendar, on="signal_date", how="inner")
        .select("signal_date", "execution_date", "symbol", "target_weight")
        .sort("signal_date", "symbol")
    )
