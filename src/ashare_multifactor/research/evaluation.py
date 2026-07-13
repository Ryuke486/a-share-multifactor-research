from __future__ import annotations

import polars as pl
from scipy.stats import spearmanr


_MIN_IC_STOCKS = 20
_RANK_IC_SCHEMA = {
    "date": pl.Date,
    "n_stocks": pl.Int64,
    "rank_ic": pl.Float64,
    "reason": pl.String,
}


def monthly_signal_panel(frame: pl.DataFrame) -> pl.DataFrame:
    """Keep every security on the last observed trading date of each month."""
    sort_columns = ["date", *(column for column in ("symbol",) if column in frame.columns)]
    return (
        frame.with_columns(pl.col("date").dt.truncate("1mo").alias("_signal_month"))
        .filter(pl.col("date") == pl.col("date").max().over("_signal_month"))
        .drop("_signal_month")
        .sort(sort_columns)
    )


def rank_ic_by_date(frame: pl.DataFrame) -> pl.DataFrame:
    """Calculate cross-sectional Spearman IC for each signal date."""
    rows: list[dict[str, object]] = []
    for cross_section in frame.sort("date").partition_by("date", maintain_order=True):
        valid = cross_section.filter(
            pl.col("momentum_z").is_not_null()
            & pl.col("momentum_z").is_finite()
            & pl.col("forward_return_20").is_not_null()
            & pl.col("forward_return_20").is_finite()
        )
        n_stocks = valid.height
        rank_ic: float | None = None
        reason: str | None = None
        if n_stocks < _MIN_IC_STOCKS:
            reason = "insufficient_stocks"
        elif valid.get_column("momentum_z").n_unique() == 1:
            reason = "constant_factor"
        elif valid.get_column("forward_return_20").n_unique() == 1:
            reason = "constant_forward_return"
        else:
            rank_ic = float(
                spearmanr(
                    valid.get_column("momentum_z").to_numpy(),
                    valid.get_column("forward_return_20").to_numpy(),
                ).statistic
            )
        rows.append(
            {
                "date": cross_section.get_column("date").item(0),
                "n_stocks": n_stocks,
                "rank_ic": rank_ic,
                "reason": reason,
            }
        )
    return pl.DataFrame(rows, schema=_RANK_IC_SCHEMA)
