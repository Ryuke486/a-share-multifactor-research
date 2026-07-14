from __future__ import annotations

import polars as pl


def top_n_equal(panel: pl.DataFrame, *, portfolio_size: int, portfolio_name: str) -> pl.DataFrame:
    if panel.get_column("symbol").n_unique() != panel.height:
        raise ValueError("duplicate securities are not allowed")
    selected = panel.filter(pl.col("score").is_finite()).sort(
        "score", "symbol", descending=[True, False]
    ).head(portfolio_size)
    if selected.is_empty():
        raise ValueError("portfolio has no eligible securities")
    return selected.with_row_index("selection_rank", offset=1).with_columns(
        pl.lit(portfolio_name).alias("portfolio_name"),
        pl.lit(1.0 / selected.height).alias("target_weight"),
    )
