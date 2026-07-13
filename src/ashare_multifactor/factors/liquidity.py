"""Liquidity factors over the shared valid-trade-observation sequence."""

from __future__ import annotations

import polars as pl

from ashare_multifactor.factors.definitions import factor_source_columns
from ashare_multifactor.factors.transforms import prepare_factor_inputs, safe_return
from ashare_multifactor.research.trade_observations import is_valid_trade_observation


_FACTOR_NAMES = ("turnover_20", "amihud_20")
_SOURCE_COLUMNS = factor_source_columns(_FACTOR_NAMES)
_WINDOW_SIZE = 20


def compute_liquidity_factors(frame: pl.DataFrame) -> pl.DataFrame:
    """Compute complete-window turnover and Amihud factors while retaining all rows."""
    inputs = prepare_factor_inputs(frame, _FACTOR_NAMES)
    valid_observations = inputs.filter(is_valid_trade_observation()).with_columns(
        _valid_turnover().alias("_turnover"),
        _daily_amihud().alias("_daily_amihud"),
    )
    factors = valid_observations.with_columns(
        pl.col("_turnover")
        .rolling_mean(window_size=_WINDOW_SIZE, min_samples=_WINDOW_SIZE)
        .over("symbol")
        .alias("turnover_20"),
        pl.col("_daily_amihud")
        .rolling_mean(window_size=_WINDOW_SIZE, min_samples=_WINDOW_SIZE)
        .over("symbol")
        .alias("amihud_20"),
    ).select("date", "symbol", *_FACTOR_NAMES)
    return (
        inputs.select("date", "symbol")
        .join(factors, on=["date", "symbol"], how="left", validate="1:1")
        .sort("date", "symbol")
    )


def _valid_turnover() -> pl.Expr:
    turnover = pl.col("turnover")
    valid = turnover.is_not_null() & turnover.is_finite() & (turnover >= 0)
    return pl.when(valid).then(turnover)


def _daily_amihud() -> pl.Expr:
    daily_return = safe_return(pl.col("close_adj"), pl.col("prev_close_adj")).abs()
    amihud = daily_return / pl.col("amount")
    valid = amihud.is_not_null() & amihud.is_finite()
    return pl.when(valid).then(amihud)
