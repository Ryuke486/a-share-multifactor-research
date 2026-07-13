from __future__ import annotations

from collections.abc import Sequence

import polars as pl

from ashare_multifactor.research.trade_observations import is_valid_trade_observation


def add_forward_returns(frame: pl.DataFrame, horizons: Sequence[int]) -> pl.DataFrame:
    """Add per-symbol valid-observation returns and auditable gap diagnostics."""
    base = (
        frame.sort(["symbol", "date"])
        .with_columns(is_valid_trade_observation())
        .with_columns(
            pl.col("date")
            .cum_count()
            .over("symbol")
            .cast(pl.Int64)
            .alias("_panel_observation_index")
        )
    )
    valid = base.filter("is_valid_trade_observation")
    target_expressions: list[pl.Expr] = []
    for horizon in horizons:
        close_column = f"_target_close_{horizon}"
        date_column = f"_target_date_{horizon}"
        index_column = f"_target_index_{horizon}"
        target_expressions.extend(
            (
                pl.col("close_adj").shift(-horizon).over("symbol").alias(close_column),
                pl.col("date").shift(-horizon).over("symbol").alias(date_column),
                pl.col("_panel_observation_index")
                .shift(-horizon)
                .over("symbol")
                .alias(index_column),
            )
        )
    valid = valid.with_columns(target_expressions)

    label_columns: list[str] = []
    label_expressions: list[pl.Expr] = []
    for horizon in horizons:
        close_column = f"_target_close_{horizon}"
        date_column = f"_target_date_{horizon}"
        index_column = f"_target_index_{horizon}"
        return_column = f"forward_return_{horizon}"
        calendar_column = f"forward_calendar_days_{horizon}"
        skipped_column = f"forward_skipped_observations_{horizon}"
        label_columns.extend((return_column, calendar_column, skipped_column))
        target_exists = pl.col(close_column).is_not_null()
        raw_return = pl.col(close_column) / pl.col("close_adj") - 1.0
        label_expressions.extend(
            (
                pl.when(target_exists & raw_return.is_finite())
                .then(raw_return)
                .alias(return_column),
                pl.when(target_exists)
                .then((pl.col(date_column) - pl.col("date")).dt.total_days())
                .alias(calendar_column),
                pl.when(target_exists)
                .then(
                    pl.col(index_column)
                    - pl.col("_panel_observation_index")
                    - horizon
                )
                .alias(skipped_column),
            )
        )
    labels = valid.with_columns(label_expressions).select(
        "date", "symbol", *label_columns
    )
    return (
        base.join(
            labels,
            on=["date", "symbol"],
            how="left",
            validate="1:1",
        )
        .drop("_panel_observation_index")
        .sort(["symbol", "date"])
    )
