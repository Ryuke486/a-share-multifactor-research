from __future__ import annotations

from collections.abc import Sequence
from datetime import date

import numpy as np
import polars as pl

from ashare_multifactor.combination.definitions import REPRESENTATIVE_FACTORS
from ashare_multifactor.combination.eligibility import candidate_registry


def build_composite_scores(
    panel: pl.DataFrame,
    classifications: pl.DataFrame,
    *,
    methods: Sequence[str],
    minimum_families: int = 2,
    analysis_start: date = date(2005, 1, 1),
    analysis_end: date = date(2016, 12, 31),
) -> pl.DataFrame:
    required = {"date", "symbol", "factor_name", "family", "score_size_neutral"}
    missing = sorted(required - set(panel.columns))
    if missing:
        raise ValueError("factor panel is missing columns: " + ", ".join(missing))
    if panel.select("date", "symbol", "factor_name").is_duplicated().any():
        raise ValueError("duplicate factor panel keys are not allowed")
    if not panel.is_empty() and (
        panel.get_column("date").min() < analysis_start
        or panel.get_column("date").max() > analysis_end
    ):
        raise ValueError("factor combination must remain inside the research period")
    registry = candidate_registry(classifications)
    candidates = {factor for factors in registry.values() for factor in factors}
    expected_families = {
        factor: family for family, factors in registry.items() for factor in factors
    }
    candidate_rows = panel.filter(pl.col("factor_name").is_in(candidates))
    actual = candidate_rows.select("factor_name", "family").unique()
    if actual.filter(
        pl.col("family")
        != pl.col("factor_name").replace_strict(expected_families)
    ).height:
        raise ValueError("factor panel families do not match the frozen registry")
    eligible = panel.filter(pl.col("factor_name").is_in(candidates)).filter(
        pl.col("score_size_neutral").is_finite()
    )
    outputs = [_static_method(eligible, method, minimum_families) for method in methods]
    return pl.concat(outputs).sort("date", "symbol", "method") if outputs else pl.DataFrame()


def build_rolling_composite_scores(
    panel: pl.DataFrame,
    classifications: pl.DataFrame,
    weights: pl.DataFrame,
    *,
    minimum_families: int = 2,
) -> pl.DataFrame:
    registry = candidate_registry(classifications)
    candidates = {factor for factors in registry.values() for factor in factors}
    eligible = panel.filter(
        pl.col("factor_name").is_in(candidates) & pl.col("score_size_neutral").is_finite()
    )
    weighted = eligible.join(
        weights.select("date", "factor_name", "factor_weight"),
        on=["date", "factor_name"],
        how="inner",
        validate="m:1",
    )
    family_scores = weighted.sort(
        "date", "symbol", "family", "factor_name"
    ).group_by("date", "symbol", "family", maintain_order=True).agg(
        (pl.col("score_size_neutral") * pl.col("factor_weight")).sum()
        .truediv(pl.col("factor_weight").sum())
        .alias("family_score"),
        pl.len().alias("factor_count"),
    )
    scores = family_scores.group_by("date", "symbol", maintain_order=True).agg(
        pl.col("family_score").mean().alias("raw_score"),
        pl.len().alias("family_count"),
        pl.col("factor_count").sum().alias("factor_count"),
    ).filter(pl.col("family_count") >= minimum_families).with_columns(
        pl.lit("rolling_ic_family").alias("method")
    )
    return _deterministic_standardize(scores)


def _static_method(panel: pl.DataFrame, method: str, minimum_families: int) -> pl.DataFrame:
    if method == "representative_equal":
        panel = panel.filter(pl.col("factor_name").is_in(REPRESENTATIVE_FACTORS))
    elif method not in {"candidate_equal", "family_equal"}:
        raise ValueError(f"unsupported static combination method: {method}")
    family_scores = panel.sort(
        "date", "symbol", "family", "factor_name"
    ).group_by("date", "symbol", "family", maintain_order=True).agg(
        pl.col("score_size_neutral").mean().alias("family_score"),
        pl.len().alias("factor_count"),
    )
    score_expression = (
        pl.col("family_score").mean()
        if method != "candidate_equal"
        else (pl.col("family_score") * pl.col("factor_count")).sum()
        / pl.col("factor_count").sum()
    )
    scores = family_scores.group_by("date", "symbol", maintain_order=True).agg(
        score_expression.alias("raw_score"),
        pl.len().alias("family_count"),
        pl.col("factor_count").sum().alias("factor_count"),
    ).filter(pl.col("family_count") >= minimum_families)
    scores = scores.with_columns(pl.lit(method).alias("method"))
    return scores.with_columns(
        ((pl.col("raw_score") - pl.col("raw_score").mean().over("date", "method"))
         / pl.col("raw_score").std(ddof=0).over("date", "method"))
        .fill_nan(0.0)
        .alias("score")
    ).select("date", "symbol", "method", "score", "raw_score", "family_count", "factor_count")


def _deterministic_standardize(scores: pl.DataFrame) -> pl.DataFrame:
    parts = []
    for frame in scores.sort("date", "symbol").partition_by("date", maintain_order=True):
        values = frame.get_column("raw_score").to_numpy()
        mean = float(np.mean(values))
        std = float(np.std(values, ddof=0))
        standardized = np.zeros(len(values)) if std == 0.0 else (values - mean) / std
        parts.append(frame.with_columns(pl.Series("score", standardized)))
    return pl.concat(parts).select(
        "date", "symbol", "method", "score", "raw_score", "family_count", "factor_count"
    )
