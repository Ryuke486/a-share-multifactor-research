from __future__ import annotations

import polars as pl

from ashare_multifactor.config import FactorResearchSettings
from ashare_multifactor.data.security import filter_supported_markets
from ashare_multifactor.research.trade_observations import is_valid_trade_observation


_RAW_PRICE_COLUMNS = ("open_raw", "high_raw", "low_raw", "close_raw")


def build_research_universe(
    frame: pl.DataFrame,
    settings: FactorResearchSettings,
) -> pl.DataFrame:
    """Add point-in-time eligibility and stable liquidity ranks for factor research."""
    scoped = filter_supported_markets(frame, settings.supported_markets)
    with_validity = scoped.sort(["symbol", "date"]).with_columns(
        is_valid_trade_observation()
    )
    with_history = with_validity.with_columns(
        pl.col("is_valid_trade_observation")
        .cast(pl.UInt32)
        .cum_sum()
        .over("symbol")
        .alias("history_observations")
    )
    valid_liquidity = with_history.filter("is_valid_trade_observation").with_columns(
        pl.col("amount")
        .rolling_mean(
            window_size=settings.liquidity_lookback,
            min_samples=settings.liquidity_lookback,
        )
        .over("symbol")
        .alias("liquidity_amount_mean")
    )
    enriched = with_history.join(
        valid_liquidity.select("date", "symbol", "liquidity_amount_mean"),
        on=["date", "symbol"],
        how="left",
        validate="1:1",
    ).sort(["symbol", "date"])
    valid_prices = pl.all_horizontal(
        pl.col(column).is_not_null() & pl.col(column).is_finite() & (pl.col(column) > 0)
        for column in _RAW_PRICE_COLUMNS
    )
    passes_filters = (
        pl.col("is_valid_trade_observation")
        & (pl.col("history_observations") >= settings.minimum_history)
        & ~pl.col("is_st")
        & valid_prices
        & pl.col("liquidity_amount_mean").is_finite()
        & (pl.col("liquidity_amount_mean") > 0)
    ).fill_null(False)
    ranked = enriched.with_columns(passes_filters.alias("_passes_research_filters")).with_columns(
        pl.when(pl.col("_passes_research_filters"))
        .then(pl.col("liquidity_amount_mean"))
        .rank(method="ordinal", descending=True)
        .over("date")
        .alias("liquidity_rank")
    )
    is_eligible = (
        pl.col("_passes_research_filters")
        & (pl.col("liquidity_rank") <= settings.universe_size)
    ).fill_null(False)
    return ranked.with_columns(is_eligible.alias("is_research_eligible")).drop(
        "_passes_research_filters"
    )
