from __future__ import annotations

import polars as pl


FROZEN_CANDIDATE_FAMILIES = frozenset(
    {"liquidity", "reversal", "low_volatility"}
)


def build_ablation_scores(
    factor_panel: pl.DataFrame,
    rolling_weights: pl.DataFrame,
    *,
    removed_family: str,
) -> pl.DataFrame:
    """Remove exactly one frozen factor family and renormalize existing weights."""
    if removed_family not in FROZEN_CANDIDATE_FAMILIES:
        raise ValueError("removed family is not in frozen candidate families")
    eligible = factor_panel.filter(
        pl.col("family").is_in(FROZEN_CANDIDATE_FAMILIES)
        & (pl.col("family") != removed_family)
        & pl.col("score_size_neutral").is_finite()
    )
    weights = rolling_weights.filter(pl.col("family") != removed_family)
    weighted = eligible.join(
        weights.select("date", "factor_name", "weight"),
        on=["date", "factor_name"],
        how="inner",
        validate="m:1",
    )
    scores = (
        weighted.sort("date", "symbol", "factor_name")
        .group_by("date", "symbol", maintain_order=True)
        .agg(
            (
                (pl.col("score_size_neutral") * pl.col("weight")).sum()
                / pl.col("weight").sum()
            ).alias("raw_score"),
            pl.col("family").n_unique().alias("family_count"),
            pl.len().alias("factor_count"),
        )
        .filter(pl.col("family_count") == len(FROZEN_CANDIDATE_FAMILIES) - 1)
        .with_columns(
            (
                (pl.col("raw_score") - pl.col("raw_score").mean().over("date"))
                / pl.col("raw_score").std(ddof=0).over("date")
            )
            .fill_nan(0.0)
            .alias("score"),
            pl.lit("rolling_ic_family").alias("method"),
        )
    )
    return scores.select(
        "date",
        "symbol",
        "method",
        "score",
        "raw_score",
        "family_count",
        "factor_count",
    ).sort("date", "symbol")
