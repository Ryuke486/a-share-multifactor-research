from __future__ import annotations

import polars as pl


_TRADE_OBSERVATION_COLUMNS = ("close_adj", "volume", "amount")


def is_valid_trade_observation() -> pl.Expr:
    """Return the authoritative positive-finite trade-observation predicate."""
    return pl.all_horizontal(
        pl.col(column).is_not_null()
        & pl.col(column).is_finite()
        & (pl.col(column) > 0)
        for column in _TRADE_OBSERVATION_COLUMNS
    ).alias("is_valid_trade_observation")
