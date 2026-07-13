"""Raw momentum factor family."""

from __future__ import annotations

import polars as pl

from ashare_multifactor.factors.transforms import safe_return
from ashare_multifactor.research.trade_observations import is_valid_trade_observation


_KEY_COLUMNS = ("date", "symbol")
_INPUT_COLUMNS = (*_KEY_COLUMNS, "close_adj", "volume", "amount")


def compute_momentum_factors(frame: pl.DataFrame) -> pl.DataFrame:
    """Compute raw momentum values on each symbol's valid-observation history."""
    if frame.select(_KEY_COLUMNS).is_duplicated().any():
        raise ValueError("duplicate date and symbol keys are not allowed")

    base = frame.select(_INPUT_COLUMNS).with_columns(is_valid_trade_observation())
    valid = base.filter("is_valid_trade_observation").sort(["symbol", "date"])
    factors = valid.select(
        *_KEY_COLUMNS,
        safe_return(
            pl.col("close_adj"),
            pl.col("close_adj").shift(60).over("symbol"),
        ).alias("momentum_60"),
        safe_return(
            pl.col("close_adj"),
            pl.col("close_adj").shift(120).over("symbol"),
        ).alias("momentum_120"),
        safe_return(
            pl.col("close_adj").shift(21).over("symbol"),
            pl.col("close_adj").shift(252).over("symbol"),
        ).alias("momentum_12_1"),
    )
    return (
        base.select(_KEY_COLUMNS)
        .join(factors, on=_KEY_COLUMNS, how="left")
        .sort(["date", "symbol"])
    )
