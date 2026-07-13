"""Small reusable helpers for safe returns and complete rolling windows."""

from __future__ import annotations

from collections.abc import Sequence

import polars as pl

from ashare_multifactor.factors.definitions import factor_source_columns


def prepare_factor_inputs(frame: pl.DataFrame, factor_names: Sequence[str]) -> pl.DataFrame:
    """Project registered inputs, reject duplicate keys, and sort for grouped windows."""
    projected = frame.select("date", "symbol", *factor_source_columns(factor_names))
    has_duplicate_keys = projected.select(pl.struct("date", "symbol").is_duplicated().any()).item()
    if has_duplicate_keys:
        raise ValueError("duplicate date/symbol keys are not allowed")
    return projected.sort("symbol", "date")


def safe_positive_reciprocal(values: pl.Expr) -> pl.Expr:
    """Return the reciprocal only for finite strictly positive values."""
    reciprocal = 1.0 / values
    valid = values.is_not_null() & values.is_finite() & (values > 0) & reciprocal.is_finite()
    return pl.when(valid).then(reciprocal)


def safe_positive_log(values: pl.Expr) -> pl.Expr:
    """Return the natural logarithm only for finite strictly positive values."""
    logarithm = values.log()
    valid = values.is_not_null() & values.is_finite() & (values > 0) & logarithm.is_finite()
    return pl.when(valid).then(logarithm)


def safe_return(current: pl.Expr, previous: pl.Expr) -> pl.Expr:
    """Return ``current / previous - 1`` only for positive finite prices."""
    raw_return = current / previous - 1.0
    valid = (
        current.is_not_null()
        & current.is_finite()
        & (current > 0)
        & previous.is_not_null()
        & previous.is_finite()
        & (previous > 0)
        & raw_return.is_finite()
    )
    return pl.when(valid).then(raw_return)


def rolling_sample_standard_deviation(values: pl.Expr, window_size: int) -> pl.Expr:
    """Return ddof=1 rolling standard deviation for complete windows only."""
    if window_size < 2:
        raise ValueError("sample standard deviation requires window_size >= 2")
    return values.rolling_std(window_size=window_size, min_samples=window_size, ddof=1)


def rolling_downside_deviation(values: pl.Expr, window_size: int) -> pl.Expr:
    """Return rolling root mean square of negative observations."""
    if window_size < 1:
        raise ValueError("downside deviation requires window_size >= 1")
    squared_downside = values.clip(upper_bound=0.0).pow(2)
    return squared_downside.rolling_mean(
        window_size=window_size,
        min_samples=window_size,
    ).sqrt()
