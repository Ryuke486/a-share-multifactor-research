"""Raw size-factor construction from the unadjusted market-cap field."""

from __future__ import annotations

import polars as pl

from ashare_multifactor.factors.definitions import factor_source_columns
from ashare_multifactor.factors.transforms import prepare_factor_inputs, safe_positive_log


_FACTOR_NAMES = ("log_market_cap",)
_SOURCE_COLUMNS = factor_source_columns(_FACTOR_NAMES)


def compute_size_factor(frame: pl.DataFrame) -> pl.DataFrame:
    """Compute log total market cap without reconstructing market value from prices."""
    inputs = prepare_factor_inputs(frame, _FACTOR_NAMES)
    return inputs.select(
        "date",
        "symbol",
        safe_positive_log(pl.col("total_market_cap")).alias("log_market_cap"),
    ).sort("date", "symbol")
