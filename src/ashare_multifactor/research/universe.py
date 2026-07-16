from __future__ import annotations

import polars as pl

from ashare_multifactor.config import ResearchConfig
from ashare_multifactor.data.security import filter_supported_markets


_MIN_HISTORY_OBSERVATIONS = 252
_LIQUIDITY_LOOKBACK = 20
_RAW_PRICE_COLUMNS = ("open_raw", "high_raw", "low_raw", "close_raw")


def build_universe(frame: pl.DataFrame, config: ResearchConfig) -> pl.DataFrame:
    """Add point-in-time history, liquidity, and eligibility fields."""
    frame = filter_supported_markets(frame, config.supported_markets)
    finite_amount = pl.when(pl.col("amount").is_not_null() & pl.col("amount").is_finite()).then(
        pl.col("amount")
    )
    enriched = frame.sort(["symbol", "date"]).with_columns(
        pl.col("date").cum_count().over("symbol").alias("history_observations"),
        finite_amount.rolling_mean(window_size=_LIQUIDITY_LOOKBACK, min_samples=_LIQUIDITY_LOOKBACK)
        .over("symbol")
        .alias("amount_mean_20"),
    )
    ranked = enriched.with_columns(
        pl.col("amount_mean_20")
        .rank(method="ordinal", descending=True)
        .over("date")
        .alias("liquidity_rank")
    )
    valid_prices = pl.all_horizontal(
        pl.col(column).is_not_null() & pl.col(column).is_finite() & (pl.col(column) > 0)
        for column in _RAW_PRICE_COLUMNS
    )
    is_eligible = (
        (pl.col("history_observations") >= _MIN_HISTORY_OBSERVATIONS)
        & ~pl.col("is_st")
        & valid_prices
        & pl.col("amount_mean_20").is_finite()
        & (pl.col("amount_mean_20") > 0)
        & (pl.col("liquidity_rank") <= config.mvp.universe_size)
    ).fill_null(False)
    return ranked.with_columns(is_eligible.alias("is_eligible"))
