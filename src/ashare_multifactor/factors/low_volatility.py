"""Raw low-volatility factor family."""

from __future__ import annotations

import math

import polars as pl

from ashare_multifactor.factors.definitions import factor_source_columns
from ashare_multifactor.factors.transforms import (
    rolling_downside_deviation,
    rolling_sample_standard_deviation,
    safe_return,
)
from ashare_multifactor.research.trade_observations import is_valid_trade_observation


_KEY_COLUMNS = ("date", "symbol")
_FACTOR_NAMES = ("volatility_20", "volatility_60", "downside_volatility_60")
_SOURCE_COLUMNS = factor_source_columns(_FACTOR_NAMES)
_ANNUALIZATION = math.sqrt(252.0)


def compute_low_volatility_factors(frame: pl.DataFrame) -> pl.DataFrame:
    """Compute annualized raw volatility values from adjusted daily returns."""
    if frame.select(_KEY_COLUMNS).is_duplicated().any():
        raise ValueError("duplicate date and symbol keys are not allowed")

    base = frame.select(*_KEY_COLUMNS, *_SOURCE_COLUMNS).with_columns(is_valid_trade_observation())
    valid = (
        base.filter("is_valid_trade_observation")
        .sort(["symbol", "date"])
        .with_columns(
            safe_return(pl.col("close_adj"), pl.col("prev_close_adj")).alias("_daily_return")
        )
    )
    factors = valid.select(
        *_KEY_COLUMNS,
        (
            rolling_sample_standard_deviation(pl.col("_daily_return"), 20).over("symbol")
            * _ANNUALIZATION
        ).alias("volatility_20"),
        (
            rolling_sample_standard_deviation(pl.col("_daily_return"), 60).over("symbol")
            * _ANNUALIZATION
        ).alias("volatility_60"),
        (
            rolling_downside_deviation(pl.col("_daily_return"), 60).over("symbol") * _ANNUALIZATION
        ).alias("downside_volatility_60"),
    )
    return (
        base.select(_KEY_COLUMNS)
        .join(factors, on=_KEY_COLUMNS, how="left")
        .sort(["date", "symbol"])
    )
