from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from ashare_multifactor.combination.definitions import CANDIDATE_FACTORS
from ashare_multifactor.combination.panel import (
    build_composite_scores,
    build_rolling_composite_scores,
)
from ashare_multifactor.combination.rolling_ic import rolling_ic_weights
from ashare_multifactor.validation.protocol import assert_validation_read_allowed


@dataclass(frozen=True)
class ValidationCombinationExtension:
    scores: pl.DataFrame
    weights: pl.DataFrame
    audit: pl.DataFrame


def build_validation_combinations(
    panel: pl.DataFrame,
    classifications: pl.DataFrame,
    historical_rank_ic: pl.DataFrame,
    validation_rank_ic: pl.DataFrame,
    realization_dates: pl.DataFrame,
    *,
    minimum_families: int,
    window_months: int,
    minimum_months: int,
    shrinkage: float,
) -> ValidationCombinationExtension:
    """Build only the frozen family-equal and rolling-IC validation methods."""
    start = panel.get_column("date").min()
    end = panel.get_column("date").max()
    if start is None or end is None:
        raise ValueError("validation factor panel is empty")
    assert_validation_read_allowed(start, end)
    static = build_composite_scores(
        panel,
        classifications,
        methods=("family_equal",),
        minimum_families=minimum_families,
        analysis_start=start,
        analysis_end=end,
    )
    weight_dates = static.get_column("date").unique().sort().to_list()
    rank_ic = pl.concat((historical_rank_ic, validation_rank_ic), how="diagonal_relaxed")
    rolling = rolling_ic_weights(
        rank_ic,
        weight_dates=weight_dates,
        factors=CANDIDATE_FACTORS,
        window_months=window_months,
        minimum_months=minimum_months,
        shrinkage=shrinkage,
        realization_dates=realization_dates,
    )
    dynamic = build_rolling_composite_scores(
        panel,
        classifications,
        rolling,
        minimum_families=minimum_families,
    )
    scores = pl.concat((static, dynamic)).sort("date", "symbol", "method")
    weights = rolling.with_columns(
        pl.lit("rolling_ic_family").alias("method"),
        (pl.col("factor_weight") / len(CANDIDATE_FACTORS)).alias("weight"),
    ).select(
        "date",
        "method",
        "factor_name",
        "family",
        "weight",
        "history_start",
        "history_end",
        "history_months",
        "fallback_equal",
    ).sort("date", "factor_name")
    audit = scores.group_by("date", "method").agg(
        pl.len().alias("scored_securities"),
        pl.col("family_count").min().alias("minimum_family_count"),
    ).join(
        weights.group_by("date").agg(
            pl.col("fallback_equal").sum().alias("fallback_factor_count"),
            pl.col("history_months").min().alias("minimum_history_months"),
        ),
        on="date",
        how="left",
        validate="m:1",
    ).sort("date", "method")
    return ValidationCombinationExtension(scores, weights, audit)
