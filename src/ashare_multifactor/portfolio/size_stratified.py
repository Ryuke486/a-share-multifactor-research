from __future__ import annotations

import polars as pl


def size_stratified_buffered(
    panel: pl.DataFrame,
    *,
    previous_targets: pl.DataFrame,
    size_groups: int,
    per_group: int,
    buffer_rank: int,
    portfolio_name: str,
) -> pl.DataFrame:
    if panel.get_column("symbol").n_unique() != panel.height:
        raise ValueError("duplicate securities are not allowed")
    ranked_size = panel.filter(
        pl.col("score").is_finite() & pl.col("log_market_cap").is_finite()
    ).sort("log_market_cap", "symbol")
    count = ranked_size.height
    ranked_size = ranked_size.with_row_index("size_rank").with_columns(
        ((pl.col("size_rank") * size_groups / max(count, 1)).floor() + 1)
        .clip(1, size_groups)
        .cast(pl.Int64)
        .alias("size_group")
    )
    prior = set(previous_targets.get_column("symbol")) if not previous_targets.is_empty() else set()
    parts: list[pl.DataFrame] = []
    for group in range(1, size_groups + 1):
        ranked = ranked_size.filter(pl.col("size_group") == group).sort(
            "score", "symbol", descending=[True, False]
        ).with_row_index("group_rank", offset=1)
        retained = ranked.filter(
            pl.col("symbol").is_in(prior) & (pl.col("group_rank") <= buffer_rank)
        )
        retained_symbols = retained.get_column("symbol").to_list()
        fill = ranked.filter(~pl.col("symbol").is_in(retained_symbols))
        parts.append(pl.concat((retained, fill)).head(per_group))
    selected = pl.concat(parts) if parts else ranked_size.head(0)
    if selected.is_empty():
        raise ValueError("portfolio has no eligible securities")
    return selected.with_columns(
        pl.lit(portfolio_name).alias("portfolio_name"),
        pl.lit(1.0 / selected.height).alias("target_weight"),
    ).sort("size_group", "group_rank", "symbol")
