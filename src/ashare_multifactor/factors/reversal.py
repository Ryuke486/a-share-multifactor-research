"""Raw short-term reversal factor family."""

from __future__ import annotations

import polars as pl

from ashare_multifactor.factors.definitions import factor_source_columns
from ashare_multifactor.factors.transforms import safe_return
from ashare_multifactor.research.trade_observations import is_valid_trade_observation


_KEY_COLUMNS = ("date", "symbol")
_FACTOR_NAMES = ("reversal_5", "reversal_20")
_SOURCE_COLUMNS = factor_source_columns(_FACTOR_NAMES)


def compute_reversal_factors(frame: pl.DataFrame) -> pl.DataFrame:
    """Compute raw reversal returns without applying the registered direction."""
    if frame.select(_KEY_COLUMNS).is_duplicated().any():
        raise ValueError("duplicate date and symbol keys are not allowed")

    base = frame.select(*_KEY_COLUMNS, *_SOURCE_COLUMNS).with_columns(is_valid_trade_observation())
    valid = base.filter("is_valid_trade_observation").sort(["symbol", "date"])
    factors = valid.select(
        *_KEY_COLUMNS,
        safe_return(
            pl.col("close_adj"),
            pl.col("close_adj").shift(5).over("symbol"),
        ).alias("reversal_5"),
        safe_return(
            pl.col("close_adj"),
            pl.col("close_adj").shift(20).over("symbol"),
        ).alias("reversal_20"),
    )
    return (
        base.select(_KEY_COLUMNS)
        .join(factors, on=_KEY_COLUMNS, how="left")
        .sort(["date", "symbol"])
    )
