"""Forward-return labels that start where the executor can actually trade."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

import polars as pl

from ashare_multifactor.research.trade_observations import is_valid_trade_observation


def executable_forward_returns(
    panel: pl.DataFrame,
    signals: pl.DataFrame,
    *,
    horizons: Sequence[int],
    cutoff: date,
) -> pl.DataFrame:
    """Open-to-open returns entered on the next market trading day after the signal.

    The entry is the open of the first market trading day after the signal date and
    exists only if the stock is a valid trade observation that day (otherwise the
    executor could not buy it either). The exit is the open ``h`` valid observations
    later on the stock's own sequence, mirroring how the close-to-close label skips
    suspensions. Labels whose exit falls after ``cutoff`` are missing, so a period's
    labels never read past its boundary.
    """
    calendar = panel.select("date").unique().sort("date")
    next_day = (
        signals.select("date")
        .unique()
        .sort("date")
        .join_asof(
            calendar.rename({"date": "entry_date"}),
            left_on="date",
            right_on="entry_date",
            strategy="forward",
            allow_exact_matches=False,
        )
    )
    valid = (
        panel.filter(
            is_valid_trade_observation()
            & pl.col("open_adj").is_finite()
            & (pl.col("open_adj") > 0)
        )
        .sort("symbol", "date")
        .with_columns(pl.int_range(pl.len()).over("symbol").alias("observation"))
    )
    exits = [
        valid.select(
            "symbol",
            (pl.col("observation") - horizon).alias("observation"),
            pl.col("date").alias(f"_exit_date_{horizon}"),
            pl.col("open_adj").alias(f"_exit_open_{horizon}"),
        )
        for horizon in horizons
    ]
    entered = signals.select("date", "symbol").join(next_day, on="date", how="left").join(
        valid.select(
            "symbol",
            pl.col("date").alias("entry_date"),
            "observation",
            pl.col("open_adj").alias("entry_open"),
        ),
        on=["symbol", "entry_date"],
        how="left",
    )
    for horizon, exit_frame in zip(horizons, exits, strict=True):
        entered = entered.join(exit_frame, on=["symbol", "observation"], how="left")
    labels = [
        pl.when(pl.col(f"_exit_date_{horizon}") <= cutoff)
        .then(pl.col(f"_exit_open_{horizon}") / pl.col("entry_open") - 1.0)
        .alias(f"executable_return_{horizon}")
        for horizon in horizons
    ]
    return entered.select("date", "symbol", *labels).sort("date", "symbol")
