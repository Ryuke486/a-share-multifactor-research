from __future__ import annotations

import polars as pl

from ashare_multifactor.config import ResearchConfig


def add_momentum_and_forward_return(frame: pl.DataFrame, config: ResearchConfig) -> pl.DataFrame:
    """Add adjusted-price momentum, evaluation label, and eligible-only z-score."""
    clean_close = pl.when(
        pl.col("close_adj").is_not_null()
        & pl.col("close_adj").is_finite()
        & (pl.col("close_adj") > 0)
    ).then(pl.col("close_adj"))
    cleaned = frame.sort(["symbol", "date"]).with_columns(clean_close.alias("_valid_close_adj"))
    raw_momentum = (
        pl.col("_valid_close_adj")
        / pl.col("_valid_close_adj").shift(config.mvp.momentum_lookback).over("symbol")
        - 1.0
    )
    raw_forward_return = (
        pl.col("_valid_close_adj").shift(-config.mvp.forward_horizon).over("symbol")
        / pl.col("_valid_close_adj")
        - 1.0
    )
    with_returns = cleaned.with_columns(
        pl.when(raw_momentum.is_finite()).then(raw_momentum).alias("momentum_60"),
        pl.when(raw_forward_return.is_finite()).then(raw_forward_return).alias("forward_return_20"),
    )

    eligible_momentum = pl.when(pl.col("is_eligible").fill_null(False)).then(pl.col("momentum_60"))
    with_statistics = with_returns.with_columns(
        eligible_momentum.mean().over("date").alias("_momentum_mean"),
        eligible_momentum.std().over("date").alias("_momentum_std"),
    )
    raw_momentum_z = (pl.col("momentum_60") - pl.col("_momentum_mean")) / pl.col("_momentum_std")
    valid_statistics = (
        pl.col("is_eligible").fill_null(False)
        & pl.col("_momentum_mean").is_finite()
        & pl.col("_momentum_std").is_finite()
        & (pl.col("_momentum_std") > 0)
        & raw_momentum_z.is_finite()
    )
    momentum_z = pl.when(valid_statistics).then(raw_momentum_z)
    return (
        with_statistics.with_columns(momentum_z.alias("momentum_z"))
        .drop("_valid_close_adj", "_momentum_mean", "_momentum_std")
        .filter(
            pl.col("date").is_between(
                config.smoke_analysis.start,
                config.smoke_analysis.end,
                closed="both",
            )
        )
    )
