"""Raw value-factor construction with numeric validity separated from PIT readiness."""

from __future__ import annotations

import polars as pl

from ashare_multifactor.factors.definitions import factor_source_columns
from ashare_multifactor.factors.transforms import prepare_factor_inputs, safe_positive_reciprocal


_FACTOR_NAMES = ("ep_ttm", "bp", "sp_ttm")
_SOURCE_COLUMNS = factor_source_columns(_FACTOR_NAMES)


def compute_value_factors(frame: pl.DataFrame) -> pl.DataFrame:
    """Compute raw valuation reciprocals without applying PIT readiness gates."""
    inputs = prepare_factor_inputs(frame, _FACTOR_NAMES)
    return inputs.select(
        "date",
        "symbol",
        safe_positive_reciprocal(pl.col("pe_ttm")).alias("ep_ttm"),
        safe_positive_reciprocal(pl.col("pb")).alias("bp"),
        safe_positive_reciprocal(pl.col("ps_ttm")).alias("sp_ttm"),
    ).sort("date", "symbol")
