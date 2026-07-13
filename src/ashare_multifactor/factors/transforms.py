"""Small reusable helpers for safe returns and complete rolling windows."""

from __future__ import annotations

import polars as pl


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
